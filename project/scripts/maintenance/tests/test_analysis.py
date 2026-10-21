"""Unit tests for the comparison statistics; synthetic data only, no research results."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "analyze.py"
)
spec = importlib.util.spec_from_file_location("compare_benchmarks", SCRIPT)
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


def write_run(root: Path, build_id: str, run_id: str, samples: dict, *, reps=1000, footprint=None):
    directory = root / build_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocol": 1,
        "kind": "benchmark",
        "run_id": run_id,
        "build_id": build_id,
        "cases": sorted(samples),
        "repetitions": reps,
        "warmup": 100,
        "ticks_per_second": 1000000,
        "memory_high_water": None,
        "static_footprint": footprint,
        "samples": [
            {"case": case, "index": i, "ticks": ticks}
            for case, series in samples.items()
            for i, ticks in enumerate(series)
        ],
    }
    (directory / f"benchmark-{run_id}.json").write_text(json.dumps(payload))


class StatsTests(unittest.TestCase):
    def test_percentile_is_linear_interpolation(self):
        self.assertEqual(compare.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(compare.percentile(list(range(1, 101)), 99), 99)
        self.assertEqual(compare.percentile([7], 99), 7)
        # Interpolated midpoint, not nearest-rank: 50th of [0, 10] is 5.
        self.assertEqual(compare.percentile([0, 10], 50), 5)

    def test_median_abs_deviation_ignores_tail(self):
        self.assertEqual(compare.median_abs_deviation([10] * 99 + [1000]), 0)

    def test_case_stats_reports_tail_spread_and_drift(self):
        stats = compare.case_stats([10] * 99 + [1000], 1_000_000)
        self.assertEqual(stats["n"], 100)
        self.assertEqual(stats["p90_us"], 10)
        self.assertEqual(stats["p95_us"], 10)
        # Linear interpolation: p99 of [10]*99+[1000] (n=100) sits between the
        # 98th (10) and 99th (1000) samples -> ~20.
        self.assertEqual(stats["p99_us"], 20)
        self.assertEqual(stats["max_us"], 1000)
        self.assertEqual(stats["outliers_above_p99"], 1)
        self.assertEqual(stats["mad_us"], 0)
        self.assertAlmostEqual(stats["drift_us"], 19.8)
        self.assertGreater(stats["cv"], 0)
        self.assertEqual(stats["min_us"], 10)

    def test_case_stats_scales_by_ticks_per_second(self):
        stats = compare.case_stats([168], 168_000_000)
        self.assertAlmostEqual(stats["mean_us"], 1.0)


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rtos compare test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_load_latest_picks_newest_run_and_computes_stats(self):
        write_run(self.root, "zephyr-c6-bench-aaaa1111", "run-1", {0: [10, 10]})
        write_run(self.root, "zephyr-c6-bench-aaaa1111", "run-2", {0: [20, 20]})
        reports, _ = compare.load_latest(compare.collect_runs(self.root))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["cases"]["0"]["n"], 2)
        self.assertAlmostEqual(reports[0]["cases"]["0"]["mean_us"], 20.0)

    def test_run_variation_ignores_other_builds(self):
        write_run(self.root, "nuttx-stm32f4-bench-bbbb2222", "run-1", {0: [10, 10]})
        write_run(self.root, "nuttx-stm32f4-bench-cccc3333", "run-2", {0: [90, 90]})
        write_run(self.root, "nuttx-stm32f4-bench-bbbb2222", "run-3", {0: [30, 30]})
        runs = compare.collect_runs(self.root)
        reports, _ = compare.load_latest(runs)
        self.assertEqual(reports[0]["build_id"], "nuttx-stm32f4-bench-bbbb2222")
        variation = compare.run_variation(runs, reports)
        entry = variation["nuttx/stm32f4discovery/c"]["cases"]["0"]
        self.assertEqual(entry["runs"], 2)
        self.assertEqual(entry["min_us"], 10.0)
        self.assertEqual(entry["max_us"], 30.0)

    def test_run_variation_omitted_for_single_run(self):
        write_run(self.root, "nuttx-stm32f4-bench-bbbb2222", "run-1", {0: [10, 10]})
        runs = compare.collect_runs(self.root)
        reports, _ = compare.load_latest(runs)
        self.assertEqual(compare.run_variation(runs, reports), {})

    def test_load_latest_required_cases_skips_newer_partial_run(self):
        write_run(self.root, "zephyr-c6-bench-aaaa1111", "run-1", {0: [10, 10], 9: [5, 5]})
        write_run(self.root, "zephyr-c6-bench-aaaa1111", "run-2", {0: [20, 20]})
        reports, excluded = compare.load_latest(
            compare.collect_runs(self.root), [0, 9]
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(set(reports[0]["cases"]), {"0", "9"})
        self.assertEqual(excluded, [])

    def test_load_latest_required_cases_reports_excluded_path(self):
        write_run(self.root, "zephyr-c6-bench-aaaa1111", "run-1", {0: [10, 10]})
        reports, excluded = compare.load_latest(
            compare.collect_runs(self.root), [0, 9]
        )
        self.assertEqual(reports, [])
        self.assertEqual(excluded[0][:3], ("zephyr", "esp32c6", "c"))
        self.assertEqual(excluded[0][3], [0])

    def test_render_markdown_includes_tail_and_variation(self):
        write_run(self.root, "rivet-stm32f4-bench-dddd4444", "run-1", {0: [10, 10]})
        write_run(self.root, "rivet-stm32f4-bench-dddd4444", "run-2", {0: [12, 12]})
        runs = compare.collect_runs(self.root)
        reports, _ = compare.load_latest(runs)
        markdown = compare.render_markdown(reports, compare.run_variation(runs, reports))
        self.assertIn("Tail latency and spread", markdown)
        self.assertIn("Run-to-run variation", markdown)
        self.assertIn("rivet/stm32f4discovery/rust", markdown)

    def test_render_markdown_includes_static_footprint(self):
        footprint = {
            "images": [
                {
                    "image": "zephyr.elf",
                    "text": 4096,
                    "rodata": 512,
                    "data": 64,
                    "bss": 2048,
                    "flash": 4672,
                    "ram": 2112,
                }
            ]
        }
        write_run(
            self.root,
            "zephyr-stm32f4-bench-eeee5555",
            "run-1",
            {0: [10, 10]},
            footprint=footprint,
        )
        reports, _ = compare.load_latest(compare.collect_runs(self.root))
        markdown = compare.render_markdown(reports)
        self.assertIn("Static image footprint", markdown)
        self.assertIn("zephyr.elf", markdown)
        self.assertEqual(reports[0]["static_footprint"], footprint)


if __name__ == "__main__":
    unittest.main()
