"""Unit tests for the replication run-level statistics; synthetic data only."""
import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ANALYSIS = (
    Path(__file__).resolve().parents[3] / "thesis"
    / "analysis"
)
sys.path.insert(0, str(ANALYSIS / "benchmark"))
sys.path.insert(0, str(ANALYSIS / "integration"))

import replication as rs  # noqa: E402

TPS = 1_000_000


def run_doc(case: int, ticks: list, *, tps: int = TPS) -> dict:
    return {
        "ticks_per_second": tps,
        "samples": [{"case": case, "index": i, "ticks": t}
                    for i, t in enumerate(ticks)],
    }


def entry(run: int, doc: dict) -> dict:
    return {"run": run, "path": f"results/x/benchmark-run-{run}.json",
            "build_id": "x", "data": doc}


class CaseSummaryTests(unittest.TestCase):
    def test_pooled_percentiles(self):
        # Pooled samples 1..5 microseconds: median 3, p95 4.8, p99 4.96.
        entries = [entry(k, run_doc(0, [v])) for k, v in enumerate([1, 2, 3, 4, 5])]
        summary = rs.case_summary(entries, 0)
        self.assertEqual(summary["n_runs"], 5)
        self.assertAlmostEqual(summary["median"], 3.0)
        self.assertAlmostEqual(summary["p95"], 4.8, places=6)
        self.assertAlmostEqual(summary["p99"], 4.96, places=6)
        self.assertAlmostEqual(summary["jitter"], 1.96, places=6)
        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(summary["max"], 5.0)
        self.assertEqual(summary["unit"], "us")

    def test_identical_samples_have_zero_spread(self):
        entries = [entry(k, run_doc(0, [10, 10, 10])) for k in range(4)]
        summary = rs.case_summary(entries, 0)
        self.assertEqual(summary["median"], 10.0)
        self.assertEqual(summary["p99"], 10.0)
        self.assertEqual(summary["jitter"], 0.0)

    def test_non_latency_case_keeps_raw_counts(self):
        entries = [entry(k, run_doc(28, [937_500])) for k in range(3)]
        summary = rs.case_summary(entries, 28)
        self.assertEqual(summary["unit"], "count")
        self.assertEqual(summary["median"], 937_500.0)

    def test_case19_converts_each_observation_to_bytes_per_second(self):
        doc = run_doc(19, [1_000_000, 2_000_000], tps=1_000_000)
        summary = rs.case_summary([entry(0, doc)], 19)
        self.assertEqual(summary["unit"], "B/s")
        self.assertAlmostEqual(
            summary["median"],
            (rs.TP_BYTES / 1.0 + rs.TP_BYTES / 2.0) / 2.0,
        )
        self.assertEqual(summary["sample_count"], 2)

    def test_case19_uses_each_runs_timer_frequency(self):
        docs = [
            run_doc(19, [1_000_000], tps=1_000_000),
            run_doc(19, [2_000_000], tps=2_000_000),
        ]
        summary = rs.case_summary([entry(0, docs[0]), entry(1, docs[1])], 19)
        self.assertAlmostEqual(summary["median"], rs.TP_BYTES)
        self.assertAlmostEqual(summary["values"][0], rs.TP_BYTES)
        self.assertAlmostEqual(summary["values"][1], rs.TP_BYTES)

    def test_case19_percentiles_are_taken_after_conversion(self):
        ticks = [1, 2, 3, 4, 5]
        doc = run_doc(19, ticks, tps=1_000_000)
        summary = rs.case_summary([entry(0, doc)], 19)
        rates = sorted(rs.TP_BYTES * 1_000_000 / t for t in ticks)
        raw_p99 = rs.percentile(sorted(ticks), 99)
        self.assertAlmostEqual(summary["p99"], rs.percentile(rates, 99))
        self.assertNotAlmostEqual(
            summary["p99"], rs.TP_BYTES * 1_000_000 / raw_p99
        )

    def test_case19_counts_zero_and_wrapped_durations_as_invalid(self):
        wrapped = (1 << 64) - 4_240_244_583
        doc = run_doc(19, [1_000_000, 0, wrapped], tps=1_000_000)
        summary = rs.case_summary([entry(0, doc)], 19)
        self.assertEqual(summary["invalid_count"], 2)
        self.assertEqual(summary["sample_count"], 3)
        self.assertAlmostEqual(summary["invalid_fraction"], 2 / 3)
        self.assertAlmostEqual(summary["median"], rs.TP_BYTES)

    def test_case19_all_invalid_has_no_summary_values(self):
        doc = run_doc(19, [0], tps=1_000_000)
        summary = rs.case_summary([entry(0, doc)], 19)
        self.assertEqual(summary["invalid_count"], 1)
        self.assertIsNone(summary["median"])
        self.assertEqual(summary["n_runs"], 0)

    def test_case11_excludes_uint64_miss_sentinel(self):
        doc = run_doc(11, [100, rs.MISS_SENTINEL, 200])
        summary = rs.case_summary([entry(0, doc)], 11)
        self.assertEqual(summary["miss_count"], 1)
        self.assertEqual(summary["sample_count"], 3)
        self.assertAlmostEqual(summary["miss_fraction"], 1 / 3)
        # Median of the two clean samples (100, 200) in microseconds.
        self.assertEqual(summary["median"], 150.0)

    def test_case27_excludes_no_benefit_sentinel(self):
        doc = run_doc(27, [4000, rs.MISS_SENTINEL, 6000])
        summary = rs.case_summary([entry(0, doc)], 27)
        self.assertEqual(summary["miss_count"], 1)
        self.assertEqual(summary["sample_count"], 3)
        self.assertAlmostEqual(summary["miss_fraction"], 1 / 3)
        self.assertEqual(summary["unit"], "ticks")
        # Median of the two positive samples (4000, 6000) in raw ticks.
        self.assertEqual(summary["median"], 5000.0)


