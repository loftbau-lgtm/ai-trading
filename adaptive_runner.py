"""Public-data collector for the USDT paper portfolio, separate from legacy accounts."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import logging
import os
import threading
import time
from adaptive import NAME, load_config, rank_universe, validate_bars, window_metrics
from adaptive_portfolio import PaperPortfolio
from adaptive_matrix import AdaptiveMatrix
from directional_paper import DirectionalPaper, load_config as load_directional_config
from portfolio_agent import PortfolioDecisionAgent


class AdaptiveRunner:
    def __init__(self, public_get, scanner, directory):
        self.get,self.scanner = public_get,scanner
        self.directory = Path(directory)
        self.config = load_config()
        self.portfolio = PaperPortfolio(self.directory/'adaptive.sqlite3',self.config)
        self.matrix = AdaptiveMatrix(self.directory,self.config)
        directional_path=os.environ.get('DIRECTIONAL_DATABASE_PATH',str(self.directory/'directional_adaptive.sqlite3'))
        directional_config=load_directional_config(os.environ.get('DIRECTIONAL_CONFIG_PATH'))
        self.directional = DirectionalPaper(directional_path,directional_config)
        self.agent = PortfolioDecisionAgent(self.directory/'autonomous_agent.sqlite3')
        self.lock = threading.Lock()
        self.status = dict(state='starting',processed=0,total=0,error=None,updatedAt=None)
        self.ranking = []
        self.entry_enabled = True
        self.candle_cache = None  # Optional public WebSocket cache; REST repairs gaps.

    def _history(self,symbol,last,now):
        existing = self.portfolio.history(symbol)
        start = existing[-1]['time']+60000 if existing else last-1440*60000
        result = list(existing)
        if self.candle_cache and existing:
            cached=self.candle_cache.closed_bars(symbol,start,last)
            prefix=[]
            for bar in cached:
                if bar['time']!=start+len(prefix)*60000:break
                prefix.append(bar)
            if prefix:
                validate_bars(prefix,now)
                self.portfolio.save_bars(symbol,prefix)
                result.extend(prefix);start=prefix[-1]['time']+60000
        # Bound one cycle; long outages catch up across cycles, no skipping.
        for _ in range(10):
            if start > last: break
            rows = self.get('klines',dict(symbol=symbol,interval='1m',startTime=start,endTime=last+59999,limit=1000))
            bars = [dict(time=int(r[0]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),
                         volume=float(r[5]),end=int(r[6]),turnover=float(r[7]),trades=int(r[8]))
                    for r in rows if int(r[6]) < now]
            if not bars: break
            validate_bars(bars,now)
            if result and bars[0]['time'] != result[-1]['time']+60000: raise ValueError('Gap')
            self.portfolio.save_bars(symbol,bars)
            result.extend(bars)
            start = bars[-1]['time']+60000
            if len(bars) < 1000: break
        validate_bars(result,now)
        return result

    def cycle(self,stop):
        c = self.config
        started = time.monotonic()
        scan = self.scanner.snapshot()
        if not scan['fresh']: raise ValueError('SCANNER_NOT_FRESH')
        before = int(time.time()*1000)
        now = int(self.get('time')['serverTime'])
        after = int(time.time()*1000)
        clock_ok = abs(now-(before+after)//2) <= c['MAX_CLOCK_SKEW_MS'] and after-before <= c['MAX_CLOCK_SKEW_MS']
        if self.candle_cache:
            with self.candle_cache.lock:self.candle_cache.clock_drift=now-(before+after)//2
        last = now//60000*60000-60000
        tickers = {r['symbol']:r for r in scan['rows'] if r['quote'] == c['QUOTE']}
        with self.portfolio.lock:
            held = set(self.portfolio.state['positions']) | set(self.portfolio.state['pending'])
        with self.directional.lock:
            directional_held=set(self.directional.state['positions'])|set(self.directional.state['orders'])
        symbols = sorted(set(tickers)|held|directional_held|{'BTCUSDT'})
        histories,ranked,failures = {},[],[]
        with self.lock: self.status.update(state='syncing' if self.status['updatedAt'] else 'warming',processed=0,total=len(symbols),error=None)
        with ThreadPoolExecutor(max_workers=c['FETCH_WORKERS']) as pool:
            futures = {pool.submit(self._history,s,last,now):s for s in symbols}
            for future in as_completed(futures):
                if stop.is_set():
                    for f in futures: f.cancel()
                    return
                symbol = futures[future]
                try:
                    bars = future.result()
                    if not bars or bars[-1]['time'] != last: raise ValueError('Not caught up')
                    histories[symbol] = bars
                    if symbol in tickers and len(bars) >= 1441:
                        m = tickers[symbol]
                        ranked.append(dict(symbol=symbol,quote=m['quote'],spreadPct=m['spreadPct'],
                                           windows=window_metrics(bars,m['spreadPct'])))
                except Exception:
                    failures.append(symbol)
                with self.lock: self.status['processed'] += 1
        ranking = rank_universe(ranked)
        for i,row in enumerate(ranking): row['top'] = i < c['TOP_N']
        # A slow universe scan must never create retroactive orders. Refresh only
        # eligible/held markets to the current closed minute before evaluation.
        decision_now = int(self.get('time')['serverTime'])
        decision_last = decision_now//60000*60000-60000
        final_symbols = {r['symbol'] for r in ranking[:c['TOP_N']]}|held|directional_held|{'BTCUSDT'}
        with ThreadPoolExecutor(max_workers=c['FETCH_WORKERS']) as pool:
            futures = {pool.submit(self._history,s,decision_last,decision_now):s for s in sorted(final_symbols)}
            for future in as_completed(futures):
                symbol=futures[future]
                try:
                    bars=future.result()
                    if not bars or bars[-1]['time']!=decision_last:raise ValueError('Stale final candle')
                    histories[symbol]=bars
                except Exception:failures.append(symbol)
        ended = int(time.time()*1000)
        fresh = clock_ok and not failures and ended-now <= c['MAX_DATA_AGE_MS'] and self.scanner.snapshot()['fresh']
        directional_fresh=(clock_ok and ended-now<=self.directional.c['maxDataAgeMs']
            and self.scanner.snapshot()['fresh'] and all(
                symbol in histories and histories[symbol] and histories[symbol][-1]['time']==decision_last
                for symbol in final_symbols))
        # Start the independent ledger as soon as the common market snapshot is
        # ready. Its SQLite transaction cannot change either existing account.
        with ThreadPoolExecutor(max_workers=1) as directional_pool:
            directional_future=directional_pool.submit(self.directional.process,histories,ranking,ended,self.candle_cache,
                context_ok=directional_fresh,manual_kill=(self.directional.path.parent/'directional.kill').exists())
            try:
                self.portfolio.process(histories,ranking,ended,context_ok=fresh,
                                       manual_kill=(self.directory/'adaptive.kill').exists(),
                                       entry_enabled=self.entry_enabled)
                # Matrix reuses the exact same histories/ranking snapshot. No extra Binance requests.
                self.matrix.process(histories,ranking,ended,context_ok=fresh,
                                    manual_kill=(self.directory/'adaptive.kill').exists(),
                                    entry_enabled=self.entry_enabled)
            finally:
                try:directional_future.result()
                except Exception:logging.exception('Directional PAPER cycle paused')
        # Autonomous operator has its own ledger. Existing accounts remain
        # historical controls and cannot receive new entry capital here.
        agent_histories={s:histories[s] for s in final_symbols if s in histories
                         and histories[s] and histories[s][-1]['time']==decision_last}
        try:self.agent.cycle(agent_histories,ranking,ended,fresh=fresh)
        except Exception:logging.exception('Autonomous PAPER agent cycle paused')
        with self.lock:
            self.ranking = ranking[:c['TOP_N']]
            self.status.update(state='live' if fresh else 'paused',updatedAt=ended,ranked=len(ranking),
                error=None if fresh else 'Entries paused: data coverage, freshness or exchange clock check failed',
                failedSymbols=failures[:20],durationSeconds=round(time.monotonic()-started,1),clockOk=clock_ok)

    def run(self,stop):
        # Deliberately cannot activate real orders through environment alone.
        if os.environ.get('TRADING_MODE','PAPER').upper() != 'PAPER':
            with self.lock: self.status.update(state='blocked',error='LIVE is not integrated. PAPER only.')
            return
        while not stop.is_set():
            started = time.monotonic()
            try: self.cycle(stop)
            except Exception as exc:
                logging.warning('Adaptive public feed paused: %s',type(exc).__name__)
                with self.lock: self.status.update(state='paused',error='Public data unavailable; no new entries')
            stop.wait(max(5,60-(time.monotonic()-started)))

    def close(self):
        # Close PAPER SQLite resources. LIVE remains untouched.
        try:
            self.matrix.close()
        except Exception:
            pass
        try:
            self.directional.close()
        except Exception:
            pass
        try:
            self.agent.close()
        except Exception:
            pass
        try:
            self.portfolio.db.close()
        except Exception:
            pass

    def snapshot(self):
        with self.lock: status,ranking = dict(self.status),list(self.ranking)
        if status['updatedAt'] and int(time.time()*1000)-status['updatedAt'] > self.config['MAX_DATA_AGE_MS']:
            status['state'] = 'stale'
        return dict(name=NAME,status=status,ranking=ranking,**self.portfolio.snapshot())
