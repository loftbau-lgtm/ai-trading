"""Persistent, read-only research on closed public Futures candles.

Research outcomes never authorize exchange orders. A candidate needs genuine
chronological OOS and live shadow evidence before PAPER promotion.
"""
import json
import math
import sqlite3
import statistics
import threading
from pathlib import Path

from edge_lab import promotion_gate


MODELS = {
    'L1_TREND': ('LONG', 'TREND'), 'L2_BREAKOUT': ('LONG', 'BREAKOUT'),
    'S1_TREND': ('SHORT', 'TREND'), 'S2_BREAKDOWN': ('SHORT', 'BREAKOUT'),
    'N1_RANGE_LONG': ('LONG', 'RANGE'), 'N1_RANGE_SHORT': ('SHORT', 'RANGE'),
    'N2_RELATIVE_LONG': ('LONG', 'RELATIVE'), 'N2_RELATIVE_SHORT': ('SHORT', 'RELATIVE'),
}
HORIZON = 30  # minutes; signals are evaluated only on a later closed candle
TRAIN_MINUTES = 7 * 1440
VALIDATION_MINUTES = 3 * 1440


def signal(model, history, benchmark=None):
    """Pure point-in-time signal. No future bar is visible to this function."""
    if len(history) < 61:
        return False
    closes = [float(row['close']) for row in history[-61:]]
    if any(price <= 0 or not math.isfinite(price) for price in closes):
        return False
    fast = statistics.fmean(closes[-9:])
    slow = statistics.fmean(closes[-21:])
    baseline = statistics.fmean(closes[-60:])
    price = closes[-1]
    move15 = price / closes[-16] - 1
    move30 = price / closes[-31] - 1
    returns = [b / a - 1 for a, b in zip(closes[-31:-1], closes[-30:])]
    volatility = statistics.pstdev(returns)
    if volatility > .012:  # shock: do not open a research proposal
        return False
    if model == 'L1_TREND':
        return fast > slow > baseline and .003 < move30 < .04
    if model == 'L2_BREAKOUT':
        return price > max(closes[-21:-1]) and slow > baseline and .003 < move30 < .04
    if model == 'S1_TREND':
        return fast < slow < baseline and -.04 < move30 < -.003
    if model == 'S2_BREAKDOWN':
        return price < min(closes[-21:-1]) and move30 < -.003
    range_market = abs(move30) < .012 and abs(slow / baseline - 1) < .005
    if model == 'N1_RANGE_LONG':
        return range_market and price < slow * .997
    if model == 'N1_RANGE_SHORT':
        return range_market and price > slow * 1.003
    if model.startswith('N2_RELATIVE_'):
        if not benchmark or len(benchmark) < 31:
            return False
        benchmark_move = float(benchmark[-1]['close']) / float(benchmark[-31]['close']) - 1
        strength = move30 - benchmark_move
        return strength > .005 if model.endswith('LONG') else strength < -.005
    raise ValueError('Unknown research model')


def net_return(entry, exit_price, side, spread, taker_fee, slippage, funding=0):
    gross = (exit_price / entry - 1) * (1 if side == 'LONG' else -1)
    cost = 2 * (taker_fee + slippage) + spread + abs(funding)
    return gross - cost, gross, cost


