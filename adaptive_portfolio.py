"""Atomic, restartable PAPER execution. Never imports an exchange order client."""
import copy
import json
import sqlite3
import threading
import time
import logging
from pathlib import Path
from adaptive import config_hash, entry_decision, features, correlation, report, validate_bars


class PaperPortfolio:
    def __init__(self, path, config):
        self.config = config
        self.telemetry = None  # Optional post-commit observer; never a risk/decision hook.
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(path,check_same_thread=False,timeout=30)
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS portfolio(id INTEGER PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS decisions(key TEXT PRIMARY KEY,time INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS closed_trades(key TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS equity(time INTEGER PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS bars(symbol TEXT,time INTEGER,data TEXT,PRIMARY KEY(symbol,time));
        ''')
        row = self.db.execute('SELECT data FROM portfolio WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else dict(cash=config['STARTING_CAPITAL'],positions={},pending={},
            cursors={},peak=config['STARTING_CAPITAL'],day=None,dayEquity=config['STARTING_CAPITAL'],
            shockUntil=0,kill=None,configHash=config_hash(config))
        if self.state['configHash'] != config_hash(config):
            self.db.close()
            raise ValueError('Config changed: use a new paper database to preserve experiment integrity')

    def history(self,symbol):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM bars WHERE symbol=? ORDER BY time',(symbol,))]

    def save_bars(self,symbol,bars):
        if not bars: return
        with self.lock, self.db:
            self.db.executemany('INSERT OR REPLACE INTO bars VALUES(?,?,?)',[(symbol,b['time'],json.dumps(b)) for b in bars])
            # Retain all missing execution candles until cursor catches up.
            cutoff = min(bars[-1]['time']-1440*60000,self.state['cursors'].get(symbol,bars[-1]['time'])-100*60000)
            self.db.execute('DELETE FROM bars WHERE symbol=? AND time<?',(symbol,cutoff))

    def _equity(self):
        return self.state['cash']+sum(p['qty']*p['mark'] for p in self.state['positions'].values())

    def _risk(self,timestamp):
        s,c = self.state,self.config
        equity = self._equity()
        day = timestamp//86400000
        if s['day'] != day:
            s['day'],s['dayEquity'] = day,equity
            if s['kill'] == 'REJECT_DAILY_LOSS': s['kill'] = None
        s['peak'] = max(s['peak'],equity)
        dd = max(0,1-equity/s['peak'])
        if equity <= s['dayEquity']*(1-c['DAILY_LOSS_LIMIT']): s['kill'] = 'REJECT_DAILY_LOSS'
        if dd >= c['MAX_DRAWDOWN']: s['kill'] = 'REJECT_DRAWDOWN'
        return equity,dd

    def _close(self,symbol,p,reference,t,reason,spread,maker=False):
        c,s = self.config,self.state
        spread_cost = 0 if maker else reference*p['qty']*spread/200
        slip = 0 if maker else reference*p['qty']*c['SLIPPAGE']
        proceeds = reference*p['qty']-spread_cost-slip
        fee = proceeds*(c['MAKER_FEE'] if maker else c['TAKER_FEE'])
        gross = (reference-p['entry'])*p['qty']
        fees = p['entryFee']+fee
        net = gross-fees-spread_cost-slip
        trade = dict(symbol=symbol,time=t,entryTime=p['entryTime'],entry=p['entry'],exit=proceeds/p['qty'],
            qty=p['qty'],grossPnL=gross,fees=fees,spreadCost=spread_cost,slippageCost=slip,netPnL=net,
            holdingMinutes=(t-p['entryTime'])/60000,reason=reason,
            **{k:p[k] for k in ('expectedEdge','expectedCost','activityDecile','zBucket','volatilityRegime')})
        self.db.execute('INSERT INTO closed_trades VALUES(?,?)',(p['id'],json.dumps(trade)))
        s['cash'] += proceeds-fee
        del s['positions'][symbol]
        return trade

    def process(self, histories, ranking, now, context_ok=True, manual_kill=False, entry_enabled=True):
        """Replay each missed closed candle; never backfill entries with today's ranking.

        Ranking is a contemporaneous snapshot ONLY for the latest closed minute.
        Previously submitted maker orders and exits are replayed, with stale spread
        conservatively charged at MAX_SPREAD when no contemporaneous quote exists.
        """
        with self.lock:
            for bars in histories.values(): validate_bars(bars,now)
            old = copy.deepcopy(self.state)
            try:
                with self.db:
                    emitted=self._process(histories,ranking,now,context_ok,manual_kill,entry_enabled)
                    self.db.execute('INSERT OR REPLACE INTO portfolio VALUES(1,?)',(json.dumps(self.state,allow_nan=False),))
            except Exception:
                self.state = old
                raise
        if self.telemetry and emitted:
            try:self.telemetry(emitted)
            except Exception:logging.warning('Paper telemetry unavailable; committed portfolio unchanged')

    def _process(self,histories,ranking,now,context_ok,manual_kill,entry_enabled):
        emitted=[]
        s,c = self.state,self.config
        market_map = {r['symbol']:r for r in ranking}
        latest = now//60000*60000-60000
        # First exposure to a symbol warms up without manufacturing past fills.
        for symbol,bars in histories.items():
            if bars and symbol not in s['cursors']: s['cursors'][symbol] = bars[-1]['time']-60000
            cursor = s['cursors'].get(symbol)
            if bars and cursor is not None and bars[0]['time'] > cursor+60000:
                raise ValueError('Missing execution history for '+symbol)
        moments = sorted({b['time'] for symbol,bars in histories.items() for b in bars if b['time'] > s['cursors'][symbol]})
        indices = {symbol:{b['time']:i for i,b in enumerate(bars)} for symbol,bars in histories.items()}
        for t in moments:
            # UTC daily starting equity is set before this minute's marks/fills.
            self._risk(t)
            for symbol,p in s['positions'].items():
                i = indices.get(symbol,{}).get(t)
                if i is not None:
                    p['mark'] = histories[symbol][i]['close']
                    aligned = histories[symbol][max(0,i-60):i+1]
                    p['returns'] = [b['close']/a['close']-1 for a,b in zip(aligned,aligned[1:])]
            equity,dd = self._risk(t)
            btc_i = indices.get('BTCUSDT',{}).get(t)
            if btc_i is not None and btc_i >= 99:
                btc = features(histories['BTCUSDT'][:btc_i+1])
                if abs(btc['zReturn']) > c['BTC_SHOCK_Z']: s['shockUntil'] = max(s['shockUntil'],t+c['BTC_PAUSE_MINUTES']*60000)
            fresh = context_ok and t == latest and now-(t+59999) <= c['MAX_DATA_AGE_MS']
            btc_fresh = btc_i is not None and btc_i >= 99
            for symbol in sorted(histories):
                i = indices[symbol].get(t)
                if i is None or t <= s['cursors'][symbol]: continue
                bars = histories[symbol][:i+1]
                b = bars[-1]
                f = features(bars) if len(bars) >= 100 else {}
                key = f'binance:{symbol}:1m:{b["end"]}'
                m = market_map.get(symbol)
                spread = m['spreadPct'] if fresh and m and m['spreadPct'] is not None else c['MAX_SPREAD']
                d = dict(timestamp=b['end'],symbol=symbol,activityScore=m['activityScore'] if m else None,
                         spread=spread,rangeToSpread=m['rangeToSpread'] if m else None,
                         expectedCost=None,expectedEdge=None,signal='HOLD',decision='HOLD',
                         positionSize=0,entry=None,exit=None,grossPnL=0,netPnL=0,**f)
                blocked = s['kill'] or ('REJECT_MANUAL_KILL' if manual_kill else None) or (
                    'NO_CONFIRMED_EDGE' if not entry_enabled else None)
                pending = s['pending'].get(symbol)
                if pending and t > pending['submitted']:
                    # Strict trade-through; not proof of a real maker fill/queue priority.
                    eligible = not blocked and t <= pending['expires'] and b['low'] < pending['entry']
                    if t == latest and (not fresh or not 0 < spread <= c['MAX_SPREAD']): eligible = False
                    if eligible and pending['qty'] <= b['volume']*.01:
                        cost = pending['entry']*pending['qty']
                        fee = cost*c['MAKER_FEE']
                        s['cash'] -= cost+fee
                        s['positions'][symbol] = dict(pending,entryTime=t,entryFee=fee,mark=b['close'])
                        d.update(decision='PAPER_MAKER_FILL',signal='BUY',positionSize=pending['qty'],entry=pending['entry'])
                    else: d['decision'] = 'CANCEL_LIMIT_EXPIRED_OR_RISK'
                    del s['pending'][symbol]
                p = s['positions'].get(symbol)
                if p:
                    reason,reference = None,None
                    if b['low'] <= p['stop']: reason,reference = 'STOP_LOSS',min(b['open'],p['stop'])
                    elif t-p['entryTime'] >= c['MAX_HOLD_MINUTES']*60000: reason,reference = 'TIME_STOP',b['close']
                    elif p.get('exitSubmitted') is not None and t > p['exitSubmitted']:
                        if t <= p['exitExpires'] and b['high'] > p['exitLimit'] and p['qty'] <= b['volume']*.01:
                            reason,reference = 'MEAN_REVERSION_MAKER',p['exitLimit']
                        elif t >= p['exitExpires']:
                            for field in ('exitSubmitted','exitExpires','exitLimit'):p.pop(field,None)
                            d['decision']='CANCEL_EXIT_LIMIT_EXPIRED'
                    if reason:
                        trade = self._close(symbol,p,reference,t,reason,spread,maker=reason=='MEAN_REVERSION_MAKER')
                        d.update(decision=reason,signal='SELL',exit=trade['exit'],grossPnL=trade['grossPnL'],netPnL=trade['netPnL'])
                    elif t > p['entryTime'] and 'exitSubmitted' not in p and f and (b['close'] >= p['target'] or f['zReturn'] >= c['Z_RETURN_EXIT'] or f['priceZ'] > 0):
                        p.update(exitSubmitted=t,exitExpires=t+c['ORDER_TTL_MINUTES']*60000,exitLimit=b['close']*(1+spread/200))
                        d.update(decision='PLACE_EXIT_LIMIT_MAKER',signal='SELL',exit=p['exitLimit'])
                elif d['decision'] == 'HOLD':
                    equity,dd = self._risk(t)
                    reason = blocked or s['kill']
                    if not fresh or not btc_fresh: reason = reason or 'REJECT_STALE_OR_INCOMPLETE_DATA'
                    if symbol != 'BTCUSDT' and t < s['shockUntil']: reason = reason or 'REJECT_MARKET_SHOCK'
                    if not m or not f: reason = reason or 'REJECT_WARMUP_OR_UNRANKED'
                    if reason: d['decision'] = reason
                    else:
                        d.update(entry_decision(f,m,c,equity,dd))
                        if d['signal'] == 'BUY':
                            positions = list(s['positions'].values())+list(s['pending'].values())
                            reserved = sum(p['qty']*p['entry']*(1+c['MAKER_FEE']) for p in s['pending'].values())
                            exposure = sum(p['qty']*p.get('mark',p['entry']) for p in positions)
                            returns = [(y['close']/x['close']-1) for x,y in zip(bars[-61:-1],bars[-60:])]
                            correlated = sum(p['qty']*p.get('mark',p['entry']) for p in positions if correlation(returns,p['returns']) >= c['CORRELATION_THRESHOLD'])
                            budget = min(equity*c['MAX_TOTAL_EXPOSURE']-exposure,equity*c['MAX_CORRELATED_EXPOSURE']-correlated,
                                         (s['cash']-reserved)/(1+c['MAKER_FEE']))
                            qty = min(d['positionSize'],max(0,budget)/d['entry'])
                            if len(positions) >= c['MAX_OPEN_POSITIONS']: d['decision'] = 'REJECT_MAX_POSITIONS'
                            elif qty*d['entry'] < 5: d['decision'] = 'REJECT_EXPOSURE_OR_MIN_NOTIONAL'
                            else:
                                s['pending'][symbol] = dict(id=key,qty=qty,entry=d['entry'],stop=d['stop'],target=d['target'],
                                    submitted=t,expires=t+c['ORDER_TTL_MINUTES']*60000,returns=returns,
                                    expectedEdge=d['expectedEdge'],expectedCost=d['expectedCost'],
                                    activityDecile=min(10,int(m['activityPercentile']//10)+1),
                                    zBucket='below -3' if f['zReturn'] < -3 else '-3 to -2' if f['zReturn'] <= -2 else 'above -2',
                                    volatilityRegime='high' if f['volatility'] >= .3 else 'medium' if f['volatility'] >= .1 else 'low')
                                d['positionSize'] = qty
                            if d['decision'].startswith('REJECT_'): d.update(signal='HOLD',positionSize=0)
                d.update(configHash=s['configHash'],marketContext=m,sourceBar=b,executionModel='PAPER_CANDLE_PROXY')
                self.db.execute('INSERT INTO decisions VALUES(?,?,?)',(key,t,json.dumps(d,allow_nan=False)))
                if self.telemetry:
                    stamp=time.time_ns()//1000000
                    emitted.append(dict(id=key,decisionTimestamp=stamp,orderCreatedTimestamp=stamp if d['decision']=='PLACE_LIMIT_MAKER' else None))
                s['cursors'][symbol] = t
            equity,dd = self._risk(t)
            point = dict(time=t,equity=equity,drawdown=dd)
            self.db.execute('INSERT OR REPLACE INTO equity VALUES(?,?)',(t,json.dumps(point)))
        return emitted

    def snapshot(self):
        with self.lock:
            trades = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM closed_trades ORDER BY rowid')]
            points = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM equity ORDER BY time')]
            decisions = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM decisions ORDER BY time DESC,rowid DESC LIMIT 50')]
            return dict(mode='PAPER',cash=self.state['cash'],equity=self._equity(),kill=self.state['kill'],
                        positions=copy.deepcopy(self.state['positions']),pending=copy.deepcopy(self.state['pending']),
                        metrics=report(trades,points,self.config),decisions=decisions,trades=trades[-100:],config=self.config)
