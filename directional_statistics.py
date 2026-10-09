"""Deterministic, explicitly empirical PAPER statistics; no Gaussian assumptions."""
import random
import statistics as stats
from collections import defaultdict


HORIZONS = (100, 250, 500, 1000, 2500, 5000)


def quantile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    x = (len(ordered) - 1) * p
    lo = int(x)
    return ordered[lo] * (1 - (x - lo)) + ordered[min(lo + 1, len(ordered) - 1)] * (x - lo)


def drawdown(sequence, capital=100):
    equity = peak = capital
    result = 0.0
    for value in sequence:
        equity += value
        peak = max(peak, equity)
        result = max(result, (peak - equity) / peak if peak > 0 else 1.0)
    return result


def expectancy(trades, minimum=100, iterations=400):
    values = [float(t['netPnL']) for t in trades]
    n = len(values)
    if not n:
        return dict(trades=0, netExpectancy=None, expectancyLower=None, expectancyMedian=None,
                    expectancyUpper=None, status='INSUFFICIENT_SAMPLE', profitFactor=None,
                    winRate=None, maxDrawdown=None)
    if n>=minimum:
        rng = random.Random(917 + n + round(sum(values) * 100000))
        draws = [stats.mean(rng.choices(values, k=n)) for _ in range(iterations)]
        lower, median, upper = (quantile(draws, p) for p in (.025, .5, .975))
    else:lower=median=upper=None
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    status = ('INSUFFICIENT_SAMPLE' if n < minimum else
              'REJECTED_EDGE' if upper <= 0 else
              'CONFIRMED_POSITIVE_EDGE' if lower > 0 else 'UNCERTAIN_EDGE')
    return dict(trades=n, netExpectancy=stats.mean(values), expectancyLower=lower,
                expectancyMedian=median, expectancyUpper=upper, status=status,
                profitFactor=wins / losses if losses else None,
                winRate=sum(v > 0 for v in values) / n, maxDrawdown=drawdown(values))


def monte_carlo(trades, equity, start=100, minimum=100, iterations=400):
    values = [float(t['netPnL']) for t in trades]
    if len(values) < minimum:
        return dict(status='INSUFFICIENT_SAMPLE', horizons={}, estimatedTradesToPositive={str(p):None for p in (.8,.9,.95)},
                    estimatedTradesToRecover=None, recoveryProbability={})
    rng = random.Random(202603 + len(values) + round(sum(values) * 100000))
    samples={n:dict(finals=[],dds=[],streaks=[],ruins=[]) for n in HORIZONS}
    above_start=[0]*(max(HORIZONS)+1)
    for _ in range(iterations):
        balance=high=equity
        maxdd=losing=longest=0
        ruined=False
        for count in range(1,max(HORIZONS)+1):
            value=rng.choice(values)
            balance+=value
            ruined |= balance<=0
            high=max(high,balance)
            maxdd=max(maxdd,(high-balance)/high if high>0 else 1)
            losing=losing+1 if value<0 else 0
            longest=max(longest,losing)
            above_start[count]+=balance>start
            if count in samples:
                slot=samples[count]
                slot['finals'].append(balance);slot['dds'].append(maxdd)
                slot['streaks'].append(longest);slot['ruins'].append(ruined)
    results={}
    for count,slot in samples.items():
        finals=slot['finals']
        results[str(count)] = dict(medianFinalEquity=quantile(finals,.5), p5Equity=quantile(finals,.05),
            p25Equity=quantile(finals,.25), p75Equity=quantile(finals,.75), p95Equity=quantile(finals,.95),
            probabilityAboveCurrent=sum(x > equity for x in finals)/iterations,
            probabilityAboveStarting=sum(x > start for x in finals)/iterations,
            expectedMaxDrawdown=stats.mean(slot['dds']), probabilityOfRuin=sum(slot['ruins'])/iterations,
            longestLosingStreak=quantile(slot['streaks'],.95))
    positive = stats.mean(values) > 0
    # Require confidence to hold through all later tested N, avoiding an
    # isolated Monte Carlo fluctuation being reported as the first crossing.
    stable=[0.]*(max(HORIZONS)+2)
    floor=1.
    for n in range(max(HORIZONS),0,-1):
        floor=min(floor,above_start[n]/iterations)
        stable[n]=floor
    targets={str(p):(0 if equity>start else next((n for n in range(1,max(HORIZONS)+1) if stable[n]>=p),None))
             if positive else None for p in (.8,.9,.95)}
    return dict(status='ESTIMATE' if positive else 'NO_STATISTICAL_EDGE', horizons=results,
                estimatedTradesToPositive=targets,
                estimatedTradesToRecover=(start-equity)/stats.mean(values) if positive and equity < start else None,
                recoveryProbability={str(n):results[str(n)]['probabilityAboveStarting'] for n in (100,250,500,1000)} if equity < start else {})


def groups(trades, key, minimum=100, iterations=400):
    buckets = defaultdict(list)
    for trade in trades:
        buckets[str(trade.get(key) or 'UNKNOWN')].append(trade)
    return {name:expectancy(rows,minimum,iterations) for name,rows in buckets.items()}


