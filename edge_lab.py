"""Read-only PAPER candidate ranking; historical results are diagnostic, never OOS."""
import json
from collections import defaultdict

from directional_statistics import expectancy
from engine import SLIPPAGE, STRATEGIES


MIN_DIAGNOSTIC_SAMPLE = 100


def base_round_trips(rows):
    open_entries = {}
    completed = defaultdict(list)
    for row in rows:
        row = dict(row)
        key = (row['strategy'], row['symbol'])
        if row['side'] == 'BUY':
            open_entries[key] = row
        elif row['side'] == 'SELL' and key in open_entries:
            entry = open_entries.pop(key)
            qty = float(row['qty'])
            buy_reference = float(entry['price']) / (1 + SLIPPAGE)
            sell_reference = float(row['price']) / (1 - SLIPPAGE)
            fees = float(entry['fee']) + float(row['fee'])
            slip = qty * (buy_reference + sell_reference) * SLIPPAGE
            gross = (sell_reference - buy_reference) * qty
            completed[row['strategy']].append(dict(symbol=row['symbol'], entryTime=entry['time'],
                exitTime=row['time'], grossPnL=gross, fees=fees, spreadCost=None,
                slippageCost=slip, fundingCost=0.0, netPnL=float(row['pnl']),
                costModelComplete=False))
    return completed


def _normalise(row):
    return dict(symbol=row.get('symbol'), entryTime=row.get('entryTime'),
        exitTime=row.get('exitTime', row.get('time')),
        grossPnL=float(row.get('grossPnL', 0)), fees=float(row.get('fees', 0)),
        spreadCost=None if row.get('spreadCost') is None else float(row['spreadCost']),
        slippageCost=float(row.get('slippageCost', 0)),
        fundingCost=float(row.get('fundingCost', 0)), netPnL=float(row['netPnL']),
        costModelComplete=row.get('costModelComplete', True) and not row.get('fundingNotModelled', False))


def promotion_gate(evidence, minimum=MIN_DIAGNOSTIC_SAMPLE):
    """Only independent OOS evidence may authorize active PAPER capital."""
    required = ('independentOos', 'walkForwardValidated', 'shadowValidated', 'costModelComplete',
        'oosTrades', 'oosNetExpectancy', 'oosCiLow', 'oosProfitFactor',
        'oosMaxDrawdown', 'costStress25', 'fillStress75', 'symbolConcentration',
        'periodConcentration', 'symbolCount', 'periodCount', 'selectionBiasRisk')
    if not evidence or any(evidence.get(key) is None for key in required):
        return False
    return (all(evidence[key] is True for key in ('independentOos', 'walkForwardValidated',
        'shadowValidated', 'costModelComplete'))
        and evidence['oosTrades'] >= minimum and evidence['oosNetExpectancy'] > 0
        and evidence['oosCiLow'] > 0 and evidence['oosProfitFactor'] > 1
        and evidence['oosMaxDrawdown'] <= .2 and evidence['costStress25'] > 0
        and evidence['fillStress75'] > 0 and evidence['symbolConcentration'] < .5
        and evidence['periodConcentration'] < .5 and evidence['symbolCount'] >= 3
        and evidence['periodCount'] >= 3 and evidence['selectionBiasRisk'] == 'LOW')


