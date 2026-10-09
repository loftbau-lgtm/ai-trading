"""Read-only diagnostics and deterministic entry guard for Directional PAPER."""
import math
import statistics
from collections import defaultdict

from directional_statistics import drawdown, expectancy


MIN_EDGE_SAMPLE = 100


def guard_status(trades, minimum=MIN_EDGE_SAMPLE, iterations=400):
    minimum = max(MIN_EDGE_SAMPLE, minimum)
    if len(trades) < minimum:
        return None
    all_time = expectancy(trades, minimum, iterations)
    if all_time['expectancyUpper'] is not None and all_time['expectancyUpper'] <= 0:
        return 'PAUSED_NEGATIVE_EDGE'
    recent = expectancy(trades[-minimum:], minimum, iterations)
    if len(trades) > minimum and recent['expectancyUpper'] is not None and recent['expectancyUpper'] <= 0:
        return 'PAUSED_EDGE_DECAY'
    if len(trades) >= 300 and all_time['expectancyMedian'] is not None and all_time['expectancyMedian'] < 0:
        return 'SHADOW'
    return None


def _summary(trades):
    values = [float(t['netPnL']) for t in trades]
    gross = sum(float(t.get('grossPnL', t['netPnL'])) for t in trades)
    fees = sum(float(t.get('fees', 0)) for t in trades)
    spread = sum(float(t.get('spreadCost', 0)) for t in trades)
    slippage = sum(float(t.get('slippageCost', 0)) for t in trades)
    funding = sum(float(t.get('fundingCost', 0)) for t in trades)
    costs = fees + spread + slippage + funding
    net = sum(values)
    wins = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    return dict(trades=len(trades), grossPnL=gross, fees=fees, spreadCost=spread,
        slippageCost=slippage, fundingCost=funding, totalCosts=costs, netPnL=net,
        grossExpectancy=gross / len(trades) if trades else None,
        costPerTrade=costs / len(trades) if trades else None,
        netExpectancy=net / len(trades) if trades else None,
        profitFactor=wins / losses if losses else None,
        winRate=sum(x > 0 for x in values) / len(values) if values else None,
        maxDrawdown=drawdown(values) if values else None)


def _bucket(value, edges, labels):
    for end, label in zip(edges, labels):
        if value < end:
            return label
    return labels[-1]


def _group(trades, key):
    groups = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade)
    return {name: _summary(rows) for name, rows in sorted(groups.items())}


def _correlation(xs, ys):
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    numerator = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))
    return numerator / denominator if denominator else None


