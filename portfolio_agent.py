"""Autonomous, isolated PAPER portfolio operator. No exchange-order imports."""
import copy
import hashlib
import json
import math
import os
import sqlite3
import statistics
import threading
import urllib.request
from pathlib import Path


FAMILIES = ('L1_TREND', 'L2_BREAKOUT', 'S1_TREND', 'S2_BREAKDOWN',
            'N1_RANGE', 'N2_RELATIVE')
FEE = .001
SLIPPAGE = .0002
MAX_SYMBOL = .25
MAX_GROSS = .75
MAX_NET = .5
RISK_PER_TRADE = .005
MIN_EDGE_MULTIPLIER = 1.5


def config_identity(provider, model, version, prompt_hash):
    config = dict(provider=provider, model=model, version=version, promptHash=prompt_hash, fee=FEE,
                  slippage=SLIPPAGE, maxSymbol=MAX_SYMBOL, maxGross=MAX_GROSS,
                  maxNet=MAX_NET, riskPerTrade=RISK_PER_TRADE)
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _return(bars, period):
    return bars[-1]['close'] / bars[-1-period]['close'] - 1


def hypotheses(histories, ranking):
    """Six uncalibrated sensor families; hypotheses are not proof of edge."""
    market = {r['symbol']: r for r in ranking}
    usable = {s: b for s, b in histories.items() if len(b) >= 61}
    if not usable:
        return []
    btc = usable.get('BTCUSDT')
    btc15 = _return(btc, 15) if btc else 0
    relative = {s: _return(b, 15) - btc15 for s, b in usable.items()}
    rows = []
    for symbol, bars in usable.items():
        if symbol not in market:
            continue
        close = bars[-1]['close']
        r5, r15, r30 = (_return(bars, n) for n in (5, 15, 30))
        changes = [y['close']/x['close']-1 for x, y in zip(bars[-31:-1], bars[-30:])]
        volatility = statistics.pstdev(changes) * math.sqrt(15)
        spread = market[symbol].get('spreadPct')
        try:spread=float(spread)
        except (TypeError,ValueError):continue
        if not math.isfinite(spread) or spread < 0:
            continue
        cost = 2*(FEE+SLIPPAGE) + spread/100
        high = max(b['high'] for b in bars[-21:-1])
        low = min(b['low'] for b in bars[-21:-1])
        setups = (
            ('L1_TREND', 'LONG', r15 if r5 > 0 and r15 > 0 and r30 > 0 else 0),
            ('L2_BREAKOUT', 'LONG', max(r15, 0) if close > high else 0),
            ('S1_TREND', 'SHORT', -r15 if r5 < 0 and r15 < 0 and r30 < 0 else 0),
            ('S2_BREAKDOWN', 'SHORT', max(-r15, 0) if close < low else 0),
            ('N1_RANGE', 'LONG' if r5 < 0 else 'SHORT',
                abs(r5) if abs(r15) < volatility and abs(r5) > cost else 0),
            ('N2_RELATIVE', 'LONG' if relative[symbol] > 0 else 'SHORT',
                abs(relative[symbol]) if len(relative) >= 3 else 0),
        )
        for family, direction, gross in setups:
            if gross <= 0:
                continue
            # This is only a ranking heuristic; no probability is asserted.
            expected_net = gross - cost - .25*volatility
            rows.append(dict(symbol=symbol, family=family, directionCandidate=direction,
                confidence=None, expectedMove=gross, expectedNetEdge=expected_net,
                expectedCost=cost, risk=volatility, timeHorizon=15,
                reasonCodes=[family, 'UNCALIBRATED_SENSOR'], price=close,
                volatility=volatility, utility=expected_net-.5*volatility))
    return sorted(rows, key=lambda row: row['utility'], reverse=True)


