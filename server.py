import csv, hmac, io, json, logging, os, queue, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from engine import Engine, SYMBOLS
from terminal import BinanceReadOnly, observations, WATCH_SYMBOLS, validate_history
from scanner import Scanner
from adaptive_runner import AdaptiveRunner
from microstructure import MicrostructureStore
from shadow_execution import ShadowExecution
from public_streams import MicrostructureService
from market_data import DEFAULT_HOSTS, MarketDataUnavailable, PublicMarketDataClient
from edge_lab import snapshot as edge_lab_snapshot
from futures_public import FuturesPublicClient
from futures_paper import FuturesPaperAccount
from futures_edge_engine import FuturesEdgeEngine

ROOT=Path(__file__).parent
lock=threading.RLock()
engine=Engine(os.environ.get('DATABASE_PATH', str(ROOT/'data/quantlab.sqlite3')))
status={'state':'starting','error':None,'lastSync':None}
stop=threading.Event()
exchange=BinanceReadOnly()
watch={s:{'bars':[],'state':'starting'} for s in WATCH_SYMBOLS if s not in SYMBOLS}
# Shared public market data client. Private reads remain isolated in terminal.py.
market_client=PublicMarketDataClient(tuple(filter(None,
    (host.strip() for host in os.environ.get('BINANCE_PUBLIC_HOSTS',','.join(DEFAULT_HOSTS)).split(',')))))
def public_get(endpoint, params=None):
    return market_client.get(endpoint,params)
