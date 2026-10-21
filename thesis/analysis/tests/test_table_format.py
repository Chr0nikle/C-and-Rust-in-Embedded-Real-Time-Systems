"""Percentage formatting guards for benchmark sentinel annotations."""
import importlib.util
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "tables", ANALYSIS / "benchmark" / "tables.py"
)
tables = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tables)


class PercentageFormattingTests(unittest.TestCase):
    def test_exact_zero_remains_zero(self):
        self.assertEqual(tables.format_percent(0, 1000), "0")

    def test_small_nonzero_fractions_keep_precision(self):
        self.assertEqual(tables.format_percent(1, 1000), "0.1")
        self.assertEqual(tables.format_percent(6, 1000), "0.6")
        self.assertEqual(tables.format_percent(6, 994), "0.6")
        self.assertEqual(tables.format_percent(1, 2000), "0.05")

    def test_ordinary_percentages_remain_integer(self):
        self.assertEqual(tables.format_percent(20, 1000), "2")
        self.assertEqual(tables.format_percent(130, 1000), "13")
        self.assertEqual(tables.format_percent(1000, 1000), "100")

    def test_zero_denominator_is_zero(self):
        self.assertEqual(tables.format_percent(1, 0), "0")

    def test_generated_appendix_uses_nonzero_small_percentages(self):
        generated = (ANALYSIS.parent / "generated" / "typ" / "all-results.typ").read_text()
        self.assertIn("0.1\u2009% miss, n=999", generated)
        self.assertIn("0.6\u2009% no benefit, n=994", generated)
        self.assertNotIn("0\u2009% miss, n=999", generated)
        self.assertNotIn("0\u2009% no benefit, n=999", generated)


if __name__ == "__main__":
    unittest.main()
