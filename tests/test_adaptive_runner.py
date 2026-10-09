import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from adaptive_runner import AdaptiveRunner


def history(n):
    return [dict(time=i*60000,end=i*60000+59999,open=100,high=101,low=99,close=100,
                 volume=10,turnover=1000,trades=10) for i in range(n)]


class RunnerTests(unittest.TestCase):
    def test_final_refresh_uses_wall_time_not_beginning_of_slow_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner=Mock()
            scanner.snapshot.return_value=dict(fresh=True,rows=[dict(symbol='BTCUSDT',quote='USDT',spreadPct=.02)])
            get=Mock(side_effect=[dict(serverTime=86460001),dict(serverTime=86520001)])
            runner=AdaptiveRunner(get,scanner,directory)
            try:
                runner._history=Mock(side_effect=lambda s,last,now:history(last//60000+1))
                runner.portfolio.process=Mock()
                with patch('adaptive_runner.time.time',side_effect=[86460.001,86460.001,86520.001]):
                    runner.cycle(threading.Event())
                args=runner.portfolio.process.call_args
                self.assertEqual(args.args[2],86520001)
                self.assertEqual(args.args[0]['BTCUSDT'][-1]['time'],86460000)
                self.assertEqual(runner._history.call_count,2)
                self.assertTrue(args.kwargs['context_ok'])
            finally:runner.close()

    def test_live_env_does_not_activate_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            runner=AdaptiveRunner(Mock(),Mock(),directory)
            try:
                with patch.dict('os.environ',{'TRADING_MODE':'LIVE','ENABLE_LIVE_ORDERS':'true'}):
                    runner.run(threading.Event())
                self.assertEqual(runner.status['state'],'blocked')
                runner.get.assert_not_called()
            finally:runner.close()

    def test_clock_skew_pauses_entries_even_with_market_data(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner=Mock()
            scanner.snapshot.return_value=dict(fresh=True,rows=[dict(symbol='BTCUSDT',quote='USDT',spreadPct=.02)])
            get=Mock(side_effect=[dict(serverTime=86460001),dict(serverTime=86520001)])
            runner=AdaptiveRunner(get,scanner,directory)
            try:
                runner._history=Mock(side_effect=lambda s,last,now:history(last//60000+1))
                runner.portfolio.process=Mock()
                runner.matrix.process=Mock()
                runner.directional.process=Mock()
                with patch('adaptive_runner.time.time',side_effect=[1.0,1.0,2.0]):
                    runner.cycle(threading.Event())
                self.assertFalse(runner.portfolio.process.call_args.kwargs['context_ok'])
                self.assertFalse(runner.matrix.process.call_args.kwargs['context_ok'])
                self.assertFalse(runner.status['clockOk'])
            finally:runner.close()
