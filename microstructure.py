"""Bounded public-market cache. No access to paper cash, orders or positions."""
from collections import defaultdict, deque
import json
import math
from pathlib import Path
import sqlite3
import statistics as stats
import threading
import time
from adaptive import validate_bars


def load_config():
    c=json.loads(Path(__file__).with_name('microstructure_config.json').read_text())
    if c['diagnosticOnly'] is not True: raise ValueError('Shadow layer must remain diagnostic')
    if any(not math.isfinite(float(v)) or v < 0 for v in c['weights'].values()) or sum(c['weights'].values()) <= 0:raise ValueError('Invalid weights')
    if not 1 <= c['maxMicroSymbols'] <= 600:raise ValueError('Subscription bound')
    for key in ('maxBookAgeMs','maxTradeAgeMs','maxClockDriftMs','minHistoryMs','minSpreadSamples','minProbabilitySamples','volatilityHistoryMinutes','minVolatilitySamples','liquidityReferenceNotional','aggregateRetentionDays','candleRetentionMinutes','observationToleranceMs','shadowNotional','maxSpreadPct','spreadShockThreshold'):
        if not isinstance(c[key],(int,float)) or not math.isfinite(c[key]) or c[key]<=0:raise ValueError('Invalid micro config: '+key)
    for key in ('makerFeePct','takerFeePct','slippagePctPerSide'):
        if not 0<=c[key]<=5:raise ValueError('Invalid fee/slippage')
    if c['bookSampleMs']<1000 or c['rawBookRetentionMinutes']<15:raise ValueError('Unsafe sampling/retention')
    if len(c['volatilityPercentiles'])!=3 or sorted(c['volatilityPercentiles'])!=c['volatilityPercentiles']:raise ValueError('Invalid regime percentiles')
    for name in ('executionHorizonsSeconds','outcomeHorizonsSeconds'):
        if not c[name] or any(type(v) is not int or not 1<=v<=1800 for v in c[name]):raise ValueError('Invalid horizons')
    return c


def quantile(values,p):
    if not values:return None
    ordered=sorted(values);i=(len(ordered)-1)*p;lo=int(i);hi=min(lo+1,len(ordered)-1)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(i-lo)


def edge_bucket(ratio):
    if ratio is None:return 'UNKNOWN'
    for edge,label in ((1,'<1.0'),(1.5,'1.0–1.5'),(2,'1.5–2.0'),(3,'2.0–3.0'),(5,'3.0–5.0')):
        if ratio < edge:return label
    return '>=5.0'


class FeeSchedule:
    """Replaceable provider; percentages, no private API access."""
    def __init__(self,config):self.c=config
    def estimate(self,spread,gross_edge):
        if spread is None:return dict(makerFee=self.c['makerFeePct'],takerFee=self.c['takerFeePct'],spreadCost=None,slippageCost=None,totalExecutionCost=None,costAsPctOfGrossEdge=None,costConsumption=None,edgeCostRatio=None,unit='PERCENT_OF_NOTIONAL')
        slip=2*self.c['slippagePctPerSide']
        cost=self.c['makerFeePct']+self.c['takerFeePct']+spread+slip
        return dict(makerFee=self.c['makerFeePct'],takerFee=self.c['takerFeePct'],spreadCost=spread,slippageCost=slip,
                    totalExecutionCost=cost,costAsPctOfGrossEdge=cost/gross_edge*100 if gross_edge and gross_edge>0 else None,
                    costConsumption=cost/gross_edge if gross_edge and gross_edge>0 else None,
                    edgeCostRatio=gross_edge/cost if gross_edge is not None and cost>0 else None,unit='PERCENT_OF_NOTIONAL')


