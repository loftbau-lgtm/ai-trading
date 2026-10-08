"""Public strategy observations and a strictly read-only Binance adapter."""
import hashlib
import hmac
import json
import math
import os
import threading
import time
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from engine import SYMBOLS, STRATEGIES, signal

WATCH_SYMBOLS = SYMBOLS + ('BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT',
                          'AVAXUSDT', 'LINKUSDT', 'DOTUSDT', 'LTCUSDT')


def validate_history(bars, last):
    if len(bars) != 120 or bars[-1]['time'] != last:
        raise ValueError('Incomplete market history')
    for i, bar in enumerate(bars):
        if bar['time'] != last-(119-i)*60000 or bar['end'] != bar['time']+59999:
            raise ValueError('Non-contiguous closed candles')
        if any(not math.isfinite(bar[k]) or bar[k] <= 0 for k in ('open','high','low','close')):
            raise ValueError('Invalid market prices')


def observations(markets, cursor, market_status, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    fresh = bool(cursor and 0 <= now_ms - cursor <= 180000 and market_status in ('live', 'syncing')
                 and markets and all(len(bars) >= 100 for bars in markets.values()))
    return {'fresh': fresh, 'candleTime': cursor, 'items': [
        {'symbol': symbol, 'strategy': strategy,
         'signal': signal(strategy, markets[symbol]) if fresh else 'UNAVAILABLE'}
        for symbol in markets for strategy in STRATEGIES
    ]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward API credentials to a redirect target.
        return None


class BinanceReadOnly:
    BASE = 'https://api.binance.com'
    PATHS = frozenset(('/api/v3/account', '/api/v3/openOrders'))

    def __init__(self):
        self.lock = threading.Lock()
        self.cached = None
        self.next_request = 0

    @staticmethod
    def configured():
        return all(os.environ.get(k, '').strip() for k in
                   ('BINANCE_API_KEY', 'BINANCE_API_SECRET', 'QUANTLAB_ACCOUNT_TOKEN'))

    @staticmethod
    def authorized(header):
        token = os.environ.get('QUANTLAB_ACCOUNT_TOKEN', '')
        return bool(len(token) >= 32 and hmac.compare_digest(
            header.encode('utf-8'), ('Bearer ' + token).encode('utf-8')))

    def _get(self, path, params=None):
        if path not in self.PATHS:
            raise ValueError('Only account and open-order reads are supported')
        query = urlencode({**(params or {}), 'timestamp': int(time.time()*1000), 'recvWindow': 5000})
        signature = hmac.new(os.environ['BINANCE_API_SECRET'].encode(), query.encode(), hashlib.sha256).hexdigest()
        request = Request(self.BASE + path + '?' + query + '&signature=' + signature,
                          headers={'X-MBX-APIKEY': os.environ['BINANCE_API_KEY']}, method='GET')
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            return json.load(response)

    def snapshot(self):
        if not self.configured():
            return {'state': 'not_configured', 'readOnly': True, 'balances': [], 'orders': []}
        with self.lock:
            if time.monotonic() < self.next_request:
                return self.cached
            try:
                account = self._get('/api/v3/account', {'omitZeroBalances': 'true'})
                orders = []
                for symbol in SYMBOLS:
                    orders.extend(self._get('/api/v3/openOrders', {'symbol': symbol}))
                balances = [{k: b[k] for k in ('asset', 'free', 'locked')}
                            for b in account['balances']
                            if Decimal(b['free']) > 0 or Decimal(b['locked']) > 0]
                fields = ('symbol', 'orderId', 'side', 'type', 'status', 'price', 'origQty', 'executedQty')
                self.cached = {'state': 'connected', 'readOnly': True, 'balances': balances,
                               'orders': [{k: o.get(k) for k in fields} for o in orders],
                               'updatedAt': int(time.time()*1000)}
                self.next_request = time.monotonic() + 30
            except Exception:
                # Do not expose signed URLs, credentials or upstream error bodies.
                self.cached = {'state': 'error', 'readOnly': True, 'balances': [], 'orders': [],
                               'message': 'Odczyt Binance nie powiódł się. Sprawdź konfigurację, uprawnienia i zegar serwera.'}
                self.next_request = time.monotonic() + 60
            return self.cached
