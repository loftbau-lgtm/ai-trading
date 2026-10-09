"""Shared, public-only Binance Spot market data with bounded host failover."""
import json
import logging
import math
import socket
import threading
import time
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_HOSTS = (
    'https://data-api.binance.vision/api/v3/',
    'https://api.binance.com/api/v3/',
    'https://api1.binance.com/api/v3/',
    'https://api2.binance.com/api/v3/',
    'https://api3.binance.com/api/v3/',
)
WEIGHTS = {'time': 1, 'klines': 2, 'exchangeInfo': 20, 'ticker/24hr': 80}


class MarketDataUnavailable(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _error_code(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return 'TIMEOUT'
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'TIMEOUT'
    if isinstance(reason, socket.gaierror):
        return 'DNS_ERROR'
    if getattr(reason, 'winerror', None) == 10013 or getattr(exc, 'winerror', None) == 10013:
        return 'LOCAL_SOCKET_ACCESS_DENIED'
    return 'HOST_UNAVAILABLE'


def _valid(endpoint, data):
    if endpoint == 'time':
        return isinstance(data, dict) and isinstance(data.get('serverTime'), (int, float)) and not isinstance(data['serverTime'], bool) and math.isfinite(data['serverTime'])
    if endpoint == 'klines':
        if not isinstance(data, list):
            return False
        try:
            for row in data:
                if not isinstance(row, list) or len(row) < 9:
                    return False
                if not all(math.isfinite(float(row[i])) for i in range(9)):
                    return False
                if int(row[6]) < int(row[0]) or int(row[8]) < 0 or any(float(row[i]) <= 0 for i in (1, 2, 3, 4)):
                    return False
        except (ValueError, TypeError, OverflowError):
            return False
        return True
    if endpoint == 'exchangeInfo':
        return isinstance(data, dict) and isinstance(data.get('symbols'), list) and all(
            isinstance(item, dict) and all(key in item for key in
                ('symbol', 'baseAsset', 'quoteAsset', 'status', 'isSpotTradingAllowed'))
            for item in data['symbols'])
    if endpoint == 'ticker/24hr':
        rows = data if isinstance(data, list) else [data]
        if not rows:
            return False
        try:
            return all(isinstance(item, dict) and isinstance(item.get('symbol'), str)
                and all(math.isfinite(float(item[key])) for key in
                    ('lastPrice', 'openPrice', 'highPrice', 'lowPrice', 'quoteVolume',
                     'bidPrice', 'askPrice', 'priceChangePercent'))
                and int(item['count']) >= 0 and int(item['closeTime']) > 0 for item in rows)
        except (KeyError, ValueError, TypeError, OverflowError):
            return False
    return False


class PublicMarketDataClient:
    def __init__(self, hosts=DEFAULT_HOSTS, opener=None, clock=None, timeout=20, recovery_seconds=60):
        self.hosts = tuple(host.rstrip('/') + '/' for host in hosts)
        if not self.hosts or len(set(self.hosts)) != len(self.hosts) or any(host not in DEFAULT_HOSTS for host in self.hosts):
            raise ValueError('Only configured public Binance Spot API hosts are allowed')
        self.opener = opener or urlopen
        self.clock = clock or time.monotonic
        self.timeout = timeout
        self.recovery_seconds = recovery_seconds
        self.lock = threading.RLock()
        self.active_host = self.hosts[0]
        self.failover_count = 0
        self.rate_resume_at = 0.0
        self.next_retry_at = 0.0
        self.outage_failures = 0
        self.weights = deque()
        self.health = {host: dict(state='UNAVAILABLE', lastSuccess=None, lastFailure=None,
            failureCount=0, consecutiveFailures=0, cooldownUntil=None, lastLatencyMs=None,
            lastError=None, _cooldown_at=None) for host in self.hosts}

    def _reserve_weight(self, endpoint):
        now = self.clock()
        with self.lock:
            if now < self.rate_resume_at:
                raise MarketDataUnavailable('RATE_LIMIT', 'Public API cooldown')
            if now < self.next_retry_at:
                raise MarketDataUnavailable('ALL_HOSTS_UNAVAILABLE', 'Public API retry backoff')
            while self.weights and self.weights[0][0] <= now - 60:
                self.weights.popleft()
            weight = WEIGHTS[endpoint]
            if sum(item[1] for item in self.weights) + weight > 2400:
                self.rate_resume_at = max(self.rate_resume_at, self.weights[0][0] + 60)
                raise MarketDataUnavailable('RATE_LIMIT', 'Public API local weight budget')
            self.weights.append((now, weight))

    def _failed(self, host, code, latency):
        now = self.clock()
        with self.lock:
            item = self.health[host]
            item.update(state='COOLDOWN', lastFailure=int(time.time() * 1000),
                failureCount=item['failureCount'] + 1,
                consecutiveFailures=item['consecutiveFailures'] + 1,
                cooldownUntil=int(time.time() * 1000 + self.recovery_seconds * 1000),
                _cooldown_at=now + self.recovery_seconds,
                lastLatencyMs=round(latency, 1), lastError=code)

    def get(self, endpoint, params=None):
        if endpoint not in WEIGHTS:
            raise ValueError('Public market data endpoint not allowed')
        with self.lock:
            if self.clock() < self.next_retry_at:
                raise MarketDataUnavailable('ALL_HOSTS_UNAVAILABLE', 'Public API retry backoff')
            active = self.active_host
            order = (active,) + tuple(host for host in self.hosts if host != active)
        attempted = []
        for host in order:
            with self.lock:
                cooldown_at = self.health[host]['_cooldown_at']
                if cooldown_at is not None and self.clock() < cooldown_at:
                    continue
            self._reserve_weight(endpoint)
            started = self.clock()
            try:
                request = Request(host + endpoint + '?' + urlencode(params or {}),
                    headers={'User-Agent': 'QuantLabAI/1.0'})
                with self.opener(request, timeout=self.timeout) as response:
                    data = json.load(response)
                if not _valid(endpoint, data):
                    raise MarketDataUnavailable('INVALID_RESPONSE')
            except HTTPError as exc:
                if exc.code in (418, 429):
                    try:
                        delay = max(120, int(exc.headers.get('Retry-After', '120')))
                    except (ValueError, TypeError):
                        delay = 120
                    with self.lock:
                        self.rate_resume_at = max(self.rate_resume_at, self.clock() + delay)
                        self.health[host].update(state='COOLDOWN',
                            cooldownUntil=int(time.time() * 1000 + delay * 1000), _cooldown_at=self.rate_resume_at,
                            lastFailure=int(time.time() * 1000), lastError='RATE_LIMIT')
                    logging.warning('Binance rate limit host=%s endpoint=%s retryAfter=%ss', host, endpoint, delay)
                    raise MarketDataUnavailable('RATE_LIMIT') from exc
                if exc.code < 500:
                    raise
                code = 'HOST_UNAVAILABLE'
            except (URLError, OSError, MarketDataUnavailable, ValueError, json.JSONDecodeError) as exc:
                code = exc.code if isinstance(exc, MarketDataUnavailable) else (
                    'INVALID_RESPONSE' if isinstance(exc, (ValueError, json.JSONDecodeError)) else _error_code(exc))
            else:
                latency = (self.clock() - started) * 1000
                with self.lock:
                    item = self.health[host]
                    recovered = item['failureCount'] > 0 and item['consecutiveFailures'] > 0
                    item.update(state='HEALTHY', lastSuccess=int(time.time() * 1000),
                        consecutiveFailures=0, cooldownUntil=None, _cooldown_at=None,
                        lastLatencyMs=round(latency, 1), lastError=None)
                    if host != self.active_host and self.health[self.active_host]['consecutiveFailures']:
                        logging.warning('Binance market data failover %s -> %s endpoint=%s latencyMs=%.1f',
                            self.active_host, host, endpoint, latency)
                        self.active_host = host
                        self.failover_count += 1
                    elif recovered:
                        logging.info('Binance market data recovered host=%s endpoint=%s latencyMs=%.1f', host, endpoint, latency)
                    self.outage_failures = 0
                    self.next_retry_at = 0.0
                return data
            latency = (self.clock() - started) * 1000
            self._failed(host, code, latency)
            attempted.append(code)
            logging.warning('Binance market data failure host=%s endpoint=%s error=%s latencyMs=%.1f',
                host, endpoint, code, latency)
        with self.lock:
            self.outage_failures += 1
            delay = min(60, 5 * 2 ** min(self.outage_failures - 1, 4))
            self.next_retry_at = self.clock() + delay
            # During a total outage retry after the global backoff, while an
            # individually failed host keeps its longer cooldown after recovery.
            for host in self.hosts:
                item = self.health[host]
                if item['_cooldown_at'] is not None:
                    item['_cooldown_at'] = min(item['_cooldown_at'], self.next_retry_at)
                    item['cooldownUntil'] = int(time.time() * 1000 + delay * 1000)
        raise MarketDataUnavailable('ALL_HOSTS_UNAVAILABLE', ','.join(attempted) or 'Hosts cooling down')

    def probe(self):
        try:
            self.get('time')
        except Exception as exc:
            logging.warning('Binance startup probe unavailable: %s', type(exc).__name__)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            hosts = []
            for host, item in self.health.items():
                entry = {key: value for key, value in item.items() if not key.startswith('_')}
                entry['host'] = host
                if item['_cooldown_at'] is not None and now < item['_cooldown_at']:
                    entry['state'] = 'COOLDOWN'
                elif entry['consecutiveFailures']:
                    entry['state'] = 'DEGRADED' if entry['lastSuccess'] else 'UNAVAILABLE'
                hosts.append(entry)
            blocked = all(item['lastError'] == 'LOCAL_SOCKET_ACCESS_DENIED' for item in self.health.values())
            rate_limited = now < self.rate_resume_at
            unavailable = self.outage_failures > 0 and not any(
                item['lastSuccess'] and not item['consecutiveFailures'] for item in self.health.values())
            return dict(activeHost=self.active_host, state='RATE_LIMIT' if rate_limited else
                'MARKET_DATA_UNAVAILABLE' if unavailable else
                next(item['state'] for item in hosts if item['host'] == self.active_host),
                failoverCount=self.failover_count, hosts=hosts,
                likelyLocalNetworkBlock=blocked, diagnostic='LOCAL_SOCKET_ACCESS_DENIED' if blocked else
                'RATE_LIMIT' if rate_limited else 'ALL_HOSTS_UNAVAILABLE' if unavailable else None,
                retryInSeconds=max(0, round(self.next_retry_at - now, 1)),
                rateLimitRetryInSeconds=max(0, round(self.rate_resume_at - now, 1)))
