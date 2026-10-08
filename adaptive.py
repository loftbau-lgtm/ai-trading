"""Deterministic closed-candle model. Percentages are percent, risk/fees are fractions.

No network access and no exchange order submission in this module.
"""
import hashlib
import json
import math
import statistics as stats
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from engine import ema

NAME = 'Activity-Filtered Adaptive Mean Reversion'
WINDOWS = {'15m': 15, '1h': 60, '4h': 240, '24h': 1440}


def load_config(path=None):
    config = json.loads(Path(path or Path(__file__).with_name('adaptive_config.json')).read_text())
    for key, value in config.items():
        if key != 'QUOTE' and (not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('Invalid config: ' + key)
    if config['QUOTE'] != 'USDT':
        raise ValueError('This portfolio requires USDT; do not mix quote units')
    if not .002 <= config['RISK_PER_TRADE'] <= .005:
        raise ValueError('Risk must be 0.20–0.50%')
    for key in ('MAX_SYMBOL_EXPOSURE','MAX_TOTAL_EXPOSURE','MAX_CORRELATED_EXPOSURE','DAILY_LOSS_LIMIT','MAX_DRAWDOWN'):
        if not 0 < config[key] <= 1: raise ValueError(key)
    for key in ('TOP_N','MAX_OPEN_POSITIONS','MAX_HOLD_MINUTES','ORDER_TTL_MINUTES','FETCH_WORKERS','MIN_PAPER_TRADES'):
        if config[key] < 1 or int(config[key]) != config[key]: raise ValueError(key)
    for key in ('MAX_SPREAD','ATR_STOP_MULTIPLIER','MIN_STOP_PCT','STARTING_CAPITAL','MAX_DATA_AGE_MS','MAX_CLOCK_SKEW_MS','MIN_EDGE_MULTIPLIER'):
        if config[key] <= 0: raise ValueError(key)
    for key in ('MAKER_FEE','TAKER_FEE','SLIPPAGE'):
        if not 0 <= config[key] < .05: raise ValueError(key)
    return config


def config_hash(config):
    # Collector concurrency is operational, not a strategy parameter.
    return hashlib.sha256(json.dumps({k:v for k,v in config.items() if k != 'FETCH_WORKERS'}, sort_keys=True).encode()).hexdigest()


def validate_bars(bars, now):
    for i, b in enumerate(bars):
        if b['time'] % 60000 or b['end'] != b['time'] + 59999 or b['end'] >= now:
            raise ValueError('Unclosed or misaligned candle')
        if i and b['time'] != bars[i-1]['time'] + 60000: raise ValueError('Candle gap')
        if any(not math.isfinite(b[k]) or b[k] <= 0 for k in ('open','high','low','close')):
            raise ValueError('Invalid OHLC')
        if not b['low'] <= min(b['open'],b['close']) <= max(b['open'],b['close']) <= b['high']:
            raise ValueError('Inconsistent OHLC')
        if any(not math.isfinite(b[k]) or b[k] < 0 for k in ('volume','turnover','trades')):
            raise ValueError('Invalid volume')


def percentile(value, values):
    values = sorted(values)
    return .5 if len(values) < 2 else (bisect_left(values,value)+bisect_right(values,value)-1)/(2*(len(values)-1))


def zscore(value, values):
    sd = stats.pstdev(values)
    return (value-stats.mean(values))/sd if sd > 0 else 0.0


def resample_closed(bars, minutes):
    groups = defaultdict(list)
    for b in bars: groups[b['time']//(minutes*60000)].append(b)
    result = []
    for bucket, rows in sorted(groups.items()):
        if len(rows) != minutes or rows[0]['time'] != bucket*minutes*60000: continue
        result.append(dict(time=rows[0]['time'],end=rows[-1]['end'],open=rows[0]['open'],close=rows[-1]['close'],
                           high=max(b['high'] for b in rows),low=min(b['low'] for b in rows),
                           volume=sum(b['volume'] for b in rows),turnover=sum(b['turnover'] for b in rows),trades=sum(b['trades'] for b in rows)))
    return result


def features(bars):
    if len(bars) < 100: raise ValueError('Insufficient warmup')
    # Fixed 100-bar indicator lookback guarantees replay independent of cache length.
    bars = bars[-100:]
    prices = [b['close'] for b in bars]
    returns = [(b/a-1)*100 for a,b in zip(prices, prices[1:])]
    volumes = [b['volume'] for b in bars[-20:]]
    volume = sum(volumes)
    vwap = sum((b['high']+b['low']+b['close'])/3*b['volume'] for b in bars[-20:])/volume if volume else prices[-1]
    atr = stats.mean(max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close'])) for a,b in zip(bars[-15:-1],bars[-14:]))
    result = {f'return{n}m':(prices[-1]/prices[-1-n]-1)*100 for n in (1,3,5,15)}
    result.update(meanReturn20=stats.mean(returns[-20:]),stdReturn20=stats.pstdev(returns[-20:]),
                  zReturn=zscore(returns[-1],returns[-20:]),priceZ=zscore(prices[-1],prices[-20:]),
                  volumeZ=zscore(volumes[-1],volumes),volatility=stats.pstdev(returns[-20:]),
                  VWAP=vwap,distanceFromVWAP=(prices[-1]/vwap-1)*100,
                  SMA20=stats.mean(prices[-20:]),EMA20=ema(prices,20),EMA50=ema(prices,50),ATR=atr,
                  close=prices[-1])
    result['trendStrength'] = abs(result['EMA20']-result['EMA50'])/atr if atr else 0
    result['timeframes'] = {str(n)+'m':resample_closed(bars,n)[-1:] for n in (3,5,15)}
    return result


def window_metrics(bars, spread):
    if len(bars) < 1441: raise ValueError('24h warmup incomplete')
    result = {}
    for name, n in WINDOWS.items():
        rows = bars[-n:]
        prices = [b['close'] for b in bars[-n-1:]]
        result[name] = {'rangePct':(max(b['high'] for b in rows)-min(b['low'] for b in rows))/rows[0]['open']*100,
                        'turnover':sum(b['turnover'] for b in rows), 'trades':sum(b['trades'] for b in rows),
                        'volatility':stats.pstdev([(b/a-1)*100 for a,b in zip(prices,prices[1:])]),
                        'spreadPct':spread, 'spreadSource':'current snapshot; historical spread unavailable'}
    return result


def rank_universe(rows):
    """Input includes closed-candle windows. No mixed currency volume comparisons."""
    groups = defaultdict(list)
    for row in rows: groups[row['quote']].append(row)
    for group in groups.values():
        for row in group:
            spread = row['spreadPct']
            row['activityScore'] = 0
            for name, weight in zip(WINDOWS, (.4,.3,.2,.1)):
                win = row['windows'][name]
                score = sum(w*percentile(win[key],[r['windows'][name][key] for r in group])
                            for key,w in (('rangePct',.45),('turnover',.35),('trades',.2)))
                win['score'] = score/(1+spread/.1)*100 if spread is not None and spread > 0 else 0
                row['activityScore'] += weight*win['score']
            day = row['windows']['24h']
            row.update(rangeToSpread=day['rangePct']/spread if spread and spread > 0 else 0,
                       avgTradeNotional=day['turnover']/day['trades'] if day['trades'] else 0,
                       tradeIntensity=day['trades']/1440)
        for row in group:
            row['activityPercentile'] = 100*percentile(row['activityScore'],[r['activityScore'] for r in group])
    return sorted(rows,key=lambda r:(-r['activityScore'],r['symbol']))


def entry_decision(f, market, config, equity, drawdown):
    c = config
    spread = market['spreadPct']
    result = dict(signal='HOLD',decision='REJECT_NO_DEVIATION',positionSize=0,entry=None,exit=None,
                  expectedCost=None,expectedEdge=None,grossPnL=0,netPnL=0,**f,
                  activityScore=market['activityScore'],activityPercentile=market['activityPercentile'],
                  spread=spread,rangeToSpread=market['rangeToSpread'])
    def reject(reason):
        result['decision'] = reason
        return result
    if not market.get('top'): return reject('REJECT_OUTSIDE_TOP_N')
    if market['activityPercentile'] < c['MIN_ACTIVITY_PERCENTILE']: return reject('REJECT_LOW_ACTIVITY')
    if spread is None or not 0 < spread <= c['MAX_SPREAD']: return reject('REJECT_SPREAD')
    if market['rangeToSpread'] < c['MIN_RANGE_TO_SPREAD']: return reject('REJECT_RANGE_TO_SPREAD')
    if market['windows']['24h']['turnover'] < c['MIN_TURNOVER']: return reject('REJECT_TURNOVER')
    if market['tradeIntensity'] < c['MIN_TRADE_INTENSITY']: return reject('REJECT_TRADE_INTENSITY')
    if f['zReturn'] > c['Z_RETURN_ENTRY'] and f['priceZ'] > c['PRICE_Z_ENTRY']: return result
    result['signal'] = 'LONG_CANDIDATE'
    if f['volumeZ'] < c['MIN_VOLUME_Z']: return reject('REJECT_VOLUME')
    if f['ATR'] <= 0 or f['trendStrength'] > c['TREND_THRESHOLD'] or (f['EMA20'] < f['EMA50'] and f['return5m'] < c['DOWN_MOMENTUM_PCT']):
        return reject('REJECT_TREND')
    # A LONG must be BELOW equilibrium, not merely far away on either side.
    if f['distanceFromVWAP'] > -c['MIN_VWAP_DISTANCE_PCT']: return reject('REJECT_VWAP_DISTANCE')
    price = f['close']*(1-spread/200)  # Passive bid proxy; frozen until expiry.
    target = min(f['VWAP'],f['SMA20'])
    cost = (c['MAKER_FEE']+c['TAKER_FEE']+2*c['SLIPPAGE'])*100+spread
    edge = (target-price)/price*100
    result.update(expectedCost=cost,expectedEdge=edge,entry=price,target=target)
    if edge < cost*c['MIN_EDGE_MULTIPLIER']: return reject('REJECT_LOW_EDGE')
    distance = max(c['ATR_STOP_MULTIPLIER']*f['ATR'], price*c['MIN_STOP_PCT']/100)
    adjustment = min(1,market['activityPercentile']/100)/(1+spread/c['MAX_SPREAD'])
    adjustment /= 1+f['volatility']/max(c['MIN_STOP_PCT'],.001)
    adjustment *= max(0,1-drawdown/c['MAX_DRAWDOWN'])
    # Risk includes estimated stop execution costs as well as price distance.
    qty = equity*c['RISK_PER_TRADE']*adjustment/(distance+price*cost/100)
    qty = min(qty,equity*c['MAX_SYMBOL_EXPOSURE']/price)
    result.update(signal='BUY',decision='PLACE_LIMIT_MAKER',positionSize=qty,stop=price-distance)
    return result


def correlation(a,b):
    if len(a) != len(b) or len(a) < 20: return 1.0  # Fail conservative.
    try: return stats.correlation(a,b)
    except stats.StatisticsError: return 1.0


def report(trades, equity_points, config):
    pnl = [t['netPnL'] for t in trades]
    wins, losses = [p for p in pnl if p > 0],[-p for p in pnl if p < 0]
    avg_win = stats.mean(wins) if wins else 0
    avg_loss = stats.mean(losses) if losses else 0
    n = len(pnl)
    expectancy = (len(wins)*avg_win-len(losses)*avg_loss)/n if n else None
    peak, maxdd = config['STARTING_CAPITAL'],0
    for point in equity_points:
        peak = max(peak,point['equity'])
        maxdd = max(maxdd,1-point['equity']/peak)
    # UTC daily close returns; do not annualize irregular event returns.
    days = {}
    for point in equity_points: days[point['time']//86400000] = point['equity']
    values = list(days.values())
    returns = [b/a-1 for a,b in zip(values,values[1:]) if a > 0]
    sd = stats.pstdev(returns) if len(returns) > 1 else 0
    downside = math.sqrt(stats.mean(min(r,0)**2 for r in returns)) if returns else 0
    groups = {}
    for key in ('symbol','activityDecile','zBucket','volatilityRegime'):
        sums = defaultdict(float)
        for t in trades: sums[str(t[key])] += t['netPnL']
        groups[key] = dict(sums)
    hours = (equity_points[-1]['time']-equity_points[0]['time'])/3600000 if len(equity_points) > 1 else 0
    return {'trades':n,'netPnL':sum(pnl),'grossPnL':sum(t['grossPnL'] for t in trades),
            'fees':sum(t['fees'] for t in trades),'spreadCosts':sum(t['spreadCost'] for t in trades),
            'slippageCosts':sum(t['slippageCost'] for t in trades),'expectancyNet':expectancy,
            'profitFactor':sum(wins)/sum(losses) if losses else None,
            'winRate':len(wins)/n*100 if n else None,'averageWin':avg_win,'averageLoss':avg_loss,
            'maxDrawdown':maxdd*100,'sharpe':stats.mean(returns)/sd*math.sqrt(365) if sd else None,
            'sortino':stats.mean(returns)/downside*math.sqrt(365) if downside else None,
            'tradesPerHour':n/hours if hours else None,
            'averageHoldingMinutes':stats.mean(t['holdingMinutes'] for t in trades) if n else None,
            'averageCost':stats.mean(t['fees']+t['spreadCost']+t['slippageCost'] for t in trades) if n else None,
            'averageEdgeBeforePct':stats.mean(t['expectedEdge'] for t in trades) if n else None,
            'averageEdgeAfterPct':stats.mean(t['expectedEdge']-t['expectedCost'] for t in trades) if n else None,
            'breakdown':groups,'paperGatePassed':bool(n >= config['MIN_PAPER_TRADES'] and expectancy > 0 and maxdd < config['MAX_DRAWDOWN']),
            'liveReady':False,'liveBlockers':['Live adapter not integrated/audited','Walk-forward out-of-sample evidence required']}
