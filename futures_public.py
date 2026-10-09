"""Public USDⓈ-M Futures market data only. No signed/private endpoints."""
import json
import math
import urllib.parse
import urllib.request


BASE_URL = 'https://fapi.binance.com'
PUBLIC_PATHS = frozenset({
    '/fapi/v1/time', '/fapi/v1/exchangeInfo', '/fapi/v1/klines',
    '/fapi/v1/ticker/bookTicker', '/fapi/v1/ticker/price',
    '/fapi/v1/depth', '/fapi/v1/aggTrades', '/fapi/v1/premiumIndex',
    '/fapi/v1/fundingRate', '/fapi/v1/openInterest',
})


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError('Invalid Futures market price/quantity')
    return number


class FuturesPublicClient:
    def __init__(self, reader=None):
        self.reader = reader or self._read_http
        self._catalog = None
        self._catalog_at = 0

    def _read_http(self, path, params):
        if path not in PUBLIC_PATHS:
            raise ValueError('Non-public Futures endpoint rejected')
        url = BASE_URL + path + ('?' + urllib.parse.urlencode(params) if params else '')
        request = urllib.request.Request(url, headers={'User-Agent':'QuantLab-PAPER/1.0'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=6) as response:
            if response.status != 200:
                raise ValueError('Futures public data HTTP failure')
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise ValueError('Futures public response too large')
            return json.loads(body)

    def get(self, path, params=None):
        if path not in PUBLIC_PATHS:
            raise ValueError('Non-public Futures endpoint rejected')
        return self.reader(path, params or {})

    def catalog(self, now):
        if self._catalog is None or now-self._catalog_at > 3600000:
            data = self.get('/fapi/v1/exchangeInfo')
            symbols = {row['symbol'] for row in data['symbols'] if
                       row.get('status') == 'TRADING' and
                       row.get('contractType') == 'PERPETUAL' and
                       row.get('quoteAsset') == 'USDT'}
            self._catalog, self._catalog_at = symbols, now
        return self._catalog

    def snapshot(self, symbols=('BTCUSDT','ETHUSDT','SOLUSDT'), funding_since=None):
        for attempt in range(2):
            try:return self._snapshot_once(symbols,funding_since)
            except ValueError as exc:
                transient=('crossed candle boundary','closed candle is stale')
                if attempt or not any(reason in str(exc) for reason in transient):
                    raise

    def _snapshot_once(self, symbols, funding_since):
        now = int(self.get('/fapi/v1/time')['serverTime'])
        available = self.catalog(now)
        if any(symbol not in available for symbol in symbols):
            raise ValueError('Futures symbol unavailable')
        result = {}
        for symbol in symbols:
            raw = self.get('/fapi/v1/klines',dict(symbol=symbol,interval='1m',limit=120))
            bars = [dict(time=int(row[0]),open=positive(row[1]),high=positive(row[2]),
                low=positive(row[3]),close=positive(row[4]),volume=float(row[5]),end=int(row[6]))
                for row in raw if int(row[6]) < now]
            if len(bars) < 61 or any(b['time']-a['time'] != 60000 for a,b in zip(bars,bars[1:])):
                raise ValueError('Futures candle history incomplete')
            quote = self.get('/fapi/v1/ticker/bookTicker',dict(symbol=symbol))
            contract = self.get('/fapi/v1/ticker/price',dict(symbol=symbol))
            premium = self.get('/fapi/v1/premiumIndex',dict(symbol=symbol))
            oi = self.get('/fapi/v1/openInterest',dict(symbol=symbol))
            depth = self.get('/fapi/v1/depth',dict(symbol=symbol,limit=5))
            trades = self.get('/fapi/v1/aggTrades',dict(symbol=symbol,limit=20))
            bid,ask = positive(quote['bidPrice']),positive(quote['askPrice'])
            if bid >= ask:
                raise ValueError('Invalid Futures book ticker')
            mark,index = positive(premium['markPrice']),positive(premium['indexPrice'])
            quote_time = int(quote.get('time',now))
            mark_time = int(premium['time'])
            oi_time = int(oi['time'])
            contract_time=int(contract.get('time',now))
            if any(t > now+30000 or now-t > 30000 for t in
                   (quote_time,mark_time,oi_time,contract_time)):
                raise ValueError('Stale or future Futures market sample')
            last = bars[-1]['time']
            if last != now//60000*60000-60000:
                raise ValueError('Futures closed candle is stale')
            bid_depth=sum(float(item[1]) for item in depth.get('bids',[]))
            ask_depth=sum(float(item[1]) for item in depth.get('asks',[]))
            buy_flow=sum(float(item['q']) for item in trades if not item.get('m'))
            sell_flow=sum(float(item['q']) for item in trades if item.get('m'))
            settled=[]
            if funding_since and symbol in funding_since:
                start = int(funding_since[symbol])+1
                settled = self.get('/fapi/v1/fundingRate',dict(symbol=symbol,
                    startTime=start,endTime=now,limit=100))
                if len(settled) == 100:
                    raise ValueError('Funding history may be truncated; do not skip accrual')
                settled = [dict(fundingTime=int(row['fundingTime']),
                    fundingRate=float(row['fundingRate']),markPrice=positive(row['markPrice']))
                    for row in settled if int(row['fundingTime']) <= now]
            result[symbol] = dict(symbol=symbol, bars=bars, timestamp=now,
                contractPrice=positive(contract['price']),contractTime=contract_time,
                markPrice=mark,markTime=mark_time,indexPrice=index,
                bid=bid,ask=ask,bidQty=positive(quote['bidQty']),askQty=positive(quote['askQty']),
                fundingRate=float(premium['lastFundingRate']),
                nextFundingTime=int(premium['nextFundingTime']),
                openInterest=positive(oi['openInterest']),openInterestTime=oi_time,
                quoteTime=quote_time,
                orderBookImbalance=(bid_depth-ask_depth)/(bid_depth+ask_depth) if bid_depth+ask_depth else None,
                takerImbalance=(buy_flow-sell_flow)/(buy_flow+sell_flow) if buy_flow+sell_flow else None,
                settledFunding=settled)
        decision_now=int(self.get('/fapi/v1/time')['serverTime'])
        if any(m['bars'][-1]['time'] != decision_now//60000*60000-60000 or
               any(decision_now-t < 0 or decision_now-t > 30000 for t in
                   (m['quoteTime'],m['contractTime'],m['markTime'],m['openInterestTime']))
               for m in result.values()):
            raise ValueError('Futures snapshot crossed candle boundary or became stale')
        return dict(serverTime=decision_now, markets=result,
                    source='BINANCE_USDS_M_PUBLIC',paperOnly=True)
