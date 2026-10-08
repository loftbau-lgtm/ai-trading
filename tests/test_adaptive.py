import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from adaptive import load_config, features, rank_universe, window_metrics, validate_bars, entry_decision, report, resample_closed
from adaptive_portfolio import PaperPortfolio
from adaptive_validation import walk_forward


def history(n=1441,start=0):
    return [dict(time=(start+i)*60000,end=(start+i)*60000+59999,open=100,high=100.2,low=99.8,
                 close=100,volume=10000,turnover=1000000,trades=1000) for i in range(n)]


def market(symbol='ETHUSDT'):
    return dict(symbol=symbol,quote='USDT',top=True,activityScore=90,activityPercentile=95,spreadPct=.02,
                rangeToSpread=100,tradeIntensity=100,windows=window_metrics(history(),.02))


def candidate():
    return dict(zReturn=-2.5,priceZ=-2.5,volumeZ=0,ATR=.2,trendStrength=.2,EMA20=100,EMA50=100,
                return5m=0,distanceFromVWAP=-1,VWAP=101,SMA20=101,close=100,volatility=.05)


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=load_config()
        self.path=Path(self.temp.name)/'paper.sqlite3'
        self.p=PaperPortfolio(self.path,self.config)
    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def test_closed_contiguous_finite_bars_only(self):
        bars=history(100)
        validate_bars(bars,6000000)
        for mutate in (lambda b:b[-1].update(end=6000001),lambda b:b[-1].update(close=math.nan),
                       lambda b:b[-1].update(volume=-1),lambda b:b[-1].update(low=102)):
            bad=copy.deepcopy(bars);mutate(bad)
            with self.assertRaises(ValueError):validate_bars(bad,6000000)
        with self.assertRaises(ValueError):validate_bars(bars[:50]+bars[51:],6000000)

    def test_features_fixed_lookback_and_zero_variance(self):
        self.assertEqual(features(history()),features(history()[-100:]))
        f=features(history());self.assertEqual(f['zReturn'],0);self.assertEqual(f['volumeZ'],0)
        self.assertEqual(f['VWAP'],100)
        json.dumps(f,allow_nan=False)

    def test_resampling_excludes_partial_intervals(self):
        self.assertEqual(len(resample_closed(history(16),15)),1)
        self.assertEqual(resample_closed(history(16),15)[0]['end'],899999)
        self.assertEqual(resample_closed(history(14),15),[])

    def test_risk_limits_and_daily_reset(self):
        self.p.state['cash']=97.9
        self.p.state['day']=0
        self.p._risk(60000)
        self.assertEqual(self.p.state['kill'],'REJECT_DAILY_LOSS')
        self.p._risk(86400000)
        self.assertIsNone(self.p.state['kill'])
        self.p.state['cash']=94
        self.p._risk(86460000)
        self.assertEqual(self.p.state['kill'],'REJECT_DRAWDOWN')
        self.p._risk(172800000)
        self.assertEqual(self.p.state['kill'],'REJECT_DRAWDOWN')

    def test_four_windows_and_percentile_quote_isolation(self):
        a,b=market('A'),market('B');b['quote']='BTC'
        b['windows']['24h']['turnover']=1
        rows=rank_universe([a,b])
        self.assertAlmostEqual(rows[0]['activityScore'],rows[1]['activityScore'])
        self.assertEqual(a['windows']['15m']['trades'],15000)
        self.assertEqual(a['tradeIntensity'],1000)

    def test_entry_filters_and_positive_target(self):
        f,m=candidate(),market()
        self.assertEqual(entry_decision(f,m,self.config,100,0)['signal'],'BUY')
        for key,value,reason in [('spreadPct',None,'REJECT_SPREAD'),('activityPercentile',50,'REJECT_LOW_ACTIVITY'),('top',False,'REJECT_OUTSIDE_TOP_N')]:
            changed={**m,key:value}
            self.assertEqual(entry_decision(f,changed,self.config,100,0)['decision'],reason)
        self.assertEqual(entry_decision({**f,'distanceFromVWAP':1},m,self.config,100,0)['decision'],'REJECT_VWAP_DISTANCE')
        self.assertEqual(entry_decision({**f,'VWAP':100,'SMA20':100},m,self.config,100,0)['decision'],'REJECT_LOW_EDGE')
        self.assertEqual(entry_decision({**f,'trendStrength':10},m,self.config,100,0)['decision'],'REJECT_TREND')

    def test_idempotency_restart_and_config_identity(self):
        bars=history(101)
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6060000)
        first=self.p.snapshot()
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6060000)
        self.assertEqual(first,self.p.snapshot())
        self.p.db.close();self.p=PaperPortfolio(self.path,self.config)
        self.assertEqual(first,self.p.snapshot())
        changed={**self.config,'MAX_SPREAD':.2}
        with self.assertRaises(ValueError):PaperPortfolio(self.path,changed)

    def test_transaction_rolls_back_cursor_on_error(self):
        bars=history(101)
        with patch('adaptive_portfolio.entry_decision',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6060000)
        self.assertEqual(self.p.state['cursors'],{})
        self.assertEqual(self.p.snapshot()['decisions'],[])

    def test_stale_context_and_manual_kill_prevent_entries(self):
        bars=history(101)
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6060000,context_ok=False)
        self.assertEqual(self.p.snapshot()['decisions'][0]['decision'],'REJECT_STALE_OR_INCOMPLETE_DATA')
        bars=history(102)
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6120000,manual_kill=True)
        self.assertEqual(self.p.snapshot()['decisions'][0]['decision'],'REJECT_MANUAL_KILL')

    def test_maker_later_bar_stop_and_cost_reconciliation(self):
        bars=history(101)
        # Controlled valid signal with sufficient allocation for 5 USDT minimum.
        self.p.config={**self.config,'RISK_PER_TRADE':.005}
        decision=entry_decision(candidate(),market(),self.p.config,100,0)
        decision['positionSize']=.1
        with patch('adaptive_portfolio.entry_decision',return_value=decision):
            self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6060000)
        self.assertEqual(len(self.p.state['pending']),1)
        self.assertEqual(len(self.p.state['positions']),0)
        bars=history(102)
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6120000)
        self.assertEqual(len(self.p.state['positions']),1)
        stop=self.p.state['positions']['BTCUSDT']['stop']
        bars=history(103);bars[-1].update(low=stop-1,close=stop,open=100)
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6180000)
        trade=self.p.snapshot()['trades'][0]
        self.assertEqual(trade['reason'],'STOP_LOSS')
        self.assertAlmostEqual(trade['netPnL'],trade['grossPnL']-trade['fees']-trade['spreadCost']-trade['slippageCost'])
        self.assertAlmostEqual(self.p.state['cash'],100+trade['netPnL'])
        self.assertEqual(len(self.p.state['positions']),0)
        self.p.process({'BTCUSDT':bars},[market('BTCUSDT')],6180000)
        self.assertEqual(len(self.p.snapshot()['trades']),1)

    def test_catchup_never_uses_present_ranking_for_old_entries(self):
        self.p.process({'BTCUSDT':history(101)},[market('BTCUSDT')],6060000)
        self.p.process({'BTCUSDT':history(105)},[market('BTCUSDT')],6300000)
        d=self.p.snapshot()['decisions']
        self.assertEqual(len(d),5)
        self.assertTrue(all(r['decision']=='REJECT_STALE_OR_INCOMPLETE_DATA' for r in d[1:4]))

    def test_mean_exit_uses_later_maker_fill_without_double_cost(self):
        d=entry_decision(candidate(),market(),self.config,100,0)
        d['positionSize']=.1
        with patch('adaptive_portfolio.entry_decision',return_value=d):
            self.p.process({'BTCUSDT':history(101)},[market('BTCUSDT')],6060000)
        self.p.process({'BTCUSDT':history(102)},[market('BTCUSDT')],6120000)
        self.p.process({'BTCUSDT':history(103)},[market('BTCUSDT')],6180000)
        self.assertEqual(len(self.p.snapshot()['trades']),0)
        self.assertIn('exitLimit',self.p.state['positions']['BTCUSDT'])
        self.p.process({'BTCUSDT':history(104)},[market('BTCUSDT')],6240000)
        trade=self.p.snapshot()['trades'][0]
        self.assertEqual(trade['reason'],'MEAN_REVERSION_MAKER')
        self.assertEqual(trade['spreadCost'],0)
        self.assertEqual(trade['slippageCost'],0)
        self.assertAlmostEqual(self.p.state['cash'],100+trade['netPnL'])

    def test_report_no_fake_readiness_and_net_expectancy(self):
        self.assertIsNone(self.p.snapshot()['metrics']['expectancyNet'])
        trade=dict(netPnL=1,grossPnL=2,fees=.5,spreadCost=.3,slippageCost=.2,holdingMinutes=10,
                   expectedEdge=1,expectedCost=.5,symbol='BTCUSDT',activityDecile=10,zBucket='-2',volatilityRegime='low')
        result=report([trade,{**trade,'netPnL':-2}],[],self.config)
        self.assertEqual(result['expectancyNet'],-.5)
        self.assertFalse(result['paperGatePassed']);self.assertFalse(result['liveReady'])

    def test_walk_forward_rejects_overlap_and_future_data(self):
        with self.assertRaises(ValueError):walk_forward([dict(now=2),dict(now=1)],[self.config],3,4,5)
        frames=[dict(now=i,histories={},ranking=[]) for i in (1,3,5)]
        result=walk_forward(frames,[self.config],2,4,6)
        self.assertEqual(result['status'],'INSUFFICIENT_TRAIN_EVIDENCE')


if __name__=='__main__':unittest.main()
