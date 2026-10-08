"""Isolated live order adapter. NOT wired to the public application.

No automatic activation: caller must provide three independent opt-ins and a
paper report. Unknown outcomes stay locked until reconciled; never blindly retry.
Portfolio fill/fee-asset reconciliation and exchange-side protective orders still
require integration testing before this adapter can be used by the strategy.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from adaptive import config_hash


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class LiveLocked(RuntimeError): pass


class BinanceLiveAdapter:
    def __init__(self, path, config, env, confirmation, paper_report, catalog, transport=None):
        if (env.get('TRADING_MODE') != 'LIVE' or env.get('ENABLE_LIVE_ORDERS') != 'true'
            or confirmation != 'CONFIRM LIVE '+config_hash(config)):
            raise LiveLocked('Three explicit LIVE confirmations required')
        if not (paper_report.get('paperGatePassed') and paper_report.get('trades',0) >= config['MIN_PAPER_TRADES']
                and (paper_report.get('expectancyNet') or 0) > 0):
            raise LiveLocked('Minimum paper evidence not satisfied')
        if not env.get('BINANCE_API_KEY') or not env.get('BINANCE_API_SECRET'):
            raise LiveLocked('Server-side credentials required')
        self.key,self.secret=env['BINANCE_API_KEY'],env['BINANCE_API_SECRET']
        self.config,self.catalog=config,catalog
        self.transport=transport or self._http
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,symbol TEXT,state TEXT,created INTEGER,data TEXT);')

    def _http(self,method,path,params):
        query=urlencode({**params,'timestamp':int(time.time()*1000),'recvWindow':5000})
        signature=hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        payload=query+'&signature='+signature
        url='https://api.binance.com/api/v3/'+path
        data=payload.encode() if method=='POST' else None
        if data is None: url+='?'+payload
        request=Request(url,data=data,method=method,headers={'X-MBX-APIKEY':self.key,'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with build_opener(NoRedirect()).open(request,timeout=10) as response:return json.load(response)
        except Exception:
            # Do not leak signed URLs, response bodies or credentials.
            raise LiveLocked('Exchange outcome unknown; reconcile by client ID') from None

    def _normalize(self,symbol,side,qty,price):
        market=self.catalog.get(symbol)
        if not market or market.get('status')!='TRADING' or market.get('isSpotTradingAllowed') is not True or market.get('quoteAsset')!=self.config['QUOTE']:
            raise LiveLocked('Market not allowed')
        filters={f['filterType']:f for f in market['filters']}
        pf,lot=filters['PRICE_FILTER'],filters['LOT_SIZE']
        p,q=Decimal(str(price)),Decimal(str(qty))
        tick,step=Decimal(pf['tickSize']),Decimal(lot['stepSize'])
        if not p.is_finite() or not q.is_finite() or min(p,q,tick,step)<=0:raise LiveLocked('Invalid order')
        p=(p/tick).to_integral_value(rounding=ROUND_DOWN if side=='BUY' else ROUND_UP)*tick
        q=(q/step).to_integral_value(rounding=ROUND_DOWN)*step
        if not Decimal(lot['minQty'])<=q<=Decimal(lot['maxQty']):raise LiveLocked('LOT_SIZE')
        if Decimal(pf['minPrice'])>0 and p<Decimal(pf['minPrice']):raise LiveLocked('PRICE_FILTER')
        if Decimal(pf['maxPrice'])>0 and p>Decimal(pf['maxPrice']):raise LiveLocked('PRICE_FILTER')
        for name in ('MIN_NOTIONAL','NOTIONAL'):
            f=filters.get(name)
            if f and p*q<Decimal(f['minNotional']):raise LiveLocked(name)
            if f and 'maxNotional' in f and p*q>Decimal(f['maxNotional']):raise LiveLocked(name)
        return format(q,'f'),format(p,'f')

    def submit_maker(self,signal_key,symbol,side,qty,price):
        if side not in ('BUY','SELL'):raise LiveLocked('Spot BUY/SELL only')
        client='qlab_'+hashlib.sha256((signal_key+':'+side).encode()).hexdigest()[:28]
        with self.lock:
            known=self.db.execute('SELECT data FROM orders WHERE id=?',(client,)).fetchone()
            if known:return self.reconcile(client)
            if self.db.execute("SELECT 1 FROM orders WHERE state='UNKNOWN' LIMIT 1").fetchone():
                raise LiveLocked('Unresolved order: new submissions frozen')
            qty,price=self._normalize(symbol,side,qty,price)
            with self.db:self.db.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(client,symbol,'UNKNOWN',int(time.time()*1000),'{}'))
            try:
                response=self.transport('POST','order',dict(symbol=symbol,side=side,type='LIMIT_MAKER',quantity=qty,price=price,newClientOrderId=client,newOrderRespType='FULL'))
            except Exception:raise LiveLocked('Submission uncertain; do not resubmit') from None
            self._save(client,response)
            return response

    def _save(self,client,response):
        if response.get('clientOrderId')!=client or response.get('status') not in ('NEW','PARTIALLY_FILLED','FILLED','CANCELED','PENDING_CANCEL','REJECTED','EXPIRED','EXPIRED_IN_MATCH'):
            raise LiveLocked('Unrecognized order response; reconciliation required')
        with self.db:self.db.execute('UPDATE orders SET state=?,data=? WHERE id=?',(response['status'],json.dumps(response),client))

    def reconcile(self,client):
        with self.lock:
            row=self.db.execute('SELECT symbol FROM orders WHERE id=?',(client,)).fetchone()
            if not row:raise LiveLocked('Order does not belong to this adapter')
            response=self.transport('GET','order',dict(symbol=row[0],origClientOrderId=client))
            self._save(client,response)
            return response

    def cancel(self,client):
        with self.lock:
            row=self.db.execute('SELECT symbol,state FROM orders WHERE id=?',(client,)).fetchone()
            if not row:raise LiveLocked('Order does not belong to this adapter')
            if row[1] not in ('NEW','PARTIALLY_FILLED','PENDING_CANCEL','UNKNOWN'):return self.reconcile(client)
            with self.db:self.db.execute("UPDATE orders SET state='UNKNOWN' WHERE id=?",(client,))
            response=self.transport('DELETE','order',dict(symbol=row[0],origClientOrderId=client))
            self._save(client,response)
            return response

    def expire(self,now_ms):
        with self.lock:
            ids=[r[0] for r in self.db.execute("SELECT id FROM orders WHERE state IN ('NEW','PARTIALLY_FILLED') AND created<=?",(now_ms-self.config['ORDER_TTL_MINUTES']*60000,))]
            return [self.cancel(client) for client in ids]
