"""Closed-candle, deterministic directional estimates. Probabilities are uncalibrated priors."""
import math
import statistics as stats


def clamp(x, low=-1.0, high=1.0):
    return max(low, min(high, x))


def returns(bars, n):
    return bars[-1]['close'] / bars[-1-n]['close'] - 1 if len(bars) > n else 0.0


def beta(bars, btc):
    if len(bars) < 62 or len(btc) < 62:
        return None
    a = [y['close']/x['close']-1 for x,y in zip(bars[-61:-1],bars[-60:])]
    b = [y['close']/x['close']-1 for x,y in zip(btc[-61:-1],btc[-60:])]
    variance = stats.variance(b)
    return stats.covariance(a,b)/variance if variance > 1e-12 else None


def context(histories):
    usable = {s:b for s,b in histories.items() if len(b) >= 61}
    if not usable:
        return dict(breadth=0, count=0, strengths={}, up5=0, down5=0, aboveVwap=0, breakHigh=0, breakLow=0)
    strengths = {s:{str(n):returns(b,n) for n in (5,15,60)} for s,b in usable.items()}
    n = len(usable)
    up = sum(returns(b,5)>0 for b in usable.values())/n
    down = sum(returns(b,5)<0 for b in usable.values())/n
    vwap = sum(b[-1]['close'] > sum((x['high']+x['low']+x['close'])/3*x['volume'] for x in b[-20:]) /
               max(sum(x['volume'] for x in b[-20:]),1e-12) for b in usable.values())/n
    high = sum(b[-1]['close'] > max(x['high'] for x in b[-21:-1]) for b in usable.values())/n
    low = sum(b[-1]['close'] < min(x['low'] for x in b[-21:-1]) for b in usable.values())/n
    return dict(breadth=up-down, count=n, strengths=strengths, up5=up, down5=down,
                aboveVwap=vwap, breakHigh=high, breakLow=low)


