import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from futures_paper import FuturesPaperAccount, load_config, position_key
from futures_public import FuturesPublicClient, PUBLIC_PATHS


def bars(prices,start=0):
    return [dict(time=start+i*60000,end=start+i*60000+59999,open=price,
        high=price*1.002,low=price*.998,close=price,volume=100.)
        for i,price in enumerate(prices)]


def market(symbol='BTCUSDT',price=100.,time=3_660_000,history=None,rate=.0001):
    history=history or bars([price]*61)
    return dict(symbol=symbol,bars=history,timestamp=time,contractPrice=price,
        markPrice=price,indexPrice=price,bid=price-.01,ask=price+.01,
        fundingRate=rate,nextFundingTime=time+28_800_000,openInterest=1000.,
        settledFunding=[])


def entry(symbol,side,price=100.,family='L1_TREND'):
    sign=1 if side=='LONG' else -1
    return dict(action='OPEN_'+side,symbol=symbol,family=family,
        positionSize=.1,stop=price*(1-sign*.02),target=price*(1+sign*.04),
        expectedNetReturn=.02,expectedMove=.04,fullRoundTripCost=.002,
        decisionTime=3_660_000)


class FuturesPaperTests(unittest.TestCase):
    def make_account(self,directory):
        account=FuturesPaperAccount(Path(directory)/'binance_futures_paper.sqlite3')
        account.promoted=frozenset({'L1_TREND','S1_TREND'})  # Test-only validated fixtures.
        account.edge_evidence={'L1_TREND':{'probabilityNetProfit':.7},
                               'S1_TREND':{'probabilityNetProfit':.7}}
        return account

    def test_risk_profile_migration_preserves_paper_history(self):
        with tempfile.TemporaryDirectory() as directory:
            old={**load_config(),'maxSymbolExposure':.25}
            path=Path(directory)/'binance_futures_paper.sqlite3'
            account=FuturesPaperAccount(path,old)
            with account.db:
                account.state['wallet']=75.
                account.state['cursor']=3_600_000
                account._persist()
            account.close()
            migrated=FuturesPaperAccount(path)
            self.assertEqual(migrated.state['wallet'],75.)
            self.assertEqual(migrated.state['cursor'],3_600_000)
            self.assertEqual(migrated.config['maxSymbolExposure'],.4)
            self.assertEqual(migrated.snapshot()['events'][0]['action'],'RISK_PROFILE_MIGRATION')
            migrated.close()

    def test_pause_all_persists_and_vetoes_new_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            account=self.make_account(directory)
            m=market()
            self.assertTrue(account.set_paused(True,3_660_000))
            self.assertEqual(account._entry_veto(entry('BTCUSDT','LONG'),m,
                {'BTCUSDT':m}),'PAPER_PAUSED')
            account.close()
            restored=FuturesPaperAccount(Path(directory)/'binance_futures_paper.sqlite3')
            self.assertTrue(restored.snapshot()['pausedAll'])
            self.assertTrue(restored.set_paused(False,3_720_000))
            self.assertFalse(restored.snapshot()['pausedAll'])
            restored.close()

    def test_long_keep_close_positive_net_and_bid_ask_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            m=market()
            with a.db:
                self.assertIsNone(a._open(entry('BTCUSDT','LONG'),m,{'BTCUSDT':m},3_660_000))
                a._persist()
            p=a.state['positions'][position_key('BTCUSDT','LONG')]
            self.assertEqual(p['positionSide'],'LONG')
            self.assertGreater(p['entryPrice'],m['ask'])
            higher=market(price=105.,time=3_720_000,history=bars([100.]*60+[105.],start=60_000))
            self.assertTrue(a.cycle(dict(serverTime=3_720_000,markets={'BTCUSDT':higher})))
            self.assertEqual(a.snapshot()['lastDecision']['action'],'KEEP_LONG')
            self.assertGreater(a.snapshot()['positions']['BTCUSDT|LONG']['netLiquidationPnL'],0)
            with a.db:
                trade=a._close('BTCUSDT','LONG',higher,3_720_001,'AGENT_CLOSE')
                a._persist()
            self.assertGreater(trade['netPnL'],0)
            self.assertEqual(a.snapshot()['events'][0]['action'],'CLOSE LONG')
            self.assertEqual(a.snapshot()['events'][0]['executionSide'],'SELL')
            self.assertAlmostEqual(trade['grossTradingPnL']+trade['fundingPnL']-trade['fees']-
                trade['spreadCost']-trade['slippageCost'],trade['netPnL'])
            a.close()

    def test_short_keep_close_positive_net_and_buy_to_close(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            m=market()
            d=entry('BTCUSDT','SHORT',family='S1_TREND')
            with a.db:
                self.assertIsNone(a._open(d,m,{'BTCUSDT':m},3_660_000))
                a._persist()
            p=a.state['positions']['BTCUSDT|SHORT']
            self.assertLess(p['entryPrice'],m['bid'])
            lower=market(price=95.,time=3_720_000,history=bars([100.]*60+[95.],start=60_000))
            self.assertTrue(a.cycle(dict(serverTime=3_720_000,markets={'BTCUSDT':lower})))
            self.assertEqual(a.snapshot()['lastDecision']['action'],'KEEP_SHORT')
            with a.db:
                trade=a._close('BTCUSDT','SHORT',lower,3_720_001,'AGENT_CLOSE')
                a._persist()
            self.assertGreater(trade['netPnL'],0)
            self.assertEqual(a.snapshot()['events'][0]['action'],'CLOSE SHORT')
            self.assertEqual(a.snapshot()['events'][0]['executionSide'],'BUY')
            a.close()

    def test_losing_short_mark_stop_is_controlled(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            m=market()
            with a.db:
                a._open(entry('BTCUSDT','SHORT',family='S1_TREND'),m,{'BTCUSDT':m},3_660_000)
                a._persist()
            losing=market(price=103.,time=3_720_000,history=bars([100.]*60+[103.],start=60_000))
            a.cycle(dict(serverTime=3_720_000,markets={'BTCUSDT':losing}))
            self.assertEqual(a.snapshot()['positions'],{})
            self.assertLess(a.snapshot()['trades'][0]['netPnL'],0)
            self.assertEqual(a.snapshot()['trades'][0]['reason'],'MARK_STOP')
            a.close()

    def test_profit_target_closes_long_and_short_after_minimum_hold(self):
        for side, price, family in [('LONG',105.,'L1_TREND'),
                                    ('SHORT',95.,'S1_TREND')]:
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                a=self.make_account(directory)
                entry_market=market()
                with a.db:
                    self.assertIsNone(a._open(entry('BTCUSDT',side,family=family),
                        entry_market,{'BTCUSDT':entry_market},3_660_000))
                    a._persist()
                later=market(price=price,time=3_960_000)
                with a.db:
                    exits=a._mark_risk({'BTCUSDT':later},3_960_000)
                    a._persist()
                self.assertEqual(exits[0]['reason'],'PROFIT_TARGET')
                self.assertEqual(a.snapshot()['positions'],{})
                a.close()

    def test_profitable_position_reduces_once_without_full_close(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            first=market()
            with a.db:
                a._open(entry('BTCUSDT','LONG'),first,{'BTCUSDT':first},3_660_000)
                a._persist()
            later=market(price=103.,time=3_960_000)
            with a.db:
                actions=a._mark_risk({'BTCUSDT':later},3_960_000)
                a._persist()
            self.assertEqual(actions[0]['reason'],'PROFIT_PARTIAL_25')
            self.assertTrue(a.state['positions']['BTCUSDT|LONG']['partialTaken'])
            quantity=a.state['positions']['BTCUSDT|LONG']['qty']
            with a.db:
                self.assertEqual(a._mark_risk({'BTCUSDT':later},4_020_000),[])
            self.assertEqual(a.state['positions']['BTCUSDT|LONG']['qty'],quantity)
            a.close()

    def test_counterfactual_uses_later_closed_candle_once(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            previous=dict(time=3_660_000,candleTime=3_600_000,
                          action='CLOSE_LONG',symbol='BTCUSDT',decisionMid=100.)
            with a.db:
                a.db.execute('INSERT INTO futures_decisions VALUES(?,?)',
                             (3_600_000,json.dumps(previous)))
            history=bars([100.]*60+[105.],start=300_000)
            later=market(price=105.,time=3_960_000,history=history)
            snapshot=dict(serverTime=3_960_000,markets={'BTCUSDT':later})
            self.assertTrue(a.cycle(snapshot))
            outcome=a.snapshot()['outcomes'][0]
            self.assertEqual(outcome['horizonMinutes'],5)
            self.assertEqual(outcome['verdict'],'PREMATURE_CLOSE_LOSS')
            self.assertGreater(outcome['counterfactualNetReturn'],0)
            self.assertFalse(a.cycle(snapshot))
            self.assertEqual(len(a.snapshot()['outcomes']),1)
            a.close()

    def test_settled_funding_uses_event_mark_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            m=market()
            with a.db:
                a._open(entry('BTCUSDT','SHORT',family='S1_TREND'),m,{'BTCUSDT':m},3_660_000)
                a._persist()
            next_m=market(price=99.,time=3_720_000,history=bars([100.]*60+[99.],start=60_000))
            next_m['settledFunding']=[dict(fundingTime=3_690_000,fundingRate=.01,markPrice=101.)]
            a.cycle(dict(serverTime=3_720_000,markets={'BTCUSDT':next_m}))
            funding=a.snapshot()['fundingPnL']
            self.assertGreater(funding,0)
            self.assertFalse(a.cycle(dict(serverTime=3_720_000,markets={'BTCUSDT':next_m})))
            self.assertEqual(a.snapshot()['fundingPnL'],funding)
            a.close()

    def test_cost_and_same_symbol_opposite_veto(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            m=market()
            d=entry('BTCUSDT','LONG')
            self.assertEqual(a._entry_veto({**d,'expectedMove':.001},m,{'BTCUSDT':m}),
                             'NO_POSITIVE_NET_EDGE')
            self.assertEqual(a._entry_veto({**d,'fullRoundTripCost':.0001},m,{'BTCUSDT':m}),
                             'UNDERSTATED_FUTURES_COST')
            stale=copy.deepcopy(m)
            stale['bars']=stale['bars'][:-1]
            self.assertEqual(a._entry_veto(d,stale,{'BTCUSDT':stale}),
                             'STALE_CLOSED_CANDLE')
            with a.db:
                a._open(d,m,{'BTCUSDT':m},3_660_000)
                a._persist()
            self.assertEqual(a._entry_veto(entry('BTCUSDT','SHORT',family='S1_TREND'),
                m,{'BTCUSDT':m}),'SAME_SYMBOL_OPPOSITE_EXPOSURE')
            a.close()

    def test_hedge_reduces_btc_beta_exposure_more_than_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            btc_prices=[100-i*.4+(i%2)*1 for i in range(61)]
            sol_prices=[100-i*.5+(i%2)*1.2 for i in range(61)]
            btc=market('BTCUSDT',btc_prices[-1],history=bars(btc_prices))
            sol=market('SOLUSDT',sol_prices[-1],history=bars(sol_prices))
            with a.db:
                a._open(entry('SOLUSDT','LONG',sol['ask']),sol,
                        {'BTCUSDT':btc,'SOLUSDT':sol},3_660_000)
                a._persist()
            pick=dict(symbol='BTCUSDT',directionCandidate='SHORT',family='S1_TREND',
                expectedNetEdge=.03,expectedMove=.04,fullRoundTripCost=.002,volatility=.01)
            hedge=a._hedge_candidate([pick],{'BTCUSDT':btc,'SOLUSDT':sol},3_660_000)
            self.assertIsNotNone(hedge)
            self.assertGreater(hedge['riskReductionEstimate'],hedge['costEstimate'])
            self.assertLess(hedge['hedgeRatio'],1)
            self.assertIsNone(a._open(hedge,btc,{'BTCUSDT':btc,'SOLUSDT':sol},3_660_000))
            self.assertTrue(a.state['positions']['BTCUSDT|SHORT']['hedge'])
            self.assertLess(a._risk_exposure({'BTCUSDT':btc,'SOLUSDT':sol})[1],
                a.state['positions']['SOLUSDT|LONG']['qty']*sol['markPrice'])
            a.close()

    def test_public_client_rejects_private_endpoint(self):
        client=FuturesPublicClient(reader=lambda path,params: {})
        self.assertIn('/fapi/v1/premiumIndex',PUBLIC_PATHS)
        with self.assertRaises(ValueError):client.get('/fapi/v1/account')

    def test_public_futures_snapshot_keeps_prices_and_funding_separate(self):
        calls=[]
        def reader(path,params):
            calls.append((path,params))
            if path=='/fapi/v1/time':return {'serverTime':3_660_000}
            if path=='/fapi/v1/exchangeInfo':return {'symbols':[dict(symbol='BTCUSDT',
                status='TRADING',contractType='PERPETUAL',quoteAsset='USDT')]}
            if path=='/fapi/v1/klines':return [[b['time'],str(b['open']),str(b['high']),
                str(b['low']),str(b['close']),str(b['volume']),b['end']]
                for b in bars([100.]*61)]
            if path=='/fapi/v1/ticker/bookTicker':return dict(bidPrice='99.99',askPrice='100.01',
                bidQty='5',askQty='6',time=3_659_000)
            if path=='/fapi/v1/ticker/price':return {'price':'100.02'}
            if path=='/fapi/v1/premiumIndex':return dict(markPrice='100.03',
                indexPrice='99.97',lastFundingRate='0.0001',nextFundingTime=4_000_000,
                time=3_659_000)
            if path=='/fapi/v1/openInterest':return dict(openInterest='1234',time=3_659_000)
            if path=='/fapi/v1/depth':return dict(bids=[['99.99','4']],asks=[['100.01','3']])
            if path=='/fapi/v1/aggTrades':return [dict(q='2',m=False,T=3_659_000)]
            if path=='/fapi/v1/fundingRate':return [dict(fundingTime=3_650_000,
                fundingRate='0.001',markPrice='101')]
            raise AssertionError(path)
        data=FuturesPublicClient(reader).snapshot(('BTCUSDT',),{'BTCUSDT':3_640_000})
        m=data['markets']['BTCUSDT']
        self.assertEqual((m['contractPrice'],m['markPrice'],m['indexPrice']),(100.02,100.03,99.97))
        self.assertEqual(m['settledFunding'][0]['fundingTime'],3_650_000)
        self.assertEqual(m['openInterest'],1234)
        self.assertTrue(all(path in PUBLIC_PATHS for path,_ in calls))

    def test_equal_long_short_edge_stays_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            a=self.make_account(directory)
            m=market()
            candidates=[dict(symbol='BTCUSDT',directionCandidate='LONG',family='L1_TREND',
                expectedNetEdge=.02,expectedMove=.04,fullRoundTripCost=.002,
                volatility=.01,probabilityNetProfit=.7),
                dict(symbol='BTCUSDT',directionCandidate='SHORT',family='S1_TREND',
                expectedNetEdge=.0205,expectedMove=.04,fullRoundTripCost=.002,
                volatility=.01,probabilityNetProfit=.7)]
            with patch.object(a,'_opportunities',return_value=candidates):
                self.assertTrue(a.cycle(dict(serverTime=3_660_000,markets={'BTCUSDT':m})))
            self.assertEqual(a.snapshot()['lastDecision']['action'],'FLAT')
            self.assertEqual(a.snapshot()['positions'],{})
            a.close()

    def test_public_snapshot_retries_once_after_minute_boundary(self):
        calls={'time':0}
        def reader(path,params):
            if path=='/fapi/v1/time':
                calls['time']+=1
                return {'serverTime':3_660_000 if calls['time']==1 else 3_720_000}
            if path=='/fapi/v1/exchangeInfo':return {'symbols':[dict(symbol='BTCUSDT',
                status='TRADING',contractType='PERPETUAL',quoteAsset='USDT')]}
            if path=='/fapi/v1/klines':
                start=0 if calls['time']==1 else 60_000
                return [[b['time'],str(b['open']),str(b['high']),str(b['low']),
                    str(b['close']),str(b['volume']),b['end']] for b in bars([100.]*61,start)]
            if path=='/fapi/v1/ticker/bookTicker':return dict(bidPrice='99.99',askPrice='100.01',
                bidQty='5',askQty='5')
            if path=='/fapi/v1/ticker/price':return {'price':'100'}
            if path=='/fapi/v1/premiumIndex':return dict(markPrice='100',indexPrice='100',
                lastFundingRate='0.0001',nextFundingTime=4_000_000,
                time=3_659_000 if calls['time']==1 else 3_719_000)
            if path=='/fapi/v1/openInterest':return dict(openInterest='1000',
                time=3_659_000 if calls['time']==1 else 3_719_000)
            if path=='/fapi/v1/depth':return dict(bids=[['99','1']],asks=[['101','1']])
            if path=='/fapi/v1/aggTrades':return []
            raise AssertionError(path)
        data=FuturesPublicClient(reader).snapshot(('BTCUSDT',))
        self.assertEqual(data['serverTime'],3_720_000)
        self.assertEqual(data['markets']['BTCUSDT']['bars'][-1]['time'],3_660_000)
        self.assertEqual(calls['time'],4)


if __name__=='__main__':
    unittest.main()
