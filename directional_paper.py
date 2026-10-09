"""Isolated synthetic LONG/SHORT PAPER ledger. No exchange client or order transport."""
import copy
import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from adaptive import correlation, validate_bars
from directional_model import context, evaluate
from directional_statistics import (bootstrap_robustness, concentration, cost_stress,
    expectancy, groups, monte_carlo, prediction_quality, sequence_stress, walk_forward)


def load_config(path=None):
    config=json.loads(Path(path or Path(__file__).with_name('directional_config.json')).read_text())
    for key in ('startingCapital','maxSpreadPct','minEdgeMultiplier','riskPerTrade','maxDataAgeMs'):
        if not math.isfinite(config[key]) or config[key]<=0:raise ValueError(key)
    for key in ('maxSymbolExposure','maxGrossExposure','maxNetExposure','maxLongExposure','maxShortExposure','maxCorrelatedExposure','dailyLossLimit','maxDrawdown'):
        if not 0<config[key]<=1:raise ValueError(key)
    if sum(config['weights'].values())<.99 or sum(config['weights'].values())>1.01:raise ValueError('weights')
    if config['fundingRate'] is not None:
        raise ValueError('Historical funding is not integrated; fundingRate must remain null')
    if config.get('portfolioMode','LONG_SHORT') not in ('LONG_ONLY','SHORT_ONLY','LONG_SHORT','MARKET_NEUTRAL','CONTROL_FLAT_BASELINE'):
        raise ValueError('portfolioMode')
    if config.get('signalStyle','HYBRID') not in ('HYBRID','TREND_FOLLOWING','MEAN_REVERSION'):
        raise ValueError('signalStyle')
    return config


