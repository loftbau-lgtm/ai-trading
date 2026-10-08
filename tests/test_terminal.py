import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

from engine import SYMBOLS
from terminal import BinanceReadOnly, observations, WATCH_SYMBOLS, validate_history


class TerminalTests(unittest.TestCase):
    def test_additional_pairs_and_missing_history(self):
        self.assertEqual(len(WATCH_SYMBOLS), 11)
        self.assertEqual(len(set(WATCH_SYMBOLS)), 11)
        bars = [{'close': 100, 'high': 101, 'low': 99}]*120
        data = observations({'BNBUSDT': bars},60000,'live',120000)
        self.assertTrue(data['fresh'])
        self.assertEqual(len(data['items']),6)
        self.assertTrue(all(s['symbol']=='BNBUSDT' for s in data['items']))
        self.assertFalse(observations({'BNBUSDT': []},60000,'live',120000)['fresh'])

    def test_watchlist_history_rejects_gaps_and_invalid_prices(self):
        bars = [dict(time=i*60000,end=i*60000+59999,open=100,high=101,low=99,close=100) for i in range(120)]
        validate_history(bars,119*60000)
        with self.assertRaises(ValueError): validate_history(bars[:-1],119*60000)
        bars[3]['time'] += 60000
        with self.assertRaises(ValueError): validate_history(bars,119*60000)
        bars[3]['time'] -= 60000
        bars[3]['close'] = float('nan')
        with self.assertRaises(ValueError): validate_history(bars,119*60000)

    def test_signals_require_fresh_live_data(self):
        bars = {s: [{'close': 100, 'high': 101, 'low': 99}]*120 for s in SYMBOLS}
        data = observations(bars, 60000, 'live', 120000)
        self.assertTrue(data['fresh'])
        self.assertEqual(len(data['items']), 18)
        for status, now in [('error', 120000), ('live', 300000), ('live', 0)]:
            data = observations(bars, 60000, status, now)
            self.assertFalse(data['fresh'])
            self.assertTrue(all(s['signal'] == 'UNAVAILABLE' for s in data['items']))

    def test_account_requires_separate_strong_token(self):
        client = BinanceReadOnly()
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(client.authorized(''))
            self.assertEqual(client.snapshot()['state'], 'not_configured')
        with patch.dict(os.environ, {'QUANTLAB_ACCOUNT_TOKEN': 'short'}):
            self.assertFalse(client.authorized('Bearer short'))
        with patch.dict(os.environ, {'QUANTLAB_ACCOUNT_TOKEN': 'x'*40}):
            self.assertTrue(client.authorized('Bearer ' + 'x'*40))
            self.assertFalse(client.authorized('Bearer ' + 'y'*40))

    def test_orders_cannot_be_submitted(self):
        with patch('terminal.build_opener') as opener:
            with self.assertRaises(ValueError):
                BinanceReadOnly()._get('/api/v3/order', {'side': 'BUY'})
            opener.assert_not_called()

    def test_hmac_get_request(self):
        with patch.dict(os.environ, {'BINANCE_API_KEY': 'test-key', 'BINANCE_API_SECRET': 'test-secret'}), patch('terminal.time.time', return_value=123), patch('terminal.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{}'
            BinanceReadOnly()._get('/api/v3/account')
            request = opener.return_value.open.call_args.args[0]
            signature = hmac.new(b'test-secret', b'timestamp=123000&recvWindow=5000', hashlib.sha256).hexdigest()
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.full_url, 'https://api.binance.com/api/v3/account?timestamp=123000&recvWindow=5000&signature=' + signature)

    def test_cache_and_no_secret_or_upstream_error_leak(self):
        env = {'BINANCE_API_KEY': 'test-key', 'BINANCE_API_SECRET': 'test-secret', 'QUANTLAB_ACCOUNT_TOKEN': 'x'*40}
        with patch.dict(os.environ, env), patch.object(BinanceReadOnly, '_get', side_effect=Exception('sensitive signed URL')) as fetch:
            client = BinanceReadOnly()
            first = client.snapshot()
            self.assertEqual(first, client.snapshot())
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn('sensitive', str(first))
            self.assertEqual(first['balances'], [])

    def test_only_whitelisted_account_fields_are_returned(self):
        env = {'BINANCE_API_KEY': 'test-key', 'BINANCE_API_SECRET': 'test-secret', 'QUANTLAB_ACCOUNT_TOKEN': 'x'*40}
        with patch.dict(os.environ, env), patch.object(BinanceReadOnly, '_get', side_effect=[{'uid': 123, 'balances': [{'asset': 'USDT', 'free': '20', 'locked': '0'}, {'asset': 'BTC', 'free': '0', 'locked': '0'}]}, [], [], []]) as fetch:
            client = BinanceReadOnly()
            data = client.snapshot()
            self.assertEqual(data['state'], 'connected')
            self.assertEqual(len(data['balances']), 1)
            self.assertNotIn('uid', data)
            self.assertEqual(data, client.snapshot())
            self.assertEqual(fetch.call_count, 4)


if __name__ == '__main__':
    unittest.main()
