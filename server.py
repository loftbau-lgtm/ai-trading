import csv, io, json, logging, os, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen, Request
from engine import Engine, SYMBOLS

ROOT=Path(__file__).parent
lock=threading.RLock()
engine=Engine(os.environ.get('DATABASE_PATH', str(ROOT/'data/quantlab.sqlite3')))
status={'state':'starting','error':None,'lastSync':None}
stop=threading.Event()
# Fixed allowlist; no authenticated or order endpoints exist in this application.
BASE='https://data-api.binance.vision/api/v3/'
def public_get(endpoint, params=None):
    if endpoint not in ('time','klines'): raise ValueError('Public market data only')
    request=Request(BASE+endpoint+'?'+urlencode(params or {}),headers={'User-Agent':'QuantLabAI/1.0'})
    with urlopen(request,timeout=20) as response: return json.load(response)
def candles(symbol,start,end):
    rows=public_get('klines',{'symbol':symbol,'interval':'1m','startTime':start,'endTime':end,'limit':1000})
    return [{'time':int(r[0]),'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'end':int(r[6])} for r in rows if int(r[6])<=end]
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
def snapshot():
    with lock:
        return {'paperOnly':True,'status':dict(status),'cursor':engine.cursor(),'accounts':engine.accounts(),
            'markets':{s:engine.history(s) for s in SYMBOLS},
            'trades':[dict(r) for r in engine.db.execute('SELECT * FROM trades ORDER BY time DESC,id DESC LIMIT 200')],
            'history':[dict(r) for r in engine.db.execute('SELECT * FROM equity WHERE time IN (SELECT DISTINCT time FROM equity ORDER BY time DESC LIMIT 240) ORDER BY time')],
            'config':{'capital':100,'feePct':.1,'slippagePct':.02,'buyLimitPct':25}}
class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/'public'),**kwargs)
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        super().end_headers()
    def do_GET(self):
        path=urlparse(self.path).path
        if path in ('/api/state','/healthz'):
            data=snapshot() if path=='/api/state' else {'ok':True,'paperOnly':True,'marketData':status['state']}
            body=json.dumps(data,allow_nan=False).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path=='/api/trades.csv':
            with lock: rows=[dict(r) for r in engine.db.execute('SELECT * FROM trades ORDER BY id')]
            out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=['id','strategy','symbol','time','side','qty','price','entry','fee','pnl']); writer.writeheader(); writer.writerows(rows)
            self.send_response(200); self.send_header('Content-Type','text/csv'); self.send_header('Content-Disposition','attachment; filename="quantlab-trades.csv"'); self.end_headers(); self.wfile.write(out.getvalue().encode())
        elif path in ('/','/index.html','/app.js','/style.css','/manifest.json','/icon.svg'): super().do_GET()
        else: self.send_error(404)
if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=worker,daemon=True).start()
    server=ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8000'))),Handler)
    try: server.serve_forever()
    finally: stop.set(); server.server_close()
