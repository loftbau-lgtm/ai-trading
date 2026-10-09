"""Isolated USDⓈ-M-style PAPER ledger; never sends an exchange order."""
import copy
import hashlib
import json
import math
import sqlite3
import statistics
import threading
from pathlib import Path

from portfolio_agent import hypotheses
from futures_edge_engine import signal as research_signal
from futures_external_agent import external_decision


def load_config(path=None):
    source=Path(path or Path(__file__).with_name('futures_paper_config.json'))
    config=json.loads(source.read_text(encoding='utf-8'))
    for key in ('startingCapital','makerFee','takerFee','slippage',
                'maxSymbolExposure','maxGrossExposure','maxNetExposure',
                'maxCorrelatedExposure','correlationThreshold','maxPositionRisk',
                'maxDrawdown','maintenanceMarginRate',
                'minEdgeMultiplier','minRewardRisk','minProbabilityNetProfit'):
        value=config[key]
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('Invalid Futures PAPER config: '+key)
    if any(config[key]>1 for key in ('maxSymbolExposure','maxGrossExposure','maxNetExposure',
        'maxCorrelatedExposure','correlationThreshold')):
        raise ValueError('Effective leverage must not exceed 1x')
    return config


def position_key(symbol, side):
    if side not in ('LONG','SHORT'):
        raise ValueError('positionSide must be LONG or SHORT')
    return symbol+'|'+side


def direction(side):
    return 1 if side=='LONG' else -1


def finite_positive(value):
    value=float(value)
    if not math.isfinite(value) or value<=0:
        raise ValueError('Invalid Futures PAPER value')
    return value


def quote_fill(market, execution_side, slippage):
    bid,ask=finite_positive(market['bid']),finite_positive(market['ask'])
    if bid>=ask:
        raise ValueError('Crossed Futures book')
    mid=(bid+ask)/2
    if execution_side=='BUY':
        quote=ask
        fill=quote*(1+slippage)
        spread_cost=fill*0+(ask-mid)
        slip_cost=fill-quote
    elif execution_side=='SELL':
        quote=bid
        fill=quote*(1-slippage)
        spread_cost=mid-bid
        slip_cost=quote-fill
    else:
        raise ValueError('Invalid synthetic execution side')
    return dict(mid=mid,quote=quote,fill=fill,spreadPerUnit=spread_cost,
                slippagePerUnit=slip_cost)