def diagnostic(trades, decisions=None, labels=None, orders=0, fills=0, filled_long=0, filled_short=0, open_positions=0,
               pending_orders=0, decision_ids=None, minimum=MIN_EDGE_SAMPLE, iterations=400, paused=None,
               starting_capital=100):
    """Build a report without writing to any ledger or changing model decisions."""
    decisions = decisions or {}
    labels = labels or {}
    trades = sorted(trades, key=lambda t: (t['exitTime'], t['id']))
    summary = _summary(trades)
    ci = expectancy(trades, max(minimum, MIN_EDGE_SAMPLE), iterations)
    rolling = {str(n): _summary(trades[-n:])['netExpectancy'] for n in (50, 100, 250)}
    rolling['all'] = summary['netExpectancy']
    sides = _group(trades, lambda t: t['side'])
    regimes = _group(trades, lambda t: t.get('regime') or 'UNKNOWN')
    regime_sides = _group(trades, lambda t: (t.get('regime') or 'UNKNOWN') + '/' + t['side'])
    probability_labels = ('0.50-0.55', '0.55-0.60', '0.60-0.65', '0.65-0.70', '0.70-0.80', '0.80+')
    edge_labels = ('<1.0', '1.0-1.25', '1.25-1.5', '1.5-2.0', '2.0-3.0', '3.0+')
    probability_groups, edge_groups = defaultdict(list), defaultdict(list)
    probability_hits = defaultdict(list)
    calibration = defaultdict(list)
    detailed = []
    for trade in trades:
        side = trade['side']
        prediction = decisions.get(trade['id'], {})
        probability = prediction.get('pUp' if side == 'LONG' else 'pDown')
        ratio = trade.get('edgeCostRatio')
        returns = {h: labels.get((trade['id'], h), {}).get('futureReturnPct') for h in (5, 15, 30)}
        if probability is not None and probability >= .5:
            bucket = _bucket(probability, (.55, .60, .65, .70, .80), probability_labels)
            probability_groups[(side, bucket)].append(trade)
            if returns[15] is not None:
                label = labels.get((trade['id'], 15), {}).get('label')
                hit = (label == ('UP' if side == 'LONG' else 'DOWN')) if label else (
                    (returns[15] > 0) if side == 'LONG' else (returns[15] < 0))
                probability_hits[(side, bucket)].append(int(hit))
                calibration[side].append((float(probability), int(hit)))
        if ratio is not None:
            edge_groups[_bucket(float(ratio), (1, 1.25, 1.5, 2, 3), edge_labels)].append(trade)
        detailed.append(dict(id=trade['id'], symbol=trade['symbol'], side=side,
            pUp=prediction.get('pUp'), pDown=prediction.get('pDown'), pFlat=prediction.get('pFlat'),
            selectedAction=prediction.get('selectedAction', prediction.get('action')),
            actualReturn5m=returns[5], actualReturn15m=returns[15], actualReturn30m=returns[30]))
    probability_buckets = {}
    for (side, bucket), rows in sorted(probability_groups.items()):
        hits = probability_hits[(side, bucket)]
        probability_buckets[f'{side}/{bucket}'] = dict(_summary(rows),
            labelled=len(hits), actualDirectionAccuracy=sum(hits)/len(hits) if hits else None)
    edge_buckets = {name: _summary(rows) for name, rows in sorted(edge_groups.items())}
    calibration_report = {}
    for side in ('LONG', 'SHORT'):
        pairs = calibration[side]
        residuals = [actual-predicted for predicted, actual in pairs]
        n = len(pairs)
        error = statistics.mean(residuals) if n else None
        se = statistics.stdev(residuals)/math.sqrt(n) if n >= 2 else None
        calibration_report[side] = dict(samples=n, predicted=statistics.mean(p for p, _ in pairs) if n else None,
            actual=statistics.mean(a for _, a in pairs) if n else None,
            residual=error, calibrated=abs(error) <= 1.96*se if n >= max(minimum, MIN_EDGE_SAMPLE) else None)
    times = [int(t['exitTime']) for t in trades]
    first = min((int(t['entryTime']) for t in trades), default=None)
    last = max(times, default=None)
    hours = max((last-first)/3600000, 1/60) if first is not None else None
    hourly = defaultdict(list)
    for trade in trades:
        hourly[int(trade['exitTime'])//3600000].append(trade)
    hourly_rows = [dict(hour=hour, trades=len(rows), netExpectancy=_summary(rows)['netExpectancy'])
        for hour, rows in sorted(hourly.items())]
    frequency_correlation = _correlation([row['trades'] for row in hourly_rows],
        [row['netExpectancy'] for row in hourly_rows])
    reentry = {str(n): 0 for n in (1, 3, 5, 10)}
    reentry_minutes = []
    previous_exit = {}
    for trade in sorted(trades, key=lambda t: (t['entryTime'], t['id'])):
        key = (trade['symbol'], trade['side'])
        earlier = previous_exit.get(key)
        if earlier is not None and 0 <= trade['entryTime']-earlier:
            elapsed = (trade['entryTime']-earlier)/60000
            reentry_minutes.append(elapsed)
            for minutes in (1, 3, 5, 10):
                reentry[str(minutes)] += elapsed <= minutes
        previous_exit[key] = max(previous_exit.get(key, 0), trade['exitTime'])
    turnover = sum(float(t['entryPrice'])*float(t['qty'])+
        float(t['exitPrice'])*float(t['qty']) for t in trades)
    gross_cumulative = net_cumulative = costs_cumulative = 0.0
    curve = []
    for trade in trades:
        gross_cumulative += float(trade['grossPnL'])
        net_cumulative += float(trade['netPnL'])
        costs_cumulative += float(trade['totalCost'])
        curve.append(dict(time=trade['exitTime'], grossEquity=starting_capital+gross_cumulative,
            netEquity=starting_capital+net_cumulative, cumulativeCosts=costs_cumulative))
    semantic_keys = [(t.get('modelId'), t['symbol'], '1m', t['id'].rsplit(':', 1)[-1]) for t in trades]
    trade_duplicates = len(semantic_keys)-len(set(semantic_keys))
    decision_keys = [tuple(parts[1:]) if len(parts := item.rsplit(':', 4)) == 5 else (item,)
        for item in (decision_ids or ())]
    decision_duplicates = len(decision_keys)-len(set(decision_keys))
    churn_rate = reentry['1']/len(trades) if trades else None
    cost_dominated = summary['grossPnL'] > 0 and summary['netPnL'] < 0
    overtrading = len(trades) >= max(minimum, MIN_EDGE_SAMPLE) and (
        (churn_rate is not None and churn_rate >= .2) or
        (frequency_correlation is not None and frequency_correlation <= -.3))
    if len(trades) < max(minimum, MIN_EDGE_SAMPLE):
        overtrading = None
    calibration_values = [calibration_report[side]['calibrated'] for side in ('LONG', 'SHORT')]
    calibrated = None if any(value is None for value in calibration_values) else all(calibration_values)
    alerts = []
    if paused in ('PAUSED_NEGATIVE_EDGE', 'PAUSED_EDGE_DECAY'):
        alerts.append('NEGATIVE EDGE')
    if overtrading:
        alerts.append('OVERTRADING')
    if cost_dominated:
        alerts.append('COST DOMINATED')
    if any(row['calibrated'] is False for row in calibration_report.values()):
        alerts.append('DIRECTION MIS-CALIBRATED')
    if any(regime_sides.get(key, {}).get('trades', 0) >= max(minimum, MIN_EDGE_SAMPLE) and
           regime_sides[key]['netExpectancy'] < 0 for key in ('TREND_UP/LONG', 'TREND_DOWN/SHORT')):
        alerts.append('REGIME FAILURE')
    if len(trades) >= max(minimum, MIN_EDGE_SAMPLE) and churn_rate is not None and churn_rate >= .1:
        alerts.append('CHURN')
    return dict(mode='PAPER_ONLY', liveReady=False,
        model='CONTROL-G0-LOSING' if paused in ('PAUSED_NEGATIVE_EDGE','PAUSED_EDGE_DECAY') else 'CONTROL-G0-DIAGNOSTIC',
        status=paused or 'ACTIVE', minimumEdgeSample=max(minimum, MIN_EDGE_SAMPLE),
        completedTrades=len(trades), openPositions=open_positions, orders=orders, fills=fills,
        pendingOrders=pending_orders, buyOperations=filled_long+sides.get('SHORT', {}).get('trades', 0),
        sellOperations=filled_short+sides.get('LONG', {}).get('trades', 0),
        longTrades=sides.get('LONG', {}).get('trades', 0), shortTrades=sides.get('SHORT', {}).get('trades', 0),
        duplicates=trade_duplicates+decision_duplicates, tradeDuplicates=trade_duplicates,
        duplicateDecisions=decision_duplicates, **summary, expectancyLower=ci['expectancyLower'],
        expectancyMedian=ci['expectancyMedian'], expectancyUpper=ci['expectancyUpper'],
        costConsumption=summary['totalCosts']/abs(summary['grossPnL']) if summary['grossPnL'] else None,
        rollingExpectancy=rolling, bySide=sides, byRegime=regimes, regimeBySide=regime_sides,
        probabilityBuckets=probability_buckets, edgeCostBuckets=edge_buckets,
        directionCalibration=calibration_report, directionCalibrated=calibrated,
        tradesPerHour=len(trades)/hours if hours else None,
        tradesPerSymbolHour={symbol:len(rows)/hours for symbol,rows in
            _group_rows(trades, lambda t:t['symbol']).items()} if hours else {},
        roundTripsPerHour=len(trades)/hours if hours else None,
        averageHoldMinutes=statistics.mean(t['holdingMinutes'] for t in trades) if trades else None,
        medianHoldMinutes=statistics.median(t['holdingMinutes'] for t in trades) if trades else None,
        reentryWithinMinutes=reentry, churnRate=churn_rate,
        averageReentryMinutes=statistics.mean(reentry_minutes) if reentry_minutes else None,
        medianReentryMinutes=statistics.median(reentry_minutes) if reentry_minutes else None,
        sameSymbolReentryWithin1m=reentry['1'],sameSymbolReentryWithin3m=reentry['3'],
        sameSymbolReentryWithin5m=reentry['5'],sameSymbolReentryWithin10m=reentry['10'],
        minReentryCooldownMinutes=None,
        notionalTurnover=turnover, notionalTurnoverPerHour=turnover/hours if hours else None,
        notionalTurnoverPerDay=turnover/hours*24 if hours else None,
        costPerTurnover=summary['totalCosts']/turnover if turnover else None,
        hourlyFrequency=hourly_rows, frequencyExpectancyCorrelation=frequency_correlation,
        equityCurve=curve, tradeDiagnostics=detailed, returnBasis='CLOSED_CANDLES_AFTER_DECISION',
        fundingNotModelled=any(t.get('fundingNotModelled') for t in trades),
        alerts=alerts, overtrading=overtrading, costDominated=cost_dominated,
        diagnosticOnly=True)


def _group_rows(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups
