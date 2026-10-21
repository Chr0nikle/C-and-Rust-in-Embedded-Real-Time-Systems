"""Positive, negative and unsupported fixture tests for the safety checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from checks.abi import check_01
from checks.analysers import check_05
from checks.arrays import check_03
from checks.c_extract import extract_c
from checks.layout import check_02
from common.config import discover_c, resolve_repo_root
from checks.queues import check_04
from checks.reporting import structural_rows
from common.extractor import build_extractor, extract_rust

SAFETY = Path(__file__).resolve().parent.parent
REPO = resolve_repo_root(SAFETY)
STAGED_SOURCES = (REPO / ".local" / "source").is_dir()
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLANG_ARGS = ["-target", "arm-none-eabi", "-mcpu=cortex-m4", "-mthumb"]
TARGET = {"arch": "arm", "os": "none", "family": "none", "pointer_width": "32"}


class SafetyCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.extractor = build_extractor(SAFETY)

    def analyze(self, fixture_dir: str, c_name: str, rust_name: str):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-fixture-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        c_path = root / "firmware/common/c/src/fixture.c"
        rust_path = root / "firmware/c-kernels/freertos/rust/src/lib.rs"
        c_path.parent.mkdir(parents=True, exist_ok=True)
        rust_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / fixture_dir / c_name, c_path)
        shutil.copyfile(FIXTURES / fixture_dir / rust_name, rust_path)
        c_facts = extract_c(
            root,
            [{"path": str(c_path), "args": CLANG_ARGS}],
            [],
        )
        rust_facts = extract_rust(
            root,
            ["firmware/c-kernels/freertos/rust/src/lib.rs"],
            TARGET,
            self.extractor,
        )
        return root, c_facts, rust_facts

    def statuses(self, rows, check_id):
        return [row["status"] for row in rows if row["check_id"] == check_id]

    def test_check01_correct(self):
        root, c, rust = self.analyze("abi", "c_correct.c", "rust_correct.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        self.assertIn("PASS", self.statuses(rows, "CHECK-01"))

    def test_check01_detects_width_mismatch(self):
        root, c, rust = self.analyze("abi", "c_incorrect.c", "rust_incorrect.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-01" and row["interface"] == "abi_u32"]
        self.assertEqual(selected[0]["status"], "FAIL")
        self.assertIn("width", selected[0]["reason"])

    def test_check01_detects_signedness_mismatch(self):
        root, c, rust = self.analyze("abi", "c_signed.c", "rust_signed.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-01" and row["interface"] == "abi_signed"]
        self.assertEqual(selected[0]["status"], "FAIL")
        self.assertIn("signed", selected[0]["reason"])

    def test_check01_missing_c_declaration_is_unknown(self):
        root, c, rust = self.analyze("abi", "c_unsupported.c", "rust_unsupported.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-01" and row["interface"] == "abi_u32"]
        self.assertEqual(selected[0]["status"], "UNKNOWN")

    def test_check02_correct(self):
        root, c, rust = self.analyze("layout", "c_correct.c", "rust_correct.rs")
        rows = check_02(c, rust, repo_root=root, configuration="fixture", tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-02" and row["interface"] == "Pair"]
        self.assertEqual(selected[0]["status"], "PASS")

    def test_check02_detects_size_mismatch(self):
        root, c, rust = self.analyze("layout", "c_incorrect.c", "rust_incorrect.rs")
        rows = check_02(c, rust, repo_root=root, configuration="fixture", tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-02" and row["interface"] == "Pair"]
        self.assertEqual(selected[0]["status"], "FAIL")

    def test_check02_repr_c_unsupported_is_unknown(self):
        root, c, rust = self.analyze("layout", "c_correct.c", "rust_unsupported.rs")
        rows = check_02(c, rust, repo_root=root, configuration="fixture", tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-02" and row["interface"] == "Pair"]
        self.assertEqual(selected[0]["status"], "UNKNOWN")

    def test_check03_correct(self):
        root, c, rust = self.analyze("array", "c_correct.c", "rust_correct.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["PASS"])
        self.assertIn("1024", rows[0]["evidence"])

    def test_check03_detects_bound_exceeding_capacity(self):
        root, c, rust = self.analyze("array", "c_incorrect.c", "rust_incorrect.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["FAIL"])
        self.assertIn("2048", rows[0]["reason"])

    def test_check03_dynamic_bound_is_unknown(self):
        root, c, rust = self.analyze("array", "c_correct.c", "rust_unsupported.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["UNKNOWN"])

    def test_check04_c_correct(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_correct.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        c_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "c"]
        self.assertTrue(any(row["status"] == "PASS" for row in c_rows))

    def test_check04_c_detects_small_buffer(self):
        root, c, rust = self.analyze("queue", "c_incorrect.c", "rust_correct.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        c_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "c"]
        self.assertTrue(any(row["status"] == "FAIL" for row in c_rows))

    def test_check04_c_unresolved_item_size_is_unknown(self):
        root, c, rust = self.analyze("queue", "c_unsupported.c", "rust_correct.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        c_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "c"]
        self.assertTrue(any(row["status"] == "UNKNOWN" for row in c_rows))

    def test_check04_rust_correct(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_correct.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        rust_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "rust"]
        self.assertTrue(any(row["status"] == "PASS" for row in rust_rows))

    def test_check04_rust_detects_small_buffer(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_incorrect.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        rust_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "rust"]
        self.assertTrue(any(row["status"] == "FAIL" for row in rust_rows))

    def test_check04_rust_unresolved_item_size_is_unknown(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_unsupported.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        rust_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "rust"]
        self.assertTrue(any(row["status"] == "UNKNOWN" for row in rust_rows))

    def test_check05_is_not_checked(self):
        rows = [
            check_05(
                {"clang_c_benchmark": {"status": "completed"}, "clippy": {"status": "completed"}},
                configuration="fixture-c",
                language="c",
                clang_key="clang_c_benchmark",
                clippy=False,
                tool_version="test",
            ),
            check_05(
                {"clippy": {"status": "completed"}},
                configuration="fixture-rust",
                language="rust",
                clang_key=None,
                clippy=True,
                tool_version="test",
            ),
        ]
        self.assertEqual([row["status"] for row in rows], ["NOT_CHECKED", "NOT_CHECKED"])

    def test_structural_extraction_has_source_locations(self):
        root, c, rust = self.analyze("abi", "c_correct.c", "rust_correct.rs")
        self.assertTrue(c["functions"])
        self.assertTrue(rust["foreign_fns"])
        self.assertGreater(c["functions"][0]["line"], 0)
        self.assertGreater(rust["foreign_fns"][0]["line"], 0)


    def test_check01_unsupported_record_is_unknown(self):
        root, c, rust = self.analyze("abi", "c_record.c", "rust_record.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        selected = [row for row in rows if row["interface"] == "abi_record"]
        self.assertEqual(selected[0]["status"], "UNKNOWN")
        self.assertEqual(selected[0]["source_type_status"], "UNKNOWN")

    def test_check01_pointer_depth(self):
        root, c, rust = self.analyze("abi", "c_ptr.c", "rust_ptr.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        selected = [row for row in rows if row["interface"] == "abi_ptr"]
        self.assertEqual(selected[0]["status"], "FAIL")
        self.assertEqual(selected[0]["abi_representation_status"], "FAIL")

    def test_check01_inactive_declaration(self):
        root, c, rust = self.analyze("abi", "c_inactive.c", "rust_inactive.rs")
        rows = check_01(c, rust, configuration="fixture", pointer_width=32, tool_version="test")
        selected = [row for row in rows if row["interface"] == "abi_inactive"]
        self.assertEqual(selected[0]["status"], "NOT_CHECKED")

    def test_check03_element_return_is_not_applicable(self):
        root, c, rust = self.analyze("array", "c_element.c", "rust_element.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["NOT_CHECKED"])

    def test_check03_alias_larger_bound_fails(self):
        root, c, rust = self.analyze("array", "c_blocks.c", "rust_alias.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["FAIL"])
        self.assertIn("8", rows[0]["reason"])

    def test_check03_multiple_aliases_fail(self):
        root, c, rust = self.analyze("array", "c_blocks.c", "rust_multiple_aliases.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["FAIL"])

    def test_check03_reassignment_is_unknown(self):
        root, c, rust = self.analyze("array", "c_blocks.c", "rust_reassignment.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["UNKNOWN"])

    def test_check03_inclusive_bound_fails(self):
        root, c, rust = self.analyze("array", "c_blocks.c", "rust_inclusive.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["FAIL"])

    def test_check03_reversed_bound_is_unknown(self):
        root, c, rust = self.analyze("array", "c_blocks.c", "rust_reversed.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["UNKNOWN"])

    def test_check03_unresolved_alias_is_unknown(self):
        root, c, rust = self.analyze("array", "c_blocks.c", "rust_unresolved.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["UNKNOWN"])

    def test_check03_shadowed_constant_is_used(self):
        root, c, rust = self.analyze("array", "c_correct.c", "rust_shadowed.rs")
        rows = check_03(c, rust, configuration="fixture", tool_version="test")
        self.assertEqual(self.statuses(rows, "CHECK-03"), ["FAIL"])
        self.assertIn("2048", rows[0]["reason"])

    def test_check04_semaphore_is_not_applicable(self):
        root, c, rust = self.analyze("queue", "c_semaphore.c", "rust_semaphore.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        selected = [row for row in rows if row["check_id"] == "CHECK-04"]
        self.assertTrue(any(row["status"] == "NOT_APPLICABLE" for row in selected))
        self.assertTrue(all(row["status"] != "PASS" for row in selected))

    def test_check04_helper_alias_resolves(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_helper.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        helper = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "rust" and row["interface"].startswith("xQueueGenericSend:queue")]
        self.assertTrue(any(row["status"] == "PASS" for row in helper))

    def test_check04_struct_payload(self):
        root, c, rust = self.analyze("queue", "c_struct.c", "rust_correct.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        c_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "c"]
        self.assertTrue(any(row["status"] == "PASS" for row in c_rows))

    def test_check04_multiple_queues(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_multi.rs")
        rows = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        rust_rows = [row for row in rows if row["check_id"] == "CHECK-04" and row["language"] == "rust"]
        self.assertTrue(any(row["status"] == "PASS" and "S_Q4" in row["interface"] for row in rust_rows))
        self.assertTrue(any(row["status"] == "PASS" and "S_Q8" in row["interface"] for row in rust_rows))

    @unittest.skipUnless(STAGED_SOURCES, "staged RTOS sources unavailable; run scripts/setup/install.sh")
    def test_c_configuration_scope(self):
        root = resolve_repo_root(SAFETY)
        cfg0 = discover_c(root, 0)
        cfg1 = discover_c(root, 1)
        self.assertNotIn("-DBENCH_LANGUAGE_RUST", [item for unit in cfg0["translation_units"] for item in unit["clang_args"]])
        self.assertIn("-DBENCH_LANGUAGE_RUST", [item for unit in cfg1["translation_units"] for item in unit["clang_args"]])
        facts0 = extract_c(root, [{"path": unit["abs_path"], "args": unit["clang_args"]} for unit in cfg0["translation_units"]], [])
        facts1 = extract_c(root, [{"path": unit["abs_path"], "args": unit["clang_args"]} for unit in cfg1["translation_units"]], [])
        c0_calls = [call for call in facts0["calls"] if call["name"] == "bench_run_case" and call["file"].endswith("bench.c")]
        c1_calls = [call for call in facts1["calls"] if call["name"] == "bench_run_case" and call["file"].endswith("bench.c")]
        self.assertTrue(any(call["line"] == 3333 for call in c0_calls))
        self.assertFalse(any(call["line"] == 3333 for call in c1_calls))
        self.assertTrue(any(call["name"] == "rust_bench_run_case" for call in facts1["calls"]))
        self.assertFalse(any(call["name"] == "rust_bench_run_case" for call in facts0["calls"]))

    @unittest.skipUnless(STAGED_SOURCES, "staged RTOS sources unavailable; run scripts/setup/install.sh")
    def test_c_structural_dedup_and_classification(self):
        root = resolve_repo_root(SAFETY)
        cfg0 = discover_c(root, 0)
        cfg1 = discover_c(root, 1)
        facts0 = extract_c(root, [{"path": unit["abs_path"], "args": unit["clang_args"]} for unit in cfg0["translation_units"]], [])
        facts1 = extract_c(root, [{"path": unit["abs_path"], "args": unit["clang_args"]} for unit in cfg1["translation_units"]], [])
        c0, c1, rust = structural_rows(facts0, facts1, {}, c_benchmark_configuration="c-bench", c_support_configuration="c-support", rust_configuration="rust")
        for rows in (c0, c1):
            self.assertEqual(len(rows), len({json.dumps(row, sort_keys=True) for row in rows}))
            self.assertTrue(any(row["kind"] == "function_declaration" for row in rows))
            self.assertTrue(any(row["kind"] == "function_definition" for row in rows))
            self.assertTrue(any(row["kind"] == "allocation_call" for row in rows))
            self.assertTrue(any(row["kind"] == "kernel_api_call" for row in rows))
        self.assertTrue(any(row["kind"] == "function_pointer_declaration" for row in c1))

    @unittest.skipUnless(STAGED_SOURCES, "staged RTOS sources unavailable; run scripts/setup/install.sh")
    def test_rust_inactive_cfg_is_visible(self):
        root = resolve_repo_root(SAFETY)
        cfg = __import__("common.config", fromlist=["discover_rust"]).discover_rust(root)
        rust = extract_rust(root, cfg["files"], cfg["target"], self.extractor)
        pinned = [call for function in rust["functions"] for call in function["calls"] if call.get("callee") == "xTaskCreatePinnedToCore"]
        self.assertTrue(pinned and all(call["active"] == "false" for call in pinned))
        peer = [item for item in rust["constants"] if item["name"] == "PEER_STACK"]
        self.assertTrue(any(item["active"] == "false" for item in peer))
        self.assertTrue(any(item["active"] == "true" for item in peer))

    def test_missing_extractor_dependency_is_reported(self):
        with mock.patch("common.extractor.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                build_extractor(SAFETY)

    @unittest.skipUnless(STAGED_SOURCES, "staged RTOS sources unavailable; run scripts/setup/install.sh")
    def test_end_to_end_scope_outputs(self):
        root = resolve_repo_root(SAFETY)
        temporary = tempfile.TemporaryDirectory(prefix="uastw-end-to-end-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "out"
        completed = subprocess.run([
            sys.executable, str(SAFETY / "safety.py"), "analyze", "--repo-root", str(root),
            "--output", str(output), "--skip-analysers",
        ], cwd=SAFETY, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((output / "structural-c-benchmark.csv").is_file())
        self.assertTrue((output / "structural-c-support.csv").is_file())
        coverage_doc = json.loads((output / "coverage.json").read_text())
        self.assertEqual(set(coverage_doc["checks_by_configuration"]), {"freertos-stm32f4-c-benchmark", "freertos-stm32f4-c-support-rust", "freertos-stm32f4-rust"})
        self.assertEqual(coverage_doc["checks_by_configuration"]["freertos-stm32f4-c-benchmark"]["CHECK-04"]["NOT_APPLICABLE"], 19)
        self.assertEqual(coverage_doc["checks_by_configuration"]["freertos-stm32f4-rust"]["CHECK-04"]["NOT_APPLICABLE"], 1)
        self.assertGreater(coverage_doc["extraction_counts"]["c_benchmark"]["functions"], 0)
        manifest = json.loads((output / "manifest.json").read_text())
        for entry in manifest["outputs"]:
            import hashlib
            self.assertEqual(hashlib.sha256((output / entry["path"]).read_bytes()).hexdigest(), entry["sha256"])
        second = Path(temporary.name) / "out-2"
        completed2 = subprocess.run([
            sys.executable, str(SAFETY / "safety.py"), "analyze", "--repo-root", str(root),
            "--output", str(second), "--skip-analysers",
        ], cwd=SAFETY, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed2.returncode, 0, completed2.stderr)
        for path in sorted(output.rglob("*")):
            if path.is_file():
                relative = path.relative_to(output)
                self.assertEqual(path.read_bytes(), (second / relative).read_bytes(), f"nondeterministic {relative}")

    def test_repeated_fixture_check_is_deterministic(self):
        root, c, rust = self.analyze("queue", "c_correct.c", "rust_correct.rs")
        first = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        second = check_04(c, rust, repo_root=root, c_configuration="fixture-c", rust_configuration="fixture-rust", tool_version="test")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
