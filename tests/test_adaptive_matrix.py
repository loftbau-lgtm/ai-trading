import tempfile
import unittest
from pathlib import Path

from adaptive import load_config
from adaptive_matrix import build_generation_zero, AdaptiveMatrix


class AdaptiveMatrixTests(unittest.TestCase):
    def test_generation_zero_unique_and_bounded(self):
        variants = build_generation_zero(load_config())
        self.assertEqual(len(variants), 30)
        self.assertEqual(len({v["variantId"] for v in variants}), 30)
        self.assertEqual(variants[0]["name"], "CONTROL")
        self.assertTrue(all(v["configHash"] for v in variants))

    def test_variant_configs_are_independent(self):
        variants = build_generation_zero(load_config())
        variants[1]["config"]["Z_RETURN_ENTRY"] = -99
        self.assertNotEqual(variants[2]["config"]["Z_RETURN_ENTRY"], -99)

    def test_matrix_is_paper_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            m = AdaptiveMatrix(Path(td), load_config())
            snap = m.snapshot()
            self.assertEqual(snap["mode"], "PAPER")
            self.assertFalse(snap["liveReady"])
            self.assertEqual(snap["variantCount"], 30)

    def test_independent_balances(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            m = AdaptiveMatrix(Path(td), load_config())
            a=m.variants["AMR-V001"]["portfolio"]
            b=m.variants["AMR-V002"]["portfolio"]
            a.state["cash"]=90
            self.assertEqual(b.state["cash"], 100.0)


if __name__ == "__main__":
    unittest.main()
