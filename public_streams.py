"""Public Binance WebSocket collector; closed klines + bounded microstructure universe."""
import json
import logging
import threading
import time

PUBLIC_WS='wss://data-stream.binance.vision/ws'


def subscription_batches(streams):
    batch=[]
    for stream in sorted(streams):
        candidate=batch+[stream]
        if len(candidate)>100 or len(json.dumps(candidate,ensure_ascii=False).encode())>3500:
            if batch:yield batch
            batch=[stream]
        else:batch=candidate
    if batch:yield batch


def connect_public():
    # Lazy import: offline unit tests and the REST fallback don't need networking.
    from websockets.sync.client import connect
    return connect(PUBLIC_WS,open_timeout=15,close_timeout=3,ping_interval=None,max_queue=1024,max_size=1048576)


class PublicStreamWorker:
    def __init__(self,name,streams,store,connector=connect_public):
        self.name,self.streams,self.store,self.connector=name,tuple(streams),store,connector
        self.stop=threading.Event();self.socket=None

    def send_control(self,ws,method,streams,request_id):
        for batch in subscription_batches(streams):
            request_id+=1
            ws.send(json.dumps(dict(method=method,params=batch,id=request_id),ensure_ascii=False))
            # At most two application control frames/s; leave room for pong.
            if self.stop.wait(.5):break
        return request_id

    def session(self):
        with self.connector() as ws:
            self.socket=ws
            # <=1024 streams per connection; one SUBSCRIBE frame, no ping flood.
            subscribed=set(self.streams);request_id=0
            request_id=self.send_control(ws,'SUBSCRIBE',subscribed,request_id)
            self.store.connection(self.name,True)
            while not self.stop.is_set():
                desired=set(self.streams)
                if desired!=subscribed:
                    for method,changes in (('UNSUBSCRIBE',subscribed-desired),('SUBSCRIBE',desired-subscribed)):
                        if changes:
                            request_id=self.send_control(ws,method,changes,request_id)
                    removed={s.split('@')[0].upper() for s in subscribed-desired}
                    self.store.invalidate_symbols(removed)
                    subscribed=desired
                try:raw=ws.recv(timeout=1)
                except TimeoutError:continue
                event=json.loads(raw)
                if 'code' in event:raise ValueError('Public stream subscription rejected')
                if event.get('e')=='serverShutdown':raise ConnectionError('Scheduled stream shutdown')
                self.store.event(event)

    def run(self):
        delay=1;attempt=0
        while not self.stop.is_set():
            started=time.monotonic()
            try:self.session()
            except Exception as exc:logging.warning('Public WebSocket %s reconnect (%s)',self.name,type(exc).__name__)
            finally:self.socket=None;self.store.connection(self.name,False,reconnect=not self.stop.is_set())
            attempt+=1
            if time.monotonic()-started>60:delay=1
            if self.stop.wait(delay):break
            delay=min(60,delay*2)

    def close(self):
        self.stop.set()
        if self.socket:
            try:self.socket.close()
            except Exception:pass


class PublicStreamManager:
    def __init__(self,store,connector=connect_public):
        self.store,self.connector=store,connector;self.workers={}

    def configure(self,candle_symbols,micro_symbols):
        # Deterministic chunks, separate candle and high-frequency connections.
        groups={}
        candle_symbols=sorted(set(candle_symbols));micro_symbols=sorted(set(micro_symbols))
        for index in range(0,len(candle_symbols),800):
            groups['candles-'+str(index//800)]=[s.lower()+'@kline_1m' for s in candle_symbols[index:index+800]]
        for index in range(0,len(micro_symbols),200):
            groups['micro-'+str(index//200)]=[s.lower()+suffix for s in micro_symbols[index:index+200] for suffix in ('@bookTicker','@aggTrade')]
        for name in list(self.workers):
            worker,thread=self.workers[name]
            if name not in groups:
                worker.close();thread.join(timeout=4)
                self.workers.pop(name)
                with self.store.lock:self.store.connections.pop(name,None)
        for name,streams in groups.items():
            if name in self.workers:self.workers[name][0].streams=tuple(streams)
            else:
                worker=PublicStreamWorker(name,streams,self.store,self.connector)
                thread=threading.Thread(target=worker.run,daemon=True,name='binance-'+name)
                self.workers[name]=(worker,thread);thread.start()

    def close(self):
        for worker,_ in self.workers.values():worker.close()
        for _,thread in self.workers.values():thread.join(timeout=4)


class MicrostructureService:
    def __init__(self,store,shadow,scanner,adaptive):
        self.store,self.shadow,self.scanner,self.adaptive=store,shadow,scanner,adaptive
        self.manager=PublicStreamManager(store)
        self.status={'state':'starting','error':None,'tracked':[],'unobservedSymbols':0}

    def run(self,stop):
        refresh=0
        try:
            while not stop.is_set():
                started=time.monotonic();now=int(time.time()*1000)
                try:
                    if started-refresh>=60:
                        scan=self.scanner.snapshot()
                        with self.adaptive.lock:ranked=[r['symbol'] for r in self.adaptive.ranking]
                        with self.adaptive.portfolio.lock:held=list(self.adaptive.portfolio.state['positions'])
                        with self.adaptive.directional.lock:
                            directional_held=list(self.adaptive.directional.state['positions'])+list(self.adaptive.directional.state['orders'])
                        # Keep active shadows covered; no removal merely because rank changed.
                        wanted=list(dict.fromkeys(held+directional_held+self.shadow.tracked_symbols()+self.adaptive.directional.tracked_symbols()+['BTCUSDT']+ranked+
                                                  [r['symbol'] for r in scan['rows'] if r['quote']=='USDT']))
                        selected=wanted[:self.store.c['maxMicroSymbols']]
                        self.manager.configure([m['symbol'] for m in scan['markets'] if m['quote']=='USDT'],selected)
                        self.status.update(tracked=selected,unobservedSymbols=max(0,len(wanted)-len(selected)))
                        refresh=started
                    self.store.flush(now)
                    self.shadow.poll_paper(now)
                    self.shadow.advance(now)
                    self.adaptive.directional.observe(self.store,now)
                    self.status.update(state='collecting',error=None)
                except Exception as exc:
                    logging.warning('Microstructure diagnostic paused (%s)',type(exc).__name__)
                    self.status.update(state='error',error='Diagnostic collection unavailable; paper balances unaffected')
                stop.wait(max(.05,1-(time.monotonic()-started)))
        finally:
            self.manager.close();self.store.flush()

    def snapshot(self):
        now=int(time.time()*1000)
        symbols=list(self.status['tracked'])
        return dict(status=dict(self.status),quality=self.store.quality(now),
                    markets=[dict(self.store.features(s,now),symbol=s) for s in symbols],
                    diagnostics=self.shadow.report(now),diagnosticOnly=True,liveReady=False)
