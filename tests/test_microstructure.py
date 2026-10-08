import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from adaptive import load_config as paper_config
from adaptive_portfolio import PaperPortfolio
from adaptive_runner import AdaptiveRunner
from microstructure import MicrostructureStore,load_config,edge_bucket,FeeSchedule
from shadow_execution import ShadowExecution
from public_streams import PublicStreamWorker,PublicStreamManager,subscription_batches


def book(uid=1,bid=99.99,ask=100.01,symbol='BTCUSDT',bq=3,aq=1):
    return dict(u=uid,s=symbol,b=str(bid),a=str(ask),B=str(bq),A=str(aq))


def trade(uid,stamp,price=100,qty=2,maker=False,count=1):
    return dict(e='aggTrade',a=uid,s='BTCUSDT',T=stamp,E=stamp,p=str(price),q=str(qty),m=maker,f=uid*10,l=uid*10+count-1)


def bars(n):
    return [dict(time=i*60000,end=i*60000+59999,open=100,close=100,high=100.2,low=99.8,volume=10000,turnover=1000000,trades=1000) for i in range(n)]


class MicroTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True);self.root=Path(self.temp.name)
        self.t=int(time.time()*1000);self.config=load_config()
        self.store=MicrostructureStore(self.root/'micro.sqlite3',self.config)
        self.p=PaperPortfolio(self.root/'paper.sqlite3',paper_config())
        self.shadow=ShadowExecution(self.store,self.root/'paper.sqlite3',paper_config())
        self.store.clock_drift=0;self.store.connection('test',True)
    def tearDown(self):
        self.shadow.paper.close();self.store.db.close();self.p.db.close();self.temp.cleanup()
    def signal(self,key='s1',reason='REJECT_LOW_EDGE'):
        self.store.event(book(),self.t);self.store.flush(self.t)
        d=dict(symbol='BTCUSDT',timestamp=self.t,close=100,VWAP=101,SMA20=101,zReturn=-2.5,priceZ=-2.3,ATR=.2,
               volatility=.1,decision=reason,activityScore=80,activityPercentile=90,sourceBar={'time':0})
        with self.store.lock,self.store.db:self.shadow.capture(key,d,self.t,'NORMAL',50)
        return d

    def test_spread_history_and_imbalance(self):
        for i in range(61):
            stamp=self.t-60000+i*1000
            self.store.event(book(i+1),stamp);self.store.flush(stamp)
        f=self.store.features('BTCUSDT',self.t)
        self.assertAlmostEqual(f['spreadMedian1m'],.02)
        self.assertAlmostEqual(f['spreadMedian5m'],.02)
        self.assertEqual(f['spreadSamples'],61)
        self.assertEqual(f['bookImbalance'],.5)
        self.assertEqual(f['bookImbalance3m'],.5)
        self.assertEqual(f['spreadRatio'],1)
        self.assertIsNone(f['marketDataTimestamp'])

    def test_spread_shock(self):
        for i in range(40):self.store.event(book(i+1),self.t-40000+i*1000);self.store.flush(self.t-40000+i*1000)
        self.store.event(book(100,bid=99.95,ask=100.05),self.t)
        self.assertIn('REJECT_SPREAD_SHOCK',self.store.features('BTCUSDT',self.t)['diagnosticRejections'])

    def test_trade_aggressor_direction_counts_and_imbalance(self):
        minute=self.t//60000*60000-60000
        self.store.event(trade(1,minute+1,qty=3,maker=False,count=2),minute+1)
        self.store.event(trade(2,minute+2,qty=1,maker=True,count=1),minute+2)
        self.store.flush(self.t)
        f=self.store.features('BTCUSDT',self.t)
        self.assertEqual(f['flow']['buyAggressiveVolume'],3)
        self.assertEqual(f['flow']['sellAggressiveVolume'],1)
        self.assertEqual(f['tradeFlowImbalance'],.5)
        self.assertAlmostEqual(f['tradeCountImbalance'],1/3)
        self.assertEqual(f['flow']['totalTradeCount'],3)
        self.assertFalse(f['flow']['complete'])

    def test_duplicates_gap_and_invalid_book(self):
        self.assertTrue(self.store.event(trade(1,self.t),self.t))
        self.assertFalse(self.store.event(trade(1,self.t),self.t))
        self.store.event(trade(3,self.t),self.t)
        self.assertEqual(self.store.counters['tradeStreamGap'],1)
        self.assertFalse(self.store.event(book(bid=101,ask=100),self.t))
        self.assertEqual(self.store.counters['invalidEvents'],1)

    def test_complete_trade_minute_persisted_not_just_displayed(self):
        minute=self.t//60000*60000-60000
        self.store.event(trade(1,minute-1000),minute-1000)
        self.store.event(trade(2,minute+1000),minute+1000)
        self.store.flush(self.t)
        row=self.store.db.execute('SELECT data FROM trade_flow WHERE symbol=? AND time=?',('BTCUSDT',minute)).fetchone()
        self.assertTrue(json.loads(row[0])['complete'])

    def test_shadow_touch_is_not_trade_through(self):
        self.signal()
        self.store.event(trade(1,self.t+500,price=99.99,maker=True),self.t+500)
        self.store.event(book(2),self.t+1000);self.shadow.advance(self.t+1000)
        obs=json.loads(self.store.db.execute('SELECT data FROM shadow_observations WHERE horizon=1').fetchone()[0])
        self.assertTrue(obs['touch']);self.assertFalse(obs['tradeThrough']);self.assertEqual(obs['status'],'OBSERVED')
        self.store.event(trade(2,self.t+1500,price=99.98,maker=True),self.t+1500)
        self.store.event(book(3),self.t+2000);self.shadow.advance(self.t+2000)
        obs=json.loads(self.store.db.execute('SELECT data FROM shadow_observations WHERE horizon=2').fetchone()[0])
        self.assertTrue(obs['tradeThrough']);self.assertLess(obs['maxAdverseExcursion'],0)

    def test_shadow_signal_idempotency_and_rejected_tracking(self):
        self.signal();self.signal()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shadow_signals').fetchone()[0],1)
        self.store.event(book(2,bid=101,ask=101.02),self.t+60000);self.shadow.advance(self.t+60000)
        report=self.shadow.report(self.t+60000)
        row=next(r for r in report['filterEffectiveness'] if r['horizonSeconds']==60)
        self.assertEqual(row['signalsRejected'],1);self.assertEqual(row['hypotheticalWins'],1)
        self.assertEqual(self.p.state['cash'],100)
        self.assertEqual(self.p.snapshot()['trades'],[])

    def test_missed_deadline_is_censored_not_future_fill(self):
        self.signal();self.store.event(book(2),self.t+10000);self.shadow.advance(self.t+10000)
        obs=json.loads(self.store.db.execute('SELECT data FROM shadow_observations WHERE horizon=1').fetchone()[0])
        self.assertEqual(obs['status'],'CENSORED');self.assertIsNone(obs['tradeThrough'])

    def test_stale_book_and_gap_reject_diagnostics(self):
        self.signal()
        self.assertIn('REJECT_STALE_DATA',self.store.features('BTCUSDT',self.t+10000)['diagnosticRejections'])
        self.store.connection('test',False,reconnect=True)
        self.store.connection('test',True);self.store.event(book(2),self.t+1000);self.shadow.advance(self.t+1000)
        obs=json.loads(self.store.db.execute('SELECT data FROM shadow_observations WHERE horizon=1').fetchone()[0])
        self.assertEqual(obs['status'],'CENSORED')

    def test_restart_persistence_dedup_and_interrupted_shadows(self):
        self.signal();self.store.event(trade(10,self.t),self.t);self.store.flush(self.t)
        self.shadow.paper.close();self.store.db.close()
        self.store=MicrostructureStore(self.root/'micro.sqlite3',self.config)
        self.shadow=ShadowExecution(self.store,self.root/'paper.sqlite3',paper_config())
        self.assertFalse(self.store.event(trade(10,self.t),self.t))
        self.assertEqual(len(self.shadow.pending),1)
        self.assertFalse(self.shadow.pending['s1']['continuous'])

    def test_edge_buckets_and_separate_fee_costs(self):
        self.assertEqual([edge_bucket(x) for x in (.9,1,1.5,2,3,5,None)],['<1.0','1.0–1.5','1.5–2.0','2.0–3.0','3.0–5.0','>=5.0','UNKNOWN'])
        result=FeeSchedule(self.config).estimate(.02,1)
        self.assertAlmostEqual(result['totalExecutionCost'],.26)
        self.assertAlmostEqual(result['costAsPctOfGrossEdge'],26)

    def test_websocket_reconnect_offline(self):
        connector=Mock(side_effect=OSError('offline test'))
        worker=PublicStreamWorker('mock',['btcusdt@bookTicker'],self.store,connector)
        with patch.object(worker.stop,'wait',side_effect=[False,True]):worker.run()
        self.assertEqual(connector.call_count,2)
        self.assertEqual(self.store.counters['websocketReconnects'],2)

    def test_subscription_frames_bounded_and_paced(self):
        streams=[f'market{i}usdt@kline_1m' for i in range(506)]
        chunks=list(subscription_batches(streams))
        self.assertEqual(sum(map(len,chunks)),506)
        self.assertTrue(all(len(json.dumps(c).encode())<4000 for c in chunks))
        worker=PublicStreamWorker('mock',streams,self.store,Mock())
        socket=Mock()
        with patch.object(worker.stop,'wait',return_value=False) as wait:
            count=worker.send_control(socket,'SUBSCRIBE',streams,0)
        self.assertEqual(count,6);self.assertEqual(wait.call_count,6)
        self.assertTrue(all(call.args==(.5,) for call in wait.call_args_list))

    def test_volatility_and_market_regimes_are_deterministic(self):
        with self.store.db:
            self.store.db.executemany('INSERT INTO feature_history VALUES(?,?,?)',[('BTCUSDT',self.t-(i+1)*60000,i/100) for i in range(40)])
        regime,pct=self.shadow._regime('BTCUSDT',self.t,.5)
        self.assertEqual((regime,pct),('EXTREME',100))
        self.shadow.btc=dict(timestamp=self.t,zReturn=4)
        self.assertEqual(self.shadow.market_regime(self.t),'SHOCK')
        self.shadow.btc=dict(timestamp=self.t,zReturn=0,trendStrength=3,EMA20=100,EMA50=101)
        self.assertEqual(self.shadow.market_regime(self.t),'TREND_DOWN')

    def test_latency_is_measured_only_with_prior_candle_receipt(self):
        d=self.signal('first')
        # Separate test signal with causally prior WS close receipt and telemetry.
        stamp=(self.t//60000-2)*60000
        event=dict(e='kline',s='BTCUSDT',E=self.t-100,k=dict(t=stamp,T=stamp+59999,o='100',h='101',l='99',c='100',v='10',q='1000',n=1,i='1m',x=True))
        self.store.event(event,self.t-50)
        with self.store.db:
            self.store.db.execute('INSERT INTO execution_timestamps VALUES(?,?,?)',('timed',self.t-20,json.dumps(dict(decisionTimestamp=self.t-20,orderCreatedTimestamp=self.t-20))))
            self.shadow.capture('timed',{**d,'sourceBar':{'time':stamp}},self.t,'NORMAL',50)
        s=json.loads(self.store.db.execute("SELECT data FROM shadow_signals WHERE id='timed'").fetchone()[0])
        self.assertEqual(s['marketDataLatency'],50)
        self.assertEqual(s['decisionLatency'],30)
        self.assertEqual(s['totalSignalLatency'],80)

    def test_fill_proxy_requires_sample_size_and_excludes_censoring(self):
        self.signal()
        self.store.event(trade(1,self.t+500,price=99.98,maker=True),self.t+500)
        self.store.event(book(2),self.t+1000);self.shadow.advance(self.t+1000)
        report=self.shadow.report(self.t+1000)
        row=report['makerProbability']['spreadBucket'][0]
        self.assertEqual(row['samples'],1);self.assertEqual(row['tradeThroughRate'],1)
        self.assertIsNone(row['estimatedMakerFillProbability'])
        self.assertEqual(report['groups']['activityDecile']['10']['trades'],0)
        self.assertIsNone(report['groups']['activityDecile']['10']['netExpectancy'])

    def test_only_closed_klines_and_ws_cache_avoids_rest(self):
        b=bars(101)[-1]
        event=dict(e='kline',s='BTCUSDT',E=b['end']+1,k=dict(t=b['time'],T=b['end'],o='100',h='100.2',l='99.8',c='100',v='10000',q='1000000',n=1000,i='1m',x=False))
        self.assertFalse(self.store.event(event,b['end']+1))
        event['k']['x']=True;self.assertTrue(self.store.event(event,b['end']+1))
        get=Mock(side_effect=AssertionError('REST must not be called'))
        runner=AdaptiveRunner(get,Mock(),self.root/'runner')
        try:
            runner.candle_cache=self.store;runner.portfolio.save_bars('BTCUSDT',bars(100))
            result=runner._history('BTCUSDT',b['time'],b['end']+1)
            self.assertEqual(result,bars(101));get.assert_not_called()
        finally:runner.portfolio.db.close()

    def test_ws_gap_uses_only_missing_rest_range(self):
        runner=AdaptiveRunner(Mock(),Mock(),self.root/'runner')
        try:
            runner.candle_cache=self.store;runner.portfolio.save_bars('BTCUSDT',bars(100))
            b=bars(101)[-1]
            runner.get.return_value=[[b['time'],100,100.2,99.8,100,10000,b['end'],1000000,1000]]
            self.assertEqual(runner._history('BTCUSDT',b['time'],b['end']+1),bars(101))
            self.assertEqual(runner.get.call_args.args[1]['startTime'],b['time'])
        finally:runner.portfolio.db.close()

    def test_closed_ws_candle_with_measured_clock_offset(self):
        self.store.clock_drift=200
        b=bars(101)[-1]
        event=dict(e='kline',s='BTCUSDT',E=b['end']+1,k=dict(t=b['time'],T=b['end'],o='100',h='100.2',l='99.8',c='100',v='10000',q='1000000',n=1000,i='1m',x=True))
        self.assertTrue(self.store.event(event,b['end']-99))

    def test_read_only_bridge_cannot_change_paper_database(self):
        with self.assertRaises(sqlite3.OperationalError):self.shadow.paper.execute('DELETE FROM portfolio')

    def test_retention_preserves_shadow_validation(self):
        self.signal()
        self.store.flush(self.t+2000000)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM book_samples').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shadow_signals').fetchone()[0],1)

    def test_paper_results_identical_with_observer(self):
        other=PaperPortfolio(self.root/'other.sqlite3',paper_config());events=[]
        other.telemetry=events.extend
        decision=dict(signal='BUY',decision='PLACE_LIMIT_MAKER',positionSize=.1,entry=99.99,stop=99,target=101,expectedEdge=1,expectedCost=.26)
        market=dict(symbol='BTCUSDT',activityScore=99,activityPercentile=99,spreadPct=.02,rangeToSpread=100)
        try:
            for n in range(101,105):
                with patch('adaptive_portfolio.entry_decision',return_value=copy.deepcopy(decision)):
                    self.p.process({'BTCUSDT':bars(n)},[market],n*60000)
                with patch('adaptive_portfolio.entry_decision',return_value=copy.deepcopy(decision)):
                    other.process({'BTCUSDT':bars(n)},[market],n*60000)
            self.assertEqual(self.p.snapshot(),other.snapshot())
            self.assertEqual(len(self.p.snapshot()['trades']),1)
            self.assertEqual(len(events),4)
        finally:other.db.close()

    def test_bad_telemetry_cannot_roll_back_committed_paper(self):
        self.p.telemetry=Mock(side_effect=RuntimeError('observer unavailable'))
        with self.assertLogs(level='WARNING'):
            self.p.process({'BTCUSDT':bars(101)},[],6060000)
        self.assertEqual(self.p.state['cursors']['BTCUSDT'],6000000)


import sqlite3
if __name__=='__main__':unittest.main()