class NormalizeTests(unittest.TestCase):
    def test_board_and_language_tokens_are_mapped(self):
        self.assertEqual(
            rs.normalize_key("freertos", "stm32f4discovery", "c"),
            ("freertos", "stm32f4", "bench"),
        )
        self.assertEqual(
            rs.normalize_key("rivet", "stm32f4discovery", "rust"),
            ("rivet", "stm32f4", "rust"),
        )


class LoadPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rtos-replication-test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(setattr, rs, "PROJECT_ROOT", rs.PROJECT_ROOT)
        self.addCleanup(setattr, rs, "REPO_ROOT", rs.REPO_ROOT)
        rs.PROJECT_ROOT = self.root
        rs.REPO_ROOT = self.root

    def _write(self, rel: str, doc: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"rtos": "rivet", "board": "stm32f4discovery", "language": "rust",
               "build_id": "rivet-stm32f4discovery-rust-test", **doc}
        path.write_text(json.dumps(doc))

    def test_load_paths_groups_runs_and_normalizes_key(self):
        bundle = {"units": [
            {"rtos": "rivet", "board": "stm32f4discovery", "language": "rust",
             "run": 0, "result": "results/b/benchmark-run-1.json"},
            {"rtos": "rivet", "board": "stm32f4discovery", "language": "rust",
             "run": 1, "result": "results/b/benchmark-run-2.json"},
        ]}
        self._write("results/b/benchmark-run-1.json", run_doc(0, [10, 20, 30]))
        self._write("results/b/benchmark-run-2.json", run_doc(0, [12, 22, 32]))
        paths = rs.load_paths(bundle)
        key = ("rivet", "stm32f4", "rust")
        self.assertIn(key, paths)
        self.assertEqual(len(paths[key]["runs"]), 2)
        self.assertEqual(paths[key]["coverage"], [0])
        self.assertEqual(paths[key]["cases"][0]["n_runs"], 2)


class LegacyCampaignCompatibilityTests(unittest.TestCase):
    def test_v1_campaign_record_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "results" / "campaigns" / "legacy"
            run_dir.mkdir(parents=True)
            (run_dir / "benchmark-run-1.json").write_text(json.dumps({
                "build_id": "legacy-build",
                "ticks_per_second": 1000,
                "samples": [{"case": 0, "index": 0, "ticks": 100, "variant": 0}],
            }))
            bundle = {
                "schema_version": 1,
                "units": [{
                    "rtos": "freertos",
                    "board": "stm32f4discovery",
                    "language": "c",
                    "run": 0,
                    "result": "results/campaigns/legacy/benchmark-run-1.json",
                }],
            }
            original = rs.REPO_ROOT
            rs.REPO_ROOT = root
            try:
                paths = rs.load_paths(bundle)
            finally:
                rs.REPO_ROOT = original
            self.assertIn(("freertos", "stm32f4", "bench"), paths)


if __name__ == "__main__":
    unittest.main()
