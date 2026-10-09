import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_agent import FAMILIES, PortfolioDecisionAgent, external_decision, hypotheses


def bars(count=100, first=100., step=.3, start=0):
    result=[]
    for i in range(count):
        price=first+i*step
        result.append(dict(time=start+i*60000, end=start+i*60000+59999,
            open=price, high=price*1.001, low=price*.999, close=price,
            volume=100.))
    return result


class PortfolioAgentTests(unittest.TestCase):
    def test_six_sensor_families_and_no_promotion_by_default(self):
        history={'BTCUSDT':bars(), 'ETHUSDT':bars(first=80,step=.4),
                 'SOLUSDT':bars(first=20,step=.2)}
        ranking=[dict(symbol=s,spreadPct=.02) for s in history]
        proposals=hypotheses(history,ranking)
        self.assertTrue(proposals)
        self.assertTrue(all(p['family'] in FAMILIES and p['expectedCost']>0 for p in proposals))
        with tempfile.TemporaryDirectory() as directory:
            agent=PortfolioDecisionAgent(Path(directory)/'agent.sqlite3')
            self.assertTrue(agent.cycle(history,ranking,history['BTCUSDT'][-1]['end']+1))
            self.assertEqual(agent.snapshot()['lastDecision']['action'],'FLAT')
            self.assertFalse(agent.cycle(history,ranking,history['BTCUSDT'][-1]['end']+1))
            self.assertEqual(agent.snapshot()['completedTrades'],0)
            agent.close()

    def test_internal_promotion_can_open_next_candle_with_stop_and_is_restart_safe(self):
        history={'BTCUSDT':bars()}
        ranking=[dict(symbol='BTCUSDT',spreadPct=.02)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'agent.sqlite3'
            agent=PortfolioDecisionAgent(path)
            agent.promoted=frozenset({'L1_TREND'})  # Test-only validated evidence fixture.
            now=history['BTCUSDT'][-1]['end']+1
            self.assertTrue(agent.cycle(history,ranking,now))
            self.assertEqual(agent.snapshot()['lastDecision']['action'],'OPEN_LONG')
            self.assertEqual(len(agent.snapshot()['positions']),0)
            next_history={'BTCUSDT':bars(101)}
            next_now=next_history['BTCUSDT'][-1]['end']+1
            self.assertTrue(agent.cycle(next_history,ranking,next_now))
            position=agent.snapshot()['positions']['BTCUSDT']
            self.assertLess(position['stop'],position['entry'])
            self.assertGreater(position['target'],position['entry'])
            self.assertLessEqual(position['qty']*position['entry'],25.01)
            self.assertFalse(agent.cycle(next_history,ranking,next_now))
            agent.close()
            restarted=PortfolioDecisionAgent(path)
            self.assertIn('BTCUSDT',restarted.snapshot()['positions'])
            self.assertFalse(restarted.cycle(next_history,ranking,next_now))
            restarted.close()

    def test_external_cannot_bypass_hard_risk_or_no_edge(self):
        history={'BTCUSDT':bars()}
        ranking=[dict(symbol='BTCUSDT',spreadPct=.02)]
        with tempfile.TemporaryDirectory() as directory:
            agent=PortfolioDecisionAgent(Path(directory)/'agent.sqlite3')
            proposals=hypotheses(history,ranking)
            marks={'BTCUSDT':history['BTCUSDT'][-1]['close']}
            price=marks['BTCUSDT']
            decision=dict(action='OPEN_LONG',symbol='BTCUSDT',family='L1_TREND',
                          positionSize=.1,stop=price*.99,target=price*1.02)
            self.assertEqual(agent._risk_check(decision,proposals,marks,True),'NO_CONFIRMED_EDGE')
            agent.promoted=frozenset({'L1_TREND'})
            self.assertEqual(agent._risk_check({**decision,'stop':None},proposals,marks,True),
                             'INVALID_SIZE_OR_PROTECTIVE_STOP')
            self.assertEqual(agent._risk_check({**decision,'positionSize':.5},proposals,marks,True),
                             'INVALID_SIZE_OR_PROTECTIVE_STOP')
            self.assertEqual(agent._risk_check({**decision,'stop':price*1.01},proposals,marks,True),
                             'INVALID_STOP_OR_TARGET_DIRECTION')
            self.assertEqual(agent._risk_check(decision,proposals,marks,False),'STALE_DATA')
            agent.close()

    def test_external_requires_https_and_secret_then_falls_back(self):
        with patch.dict('os.environ', {'AGENT_PROVIDER':'EXTERNAL',
                                      'AGENT_API_URL':'http://example.invalid',
                                      'AGENT_API_KEY':'secret'}):
            self.assertIsNone(external_decision({'paperOnly':True}))
            with tempfile.TemporaryDirectory() as directory:
                agent=PortfolioDecisionAgent(Path(directory)/'agent.sqlite3')
                history={'BTCUSDT':bars()}
                self.assertTrue(agent.cycle(history,[dict(symbol='BTCUSDT',spreadPct=.02)],
                                            history['BTCUSDT'][-1]['end']+1))
                self.assertEqual(agent.snapshot()['externalFailures'],1)
                self.assertEqual(agent.snapshot()['lastDecision']['provider'],'LOCAL')
                agent.close()

    def test_short_position_is_stopped_and_loss_is_net_of_costs(self):
        ranking=[dict(symbol='BTCUSDT',spreadPct=.02)]
        with tempfile.TemporaryDirectory() as directory:
            agent=PortfolioDecisionAgent(Path(directory)/'agent.sqlite3')
            agent.promoted=frozenset({'S1_TREND'})
            first={'BTCUSDT':bars(step=-.3)}
            self.assertTrue(agent.cycle(first,ranking,first['BTCUSDT'][-1]['end']+1))
            self.assertEqual(agent.snapshot()['lastDecision']['action'],'OPEN_SHORT')
            second={'BTCUSDT':bars(101,step=-.3)}
            self.assertTrue(agent.cycle(second,ranking,second['BTCUSDT'][-1]['end']+1))
            position=agent.snapshot()['positions']['BTCUSDT']
            self.assertGreater(position['stop'],position['entry'])
            third=bars(102,step=-.3)
            third[-1]['high']=position['stop']*1.01
            third[-1]['close']=position['stop']*1.01
            third[-1]['low']=third[-1]['open']*.999
            self.assertTrue(agent.cycle({'BTCUSDT':third},ranking,third[-1]['end']+1))
            self.assertEqual(agent.snapshot()['completedTrades'],1)
            self.assertEqual(agent.snapshot()['positions'],{})
            self.assertEqual(agent.snapshot()['lastDecision']['riskActions'][0]['action'],'CLOSE_LOSS')
            agent.close()

    def test_version_change_requires_new_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'agent.sqlite3'
            with patch.dict('os.environ',{'AGENT_VERSION':'G0'}):
                agent=PortfolioDecisionAgent(path)
                agent.close()
            with patch.dict('os.environ',{'AGENT_VERSION':'G1'}):
                with self.assertRaisesRegex(ValueError,'new PAPER database'):
                    PortfolioDecisionAgent(path)


if __name__=='__main__':
    unittest.main()
