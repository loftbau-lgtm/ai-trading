"""Read-only observer of the existing paper experiment; separate diagnostic ledger."""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics as stats
import time
import queue
from microstructure import FeeSchedule, edge_bucket, quantile


class ShadowExecution:
    def __init__(self,store,paper_path,paper_config):
        self.store,self.db,self.lock=store,store.db,store.lock
        self.c,self.paper_config=store.c,paper_config
        self.fees=FeeSchedule(self.c)
        self.config_hash=hashlib.sha256(json.dumps(self.c,sort_keys=True).encode()).hexdigest()
        self.telemetry_queue=queue.Queue(maxsize=10000)
        self.paper=sqlite3.connect(Path(paper_path).resolve().as_uri()+'?mode=ro',uri=True,check_same_thread=False,timeout=5)
        self.paper.row_factory=sqlite3.Row
        with self.lock:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS shadow_signals(id TEXT PRIMARY KEY,time INTEGER,symbol TEXT,data TEXT);
              CREATE INDEX IF NOT EXISTS shadow_time ON shadow_signals(time);
              CREATE TABLE IF NOT EXISTS shadow_observations(id TEXT,horizon INTEGER,data TEXT,PRIMARY KEY(id,horizon));
              CREATE TABLE IF NOT EXISTS rejected_signals(id TEXT PRIMARY KEY,reason TEXT,data TEXT);
              CREATE TABLE IF NOT EXISTS execution_quality(id TEXT PRIMARY KEY,data TEXT);
              CREATE TABLE IF NOT EXISTS feature_history(symbol TEXT,time INTEGER,volatility REAL,PRIMARY KEY(symbol,time));
              CREATE TABLE IF NOT EXISTS shadow_pending(id TEXT PRIMARY KEY,data TEXT);
              CREATE TABLE IF NOT EXISTS execution_timestamps(id TEXT PRIMARY KEY,time INTEGER,data TEXT);
            ''')
            row=self.db.execute("SELECT data FROM micro_meta WHERE key='paperCursor'").fetchone()
            self.cursor=int(json.loads(row[0])) if row else self.paper.execute('SELECT COALESCE(MAX(rowid),0) FROM decisions').fetchone()[0]
            with self.db:self.db.execute("INSERT OR REPLACE INTO micro_meta VALUES('paperCursor',?)",(json.dumps(self.cursor),))
            with self.db:self.db.execute('INSERT OR IGNORE INTO micro_meta VALUES(?,?)',('config:'+self.config_hash,json.dumps(self.c)))
            self.pending={r['id']:json.loads(r['data']) for r in self.db.execute('SELECT * FROM shadow_pending')}
            # Never pretend uninterrupted coverage across restart.
            for item in self.pending.values():item['continuous']=False
            if not self.db.execute("SELECT 1 FROM micro_meta WHERE key='volatilitySeeded'").fetchone():
                # Seed only historical features, never fabricate historical shadow paths.
                cutoff=int(time.time()*1000)-self.c['volatilityHistoryMinutes']*60000
                with self.db:
                    for row in self.paper.execute('SELECT data FROM decisions ORDER BY rowid DESC LIMIT 30000'):
                        d=json.loads(row[0]);value=d.get('volatility')
                        if value is not None and d['timestamp']>=cutoff:
                            self.db.execute('INSERT OR IGNORE INTO feature_history VALUES(?,?,?)',(d['symbol'],d['timestamp'],value))
                    self.db.execute("INSERT INTO micro_meta VALUES('volatilitySeeded','true')")
        self.by_symbol=defaultdict(set)
        for key,item in self.pending.items():self.by_symbol[item['symbol']].add(key)
        self.store.listeners.append(self.on_event)
        self.btc={};self.last_report=None;self.last_report_at=0

    def enqueue_telemetry(self,events):
        for event in events:self.telemetry_queue.put_nowait(event)

    def _regime(self,symbol,t,value):
        rows=[r[0] for r in self.db.execute('SELECT volatility FROM feature_history WHERE symbol=? AND time<? AND time>=? ORDER BY time',
                                           (symbol,t,t-self.c['volatilityHistoryMinutes']*60000))]
        if value is None or len(rows)<self.c['minVolatilitySamples']:return 'UNKNOWN',None
        pct=100*sum(v<=value for v in rows)/len(rows)
        a,b,c=self.c['volatilityPercentiles']
        return ('LOW' if pct<a else 'NORMAL' if pct<b else 'HIGH' if pct<c else 'EXTREME'),pct

    def market_regime(self,now):
        f=self.btc
        if not f or now-f.get('timestamp',0)>180000:return 'UNKNOWN'
        if abs(f.get('zReturn',0))>self.paper_config['BTC_SHOCK_Z']:return 'SHOCK'
        if f.get('trendStrength',0)>self.paper_config['TREND_THRESHOLD']:
            return 'TREND_UP' if f.get('EMA20',0)>f.get('EMA50',0) else 'TREND_DOWN'
        return 'RANGE'

    def poll_paper(self,now=None):
        now=int(time.time()*1000) if now is None else now
        rows=self.paper.execute('SELECT rowid,key,data FROM decisions WHERE rowid>? ORDER BY rowid LIMIT 2000',(self.cursor,)).fetchall()
        with self.lock,self.db:
            while True:
                try:event=self.telemetry_queue.get_nowait()
                except queue.Empty:break
                self.db.execute('INSERT OR IGNORE INTO execution_timestamps VALUES(?,?,?)',(event['id'],event['decisionTimestamp'],json.dumps(event)))
            # BTC reference from this batch is selected as of each signal, not future time.
            for row in rows:
                d=json.loads(row['data'])
                if d['symbol']=='BTCUSDT' and 'zReturn' in d:self.btc=d
                value=d.get('volatility');stamp=d['timestamp'];symbol=d['symbol']
                regime,pct=self._regime(symbol,stamp,value)
                if value is not None:self.db.execute('INSERT OR IGNORE INTO feature_history VALUES(?,?,?)',(symbol,stamp,value))
                candidate=(d.get('zReturn',0)<=self.paper_config['Z_RETURN_ENTRY'] or d.get('priceZ',0)<=self.paper_config['PRICE_Z_ENTRY'] or d.get('signal') in ('BUY','LONG_CANDIDATE'))
                if candidate and d['decision'] not in ('PAPER_MAKER_FILL','STOP_LOSS','TIME_STOP','MEAN_REVERSION_MAKER'):
                    self.capture(row['key'],d,now,regime,pct)
                self.cursor=row['rowid']
            self.db.execute("INSERT OR REPLACE INTO micro_meta VALUES('paperCursor',?)",(json.dumps(self.cursor),))
            self.db.execute('DELETE FROM feature_history WHERE time<?',(now-self.c['volatilityHistoryMinutes']*60000,))
            self.db.execute('DELETE FROM execution_timestamps WHERE time<?',(now-self.c['aggregateRetentionDays']*86400000,))
            # Closed paper fills remain exact, copied read-only, never revalued.
            for row in self.paper.execute('SELECT key,data FROM closed_trades'):
                if self.db.execute('SELECT 1 FROM shadow_signals WHERE id=?',(row['key'],)).fetchone():
                    t=json.loads(row['data']);t.update(paperFilled=True,closed=True,totalExecutionCost=t['fees']+t['spreadCost']+t['slippageCost'])
                    entry_fee=t['qty']*t['entry']*self.paper_config['MAKER_FEE']
                    t['makerFee']=t['fees'] if t['reason']=='MEAN_REVERSION_MAKER' else entry_fee
                    t['takerFee']=t['fees']-t['makerFee']
                    gross_edge=t['qty']*t['entry']*t['expectedEdge']/100
                    t['costAsPctOfGrossEdge']=t['totalExecutionCost']/gross_edge*100 if gross_edge>0 else None
                    self.db.execute('INSERT OR REPLACE INTO execution_quality VALUES(?,?)',(row['key'],json.dumps(t)))
            state=self.paper.execute('SELECT data FROM portfolio WHERE id=1').fetchone()
            if state:
                for p in json.loads(state[0])['positions'].values():
                    if self.db.execute('SELECT 1 FROM shadow_signals WHERE id=?',(p['id'],)).fetchone():
                        self.db.execute('INSERT OR IGNORE INTO execution_quality VALUES(?,?)',(p['id'],json.dumps(dict(paperFilled=True,closed=False,entryFee=p['entryFee']))))

    def capture(self,key,d,now,regime='UNKNOWN',vol_pct=None):
        with self.lock:
            if self.db.execute('SELECT 1 FROM shadow_signals WHERE id=?',(key,)).fetchone():return
            timing_row=self.db.execute('SELECT data FROM execution_timestamps WHERE id=?',(key,)).fetchone()
            timing=json.loads(timing_row[0]) if timing_row else {}
            decision_at=timing.get('decisionTimestamp')
            symbol=d['symbol'];m=self.store.features(symbol,decision_at or now);book=m if m.get('bid') else None
            receipt=self.store.candle_receipt(symbol,(d.get('sourceBar') or {}).get('time',-1))
            received_at=receipt.get('_receivedTimestamp');event_at=receipt.get('_eventTimestamp')
            clock_offset=receipt.get('_clockDriftMs')
            if not decision_at or (received_at or now)>decision_at:received_at=event_at=None
            price=d.get('close') or (book or {}).get('mid')
            limit=(book or {}).get('bid')
            target=min(d.get('VWAP',price),d.get('SMA20',price)) if price else None
            edge=(target-limit)/limit*100 if limit and target else d.get('expectedEdge')
            costs=self.fees.estimate(m.get('spreadPct'),edge)
            components=dict(m['components'],volatility=1-(vol_pct if vol_pct is not None else 100)/100)
            score=100*sum(self.c['weights'][k]*components.get(k,0) for k in self.c['weights'])/sum(self.c['weights'].values())
            rejects=list(m['diagnosticRejections'])
            if vol_pct is None:rejects.append('REJECT_VOLATILITY_WARMUP')
            if score<self.c['minMicrostructureScore']:rejects.append('REJECT_MICROSTRUCTURE')
            if costs['edgeCostRatio'] is None or costs['edgeCostRatio']<=1:rejects.append('REJECT_COST_CONSUMPTION')
            if now-d['timestamp']>self.paper_config['MAX_DATA_AGE_MS']:rejects.append('REJECT_STALE_DATA')
            activity=d.get('activityPercentile')
            if activity is None:activity=(d.get('marketContext') or {}).get('activityPercentile')
            decile=min(10,int(activity//10)+1) if activity is not None else None
            signal=dict(id=key,symbol=symbol,signalTimestamp=d['timestamp'],observationStartTimestamp=now,
                        signalPrice=price,proposedLimitPrice=limit,side='BUY',baselineDecision=d['decision'],
                        diagnosticDecision=rejects[0] if rejects else 'SHADOW_ELIGIBLE',diagnosticRejections=rejects,
                        activityScore=d.get('activityScore'),activityDecile=decile,microstructureScore=score,
                        zReturn=d.get('zReturn'),priceZ=d.get('priceZ'),ATR=d.get('ATR'),volatility=d.get('volatility'),
                        volatilityRegime=regime,volatilityPercentile=vol_pct,marketRegime=self.market_regime(now),
                        expectedEdge=edge,expectedCost=costs['totalExecutionCost'],costs=costs,edgeCostRatio=costs['edgeCostRatio'],
                        edgeCostBucket=edge_bucket(costs['edgeCostRatio']),marketDataTimestamp=event_at,
                        receivedTimestamp=received_at,decisionTimestamp=decision_at,orderCreatedTimestamp=timing.get('orderCreatedTimestamp'),
                        decisionObservedTimestamp=now,marketDataLatency=received_at-event_at+clock_offset if received_at is not None and clock_offset is not None else None,
                        marketDataLatencyRaw=received_at-event_at if received_at is not None else None,clockDriftMs=clock_offset,
                        decisionLatency=decision_at-received_at if received_at is not None else None,
                        totalSignalLatency=timing['orderCreatedTimestamp']-event_at+clock_offset if timing.get('orderCreatedTimestamp') and event_at is not None and clock_offset is not None else None,
                        signalObservationLagMs=now-d['timestamp'],latencyNote='Decision timestamp is measured after evaluation; market latency available only for causally prior WS candle receipts, never inferred for bookTicker.',
                        spread=m.get('spreadPct'),spreadRatio=m.get('spreadRatio'),bookImbalance=m.get('bookImbalance'),
                        tradeFlowImbalance=m.get('tradeFlowImbalance'),microstructure=m,
                        **{k:(book or {}).get(k) for k in ('bid','ask','bidQty','askQty')},
                        configHash=self.config_hash)
            self.db.execute('INSERT INTO shadow_signals VALUES(?,?,?,?)',(key,now,symbol,json.dumps(signal,allow_nan=False)))
            if d['decision'].startswith('REJECT_') or rejects:
                self.db.execute('INSERT INTO rejected_signals VALUES(?,?,?)',(key,d['decision'] if d['decision'].startswith('REJECT_') else rejects[0],json.dumps(signal)))
            # Missing initial quote makes this experiment censored, not a loss/fill.
            pending=dict(id=key,symbol=symbol,start=now,limit=limit,reference=(book or {}).get('mid'),
                         coverageSerial=self.store.state.get(symbol,{}).get('coverageSerial',0),
                         generation=self.store.generation,continuous=bool(book and not any(r in rejects for r in ('REJECT_STALE_DATA','REJECT_FEED_DISCONNECTED','REJECT_CLOCK_DRIFT','REJECT_TRADE_STREAM_GAP'))),
                         touch=False,tradeThrough=False,bookTouch=False,mfe=0.,mae=0.,lastPrice=None,observed=[],signal=signal)
            pending['horizons']=sorted(set(self.c['executionHorizonsSeconds']+self.c['outcomeHorizonsSeconds']))
            self.pending[key]=pending;self.by_symbol[symbol].add(key)
            self.db.execute('INSERT INTO shadow_pending VALUES(?,?)',(key,json.dumps(pending)))

    def on_event(self,kind,event):
        # Called under the same RLock as the collector, no lock inversion.
        for key in tuple(self.by_symbol.get(event['symbol'],())):
            p=self.pending[key]
            if event['receivedTimestamp']<p['start'] or not p['limit']:continue
            if kind=='trade' and event['timestamp']<p['start']+(self.store.clock_drift or 0):continue
            if event['generation']!=p['generation']:p['continuous']=False
            if self.store.state.get(p['symbol'],{}).get('coverageSerial',0)!=p.get('coverageSerial',0):p['continuous']=False
            if kind=='book':
                p['bookTouch']|=event['ask']<=p['limit']
                price=event['mid']
            else:
                price=event['price']
                p['touch']|=price==p['limit']
                p['tradeThrough']|=price<p['limit']
            move=(price/p['reference']-1)*100 if p['reference'] else 0
            p['mfe']=max(p['mfe'],move);p['mae']=min(p['mae'],move);p['lastPrice']=price

    def advance(self,now=None):
        now=int(time.time()*1000) if now is None else now
        default_horizons=sorted(set(self.c['executionHorizonsSeconds']+self.c['outcomeHorizonsSeconds']))
        with self.lock,self.db:
            for key,p in list(self.pending.items()):
                changed=False
                horizons=p.get('horizons',default_horizons)
                if self.store.generation!=p['generation']:p['continuous']=False
                if self.store.state.get(p['symbol'],{}).get('coverageSerial',0)!=p.get('coverageSerial',0):p['continuous']=False
                for horizon in horizons:
                    if horizon in p['observed'] or now<p['start']+horizon*1000:continue
                    deadline=p['start']+horizon*1000
                    book=self.store.books.get(p['symbol'])
                    valid=bool(p['continuous'] and book and book['timestamp']<=now and now-deadline<=self.c['observationToleranceMs'] and now-book['timestamp']<=self.c['maxBookAgeMs'])
                    price=book['mid'] if valid else None
                    move=(price/p['reference']-1)*100 if price and p['reference'] else None
                    cost=p['signal']['expectedCost']
                    net=(move-cost)*self.c['shadowNotional']/100 if move is not None and cost is not None else None
                    obs=dict(horizonSeconds=horizon,dueTimestamp=deadline,observedTimestamp=now,actualAgeMs=now-p['start'],
                             status='OBSERVED' if valid else 'CENSORED',bestBid=book['bid'] if valid else None,bestAsk=book['ask'] if valid else None,
                             touch=p['touch'] if valid else None,tradeThrough=p['tradeThrough'] if valid else None,bookTouch=p['bookTouch'] if valid else None,
                             priceMovedAway=move>0 if move is not None else None,priceMovedThrough=p['tradeThrough'] if valid else None,
                             maxFavorableExcursion=p['mfe'] if valid else None,maxAdverseExcursion=p['mae'] if valid else None,
                             futureMovePct=move,estimatedNetPnLIfAccepted=net,notional=self.c['shadowNotional'],
                             outcomeModel='UNCONDITIONAL_MID_MARKOUT_MINUS_ESTIMATED_COST_NOT_A_TRADE')
                    self.db.execute('INSERT OR IGNORE INTO shadow_observations VALUES(?,?,?)',(key,horizon,json.dumps(obs)))
                    p['observed'].append(horizon)
                    changed=True
                if len(p['observed'])==len(horizons):
                    self.db.execute('DELETE FROM shadow_pending WHERE id=?',(key,));self.by_symbol[p['symbol']].discard(key);del self.pending[key]
                elif changed:self.db.execute('INSERT OR REPLACE INTO shadow_pending VALUES(?,?)',(key,json.dumps(p)))

    def tracked_symbols(self):
        with self.lock:return sorted({p['symbol'] for p in self.pending.values()})

    @staticmethod
    def _trade_metrics(rows,signals,filled,accepted):
        pnl=[r['netPnL'] for r in rows];wins=[p for p in pnl if p>0];losses=[-p for p in pnl if p<0]
        cumulative=peak=dd=0
        for p in pnl:cumulative+=p;peak=max(peak,cumulative);dd=max(dd,peak-cumulative)
        return dict(signals=signals,acceptedSignals=accepted,trades=len(rows),paperFillRate=filled/accepted if accepted else None,
                    filledFractionOfAllSignals=filled/signals if signals else None,
                    netPnL=sum(pnl),netExpectancy=stats.mean(pnl) if pnl else None,profitFactor=sum(wins)/sum(losses) if losses else None,
                    winRate=len(wins)/len(pnl) if pnl else None,maxDrawdownQuote=dd,
                    averageHoldingMinutes=stats.mean(r['holdingMinutes'] for r in rows) if rows else None,
                    costPerTrade=stats.mean(r['totalExecutionCost'] for r in rows) if rows else None)

    def report(self,now=None):
        now=int(time.time()*1000) if now is None else now
        with self.lock:
            if self.last_report and now-self.last_report_at<10000:return self.last_report
            signals={r['id']:json.loads(r['data']) for r in self.db.execute('SELECT id,data FROM shadow_signals ORDER BY time')}
            signals={k:v for k,v in signals.items() if v['configHash']==self.config_hash}
            observations={(r['id'],r['horizon']):json.loads(r['data']) for r in self.db.execute('SELECT * FROM shadow_observations') if r['id'] in signals}
            execution={r['id']:json.loads(r['data']) for r in self.db.execute('SELECT * FROM execution_quality') if r['id'] in signals}
            groups={};probabilities={};filters=defaultdict(list)
            for dimension in ('edgeCostBucket','activityDecile','volatilityRegime','marketRegime'):
                buckets=defaultdict(list)
                for key,s in signals.items():buckets[str(s[dimension])].append(key)
                groups[dimension]={}
                for bucket,keys in buckets.items():
                    trades=sorted([execution[k] for k in keys if execution.get(k,{}).get('closed')],key=lambda r:r['time'])
                    groups[dimension][bucket]=self._trade_metrics(trades,len(keys),sum(execution.get(k,{}).get('paperFilled',False) for k in keys),sum(signals[k]['baselineDecision']=='PLACE_LIMIT_MAKER' for k in keys))
            proxy=defaultdict(list)
            for key,s in signals.items():
                for h in self.c['executionHorizonsSeconds']:
                    o=observations.get((key,h))
                    if not o or o['status']!='OBSERVED':continue
                    spread=s['spread'];distance=(s['proposedLimitPrice']/s['microstructure']['mid']-1)*100 if s['proposedLimitPrice'] and s['microstructure'].get('mid') else None
                    buckets=dict(spreadBucket='<0.02%' if spread is not None and spread<.02 else '0.02–0.05%' if spread is not None and spread<.05 else '>=0.05%' if spread is not None else 'UNKNOWN',
                                 distanceFromMid='<-0.05%' if distance is not None and distance<-.05 else '-0.05–0%' if distance is not None else 'UNKNOWN',
                                 activityDecile=str(s['activityDecile']),volatilityRegime=s['volatilityRegime'],
                                 bookImbalance='UNKNOWN' if s['bookImbalance'] is None else 'NEGATIVE' if s['bookImbalance']<-.33 else 'POSITIVE' if s['bookImbalance']>.33 else 'NEUTRAL',orderAge=str(h))
                    for dimension,bucket in buckets.items():
                        # Each dimension has a separate horizon, avoiding repeated-label inflation.
                        proxy[(dimension,bucket,h)].append(o)
                reasons=[('BASELINE',s['baselineDecision'])] if s['baselineDecision'].startswith('REJECT_') else []
                reasons.extend(('DIAGNOSTIC',reason) for reason in set(s['diagnosticRejections']))
                for layer,reason in reasons:
                    for h in self.c['outcomeHorizonsSeconds']:
                        o=observations.get((key,h))
                        filters[(layer,reason,h)].append(o)
            for (dimension,bucket,h),values in proxy.items():
                n=len(values);hits=sum(o['tradeThrough'] for o in values);p=hits/n
                z=1.96;center=(p+z*z/(2*n))/(1+z*z/n);width=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
                probabilities.setdefault(dimension,[]).append(dict(bucket=bucket,ageSeconds=h,samples=n,touchRate=sum(o['touch'] for o in values)/n,
                    tradeThroughRate=p,estimatedMakerFillProbability=p if n>=self.c['minProbabilitySamples'] else None,
                    confidenceInterval95=[center-width,center+width],label='TRADE_THROUGH_PROXY_NOT_QUEUE_FILL_PROBABILITY'))
            filter_report=[]
            for (layer,reason,h),items in sorted(filters.items()):
                valid=[o for o in items if o and o['status']=='OBSERVED' and o['estimatedNetPnLIfAccepted'] is not None]
                moves=[o['futureMovePct'] for o in valid];nets=[o['estimatedNetPnLIfAccepted'] for o in valid]
                filter_report.append(dict(layer=layer,filter=reason,horizonSeconds=h,signalsRejected=len(items),observed=len(valid),
                    hypotheticalWins=sum(n>0 for n in nets),hypotheticalLosses=sum(n<0 for n in nets),
                    averageFutureMove=stats.mean(moves) if moves else None,medianFutureMove=stats.median(moves) if moves else None,
                    MFE=stats.mean(o['maxFavorableExcursion'] for o in valid) if valid else None,
                    MAE=stats.mean(o['maxAdverseExcursion'] for o in valid) if valid else None,estimatedNetPnLIfAccepted=sum(nets) if nets else None))
            self.last_report=dict(signals=len(signals),baselineRejected=sum(s['baselineDecision'].startswith('REJECT_') for s in signals.values()),
                diagnosticRejected=sum(bool(s['diagnosticRejections']) for s in signals.values()),paperFilled=sum(e.get('paperFilled',False) for e in execution.values()),
                pending=len(self.pending),observations=len(observations),censored=sum(o['status']!='OBSERVED' for o in observations.values()),
                groups=groups,filterEffectiveness=filter_report,makerProbability=probabilities,
                recentSignals=list(signals.values())[-30:],liveReady=False,diagnosticOnly=True,configHash=self.config_hash,
                interpretation='Markouts are conditional on observed samples, not causal filter effectiveness. Maker probability is an uncalibrated trade-through proxy.')
            self.last_report_at=now
            return self.last_report