def local_decision(proposals, positions, promoted):
    """One best portfolio action per cycle; no promoted edge means FLAT."""
    for symbol, position in sorted(positions.items()):
        same = next((p for p in proposals if p['symbol'] == symbol and
                     p['directionCandidate'] == position['direction']), None)
        opposite = next((p for p in proposals if p['symbol'] == symbol and
                         p['directionCandidate'] != position['direction'] and
                         p['family'] in promoted and p['expectedNetEdge'] > 0), None)
        if opposite and (not same or opposite['utility'] > same['utility']):
            return dict(action='CLOSE', symbol=symbol, reasonCodes=['OPPOSING_CONFIRMED_EDGE'])
    for proposal in proposals:
        if (proposal['family'] in promoted and proposal['symbol'] not in positions
                and proposal['expectedNetEdge'] > 0
                and proposal['expectedMove'] >= proposal['expectedCost']*MIN_EDGE_MULTIPLIER):
            side = 1 if proposal['directionCandidate'] == 'LONG' else -1
            stop_distance = max(.005, min(.03, 2*proposal['volatility']))
            return dict(action='OPEN_'+proposal['directionCandidate'], symbol=proposal['symbol'],
                family=proposal['family'], expectedNetReturn=proposal['expectedNetEdge'],
                confidence=proposal['confidence'], probabilityNetProfit=None,
                timeHorizon=proposal['timeHorizon'], reasonCodes=proposal['reasonCodes'],
                positionSize=min(MAX_SYMBOL, RISK_PER_TRADE/stop_distance),
                stop=proposal['price']*(1-side*stop_distance),
                target=proposal['price']*(1+side*2*stop_distance))
    if positions:
        symbol=sorted(positions)[0]
        return dict(action='KEEP', symbol=symbol, reasonCodes=['PROTECTIVE_STOP_ACTIVE',
            'NO_CONFIRMED_OPPOSING_EDGE'])
    return dict(action='FLAT', symbol=None, reasonCodes=['NO_CONFIRMED_EDGE'])


