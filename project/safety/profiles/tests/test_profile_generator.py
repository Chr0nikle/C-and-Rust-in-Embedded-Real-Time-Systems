from __future__ import annotations

import csv
import io
from collections import defaultdict
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from profiles.generator import (
    LAYOUTS,
    PROFILE_DIR,
    ProfileGenerationError,
    generate_profile,
    map_names,
    write_profile,
    parse_c_enum,
    parse_rust_enum,
    parse_thesis_cases,
    parse_c_wire_map,
    parse_rust_wire_map,
    validate_profile_sources,
)
from tables import generate_analysis_tables, generate_thesis_tables, source_role
from common.layers import LayerClassifier
from matched.registry import verify_profile_registry
from common.config import resolve_repo_root
from common.paths import ARTIFACTS
from profiles.definitions import PROFILE_DIR

SAFETY = Path(__file__).resolve().parents[2]
REPO = resolve_repo_root(SAFETY)
GENERATED = ARTIFACTS / "profiles"
PROFILE_FIXTURES = SAFETY / "tests" / "fixtures" / "profiles"


def load(name: str) -> dict:
    return json.loads((PROFILE_FIXTURES / name).read_text())


def _json_diff(left, right, path="$"):
    if type(left) is not type(right):
        return [f"{path}: type {type(left).__name__} != {type(right).__name__}"]
    if isinstance(left, dict):
        diffs = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                diffs.append(f"{path}.{key}: missing on one side")
            else:
                diffs.extend(_json_diff(left[key], right[key], f"{path}.{key}"))
        return diffs
    if isinstance(left, list):
        if len(left) != len(right):
            return [f"{path}: list length {len(left)} != {len(right)}"]
        diffs = []
        for index, (a, b) in enumerate(zip(left, right)):
            diffs.extend(_json_diff(a, b, f"{path}[{index}]"))
        return diffs
    return [] if left == right else [f"{path}: {left!r} != {right!r}"]


class ProfileGeneratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = {
            name: load(f"{name}-stm32f4-all.json")
            for name in ("nuttx", "zephyr", "freertos")
        }
        paths = {path.stem: path for path in sorted(PROFILE_FIXTURES.glob("*-all.json"))}
        paths.update({path.stem: path for path in sorted(PROFILE_DIR.glob("*-all.json"))})
        cls.board_profiles = {
            name: json.loads(path.read_text())
            for name, path in sorted(paths.items())
            if not name.startswith("native-")
        }

    def test_complete_case_coverage(self) -> None:
        for rtos, profile in self.profiles.items():
            present = {case["thesis_case"] for case in profile["cases"]}
            absent = {case["thesis_case"] for case in profile["absent_cases"]}
            self.assertEqual(present | absent, set(range(1, 27)), rtos)
            self.assertFalse(present & absent, rtos)
        for rtos in ("nuttx", "zephyr"):
            for board in ("stm32f4", "stm32h723", "nrf52840", "esp32c6"):
                profile = self.board_profiles[f"{rtos}-{board}-all"]
                self.assertEqual({case["thesis_case"] for case in profile["cases"]}, set(range(1, 27)) - {19, 24, 25})
                self.assertEqual({case["thesis_case"] for case in profile["absent_cases"]}, {19, 24, 25})
        expected_free_rtos = {"stm32f4": 26, "stm32h723": 26, "nrf52840": 24, "esp32c6": 22}
        for board, count in expected_free_rtos.items():
            self.assertEqual(len(self.board_profiles[f"freertos-{board}-all"]["cases"]), count)

    def test_free_rtos_c6_default_wire_mapping(self) -> None:
        profile = self.board_profiles["freertos-esp32c6-all"]
        self.assertEqual(len(profile["cases"]), 22)
        self.assertEqual({case["thesis_case"] for case in profile["absent_cases"]}, {19, 24, 25, 26})
        self.assertIn(2, {case["thesis_case"] for case in profile["cases"]})

    def test_profile_availability_matches_campaign(self) -> None:
        cases = json.loads((SAFETY / "specs" / "cases.json").read_text())["cases"]
        wire_to_case = {int(case["c_wire_id"]): int(case["thesis_case"]) for case in cases}
        manifests = {}
        for path in (REPO.parent / "results/campaigns/campaign-replication-20261019T214853Z/benchmarks").glob("*/manifest.json"):
            data = json.loads(path.read_text())
            manifests[(data["rtos"], data["board"], data["language"])] = {
                wire_to_case[wire] for wire in data.get("cases", []) if wire in wire_to_case
            }
        boards = {"stm32f4": "stm32f4discovery", "stm32h723": "stm32h723", "nrf52840": "nrf52840dk", "esp32c6": "esp32c6"}
        for name, profile in self.board_profiles.items():
            rtos, board = name.split("-all")[0].rsplit("-", 1)
            advertised = manifests[(rtos, boards[board], "c")]
            absent = {case["thesis_case"] for case in profile["absent_cases"]}
            self.assertFalse(advertised & absent, f"{name}: campaign-advertised cases marked absent")
            self.assertEqual(absent - advertised, absent, f"{name}: absent cases not supported by campaign")

    def test_board_configurations_are_not_collapsed(self) -> None:
        self.assertEqual(len(self.board_profiles), 12)
        configs = [
            (profile["id"], profile["source_configs"]["c_support"]["compile_commands"], profile["source_configs"]["rust"]["target"])
            for profile in self.board_profiles.values()
        ]
        self.assertEqual(len({config[1] for config in configs}), 12)
        self.assertEqual(len({config[2] for config in configs}), 3)

    def test_board_specific_isr_roots_and_scopes(self) -> None:
        def roots(profile, case):
            entry = next(item for item in profile["cases"] if item["thesis_case"] == case)
            return entry["interrupt_roots"]
        self.assertIn("isr_on_alarm", roots(self.board_profiles["freertos-esp32c6-all"], 16))
        self.assertIn("trig_isr", roots(self.board_profiles["freertos-esp32c6-all"], 17))
        self.assertIn("TIMER1_IRQHandler", roots(self.board_profiles["freertos-nrf52840-all"], 16))
        self.assertIn("TIM2_IRQHandler", roots(self.board_profiles["freertos-stm32h723-all"], 16))
        self.assertIn("systimer_t1_isr", roots(self.board_profiles["nuttx-esp32c6-all"], 16))
        self.assertIn("trigger_isr", roots(self.board_profiles["zephyr-esp32c6-all"], 17))
        for profile in self.board_profiles.values():
            for spec in profile["source_configs"].values():
                for unit in spec.get("source_units", []) + spec.get("extra_units", []):
                    self.assertNotIn("/build/", unit, profile["id"])
                    self.assertFalse(Path(unit).name.startswith("project_elf_src_"), profile["id"])

    def test_board_rust_targets(self) -> None:
        self.assertEqual(self.board_profiles["zephyr-stm32h723-all"]["source_configs"]["rust"]["target"], "thumbv7em-none-eabi")
        self.assertEqual(self.board_profiles["nuttx-stm32h723-all"]["source_configs"]["rust"]["target"], "thumbv7em-none-eabihf")
        self.assertEqual(self.board_profiles["freertos-stm32h723-all"]["source_configs"]["rust"]["target"], "thumbv7em-none-eabihf")

    def test_native_features_recorded(self) -> None:
        expected = {
            "native-freertos-in-rust-stm32f4": ["f4"],
            "native-freertos-in-rust-stm32h723": ["stm32h723"],
            "native-rivet-stm32f4": [],
        }
        for name, features in expected.items():
            profile = json.loads((GENERATED / f"{name}.json").read_text())
            self.assertEqual(profile["features"], features)
            inventory = json.loads((ARTIFACTS / "generated_inventory" / name / "coverage.json").read_text())
            self.assertEqual(inventory["rust_features"], features)

    def test_native_profiles_are_separate(self) -> None:
        native = [json.loads(path.read_text()) for path in sorted([*PROFILE_DIR.glob("native-*.json"), *PROFILE_FIXTURES.glob("native-*.json")])]
        self.assertEqual(len(native), 3)
        for profile in native:
            self.assertEqual(profile["matched_analysis"], "not_applicable")
            self.assertEqual(profile["analysis_2"], "rust_only_source_trace")
            self.assertTrue(profile["source_files"])
            validate_profile_sources(REPO, profile)

    def test_source_backed_mappings(self) -> None:
        for rtos, profile in self.profiles.items():
            root_texts = [(REPO / path).read_text() for path in profile["c_root_search_files"]]
            bridge_texts = [(REPO / path).read_text() for path in profile["c_bridge_search_files"]]
            irq_texts = [(REPO / path).read_text() for path in profile["interrupt_search_files"]]
            self.assertTrue(root_texts, rtos)
            for case in profile["cases"]:
                for function in case["c_root_functions"]:
                    self.assertTrue(any(re.search(rf"\b{re.escape(function)}\s*\(", text) for text in root_texts),
                                    f"{rtos} case {case['thesis_case']} root {function}")
                for function in case["c_bridge_roots"]:
                    self.assertTrue(any(re.search(rf"\b{re.escape(function)}\s*\(", text) for text in bridge_texts),
                                    f"{rtos} case {case['thesis_case']} bridge {function}")
                for function in case["interrupt_roots"]:
                    self.assertTrue(any(re.search(rf"\b{re.escape(function)}\s*\(", text) for text in irq_texts),
                                    f"{rtos} case {case['thesis_case']} interrupt {function}")

    def test_registry_source_verification(self) -> None:
        for rtos in ("nuttx", "zephyr"):
            result = verify_profile_registry(REPO, SAFETY, self.profiles[rtos])
            self.assertEqual(result["selected_case_count"], 23)
            self.assertEqual(sum(1 for row in result["rows"] if row["match_status"] == "verified_from_registry"), 23, rtos)
        result = verify_profile_registry(REPO, SAFETY, self.profiles["freertos"])
        self.assertEqual(result["selected_case_count"], 26)
        self.assertEqual(sum(1 for row in result["rows"] if row["match_status"] == "verified_from_registry"), 26)

    def test_source_guards_and_change_detection(self) -> None:
        profile = json.loads(json.dumps(self.profiles["nuttx"]))
        validate_profile_sources(REPO, profile)
        path = sorted(profile["generator"]["source_guards"])[0]
        profile["generator"]["source_guards"][path] = "0" * 64
        with self.assertRaises(ProfileGenerationError):
            validate_profile_sources(REPO, profile)

    def test_unresolved_mapping_is_reported(self) -> None:
        thesis = parse_thesis_cases(REPO)
        _, unresolved = map_names(thesis, [], [], {}, {})
        self.assertEqual(len(unresolved), 26)
        self.assertTrue(all("reason" in item for item in unresolved))
        self.assertIn("unresolved", self.profiles["nuttx"]["generator"])

    def test_deterministic_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = generate_profile(REPO, "nuttx", Path(directory))
            write_profile(profile, Path(directory) / "nuttx-stm32f4-all.json")
            generated = json.loads((Path(directory) / "nuttx-stm32f4-all.json").read_text())
        stored = json.loads((GENERATED / "nuttx-stm32f4-all.json").read_text())
        if generated != stored:
            self.fail(
                "byte-for-byte profile reproduction failed; first differences: "
                + "; ".join(_json_diff(generated, stored)[:8])
                + ". Semantic equivalence is checked separately; historical build IDs are not rewritten."
            )

    def test_profile_content_is_stable_across_revision_metadata(self) -> None:
        """Profile semantics must not change when only the Git revision changes."""
        import copy
        with tempfile.TemporaryDirectory() as directory:
            profile = generate_profile(REPO, "nuttx", Path(directory))
            write_profile(profile, Path(directory) / "nuttx-stm32f4-all.json")
            generated = json.loads((Path(directory) / "nuttx-stm32f4-all.json").read_text())
        stored = json.loads((GENERATED / "nuttx-stm32f4-all.json").read_text())

        def normalize(document: dict) -> dict:
            document = copy.deepcopy(document)
            document.get("generator", {}).pop("source_revision", None)
            for config in document.get("source_configs", {}).values():
                config.pop("build_id", None)
                config.pop("replace_build_id", None)
            return document

        self.assertEqual(normalize(generated), normalize(stored))

    def test_inventory_covers_all_configurations(self) -> None:
        inventory = ARTIFACTS / "generated_inventory"
        expected = set(self.board_profiles) | {path.stem for path in GENERATED.glob("native-*.json")}
        observed = {path.name for path in inventory.iterdir() if path.is_dir()}
        self.assertTrue(expected.issubset(observed))
        for name in expected:
            coverage = json.loads((inventory / name / "coverage.json").read_text())
            self.assertGreater(coverage["operation_rows"], 0, name)
            self.assertIn("sources.json", {path.name for path in (inventory / name).iterdir()})

    def test_free_rtos_trace_hook_boundaries(self) -> None:
        expected = {
            "freertos-stm32f4-all": (19, 24, 25),
            "freertos-stm32h723-all": (19, 24, 25),
            "freertos-nrf52840-all": (19,),
        }
        for profile, cases in expected.items():
            output = ARTIFACTS / "generated_outputs" / profile
            rows = {int(row["thesis_case"]): row for row in csv.DictReader(io.StringIO((output / "case-coverage.csv").read_text()))}
            for number in cases:
                row = rows[number]
                self.assertEqual(row["exposure_status"], "comparable", (profile, number))
                self.assertEqual(row["external_entry_status"], "resolved_external_boundary", (profile, number))
                self.assertIn("xPortSysTickHandler", row["analysis_boundary"])
                evidence = json.loads(row["external_entry_evidence"])
                self.assertTrue(evidence["project_hook"]["path"].endswith("FreeRTOSConfig.h"))
                self.assertTrue(evidence["kernel_entry"]["path"].endswith("port.c"))

    def test_native_per_case_traces(self) -> None:
        expected = {
            "native-freertos-in-rust-stm32f4": 26,
            "native-freertos-in-rust-stm32h723": 26,
            "native-rivet-stm32f4": 24,
        }
        inventory = ARTIFACTS / "generated_inventory"
        for name, count in expected.items():
            summary = json.loads((inventory / name / "native-case-trace-summary.json").read_text())
            self.assertEqual(summary["case_count"], count, name)
            self.assertEqual(len({case["thesis_case"] for case in summary["cases"]}), count, name)
            self.assertTrue(all(case["status"] in ("complete", "complete_with_external_boundary") for case in summary["cases"]), name)
            self.assertIn("measure", summary["measure_function"] or "")

    def test_no_implemented_case_has_incomplete_trace(self) -> None:
        matrix = list(csv.DictReader(io.StringIO((ARTIFACTS / "generated_outputs" / "benchmark-support-matrix.csv").read_text())))
        incomplete = [row for row in matrix if row["status"] == "implemented_analysis_incomplete"]
        self.assertEqual(incomplete, [])
        implemented = [row for row in matrix if row["status"] in ("fully_implemented_and_analysed", "native_inventory_and_rust_case_trace")]
        self.assertEqual(len(implemented), 564 + 76)

    def test_layer_rules_cover_all_matched_and_native_configurations(self) -> None:
        classifier = LayerClassifier(SAFETY / "specs" / "layers.json")
        inventory = ARTIFACTS / "generated_inventory"
        expected_profiles = set(self.board_profiles) | {path.stem for path in GENERATED.glob("native-*.json")}
        self.assertEqual(len(expected_profiles), 15)
        role_layers = {
            "c_application": "benchmark_application",
            "c_adapter": "c_benchmark_rtos_adapter",
            "c_support": "c_support_linked_with_rust",
            "rust_application": "benchmark_application",
            "rust_native_application": "rust_native_application",
            "rust_ffi_boundary": "rust_ffi_boundary",
            "shared_c": "shared_infrastructure",
            "shared_rust": "shared_infrastructure",
        }
        for profile in sorted(expected_profiles):
            rows = list(csv.DictReader(io.StringIO((inventory / profile / "operations.csv").read_text())))
            self.assertTrue(rows, profile)
            self.assertFalse([row for row in rows if row["layer"] == "unclassified"], profile)
            for row in rows:
                expected = role_layers[source_role(row)]
                self.assertEqual(row["layer"], expected, (profile, row["source_file"], row["category"]))
            for scope in ("c-benchmark", "c-support", "rust"):
                selected = [row for row in rows if row["scope"] == scope]
                for row in selected:
                    self.assertEqual(row["layer"], classifier.classify(scope, row["source_file"]), (profile, scope, row["source_file"]))
        for path in sorted(expected_profiles):
            self.assertTrue((inventory / path / "operations.csv").is_file())
        baseline = json.loads((SAFETY / "specs" / "layer-classification-baseline.json").read_text())
        self.assertEqual(set(baseline["configurations"]), expected_profiles)
        comparison = list(csv.DictReader(io.StringIO((ARTIFACTS / "generated_outputs" / "analysis-tables" / "layer-classification-comparison.csv").read_text())))
        by_config = defaultdict(lambda: {"before": defaultdict(int), "after": defaultdict(int)})
        for row in comparison:
            by_config[row["configuration"]]["before"][row["layer"]] += int(row["before"])
            by_config[row["configuration"]]["after"][row["layer"]] += int(row["after"])
        for configuration, snapshot in baseline["configurations"].items():
            rows = list(csv.DictReader(io.StringIO((inventory / configuration / "operations.csv").read_text())))
            active = [row for row in rows if row["activity_status"] == "true" and row["resolution_status"] == "resolved"]
            current = defaultdict(int)
            for row in active:
                current[row["layer"]] += 1
            observed = by_config[configuration]
            self.assertEqual({layer: value for layer, value in observed["before"].items() if value}, snapshot["active_resolved_by_layer"], configuration)
            self.assertEqual(sum(observed["before"].values()), snapshot["active_resolved"], configuration)
            self.assertEqual({layer: value for layer, value in observed["after"].items() if value}, dict(current), configuration)
            self.assertEqual(sum(observed["after"].values()), len(active), configuration)
        self.assertGreater(sum(value for row in comparison if row["layer"] == "unclassified" for value in [int(row["before"])]), 0)
        self.assertEqual(sum(int(row["after"]) for row in comparison if row["layer"] == "unclassified"), 0)

    def test_thesis_safety_tables_are_reproducible(self) -> None:
        committed = SAFETY.parents[1] / "thesis" / "generated" / "typ"
        if not (committed / "safety-comparison.typ").is_file():
            self.skipTest("thesis generated directory is outside the isolated safety tooling copy")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            tables = root / "tables"
            generate_thesis_tables(REPO, generated_dir=generated, tables_dir=tables)
            names = (
                "safety-comparison.typ",
                "safety-c-categories.typ",
                "safety-rust-categories.typ",
                "safety-role-counts.typ",
                "safety-trace-coverage.typ",
                "safety-native-results.typ",
            )
            for name in names:
                self.assertEqual((generated / name).read_bytes(), (committed / name).read_bytes(), name)
            actual = json.loads((tables / "safety-revalidation.json").read_text())
            expected = json.loads((ARTIFACTS / "generated_outputs" / "analysis-tables" / "safety-revalidation.json").read_text())
            self.assertEqual(actual, expected)
            self.assertTrue(all(check["status"] for check in actual["checks"].values()))

    def test_analysis_tables_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generate_analysis_tables(REPO, Path(directory))
            committed = ARTIFACTS / "generated_outputs" / "analysis-tables"
            for name in ("operation-summary.csv", "operation-role-totals.csv", "matched-operation-comparison.csv", "matched-trace-summary.csv", "native-summary.csv", "layer-classification-comparison.csv"):
                self.assertEqual((Path(directory) / name).read_text(), (committed / name).read_text(), name)

    def test_generated_profiles_validate_end_to_end(self) -> None:
        frozen = {"nuttx": 961, "zephyr": 780}
        for rtos, calls in frozen.items():
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "out"
                result = subprocess.run(
                    [sys.executable, str(SAFETY / "safety.py"), "matched-api",
                     "--profile", str(GENERATED / f"{rtos}-stm32f4-all.json"),
                     "--output", str(output)],
                    cwd=REPO, text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                coverage = json.loads((output / "coverage.json").read_text())
                validation = json.loads((output / "validation.json").read_text())
                self.assertEqual(validation["status"], "passed")
                self.assertEqual(coverage["verified_roots"], 23)
                self.assertEqual(coverage["call_site_occurrences"], calls)


if __name__ == "__main__":
    unittest.main()
