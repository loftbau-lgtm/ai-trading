"""Deterministic, public-data-only paper trading engine."""
import json, math, sqlite3, statistics
from pathlib import Path

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
STRATEGIES = ('SMA 9/21', 'EMA 9/21', 'RSI mean reversion', 'Bollinger Bands', '20-bar breakout', 'Momentum')
FEE, SLIPPAGE = .001, .0002

def ema(values, period):
    result = values[0]
    for value in values[1:]:
        result += 2 / (period + 1) * (value - result)
    return result

def signal(name, bars):
    if len(bars) < 100:
        return 'HOLD'
    c = [b['close'] for b in bars]
    if name.startswith(('SMA', 'EMA')):
        avg = (lambda v, n: statistics.mean(v[-n:])) if name.startswith('SMA') else ema
        before = avg(c[:-1], 9) - avg(c[:-1], 21)
        now = avg(c, 9) - avg(c, 21)
        return 'BUY' if before <= 0 < now else 'SELL' if before >= 0 > now else 'HOLD'
    if name.startswith('RSI'):
        changes = [b-a for a,b in zip(c[-15:-1],c[-14:])]
        gain = sum(max(x,0) for x in changes)/14
        loss = sum(max(-x,0) for x in changes)/14
        rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100-100/(1+gain/loss)
        return 'BUY' if rsi < 30 else 'SELL' if rsi > 55 else 'HOLD'
    if name == 'Bollinger Bands':
        mean, sd = statistics.mean(c[-20:]), statistics.pstdev(c[-20:])
        return 'BUY' if c[-1] < mean-2*sd else 'SELL' if c[-1] >= mean else 'HOLD'
    if name == '20-bar breakout':
        return 'BUY' if c[-1] > max(b['high'] for b in bars[-21:-1]) else 'SELL' if c[-1] < min(b['low'] for b in bars[-11:-1]) else 'HOLD'
    momentum = c[-1]/c[-11]-1
    return 'BUY' if momentum > .003 else 'SELL' if momentum < 0 else 'HOLD'