def external_decision(snapshot, timeout=4):
    """Optional HTTPS JSON model. A failure always falls back to local logic."""
    if os.environ.get('AGENT_PROVIDER', 'LOCAL').upper() != 'EXTERNAL':
        return None
    url = os.environ.get('AGENT_API_URL', '')
    key = os.environ.get('AGENT_API_KEY', '')
    if not url.startswith('https://') or not key:
        return None
    body = json.dumps(snapshot, allow_nan=False).encode()
    request = urllib.request.Request(url, body, method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            data = response.read(65537)
            if len(data) > 65536:
                return None
            decision = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(decision, dict) or decision.get('action') not in (
                    'OPEN_LONG','OPEN_SHORT','CLOSE','KEEP','FLAT'):
                return None
            if not isinstance(decision.get('symbol'), (str,type(None))):
                return None
            reasons=decision.get('reasonCodes',[])
            if not isinstance(reasons,list) or len(reasons)>20 or any(
                    not isinstance(reason,str) or len(reason)>100 for reason in reasons):
                return None
            allowed=('action','symbol','family','positionSize','stop','target',
                     'expectedNetReturn','confidence','probabilityNetProfit',
                     'timeHorizon','reasonCodes','hedgeRatio')
            return {key:decision[key] for key in allowed if key in decision}
    except (OSError, ValueError, TimeoutError):
        return None


class PortfolioDecisionAgent:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.provider = os.environ.get('AGENT_PROVIDER', 'LOCAL').upper()
        if self.provider not in ('LOCAL','EXTERNAL'):
            raise ValueError('AGENT_PROVIDER must be LOCAL or EXTERNAL')
        self.model = os.environ.get('AGENT_MODEL', 'DETERMINISTIC_LOCAL')
        self.version = os.environ.get('AGENT_VERSION', 'G0')
        self.prompt_hash = os.environ.get('AGENT_PROMPT_HASH',
            hashlib.sha256(b'quantlab-agent-json-schema-v1').hexdigest())
        self.config_hash = config_identity(self.provider, self.model, self.version, self.prompt_hash)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS agent_state(id INTEGER PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS agent_decisions(time INTEGER PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS agent_trades(id INTEGER PRIMARY KEY,time INTEGER,data TEXT);''')
        row = self.db.execute('SELECT data FROM agent_state WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else dict(cursor=None, cash=100., positions={},
            pending=None, peak=100., version=self.version, provider=self.provider,
            model=self.model, configHash=self.config_hash, lastDecision=None, lastMarks={},
            vetoes=0, externalFailures=0)
        if self.state['configHash'] != self.config_hash:
            self.db.close()
            raise ValueError('Agent version/config changed; use a new PAPER database')
        if row is None:
            with self.db:
                self.db.execute('INSERT INTO agent_state VALUES(1,?)',
                                (json.dumps(self.state, allow_nan=False),))
        # Promotions cannot be supplied through HTTP or environment variables.
        # Until independent OOS/shadow validation exists, no entry capital is assigned.
        self.promoted = frozenset()

    def _equity(self, marks):
        return self.state['cash'] + sum(p['qty']*(marks.get(symbol, p['entry'])-p['entry'])*
            (1 if p['direction'] == 'LONG' else -1) for symbol, p in self.state['positions'].items())

    def _close(self, symbol, price, timestamp, reason):
        p = self.state['positions'].pop(symbol)
        direction = 1 if p['direction'] == 'LONG' else -1
        fill = price*(1-SLIPPAGE*direction)
        fee = abs(fill*p['qty'])*FEE
        pnl = (fill-p['entry'])*p['qty']*direction-p['entryFee']-fee
        self.state['cash'] += (fill-p['entry'])*p['qty']*direction-fee
        self.db.execute('INSERT INTO agent_trades(time,data) VALUES(?,?)', (timestamp,
            json.dumps(dict(symbol=symbol, direction=p['direction'], entryTime=p['entryTime'],
                exitTime=timestamp, entry=p['entry'], exit=fill, qty=p['qty'],
                fees=p['entryFee']+fee, netPnL=pnl, reason=reason, family=p['family'],
                MFE=p['MFE'], MAE=p['MAE']), allow_nan=False)))
        return pnl

    def _risk_check(self, decision, proposals, marks, fresh):
        action = decision.get('action')
        symbol = decision.get('symbol')
        if not isinstance(action,str) or not isinstance(symbol,(str,type(None))):
            return 'INVALID_ACTION'
        if action == 'FLAT':
            return None
        if action not in ('OPEN_LONG', 'OPEN_SHORT', 'CLOSE', 'KEEP'):
            return 'INVALID_ACTION'
        if symbol not in marks:
            return 'UNKNOWN_OR_STALE_SYMBOL'
        if action in ('CLOSE', 'KEEP'):
            return None if symbol in self.state['positions'] else 'NO_POSITION'
        if not fresh:
            return 'STALE_DATA'
        if symbol in self.state['positions']:
            return 'DUPLICATE_SYMBOL_EXPOSURE'
        family = decision.get('family')
        if not isinstance(family,str) or family not in self.promoted:
            return 'NO_CONFIRMED_EDGE'
        proposal = next((p for p in proposals if p['symbol'] == symbol and p['family'] == family
                         and 'OPEN_'+p['directionCandidate'] == action), None)
        if not proposal or proposal['expectedNetEdge'] <= 0:
            return 'NO_POSITIVE_NET_EDGE'
        if proposal['expectedMove'] < proposal['expectedCost']*MIN_EDGE_MULTIPLIER:
            return 'MICRO_EDGE'
        try:
            size=float(decision['positionSize'])
            stop=float(decision['stop'])
            target=float(decision['target'])
        except (KeyError, TypeError, ValueError):
            return 'INVALID_SIZE_OR_PROTECTIVE_STOP'
        if not all(math.isfinite(x) for x in (size, stop, target)) or not 0<size<=MAX_SYMBOL:
            return 'INVALID_SIZE_OR_PROTECTIVE_STOP'
        side=1 if action=='OPEN_LONG' else -1
        if side*(marks[symbol]-stop)<=0 or side*(target-marks[symbol])<=0:
            return 'INVALID_STOP_OR_TARGET_DIRECTION'
        equity=self._equity(marks)
        if equity<=0 or size*equity*((abs(marks[symbol]-stop))/marks[symbol])>RISK_PER_TRADE*equity:
            return 'RISK_LIMIT'
        gross=sum(p['qty']*marks.get(s,p['entry']) for s,p in self.state['positions'].items())
        signed=sum(p['qty']*marks.get(s,p['entry'])*(1 if p['direction']=='LONG' else -1)
                   for s,p in self.state['positions'].items())
        if gross+size*equity>MAX_GROSS*equity or abs(signed+side*size*equity)>MAX_NET*equity:
            return 'PORTFOLIO_EXPOSURE_LIMIT'
        return None

    def cycle(self, histories, ranking, now, fresh=True):
        """Latest closed candle only. Pending orders fill at the *next* candle open."""
        times = {bars[-1]['time'] for bars in histories.values() if bars}
        if len(times) != 1:
            return False
        timestamp = times.pop()
        if timestamp+59999 >= now:
            return False
        with self.lock:
            if self.state['cursor'] is not None and timestamp <= self.state['cursor']:
                return False
            previous = copy.deepcopy(self.state)
            marks = {s: bars[-1]['close'] for s, bars in histories.items() if bars}
            proposals = hypotheses(histories, ranking)
            try:
                with self.db:
                    pending = self.state.pop('pending', None)
                    if pending and self.state['cursor'] == timestamp-60000:
                        symbol = pending['symbol']
                        if symbol in histories:
                            bar = histories[symbol][-1]
                            if pending['action'] == 'CLOSE' and symbol in self.state['positions']:
                                self._close(symbol, bar['open'], timestamp, 'AGENT_CLOSE')
                            elif pending['action'] in ('OPEN_LONG', 'OPEN_SHORT') and fresh:
                                self._open(pending, bar['open'], timestamp, marks)
                    risk_actions=[]
                    for symbol, p in list(self.state['positions'].items()):
                        if symbol not in histories:
                            continue
                        bar = histories[symbol][-1]
                        direction = 1 if p['direction'] == 'LONG' else -1
                        p['MFE'] = max(p['MFE'], (bar['high']-p['entry'])/p['entry'] if direction == 1
                            else (p['entry']-bar['low'])/p['entry'])
                        p['MAE'] = max(p['MAE'], (p['entry']-bar['low'])/p['entry'] if direction == 1
                            else (bar['high']-p['entry'])/p['entry'])
                        hit = bar['low'] <= p['stop'] if direction == 1 else bar['high'] >= p['stop']
                        if hit:
                            conservative = min(p['stop'], bar['close']) if direction == 1 else max(p['stop'], bar['close'])
                            net=self._close(symbol, conservative, timestamp, 'HARD_STOP')
                            risk_actions.append(dict(action='CLOSE_LOSS' if net<0 else 'CLOSE_PROFIT',
                                symbol=symbol,reason='HARD_STOP',netPnL=net))
                        else:
                            favorable=(bar['close']-p['entry'])*direction/p['entry']
                            if favorable>=.01:
                                trail=bar['close']*(1-.01*direction)
                                new_stop=max(p['stop'],p['entry'],trail) if direction==1 else min(p['stop'],p['entry'],trail)
                                if new_stop!=p['stop']:
                                    p['stop']=new_stop
                                    risk_actions.append(dict(action='MOVE_STOP',symbol=symbol,stop=new_stop))
                    equity = self._equity(marks)
                    self.state['peak'] = max(self.state['peak'], equity)
                    local = local_decision(proposals, self.state['positions'], self.promoted)
                    try:timeout=max(1.,min(15.,float(os.environ.get('AGENT_TIMEOUT','4'))))
                    except ValueError:timeout=4.
                    external = external_decision(dict(time=timestamp, paperOnly=True,
                        proposals=proposals[:50], positions=self.state['positions'], equity=equity),timeout)
                    if self.provider == 'EXTERNAL' and external is None:
                        self.state['externalFailures'] += 1
                    selected = external if external is not None else local
                    veto = self._risk_check(selected, proposals, marks, fresh)
                    if veto:
                        self.state['vetoes'] += 1
                        selected = local
                        fallback_veto = self._risk_check(selected, proposals, marks, fresh)
                        if fallback_veto:
                            selected = dict(action='FLAT', symbol=None, reasonCodes=[fallback_veto])
                    if selected['action'] in ('OPEN_LONG', 'OPEN_SHORT', 'CLOSE'):
                        self.state['pending'] = {key: selected[key] for key in ('action','symbol')}
                        if selected['action'].startswith('OPEN_'):
                            for key in ('family','positionSize','stop','target'):
                                self.state['pending'][key] = selected[key]
                    self.state['cursor'] = timestamp
                    self.state['lastMarks'] = marks
                    record = dict(time=timestamp, provider='EXTERNAL' if selected is external else 'LOCAL',
                        model=self.model, agentVersion=self.version, configHash=self.config_hash,
                        promptHash=self.prompt_hash,
                        action=selected['action'], symbol=selected.get('symbol'),
                        reasonCodes=selected.get('reasonCodes', []), veto=veto,
                        riskActions=risk_actions,
                        candidates=len(proposals), promotedFamilies=sorted(self.promoted),
                        equity=equity, noConfirmedEdge=not self.promoted)
                    self.state['lastDecision'] = record
                    self.db.execute('INSERT INTO agent_decisions VALUES(?,?)',
                                    (timestamp, json.dumps(record, allow_nan=False)))
                    self.db.execute('INSERT OR REPLACE INTO agent_state VALUES(1,?)',
                                    (json.dumps(self.state, allow_nan=False),))
            except BaseException:
                self.state = previous
                raise
            return True

    def _open(self, decision, price, timestamp, marks):
        symbol = decision['symbol']
        if symbol in self.state['positions'] or decision['family'] not in self.promoted:
            return
        direction = decision['action'].removeprefix('OPEN_')
        equity = self._equity(marks)
        if equity <= 0 or self.state['peak'] > 0 and equity < self.state['peak']*.95:
            return
        gross = sum(p['qty']*marks.get(s, p['entry']) for s, p in self.state['positions'].items())
        signed = sum(p['qty']*marks.get(s, p['entry'])*(1 if p['direction']=='LONG' else -1)
                     for s,p in self.state['positions'].items())
        side = 1 if direction == 'LONG' else -1
        fill = price*(1+SLIPPAGE*side)
        stop = float(decision['stop'])
        target = float(decision['target'])
        if side*(fill-stop)<=0 or side*(target-fill)<=0:
            return
        distance = abs(fill-stop)/fill
        requested = float(decision['positionSize'])*equity
        limits=(equity*MAX_SYMBOL, equity*MAX_GROSS-gross,
                max(0, equity*MAX_NET-side*signed), equity*RISK_PER_TRADE/distance)
        if any(requested > limit+1e-8 for limit in limits):
            return
        amount=requested
        if amount <= .01:
            return
        qty = amount/fill
        fee = amount*FEE
        self.state['cash'] -= fee
        self.state['positions'][symbol] = dict(direction=direction, entry=fill, entryTime=timestamp,
            qty=qty, entryFee=fee, stop=stop, target=target,
            family=decision['family'], initialEV=None, currentEV=None, initialRegime=None,
            currentRegime=None, initialConfidence=None, currentConfidence=None, MFE=0., MAE=0.)

    def snapshot(self):
        with self.lock:
            trades=[json.loads(row[0]) for row in self.db.execute('SELECT data FROM agent_trades ORDER BY id')]
            net=sum(row['netPnL'] for row in trades)
            marks=self.state.get('lastMarks',{})
            equity=self._equity(marks)
            return dict(mode='PAPER_ONLY', autonomous=True, liveReady=False,
                manualTradeApproval=False, provider=self.provider, model=self.model,
                agentVersion=self.version, configHash=self.config_hash,promptHash=self.prompt_hash,
                externalConfigured=self.provider=='EXTERNAL' and bool(os.environ.get('AGENT_API_KEY')),
                promotedFamilies=sorted(self.promoted), cash=self.state['cash'],
                positions=copy.deepcopy(self.state['positions']), cursor=self.state['cursor'],
                lastDecision=copy.deepcopy(self.state['lastDecision']),
                vetoes=self.state['vetoes'], externalFailures=self.state['externalFailures'],
                completedTrades=len(trades), agentNetPnL=equity-100,
                agentExpectancy=net/len(trades) if trades else None,
                equity=equity,
                families=FAMILIES, status='NO_CONFIRMED_EDGE' if not self.promoted else 'ACTIVE_PAPER')

    def close(self):
        self.db.close()
