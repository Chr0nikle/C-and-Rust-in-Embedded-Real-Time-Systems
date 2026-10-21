"""Unit tests for the benchmark campaign driver and status monitor.

Synthetic marker/result trees only; no research results and no hardware.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"
if str(BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS))

import campaign as fc  # noqa: E402

cs = fc


def write_result(root: Path, build_id: str, run_id: str, rtos: str, board: str,
                 language: str, *, mtime=None) -> Path:
    directory = root / "results" / "campaigns" / build_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"benchmark-run-{run_id}.json"
    path.write_text(json.dumps({
        "rtos": rtos, "board": board, "language": language,
        "build_id": build_id, "repetitions": 100, "warmup": 10,
        "ticks_per_second": 1000000,
        "samples": [{"case": 0, "index": 0, "ticks": 100, "variant": 0}],
    }))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class ComboDoneTests(unittest.TestCase):
    def test_bare_marker_is_done(self):
        self.assertTrue(cs.combo_done("a-b-c", {"a-b-c"}, 10))

    def test_all_run_markers_are_done(self):
        markers = {f"a-b-c-run{k}" for k in range(10)}
        self.assertTrue(cs.combo_done("a-b-c", markers, 10))

    def test_partial_run_markers_are_pending(self):
        markers = {f"a-b-c-run{k}" for k in range(9)}
        self.assertFalse(cs.combo_done("a-b-c", markers, 10))

    def test_no_runs_configured_needs_bare_marker(self):
        self.assertFalse(cs.combo_done("a-b-c", set(), 0))


class CampaignRunsTests(unittest.TestCase):
    def _with_log(self, lines):
        original = cs._log_lines
        cs._log_lines = lambda: lines
        self.addCleanup(setattr, cs, "_log_lines", original)

    def test_parses_runs_from_latest_start_line(self):
        self._with_log([
            "=== campaign start: 10 units (5 combos x 2 runs), pending=10 skip=0 ===",
            "noise",
            "=== campaign start: 450 units (45 combos x 10 runs), pending=3 skip=447 ===",
        ])
        self.assertEqual(cs.campaign_runs(), 10)

    def test_missing_start_line_defaults_to_zero(self):
        self._with_log(["unrelated log line"])
        self.assertEqual(cs.campaign_runs(), 0)


class CampaignDirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rtos-campaign-status-test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("DONE", "LOG", "LIVE_DIR", "COMBOS"):
            self.addCleanup(setattr, cs, name, getattr(cs, name))
        (self.root / "state/done").mkdir(parents=True)
        (self.root / "state/live").mkdir()
        (self.root / "metadata").mkdir()
        (self.root / "metadata/campaign-config.json").write_text(json.dumps({
            "matched_combinations": [
                {"rtos": "freertos", "board": "esp32c6", "language": "c"},
                {"rtos": "nuttx", "board": "stm32h723", "language": "rust"},
            ],
        }))

    def test_configure_campaign_uses_supervisor_layout_and_combos(self):
        cs.configure_campaign(self.root)
        self.assertEqual(cs.DONE, self.root / "state/done")
        self.assertEqual(cs.LOG, self.root / "campaign.log")
        self.assertEqual(cs.LIVE_DIR, self.root / "state/live")
        self.assertEqual(cs.COMBOS, [
            ("freertos", "esp32c6", "c"),
            ("nuttx", "stm32h723", "rust"),
        ])

    def test_missing_campaign_directory_is_rejected(self):
        with self.assertRaises(SystemExit):
            cs.configure_campaign(self.root / "missing")


class ResultIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rtos-campaign-test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("PROJECT", "RESULTS", "DONE"):
            self.addCleanup(setattr, fc, name, getattr(fc, name))
        fc.PROJECT = self.root
        fc.RESULTS = self.root / "results" / "campaigns"
        fc.DONE = self.root / ".campaign-done"
        fc.DONE.mkdir(parents=True, exist_ok=True)

    def test_result_index_prefers_newest_matching_unit(self):
        write_result(self.root, "rivet-stm32f4-bench-a", "1", "rivet",
                     "stm32f4discovery", "rust", mtime=1000)
        newest = write_result(self.root, "rivet-stm32f4-bench-b", "2", "rivet",
                              "stm32f4discovery", "rust", mtime=2000)
        index = fc.result_index()
        self.assertEqual(index[("rivet", "stm32f4discovery", "rust")], str(newest))

    def test_collect_records_resolves_legacy_ok_marker(self):
        write_result(self.root, "rivet-stm32f4-bench-a", "7", "rivet",
                     "stm32f4discovery", "rust", mtime=1000)
        (fc.DONE / "rivet-stm32f4discovery-rust-run0").write_text("ok\n")
        records, missing = fc.collect_records([("rivet", "stm32f4discovery", "rust")], 1)
        self.assertEqual(missing, [])
        self.assertEqual(
            records[0]["result"],
            "results/campaigns/rivet-stm32f4-bench-a/benchmark-run-7.json",
        )

    def test_collect_records_reports_absent_marker(self):
        records, missing = fc.collect_records([("freertos", "pico2", "c")], 1)
        self.assertEqual(records, [])
        self.assertEqual(missing, ["freertos/pico2/c run=0"])

    def test_collect_records_rejects_invalid_or_misattributed_result(self):
        path = write_result(self.root, "rivet-stm32f4-bench-a", "7", "rivet",
                            "stm32f4discovery", "rust")
        (fc.DONE / "rivet-stm32f4discovery-rust-run0").write_text(
            "results/campaigns/rivet-stm32f4-bench-a/benchmark-run-7.json\n"
        )
        payload = json.loads(path.read_text())
        payload["samples"] = []
        path.write_text(json.dumps(payload))
        records, missing = fc.collect_records(
            [("rivet", "stm32f4discovery", "rust")], 1
        )
        self.assertEqual(records, [])
        self.assertEqual(missing, ["rivet/stm32f4discovery/rust run=0"])

        payload["samples"] = [{"case": 0, "index": 0, "ticks": 1}]
        payload["board"] = "stm32h723"
        path.write_text(json.dumps(payload))
        records, missing = fc.collect_records(
            [("rivet", "stm32f4discovery", "rust")], 1
        )
        self.assertEqual(records, [])
        self.assertEqual(missing, ["rivet/stm32f4discovery/rust run=0"])


class CaseSelectionTests(unittest.TestCase):
    def test_parse_cases_normalizes_and_deduplicates(self):
        self.assertEqual(fc.parse_cases("19"), [19])
        self.assertEqual(fc.parse_cases("19, 2,19"), [2, 19])
        self.assertIsNone(fc.parse_cases(None))

    def test_parse_cases_rejects_empty_and_out_of_range(self):
        with self.assertRaises(SystemExit):
            fc.parse_cases("")
        with self.assertRaises(SystemExit):
            fc.parse_cases("29")

    def test_case_selection_accepts_exact_case19_budget(self):
        data = {"cases": [19], "repetitions": 100, "warmup": 10,
                "samples": [{"case": 19, "index": i, "ticks": 1} for i in range(100)]}
        self.assertIsNone(fc.case_selection_error(data, [19], 10))

    def test_case_selection_rejects_full_suite_result(self):
        data = {"cases": [18, 19], "repetitions": 100, "warmup": 10,
                "samples": [{"case": 19, "index": 0, "ticks": 1}]}
        self.assertIn("case selection mismatch", fc.case_selection_error(data, [19], 10))

    def test_case_selection_rejects_wrong_sample_count(self):
        data = {"cases": [19], "repetitions": 100, "warmup": 10,
                "samples": [{"case": 19, "index": 0, "ticks": 1}]}
        self.assertIn("sample count mismatch", fc.case_selection_error(data, [19], 10))

    def test_case_selection_rejects_wrong_warmup(self):
        data = {"cases": [19], "repetitions": 1, "warmup": 9,
                "samples": [{"case": 19, "index": 0, "ticks": 1}]}
        self.assertIn("warmup mismatch", fc.case_selection_error(data, [19], 10))


class MeasurementValidityTests(unittest.TestCase):
    def test_case19_zero_and_wrapped_durations_are_flagged(self):
        data = {"samples": [
            {"case": 19, "index": 0, "ticks": 100},
            {"case": 19, "index": 1, "ticks": 0},
            {"case": 19, "index": 2, "ticks": (1 << 64) - 16},
        ]}
        issues = fc.measurement_issues(data)
        self.assertEqual([i["reason"] for i in issues],
                         ["zero_duration", "wrapped_duration"])
        self.assertEqual([i["ticks"] for i in issues], [0, (1 << 64) - 16])

    def test_other_cases_are_not_given_case19_validity_rules(self):
        data = {"samples": [
            {"case": 3, "index": 0, "ticks": 0},
            {"case": 27, "index": 0, "ticks": (1 << 64) - 1},
        ]}
        self.assertEqual(fc.measurement_issues(data), [])

    def test_collect_records_preserves_raw_invalid_samples(self):
        with tempfile.TemporaryDirectory(prefix="rtos-campaign-validity ") as temporary:
            root = Path(temporary)
            for name in ("PROJECT", "RESULTS", "DONE"):
                self.addCleanup(setattr, fc, name, getattr(fc, name))
            fc.PROJECT = root
            fc.RESULTS = root / "results" / "campaigns"
            fc.DONE = root / ".campaign-done"
            fc.DONE.mkdir(parents=True)
            path = write_result(root, "rivet-stm32f4-bench-a", "7", "rivet",
                                "stm32f4discovery", "rust")
            payload = json.loads(path.read_text())
            payload["samples"] = [{"case": 19, "index": 0, "ticks": 0}]
            path.write_text(json.dumps(payload))
            (fc.DONE / "rivet-stm32f4discovery-rust-run0").write_text(
                "results/campaigns/rivet-stm32f4-bench-a/benchmark-run-7.json\n"
            )
            records, missing = fc.collect_records(
                [("rivet", "stm32f4discovery", "rust")], 1
            )
            self.assertEqual(missing, [])
            self.assertEqual(records[0]["measurement_status"], "invalid_samples")
            self.assertEqual(records[0]["sample_issues"][0]["ticks"], 0)


class ConsoleRoutingTests(unittest.TestCase):
    def test_manifest_selector_wins_and_must_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            board = project / "boards" / "nrf52840dk"
            board.mkdir(parents=True)
            port = project / "console"
            port.write_text("")
            (board / "board.toml").write_text(
                f'[serial]\nport_selector = "{port}"\n'
            )
            self.assertEqual(fc.resolve_board_port(project, "nrf52840dk"), str(port))
            port.unlink()
            with self.assertRaises(ValueError):
                fc.resolve_board_port(project, "nrf52840dk")

    def test_esp32c6_requires_exactly_one_by_id_console(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            board = project / "boards" / "esp32c6"
            board.mkdir(parents=True)
            (board / "board.toml").write_text('[serial]\nport_selector = ""\n')
            one = "/dev/serial/by-id/c6"
            self.assertEqual(
                fc.resolve_board_port(project, "esp32c6", c6_matches=[one]), one
            )
            for matches in ([], [one, "/dev/serial/by-id/other"]):
                with self.assertRaises(ValueError):
                    fc.resolve_board_port(
                        project, "esp32c6", c6_matches=matches
                    )


class CombinationSelectionTests(unittest.TestCase):
    def test_exact_combination_filter(self):
        combos = [("freertos", "stm32f4discovery", "c"),
                  ("freertos", "stm32f4discovery", "rust"),
                  ("zephyr", "stm32f4discovery", "c")]
        self.assertEqual(
            fc.select_combinations(combos, None, ["freertos/stm32f4discovery/c"]),
            [("freertos", "stm32f4discovery", "c")],
        )
        self.assertEqual(len(fc.select_combinations(combos, "freertos", None)), 2)
        with self.assertRaises(SystemExit):
            fc.select_combinations(combos, None, ["nuttx/absent/c"])


class C6PowerCycleTests(unittest.TestCase):
    def _completed(self, stdout=""):
        return mock.Mock(returncode=0, stdout=stdout, stderr="")

    @mock.patch.object(fc.Path, "exists", return_value=True)
    @mock.patch.object(fc.subprocess, "run")
    def test_refuses_to_cycle_a_port_without_the_c6(self, run, _exists):
        run.return_value = self._completed("Port 1: 0103 power [0483:3748]")
        self.assertFalse(fc.power_cycle_c6("/dev/serial/by-id/c6"))
        self.assertEqual(run.call_count, 1)
        self.assertIn("status", run.call_args.args[0])

    @mock.patch.object(fc, "_wait_for_port", return_value=True)
    @mock.patch.object(fc.time, "sleep")
    @mock.patch.object(fc.Path, "exists", return_value=True)
    @mock.patch.object(fc.subprocess, "run")
    def test_cycles_only_after_verifying_c6_identity(self, run, _exists,
                                                     _sleep, _wait):
        run.side_effect = [
            self._completed("Port 1: [303a:1001 Espressif USB JTAG]"),
            self._completed(),
            self._completed(),
        ]
        self.assertTrue(fc.power_cycle_c6("/dev/serial/by-id/c6"))
        actions = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual(actions, ["status", "off", "on"])


class FreshCampaignTests(unittest.TestCase):
    def test_clear_resume_markers_only_selected_combinations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "done"; live = root / "live"
            done.mkdir(); live.mkdir()
            selected = done / "freertos-stm32f4discovery-c-run0"
            other = done / "zephyr-stm32f4discovery-c-run0"
            selected.write_text("old")
            other.write_text("keep")
            (live / "freertos-stm32f4discovery-c.log").write_text("log")
            original_done, original_live = fc.DONE, fc.LIVE_DIR
            fc.DONE, fc.LIVE_DIR = done, live
            try:
                removed = fc.clear_resume_markers([("freertos", "stm32f4discovery", "c")])
            finally:
                fc.DONE, fc.LIVE_DIR = original_done, original_live
            self.assertEqual(removed, 1)
            self.assertFalse(selected.exists())
            self.assertTrue(other.exists())

    def test_dry_run_with_fresh_leaves_resume_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "done"; live = root / "live"
            done.mkdir(); live.mkdir()
            marker = done / "freertos-stm32f4discovery-c-run0"
            marker.write_text("keep")
            executable = root / "rtos"
            executable.write_text("")
            originals = (fc.DONE, fc.LIVE_DIR, fc.LOG, fc.RTOS, fc.COMBOS)
            fc.DONE, fc.LIVE_DIR = done, live
            fc.LOG = root / "campaign.log"
            fc.RTOS = executable
            fc.COMBOS = [("freertos", "stm32f4discovery", "c")]
            try:
                rc = fc.campaign_main(["--fresh", "--dry-run"])
            finally:
                fc.DONE, fc.LIVE_DIR, fc.LOG, fc.RTOS, fc.COMBOS = originals
            self.assertEqual(rc, 0)
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