class Engine:
    def __init__(self, path):
        self.entry_allowed = lambda strategy: True
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS accounts(name TEXT PRIMARY KEY,cash REAL NOT NULL,realized REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS positions(strategy TEXT,symbol TEXT,qty REAL,entry REAL,cost REAL,PRIMARY KEY(strategy,symbol));
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY,strategy TEXT,symbol TEXT,time INTEGER,side TEXT,qty REAL,price REAL,entry REAL,fee REAL,pnl REAL,UNIQUE(strategy,symbol,time));
        CREATE TABLE IF NOT EXISTS candles(symbol TEXT,time INTEGER,data TEXT,PRIMARY KEY(symbol,time));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS equity(time INTEGER,strategy TEXT,value REAL,PRIMARY KEY(time,strategy));
        ''')
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO accounts(name,cash) VALUES(?,100)', [(s,) for s in STRATEGIES])
    def cursor(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
        return int(row[0]) if row else None
    def history(self, symbol):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM (SELECT time,data FROM candles WHERE symbol=? ORDER BY time DESC LIMIT 120) ORDER BY time',(symbol,))]
    def bootstrap(self, histories):
        times = [[b['time'] for b in histories[s]] for s in SYMBOLS]
        if any(len(t)<100 or t != times[0] for t in times):
            raise ValueError('Incomplete warm-up across markets')
        if any(b-a != 60000 for a,b in zip(times[0],times[0][1:])):
            raise ValueError('Gap in warm-up candles')
        with self.db:
            if self.cursor() is not None: return
            for s in SYMBOLS:
                self.db.executemany('INSERT INTO candles VALUES(?,?,?)', [(s,b['time'],json.dumps(b)) for b in histories[s]])
            self.db.execute("INSERT INTO meta VALUES('cursor',?)",(str(times[0][-1]),))
    def execute(self, strategy, symbol, side, price, timestamp):
        if side == 'BUY' and not self.entry_allowed(strategy):
            return
        a = self.db.execute('SELECT * FROM accounts WHERE name=?',(strategy,)).fetchone()
        p = self.db.execute('SELECT * FROM positions WHERE strategy=? AND symbol=?',(strategy,symbol)).fetchone()
        if side == 'BUY' and not p and a['cash'] > .01:
            fill = price*(1+SLIPPAGE)
            budget = a['cash']*.25
            qty = budget/(fill*(1+FEE))
            fee = qty*fill*FEE
            self.db.execute('UPDATE accounts SET cash=cash-? WHERE name=?',(budget,strategy))
            self.db.execute('INSERT INTO positions VALUES(?,?,?,?,?)',(strategy,symbol,qty,fill,budget))
            pnl, entry = 0, fill
        elif side == 'SELL' and p:
            qty, entry = p['qty'], p['entry']
            fill = price*(1-SLIPPAGE)
            fee = qty*fill*FEE
            proceeds = qty*fill-fee
            pnl = proceeds-p['cost']
            self.db.execute('UPDATE accounts SET cash=cash+?,realized=realized+? WHERE name=?',(proceeds,pnl,strategy))
            self.db.execute('DELETE FROM positions WHERE strategy=? AND symbol=?',(strategy,symbol))
        else: return
        self.db.execute('INSERT INTO trades(strategy,symbol,time,side,qty,price,entry,fee,pnl) VALUES(?,?,?,?,?,?,?,?,?)',(strategy,symbol,timestamp,side,qty,fill,entry,fee,pnl))
    def process(self, bars, now):
        if set(bars) != set(SYMBOLS): raise ValueError('All markets required')
        t = bars[SYMBOLS[0]]['time']
        for b in bars.values():
            if b['time'] != t or b['end'] >= now or b['end'] != t+59999:
                raise ValueError('Only aligned closed 1m candles allowed')
            if any(not math.isfinite(b[k]) or b[k] <= 0 for k in ('open','high','low','close')):
                raise ValueError('Invalid price')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.cursor()
            if cursor is None: raise ValueError('Warm-up required')
            if t <= cursor:
                self.db.rollback()
                return False
            if t != cursor+60000: raise ValueError('Candle gap: refusing to skip')
            for symbol in SYMBOLS:
                history = self.history(symbol)
                for strategy in STRATEGIES:
                    # Previous closed candle decides; next candle open fills.
                    self.execute(strategy,symbol,signal(strategy,history),bars[symbol]['open'],t)
                self.db.execute('INSERT INTO candles VALUES(?,?,?)',(symbol,t,json.dumps(bars[symbol])))
                self.db.execute('DELETE FROM candles WHERE symbol=? AND time<?',(symbol,t-119*60000))
            self.db.execute("UPDATE meta SET value=? WHERE key='cursor'",(str(t),))
            for a in self.accounts():
                self.db.execute('INSERT INTO equity VALUES(?,?,?)',(t,a['name'],a['equity']))
            self.db.execute('DELETE FROM equity WHERE time<?',(t-10080*60000,))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise
    def accounts(self):
        prices = {s:(self.history(s)[-1]['close'] if self.history(s) else 0) for s in SYMBOLS}
        result=[]
        for r in self.db.execute('SELECT * FROM accounts ORDER BY rowid'):
            a=dict(r)
            positions=[dict(p) for p in self.db.execute('SELECT * FROM positions WHERE strategy=?',(a['name'],))]
            for p in positions:
                p['mark']=prices[p['symbol']]
                p['unrealized']=p['qty']*p['mark']-p['cost']
            a['positions']=positions
            a['unrealized']=sum(p['unrealized'] for p in positions)
            a['equity']=a['cash']+sum(p['qty']*p['mark'] for p in positions)
            a['pnl']=a['equity']-100
            a['returnPct']=a['pnl']
            a['trades']=self.db.execute('SELECT COUNT(*) FROM trades WHERE strategy=?',(a['name'],)).fetchone()[0]
            a['fees']=self.db.execute('SELECT COALESCE(SUM(fee),0) FROM trades WHERE strategy=?',(a['name'],)).fetchone()[0]
            result.append(a)
        return result
