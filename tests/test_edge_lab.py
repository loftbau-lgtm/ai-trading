import tempfile
import unittest
from pathlib import Path

from edge_lab import base_round_trips, candidate, promotion_gate
from engine import Engine, STRATEGIES


class EdgeLabTests(unittest.TestCase):
    def test_base_costs_reconcile_and_do_not_claim_complete_spread(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Path(directory) / 'paper.sqlite3')
            name = STRATEGIES[0]
            engine.execute(name, 'BTCUSDT', 'BUY', 100, 60000)
            engine.execute(name, 'BTCUSDT', 'SELL', 101, 120000)
            rows = base_round_trips(engine.db.execute('SELECT * FROM trades ORDER BY time,id'))[name]
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]['grossPnL'] - rows[0]['fees'] - rows[0]['slippageCost'], rows[0]['netPnL'])
            result = candidate(name, 'BASE', rows, 'LONG')
            self.assertFalse(result['costModelComplete'])
            self.assertEqual(result['status'], 'INSUFFICIENT_SAMPLE')
            engine.db.close()

    def test_blocked_buy_still_allows_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Path(directory) / 'paper.sqlite3')
            name = STRATEGIES[0]
            engine.entry_allowed = lambda _: False
            engine.execute(name, 'BTCUSDT', 'BUY', 100, 60000)
            self.assertEqual(engine.db.execute('SELECT count(*) FROM trades').fetchone()[0], 0)
            engine.entry_allowed = lambda _: True
            engine.execute(name, 'BTCUSDT', 'BUY', 100, 120000)
            engine.entry_allowed = lambda _: False
            engine.execute(name, 'BTCUSDT', 'SELL', 101, 180000)
            self.assertEqual(engine.db.execute('SELECT count(*) FROM trades').fetchone()[0], 2)
            self.assertEqual(engine.db.execute('SELECT count(*) FROM positions').fetchone()[0], 0)
            engine.db.close()

    def test_promotion_requires_independent_complete_oos(self):
        evidence = dict(independentOos=True, walkForwardValidated=True,
            shadowValidated=True, costModelComplete=True,
            oosTrades=150, oosNetExpectancy=.1, oosCiLow=.02,
            oosProfitFactor=1.2, oosMaxDrawdown=.1, costStress25=.03,
            fillStress75=.02, symbolConcentration=.3, periodConcentration=.3,
            symbolCount=3, periodCount=3, selectionBiasRisk='LOW')
        self.assertFalse(promotion_gate(None))
        self.assertTrue(promotion_gate(evidence))
        self.assertFalse(promotion_gate({**evidence, 'oosCiLow': -.01}))
        self.assertFalse(promotion_gate({**evidence, 'selectionBiasRisk': 'HIGH_30_VARIANTS'}))
        self.assertFalse(promotion_gate({**evidence, 'costStress25': -.01}))
        self.assertFalse(promotion_gate({**evidence, 'oosTrades': 20}))
        self.assertFalse(promotion_gate({**evidence, 'independentOos': False}))
        self.assertFalse(promotion_gate({**evidence, 'costModelComplete': False}))

    def test_matrix_untested_variants_remain_shadow(self):
        result = candidate('variant', 'TREND', [], 'LONG', selection_bias='HIGH_30_VARIANTS')
        self.assertEqual(result['status'], 'SHADOW')
        self.assertFalse(result['activePaper'])


if __name__ == '__main__':
    unittest.main()
