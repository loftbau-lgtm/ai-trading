import copy
import json
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path

from directional_model import beta, context, evaluate
from directional_paper import DirectionalPaper, _class, load_config
from directional_statistics import expectancy, monte_carlo


def candles(count, start=0, change=0):
    rows=[]
    price=100
    for i in range(start,start+count):
        previous=price
        price=previous*(1+change+((i%7)*1e-5 if change else 0))
        rows.append(dict(time=i*60000,end=i*60000+59999,open=previous,
                         high=max(previous,price)*1.002,low=min(previous,price)*.998,
                         close=price,volume=100000,turnover=10000000,trades=1000))
    return rows


def signal(side,time,price=100):
    return dict(symbol='BTCUSDT',time=time,candleCloseTimestamp=time+59999,action=side,
                candidateDirection=side,regime='RANGE',confidence=.8,activityDecile=5,
                volatilityRegime='LOW',marketBreadthRegime='RANGE',edgeCostRatio=3,
                atr=.1,bid=price-.1,ask=price+.1,expectedUpMovePct=.6,
                expectedDownMovePct=.6,expectedCostPct=.2,evLongPct=.3,
                evShortPct=.3)


class DirectionalTests(unittest.TestCase):
    def test_long_short_cash_and_pnl_are_symmetric(self):
        for side in ('LONG','SHORT'):
            with self.subTest(side=side),tempfile.TemporaryDirectory() as folder:
                paper=DirectionalPaper(Path(folder)/'new.sqlite3')
                try:
                    bars=candles(110)
                    d=signal(side,bars[-1]['time'])
                    with paper.db:
                        self.assertEqual(paper._place(d,bars,bars[-1]['end']+1),'PLACE_'+side+'_MAKER')
                        paper.state['cursors']['BTCUSDT']=bars[-1]['time']
                        paper._persist()
                    order=copy.deepcopy(paper.state['orders']['BTCUSDT'])
                    fill=dict(candles(1,110)[0],open=100,close=100,low=99,high=101)
                    # Force execution without a same-candle exit.
                    paper.process({'BTCUSDT':bars+[fill]},[],fill['end']+1,None,False)
                    self.assertEqual(len(paper.state['positions']),1)
                    p=paper.state['positions']['BTCUSDT']
                    self.assertEqual(p['entry'],order['price'])
                    reference=p['entry']+(1 if side=='LONG' else -1)
                    trade=paper._close('BTCUSDT',p,reference,fill['time']+60000,'TEST',0)
                    self.assertAlmostEqual(trade['grossPnL'],p['qty'])
                    self.assertAlmostEqual(trade['netPnL'],trade['grossPnL']-trade['totalCost'])
                    self.assertAlmostEqual(paper._equity(),100+trade['netPnL'])
                finally:paper.close()

    def test_short_stop_and_restart_idempotency(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'directional.sqlite3'
            paper=DirectionalPaper(path)
            bars=candles(110)
            with paper.db:
                paper._place(signal('SHORT',bars[-1]['time']),bars,bars[-1]['end']+1)
                paper.state['cursors']['BTCUSDT']=bars[-1]['time']
                paper._persist()
            price=paper.state['orders']['BTCUSDT']['price']
            fill=dict(candles(1,110)[0],open=price,close=price,low=price-.1,high=price+.05)
            paper.process({'BTCUSDT':bars+[fill]},[],fill['end']+1,None,False)
            self.assertEqual(len(paper.state['positions']),1)
            stop=paper.state['positions']['BTCUSDT']['stop']
            paper.close()
            again=DirectionalPaper(path)
            try:
                self.assertEqual(len(again.state['positions']),1)
                bar=dict(candles(1,111)[0],open=stop+.1,close=stop+.1,low=price,high=stop+.2)
                history=bars+[fill,bar]
                again.process({'BTCUSDT':history},[],bar['end']+1,None,False)
                again.process({'BTCUSDT':history},[],bar['end']+1,None,False)
                trades=again.snapshot()['trades']
                self.assertEqual(len(trades),1)
                self.assertEqual(trades[0]['side'],'SHORT')
                self.assertEqual(trades[0]['reason'],'STOP_TAKER')
                self.assertLess(trades[0]['netPnL'],0)
                self.assertGreaterEqual(trades[0]['exitPrice'],bar['open'])
            finally:again.close()

    def test_direction_probabilities_regime_and_beta(self):
        bars=candles(120,change=.0006)
        micro=dict(bid=bars[-1]['close']-.01,ask=bars[-1]['close']+.01,
                   spreadPct=.02,bookImbalance=.5,tradeFlowImbalance=.3,diagnosticRejections=[])
        breadth=context({f'S{i}USDT':bars for i in range(10)})
        d=evaluate('BTCUSDT',bars,bars,dict(activityPercentile=90),micro,breadth,load_config(),bars[-1]['end']+1)
        self.assertEqual(d['regime'],'TREND_UP')
        self.assertEqual(d['action'],'LONG')
        self.assertAlmostEqual(sum(d[k] for k in ('pUp','pDown','pFlat')),1)
        self.assertAlmostEqual(d['btcBeta'],1)
        self.assertEqual(set(d['horizons']),{'5m','15m','30m'})
        self.assertEqual(d['candleCloseTimestamp'],bars[-1]['end'])

    def test_downtrend_can_choose_short_without_buy_bias(self):
        bars=candles(120,change=-.0008)
        micro=dict(bid=bars[-1]['close']-.01,ask=bars[-1]['close']+.01,
                   spreadPct=.02,bookImbalance=-.8,tradeFlowImbalance=-.8,diagnosticRejections=[])
        breadth=context({f'S{i}USDT':bars for i in range(10)})
        d=evaluate('BTCUSDT',bars,bars,dict(activityPercentile=90),micro,breadth,load_config(),bars[-1]['end']+1)
        self.assertEqual(d['regime'],'TREND_DOWN')
        self.assertEqual(d['action'],'SHORT')
        self.assertGreater(d['evShortPct'],d['evLongPct'])

    def test_uncertainty_negative_edge_and_monte_carlo(self):
        positive=[dict(netPnL=.1) for _ in range(120)]
        negative=[dict(netPnL=-.1) for _ in range(120)]
        self.assertEqual(expectancy(positive)['status'],'CONFIRMED_POSITIVE_EDGE')
        self.assertEqual(expectancy(negative)['status'],'REJECTED_EDGE')
        self.assertEqual(expectancy(positive[:20])['status'],'INSUFFICIENT_SAMPLE')
        mc=monte_carlo(positive,95,minimum=100,iterations=30)
        self.assertGreater(mc['estimatedTradesToPositive']['0.9'],0)
        self.assertLessEqual(mc['estimatedTradesToPositive']['0.9'],100)
        self.assertGreater(mc['recoveryProbability']['100'],.9)
        self.assertEqual(monte_carlo(negative,95,minimum=100,iterations=30)['status'],'NO_STATISTICAL_EDGE')

    def test_future_label_requires_closed_future_candles(self):
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3')
            try:
                rows=candles(125,change=.0002)
                decision=rows[100]
                key='test-label'
                paper.db.execute('INSERT INTO directional_predictions VALUES(?,?,?,?)',
                    (key,decision['time'],'BTCUSDT',json.dumps(dict(expectedCostPct=.2))))
                paper._labels({'BTCUSDT':rows[:105]},rows[104]['end']+1)
                self.assertEqual(paper.db.execute('SELECT COUNT(*) FROM directional_labels').fetchone()[0],0)
                paper._labels({'BTCUSDT':rows[:106]},rows[105]['end']+1)
                self.assertEqual([r[0] for r in paper.db.execute('SELECT horizon FROM directional_labels')],[5])
                paper._labels({'BTCUSDT':rows[:116]},rows[115]['end']+1)
                self.assertEqual([r[0] for r in paper.db.execute('SELECT horizon FROM directional_labels ORDER BY horizon')],[5,15])
            finally:paper.close()

    def test_confirmed_negative_class_blocks_order_without_sizing_up(self):
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3')
            try:
                rows=candles(110)
                d=signal('SHORT',rows[-1]['time'])
                edge=_class(d)
                with paper.db:
                    paper.db.executemany('INSERT INTO directional_trades VALUES(?,?,?)',
                        [(str(i),i,json.dumps(dict(edgeClass=edge,netPnL=-.1))) for i in range(100)])
                self.assertEqual(paper._place(d,rows,rows[-1]['end']+1),'PAUSED_NEGATIVE_EDGE')
                self.assertFalse(paper.state['orders'])
            finally:paper.close()

    def test_stale_microstructure_selects_flat(self):
        bars=candles(120,change=.0006)
        micro=dict(bid=bars[-1]['close']-.01,ask=bars[-1]['close']+.01,spreadPct=.02,
                   bookImbalance=.5,tradeFlowImbalance=.3,diagnosticRejections=['REJECT_STALE_DATA'])
        breadth=context({f'S{i}USDT':bars for i in range(10)})
        d=evaluate('BTCUSDT',bars,bars,dict(activityPercentile=90),micro,breadth,load_config(),bars[-1]['end']+1)
        self.assertEqual(d['action'],'FLAT')
        self.assertEqual(d['rejection'],'STALE_MICROSTRUCTURE')

    def test_manual_kill_cancels_pending_short(self):
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3')
            rows=candles(110)
            with paper.db:
                paper._place(signal('SHORT',rows[-1]['time']),rows,rows[-1]['end']+1)
                paper.state['cursors']['BTCUSDT']=rows[-1]['time']
                paper._persist()
            price=paper.state['orders']['BTCUSDT']['price']
            fill=dict(candles(1,110)[0],open=price,close=price,low=price-.1,high=price+.05)
            try:
                paper.process({'BTCUSDT':rows+[fill]},[],fill['end']+1,None,False,True)
                self.assertFalse(paper.state['positions'])
                self.assertFalse(paper.state['orders'])
            finally:paper.close()

    def test_market_neutral_pair_fills_together_or_waits(self):
        config=load_config();config['portfolioMode']='MARKET_NEUTRAL'
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3',config)
            rows=candles(110)
            pair='PAIR:TEST'
            long=signal('LONG',rows[-1]['time'])
            short=signal('SHORT',rows[-1]['time']);short['symbol']='ETHUSDT'
            with paper.db:
                self.assertEqual(paper._place(long,rows,rows[-1]['end']+1,pair),'PLACE_LONG_MAKER')
                self.assertEqual(paper._place(short,rows,rows[-1]['end']+1,pair),'PLACE_SHORT_MAKER')
                paper.state['cursors']={'BTCUSDT':rows[-1]['time'],'ETHUSDT':rows[-1]['time']}
                paper._persist()
            btc_bid=paper.state['orders']['BTCUSDT']['price']
            eth_ask=paper.state['orders']['ETHUSDT']['price']
            btc_110=dict(candles(1,110)[0],open=100,close=100,low=btc_bid-.1,high=100.1)
            eth_110=dict(candles(1,110)[0],open=100,close=100,low=99.9,high=eth_ask-.01)
            try:
                paper.process({'BTCUSDT':rows+[btc_110],'ETHUSDT':rows+[eth_110]},[],btc_110['end']+1,None,False)
                self.assertEqual(len(paper.state['orders']),2)
                self.assertFalse(paper.state['positions'])
                btc_111=dict(candles(1,111)[0],open=100,close=100,low=btc_bid-.1,high=100.1)
                eth_111=dict(candles(1,111)[0],open=100,close=100,low=99.9,high=eth_ask+.1)
                paper.process({'BTCUSDT':rows+[btc_110,btc_111],'ETHUSDT':rows+[eth_110,eth_111]},[],btc_111['end']+1,None,False)
                self.assertEqual(len(paper.state['positions']),2)
                self.assertFalse(paper.state['orders'])
                self.assertLessEqual(paper._exposure(False)['gross'],paper._equity()*config['maxGrossExposure'])
            finally:paper.close()

    def test_market_neutral_pair_selection_balances_btc_beta(self):
        config=load_config();config['portfolioMode']='MARKET_NEUTRAL'
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3',config)
            rows=candles(110)
            long=signal('LONG',rows[-1]['time']);long.update(btcBeta=1.1,utilityLong=.4,utilityShort=-.5,decisionTimestamp=rows[-1]['end']+1)
            short=signal('SHORT',rows[-1]['time']);short.update(symbol='ETHUSDT',btcBeta=1.2,utilityLong=-.5,utilityShort=.4,decisionTimestamp=rows[-1]['end']+1)
            try:
                with paper.db:
                    result=paper._neutral_pairs([long,short],{'BTCUSDT':rows,'ETHUSDT':rows},rows[-1]['time'],True,False)
                self.assertEqual(len(result),2)
                a=paper.state['orders']['BTCUSDT'];b=paper.state['orders']['ETHUSDT']
                self.assertAlmostEqual(a['qty']*a['price']*long['btcBeta'],b['qty']*b['price']*short['btcBeta'])
                self.assertEqual(a['pairId'],b['pairId'])
            finally:paper.close()

    def test_shadow_uses_real_trade_through_and_censors_reconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3')
            stamp=1_000_000
            class Store:
                generation=7
                lock=threading.RLock()
                connections={'micro':True}
                state={'BTCUSDT':{'tradeCoverageStart':stamp-1000,'coverageSerial':0}}
                samples=defaultdict(list)
            store=Store()
            for horizon in (1,2,5,10,30,60):
                store.samples['BTCUSDT'].append(dict(timestamp=stamp+horizon*1000,mid=100.5,bid=100.49,ask=100.51))
            paper._recent_trades['BTCUSDT'].append(dict(receivedTimestamp=stamp+500,price=99.9,aggressor='sell'))
            base=dict(symbol='BTCUSDT',candidateDirection='LONG',decisionTimestamp=stamp,
                      bid=100.,ask=100.02,expectedCostPct=.2,microGeneration=7,microCoverageSerial=0)
            with paper.db:
                paper.db.execute('INSERT INTO directional_shadow VALUES(?,?,?)',('observed',stamp,json.dumps(base)))
                paper.db.execute('INSERT INTO directional_shadow VALUES(?,?,?)',('censored',stamp,json.dumps(dict(base,microGeneration=6))))
            try:
                paper.observe(store,stamp+62001)
                outcomes=[json.loads(x[0]) for x in paper.db.execute('SELECT data FROM directional_shadow_observations WHERE id=? ORDER BY horizon',('observed',))]
                self.assertEqual(len(outcomes),6)
                self.assertTrue(all(x['status']=='OBSERVED' and x['touch'] and x['tradeThrough'] for x in outcomes))
                censored=json.loads(paper.db.execute('SELECT data FROM directional_shadow_observations WHERE id=? AND horizon=60',('censored',)).fetchone()[0])
                self.assertEqual(censored['status'],'CENSORED')
                self.assertEqual(censored['reason'],'STREAM_INTERRUPTED_OR_RESTARTED')
            finally:paper.close()

    def test_report_remains_responsive_during_long_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3')
            held=threading.Event();release=threading.Event()
            def busy():
                with paper.lock:
                    held.set();release.wait(3)
            worker=threading.Thread(target=busy)
            worker.start();self.assertTrue(held.wait(1))
            try:
                started=time.perf_counter()
                report=paper.snapshot()
                self.assertLess(time.perf_counter()-started,.5)
                self.assertEqual(report['status']['state'],'processing')
                self.assertEqual(report['mode'],'PAPER_ONLY')
            finally:
                release.set();worker.join();paper.close()


if __name__=='__main__':unittest.main()