def cost_stress(trades):
    return {str(factor):stats.mean(t['netPnL']-t['totalCost']*(factor-1) for t in trades)
            if trades else None for factor in (1,1.25,1.5,2)}


def prediction_quality(pairs):
    """pairs: chronological (prediction, label) tuples; labels are future closed candles."""
    if not pairs:
        return dict(samples=0,directionAccuracy=None,longPrecision=None,shortPrecision=None,
                    longRecall=None,shortRecall=None,brierScore=None,confusion={},calibration={})
    confusion=defaultdict(int)
    calibration=defaultdict(lambda:dict(count=0,predicted=0.,actual=0.))
    brier=[]
    for p,label in pairs:
        actual=label['label']
        predicted=max(('UP','DOWN','FLAT'),key=lambda name:p['p'+name.title()])
        confusion[predicted+'_'+actual]+=1
        for name in ('UP','DOWN','FLAT'):
            probability=p['p'+name.title()]
            brier.append((probability-(actual==name))**2)
            bucket=f'{name}_{min(9,int(probability*10))*10}-{min(9,int(probability*10))*10+10}'
            row=calibration[bucket]
            row['count']+=1;row['predicted']+=probability;row['actual']+=int(actual==name)
    def precision(name):
        hits=confusion[name+'_'+name]
        pred=sum(v for key,v in confusion.items() if key.startswith(name+'_'))
        return hits/pred if pred else None
    def recall(name):
        hits=confusion[name+'_'+name]
        actual=sum(v for key,v in confusion.items() if key.endswith('_'+name))
        return hits/actual if actual else None
    return dict(samples=len(pairs),directionAccuracy=sum(confusion[x+'_'+x] for x in ('UP','DOWN','FLAT'))/len(pairs),
                longPrecision=precision('UP'),shortPrecision=precision('DOWN'),
                longRecall=recall('UP'),shortRecall=recall('DOWN'),
                brierScore=stats.mean(brier),confusion=dict(confusion),
                calibration={key:dict(count=row['count'],predicted=row['predicted']/row['count'],
                                  actual=row['actual']/row['count']) for key,row in calibration.items()})


def walk_forward(pairs, minimum=100):
    """Chronological diagnostic only: no parameter fitting or OOS promotion."""
    ordered=sorted(pairs,key=lambda pair:pair[0]['time'])
    n=len(ordered)
    a=int(.6*n);b=int(.8*n)
    return dict(ready=False,diagnosticPartitionsReady=n>=minimum and b-a>=40 and n-b>=40,
        train=prediction_quality(ordered[:a]),validation=prediction_quality(ordered[a:b]),
        oos=prediction_quality(ordered[b:]),
        note='Chronological holdout diagnostics; model parameters are frozen, no OOS model selection')


def bootstrap_robustness(trades, iterations=400):
    if len(trades)<2:return dict(status='INSUFFICIENT_SAMPLE')
    values=[float(t['netPnL']) for t in trades]
    rng=random.Random(7041+len(values))
    metrics=defaultdict(list)
    for _ in range(iterations):
        sample=rng.choices(values,k=len(values))
        wins=sum(x for x in sample if x>0)
        losses=-sum(x for x in sample if x<0)
        metrics['netExpectancy'].append(stats.mean(sample))
        if losses>0:metrics['profitFactor'].append(wins/losses)
        metrics['maxDrawdown'].append(drawdown(sample))
    return {name:dict(lower=quantile(rows,.025),median=quantile(rows,.5),upper=quantile(rows,.975))
            for name,rows in metrics.items()}


def sequence_stress(trades, equity=100, iterations=400):
    if len(trades)<2:return dict(status='INSUFFICIENT_SAMPLE')
    values=[float(t['netPnL']) for t in trades]
    rng=random.Random(8051+len(values))
    dds=[];streaks=[];new_high=0
    for _ in range(iterations):
        sample=rng.sample(values,len(values))
        dds.append(drawdown(sample,equity))
        longest=streak=0
        for value in sample:
            streak=streak+1 if value<0 else 0
            longest=max(longest,streak)
        streaks.append(longest)
        new_high+=sum(sample)>0
    return dict(worstP95Drawdown=quantile(dds,.95),losingStreakP95=quantile(streaks,.95),
                probabilityEndingBelowStart=float(sum(values)<0),probabilityNewEquityHigh=new_high/iterations,
                note='Sequence permutation preserves total PnL; profit probability is not a forecast')


def concentration(trades):
    positive=[t for t in trades if t['netPnL']>0]
    total=sum(t['netPnL'] for t in positive)
    result={}
    for name,key in [('symbol','symbol'),('regime','regime'),('direction','side')]:
        amounts=defaultdict(float)
        for trade in positive:amounts[str(trade.get(key) or 'UNKNOWN')]+=trade['netPnL']
        result[name]=max(amounts.values())/total if total else None
    result['highConcentration']=any(v is not None and v>=.9 for v in result.values())
    return result
