import io
import json
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError

from market_data import DEFAULT_HOSTS, MarketDataUnavailable, PublicMarketDataClient


HOSTS = DEFAULT_HOSTS


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class MarketDataTests(unittest.TestCase):
    def client(self, opener, clock=None):
        return PublicMarketDataClient(HOSTS, opener=opener, clock=clock)

    def test_primary_success_and_active_host_retained(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            return Response({'serverTime': 123})
        client = self.client(opener)
        self.assertEqual(client.get('time')['serverTime'], 123)
        self.assertEqual(client.get('time')['serverTime'], 123)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(url.startswith(HOSTS[0]) for url in calls))
        self.assertEqual(client.snapshot()['failoverCount'], 0)
        self.assertEqual(client.snapshot()['hosts'][0]['state'], 'HEALTHY')

    def test_failover_to_secondary_and_keep_it(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.startswith(HOSTS[0]):
                raise URLError('offline')
            return Response({'serverTime': 123})
        client = self.client(opener)
        client.get('time')
        client.get('time')
        self.assertEqual([url.split('/api/')[0] for url in calls],
            [HOSTS[0].split('/api/')[0], HOSTS[1].split('/api/')[0], HOSTS[1].split('/api/')[0]])
        self.assertEqual(client.snapshot()['activeHost'], HOSTS[1])
        self.assertEqual(client.snapshot()['failoverCount'], 1)

    def test_first_three_fail_fourth_succeeds(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            if any(request.full_url.startswith(host) for host in HOSTS[:3]):
                raise ConnectionRefusedError()
            return Response({'serverTime': 123})
        client = self.client(opener)
        client.get('time')
        self.assertEqual(len(calls), 4)
        self.assertEqual(client.snapshot()['activeHost'], HOSTS[3])

    def test_all_hosts_fail_with_exponential_backoff(self):
        clock = Clock()
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            raise ConnectionResetError()
        client = self.client(opener, clock)
        with self.assertRaises(MarketDataUnavailable) as error:
            client.get('time')
        self.assertEqual(error.exception.code, 'ALL_HOSTS_UNAVAILABLE')
        self.assertEqual(len(calls), 5)
        self.assertEqual(client.snapshot()['retryInSeconds'], 5)
        with self.assertRaises(MarketDataUnavailable):
            client.get('time')
        self.assertEqual(len(calls), 5)
        for index, (elapsed, next_delay) in enumerate(((5, 10), (10, 20), (20, 40), (40, 60), (60, 60)), 2):
            clock.advance(elapsed)
            with self.assertRaises(MarketDataUnavailable):
                client.get('time')
            self.assertEqual(len(calls), index * 5)
            self.assertEqual(client.snapshot()['retryInSeconds'], next_delay)

    def test_winerror_10013_identifies_local_block(self):
        error = OSError('socket blocked')
        error.winerror = 10013
        client = self.client(lambda request, timeout: (_ for _ in ()).throw(error))
        with self.assertRaises(MarketDataUnavailable):
            client.get('time')
        health = client.snapshot()
        self.assertTrue(health['likelyLocalNetworkBlock'])
        self.assertEqual(health['diagnostic'], 'LOCAL_SOCKET_ACCESS_DENIED')
        self.assertTrue(all(host['lastError'] == 'LOCAL_SOCKET_ACCESS_DENIED' for host in health['hosts']))

    def test_dns_and_timeout_classification(self):
        for error, code in ((URLError(socket.gaierror('dns')), 'DNS_ERROR'),
                            (socket.timeout(), 'TIMEOUT')):
            with self.subTest(code=code):
                client = self.client(lambda request, timeout: (_ for _ in ()).throw(error))
                with self.assertRaises(MarketDataUnavailable):
                    client.get('time')
                self.assertEqual(client.snapshot()['hosts'][0]['lastError'], code)

    def test_http_500_fails_over(self):
        def opener(request, timeout):
            if request.full_url.startswith(HOSTS[0]):
                raise HTTPError(request.full_url, 500, 'failure', {}, None)
            return Response({'serverTime': 123})
        self.assertEqual(self.client(opener).get('time')['serverTime'], 123)

    def test_http_429_does_not_fail_over_and_respects_retry_after(self):
        clock = Clock()
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 429, 'limit', {'Retry-After': '180'}, None)
        client = self.client(opener, clock)
        with self.assertRaises(MarketDataUnavailable) as error:
            client.get('time')
        self.assertEqual(error.exception.code, 'RATE_LIMIT')
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.snapshot()['rateLimitRetryInSeconds'], 180)
        with self.assertRaises(MarketDataUnavailable):
            client.get('time')
        self.assertEqual(len(calls), 1)

    def test_invalid_json_and_structure_fail_over(self):
        for first in (lambda: io.BytesIO(b'{invalid'), lambda: Response({'bad': 1})):
            with self.subTest(first=first):
                def opener(request, timeout):
                    return first() if request.full_url.startswith(HOSTS[0]) else Response({'serverTime': 123})
                client = self.client(opener)
                self.assertEqual(client.get('time')['serverTime'], 123)
                self.assertEqual(client.snapshot()['hosts'][0]['lastError'], 'INVALID_RESPONSE')

    def test_response_validation_and_allowlist(self):
        client = self.client(lambda request, timeout: Response([]))
        with self.assertRaises(ValueError):
            client.get('account')
        self.assertEqual(client.get('klines'), [])
        with self.assertRaises(MarketDataUnavailable):
            client.get('ticker/24hr')
        ticker = {'symbol': 'BTCUSDT', 'count': 1, 'closeTime': 123, **{
            key: '1' for key in ('lastPrice', 'openPrice', 'highPrice', 'lowPrice',
                'quoteVolume', 'bidPrice', 'askPrice', 'priceChangePercent')}}
        client = self.client(lambda request, timeout: Response([ticker]))
        self.assertEqual(client.get('ticker/24hr')[0]['symbol'], 'BTCUSDT')
        client = self.client(lambda request, timeout: Response({}))
        with self.assertRaises(MarketDataUnavailable):
            client.get('exchangeInfo')

    def test_cooldown_recovery_after_active_host_fails(self):
        clock = Clock()
        failed = {HOSTS[0], HOSTS[2], HOSTS[3], HOSTS[4]}
        def opener(request, timeout):
            if any(request.full_url.startswith(host) for host in failed):
                raise URLError('offline')
            return Response({'serverTime': 123})
        client = self.client(opener, clock)
        client.get('time')
        self.assertEqual(client.snapshot()['activeHost'], HOSTS[1])
        clock.advance(61)
        failed.remove(HOSTS[0])
        failed.add(HOSTS[1])
        client.get('time')
        self.assertEqual(client.snapshot()['activeHost'], HOSTS[0])
        self.assertEqual(client.snapshot()['hosts'][0]['state'], 'HEALTHY')

    def test_thread_safety_and_shared_weight_budget(self):
        calls = 0
        lock = threading.Lock()
        def opener(request, timeout):
            nonlocal calls
            with lock:
                calls += 1
            return Response({'serverTime': 123})
        client = self.client(opener)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: client.get('time')['serverTime'], range(100)))
        self.assertEqual(results, [123] * 100)
        self.assertEqual(calls, 100)
        self.assertEqual(len(client.weights), 100)

    def test_shared_rate_budget_stops_requests_without_host_switch(self):
        calls = 0
        def opener(request, timeout):
            nonlocal calls
            calls += 1
            return Response({'serverTime': 123})
        client = self.client(opener, Clock())
        for _ in range(2400):
            client.get('time')
        with self.assertRaises(MarketDataUnavailable) as error:
            client.get('time')
        self.assertEqual(error.exception.code, 'RATE_LIMIT')
        self.assertEqual(calls, 2400)
        self.assertEqual(client.snapshot()['state'], 'RATE_LIMIT')
        self.assertEqual(client.snapshot()['failoverCount'], 0)


if __name__ == '__main__':
    unittest.main()