def _stats(rows):
    values = [row['netReturn'] for row in rows]
    count = len(values)
    if not count:
        return dict(trades=0, effectiveTrades=0, netExpectancy=None, grossExpectancy=None,
                    ciLow=None, ciHigh=None,
                    profitFactor=None, maxDrawdown=None, costStress25=None,
                    fillStress75=None, winRate=None)
    mean = statistics.fmean(values)
    # Independence is assessed per hourly block, not per overlapping signal.
    blocks = {}
    for row in rows:
        blocks.setdefault(row['exitTime'] // 3600000, []).append(row['netReturn'])
    hourly = [statistics.fmean(group) for group in blocks.values()]
    se = statistics.stdev(hourly) / math.sqrt(len(hourly)) if len(hourly) > 1 else float('inf')
    ci_low, ci_high = mean - 2.58 * se, mean + 2.58 * se  # conservative family-wise screen
    wins = sum(max(0, item) for item in values)
    losses = -sum(min(0, item) for item in values)
    equity = peak = 1.0
    drawdown = 0.0
    for item in values:
        equity *= max(.000001, 1 + item)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    return dict(trades=count, effectiveTrades=len(blocks), netExpectancy=mean,
                grossExpectancy=statistics.fmean(row.get('grossReturn',row['netReturn']+row['cost'])
                                                 for row in rows),
                ciLow=ci_low, ciHigh=ci_high,
                profitFactor=wins / max(losses,1e-12), maxDrawdown=drawdown,
                costStress25=statistics.fmean(row['netReturn'] - .25 * row['cost'] for row in rows),
                fillStress75=statistics.fmean(min(0, item) + .75 * max(0, item) for item in values),
                winRate=sum(item > 0 for item in values) / count)


class FuturesEdgeEngine:
    def __init__(self, path, taker_fee=.0004, slippage=.0002):
        self.path = Path(path)
        if self.path.name != 'edge_lab.sqlite3':
            raise ValueError('Edge Lab requires an isolated ledger')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.taker_fee = float(taker_fee)
        self.slippage = float(slippage)
        if not (0 <= self.taker_fee < .05 and 0 <= self.slippage < .05):
            raise ValueError('Invalid research cost configuration')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS bars(symbol TEXT NOT NULL, time INTEGER NOT NULL,
              close REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY(symbol,time));
            CREATE TABLE IF NOT EXISTS signals(model TEXT NOT NULL, symbol TEXT NOT NULL,
              candleTime INTEGER NOT NULL, entry REAL NOT NULL, spread REAL NOT NULL,
              funding REAL NOT NULL, fundingDue INTEGER NOT NULL,
              costComplete INTEGER NOT NULL,
              split TEXT NOT NULL, exitTime INTEGER, netReturn REAL, grossReturn REAL,
              cost REAL, PRIMARY KEY(model,symbol,candleTime));
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        last = self.db.execute('SELECT MAX(time) FROM bars').fetchone()[0]
        self.state = 'COLLECTING' if last is not None else 'WAITING_FOR_DATA'
        self.last_error = None
        self.last_cycle = last

    def _first_time(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='firstTime'").fetchone()
        return int(row[0]) if row else None

    def cycle(self, snapshot):
        markets = snapshot['markets']
        now = int(snapshot['serverTime'])
        if not markets or any(int(m['bars'][-1]['end']) >= now for m in markets.values()):
            raise ValueError('Research requires closed candles')
        with self.lock:
            try:
                with self.db:
                    first = self._first_time()
                    if first is None:
                        first = min(int(m['bars'][0]['time']) for m in markets.values())
                        self.db.execute("INSERT INTO metadata VALUES('firstTime',?)", (str(first),))
                    for symbol, market in markets.items():
                        for bar in market['bars']:
                            t = int(bar['time'])
                            if int(bar['end']) >= now:
                                continue
                            self.db.execute('INSERT OR IGNORE INTO bars VALUES(?,?,?,?)',
                                (symbol, t, float(bar['close']), float(bar.get('volume', 0))))
                        last = int(market['bars'][-1]['time'])
                        if last % (5 * 60000):
                            continue
                        history = [dict(time=t, close=c, volume=v) for t,c,v in
                            self.db.execute('SELECT time,close,volume FROM bars WHERE symbol=? AND time<=? ORDER BY time DESC LIMIT 61',
                                            (symbol,last)).fetchall()[::-1]]
                        if len(history) < 61 or any(b['time'] - a['time'] != 60000
                                                    for a,b in zip(history,history[1:])):
                            continue
                        mid = (float(market['bid']) + float(market['ask'])) / 2
                        spread = (float(market['ask']) - float(market['bid'])) / mid
                        # Quote/funding observations belong only to the newest bar.
                        # Historical bootstrap bars are retained, but never fabricated.
                        split = ('TRAIN' if last < first + TRAIN_MINUTES * 60000 else
                                 'VALIDATION' if last < first + (TRAIN_MINUTES + VALIDATION_MINUTES) * 60000
                                 else 'OOS')
                        benchmark = markets.get('BTCUSDT',{}).get('bars',[])
                        for model in MODELS:
                            if model.startswith('N2_') and symbol == 'BTCUSDT':
                                continue
                            if signal(model, history, benchmark):
                                self.db.execute('INSERT OR IGNORE INTO signals '
                                    '(model,symbol,candleTime,entry,spread,funding,fundingDue,costComplete,split) '
                                    'VALUES(?,?,?,?,?,?,?,?,?)',
                                    (model,symbol,last,mid,spread,
                                     float(market['fundingRate']),int(market['nextFundingTime']),0,split))
                        # Mature each outstanding signal only after its exit candle exists.
                        pending = self.db.execute('SELECT model,candleTime,entry,spread,funding,fundingDue '
                            'FROM signals WHERE symbol=? AND exitTime IS NULL AND candleTime<=?',
                            (symbol,last-HORIZON*60000)).fetchall()
                        for model,t,entry,entry_spread,funding,funding_due in pending:
                            exit_t = t + HORIZON * 60000
                            exit_row = self.db.execute('SELECT close FROM bars WHERE symbol=? AND time=?',
                                                       (symbol,exit_t)).fetchone()
                            if not exit_row:
                                continue
                            complete = exit_t == last and funding_due > exit_t + 59999
                            exit_spread = spread if complete else entry_spread
                            exit_price = mid if complete else exit_row[0]
                            # A funding interval or missed exit quote makes the cost
                            # approximation diagnostic-only, never promotable.
                            net,gross,cost = net_return(entry,exit_price,MODELS[model][0],
                                (entry_spread+exit_spread)/2,self.taker_fee,self.slippage,
                                0 if complete else funding)
                            self.db.execute('UPDATE signals SET exitTime=?,netReturn=?,grossReturn=?,cost=?,costComplete=? '
                                'WHERE model=? AND symbol=? AND candleTime=?',
                                (exit_t,net,gross,cost,int(complete),model,symbol,t))
                self.last_cycle = max(int(m['bars'][-1]['time']) for m in markets.values())
                self.state = 'COLLECTING'
                self.last_error = None
                return True
            except Exception as exc:
                self.state = 'ERROR'
                self.last_error = type(exc).__name__ + ': ' + str(exc)
                raise

    def _rows(self, model, split):
        return [dict(symbol=s,entryTime=t,exitTime=exit_t,netReturn=n,grossReturn=g,
                     cost=c,costComplete=bool(complete))
                for s,t,exit_t,n,g,c,complete in self.db.execute(
                    'SELECT symbol,candleTime,exitTime,netReturn,grossReturn,cost,costComplete '
                    'FROM signals WHERE model=? AND split=? AND exitTime IS NOT NULL ORDER BY exitTime,symbol',
                    (model,split))]

    def candidates(self):
        with self.lock:
            result = []
            for model,(side,kind) in MODELS.items():
                train = self._rows(model,'TRAIN')
                validation = self._rows(model,'VALIDATION')
                oos = self._rows(model,'OOS')
                stats = _stats(oos)
                fold_size=len(validation)//3
                folds=[validation[i*fold_size:(i+1)*fold_size]
                       if i<2 else validation[i*fold_size:]
                       for i in range(3)] if fold_size else []
                walk_forward=(len(train)>=30 and len(folds)==3 and
                    _stats(train)['netExpectancy']>0 and
                    all(len(fold)>=15 and _stats(fold)['netExpectancy']>0
                        for fold in folds))
                shadow = [r for r in oos if r['costComplete']]
                symbols = {r['symbol'] for r in oos}
                periods = {r['exitTime']//(7*86400000) for r in oos}
                symbol_concentration = max((sum(r['symbol']==s for r in oos)/len(oos)
                                            for s in symbols),default=1.)
                period_concentration = max((sum(r['exitTime']//(7*86400000)==p for r in oos)/len(oos)
                                            for p in periods),default=1.)
                evidence = dict(independentOos=True,walkForwardValidated=walk_forward,
                    shadowValidated=len(shadow)>=100,
                    costModelComplete=bool(oos) and len(shadow)==len(oos),
                    oosTrades=stats['effectiveTrades'] if oos else 0,
                    oosNetExpectancy=stats['netExpectancy'],oosCiLow=stats['ciLow'],
                    oosProfitFactor=stats['profitFactor'],oosMaxDrawdown=stats['maxDrawdown'],
                    costStress25=stats['costStress25'],fillStress75=stats['fillStress75'],
                    symbolConcentration=symbol_concentration,periodConcentration=period_concentration,
                    symbolCount=len(symbols),periodCount=len(periods),selectionBiasRisk='LOW')
                active = promotion_gate(evidence)
                # A profitable average does not imply a calibrated win chance.
                # Wilson's one-sided lower bound is used for the entry gate.
                n = stats['effectiveTrades']
                p = stats['winRate'] or 0
                z = 1.645
                probability_floor = ((p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))) /
                                     (1+z*z/n)) if n else 0
                active = active and probability_floor >= .55
                recent = oos[-50:]
                decay = len(recent)==50 and _stats(recent)['netExpectancy'] <= 0
                active = active and not decay
                if active:
                    status = 'ACTIVE_PAPER'
                elif decay and evidence['oosTrades'] >= 100:
                    status = 'PAUSED_EDGE_DECAY'
                elif stats['ciHigh'] is not None and stats['ciHigh'] <= 0:
                    status = 'NEGATIVE_EDGE'
                elif oos and stats['netExpectancy'] < 0:
                    status = 'NEGATIVE_EDGE'
                elif stats['costStress25'] is not None and stats['costStress25'] <= 0:
                    status = 'COST_DOMINATED'
                elif stats['effectiveTrades'] < 100:
                    status = 'INSUFFICIENT_SAMPLE'
                else:
                    status = 'SHADOW'
                family = model.split('_LONG')[0].split('_SHORT')[0]
                result.append(dict(modelId=model,family=family,direction=side,kind=kind,
                    status=status,trainTrades=len(train),validationTrades=len(validation),
                    validationFolds=[dict(trades=len(fold),netExpectancy=_stats(fold)['netExpectancy'])
                                     for fold in folds],
                    oosTrades=len(oos),shadowTrades=len(shadow),**stats,
                    probabilityNetProfit=probability_floor,
                    evidence=evidence,paperPromoted=active))
            return result

    def snapshot(self):
        with self.lock:
            candidates = self.candidates()
            bars = self.db.execute('SELECT COUNT(*) FROM bars').fetchone()[0]
            signals = self.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
            return dict(mode='PAPER_ONLY',state=self.state,lastError=self.last_error,
                        lastCycle=self.last_cycle,barCount=bars,signalCount=signals,
                        activeCandidates=sum(row['paperPromoted'] for row in candidates),
                        candidates=candidates,liveReady=False,
                        note='Only live-observed entry/exit quotes without an intervening funding event can qualify for promotion.')

    def close(self):
        with self.lock:
            self.db.close()