class MicrostructureStore:
    def __init__(self,path,config=None):
        self.c=config or load_config();self.lock=threading.RLock()
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=30,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS book_samples(symbol TEXT,time INTEGER,data TEXT,PRIMARY KEY(symbol,time));
          CREATE TABLE IF NOT EXISTS market_microstructure(symbol TEXT,time INTEGER,data TEXT,PRIMARY KEY(symbol,time));
          CREATE TABLE IF NOT EXISTS trade_flow(symbol TEXT,time INTEGER,data TEXT,PRIMARY KEY(symbol,time));
          CREATE TABLE IF NOT EXISTS stream_state(symbol TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS ws_candles(symbol TEXT,time INTEGER,data TEXT,PRIMARY KEY(symbol,time));
          CREATE TABLE IF NOT EXISTS quality_events(id INTEGER PRIMARY KEY,time INTEGER,kind TEXT,symbol TEXT);
          CREATE TABLE IF NOT EXISTS micro_meta(key TEXT PRIMARY KEY,data TEXT);
        ''')
        self.books={};self.samples=defaultdict(deque);self.flows={};self.state={};self.dirty=set();self.dirty_flow=set()
        self.listeners=[];self.last_flush=0;self.connections={};self.clock_drift=None
        self.counters=dict(missingCandles=0,websocketReconnects=0,tradeStreamGap=0,invalidEvents=0,duplicateEvents=0)
        row=self.db.execute("SELECT data FROM micro_meta WHERE key='counters'").fetchone()
        if row:self.counters.update(json.loads(row[0]))
        for row in self.db.execute('SELECT * FROM stream_state'):self.state[row['symbol']]=json.loads(row['data'])
        for state in self.state.values():state.pop('tradeCoverageStart',None)
        cutoff=int(time.time()*1000)-self.c['rawBookRetentionMinutes']*60000
        for row in self.db.execute('SELECT * FROM book_samples WHERE time>=? ORDER BY time',(cutoff,)):
            book=json.loads(row['data']);self.samples[row['symbol']].append(book);self.books[row['symbol']]=book
        for row in self.db.execute('SELECT * FROM trade_flow WHERE time>=?',(int(time.time()*1000)-120000,)):
            self.flows[(row['symbol'],row['time'])]=json.loads(row['data'])
        # A restart isn't continuous observation; consumers use this generation.
        self.generation=int(time.time_ns())

    def event(self,event,received=None):
        received=received if received is not None else int(time.time()*1000)
        try:
            data=event.get('data',event);symbol=data.get('s')
            if not isinstance(symbol,str) or not symbol:return False
            with self.lock:
                state=self.state.setdefault(symbol,{})
                if all(k in data for k in ('u','b','B','a','A')):
                    uid=int(data['u'])
                    if uid<=state.get('bookId',-1):self.counters['duplicateEvents']+=1;return False
                    bid,bq,ask,aq=(float(data[k]) for k in ('b','B','a','A'))
                    if not all(math.isfinite(v) and v>0 for v in (bid,bq,ask,aq)) or bid>=ask:raise ValueError('Crossed book')
                    mid=(bid+ask)/2
                    if not math.isfinite(mid) or not math.isfinite(bq+aq):raise ValueError('Overflowed book')
                    book=dict(timestamp=received,receivedTimestamp=received,marketDataTimestamp=None,timestampSource='LOCAL_RECEIVE_NO_EXCHANGE_TIME',
                              symbol=symbol,bid=bid,bidQty=bq,ask=ask,askQty=aq,mid=mid,spreadAbs=ask-bid,spreadPct=(ask-bid)/mid*100,
                              bookImbalance=(bq-aq)/(bq+aq),updateId=uid,generation=self.generation)
                    state['bookId']=uid;self.books[symbol]=book;self.dirty.add(symbol)
                    self._notify('book',book)
                elif data.get('e')=='aggTrade':
                    uid=int(data['a']);stamp=int(data['T']);price=float(data['p']);qty=float(data['q']);count=int(data['l'])-int(data['f'])+1
                    if uid<=state.get('tradeId',-1):self.counters['duplicateEvents']+=1;return False
                    if type(data['m']) is not bool or min(price,qty,count,stamp)<=0 or not all(math.isfinite(v) for v in (price,qty)) or stamp>received+self.c['maxClockDriftMs']:raise ValueError('Invalid trade')
                    previous=state.get('tradeId')
                    gap=previous is not None and uid!=previous+1
                    if gap:
                        self.counters['tradeStreamGap']+=1;state['gapAt']=received
                        state['coverageSerial']=state.get('coverageSerial',0)+1
                    state.update(tradeId=uid,tradeTime=stamp,tradeReceived=received)
                    state.setdefault('tradeCoverageStart',received)
                    minute=stamp//60000*60000;key=(symbol,minute)
                    flow=self.flows.setdefault(key,dict(symbol=symbol,time=minute,buyAggressiveVolume=0.,sellAggressiveVolume=0.,buyTradeCount=0,sellTradeCount=0,aggregateEvents=0,complete=False,gap=False))
                    side='sell' if data['m'] else 'buy'  # buyer maker => seller is aggressor
                    flow[side+'AggressiveVolume']+=qty;flow[side+'TradeCount']+=count;flow['aggregateEvents']+=1;flow['gap']|=gap
                    self.dirty_flow.add(key);self.dirty.add(symbol)
                    self._notify('trade',dict(symbol=symbol,price=price,qty=qty,timestamp=stamp,receivedTimestamp=received,aggressor=side,generation=self.generation))
                elif data.get('e')=='kline':
                    k=data['k']
                    if k.get('x') is not True or k.get('i')!='1m':return False
                    bar=dict(time=int(k['t']),end=int(k['T']),open=float(k['o']),high=float(k['h']),low=float(k['l']),close=float(k['c']),volume=float(k['v']),turnover=float(k['q']),trades=int(k['n']))
                    event_time=int(data.get('E',bar['end']))
                    if event_time>received+self.c['maxClockDriftMs']:raise ValueError('Future exchange event')
                    validate_bars([bar],max(received+(self.clock_drift or 0),event_time+1))
                    if bar['time']<=state.get('candleTime',-1):self.counters['duplicateEvents']+=1;return False
                    if state.get('candleTime') is not None and bar['time']>state['candleTime']+60000:self.counters['missingCandles']+=(bar['time']-state['candleTime'])//60000-1
                    state['candleTime']=bar['time'];self.dirty.add(symbol)
                    with self.db:self.db.execute('INSERT OR IGNORE INTO ws_candles VALUES(?,?,?)',(symbol,bar['time'],json.dumps(dict(bar,_receivedTimestamp=received,_eventTimestamp=int(data.get('E',bar['end'])),_clockDriftMs=self.clock_drift))))
                else:return False
            return True
        except (ValueError,TypeError,KeyError,OverflowError):
            with self.lock:self.counters['invalidEvents']+=1
            return False

    def _notify(self,kind,event):
        for listener in self.listeners:listener(kind,event)

    def connection(self,name,connected,reconnect=False):
        with self.lock:
            self.connections[name]=connected
            if not connected and not name.startswith('candles-'):
                self.generation+=1
                for state in self.state.values():state.pop('tradeCoverageStart',None)
            if reconnect:
                self.counters['websocketReconnects']+=1
                with self.db:self.db.execute('INSERT INTO quality_events(time,kind,symbol) VALUES(?,?,?)',(int(time.time()*1000),'RECONNECT',name))

    def invalidate_symbols(self,symbols):
        with self.lock:
            for symbol in symbols:
                state=self.state.setdefault(symbol,{})
                state['coverageSerial']=state.get('coverageSerial',0)+1
                state.pop('tradeCoverageStart',None);self.dirty.add(symbol)

    def closed_bars(self,symbol,start,end):
        with self.lock:
            return [{k:v for k,v in json.loads(r[0]).items() if not k.startswith('_')} for r in self.db.execute('SELECT data FROM ws_candles WHERE symbol=? AND time BETWEEN ? AND ? ORDER BY time',(symbol,start,end))]

    def candle_receipt(self,symbol,stamp):
        with self.lock:
            row=self.db.execute('SELECT data FROM ws_candles WHERE symbol=? AND time=?',(symbol,stamp)).fetchone()
            return json.loads(row[0]) if row else {}

    def _flow_complete(self,symbol,minute,flow,now):
        if flow.get('gap'):return False
        if flow.get('complete'):return True
        start=self.state.get(symbol,{}).get('tradeCoverageStart')
        return bool(start is not None and start<=minute and now>=minute+60000 and self.connections and all(self.connections.values()))

    def flush(self,now=None):
        now=int(time.time()*1000) if now is None else now
        with self.lock,self.db:
            for symbol,book in self.books.items():
                samples=self.samples[symbol]
                if now-book['timestamp']<=self.c['maxBookAgeMs'] and (not samples or book['timestamp']-samples[-1]['timestamp']>=self.c['bookSampleMs']):
                    samples.append(dict(book))
                    self.db.execute('INSERT OR IGNORE INTO book_samples VALUES(?,?,?)',(symbol,book['timestamp'],json.dumps(book)))
                while samples and samples[0]['timestamp']<now-self.c['rawBookRetentionMinutes']*60000:samples.popleft()
                if samples and now//60000!=self.last_flush//60000:
                    self.db.execute('INSERT OR REPLACE INTO market_microstructure VALUES(?,?,?)',(symbol,now//60000*60000,json.dumps(self.features(symbol,now))))
            for key,flow in self.flows.items():
                complete=self._flow_complete(key[0],key[1],flow,now)
                if complete!=flow['complete']:flow['complete']=complete;self.dirty_flow.add(key)
            for key in self.dirty_flow:
                flow=self.flows[key];flow.update(totalTradeVolume=flow['buyAggressiveVolume']+flow['sellAggressiveVolume'],totalTradeCount=flow['buyTradeCount']+flow['sellTradeCount'])
                self.db.execute('INSERT OR REPLACE INTO trade_flow VALUES(?,?,?)',(key[0],key[1],json.dumps(flow)))
            for symbol in self.dirty:self.db.execute('INSERT OR REPLACE INTO stream_state VALUES(?,?)',(symbol,json.dumps(self.state[symbol])))
            self.db.execute("INSERT OR REPLACE INTO micro_meta VALUES('counters',?)",(json.dumps(self.counters),))
            self.dirty.clear();self.dirty_flow.clear()
            if now//60000!=self.last_flush//60000:
                self.db.execute('DELETE FROM book_samples WHERE time<?',(now-self.c['rawBookRetentionMinutes']*60000,))
                for table in ('market_microstructure','trade_flow','quality_events'):
                    self.db.execute(f'DELETE FROM {table} WHERE time<?',(now-self.c['aggregateRetentionDays']*86400000,))
                self.db.execute('DELETE FROM ws_candles WHERE time<?',(now-self.c['candleRetentionMinutes']*60000,))
                self.flows={k:v for k,v in self.flows.items() if k[1]>=now-180000}
            self.last_flush=now

    def features(self,symbol,now):
        with self.lock:
            book=self.books.get(symbol);samples=[b for b in self.samples[symbol] if now-900000<=b['timestamp']<=now]
            if book and book['timestamp']>now:book=samples[-1] if samples else None
            spreads=[b['spreadPct'] for b in samples]
            result=dict(book or {},spreadSamples=len(samples),coverageMs=now-samples[0]['timestamp'] if samples else 0,
                        spreadP90=quantile(spreads,.9),spreadP95=quantile(spreads,.95),spreadMin=min(spreads) if spreads else None,
                        spreadMax=max(spreads) if spreads else None,spreadStd=stats.pstdev(spreads) if spreads else None,
                        marketDataLatency=None,bookLatencyAvailable=False)
            for minutes in (1,5,15):result[f'spreadMedian{minutes}m']=quantile([b['spreadPct'] for b in samples if b['timestamp']>=now-minutes*60000],.5)
            for minutes in (1,3):
                values=[b['bookImbalance'] for b in samples if b['timestamp']>=now-minutes*60000]
                result[f'bookImbalance{minutes}m']=stats.mean(values) if values else None
            median=result['spreadMedian5m'];result['spreadRatio']=book['spreadPct']/median if book and median else None
            flow=dict(self.flows.get((symbol,now//60000*60000-60000),{}))
            bv,sv=flow.get('buyAggressiveVolume',0),flow.get('sellAggressiveVolume',0)
            bc,sc=flow.get('buyTradeCount',0),flow.get('sellTradeCount',0)
            flow.update(totalTradeVolume=bv+sv,totalTradeCount=bc+sc,tradeFlowImbalance=(bv-sv)/(bv+sv) if bv+sv else None,tradeCountImbalance=(bc-sc)/(bc+sc) if bc+sc else None)
            result.update(flow=flow,tradeFlowImbalance=flow['tradeFlowImbalance'],tradeCountImbalance=flow['tradeCountImbalance'])
            st=self.state.get(symbol,{})
            minute=now//60000*60000-60000
            flow['complete']=self._flow_complete(symbol,minute,flow,now)
            result['clockDriftMs']=self.clock_drift
            raw_latency=st.get('tradeReceived',0)-st.get('tradeTime',0) if st.get('tradeTime') else None
            result['tradeDataLatencyRaw']=raw_latency
            result['tradeDataLatency']=raw_latency+self.clock_drift if raw_latency is not None and self.clock_drift is not None else None
            reasons=[]
            if not book or not 0<=now-book['timestamp']<=self.c['maxBookAgeMs']:reasons.append('REJECT_STALE_DATA')
            if len(samples)<self.c['minSpreadSamples'] or result['coverageMs']<self.c['minHistoryMs']:reasons.append('REJECT_MICROSTRUCTURE_WARMUP')
            if now-st.get('tradeReceived',0)>self.c['maxTradeAgeMs']:reasons.append('REJECT_TRADE_FLOW_STALE')
            if not flow['complete']:reasons.append('REJECT_TRADE_FLOW_INCOMPLETE')
            if now-st.get('gapAt',-10**15)<60000:reasons.append('REJECT_TRADE_STREAM_GAP')
            if self.clock_drift is None or abs(self.clock_drift)>self.c['maxClockDriftMs']:reasons.append('REJECT_CLOCK_DRIFT')
            if not self.connections or not all(self.connections.values()):reasons.append('REJECT_FEED_DISCONNECTED')
            if book and book['spreadPct']>self.c['maxSpreadPct']:reasons.append('REJECT_SPREAD')
            if result['spreadRatio'] and result['spreadRatio']>self.c['spreadShockThreshold']:reasons.append('REJECT_SPREAD_SHOCK')
            if self.c['experimentalImbalanceFilter'] and book and book['bookImbalance']<self.c['minBookImbalance']:reasons.append('REJECT_MICROSTRUCTURE_IMBALANCE')
            if book:
                components=dict(spreadQuality=max(0,1-book['spreadPct']/self.c['maxSpreadPct']),
                    spreadStability=1/(1+(result['spreadStd'] or 0)/max(median or .001,.000001)),
                    bookImbalance=(book['bookImbalance']+1)/2,tradeFlow=(flow['tradeFlowImbalance']+1)/2 if flow['tradeFlowImbalance'] is not None else 0,
                    liquidity=min(1,min(book['bid']*book['bidQty'],book['ask']*book['askQty'])/self.c['liquidityReferenceNotional']))
            else:components={k:0 for k in self.c['weights'] if k!='volatility'}
            result.update(components=components,diagnosticRejections=reasons,entryAllowed=not reasons)
            return result

    def quality(self,now):
        with self.lock:return dict(self.counters,clockDriftMs=self.clock_drift,connections=dict(self.connections),
                                  staleBookTicker=sum(now-b['timestamp']>self.c['maxBookAgeMs'] for b in self.books.values()),
                                  trackedBooks=len(self.books),generation=self.generation)