def candles(symbol,start,end):
    rows=public_get('klines',{'symbol':symbol,'interval':'1m','startTime':start,'endTime':end,'limit':1000})
    return [{'time':int(r[0]),'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'end':int(r[6])} for r in rows if int(r[6])<=end]
scanner=Scanner(public_get,candles)
adaptive=AdaptiveRunner(public_get,scanner,Path(os.environ.get('DATABASE_PATH',str(ROOT/'data/quantlab.sqlite3'))).parent)
futures=FuturesPaperAccount(adaptive.directory/'binance_futures_paper.sqlite3')
futures_public=FuturesPublicClient()
futures_edge=FuturesEdgeEngine(adaptive.directory/'edge_lab.sqlite3',
    taker_fee=futures.config['takerFee'],slippage=futures.config['slippage'])
futures_research_queue=queue.Queue(maxsize=2)
runner_status={name:'STARTING' for name in ('marketData','watch','scanner',
    'adaptive','microstructure','futures','edgeLab')}
# No candidate has passed an independent OOS promotion gate. Existing positions
# keep their normal exits, but no account receives new PAPER entry capital.
engine.entry_allowed=lambda strategy: False
adaptive.entry_enabled=False
adaptive.directional.entry_enabled=False
micro_store=MicrostructureStore(adaptive.directory/'microstructure.sqlite3')
adaptive.candle_cache=micro_store
adaptive.directional.attach_stream(micro_store)
shadow=ShadowExecution(micro_store,adaptive.directory/'adaptive.sqlite3',adaptive.config)
adaptive.portfolio.telemetry=shadow.enqueue_telemetry
microstructure=MicrostructureService(micro_store,shadow,scanner,adaptive)
def sync():
    now=int(public_get('time')['serverTime'])
    last=now//60000*60000-60000
    with lock: cursor=engine.cursor()
    if cursor is None:
        history={s:candles(s,last-119*60000,last+59999) for s in SYMBOLS}
        with lock: engine.bootstrap(history)
        cursor=last
    while cursor<last and not stop.is_set():
        end=min(last,cursor+1000*60000)
        batch={s:{b['time']:b for b in candles(s,cursor+60000,end+59999)} for s in SYMBOLS}
        for t in range(cursor+60000,end+1,60000):
            if any(t not in batch[s] for s in SYMBOLS): raise ValueError('Missing Binance candle; retrying without skipping')
            with lock: engine.process({s:batch[s][t] for s in SYMBOLS},now)
        cursor=end
    # The autonomous PAPER operator also receives the always-on core feed.
    # A full-universe cycle may also run, but the persisted cursor prevents
    # duplicate actions if both feeds reach the same candle.
    try:
        with lock: agent_histories={symbol:engine.history(symbol) for symbol in SYMBOLS}
        scan=scanner.snapshot()
        adaptive.agent.cycle(agent_histories,scan.get('rows',[]),now,
            fresh=bool(scan.get('fresh')) and cursor==last)
    except Exception:
        logging.exception('Autonomous PAPER agent core cycle paused')
    with lock: status.update(state='live',error=None,lastSync=int(time.time()*1000))
def worker():
    retry_delay=5
    while not stop.is_set():
        try:
            with lock: status['state']='syncing'
            sync()
            wait_seconds=5
            retry_delay=5
        except Exception as exc:
            logging.exception('Market sync failed')
            with lock: status.update(state='MARKET_DATA_UNAVAILABLE' if isinstance(exc,MarketDataUnavailable) else 'error',error=str(exc))
            wait_seconds=retry_delay
            retry_delay=min(retry_delay*2,60)
        stop.wait(wait_seconds)
def watch_worker():
    while not stop.is_set():
        try:
            now=int(public_get('time')['serverTime'])
            last=now//60000*60000-60000
            for symbol in watch:
                if stop.is_set(): return
                try:
                    with lock: bars=watch[symbol]['bars']
                    if not bars or bars[-1]['time'] != last:
                        bars=candles(symbol,last-119*60000,last+59999)
                        validate_history(bars,last)
                    with lock: watch[symbol]={'bars':bars,'state':'live'}
                except Exception:
                    with lock: watch[symbol]['state']='error'
                    logging.warning('Additional market unavailable: %s',symbol)
        except Exception:
            with lock:
                for item in watch.values(): item['state']='error'
            logging.warning('Additional market sync unavailable')
        stop.wait(60)
def futures_worker():
    runner_status['futures']='RUNNING'
    while not stop.is_set():
        wait_seconds=60
        try:
            feed=futures_public.snapshot(tuple(futures.config['symbols']),futures.funding_since())
            try:
                futures.cycle(feed)
            except Exception as exc:
                logging.exception('Futures PAPER execution cycle failed')
                with futures.lock:
                    futures.status='PAPER_EXECUTION_ERROR'
                    futures.error=type(exc).__name__
            try:
                futures_research_queue.put_nowait(feed)
            except queue.Full:
                # The next 120-bar snapshot can fill a transient gap. Never
                # block PAPER execution on research/database availability.
                runner_status['edgeLab']='LAGGING'
        except Exception as exc:
            logging.warning('Public Futures PAPER feed paused: %s: %s',type(exc).__name__,str(exc))
            with futures.lock:
                futures.status='MARKET_DATA_UNAVAILABLE'
                futures.error=type(exc).__name__
            if isinstance(exc,ValueError):wait_seconds=15
        stop.wait(wait_seconds)
    runner_status['futures']='STOPPED'
def futures_edge_worker():
    runner_status['edgeLab']='RUNNING'
    while not stop.is_set():
        try:
            feed=futures_research_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            futures_edge.cycle(feed)
            active={row['modelId']:row for row in futures_edge.candidates()
                    if row['paperPromoted']}
            with futures.lock:
                futures.active_models=active
                futures.promoted=frozenset(row['family'] for row in active.values())
                futures.edge_evidence={row['family']+'|'+row['direction']:
                    {'probabilityNetProfit':row['probabilityNetProfit']}
                    for row in active.values()}
            runner_status['edgeLab']='RUNNING'
        except Exception:
            runner_status['edgeLab']='ERROR'
            logging.exception('Futures Edge Lab cycle failed')
        finally:
            futures_research_queue.task_done()
    runner_status['edgeLab']='STOPPED'
def futures_watchdog():
    workers={'marketData':worker,'watch':watch_worker,
        'scanner':lambda:scanner.run(stop),'adaptive':lambda:adaptive.run(stop),
        'microstructure':lambda:microstructure.run(stop),
        'futures':futures_worker,'edgeLab':futures_edge_worker}
    threads={}
    delays={name:1 for name in workers}
    next_start={name:0 for name in workers}
    started={name:0 for name in workers}
    while not stop.is_set():
        now=time.monotonic()
        for name,target in workers.items():
            thread=threads.get(name)
            if thread is not None and thread.is_alive():
                if now-started[name]>=60:delays[name]=1
                continue
            if thread is not None:
                if next_start[name]==float('inf'):
                    runner_status[name]='RUNNER_DEAD'
                    next_start[name]=now+delays[name]
                    delays[name]=min(delays[name]*2,60)
            if now>=next_start[name]:
                thread=threading.Thread(target=target,daemon=True,name='quantlab-'+name)
                threads[name]=thread
                started[name]=now
                runner_status[name]='RUNNING'
                thread.start()
                next_start[name]=float('inf')
        stop.wait(2)
    for thread in threads.values():
        thread.join(timeout=15)
def snapshot():
    with lock:
        markets={s:engine.history(s) for s in SYMBOLS}
        terminal_markets={**markets,**{s:v['bars'] for s,v in watch.items()}}
        terminal_signals={s:observations({s:bars},bars[-1]['time'] if bars else None,
                          status['state'] if s in SYMBOLS else watch[s]['state'])
                          for s,bars in terminal_markets.items()}
        return {'paperOnly':True,'status':dict(status),'cursor':engine.cursor(),'accounts':engine.accounts(),
            'markets':markets,
            'signals':observations(markets,engine.cursor(),status['state']),
            'terminal':{'markets':terminal_markets,'signals':terminal_signals},
            'exchange':{'state':'not_connected','readOnly':True},
            'trades':[dict(r) for r in engine.db.execute('SELECT * FROM trades ORDER BY time DESC,id DESC LIMIT 200')],
            'history':[dict(r) for r in engine.db.execute('SELECT * FROM equity WHERE time IN (SELECT DISTINCT time FROM equity ORDER BY time DESC LIMIT 240) ORDER BY time')],
            'config':{'capital':100,'feePct':.1,'slippagePct':.02,'buyLimitPct':25}}
class Handler(SimpleHTTPRequestHandler):
    def log_request(self, code='-', size='-'):
        # Omit query strings from access logs.
        self.log_message('%s %s %s', self.command, urlparse(self.path).path, str(code))
    def send_json(self, data, code=200):
        body=json.dumps(data,allow_nan=False).encode()
        self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/'public'),**kwargs)
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        super().end_headers()
    def do_POST(self):
        path=urlparse(self.path).path
        if path!='/api/paper/control':
            self.send_error(404)
            return
        token=os.environ.get('PAPER_CONTROL_TOKEN','')
        if not token:
            self.send_json({'error':'Paper control token not configured'},503)
            return
        supplied=self.headers.get('Authorization','')
        if not hmac.compare_digest(supplied,'Bearer '+token):
            self.send_json({'error':'Unauthorized'},401)
            return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024:
                raise ValueError('Invalid request size')
            payload=json.loads(self.rfile.read(size))
            action=payload.get('action') if isinstance(payload,dict) else None
            if action not in ('PAUSE_ALL','RESUME'):
                raise ValueError('Expected PAUSE_ALL or RESUME')
        except (ValueError,TypeError):
            self.send_json({'error':'Invalid paper control request'},400)
            return
        changed=futures.set_paused(action=='PAUSE_ALL',int(time.time()*1000))
        self.send_json({'mode':'PAPER_ONLY','pausedAll':futures.snapshot()['pausedAll'],
                        'changed':changed})
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/microstructure':
            self.send_json(microstructure.snapshot())
        elif path=='/api/market-data/health':
            self.send_json(market_client.snapshot())
        elif path=='/api/shadow/report':
            self.send_json(shadow.report())
        elif path=='/api/adaptive':
            self.send_json(adaptive.snapshot())
        elif path=='/api/edge-lab':
            self.send_json(edge_lab_snapshot(engine,adaptive,lock))
        elif path=='/api/edge-lab/status':
            report=futures_edge.snapshot()
            self.send_json({key:value for key,value in report.items() if key!='candidates'} |
                           {'runner':runner_status['edgeLab'],'workers':dict(runner_status)})
        elif path=='/api/edge-lab/candidates':
            self.send_json({'mode':'PAPER_ONLY','candidates':futures_edge.candidates()})
        elif path=='/api/edge-lab/rankings':
            rows=futures_edge.candidates()
            rows.sort(key=lambda row:(row['paperPromoted'],
                row['ciLow'] if row['ciLow'] is not None else -1e9),reverse=True)
            self.send_json({'mode':'PAPER_ONLY','rankings':rows})
        elif path=='/api/autonomous-agent':
            report=futures.snapshot()
            self.send_json({'mode':'PAPER_ONLY','state':report['status'],
                'lastDecision':report['lastDecision'],
                'promotedFamilies':report['promotedFamilies'],
                'outcomeMetrics':report['outcomeMetrics'],
                'recentOutcomes':report['outcomes'],
                'runner':runner_status['futures']})
        elif path=='/api/portfolio-risk':
            report=futures.snapshot()
            self.send_json({key:report[key] for key in ('mode','paperOnly','equity',
                'grossExposure','netExposure','longExposure','shortExposure',
                'effectiveLeverage','noConfirmedEdge')})
        elif path=='/api/portfolio-agent':
            self.send_json(adaptive.agent.snapshot())
        elif path=='/api/futures-paper':
            self.send_json(futures.snapshot())
        elif path=='/api/adaptive-matrix':
            self.send_json(adaptive.matrix.snapshot())
        elif path=='/api/adaptive-matrix/variants':
            self.send_json({'variants': adaptive.matrix.snapshot()['variants']})
        elif path.startswith('/api/adaptive-matrix/variant/'):
            variant_id=path.rsplit('/',1)[-1]
            data=adaptive.matrix.variant_snapshot(variant_id)
            self.send_json(data if data else {'error':'not found'},200 if data else 404)
        elif path=='/api/adaptive-matrix/frontier':
            self.send_json(adaptive.matrix.frontier_snapshot())
        elif path=='/api/adaptive-matrix/stress':
            self.send_json(adaptive.matrix.stress_snapshot())
        elif path=='/api/directional':
            self.send_json(adaptive.directional.snapshot())
        elif path=='/api/directional/edge-health':
            self.send_json(adaptive.directional.edge_health())
        elif path=='/api/scanner':
            self.send_json(scanner.snapshot())
        elif path=='/api/market':
            symbol=parse_qs(urlparse(self.path).query).get('symbol',[''])[0]
            data,code=scanner.market(symbol)
            self.send_json(data,code)
        elif path=='/api/exchange/account':
            if not exchange.authorized(self.headers.get('Authorization','')):
                self.send_json({'state':'locked','message':'Wymagany token dostępu do panelu konta.'},401)
                return
            data=exchange.snapshot()
            self.send_json(data,502 if data['state']=='error' else 200)
        elif path in ('/api/state','/healthz'):
            data=snapshot() if path=='/api/state' else {'ok':True,'paperOnly':True,'marketData':status['state']}
            body=json.dumps(data,allow_nan=False).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path=='/api/trades.csv':
            with lock: rows=[dict(r) for r in engine.db.execute('SELECT * FROM trades ORDER BY id')]
            out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=['id','strategy','symbol','time','side','qty','price','entry','fee','pnl']); writer.writeheader(); writer.writerows(rows)
            self.send_response(200); self.send_header('Content-Type','text/csv'); self.send_header('Content-Disposition','attachment; filename="quantlab-trades.csv"'); self.end_headers(); self.wfile.write(out.getvalue().encode())
        elif path in ('/','/index.html','/app.js','/market_data.js','/terminal.js','/terminal.css','/scanner.js','/adaptive.js','/adaptive_matrix.js','/edge_lab.js','/portfolio_agent.js','/futures_paper.js','/microstructure.js','/directional.js','/style.css','/manifest.json','/icon.svg'): super().do_GET()
        else: self.send_error(404)
if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=market_client.probe,daemon=True).start()
    futures_thread=threading.Thread(target=futures_watchdog,daemon=True)
    futures_thread.start()
    server=ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8000'))),Handler)
    try: server.serve_forever()
    finally:
        stop.set()
        futures_thread.join(timeout=15)
        adaptive.close()
        if not futures_thread.is_alive():futures.close()
        if not futures_thread.is_alive():futures_edge.close()
        server.server_close()