def _hash(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


def _class(d):
    confidence=int(d['confidence']*10)*10
    ratio=d.get('edgeCostRatio') or 0
    return '|'.join((d['candidateDirection'],d['regime'],str(d.get('activityDecile') or 'NA'),
        d['volatilityRegime'],f'P{confidence}',f'EC{int(ratio)}'))


def _returns(bars):
    tail=bars[-61:]
    return [b['close']/a['close']-1 for a,b in zip(tail,tail[1:])]


class DirectionalPaper:
    def __init__(self,path,config=None):
        self.c=config or load_config()
        self.path=Path(path)
        if self.path.name.lower() in ('adaptive.sqlite3','microstructure.sqlite3','quantlab.sqlite3'):
            raise ValueError('Directional experiment requires a separate database filename')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.path,timeout=30,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS directional_experiments(id TEXT PRIMARY KEY,config_hash TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_models(id TEXT PRIMARY KEY,experiment_id TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_predictions(id TEXT PRIMARY KEY,time INTEGER,symbol TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_decisions(id TEXT PRIMARY KEY,time INTEGER,symbol TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_positions(id TEXT PRIMARY KEY,symbol TEXT,status TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_orders(id TEXT PRIMARY KEY,symbol TEXT,status TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_trades(id TEXT PRIMARY KEY,time INTEGER,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_equity(time INTEGER PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_labels(id TEXT,horizon INTEGER,data TEXT,PRIMARY KEY(id,horizon));
          CREATE TABLE IF NOT EXISTS directional_edge_classes(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_statistics(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_monte_carlo(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_calibration(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_regime_stats(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_shadow(id TEXT PRIMARY KEY,time INTEGER,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_shadow_observations(id TEXT,horizon INTEGER,data TEXT,PRIMARY KEY(id,horizon));
          CREATE TABLE IF NOT EXISTS directional_model_rankings(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_candidates(id TEXT PRIMARY KEY,time INTEGER,symbol TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS directional_state(id INTEGER PRIMARY KEY,data TEXT);
        ''')
        self.config_hash=_hash(self.c)
        row=self.db.execute('SELECT data FROM directional_state WHERE id=1').fetchone()
        if row:
            self.state=json.loads(row['data'])
            if self.state['configHash']!=self.config_hash:raise ValueError('Directional config changed; create a new experiment DB')
        else:
            capital=self.c['startingCapital']
            model=self.c.get('portfolioMode','LONG_SHORT')+'_'+self.c.get('signalStyle','HYBRID')+'_G'+str(self.c['generation'])
            self.state=dict(cash=capital,positions={},orders={},cursors={},peak=capital,day=None,
                dayEquity=capital,kill=None,configHash=self.config_hash,experimentId='DIRECTIONAL_G0',modelId=model)
            with self.db:
                self.db.execute('INSERT INTO directional_experiments VALUES(?,?,?)',('DIRECTIONAL_G0',self.config_hash,json.dumps(self.c)))
                self.db.execute('INSERT INTO directional_models VALUES(?,?,?)',(model,'DIRECTIONAL_G0',json.dumps(dict(generation=self.c['generation'],configHash=self.config_hash,mode=self.c.get('portfolioMode','LONG_SHORT'),style=self.c.get('signalStyle','HYBRID')))))
                self._persist()
        self.status=dict(state='starting',lastCycle=None,error=None,analysed=0)
        self.opportunities=[]
        self._trade_events=deque(maxlen=100000)
        self._recent_trades=defaultdict(deque)
        self._tracked_symbols=set()
        self._stream_generation=None
        self._overflow_at=None
        self._report_cache=None

    def attach_stream(self,store):
        self._stream_generation=store.generation
        def receive(kind,event):
            if kind=='trade' and event['symbol'] in self._tracked_symbols:
                if len(self._trade_events)==self._trade_events.maxlen:
                    self._overflow_at=event['receivedTimestamp']
                self._trade_events.append(event)
        store.listeners.append(receive)

    def tracked_symbols(self):
        with self.lock:return sorted(self._tracked_symbols)

    def observe(self,store,now=None):
        """Observe public trades and sampled books; outages/restarts are censored."""
        now=now if now is not None else time.time_ns()//1000000
        with self.lock:
            while self._trade_events:
                event=self._trade_events.popleft()
                self._recent_trades[event['symbol']].append(event)
            for symbol,events in list(self._recent_trades.items()):
                while events and events[0]['receivedTimestamp']<now-75000:events.popleft()
                if not events:del self._recent_trades[symbol]
            rows=self.db.execute('''SELECT s.id,s.data FROM directional_shadow s
                WHERE NOT EXISTS (SELECT 1 FROM directional_shadow_observations o
                                  WHERE o.id=s.id AND o.horizon=60)''').fetchall()
            incomplete=set()
            with self.db:
                for row in rows:
                    d=json.loads(row['data'])
                    signal_time=d['decisionTimestamp']
                    if signal_time>now:continue
                    for horizon in (1,2,5,10,30,60):
                        if self.db.execute('SELECT 1 FROM directional_shadow_observations WHERE id=? AND horizon=?',
                                           (row['id'],horizon)).fetchone():continue
                        target=signal_time+horizon*1000
                        if now<target+2000:
                            incomplete.add(d['symbol']);continue
                        observed=dict(symbol=d['symbol'],direction=d['candidateDirection'],horizonSeconds=horizon,
                                      signalTimestamp=signal_time,targetTimestamp=target,status='CENSORED',reason=None,
                                      touch=None,tradeThrough=None,bookTouch=None,movedAway=None,MFE=None,MAE=None,netMarkoutPct=None)
                        if store.generation!=d.get('microGeneration'):
                            observed['reason']='STREAM_INTERRUPTED_OR_RESTARTED'
                        elif self._overflow_at and signal_time<=self._overflow_at<=target+2000:
                            observed['reason']='TRADE_EVENT_QUEUE_OVERFLOW'
                        else:
                            with store.lock:
                                books=list(store.samples.get(d['symbol'],()))
                                stream_state=store.state.get(d['symbol'],{})
                                coverage=stream_state.get('tradeCoverageStart')
                                serial=stream_state.get('coverageSerial',0)
                                connected=store.connections and all(store.connections.values())
                            target_books=[x for x in books if signal_time<x['timestamp']<=target+2000 and abs(x['timestamp']-target)<=2000]
                            trades=[x for x in self._recent_trades.get(d['symbol'],()) if signal_time<x['receivedTimestamp']<=target+2000]
                            if d.get('bid') is None or d.get('ask') is None:
                                observed['reason']='NO_BOOK_PRICE'
                            elif serial!=d.get('microCoverageSerial') or not target_books or coverage is None or coverage>signal_time or not connected:
                                observed['reason']='NO_CONTINUOUS_BOOK_OR_TRADE_COVERAGE'
                            else:
                                book=min(target_books,key=lambda x:abs(x['timestamp']-target))
                                price=d['bid'] if d['candidateDirection']=='LONG' else d['ask']
                                direction=1 if d['candidateDirection']=='LONG' else -1
                                relevant=[x for x in trades if x['aggressor']==('sell' if direction==1 else 'buy')]
                                touch=any((x['price']-price)*direction<=0 for x in relevant)
                                through=any((x['price']-price)*direction<0 for x in relevant)
                                values=[(x['mid']/price-1)*direction*100 for x in books if signal_time<x['timestamp']<=target+2000]
                                markout=(book['mid']/price-1)*direction*100-d['expectedCostPct']
                                observed.update(status='OBSERVED',reason=None,bookTimestamp=book['timestamp'],
                                    actualTradeSamples=len(trades),touch=touch,tradeThrough=through,
                                    bookTouch=(book['ask']<=price if direction==1 else book['bid']>=price),
                                    movedAway=not touch and (book['mid']-price)*direction>0,
                                    MFE=max(values) if values else None,MAE=min(values) if values else None,netMarkoutPct=markout)
                        self.db.execute('INSERT INTO directional_shadow_observations VALUES(?,?,?)',
                                        (row['id'],horizon,json.dumps(observed,allow_nan=False)))
            self._tracked_symbols=incomplete

    def _persist(self):
        self.db.execute('INSERT OR REPLACE INTO directional_state VALUES(1,?)',(json.dumps(self.state,allow_nan=False),))

    def _equity(self):
        s=self.state
        result=s['cash']
        for p in s['positions'].values():
            gross=(p['mark']-p['entry'])*p['qty']*(1 if p['side']=='LONG' else -1)
            result+=p['entry']*p['qty']+gross
        return result

    def _exposure(self,including_orders=True):
        s=self.state
        rows=list(s['positions'].values())+(list(s['orders'].values()) if including_orders else [])
        long=sum(p['qty']*p.get('mark',p['price']) for p in rows if p['side']=='LONG')
        short=sum(p['qty']*p.get('mark',p['price']) for p in rows if p['side']=='SHORT')
        return dict(long=long,short=short,gross=long+short,net=long-short)

    def _risk(self,t):
        s=self.state
        equity=self._equity()
        day=t//86400000
        if day!=s['day']:
            s['day']=day;s['dayEquity']=equity
            if s['kill']=='DAILY_LOSS':s['kill']=None
        s['peak']=max(s['peak'],equity)
        dd=max(0,1-equity/s['peak']) if s['peak']>0 else 1
        if equity<=s['dayEquity']*(1-self.c['dailyLossLimit']):s['kill']='DAILY_LOSS'
        if dd>=self.c['maxDrawdown']:s['kill']='MAX_DRAWDOWN'
        return equity,dd

    def _close(self,symbol,p,reference,t,reason,spread,kind='TAKER'):
        side=p['side'];qty=p['qty'];entry=p['entry']
        if kind=='MAKER':
            exit_price=reference;spread_cost=slip=0.;fee=reference*qty*self.c['makerFee']
        else:
            spread_cost=reference*qty*spread/200
            slip=reference*qty*self.c['slippage']
            exit_price=reference-(spread/200+self.c['slippage'])*reference if side=='LONG' else reference+(spread/200+self.c['slippage'])*reference
            fee=exit_price*qty*self.c['takerFee']
        funding=0.0
        total_fee=p['entryFee']+fee
        # grossReference and explicit costs reconcile to netPnL.
        gross_reference=(reference-entry)*qty*(1 if side=='LONG' else -1)
        total_cost=total_fee+spread_cost+slip+funding
        net=gross_reference-total_cost
        self.state['cash']+=entry*qty+gross_reference-fee-spread_cost-slip-funding
        trade=dict(id=p['id'],symbol=symbol,side=side,entryTime=p['entryTime'],exitTime=t,
            entryPrice=entry,exitPrice=exit_price,referenceExitPrice=reference,qty=qty,
            grossPnL=gross_reference,netPnL=net,entryFee=p['entryFee'],exitFee=fee,fees=total_fee,
            spreadCost=spread_cost,slippageCost=slip,fundingCost=funding,fundingNotModelled=self.c['fundingRate'] is None,
            totalCost=total_cost,reason=reason,holdingMinutes=(t-p['entryTime'])/60000,
            regime=p['regime'],edgeClass=p['edgeClass'],modelId=self.state['modelId'],
            activityDecile=p['activityDecile'],volatilityRegime=p['volatilityRegime'],
            confidence=p['confidence'],edgeCostRatio=p['edgeCostRatio'],expectedMovePct=p['expectedMovePct'],
            expectedCostPct=p['expectedCostPct'],expectedNetEdgePct=p['expectedNetEdgePct'],
            marketBreadthRegime=p['marketBreadthRegime'])
        self.db.execute('INSERT INTO directional_trades VALUES(?,?,?)',(p['id'],t,json.dumps(trade,allow_nan=False)))
        self.db.execute('UPDATE directional_positions SET status=?,data=? WHERE id=?',('CLOSED',json.dumps(dict(p,trade=trade)),p['id']))
        del self.state['positions'][symbol]
        return trade

    def _fill_pair(self,pair_id,t,bar_by_time,manual_kill=False):
        orders=[o for o in self.state['orders'].values() if o.get('pairId')==pair_id]
        if len(orders)!=2:
            return
        expired=manual_kill or self.state['kill'] or any(t>=o['expires'] for o in orders)
        bars=[bar_by_time.get(o['symbol'],{}).get(t) for o in orders]
        eligible=all(bars) and all(t>o['submitted'] and t<=o['expires'] and
            (b['low']<o['price'] if o['side']=='LONG' else b['high']>o['price']) and
            o['qty']<=b['volume']*.01 for o,b in zip(orders,bars))
        total=sum(o['price']*o['qty']*(1+self.c['makerFee']) for o in orders)
        if eligible and not manual_kill and not self.state['kill'] and self.state['cash']>=total:
            for o,b in zip(orders,bars):
                fee=o['price']*o['qty']*self.c['makerFee']
                self.state['cash']-=o['price']*o['qty']+fee
                p=dict(o,entry=o['price'],entryFee=fee,entryTime=t,mark=b['close'],exitOrder=None)
                self.state['positions'][o['symbol']]=p
                self.db.execute('INSERT INTO directional_positions VALUES(?,?,?,?)',(o['id'],o['symbol'],'OPEN',json.dumps(p)))
                self.db.execute('UPDATE directional_orders SET status=? WHERE id=?',('FILLED',o['id']))
                del self.state['orders'][o['symbol']]
        elif expired:
            for o in orders:
                self.db.execute('UPDATE directional_orders SET status=? WHERE id=?',('EXPIRED',o['id']))
                del self.state['orders'][o['symbol']]

    def _fill_and_exit(self,symbol,b,t,latest,opportunity,context_ok,manual_kill=False,bar_by_time=None):
        s=self.state;c=self.c
        order=s['orders'].get(symbol)
        if order and t>order['submitted']:
            if order.get('pairId'):
                self._fill_pair(order['pairId'],t,bar_by_time or {},manual_kill)
            else:
                through=b['low']<order['price'] if order['side']=='LONG' else b['high']>order['price']
                enough=b['volume']*.01>=order['qty'] and s['cash']>=order['price']*order['qty']*(1+c['makerFee'])
                if t<=order['expires'] and through and enough and not s['kill'] and not manual_kill:
                    fee=order['price']*order['qty']*c['makerFee']
                    s['cash']-=order['price']*order['qty']+fee
                    p=dict(order,entry=order['price'],entryFee=fee,entryTime=t,mark=b['close'],exitOrder=None)
                    s['positions'][symbol]=p
                    self.db.execute('INSERT INTO directional_positions VALUES(?,?,?,?)',(p['id'],symbol,'OPEN',json.dumps(p)))
                    self.db.execute('UPDATE directional_orders SET status=? WHERE id=?',('FILLED',order['id']))
                    del s['orders'][symbol]
                elif t>=order['expires'] or manual_kill or s['kill']:
                    self.db.execute('UPDATE directional_orders SET status=? WHERE id=?',('EXPIRED',order['id']))
                    del s['orders'][symbol]
        p=s['positions'].get(symbol)
        if not p:return
        p['mark']=b['close']
        if t<=p['entryTime']:return
        side=p['side']
        stop_hit=b['low']<=p['stop'] if side=='LONG' else b['high']>=p['stop']
        spread=opportunity['spreadPct'] if t==latest and opportunity and opportunity.get('spreadPct') is not None else c['maxSpreadPct']
        if stop_hit:
            reference=min(b['open'],p['stop']) if side=='LONG' else max(b['open'],p['stop'])
            self._close(symbol,p,reference,t,'STOP_TAKER',spread)
            return
        exit_order=p.get('exitOrder')
        if exit_order and t>exit_order['submitted']:
            through=b['high']>exit_order['price'] if side=='LONG' else b['low']<exit_order['price']
            if through and t<=exit_order['expires'] and p['qty']<=b['volume']*.01:
                self._close(symbol,p,exit_order['price'],t,'TARGET_MAKER',spread,'MAKER')
                return
            if t>=exit_order['expires']:p['exitOrder']=None
        if t-p['entryTime']>=c['maxHoldMinutes']*60000:
            self._close(symbol,p,b['close'],t,'TIME_STOP',spread)
            return
        # Candle-derived EV can exit; stale book data must never force an exit.
        if t==latest and opportunity and context_ok:
            own=opportunity['evLongPct' if side=='LONG' else 'evShortPct']
            opposite=opportunity['evShortPct' if side=='LONG' else 'evLongPct']
            invalid=opportunity['regime']=='SHOCK' or (side=='LONG' and opportunity['regime']=='TREND_DOWN') or (side=='SHORT' and opportunity['regime']=='TREND_UP')
            if own<=0 or opposite>own+c['minRequiredEvPct'] or invalid:
                self._close(symbol,p,b['close'],t,'DYNAMIC_EV_OR_REGIME',spread)
                return
        if p.get('exitOrder') is None:
            target=b['high']>=p['target'] if side=='LONG' else b['low']<=p['target']
            if target:
                p['exitOrder']=dict(price=p['target'],submitted=t,expires=t+c['orderTtlMinutes']*60000)

    def _place(self,d,bars,now,pair_id=None):
        s=self.state;c=self.c;side=d['action'];symbol=d['symbol']
        if side=='FLAT' or symbol in s['positions'] or symbol in s['orders']:return 'FLAT_OR_EXISTING'
        equity,dd=self._risk(d['time'])
        if s['kill']:return s['kill']
        edge_class=_class(d)
        samples=[json.loads(r[0]) for r in self.db.execute('SELECT data FROM directional_trades ORDER BY time DESC LIMIT 3000')]
        class_stats=expectancy([t for t in samples if t['edgeClass']==edge_class],c['minSampleForEdgeClassification'],c['bootstrapIterations'])
        if class_stats['status']=='REJECTED_EDGE':return 'PAUSED_NEGATIVE_EDGE'
        # Unproven classes collect PAPER evidence at a fixed reduced risk; no loss recovery sizing.
        multiplier=1 if class_stats['status']=='CONFIRMED_POSITIVE_EDGE' else .35
        multiplier*=max(0,1-dd/c['maxDrawdown'])
        price=d['bid'] if side=='LONG' else d['ask']
        if not price or price<=0:return 'NO_BOOK_PRICE'
        stop_distance=max(c['atrStopMultiplier']*d['atr'],price*c['minStopPct']/100)
        cost_per_unit=price*d['expectedCostPct']/100
        desired=equity*c['riskPerTrade']*multiplier/(stop_distance+cost_per_unit)
        exposure=self._exposure()
        budget=min(equity*c['maxSymbolExposure'],equity*c['maxGrossExposure']-exposure['gross'],
            equity*(c['maxLongExposure'] if side=='LONG' else c['maxShortExposure'])-exposure[side.lower()],
            equity*c['maxNetExposure']-(exposure['net'] if side=='LONG' else -exposure['net']),
            s['cash']/(1+c['makerFee']))
        if len(s['positions'])+len(s['orders'])>=c['maxPositions']:return 'PORTFOLIO_LIMIT'
        candidate_returns=_returns(bars)
        correlated=0
        for p in list(s['positions'].values())+list(s['orders'].values()):
            other=p.get('returns') or []
            if correlation(candidate_returns,other)>=c['correlationThreshold']:
                correlated+=p['qty']*p['price']
        budget=min(budget,equity*c['maxCorrelatedExposure']-correlated)
        qty=min(desired,max(0,budget)/price)
        if qty*price<5:return 'PORTFOLIO_LIMIT'
        stamp=d['candleCloseTimestamp']
        oid=f"{s['experimentId']}:{s['modelId']}:{symbol}:1m:{stamp}"
        stop=price-stop_distance if side=='LONG' else price+stop_distance
        target=price+c['atrTargetMultiplier']*d['atr']*(1 if side=='LONG' else -1)
        order=dict(id=oid,symbol=symbol,side=side,price=price,qty=qty,stop=stop,target=target,
            submitted=d['time'],expires=d['time']+c['orderTtlMinutes']*60000,
            expectedMovePct=d['expectedUpMovePct' if side=='LONG' else 'expectedDownMovePct'],
            expectedCostPct=d['expectedCostPct'],expectedNetEdgePct=d['evLongPct' if side=='LONG' else 'evShortPct'],
            confidence=d['confidence'],regime=d['regime'],edgeClass=edge_class,activityDecile=d['activityDecile'],
            volatilityRegime=d['volatilityRegime'],marketBreadthRegime=d['marketBreadthRegime'],
            edgeCostRatio=d['edgeCostRatio'],returns=candidate_returns,pairId=pair_id)
        s['orders'][symbol]=order
        self.db.execute('INSERT INTO directional_orders VALUES(?,?,?,?)',(oid,symbol,'PENDING',json.dumps(order,allow_nan=False)))
        return 'PLACE_'+side+'_MAKER'

    def _utility(self,d,bars):
        equity=max(self._equity(),1)
        _,dd=self._risk(d['time'])
        exposure=self._exposure()
        current_returns=_returns(bars)
        related=sum(1 for p in self.state['positions'].values()
                    if correlation(current_returns,p.get('returns') or [])>=self.c['correlationThreshold'])
        risk=d['atrPct']*.025
        drawdown=dd*.2
        execution=max(0,(d.get('spreadRatio') or 1)-1)*(d.get('spreadPct') or 0)*.1
        corr=related*.02
        long_concentration=exposure['long']/equity*.1
        short_concentration=exposure['short']/equity*.1
        d['utilityPenalties']=dict(risk=risk,drawdown=drawdown,execution=execution,
                                   correlation=corr,longConcentration=long_concentration,shortConcentration=short_concentration)
        d['utilityLong']=d['evLongPct']-risk-drawdown-execution-corr-long_concentration
        d['utilityShort']=d['evShortPct']-risk-drawdown-execution-corr-short_concentration
        d['utilityFlat']=0

    def _mode_action(self,d):
        action=d['action']
        mode=self.c.get('portfolioMode','LONG_SHORT')
        style=self.c.get('signalStyle','HYBRID')
        if mode=='CONTROL_FLAT_BASELINE':return 'FLAT','CONTROL_FLAT_BASELINE'
        if action=='LONG' and mode=='SHORT_ONLY':return 'FLAT','MODE_SHORT_ONLY'
        if action=='SHORT' and mode=='LONG_ONLY':return 'FLAT','MODE_LONG_ONLY'
        if style=='TREND_FOLLOWING' and not ((action=='LONG' and d['regime']=='TREND_UP') or
                                             (action=='SHORT' and d['regime']=='TREND_DOWN')):
            return 'FLAT','STYLE_TREND_FOLLOWING'
        if style=='MEAN_REVERSION' and not (d['regime']=='RANGE' and
            ((action=='LONG' and d['price']<d['vwap']) or (action=='SHORT' and d['price']>d['vwap']))):
            return 'FLAT','STYLE_MEAN_REVERSION'
        return action,None

    def _neutral_pairs(self,opportunities,usable,latest,context_ok,manual_kill):
        if self.c.get('portfolioMode')!='MARKET_NEUTRAL' or not context_ok or manual_kill:
            return {}
        current=[d for d in opportunities if d['time']==latest and self._mode_action(d)[0]!='FLAT']
        pairs=[]
        for long in current:
            if long['action']!='LONG' or long['utilityLong']<=0 or long.get('btcBeta') is None or long['btcBeta']<=.1:continue
            for short in current:
                if short['action']!='SHORT' or short['utilityShort']<=0 or short.get('btcBeta') is None or short['btcBeta']<=.1:continue
                if long['symbol']==short['symbol'] or abs(long['btcBeta']-short['btcBeta'])>.3:continue
                pairs.append((-(long['utilityLong']+short['utilityShort']),long['symbol'],short['symbol'],long,short))
        selected={}
        for _,_,_,long,short in sorted(pairs):
            if long['symbol'] in selected or short['symbol'] in selected:continue
            before=copy.deepcopy(self.state)
            pair_id=f"PAIR:{latest}:{long['symbol']}:{short['symbol']}"
            left=self._place(long,usable[long['symbol']],long['decisionTimestamp'],pair_id)
            right=self._place(short,usable[short['symbol']],short['decisionTimestamp'],pair_id) if left.startswith('PLACE_') else 'PAIR_ABORTED'
            if left.startswith('PLACE_') and right.startswith('PLACE_'):
                a=self.state['orders'][long['symbol']];b=self.state['orders'][short['symbol']]
                beta_budget=min(a['qty']*a['price']*long['btcBeta'],b['qty']*b['price']*short['btcBeta'])
                a['qty']=beta_budget/(a['price']*long['btcBeta'])
                b['qty']=beta_budget/(b['price']*short['btcBeta'])
                if min(a['qty']*a['price'],b['qty']*b['price'])<5:
                    right='PAIR_MIN_NOTIONAL'
                else:
                    for o in (a,b):self.db.execute('UPDATE directional_orders SET data=? WHERE id=?',(json.dumps(o),o['id']))
            if left.startswith('PLACE_') and right.startswith('PLACE_'):
                selected[long['symbol']]=left;selected[short['symbol']]=right
                continue
            for symbol in (long['symbol'],short['symbol']):
                order=self.state['orders'].get(symbol)
                if order and symbol not in before['orders']:
                    self.db.execute('DELETE FROM directional_orders WHERE id=?',(order['id'],))
            self.state.clear();self.state.update(before)
        return selected

    def _labels(self,histories,now):
        for symbol,bars in histories.items():
            by_time={b['time']:i for i,b in enumerate(bars)}
            rows=self.db.execute('''SELECT p.id,p.time,p.data FROM directional_predictions p
                WHERE p.symbol=? AND p.time>=? AND NOT EXISTS
                (SELECT 1 FROM directional_labels l WHERE l.id=p.id AND l.horizon=30)
                ORDER BY p.time''',(symbol,bars[0]['time'] if bars else now)).fetchall()
            for row in rows:
                index=by_time.get(row['time'])
                if index is None:continue
                prediction=json.loads(row['data'])
                for horizon in (5,15,30):
                    if index+horizon>=len(bars):continue
                    if self.db.execute('SELECT 1 FROM directional_labels WHERE id=? AND horizon=?',(row['id'],horizon)).fetchone():continue
                    future=bars[index+1:index+horizon+1]
                    base=bars[index]['close']
                    ret=(future[-1]['close']/base-1)*100
                    mfe=(max(b['high'] for b in future)/base-1)*100
                    mae=(min(b['low'] for b in future)/base-1)*100
                    threshold=prediction['expectedCostPct']
                    label='UP' if ret>threshold else 'DOWN' if ret< -threshold else 'FLAT'
                    data=dict(futureReturnPct=ret,MFE=mfe,MAE=mae,label=label,
                        labelTimestamp=future[-1]['end'],source='CLOSED_CANDLES')
                    self.db.execute('INSERT INTO directional_labels VALUES(?,?,?)',(row['id'],horizon,json.dumps(data)))

    def process(self,histories,ranking,now,micro_store=None,context_ok=True,manual_kill=False):
        with self.lock:
            try:
                self._process(histories,ranking,now,micro_store,context_ok,manual_kill)
            except Exception:
                # Earlier candle commits may already be durable. Reload their
                # cursor and positions before the next retry.
                row=self.db.execute('SELECT data FROM directional_state WHERE id=1').fetchone()
                if row:self.state=json.loads(row['data'])
                self.status.update(state='paused',error='DIRECTIONAL_PROCESS_FAILED')
                raise

    def _process(self,histories,ranking,now,micro_store,context_ok,manual_kill):
        for bars in histories.values():validate_bars(bars,now)
        s=self.state;c=self.c
        latest=now//60000*60000-60000
        markets={r['symbol']:r for r in ranking[:c['topMarkets']]}
        selected=set(markets)|set(s['positions'])|set(s['orders'])|{'BTCUSDT'}
        usable={sym:histories[sym] for sym in sorted(selected) if sym in histories and histories[sym]}
        bar_by_time={symbol:{b['time']:b for b in bars} for symbol,bars in usable.items()}
        if 'BTCUSDT' not in usable:raise ValueError('BTC_REFERENCE_UNAVAILABLE')
        breadth=context({symbol:bars for symbol,bars in histories.items() if bars and bars[-1]['time']==latest})
        btc=histories['BTCUSDT']
        opportunities=[]
        for symbol,bars in usable.items():
            cursor=s['cursors'].get(symbol)
            if cursor is None:cursor=bars[-1]['time']-60000
            moments=[b for b in bars if b['time']>cursor]
            if moments and moments[0]['time']!=cursor+60000:raise ValueError('Missing directional execution candle '+symbol)
            for b in moments:
                t=b['time']
                if t>latest:break
                # No retroactive entries from today's market snapshot.
                is_current=t==latest and now-b['end']<=c['maxDataAgeMs'] and context_ok and not manual_kill
                micro=micro_store.features(symbol,now) if is_current and micro_store else {}
                btc_past=[x for x in btc if x['time']<=t]
                d=evaluate(symbol,bars[:bars.index(b)+1],btc_past,
                           markets.get(symbol),micro,breadth,c,now) if is_current else None
                with self.db:
                    self._risk(t)
                    self._fill_and_exit(symbol,b,t,latest,d,is_current,manual_kill,bar_by_time)
                    if d:
                        stamp=time.time_ns()//1000000
                        receipt=micro_store.candle_receipt(symbol,t) if micro_store else {}
                        d['marketDataTimestamp']=receipt.get('_eventTimestamp',b['end'])
                        d['receivedTimestamp']=receipt.get('_receivedTimestamp')
                        d['dataLatencyMs']=(d['receivedTimestamp']-d['marketDataTimestamp']) if d['receivedTimestamp'] else None
                        d['decisionTimestamp']=stamp
                        d['decisionLatencyMs']=(stamp-d['receivedTimestamp']) if d['receivedTimestamp'] else None
                        d['configHash']=self.config_hash
                        d['microGeneration']=micro_store.generation if micro_store else None
                        if micro_store:
                            with micro_store.lock:
                                d['microCoverageSerial']=micro_store.state.get(symbol,{}).get('coverageSerial',0)
                        else:d['microCoverageSerial']=None
                        d['edgeClass']=_class(d)
                        self._utility(d,bars[:bars.index(b)+1])
                        opportunities.append(d)
                        key=f"{s['experimentId']}:{s['modelId']}:{symbol}:1m:{d['candleCloseTimestamp']}"
                        self.db.execute('INSERT OR IGNORE INTO directional_candidates VALUES(?,?,?,?)',
                                        (key,d['time'],symbol,json.dumps(d,allow_nan=False)))
                    s['cursors'][symbol]=t
                    self._persist()
        # Compare the whole contemporaneous opportunity set before allocating capital.
        # Recover candidate snapshots persisted before a process interruption.
        saved=self.db.execute('SELECT id,time,symbol,data FROM directional_candidates ORDER BY time,symbol').fetchall()
        opportunities=[json.loads(row['data']) for row in saved]
        opportunities.sort(key=lambda d:(d['time']!=latest,-max(d['utilityLong'],d['utilityShort']),d['symbol']))
        with self.db:
            neutral=self._neutral_pairs(opportunities,usable,latest,context_ok,manual_kill)
            for d in opportunities:
                symbol=d['symbol']
                stamp=d['candleCloseTimestamp']
                key=f"{s['experimentId']}:{s['modelId']}:{symbol}:1m:{stamp}"
                if self.db.execute('SELECT 1 FROM directional_decisions WHERE id=?',(key,)).fetchone():
                    self.db.execute('DELETE FROM directional_candidates WHERE id=?',(key,))
                    continue
                action,mode_reason=self._mode_action(d)
                if d['time']!=latest or not context_ok or manual_kill:
                    action='FLAT';d['rejection']='STALE_CANDIDATE'
                elif mode_reason:
                    d['rejection']=mode_reason
                elif self.c.get('portfolioMode')=='MARKET_NEUTRAL' and d['symbol'] not in neutral:
                    action='FLAT';d['rejection']='MARKET_NEUTRAL_PAIR_REQUIRED'
                if action!='FLAT' and d['utilityLong' if action=='LONG' else 'utilityShort']<=0:
                    action='FLAT';d['rejection']='UTILITY_NONPOSITIVE'
                d['action']=action
                d['orderResult']=(neutral[symbol] if symbol in neutral else self._place(d,usable[symbol],now)) if action!='FLAT' else d['rejection'] or 'FLAT'
                d['orderCreatedTimestamp']=time.time_ns()//1000000 if d['orderResult'].startswith('PLACE_') else None
                d['totalLatencyMs']=(d['orderCreatedTimestamp']-d['marketDataTimestamp']
                    if d['orderCreatedTimestamp'] and d['receivedTimestamp'] else None)
                self.db.execute('INSERT INTO directional_predictions VALUES(?,?,?,?)',(key,d['time'],symbol,json.dumps(d,allow_nan=False)))
                self.db.execute('INSERT INTO directional_decisions VALUES(?,?,?,?)',(key,d['time'],symbol,json.dumps(d,allow_nan=False)))
                if d['candidateDirection']!='FLAT':
                    self.db.execute('INSERT INTO directional_shadow VALUES(?,?,?)',(key,d['time'],json.dumps(d,allow_nan=False)))
                    self._tracked_symbols.add(symbol)
                self.db.execute('DELETE FROM directional_candidates WHERE id=?',(key,))
            self._labels(usable,now)
            equity,dd=self._risk(latest)
            exposure=self._exposure(False)
            self.db.execute('INSERT OR REPLACE INTO directional_equity VALUES(?,?)',(latest,json.dumps(dict(time=latest,equity=equity,drawdown=dd,**exposure))))
            self._persist()
        self.opportunities=[d for d in opportunities if d['time']==latest][:30]
        self.status=dict(state='collecting' if context_ok else 'paused',lastCycle=now,error=None if context_ok else 'STALE_OR_INCOMPLETE_MARKET',analysed=len(opportunities))
        self._report_cache=None

    def snapshot(self):
        with self.lock:
            if self._report_cache and time.monotonic()-self._report_cache[0]<10:
                return self._report_cache[1]
            trades=[json.loads(r['data']) for r in self.db.execute('SELECT data FROM directional_trades ORDER BY time')]
            pair_rows=self.db.execute('''SELECT p.data,l.data FROM directional_predictions p
                JOIN directional_labels l ON l.id=p.id WHERE l.horizon=15
                ORDER BY p.time DESC,p.symbol LIMIT 50000''').fetchall()
            pairs=[(json.loads(row[0]),json.loads(row[1])) for row in reversed(pair_rows)]
            minimum=self.c['minSampleForEdgeClassification'];iterations=self.c['bootstrapIterations']
            stats=expectancy(trades,minimum,iterations)
            equity=self._equity()
            mc=monte_carlo(trades,equity,self.c['startingCapital'],minimum,self.c['monteCarloIterations'])
            by_side=groups(trades,'side',minimum,iterations)
            by_regime=groups(trades,'regime',minimum,iterations)
            by_edge=groups(trades,'edgeClass',minimum,iterations)
            segmented=[]
            for trade in trades:
                row=dict(trade)
                row['edgeCostBucket']=str(min(5,int(trade.get('edgeCostRatio') or 0)))
                row['confidenceBucket']=str(min(9,int((trade.get('confidence') or 0)*10)))
                row['holdingBucket']=str(min(30,int(trade.get('holdingMinutes') or 0)//5*5))
                segmented.append(row)
            exposure=self._exposure(False)
            realized=sum(t['netPnL'] for t in trades)
            unrealized=equity-self.c['startingCapital']-realized
            shadow=self.db.execute('''SELECT COUNT(*),SUM(status='OBSERVED'),SUM(status='CENSORED'),
                SUM(CASE WHEN status='OBSERVED' THEN tradeThrough ELSE 0 END)
                FROM (SELECT json_extract(data,'$.status') status,json_extract(data,'$.tradeThrough') tradeThrough
                      FROM directional_shadow_observations WHERE horizon=60)''').fetchone()
            observed=int(shadow[1] or 0)
            through=int(shadow[3] or 0)
            fill_rate=through/observed if observed else None
            fill_stress=dict(status='DIAGNOSTIC_PROXY' if observed>=minimum else 'INSUFFICIENT_EXECUTION_EVIDENCE',
                observedSignals=observed,tradeThrough=through,observedTradeThroughRate=fill_rate,
                scenarios={str(f):fill_rate*f if fill_rate is not None and observed>=minimum else None for f in (1,.9,.75,.5)},
                note='Trade-through rate proxy, not exchange queue-fill probability or realized expectancy')
            costs=cost_stress(trades)
            fragile=(len(trades)>=minimum and costs['1'] is not None and costs['1']>0 and costs['1.25']<=0)
            filter_groups=defaultdict(list)
            for prediction,label in pairs:
                reason=prediction.get('rejection')
                if reason and prediction.get('candidateDirection') in ('LONG','SHORT'):
                    direction=1 if prediction['candidateDirection']=='LONG' else -1
                    filter_groups[reason].append(direction*label['futureReturnPct']-prediction['expectedCostPct'])
            filter_effectiveness={reason:dict(observed=len(values),hypotheticalMeanNetMovePct=sum(values)/len(values),
                                hypotheticalPositiveRate=sum(v>0 for v in values)/len(values))
                                for reason,values in filter_groups.items()}
            recent_shadow=[]
            for row in self.db.execute('SELECT id,data FROM directional_shadow ORDER BY time DESC LIMIT 20'):
                item=json.loads(row['data'])
                outcome=self.db.execute('SELECT data FROM directional_shadow_observations WHERE id=? AND horizon=60',(row['id'],)).fetchone()
                recent_shadow.append(dict(symbol=item['symbol'],direction=item['candidateDirection'],
                    signalTimestamp=item['decisionTimestamp'],rejection=item.get('rejection'),
                    proposedPrice=item.get('bid') if item['candidateDirection']=='LONG' else item.get('ask'),
                    expectedCostPct=item['expectedCostPct'],evLongPct=item['evLongPct'],evShortPct=item['evShortPct'],
                    outcome60s=json.loads(outcome[0]) if outcome else None))
            report=dict(mode='PAPER_ONLY',liveReady=False,status=dict(self.status),configHash=self.config_hash,
                generation=self.c['generation'],capital=self.c['startingCapital'],cash=self.state['cash'],
                equity=equity,netPnL=equity-self.c['startingCapital'],realizedPnL=realized,unrealizedPnL=unrealized,kill=self.state['kill'],
                exposure=exposure,positions=copy.deepcopy(self.state['positions']),orders=copy.deepcopy(self.state['orders']),
                opportunities=copy.deepcopy(self.opportunities),trades=trades[-50:],tradeCount=len(trades),
                longTrades=sum(t['side']=='LONG' for t in trades),shortTrades=sum(t['side']=='SHORT' for t in trades),
                statistics=stats,monteCarlo=mc,bySide=by_side,byRegime=by_regime,edgeClasses=by_edge,
                regimeBySide={f'{r}_{side}':expectancy([t for t in trades if t['regime']==r and t['side']==side],minimum,iterations)
                              for r in ('TREND_UP','TREND_DOWN','RANGE','SHOCK') for side in ('LONG','SHORT')},
                predictionQuality=prediction_quality(pairs),predictionQualityScope='MOST_RECENT_50000_LABELS',
                walkForward=walk_forward(pairs,minimum),
                bootstrapRobustness=bootstrap_robustness(trades,iterations),sequenceStress=sequence_stress(trades,equity,iterations),
                concentration=concentration(trades),costStress=costs,fragileEdge=fragile,fillStress=fill_stress,
                performanceBuckets={key:groups(segmented,key,minimum,iterations) for key in
                    ('symbol','activityDecile','volatilityRegime','marketBreadthRegime','edgeCostBucket','confidenceBucket','holdingBucket')},
                filterEffectiveness=filter_effectiveness,recentShadow=recent_shadow,
                shadow=dict(signals=int(self.db.execute('SELECT COUNT(*) FROM directional_shadow').fetchone()[0]),
                            observations60s=int(shadow[0] or 0),observed=observed,censored=int(shadow[2] or 0)),
                config=self.c)
            self._report_cache=(time.monotonic(),report)
            return report

    def close(self):
        with self.lock:self.db.close()