def candidate(name, family, trades, direction, regime='ALL', selection_bias='UNASSESSED',
              evidence=None, minimum=MIN_DIAGNOSTIC_SAMPLE):
    rows = [_normalise(row) for row in trades]
    n = len(rows)
    gross = sum(row['grossPnL'] for row in rows)
    costs = sum(row['fees'] + (row['spreadCost'] or 0) + row['slippageCost'] + row['fundingCost'] for row in rows)
    net = sum(row['netPnL'] for row in rows)
    stats = expectancy(rows, minimum)
    profitable = sum(max(0, row['netPnL']) for row in rows)
    losing = -sum(min(0, row['netPnL']) for row in rows)
    hours = len({int(row['exitTime']) // 3600000 for row in rows if row['exitTime'] is not None})
    symbols = {row['symbol'] for row in rows if row['symbol']}
    cost_stress = (net - costs*.25)/n if n else None
    fill_stress = (sum(min(0, row['netPnL']) + .75*max(0, row['netPnL']) for row in rows)/n) if n else None
    active = promotion_gate(evidence, minimum)
    if active:
        status = 'ACTIVE_PAPER'
    elif selection_bias == 'HIGH_30_VARIANTS':
        status = 'SHADOW'
    elif n < minimum:
        status = 'INSUFFICIENT_SAMPLE'
    elif stats['expectancyUpper'] is not None and stats['expectancyUpper'] <= 0:
        status = 'NEGATIVE_EDGE'
    elif net < 0:
        status = 'NEGATIVE_EDGE'
    elif stats['expectancyLower'] is not None and stats['expectancyLower'] > 0:
        status = 'PAPER_CANDIDATE'
    else:
        status = 'UNCERTAIN_EDGE'
    return dict(strategy=name, family=family, direction=direction, regime=regime,
        trades=n, effectiveTrades=min(n, hours), grossExpectancy=gross/n if n else None,
        grossPnL=gross, fees=sum(row['fees'] for row in rows),
        spreadCost=sum((row['spreadCost'] or 0) for row in rows),
        slippageCost=sum(row['slippageCost'] for row in rows),
        fundingCost=sum(row['fundingCost'] for row in rows), netPnL=net,
        costs=costs, netExpectancy=net/n if n else None, ciLow=stats['expectancyLower'],
        ciHigh=stats['expectancyUpper'], profitFactor=profitable/losing if losing else None,
        maxDrawdown=stats['maxDrawdown'], oos=evidence.get('oosNetExpectancy') if evidence else None,
        costStress25=cost_stress, fillStress75=fill_stress, status=status,
        selectionBiasRisk=selection_bias, costModelComplete=all(row['costModelComplete'] for row in rows),
        historicalOnly=True, activePaper=active, symbols=len(symbols))


def snapshot(engine, adaptive, engine_lock):
    """Existing ledgers are never promoted using their own diagnostic trades."""
    rows = []
    with engine_lock:
        base_rows = base_round_trips(engine.db.execute('SELECT * FROM trades ORDER BY time,id').fetchall())
    for name in STRATEGIES:
        rows.append(candidate(name, 'BASE', base_rows[name], 'LONG'))
    with adaptive.portfolio.lock:
        adaptive_trades = [json.loads(row[0]) for row in adaptive.portfolio.db.execute(
            'SELECT data FROM closed_trades')]
    rows.append(candidate('Adaptive', 'ADAPTIVE', adaptive_trades, 'LONG'))
    with adaptive.directional.lock:
        directional_trades = [json.loads(row[0]) for row in adaptive.directional.db.execute(
            'SELECT data FROM directional_trades')]
    rows.append(candidate('Directional', 'DIRECTIONAL', directional_trades, 'LONG/SHORT'))
    for vid, variant in sorted(adaptive.matrix.variants.items()):
        portfolio = variant['portfolio']
        with portfolio.lock:
            trades = [json.loads(row[0]) for row in portfolio.db.execute('SELECT data FROM closed_trades')]
        rows.append(candidate(vid, variant['family'], trades, 'LONG', selection_bias='HIGH_30_VARIANTS'))
    rows.sort(key=lambda row: (row['activePaper'], row['ciLow'] if row['ciLow'] is not None else -1e9,
        row['netExpectancy'] if row['netExpectancy'] is not None else -1e9), reverse=True)
    confirmed = [row for row in rows if row['activePaper']]
    return dict(mode='PAPER_ONLY', liveReady=False, headline='NO CONFIRMED EDGE' if not confirmed else 'CONFIRMED PAPER EDGE',
        noConfirmedEdge=not confirmed, confirmedStrategies=len(confirmed), activePaperStrategies=len(confirmed),
        pausedNegativeStrategies=sum(row['status']=='NEGATIVE_EDGE' for row in rows),
        shadowCandidates=len(adaptive.matrix.variants)+1, candidates=rows,
        targetMultiple=5, targetGuaranteed=False,
        note='Historical trades are diagnostic only; OOS and fill evidence are required before promotion.')