def evaluate(symbol, bars, btc_bars, market, micro, breadth, config, now):
    if len(bars) < 100 or len(btc_bars) < 100:
        return None
    b=bars[-100:]
    close=b[-1]['close']
    tr=[max(x['high']-x['low'],abs(x['high']-p['close']),abs(x['low']-p['close'])) for p,x in zip(b[-15:-1],b[-14:])]
    atr=stats.mean(tr)
    if atr<=0:
        return None
    atr_pct=100*atr/close
    ema20=close
    ema50=close
    for x in b:
        ema20 += (x['close']-ema20)*2/21
        ema50 += (x['close']-ema50)*2/51
    ema_prev=ema20
    # Explicit previous closed-bar EMA, without future observations.
    ema_prev=b[0]['close']
    for x in b[:-1]: ema_prev += (x['close']-ema_prev)*2/21
    slope=(ema20-ema_prev)/max(atr,1e-12)
    trend=(ema20-ema50)/atr
    recent=[x['close']/p['close']-1 for p,x in zip(b[-61:-1],b[-60:])]
    volatility=stats.pstdev(recent)*100
    range_expansion=(b[-1]['high']-b[-1]['low'])/max(stats.mean(x['high']-x['low'] for x in b[-21:-1]),1e-12)
    breakout=(close-max(x['high'] for x in b[-21:-1]))/atr if close>max(x['high'] for x in b[-21:-1]) else \
             (close-min(x['low'] for x in b[-21:-1]))/atr if close<min(x['low'] for x in b[-21:-1]) else 0
    btc5=returns(btc_bars,5)
    btc15=returns(btc_bars,15)
    btc30=returns(btc_bars,30)
    rel5=returns(b,5)-btc5
    rel15=returns(b,15)-btc15
    rel60=returns(b,60)-returns(btc_bars,60)
    flow=micro.get('tradeFlowImbalance')
    book=micro.get('bookImbalance')
    liquidity=min(float(micro.get('bidQty') or 0)*float(micro.get('bid') or 0),float(micro.get('askQty') or 0)*float(micro.get('ask') or 0))
    volume=sum(x['volume'] for x in b[-20:])
    vwap=sum((x['high']+x['low']+x['close'])/3*x['volume'] for x in b[-20:])/volume if volume else close
    pressure=(.35*clamp(trend/3)+.15*clamp(btc15/max(atr/close*math.sqrt(15),.0001))+
              .15*clamp(breadth['breadth'])+.1*clamp(breakout)+
              .1*clamp(flow or 0)+.05*clamp(book or 0)+
              .1*clamp(breadth['breakHigh']-breadth['breakLow']))
    regime='SHOCK' if range_expansion>=3 or abs(btc5)>=.025 or volatility>=1.5 else \
           'TREND_UP' if trend>=.8 and returns(b,15)>0 and pressure>.1 else \
           'TREND_DOWN' if trend<=-.8 and returns(b,15)<0 and pressure<-.1 else 'RANGE'
    if market and market.get('activityPercentile') is not None:
        activity_decile=min(10,int(market['activityPercentile']//10)+1)
    else: activity_decile=None
    spread=micro.get('spreadPct')
    cost_pct=100*(config['makerFee']+config['takerFee']+config['slippage'])+(spread or 0)/2
    funding_not_modelled=config['fundingRate'] is None
    if config['fundingRate'] is not None:cost_pct += abs(config['fundingRate'])*100
    weights=config['weights']
    horizon_results={}
    for horizon in (5,15,30):
        base={5:returns(b,1)*.2+returns(b,5)*.8,15:returns(b,5)*.35+returns(b,15)*.65,
              30:returns(b,15)*.45+returns(b,30)*.55}[horizon]
        scale=max(atr/close*math.sqrt(horizon),.0001)
        components=dict(momentum=clamp(base/scale),emaSlope=clamp(slope),trend=clamp(trend/3),
                        breakout=clamp(breakout),flow=clamp(flow or 0),book=clamp(book or 0),
                        btc=clamp(({5:btc5,15:btc15,30:btc30}[horizon])/scale),
                        relativeStrength=clamp(({5:rel5,15:rel15,30:(rel15+rel60)/2}[horizon])/scale),
                        breadth=clamp(breadth['breadth']))
        score=sum(weights[k]*components[k] for k in weights)
        up=math.exp(2.2*score)
        down=math.exp(-2.2*score)
        flat=1.2
        total=up+down+flat
        pu,pd,pf=up/total,down/total,flat/total
        prior=max(atr_pct*math.sqrt(horizon)*.65,volatility*math.sqrt(horizon)*.6)
        past_moves=[(b[i]['close']/b[i-horizon]['close']-1)*100 for i in range(horizon,len(b))]
        historical_up=[v for v in past_moves if v>0]
        historical_down=[-v for v in past_moves if v<0]
        # Shrink noisy overlapping closed-candle samples toward an ATR prior.
        up_move=(20*prior+sum(historical_up))/(20+len(historical_up))
        down_move=(20*prior+sum(historical_down))/(20+len(historical_down))
        risk=atr_pct*.12+volatility*.05
        evlong=pu*up_move-pd*down_move-cost_pct-risk
        evshort=pd*down_move-pu*up_move-cost_pct-risk
        horizon_results[str(horizon)+'m']=dict(pUp=pu,pDown=pd,pFlat=pf,expectedUpMovePct=up_move,
            expectedDownMovePct=down_move,score=score,evLongPct=evlong,evShortPct=evshort,
            components=components)
    votes=[('LONG' if h['pUp']>h['pDown'] and h['pUp']>h['pFlat'] else
            'SHORT' if h['pDown']>h['pUp'] and h['pDown']>h['pFlat'] else 'FLAT') for h in horizon_results.values()]
    selected=horizon_results['15m']
    direction=max(('LONG','SHORT'),key=lambda side:selected['evLongPct' if side=='LONG' else 'evShortPct'])
    probability=selected['pUp' if direction=='LONG' else 'pDown']
    ev=selected['evLongPct' if direction=='LONG' else 'evShortPct']
    ratio=selected['expectedUpMovePct' if direction=='LONG' else 'expectedDownMovePct']/cost_pct if cost_pct>0 else None
    consensus=votes.count(direction)>=2
    micro_reasons=micro.get('diagnosticRejections') or []
    reason=('INSUFFICIENT_CROSS_SECTION' if breadth['count']<10 else
            'SHOCK' if regime=='SHOCK' else 'CONFLICTING_HORIZONS' if not consensus else
            'STALE_MICROSTRUCTURE' if micro_reasons else 'SPREAD' if spread is None or spread>config['maxSpreadPct'] else
            'LOW_CONFIDENCE' if probability<config['minDirectionProbability'] else
            'LOW_EDGE' if ev<=config['minRequiredEvPct'] or ratio is None or ratio<=config['minEdgeMultiplier'] else None)
    action=direction if reason is None else 'FLAT'
    return dict(symbol=symbol,time=b[-1]['time'],candleCloseTimestamp=b[-1]['end'],marketDataTimestamp=b[-1]['end'],
        receivedTimestamp=None,regime=regime,action=action,candidateDirection=direction,rejection=reason,
        horizons=horizon_results,pUp=selected['pUp'],pDown=selected['pDown'],pFlat=selected['pFlat'],
        expectedUpMovePct=selected['expectedUpMovePct'],expectedDownMovePct=selected['expectedDownMovePct'],
        evLongPct=selected['evLongPct'],evShortPct=selected['evShortPct'],evFlatPct=0,
        confidence=probability,expectedCostPct=cost_pct,edgeCostRatio=ratio,
        fundingNotModelled=funding_not_modelled,atr=atr,atrPct=atr_pct,volatility=volatility,
        volatilityRegime='HIGH' if volatility>.6 else 'MEDIUM' if volatility>.2 else 'LOW',
        emaSlope=slope,trendStrength=trend,regimePressure=pressure,rangeExpansion=range_expansion,breakout=breakout,
        return5m=returns(b,5),return15m=returns(b,15),return30m=returns(b,30),
        relativeStrength5m=rel5,relativeStrength15m=rel15,relativeStrength1h=rel60,
        btcReturn5m=btc5,btcReturn15m=btc15,btcReturn30m=btc30,btcBeta=beta(b,btc_bars),
        marketBreadth=breadth['breadth'],marketBreadthCount=breadth['count'],marketBreadthRegime='BULL' if breadth['breadth']>.2 else 'BEAR' if breadth['breadth']<-.2 else 'RANGE',
        activityDecile=activity_decile,spreadPct=spread,spreadRatio=micro.get('spreadRatio'),
        bookImbalance=book,tradeFlowImbalance=flow,aggressiveBuyVolume=(micro.get('flow') or {}).get('buyAggressiveVolume'),
        aggressiveSellVolume=(micro.get('flow') or {}).get('sellAggressiveVolume'),liquidity=liquidity,
        bid=micro.get('bid'),ask=micro.get('ask'),vwap=vwap,price=close,
        dataLatencyMs=None,microRejections=micro_reasons)
