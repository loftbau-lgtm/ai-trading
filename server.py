import csv, io, json, logging, os, threading, time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from engine import Engine, SYMBOLS
from terminal import BinanceReadOnly, observations, WATCH_SYMBOLS, validate_history
from scanner import Scanner
from adaptive_runner import AdaptiveRunner

ROOT=Path(__file__).parent
lock=threading.RLock()
engine=Engine(os.environ.get('DATABASE_PATH', str(ROOT/'data/quantlab.sqlite3')))
status={'state':'starting','error':None,'lastSync':None}
stop=threading.Event()
exchange=BinanceReadOnly()
watch={s:{'bars':[],'state':'starting'} for s in WATCH_SYMBOLS if s not in SYMBOLS}
# Fixed allowlist for public market data. Private reads are isolated in terminal.py.
BASE='https://data-api.binance.vision/api/v3/'
feed_lock=threading.Lock()
feed_resume_at=0
feed_weights=deque()
def public_get(endpoint, params=None):
    global feed_resume_at
    if endpoint not in ('time','klines','exchangeInfo','ticker/24hr'): raise ValueError('Public market data only')
    with feed_lock:
        if time.monotonic()<feed_resume_at: raise RuntimeError('Public API cooldown')
        instant=time.monotonic()
        while feed_weights and feed_weights[0][0]<=instant-60: feed_weights.popleft()
        weight={'time':1,'klines':2,'exchangeInfo':20,'ticker/24hr':80}[endpoint]
        if sum(w for _,w in feed_weights)+weight>2400:
            raise RuntimeError('Public API local weight budget')
        feed_weights.append((instant,weight))
    request=Request(BASE+endpoint+'?'+urlencode(params or {}),headers={'User-Agent':'QuantLabAI/1.0'})
    try:
        with urlopen(request,timeout=20) as response: return json.load(response)
    except HTTPError as exc:
        if exc.code in (418,429):
            try: delay=max(120,int(exc.headers.get('Retry-After','120')))
            except ValueError: delay=120
            with feed_lock: feed_resume_at=max(feed_resume_at,time.monotonic()+delay)
        raise
def candles(symbol,start,end):
    rows=public_get('klines',{'symbol':symbol,'interval':'1m','startTime':start,'endTime':end,'limit':1000})
    return [{'time':int(r[0]),'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'end':int(r[6])} for r in rows if int(r[6])<=end]
scanner=Scanner(public_get,candles)
adaptive=AdaptiveRunner(public_get,scanner,Path(os.environ.get('DATABASE_PATH',str(ROOT/'data/quantlab.sqlite3'))).parent)
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
    with lock: status.update(state='live',error=None,lastSync=int(time.time()*1000))
def worker():
    delay=5
    while not stop.is_set():
        try:
            with lock: status['state']='syncing'
            sync()
            delay=5
        except Exception as exc:
            logging.exception('Market sync failed')
            with lock: status.update(state='error',error=str(exc))
            delay=min(delay*2,300)
        stop.wait(delay)
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
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/adaptive':
            self.send_json(adaptive.snapshot())
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
        elif path in ('/','/index.html','/app.js','/terminal.js','/terminal.css','/scanner.js','/adaptive.js','/style.css','/manifest.json','/icon.svg'): super().do_GET()
        else: self.send_error(404)
if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=worker,daemon=True).start()
    threading.Thread(target=watch_worker,daemon=True).start()
    threading.Thread(target=scanner.run,args=(stop,),daemon=True).start()
    threading.Thread(target=adaptive.run,args=(stop,),daemon=True).start()
    server=ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8000'))),Handler)
    try: server.serve_forever()
    finally: stop.set(); server.server_close()
