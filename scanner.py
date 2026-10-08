"""Public Spot market discovery and descriptive activity ranking, no trading."""
from bisect import bisect_left, bisect_right
from collections import defaultdict, OrderedDict
import math
import threading
import time

from terminal import observations


def active_markets(info):
    return {s['symbol']: {'symbol': s['symbol'], 'base': s['baseAsset'], 'quote': s['quoteAsset']}
            for s in info['symbols']
            if s.get('status') == 'TRADING' and s.get('isSpotTradingAllowed') is True}


def rank_markets(catalog, tickers, now_ms):
    rows = []
    for item in tickers:
        symbol = item.get('symbol')
        if symbol not in catalog:
            continue
        try:
            last, opening, high, low, volume, bid, ask, change = [float(item[k]) for k in
                ('lastPrice', 'openPrice', 'highPrice', 'lowPrice', 'quoteVolume', 'bidPrice', 'askPrice', 'priceChangePercent')]
            count, close = int(item['count']), int(item['closeTime'])
            if not all(math.isfinite(v) for v in (last, opening, high, low, volume, bid, ask, change)):
                continue
            if min(last, opening, high, low, volume) <= 0 or high < low or count <= 0:
                continue
            if not -60000 <= now_ms-close <= 180000:
                continue
            spread = (ask-bid)/((ask+bid)/2)*100 if 0 < bid <= ask else None
            rows.append({**catalog[symbol], 'price': last, 'changePct': change,
                         'rangePct': (high-low)/opening*100, 'quoteVolume': volume,
                         'trades': count, 'spreadPct': spread, 'closeTime': close})
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    groups = defaultdict(list)
    for row in rows:
        groups[row['quote']].append(row)
    for group in groups.values():
        ordered = {key: sorted(r[key] for r in group) for key in ('rangePct','quoteVolume','trades')}
        for row in group:
            def percentile(key):
                values = ordered[key]
                if len(values) == 1: return 0.5
                return (bisect_left(values,row[key])+bisect_right(values,row[key])-1)/(2*(len(values)-1))
            score = 100*(.45*percentile('rangePct')+.35*percentile('quoteVolume')+.20*percentile('trades'))
            row['score'] = round(score/(1+row['spreadPct']/.1),2) if row['spreadPct'] is not None else 0
    return sorted(rows, key=lambda row: (-row['score'],row['symbol']))


class Scanner:
    def __init__(self, public_get, fetch_candles):
        self.public_get, self.fetch_candles = public_get, fetch_candles
        self.lock = threading.Lock()
        self.market_lock = threading.Lock()
        self.catalog, self.rows = {}, []
        self.catalog_at = 0
        self.updated_at = None
        self.error = None
        self.cache = OrderedDict()
        self.next_market = 0

    def refresh(self):
        try:
            if not self.catalog or time.monotonic()-self.catalog_at > 3600:
                catalog = active_markets(self.public_get('exchangeInfo', {'permissions':'SPOT','showPermissionSets':'false'}))
                if not catalog: raise ValueError('Empty market catalog')
                with self.lock:
                    self.catalog, self.catalog_at = catalog, time.monotonic()
            tickers = self.public_get('ticker/24hr')
            now = int(time.time()*1000)
            rows = rank_markets(self.catalog,tickers,now)
            if not rows: raise ValueError('No current market data')
            with self.lock:
                self.rows, self.updated_at, self.error = rows, now, None
        except Exception:
            with self.lock:
                self.error = 'Skaner chwilowo niedostępny. Ponawiam automatycznie.'

    def run(self, stop):
        while not stop.is_set():
            self.refresh()
            stop.wait(60 if self.error is None else 120)

    def snapshot(self):
        with self.lock:
            fresh = bool(self.updated_at and int(time.time()*1000)-self.updated_at <= 180000 and not self.error)
            return {'state': 'live' if fresh else 'error' if self.error else 'starting' if not self.updated_at else 'stale',
                    'fresh': fresh, 'updatedAt': self.updated_at, 'error': self.error,
                    'activeCount': len(self.catalog), 'rankedCount': len(self.rows),
                    'markets': sorted(self.catalog.values(),key=lambda row:row['symbol']),
                    'rows': self.rows if fresh else [], 'refreshSeconds':60}

    def market(self, symbol):
        with self.lock:
            metadata = self.catalog.get(symbol)
        if not metadata:
            return {'error':'Pary nie ma w katalogu aktywnych rynków Spot.'},404
        if not self.market_lock.acquire(blocking=False):
            return {'error':'Trwa pobieranie świec. Spróbuj za chwilę.'},429
        try:
            cached = self.cache.get(symbol)
            if cached and time.monotonic()-cached['at'] < 60:
                self.cache.move_to_end(symbol)
                bars = cached['bars']
            else:
                if time.monotonic() < self.next_market:
                    return {'error':'Odczekaj sekundę przed kolejną parą.'},429
                self.next_market = time.monotonic()+1
                now = int(self.public_get('time')['serverTime'])
                last = now//60000*60000-60000
                bars = self.fetch_candles(symbol,last-119*60000,last+59999)
                if not bars or len(bars) > 120 or bars[-1]['time'] != last:
                    raise ValueError('Missing candles')
                for i, bar in enumerate(bars):
                    if bar['time'] != last-(len(bars)-1-i)*60000 or bar['end'] != bar['time']+59999:
                        raise ValueError('Candle gap')
                    if any(not math.isfinite(bar[k]) or bar[k] <= 0 for k in ('open','high','low','close')):
                        raise ValueError('Invalid candle')
                self.cache[symbol] = {'at':time.monotonic(),'bars':bars}
                self.cache.move_to_end(symbol)
                while len(self.cache) > 64: self.cache.popitem(last=False)
            return {'market':metadata, 'bars':bars,
                    'signals':observations({symbol:bars},bars[-1]['time'],'live')},200
        except Exception:
            self.cache.pop(symbol,None)
            self.next_market = time.monotonic()+5
            return {'error':'Nie można pobrać aktualnych świec tej pary. Spróbuj ponownie.'},502
        finally:
            self.market_lock.release()
