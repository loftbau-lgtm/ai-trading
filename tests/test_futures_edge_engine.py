import tempfile
import unittest
from pathlib import Path

from futures_edge_engine import FuturesEdgeEngine, _stats, net_return, signal


def feed(count=121, start=0, trend=.0005):
    markets = {}
    for symbol, multiplier in [('BTCUSDT',1),('ETHUSDT',1.5),('SOLUSDT',2)]:
        bars = []
        for i in range(count):
            price = 100 * (1+trend*multiplier)**i
            bars.append(dict(time=start+i*60000,end=start+i*60000+59999,
                             close=price,volume=100.))
        price = bars[-1]['close']
        markets[symbol] = dict(bars=bars,bid=price*.9999,ask=price*1.0001,
            fundingRate=.0001,nextFundingTime=start+(count+1000)*60000)
    return dict(serverTime=start+count*60000,markets=markets)


class FuturesEdgeEngineTests(unittest.TestCase):
    def test_closed_candles_persistence_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'edge_lab.sqlite3'
            engine=FuturesEdgeEngine(path)
            snapshot=feed()
            engine.cycle(snapshot)
            before=engine.snapshot()
            self.assertEqual(before['state'],'COLLECTING')
            self.assertEqual(before['barCount'],363)
            engine.cycle(snapshot)
            self.assertEqual(engine.snapshot()['barCount'],before['barCount'])
            self.assertEqual(engine.snapshot()['signalCount'],before['signalCount'])
            engine.close()
            again=FuturesEdgeEngine(path)
            self.assertEqual(again.snapshot()['barCount'],before['barCount'])
            self.assertEqual(again.snapshot()['signalCount'],before['signalCount'])
            again.close()

    def test_future_bar_not_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=FuturesEdgeEngine(Path(directory)/'edge_lab.sqlite3')
            snapshot=feed()
            snapshot['serverTime']=snapshot['markets']['BTCUSDT']['bars'][-1]['end']
            with self.assertRaises(ValueError):
                engine.cycle(snapshot)
            self.assertEqual(engine.snapshot()['barCount'],0)
            engine.close()

    def test_signal_is_point_in_time(self):
        history=[dict(close=100*(1.0005)**i) for i in range(61)]
        initial=signal('L1_TREND',history)
        self.assertTrue(initial)
        self.assertEqual(initial,signal('L1_TREND',(history+[dict(close=1)])[:61]))

    def test_cost_stress_and_negative_edge(self):
        net,gross,cost=net_return(100,100.1,'LONG',.0002,.0005,.0002,.0001)
        self.assertAlmostEqual(gross,.001)
        self.assertAlmostEqual(net,gross-cost)
        rows=[dict(exitTime=i*3600000,netReturn=-.01,cost=.002) for i in range(120)]
        stats=_stats(rows)
        self.assertLess(stats['ciHigh'],0)
        self.assertLess(stats['costStress25'],0)

    def test_oos_not_inferred_from_train(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=FuturesEdgeEngine(Path(directory)/'edge_lab.sqlite3')
            engine.cycle(feed())
            for row in engine.candidates():
                self.assertEqual(row['oosTrades'],0)
                self.assertFalse(row['paperPromoted'])
            engine.close()

    def test_shadow_outcome_matures_only_after_future_closed_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=FuturesEdgeEngine(Path(directory)/'edge_lab.sqlite3')
            engine.cycle(feed(121))
            before=engine.db.execute('SELECT COUNT(*) FROM signals WHERE exitTime IS NOT NULL').fetchone()[0]
            self.assertEqual(before,0)
            engine.cycle(feed(151))
            after=engine.db.execute('SELECT COUNT(*) FROM signals WHERE exitTime IS NOT NULL').fetchone()[0]
            self.assertGreater(after,0)
            engine.cycle(feed(151))
            self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM signals WHERE exitTime IS NOT NULL').fetchone()[0],after)
            engine.close()

    def test_positive_paper_promotion_then_rolling_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=FuturesEdgeEngine(Path(directory)/'edge_lab.sqlite3')
            model='L1_TREND'
            with engine.db:
                for split,count,offset in [('TRAIN',35,0),('VALIDATION',45,1000000000),
                                           ('OOS',180,2000000000)]:
                    for i in range(count):
                        week=i//60 if split=='OOS' else 0
                        time=offset+week*7*86400000+(i%60)*3600000
                        symbol=('BTCUSDT','ETHUSDT','SOLUSDT')[i%3]
                        engine.db.execute('INSERT INTO signals '
                            '(model,symbol,candleTime,entry,spread,funding,fundingDue,'
                            'costComplete,split,exitTime,netReturn,grossReturn,cost) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (model,symbol,time,100,.0002,0,time+86400000,1,
                             split,time+1800000,.01,.012,.002))
            row=next(item for item in engine.candidates() if item['modelId']==model)
            self.assertTrue(row['paperPromoted'],row)
            self.assertEqual(row['status'],'ACTIVE_PAPER')
            with engine.db:
                for i in range(50):
                    time=2000000000+4*7*86400000+i*3600000
                    symbol=('BTCUSDT','ETHUSDT','SOLUSDT')[i%3]
                    engine.db.execute('INSERT INTO signals '
                        '(model,symbol,candleTime,entry,spread,funding,fundingDue,'
                        'costComplete,split,exitTime,netReturn,grossReturn,cost) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (model,symbol,time,100,.0002,0,time+86400000,1,
                         'OOS',time+1800000,-.002,0,.002))
            row=next(item for item in engine.candidates() if item['modelId']==model)
            self.assertFalse(row['paperPromoted'])
            self.assertEqual(row['status'],'PAUSED_EDGE_DECAY')
            engine.close()


if __name__ == '__main__':
    unittest.main()
