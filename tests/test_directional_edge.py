import json
import tempfile
import unittest
from pathlib import Path

from directional_edge import diagnostic, guard_status
from directional_paper import DirectionalPaper


def trade(index, net=-0.07, side='LONG', regime='TREND_UP', symbol='BTCUSDT', edge_ratio=1.1):
    entry=index*600000
    costs=0.02
    return dict(id=f'DIRECTIONAL_G0:HYBRID_G0:{symbol}:1m:{entry+59999}',
        modelId='HYBRID_G0', symbol=symbol, side=side, regime=regime,
        entryTime=entry, exitTime=entry+300000, entryPrice=100, exitPrice=100+net+costs,
        qty=1, holdingMinutes=5, grossPnL=net+costs, fees=costs,
        spreadCost=0, slippageCost=0, fundingCost=0, totalCost=costs,
        netPnL=net, edgeCostRatio=edge_ratio, edgeClass='TEST')


class DirectionalEdgeTests(unittest.TestCase):
    def test_negative_edge_pauses_after_sufficient_sample(self):
        self.assertIsNone(guard_status([trade(i) for i in range(99)],100,30))
        self.assertEqual(guard_status([trade(i) for i in range(100)],100,30),'PAUSED_NEGATIVE_EDGE')

    def test_rolling_decay_pauses_without_discarding_prior_positive_history(self):
        rows=[trade(i,net=.5) for i in range(200)]+[trade(i+200,net=-.1) for i in range(100)]
        self.assertEqual(guard_status(rows,100,30),'PAUSED_EDGE_DECAY')

    def test_negative_median_300_switches_to_shadow(self):
        rows=[trade(i,net=-.1 if i<160 else .11) for i in range(300)]
        self.assertEqual(guard_status(rows,100,100),'SHADOW')

    def test_open_position_survives_pause_pending_order_expires_restart_sticks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            path=Path(folder)/'directional.sqlite3'
            paper=DirectionalPaper(path)
            original_model=paper.state['modelId']
            position=dict(id='open-1',symbol='BTCUSDT',side='LONG',entry=100,price=100,qty=.1,
                entryFee=.01,entryTime=1000,mark=100,stop=90,target=110,regime='TREND_UP',
                edgeClass='TEST',activityDecile=1,volatilityRegime='LOW',confidence=.6,
                edgeCostRatio=2,expectedMovePct=.2,expectedCostPct=.1,expectedNetEdgePct=.1,
                marketBreadthRegime='BULL')
            order=dict(id='pending-1',symbol='ETHUSDT',side='LONG',price=100,qty=.1)
            try:
                with paper.db:
                    paper.db.executemany('INSERT INTO directional_trades VALUES(?,?,?)',
                        [(row['id'],row['exitTime'],json.dumps(row)) for row in (trade(i) for i in range(100))])
                    paper.state['positions']['BTCUSDT']=position
                    paper.state['orders']['ETHUSDT']=order
                    paper.db.execute('INSERT INTO directional_positions VALUES(?,?,?,?)',
                        ('open-1','BTCUSDT','OPEN',json.dumps(position)))
                    paper.db.execute('INSERT INTO directional_orders VALUES(?,?,?,?)',
                        ('pending-1','ETHUSDT','PENDING',json.dumps(order)))
                    paper._persist()
                    self.assertEqual(paper._refresh_edge_guard(),'PAUSED_NEGATIVE_EDGE')
                self.assertIn('BTCUSDT',paper.state['positions'])
                self.assertFalse(paper.state['orders'])
                self.assertEqual(paper._place({'action':'LONG','symbol':'ETHUSDT'},[],0),'PAUSED_NEGATIVE_EDGE')
                self.assertEqual(paper.db.execute('SELECT status FROM directional_orders WHERE id=?',
                    ('pending-1',)).fetchone()[0],'EXPIRED')
                self.assertEqual(paper.db.execute('SELECT COUNT(*) FROM directional_trades').fetchone()[0],100)
            finally:
                paper.close()
            again=DirectionalPaper(path)
            try:
                self.assertEqual(again.state['edgePause'],'PAUSED_NEGATIVE_EDGE')
                self.assertEqual(again.state['modelId'],original_model)
                self.assertIn('BTCUSDT',again.state['positions'])
                self.assertFalse(again.state['orders'])
                self.assertEqual(again.edge_health()['completedTrades'],100)
                self.assertFalse(again.edge_health()['liveReady'])
            finally:
                again.close()

    def test_cost_side_regime_churn_and_rolling_diagnostics(self):
        first=trade(0,net=.08,side='LONG',regime='TREND_UP',edge_ratio=3.1)
        second=trade(1,net=-.09,side='SHORT',regime='TREND_DOWN',edge_ratio=2.1)
        third=trade(2,net=-.09,side='SHORT',regime='TREND_DOWN',edge_ratio=2.1)
        third['entryTime']=second['exitTime']+60000
        third['exitTime']=third['entryTime']+300000
        report=diagnostic([first,second,third],minimum=100,iterations=30)
        self.assertAlmostEqual(report['grossPnL']-report['totalCosts'],report['netPnL'])
        self.assertEqual(report['bySide']['SHORT']['trades'],2)
        self.assertEqual(report['regimeBySide']['TREND_DOWN/SHORT']['trades'],2)
        self.assertEqual(report['reentryWithinMinutes']['1'],1)
        self.assertAlmostEqual(report['churnRate'],1/3)
        self.assertAlmostEqual(report['rollingExpectancy']['50'],(-.10)/3)
        self.assertEqual(report['edgeCostBuckets']['3.0+']['trades'],1)
        self.assertEqual(report['edgeCostBuckets']['2.0-3.0']['trades'],2)
        self.assertEqual(len(report['equityCurve']),3)

    def test_existing_position_exits_by_its_stop_after_entry_pause(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            paper=DirectionalPaper(Path(folder)/'directional.sqlite3')
            position=dict(id='open-2',symbol='BTCUSDT',side='LONG',entry=100,price=100,qty=.1,
                entryFee=.01,entryTime=0,mark=100,stop=90,target=110,regime='TREND_UP',
                edgeClass='TEST',activityDecile=1,volatilityRegime='LOW',confidence=.6,
                edgeCostRatio=2,expectedMovePct=.2,expectedCostPct=.1,expectedNetEdgePct=.1,
                marketBreadthRegime='BULL')
            try:
                with paper.db:
                    paper.db.executemany('INSERT INTO directional_trades VALUES(?,?,?)',
                        [(row['id'],row['exitTime'],json.dumps(row)) for row in (trade(i) for i in range(100))])
                    paper.state['positions']['BTCUSDT']=position
                    paper.db.execute('INSERT INTO directional_positions VALUES(?,?,?,?)',
                        ('open-2','BTCUSDT','OPEN',json.dumps(position)))
                    paper._persist()
                    paper._refresh_edge_guard()
                self.assertIn('BTCUSDT',paper.state['positions'])
                bar=dict(time=60000,end=119999,open=89,high=91,low=88,close=89,volume=10000)
                with paper.db:
                    paper._fill_and_exit('BTCUSDT',bar,60000,60000,None,True)
                    paper._persist()
                self.assertNotIn('BTCUSDT',paper.state['positions'])
                self.assertEqual(paper.db.execute('SELECT COUNT(*) FROM directional_trades').fetchone()[0],101)
                self.assertEqual(paper.state['edgePause'],'PAUSED_NEGATIVE_EDGE')
            finally:
                paper.close()

    def test_duplicate_decision_key_detected_without_mutating_input(self):
        first=trade(0)
        second=dict(first,id='DIFFERENT:MODEL:BTCUSDT:1m:59999')
        source=[first,second]
        report=diagnostic(source,minimum=100,iterations=30)
        self.assertEqual(report['duplicates'],1)
        self.assertEqual(source[0]['netPnL'],-.07)
        self.assertEqual(report['mode'],'PAPER_ONLY')
        decision_ids=['DIRECTIONAL_G0:HYBRID_G0:BTCUSDT:1m:59999',
                      'OTHER_EXPERIMENT:HYBRID_G0:BTCUSDT:1m:59999']
        self.assertEqual(diagnostic([],decision_ids=decision_ids)['duplicateDecisions'],1)

    def test_probability_labels_and_buckets(self):
        row=trade(0,net=.1)
        decisions={row['id']:dict(pUp=.62,pDown=.2,pFlat=.18,action='LONG')}
        labels={(row['id'],5):dict(futureReturnPct=.1),
            (row['id'],15):dict(futureReturnPct=.2),
            (row['id'],30):dict(futureReturnPct=.3)}
        report=diagnostic([row],decisions,labels,minimum=100,iterations=30)
        self.assertEqual(report['probabilityBuckets']['LONG/0.60-0.65']['actualDirectionAccuracy'],1)
        self.assertEqual(report['tradeDiagnostics'][0]['actualReturn30m'],.3)
        self.assertIsNone(report['directionCalibrated'])
        self.assertIsNone(report['overtrading'])


if __name__ == '__main__':
    unittest.main()