class FuturesPaperAccount:
    def __init__(self,path,config=None):
        self.config=config or load_config()
        self.path=Path(path)
        if self.path.name!='binance_futures_paper.sqlite3':
            raise ValueError('Futures PAPER requires its own ledger filename')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.path,check_same_thread=False,timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS futures_state(id INTEGER PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS futures_events(id INTEGER PRIMARY KEY,time INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS futures_trades(id INTEGER PRIMARY KEY,time INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS futures_funding(id TEXT PRIMARY KEY,time INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS futures_decisions(time INTEGER PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS futures_outcomes(decisionTime INTEGER NOT NULL,
                horizon INTEGER NOT NULL,data TEXT NOT NULL,
                PRIMARY KEY(decisionTime,horizon));''')
        self.config_hash=hashlib.sha256(json.dumps(self.config,sort_keys=True).encode()).hexdigest()
        row=self.db.execute('SELECT data FROM futures_state WHERE id=1').fetchone()
        self.state=json.loads(row[0]) if row else dict(cursor=None,wallet=self.config['startingCapital'],
            positions={},peak=self.config['startingCapital'],realizedPnL=0.,fundingPnL=0.,
            fees=0.,spreadCost=0.,slippageCost=0.,lastDecision=None,lastMarkets={},
            pausedAll=False,configHash=self.config_hash)
        self.state.setdefault('pausedAll',False)
        if self.state['configHash']!=self.config_hash:
            prior={**self.config,'maxSymbolExposure':.25}
            prior_hash=hashlib.sha256(json.dumps(prior,sort_keys=True).encode()).hexdigest()
            if self.state['configHash']!=prior_hash:
                self.db.close()
                raise ValueError('Futures PAPER config changed; use a new ledger')
            with self.db:
                self.state['configHash']=self.config_hash
                self._persist()
                self._event(self.state['cursor'] or 0,'RISK_PROFILE_MIGRATION',
                    'PORTFOLIO',None,'NONE',oldMaxSymbolExposure=.25,
                    newMaxSymbolExposure=self.config['maxSymbolExposure'])
        if row is None:
            with self.db:self._persist()
        self.promoted=frozenset()
        self.edge_evidence={}
        self.active_models={}
        self.status='starting'
        self.error=None

    def _persist(self):
        self.db.execute('INSERT OR REPLACE INTO futures_state VALUES(1,?)',
                        (json.dumps(self.state,allow_nan=False),))

    def funding_since(self):
        with self.lock:
            result={}
            for p in self.state['positions'].values():
                symbol=p['symbol']
                result[symbol]=min(result.get(symbol,p['lastFundingTime']),p['lastFundingTime'])
            return result

    def set_paused(self,paused,time_ms):
        with self.lock:
            paused=bool(paused)
            if self.state['pausedAll']==paused:
                return False
            with self.db:
                self.state['pausedAll']=paused
                self._event(int(time_ms),'PAUSE_ALL' if paused else 'RESUME',
                            'PORTFOLIO',None,'NONE')
                self._persist()
            return True

    def _market_equity(self,markets):
        equity=self.state['wallet']
        for p in self.state['positions'].values():
            market=markets.get(p['symbol'])
            mark=market['markPrice'] if market else p['lastMarkPrice']
            equity+=direction(p['positionSide'])*(mark-p['entryPrice'])*p['qty']
        return equity

    def _risk_exposure(self,markets):
        gross=net=0.
        for p in self.state['positions'].values():
            market=markets.get(p['symbol'])
            mark=market['markPrice'] if market else p['lastMarkPrice']
            amount=p['qty']*mark
            gross+=amount
            net+=direction(p['positionSide'])*amount
        return gross,net

    def _event(self,time,action,symbol,side,execution_side,**other):
        event=dict(time=time,action=action,symbol=symbol,positionSide=side,
                   executionSide=execution_side,paperOnly=True,**other)
        self.db.execute('INSERT INTO futures_events(time,data) VALUES(?,?)',
                        (time,json.dumps(event,allow_nan=False)))
        return event

    def _entry_veto(self,decision,market,markets):
        c=self.config
        action=decision.get('action')
        if action not in ('OPEN_LONG','OPEN_SHORT'):
            return 'INVALID_ACTION'
        if self.state['pausedAll']:
            return 'PAPER_PAUSED'
        side=action.removeprefix('OPEN_')
        symbol=decision.get('symbol')
        if not isinstance(symbol,str) or symbol not in markets:
            return 'UNKNOWN_SYMBOL'
        if not isinstance(decision.get('family'),str) or decision['family'] not in self.promoted:
            return 'NO_CONFIRMED_EDGE'
        if self.active_models:
            model=self.active_models.get(decision.get('modelId'))
            if not model or model['family']!=decision['family'] or model['direction']!=side:
                return 'UNVALIDATED_MODEL_OR_DIRECTION'
        evidence=self.edge_evidence.get(decision['family']+'|'+side,
                                         self.edge_evidence.get(decision['family'])) or {}
        probability=evidence.get('probabilityNetProfit')
        if probability is None or not c['minProbabilityNetProfit']<=probability<=1:
            return 'UNVALIDATED_NET_PROFIT_PROBABILITY'
        if position_key(symbol,side) in self.state['positions']:
            return 'DUPLICATE_POSITION_SIDE'
        if position_key(symbol,'SHORT' if side=='LONG' else 'LONG') in self.state['positions']:
            return 'SAME_SYMBOL_OPPOSITE_EXPOSURE'
        try:
            fraction=float(decision['positionSize'])
            stop=float(decision['stop'])
            target=float(decision['target'])
            expected=float(decision['expectedNetReturn'])
            move=float(decision['expectedMove'])
            cost=float(decision['fullRoundTripCost'])
        except (KeyError,ValueError,TypeError):
            return 'MISSING_RISK_OR_EDGE_FIELD'
        if not all(math.isfinite(x) for x in (fraction,stop,target,expected,move,cost)):
            return 'INVALID_NONFINITE_FIELD'
        if fraction<=0 or fraction>c['maxSymbolExposure']:
            return 'SYMBOL_EXPOSURE_LIMIT'
        quote=quote_fill(market,'BUY' if side=='LONG' else 'SELL',c['slippage'])
        price=quote['fill']
        sign=direction(side)
        if sign*(price-stop)<=0 or sign*(target-price)<=0:
            return 'INVALID_STOP_OR_TARGET'
        stop_distance=abs(price-stop)/price
        reward_distance=abs(target-price)/price
        mid=(market['bid']+market['ask'])/2
        model_cost=(2*(c['takerFee']+c['slippage'])+
            (market['ask']-market['bid'])/mid+abs(market['fundingRate']))
        if cost+1e-9<model_cost:
            return 'UNDERSTATED_FUTURES_COST'
        if reward_distance/stop_distance<c['minRewardRisk']:
            return 'LOW_REWARD_RISK'
        if (expected<=0 or expected>move-cost+1e-9 or cost<=0 or
                move<=cost*c['minEdgeMultiplier'] or reward_distance<=cost):
            return 'NO_POSITIVE_NET_EDGE'
        equity=self._market_equity(markets)
        if equity<=0 or equity<self.state['peak']*(1-c['maxDrawdown']):
            return 'DRAWDOWN_LIMIT'
        requested=equity*fraction
        gross,net=self._risk_exposure(markets)
        if requested+gross>equity*c['maxGrossExposure']+1e-9:
            return 'GROSS_EXPOSURE_LIMIT'
        if abs(net+sign*requested)>equity*c['maxNetExposure']+1e-9:
            return 'NET_EXPOSURE_LIMIT'
        if requested*stop_distance>equity*c['maxPositionRisk']+1e-9:
            return 'POSITION_RISK_LIMIT'
        new_bars=market['bars']
        if len(new_bars)>=31:
            new_returns=[b['close']/a['close']-1 for a,b in zip(new_bars[-31:-1],new_bars[-30:])]
            if statistics.stdev(new_returns)>0:
                correlated=requested
                for p in self.state['positions'].values():
                    if p['positionSide']!=side or p['symbol'] not in markets:continue
                    old_bars=markets[p['symbol']]['bars']
                    if len(old_bars)<31:continue
                    old_returns=[b['close']/a['close']-1 for a,b in zip(old_bars[-31:-1],old_bars[-30:])]
                    if statistics.stdev(old_returns)>0 and statistics.correlation(
                            new_returns,old_returns)>=c['correlationThreshold']:
                        correlated+=p['qty']*markets[p['symbol']]['markPrice']
                if correlated>equity*c['maxCorrelatedExposure']+1e-9:
                    return 'CORRELATED_EXPOSURE_LIMIT'
        age=int(decision.get('decisionTime',market['timestamp']))-int(market['timestamp'])
        if age<0 or age>c['maxDataAgeMs']:
            return 'STALE_MARKET_DATA'
        if int(market['bars'][-1]['time']) != int(decision.get('decisionTime',market['timestamp']))//60000*60000-60000:
            return 'STALE_CLOSED_CANDLE'
        return None

    def _open(self,decision,market,markets,time):
        veto=self._entry_veto(decision,market,markets)
        if veto:return veto
        side=decision['action'].removeprefix('OPEN_')
        execution_side='BUY' if side=='LONG' else 'SELL'
        quote=quote_fill(market,execution_side,self.config['slippage'])
        equity=self._market_equity(markets)
        notional=equity*float(decision['positionSize'])
        qty=notional/quote['fill']
        fee=notional*self.config['takerFee']
        spread=quote['spreadPerUnit']*qty
        slip=quote['slippagePerUnit']*qty
        self.state['wallet']-=fee
        self.state['fees']+=fee
        self.state['spreadCost']+=spread
        self.state['slippageCost']+=slip
        p=dict(symbol=decision['symbol'],positionSide=side,qty=qty,
               entryTime=time,entryPrice=quote['fill'],entryContractPrice=quote['mid'],
               entryFee=fee,entrySpreadCost=spread,entrySlippageCost=slip,
               stop=float(decision['stop']),target=float(decision['target']),
               initialMargin=notional,maintenanceMarginRate=self.config['maintenanceMarginRate'],
               lastMarkPrice=market['markPrice'],fundingPnL=0.,lastFundingTime=time,
               fundingRate=market['fundingRate'],nextFundingTime=market['nextFundingTime'],
               family=decision['family'],hedge=bool(decision.get('hedgeRatio')),
               hedgeRatio=decision.get('hedgeRatio'),MFE=0.,MAE=0.,
               initialStopDistance=abs(quote['fill']-float(decision['stop']))/quote['fill'],
               minimumProfitTarget=float(decision['target']),
               dynamicTarget=float(decision['target']),partialTaken=False,
               probabilityNetProfit=self.edge_evidence.get(decision['family']+'|'+side,
                   self.edge_evidence.get(decision['family']))['probabilityNetProfit'],
               EVKeep=decision['expectedNetReturn'],agentDecision=decision['action'])
        self.state['positions'][position_key(p['symbol'],side)]=p
        self._event(time,'HEDGE OPEN' if p['hedge'] else 'OPEN '+side,p['symbol'],side,
                    execution_side,qty=qty,price=quote['fill'],fee=fee,
                    spreadCost=spread,slippageCost=slip)
        return None

    def _close(self,symbol,side,market,time,reason,portion=1.):
        key=position_key(symbol,side)
        p=self.state['positions'][key]
        portion=float(portion)
        if not 0<portion<=1:raise ValueError('Invalid close fraction')
        sign=direction(side)
        execution_side='SELL' if side=='LONG' else 'BUY'
        quote=quote_fill(market,execution_side,self.config['slippage'])
        qty=p['qty']*portion
        exit_fee=quote['fill']*qty*self.config['takerFee']
        exit_spread=quote['spreadPerUnit']*qty
        exit_slip=quote['slippagePerUnit']*qty
        gross=(quote['mid']-p['entryContractPrice'])*qty*sign
        funding=p['fundingPnL']*portion
        entry_fee=p['entryFee']*portion
        entry_spread=p['entrySpreadCost']*portion
        entry_slip=p['entrySlippageCost']*portion
        net=gross+funding-entry_fee-exit_fee-entry_spread-exit_spread-entry_slip-exit_slip
        self.state['wallet']+=(quote['fill']-p['entryPrice'])*qty*sign-exit_fee
        self.state['realizedPnL']+=net
        self.state['fees']+=exit_fee
        self.state['spreadCost']+=exit_spread
        self.state['slippageCost']+=exit_slip
        trade=dict(symbol=symbol,positionSide=side,entryTime=p['entryTime'],exitTime=time,
                   qty=qty,entryPrice=p['entryPrice'],exitPrice=quote['fill'],
                   contractEntryPrice=p['entryContractPrice'],contractExitPrice=quote['mid'],
                   grossTradingPnL=gross,fundingPnL=funding,fees=entry_fee+exit_fee,
                   spreadCost=entry_spread+exit_spread,slippageCost=entry_slip+exit_slip,
                   netPnL=net,reason=reason,hedge=p['hedge'],family=p['family'])
        self.db.execute('INSERT INTO futures_trades(time,data) VALUES(?,?)',
                        (time,json.dumps(trade,allow_nan=False)))
        action=('HEDGE CLOSE' if p['hedge'] else 'CLOSE '+side) if portion==1 else 'REDUCE '+side
        self._event(time,action,symbol,side,execution_side,qty=qty,price=quote['fill'],
                    fee=exit_fee,netPnL=net,reason=reason)
        if portion==1:
            del self.state['positions'][key]
        else:
            p['qty']-=qty
            p['initialMargin']*=1-portion
            p['entryFee']*=1-portion
            p['entrySpreadCost']*=1-portion
            p['entrySlippageCost']*=1-portion
            p['fundingPnL']*=1-portion
        return trade

    def _funding(self,markets,time):
        for key,p in list(self.state['positions'].items()):
            market=markets.get(p['symbol'])
            if not market:continue
            for event in sorted(market.get('settledFunding',[]),key=lambda row:row['fundingTime']):
                funding_time=int(event['fundingTime'])
                if not p['lastFundingTime']<funding_time<=time:continue
                rate=float(event['fundingRate'])
                mark=finite_positive(event['markPrice'])
                amount=-direction(p['positionSide'])*p['qty']*mark*rate
                event_id=key+'|'+str(funding_time)
                if self.db.execute('SELECT 1 FROM futures_funding WHERE id=?',(event_id,)).fetchone():
                    p['lastFundingTime']=funding_time
                    continue
                p['fundingPnL']+=amount
                p['lastFundingTime']=funding_time
                self.state['wallet']+=amount
                self.state['fundingPnL']+=amount
                data=dict(symbol=p['symbol'],positionSide=p['positionSide'],fundingTime=funding_time,
                          fundingRate=rate,markPrice=mark,fundingPaidReceived=amount)
                self.db.execute('INSERT INTO futures_funding VALUES(?,?,?)',
                                (event_id,funding_time,json.dumps(data,allow_nan=False)))

    def _mark_risk(self,markets,time):
        actions=[]
        for key,p in list(self.state['positions'].items()):
            market=markets.get(p['symbol'])
            if not market:continue
            mark=finite_positive(market['markPrice'])
            p['lastMarkPrice']=mark
            p['fundingRate']=market['fundingRate']
            p['nextFundingTime']=market['nextFundingTime']
            sign=direction(p['positionSide'])
            move=sign*(mark-p['entryPrice'])/p['entryPrice']
            p['MFE']=max(p['MFE'],move)
            p['MAE']=max(p['MAE'],-move)
            margin_equity=p['initialMargin']+sign*(mark-p['entryPrice'])*p['qty']+p['fundingPnL']-p['entryFee']
            maintenance=p['qty']*mark*p['maintenanceMarginRate']
            hit=mark<=p['stop'] if sign==1 else mark>=p['stop']
            mature=time-int(p['entryTime'])>=5*60000
            target_hit=mature and (mark>=p['target'] if sign==1 else mark<=p['target'])
            if hit or margin_equity<=maintenance or target_hit:
                reason=('MARK_STOP' if hit else 'ESTIMATED_MARGIN_LIQUIDATION'
                        if margin_equity<=maintenance else 'PROFIT_TARGET')
                actions.append(self._close(p['symbol'],p['positionSide'],market,time,reason))
                continue
            if mature and not p.get('partialTaken') and move>=max(.01,p.get('initialStopDistance',.01)):
                liquidation=quote_fill(market,'SELL' if sign==1 else 'BUY',self.config['slippage'])
                net=(sign*(liquidation['fill']-p['entryPrice'])*p['qty']-
                     p['entryFee']-liquidation['fill']*p['qty']*self.config['takerFee'])
                if net>0:
                    actions.append(self._close(p['symbol'],p['positionSide'],market,time,
                                               'PROFIT_PARTIAL_25',.25))
                    p['partialTaken']=True
                    p['stop']=max(p['stop'],p['entryPrice']) if sign==1 else min(p['stop'],p['entryPrice'])
                    p['agentDecision']='REDUCE_'+p['positionSide']
            if move>=.01:
                trailing=mark*(1-.01*sign)
                tighter=max(p['stop'],p['entryPrice'],trailing) if sign==1 else min(p['stop'],p['entryPrice'],trailing)
                if tighter!=p['stop']:
                    p['stop']=tighter
                    p['agentDecision']='TRAIL_'+p['positionSide']
        # A price move can raise effective leverage after an initially valid
        # entry. Liquidate the largest PAPER risk until gross <= equity.
        while self.state['positions']:
            gross,_=self._risk_exposure(markets)
            equity=self._market_equity(markets)
            if equity>0 and gross<=equity:
                break
            exposed=[p for p in self.state['positions'].values() if p['symbol'] in markets]
            if not exposed:break
            largest=max(exposed,key=lambda p:p['qty']*markets[p['symbol']]['markPrice'])
            actions.append(self._close(largest['symbol'],largest['positionSide'],
                                       markets[largest['symbol']],time,'EFFECTIVE_LEVERAGE_LIMIT'))
        return actions

    def _evaluate_outcomes(self,markets,candle_time):
        """Mature prior decisions from later closed candles, once per horizon."""
        for decision_time,raw in self.db.execute('SELECT time,data FROM futures_decisions '
                'WHERE time BETWEEN ? AND ? ORDER BY time',
                (candle_time-119*60000,candle_time-5*60000)):
            decision=json.loads(raw)
            action=decision.get('action','')
            symbol=decision.get('symbol')
            if not symbol or symbol not in markets or not action.endswith(('LONG','SHORT')):
                continue
            side='LONG' if action.endswith('LONG') else 'SHORT'
            reference=decision.get('decisionMid')
            if not reference:
                continue
            for horizon in (5,15,30):
                exit_time=decision_time+horizon*60000
                if exit_time>candle_time or self.db.execute(
                        'SELECT 1 FROM futures_outcomes WHERE decisionTime=? AND horizon=?',
                        (decision_time,horizon)).fetchone():
                    continue
                bar=next((bar for bar in markets[symbol]['bars'] if bar['time']==exit_time),None)
                if not bar:
                    continue
                market=markets[symbol]
                spread=(market['ask']-market['bid'])/((market['ask']+market['bid'])/2)
                cost=2*(self.config['takerFee']+self.config['slippage'])+spread
                gross=direction(side)*(bar['close']/reference-1)
                net=gross-cost
                if action.startswith('KEEP_'):
                    verdict='CAPTURED_PROFIT' if net>0 else 'BAD_HOLD_LOSS'
                elif action.startswith(('CLOSE_','REDUCE_')):
                    verdict='PREMATURE_CLOSE_LOSS' if net>0 else 'AVOIDED_LOSS'
                else:
                    verdict='OBSERVATION'
                result=dict(decisionTime=decision_time,exitTime=exit_time,
                    horizonMinutes=horizon,action=action,symbol=symbol,
                    positionSide=side,referencePrice=reference,
                    futureClose=bar['close'],grossReturn=gross,
                    estimatedRoundTripCost=cost,counterfactualNetReturn=net,
                    verdict=verdict,paperOnly=True)
                self.db.execute('INSERT INTO futures_outcomes VALUES(?,?,?)',
                                (decision_time,horizon,json.dumps(result,allow_nan=False)))

    def _opportunities(self,markets):
        if self.active_models:
            rows=[]
            benchmark=markets.get('BTCUSDT',{}).get('bars',[])
            for model,evidence in self.active_models.items():
                side=evidence['direction']
                family=evidence['family']
                for symbol,m in markets.items():
                    if model.startswith('N2_') and symbol=='BTCUSDT':
                        continue
                    if not research_signal(model,m['bars'],benchmark):
                        continue
                    mid=(m['bid']+m['ask'])/2
                    cost=2*(self.config['takerFee']+self.config['slippage'])+(
                        m['ask']-m['bid'])/mid+abs(m['fundingRate'])
                    expected_move=evidence['grossExpectancy']
                    expected_net=min(evidence['netExpectancy'],expected_move-cost)
                    changes=[b['close']/a['close']-1 for a,b in zip(m['bars'][-31:-1],m['bars'][-30:])]
                    rows.append(dict(modelId=model,symbol=symbol,family=family,
                        directionCandidate=side,expectedMove=expected_move,
                        expectedNetEdge=expected_net,fullRoundTripCost=cost,
                        probabilityNetProfit=evidence['probabilityNetProfit'],
                        volatility=statistics.pstdev(changes)*math.sqrt(15),
                        risk=statistics.pstdev(changes)*math.sqrt(15),
                        evidenceStatus='CONFIRMED'))
            return sorted(rows,key=lambda row:row['expectedNetEdge'],reverse=True)
        histories={s:m['bars'] for s,m in markets.items()}
        ranking=[dict(symbol=s,spreadPct=100*(m['ask']-m['bid'])/((m['ask']+m['bid'])/2))
                 for s,m in markets.items()]
        proposals=hypotheses(histories,ranking)
        rows=[]
        for p in proposals:
            m=markets[p['symbol']]
            cost=(2*(self.config['takerFee']+self.config['slippage'])+
                (m['ask']-m['bid'])/((m['ask']+m['bid'])/2))
            funding=abs(m['fundingRate'])
            expected=p['expectedMove']-cost-funding-p['risk']*.25
            evidence=self.edge_evidence.get(p['family']) or {}
            rows.append(dict(**{**p,'expectedNetEdge':expected},fullRoundTripCost=cost+funding,
                probabilityNetProfit=evidence.get('probabilityNetProfit'),
                evidenceStatus='CONFIRMED' if p['family'] in self.promoted else 'UNVALIDATED'))
        return sorted(rows,key=lambda row:row['expectedNetEdge'],reverse=True)

    def _market_features(self,markets):
        previous=self.state.get('lastMarkets',{})
        btc=markets.get('BTCUSDT')
        if btc and len(btc['bars'])>=31:
            btc_bars=btc['bars']
            btc_return15=btc_bars[-1]['close']/btc_bars[-16]['close']-1
            changes=[b['close']/a['close']-1 for a,b in zip(btc_bars[-31:-1],btc_bars[-30:])]
            volatility=statistics.stdev(changes)*math.sqrt(15)
            regime=('SHOCK' if volatility>=.04 or abs(btc_return15)>=.05 else
                    'BULL' if btc_return15>.005 else
                    'BEAR' if btc_return15<-.005 else 'RANGE')
        else:
            btc_return15=volatility=None
            regime='UNKNOWN'
        result={}
        for symbol,market in markets.items():
            old=previous.get(symbol,{})
            prior_oi=old.get('openInterest')
            prior_funding=old.get('fundingRate')
            result[symbol]=dict(regime=regime,btcReturn15m=btc_return15,
                btcVolatility15m=volatility,
                markIndexBasis=(market['markPrice']-market['indexPrice'])/market['indexPrice'],
                fundingRate=market['fundingRate'],
                fundingTrend=market['fundingRate']-prior_funding if prior_funding is not None else None,
                openInterest=market['openInterest'],
                openInterestChange=(market['openInterest']/prior_oi-1) if prior_oi else None,
                orderBookImbalance=market.get('orderBookImbalance'),
                takerImbalance=market.get('takerImbalance'))
        return result

    def _hedge_candidate(self,opportunities,markets,time):
        """Partial cross-symbol BTC short only if estimated beta-risk reduction exceeds cost."""
        btc=markets.get('BTCUSDT')
        if not btc or position_key('BTCUSDT','SHORT') in self.state['positions']:
            return None
        bearish=[p for p in opportunities if p['symbol']=='BTCUSDT' and
                 p['directionCandidate']=='SHORT' and p['family'] in self.promoted and
                 p['expectedNetEdge']>0]
        if not bearish:return None
        pick=bearish[0]
        btc_bars=btc['bars']
        if len(btc_bars)<31 or btc_bars[-1]['close']>=btc_bars[-16]['close']:
            return None
        btc_returns=[b['close']/a['close']-1 for a,b in zip(btc_bars[-31:-1],btc_bars[-30:])]
        variance=statistics.variance(btc_returns)
        if variance<=0:return None
        volatility=statistics.stdev(btc_returns)*math.sqrt(15)
        equity=self._market_equity(markets)
        if equity<=0:return None
        for p in self.state['positions'].values():
            if p['positionSide']!='LONG' or p['symbol']=='BTCUSDT' or p['symbol'] not in markets:
                continue
            alt_bars=markets[p['symbol']]['bars']
            if len(alt_bars)<31:continue
            alt_returns=[b['close']/a['close']-1 for a,b in zip(alt_bars[-31:-1],alt_bars[-30:])]
            if statistics.stdev(alt_returns)==0:continue
            corr=statistics.correlation(alt_returns,btc_returns)
            beta=statistics.covariance(alt_returns,btc_returns)/variance
            if corr<.5 or beta<=0:continue
            exposure=p['qty']*markets[p['symbol']]['markPrice']
            factor_exposure=exposure*beta
            desired_notional=min(.1*equity,.5*factor_exposure)
            cost=desired_notional*pick['fullRoundTripCost']
            risk_reduction=desired_notional*volatility
            if desired_notional<=0 or risk_reduction<=cost:
                continue
            bid=btc['bid']
            distance=max(.005,min(.03,2*pick['volatility']))
            fraction=min(desired_notional/equity,self.config['maxPositionRisk']/distance)
            return dict(action='OPEN_SHORT',symbol='BTCUSDT',family=pick['family'],
                modelId=pick.get('modelId'),
                positionSize=fraction,stop=bid*(1+distance),target=bid*(1-2*distance),
                expectedNetReturn=pick['expectedNetEdge'],expectedMove=pick['expectedMove'],
                fullRoundTripCost=pick['fullRoundTripCost'],decisionTime=time,
                hedgeRatio=fraction*equity/exposure,
                riskReductionEstimate=risk_reduction,costEstimate=cost,
                correlation=corr,beta=beta)
        return None

    def cycle(self,snapshot):
        now=int(snapshot['serverTime'])
        markets=snapshot['markets']
        if not markets or any(m['bars'][-1]['end']>=now for m in markets.values()):
            raise ValueError('Only closed Futures candles may be evaluated')
        candle_times={m['bars'][-1]['time'] for m in markets.values()}
        if len(candle_times)!=1:raise ValueError('Futures candles are not aligned')
        timestamp=candle_times.pop()
        if timestamp != now//60000*60000-60000:
            raise ValueError('Futures closed candle is stale')
        with self.lock:
            if self.state['cursor'] is not None and timestamp<=self.state['cursor']:
                return False
            old=copy.deepcopy(self.state)
            try:
                with self.db:
                    self._funding(markets,now)
                    exits=self._mark_risk(markets,now)
                    self._evaluate_outcomes(markets,timestamp)
                    opportunities=self._opportunities(markets)
                    features=self._market_features(markets)
                    conflict_symbols=set()
                    for symbol in markets:
                        long=max((p['expectedNetEdge'] for p in opportunities if p['symbol']==symbol
                            and p['directionCandidate']=='LONG'),default=None)
                        short=max((p['expectedNetEdge'] for p in opportunities if p['symbol']==symbol
                            and p['directionCandidate']=='SHORT'),default=None)
                        if (long is not None and short is not None and long>0 and short>0 and
                                abs(long-short)<=max(.001,.1*max(long,short))):
                            conflict_symbols.add(symbol)
                    decision=dict(action='FLAT',symbol=None,reasonCodes=['NO_CONFIRMED_EDGE'])
                    provider='LOCAL'
                    agent_choice=external_decision(dict(time=now,paperOnly=True,
                        equity=self._market_equity(markets),
                        opportunities=[p for p in opportunities if p['family'] in self.promoted][:30],
                        positions=list(self.state['positions'].values())))
                    if agent_choice:
                        action=agent_choice['action']
                        symbol=agent_choice.get('symbol')
                        side=agent_choice.get('positionSide')
                        if action in ('CLOSE_LONG','CLOSE_SHORT','REDUCE_LONG','REDUCE_SHORT'):
                            key=position_key(symbol,side)
                            if key in self.state['positions'] and symbol in markets:
                                portion=(agent_choice['desiredSize'] if action.startswith('REDUCE_') else 1.)
                                trade=self._close(symbol,side,markets[symbol],now,
                                    'EXTERNAL_'+action,portion)
                                decision=dict(action=action,symbol=symbol,
                                    reasonCodes=agent_choice.get('reasonCodes',[]),
                                    netPnL=trade['netPnL'])
                                provider='EXTERNAL'
                        elif action.startswith('KEEP_'):
                            if position_key(symbol,side) in self.state['positions']:
                                decision=dict(action=action,symbol=symbol,
                                    reasonCodes=agent_choice.get('reasonCodes',[]))
                                provider='EXTERNAL'
                        elif action.startswith('OPEN_') or action=='HEDGE':
                            chosen=next((p for p in opportunities if p['symbol']==symbol and
                                p['directionCandidate']==side and p['family'] in self.promoted),None)
                            permitted_hedge=self._hedge_candidate(opportunities,markets,now) if action=='HEDGE' else None
                            if action=='HEDGE' and (not permitted_hedge or
                                    symbol!=permitted_hedge['symbol'] or side!='SHORT' or
                                    agent_choice['desiredSize']>permitted_hedge['positionSize']*
                                        self._market_equity(markets) or
                                    agent_choice.get('hedgeRatio') is None or
                                    agent_choice['hedgeRatio']>permitted_hedge['hedgeRatio']):
                                chosen=None
                            if chosen and symbol in markets:
                                equity=self._market_equity(markets)
                                size=float(agent_choice['desiredSize'])
                                external_open=dict(action='OPEN_'+side,symbol=symbol,
                                    family=chosen['family'],modelId=chosen.get('modelId'),
                                    positionSize=size/equity if equity>0 else 0,
                                    stop=agent_choice['stop'],target=agent_choice['target'],
                                    expectedNetReturn=chosen['expectedNetEdge'],
                                    expectedMove=chosen['expectedMove'],
                                    fullRoundTripCost=chosen['fullRoundTripCost'],
                                    decisionTime=now,hedgeRatio=agent_choice.get('hedgeRatio')
                                    if action=='HEDGE' else None)
                                if size<=40 and self._entry_veto(external_open,
                                        markets[symbol],markets) is None:
                                    self._open(external_open,markets[symbol],markets,now)
                                    decision=dict(action=action,symbol=symbol,
                                        reasonCodes=agent_choice.get('reasonCodes',[]))
                                    provider='EXTERNAL'
                    hedge=self._hedge_candidate(opportunities,markets,now)
                    if decision['action']=='FLAT' and hedge and self._entry_veto(hedge,markets['BTCUSDT'],markets) is None:
                        self._open(hedge,markets['BTCUSDT'],markets,now)
                        decision=dict(**hedge)
                        decision['action']='HEDGE'
                    for p in opportunities if decision['action']=='FLAT' else []:
                        if p['symbol'] in conflict_symbols:continue
                        if p['family'] not in self.promoted:continue
                        side=p['directionCandidate']
                        sign=direction(side)
                        price=markets[p['symbol']]['ask' if side=='LONG' else 'bid']
                        distance=max(.005,min(.03,2*p['volatility']))
                        strength=p['expectedNetEdge']/max(p['fullRoundTripCost'],1e-9)
                        probability=p.get('probabilityNetProfit') or 0
                        size_tier=(.4 if strength>=3 and probability>=.75
                            else .3 if strength>=2 and probability>=.68
                            else .2 if strength>=1 and probability>=.6
                            else .1)
                        choice=dict(action='OPEN_'+side,symbol=p['symbol'],family=p['family'],
                            modelId=p.get('modelId'),
                            positionSize=min(size_tier,self.config['maxSymbolExposure'],
                                self.config['maxPositionRisk']/distance),
                            stop=price*(1-sign*distance),target=price*(1+sign*distance*2),
                            expectedNetReturn=p['expectedNetEdge'],expectedMove=p['expectedMove'],
                            fullRoundTripCost=p['fullRoundTripCost'],decisionTime=now)
                        veto=self._entry_veto(choice,markets[p['symbol']],markets)
                        if veto is None:
                            self._open(choice,markets[p['symbol']],markets,now)
                            decision=choice
                            break
                    if decision['action']=='FLAT' and self.state['positions']:
                        side=next(iter(self.state['positions'].values()))['positionSide']
                        decision=dict(action='KEEP_'+side,symbol=next(iter(self.state['positions'].values()))['symbol'],
                                      reasonCodes=['PROTECTIVE_STOP_ACTIVE'])
                    if exits and decision['action']=='FLAT':
                        trade=exits[0]
                        decision=dict(action='CLOSE_'+trade['positionSide'],symbol=trade['symbol'],
                                      reasonCodes=[trade['reason']])
                    self.state['cursor']=timestamp
                    self.state['peak']=max(self.state['peak'],self._market_equity(markets))
                    self.state['lastMarkets']={s:{k:m[k] for k in ('contractPrice','markPrice','indexPrice',
                        'bid','ask','fundingRate','nextFundingTime','openInterest','timestamp')}
                        for s,m in markets.items()}
                    evaluations={}
                    for symbol in markets:
                        long=max((p['expectedNetEdge'] for p in opportunities if p['symbol']==symbol
                                  and p['directionCandidate']=='LONG'),default=None)
                        short=max((p['expectedNetEdge'] for p in opportunities if p['symbol']==symbol
                                   and p['directionCandidate']=='SHORT'),default=None)
                        evaluations[symbol]=dict(EV_LONG=long,EV_SHORT=short,EV_FLAT=0,
                            P_LONG_PROFIT=max((p['probabilityNetProfit'] for p in opportunities if
                                p['symbol']==symbol and p['directionCandidate']=='LONG' and
                                p['probabilityNetProfit'] is not None),default=None),
                            P_SHORT_PROFIT=max((p['probabilityNetProfit'] for p in opportunities if
                                p['symbol']==symbol and p['directionCandidate']=='SHORT' and
                                p['probabilityNetProfit'] is not None),default=None),
                            calibrated=False,features=features[symbol])
                    record=dict(time=now,candleTime=timestamp,action=decision['action'],
                        symbol=decision.get('symbol'),reasonCodes=decision.get('reasonCodes',[]),
                        provider=provider,
                        decisionMid=((markets[decision['symbol']]['bid']+
                            markets[decision['symbol']]['ask'])/2)
                            if decision.get('symbol') in markets else None,
                        opportunities=len(opportunities),evaluations=evaluations,
                        promotedFamilies=sorted(self.promoted),paperOnly=True)
                    self.state['lastDecision']=record
                    self.db.execute('INSERT INTO futures_decisions VALUES(?,?)',
                                    (timestamp,json.dumps(record,allow_nan=False)))
                    self._persist()
                self.status='live'
                self.error=None
                return True
            except BaseException:
                self.state=old
                self.status='paused'
                raise

    def _position_view(self,p,market):
        mark=market['markPrice']
        side=p['positionSide']
        sign=direction(side)
        quote=quote_fill(market,'SELL' if side=='LONG' else 'BUY',self.config['slippage'])
        exit_fee=quote['fill']*p['qty']*self.config['takerFee']
        gross_mark=sign*(mark-p['entryPrice'])*p['qty']
        net=sign*(quote['fill']-p['entryPrice'])*p['qty']+p['fundingPnL']-p['entryFee']-exit_fee
        exit_cost=sign*(mark-quote['fill'])*p['qty']+exit_fee
        maintenance=p['qty']*mark*p['maintenanceMarginRate']
        margin_equity=p['initialMargin']+gross_mark+p['fundingPnL']-p['entryFee']
        margin_ratio=maintenance/margin_equity if margin_equity>0 else None
        if side=='LONG':
            liquidation=(p['qty']*p['entryPrice']-p['initialMargin']-p['fundingPnL']+
                p['entryFee'])/(p['qty']*(1-p['maintenanceMarginRate']))
        else:
            liquidation=(p['initialMargin']+p['qty']*p['entryPrice']+p['fundingPnL']-
                p['entryFee'])/(p['qty']*(1+p['maintenanceMarginRate']))
        return dict(**p,contractPrice=market['contractPrice'],markPrice=mark,
            indexPrice=market['indexPrice'],grossUnrealizedPnL=gross_mark,
            estimatedExitCost=exit_cost,netLiquidationPnL=net,
            maintenanceMargin=maintenance,marginRatio=margin_ratio,
            estimatedLiquidationPrice=max(0,liquidation),effectiveLeverage=1.)

    def snapshot(self):
        with self.lock:
            markets=self.state['lastMarkets']
            positions={key:self._position_view(p,markets.get(p['symbol'],dict(
                markPrice=p['lastMarkPrice'],contractPrice=p['lastMarkPrice'],
                indexPrice=p['lastMarkPrice'],bid=p['lastMarkPrice']*.999,
                ask=p['lastMarkPrice']*1.001))) for key,p in self.state['positions'].items()}
            gross=sum(p['qty']*p['markPrice'] for p in positions.values())
            long=sum(p['qty']*p['markPrice'] for p in positions.values() if p['positionSide']=='LONG')
            short=gross-long
            gross_unrealized=sum(p['grossUnrealizedPnL'] for p in positions.values())
            net_liquidation=sum(p['netLiquidationPnL'] for p in positions.values())
            trades=[json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM futures_trades ORDER BY id DESC LIMIT 50')]
            events=[json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM futures_events ORDER BY id DESC LIMIT 50')]
            outcomes=[json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM futures_outcomes ORDER BY decisionTime DESC,horizon DESC LIMIT 50')]
            complete_outcomes=[json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM futures_outcomes WHERE horizon=30')]
            verdicts=('CAPTURED_PROFIT','AVOIDED_LOSS','PREMATURE_CLOSE_LOSS','BAD_HOLD_LOSS')
            outcome_metrics={name:sum(row['verdict']==name for row in complete_outcomes)
                             for name in verdicts}
            outcome_metrics.update(evaluated30m=len(complete_outcomes),
                meanCounterfactualNetReturn=(statistics.fmean(
                    row['counterfactualNetReturn'] for row in complete_outcomes)
                    if complete_outcomes else None),
                unit='decision counts and fractional return; not realized USDT')
            equity=self.state['wallet']+gross_unrealized
            return dict(mode='BINANCE_USDS_M_FUTURES_PAPER',paperOnly=True,liveReady=False,
                status=self.status,error=self.error,cursor=self.state['cursor'],
                pausedAll=self.state['pausedAll'],
                noConfirmedEdge=not self.promoted,config=self.config,
                wallet=self.state['wallet'],equity=equity,
                netLiquidationEquity=self.config['startingCapital']+self.state['realizedPnL']+net_liquidation,
                longExposure=long,shortExposure=short,netExposure=long-short,grossExposure=gross,
                effectiveLeverage=gross/equity if equity>0 else None,
                fundingPnL=self.state['fundingPnL'],fees=self.state['fees'],
                spreadCost=self.state['spreadCost'],slippageCost=self.state['slippageCost'],
                realizedPnL=self.state['realizedPnL'],grossUnrealizedPnL=gross_unrealized,
                netLiquidationPnL=net_liquidation,positions=positions,
                trades=trades,events=events,lastDecision=self.state['lastDecision'],
                outcomes=outcomes,outcomeMetrics=outcome_metrics,
                marketPrices=copy.deepcopy(markets),promotedFamilies=sorted(self.promoted),
                executionModel='TAKER_BID_ASK_ONLY',makerFillsEnabled=False,
                liquidationEstimateOnly=True)

    def close(self):
        self.db.close()
