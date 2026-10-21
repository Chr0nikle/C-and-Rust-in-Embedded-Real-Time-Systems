"""Focused pilot tests for NuttX and Zephyr STM32F4 profiles."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from common.layers import LayerClassifier
from matched.c_extract import extract_c_index
from matched.classify import map_arguments
from matched.pipeline import rtos_match_status, run_matched_api
from profiles.definitions import discover_profile_configs, load_profile
from matched.r_extract import build_rust_index
from matched.registry import verify_profile_registry
from common.config import resolve_repo_root

HERE = Path(__file__).resolve().parent
SAFETY = HERE.parent.parent
REPO = resolve_repo_root(SAFETY)
EXTRACTOR = SAFETY / "rust_extractor/target/release/uastw-rust-extract"
CLANG = ["-target", "arm-none-eabi", "-mcpu=cortex-m4", "-mthumb"]


class CrossRtosPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classifier = LayerClassifier(SAFETY / "specs/layers.json")

    def test_profiles_verify_exactly_five_and_case19_is_absent(self):
        for name in ("nuttx-stm32f4.json", "zephyr-stm32f4.json"):
            profile = load_profile(REPO, name)
            self.assertEqual(len(profile["cases"]), 5)
            self.assertEqual({case["thesis_case"] for case in profile["cases"]}, {2, 9, 16, 17, 26})
            self.assertEqual([item["thesis_case"] for item in profile["absent_cases"]], [19, 24, 25])
            verification = verify_profile_registry(REPO, SAFETY, profile)
            self.assertEqual(len(verification["rows"]), 5)
            self.assertTrue(all(row["match_status"] == "verified_from_registry" for row in verification["rows"]))
            self.assertTrue(all(row["checks"]["wire_ids_agree"] for row in verification["rows"]))
            self.assertTrue(all(row["checks"]["c_port_mapping"] for row in verification["rows"]))

    @unittest.skipUnless((REPO / ".local/source/nuttx/build-bench/compile_commands.json").is_file(), "NuttX compile database unavailable")
    def test_profile_source_and_build_scopes(self):
        nuttx = discover_profile_configs(REPO, load_profile(REPO, "nuttx-stm32f4.json"))
        zephyr = discover_profile_configs(REPO, load_profile(REPO, "zephyr-stm32f4.json"))
        for discovery, prefix, support_units in (
            (nuttx, "nuttx", {"main.c", "port.c", "isr.c"}),
            (zephyr, "zephyr", {"main.c", "port.c"}),
        ):
            configs = discovery["configurations"]
            benchmark = configs[f"{prefix}-stm32f4-c-benchmark"]
            support = configs[f"{prefix}-stm32f4-c-support-rust"]
            self.assertFalse(any(unit["has_bench_language_rust"] for unit in benchmark["translation_units"]))
            self.assertTrue(all(unit["has_bench_language_rust"] for unit in support["translation_units"]))
            self.assertTrue({Path(unit["path"]).name for unit in support["translation_units"]} == support_units)
            self.assertTrue(any(unit["path"].endswith("common/c/src/runner.c") for unit in benchmark["translation_units"]))
            self.assertFalse(any(unit["path"].endswith("common/c/src/runner.c") for unit in support["translation_units"]))
            self.assertTrue(discovery["configurations"][f"{prefix}-stm32f4-rust"]["files"])
            self.assertEqual(discovery["configurations"][f"{prefix}-stm32f4-rust"]["target"]["target"], "thumbv7em-none-eabi")
            for unit in benchmark["translation_units"] + support["translation_units"]:
                args = unit["clang_args"]
                self.assertIn("-target", args)
                self.assertIn("arm-none-eabi", args)
                self.assertFalse(any("/lib/gcc/arm-none-eabi/" in arg for arg in args))
                if prefix == "zephyr":
                    self.assertFalse(any(arg in ("-fno-printf-return-value", "-mfp16-format=ieee", "-fno-reorder-functions") for arg in args))
                    self.assertTrue(any("clang" in arg and arg.endswith("/include") for arg in args))
            self.assertTrue(discovery["generated_headers"])
        self.assertEqual(nuttx["external_source"]["revision"], "20752312eaac24994487891207c81fa22c02f7b5")
        self.assertEqual(zephyr["external_source"]["revision"], "dccb09599635bdff17633fa7e9dab014b91dce90")

    def test_nuttx_watchdog_callback_c_and_rust(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-nuttx-watchdog-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        csrc = root / "firmware/c-kernels/nuttx/c"
        csrc.mkdir(parents=True)
        (csrc / "watchdog.c").write_text(
            "typedef void (*wdentry_t)(unsigned int);\n"
            "extern int wd_start(void *, unsigned int, wdentry_t, unsigned int);\n"
            "static void watchdog_cb(unsigned int arg) { (void)arg; }\n"
            "int port_timer_probe(void) { return wd_start(0, 0, watchdog_cb, 0); }\n"
        )
        config = {
            "configuration": "nuttx-watchdog-c",
            "rust_mode": 0,
            "project_source_roots": ["firmware/c-kernels/nuttx/c"],
            "project_header_roots": ["firmware/c-kernels/nuttx/c"],
            "rtos_symbols": ["wd_start"],
            "callback_apis": {"wd_start": {"invocation_kind": "watchdog_callback", "callback_parameter": 2}},
            "translation_units": [{
                "path": "firmware/c-kernels/nuttx/c/watchdog.c",
                "abs_path": str(csrc / "watchdog.c"),
                "project_written": True,
                "clang_args": CLANG + ["-I", str(csrc)],
            }],
        }
        index = extract_c_index(root, config)
        registration = next(row for row in index["callback_registrations"] if row["api"] == "wd_start")
        self.assertEqual(registration["invocation_kind"], "watchdog_callback")
        self.assertEqual(registration["resolution_status"], "resolved_callback_definition")
        self.assertEqual(registration["callback_symbol"], "watchdog_cb")

        rsrc = root / "firmware/c-kernels/nuttx/rust/src"
        rsrc.mkdir(parents=True)
        (rsrc / "lib.rs").write_text(
            "#![allow(dead_code)]\n"
            "extern \"C\" {\n"
            "    fn wd_start_abstick(wd: *mut core::ffi::c_void, delay: u32, entry: Option<extern \"C\" fn(u32)>, arg: u32) -> i32;\n"
            "}\n"
            "extern \"C\" fn watchdog_cb(_arg: u32) {}\n"
            "pub unsafe fn arm() -> i32 { wd_start_abstick(core::ptr::null_mut(), 0, Some(watchdog_cb), 0) }\n"
        )
        rust_config = {
            "configuration": "nuttx-watchdog-rust",
            "files": ["firmware/c-kernels/nuttx/rust/src/lib.rs"],
            "target": {"arch": "arm", "os": "none", "family": "none", "pointer_width": "32"},
            "callback_apis": {"wd_start_abstick": {"invocation_kind": "watchdog_callback", "callback_parameter": 2}},
            "language_builtin_calls": [],
            "language_builtin_methods": [],
        }
        rust_index = build_rust_index(root, rust_config, EXTRACTOR, self.classifier)
        rust_registration = next(row for row in rust_index["callback_registrations"] if row["api"] == "wd_start_abstick")
        self.assertEqual(rust_registration["invocation_kind"], "watchdog_callback")
        self.assertEqual(rust_registration["resolution_status"], "resolved_callback_definition")
        self.assertEqual(rust_registration["callback_symbol"], "watchdog_cb")

    def test_nuttx_pilot_outputs_and_contract(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-nuttx-pilot-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "out"
        result = run_matched_api(REPO, output, profile_id="nuttx-stm32f4.json")
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertEqual(result["coverage"]["selected_cases"], 5)
        self.assertEqual(result["coverage"]["verified_roots"], 5)
        self.assertTrue(result["coverage"]["absent_case_19"])
        with (output / "case-availability.csv").open() as handle:
            availability = {int(row["thesis_case"]): row for row in csv.DictReader(handle)}
        self.assertEqual(len(availability), 26)
        self.assertEqual(availability[19]["source_status"], "absent")
        self.assertEqual(availability[19]["pilot_status"], "absent_not_quantified")
        self.assertNotIn(19, result["summary"]["selected_cases"])
        with (output / "case-coverage.csv").open() as handle:
            coverage = list(csv.DictReader(handle))
        self.assertEqual({int(row["thesis_case"]) for row in coverage}, {2, 9, 16, 17, 26})
        self.assertTrue(all(row["c_call_path_status"] == "resolved_within_model" and row["rust_call_path_status"] == "resolved_within_model" for row in coverage))
        with (output / "callback-registrations.csv").open() as handle:
            callbacks = list(csv.DictReader(handle))
        self.assertTrue(any(row["language"] == "c" and row["invocation_kind"] == "thread_entry" for row in callbacks))
        self.assertTrue(any(row["language"] == "rust" and row["invocation_kind"] == "thread_entry" for row in callbacks))
        with (output / "rtos-target-relationships.csv").open() as handle:
            targets = list(csv.DictReader(handle))
        self.assertTrue(any(row["rust_cross_language_path_ids"] not in ("", "[]") for row in targets))
        self.assertFalse(any(row["match_status"] == "rust_only_rtos_api" for row in targets))
        self.assertTrue(any(row["raw_c_target_names"] or row["raw_rust_target_names"] for row in targets))
        target_names = {row["target_name"] for row in targets}
        for name in ("pthread_attr_init", "pthread_attr_destroy", "pthread_attr_setschedpolicy", "pthread_attr_setinheritsched", "pthread_attr_setschedparam", "clock_gettime", "up_irq_save", "up_irq_restore", "pthread_self"):
            self.assertIn(name, target_names)
        self.assertTrue(all(row["target_name"] != "pthread_self" or row["rust_language_path_ids"] not in ("", "[]") for row in targets if row["target_name"] == "pthread_self"))
        with (output / "call-edges.csv").open() as handle:
            edges = list(csv.DictReader(handle))
        levels = {level for row in edges for level in json.loads(row["levels"])}
        self.assertTrue({"A", "B", "C"}.issubset(levels))
        self.assertTrue(all(row["target_category"] != "c_bridge_function" for row in targets))
        with (output / "case-comparison.csv").open() as handle:
            comparison = {int(row["thesis_case"]): row for row in csv.DictReader(handle)}
        self.assertGreater(int(comparison[16]["c_dispatch_context_arguments"]), 0)
        self.assertGreater(int(comparison[16]["c_output_pointer_arguments"]), 0)
        for row in comparison.values():
            self.assertLessEqual(
                int(row["c_application_explicit_size_memberships"]),
                int(row["c_all_level_explicit_size_memberships"]),
            )
            self.assertLessEqual(
                int(row["rust_application_explicit_size_memberships"]),
                int(row["rust_all_level_explicit_size_memberships"]),
            )
        with (output / "target-aliases.csv").open() as handle:
            aliases = list(csv.DictReader(handle))
        self.assertTrue(aliases)
        self.assertTrue(all(row["verification_status"] == "source_backed" for row in aliases))
        with (output / "semantic-limitations.csv").open() as handle:
            semantic = list(csv.DictReader(handle))
        self.assertEqual(semantic[0]["limitation"], "priority_inheritance_protocol_not_explicitly_established")
        self.assertTrue((output / "manifest.json").is_file())
        for path in output.iterdir():
            if path.suffix in (".csv", ".json"):
                self.assertNotIn("/home/", path.read_text(), msg=f"absolute path in {path.name}")

    def test_zephyr_pilot_outputs_and_aliases(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-zephyr-pilot-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "out"
        result = run_matched_api(REPO, output, profile_id="zephyr-stm32f4.json")
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertEqual(result["coverage"]["selected_cases"], 5)
        with (output / "target-aliases.csv").open() as handle:
            aliases = list(csv.DictReader(handle))
        self.assertTrue(any(row["rust_symbol"] == "z_impl_k_msgq_put" and row["canonical_target"] == "k_msgq_put" for row in aliases))
        self.assertTrue(all(row["verification_status"] == "source_backed" for row in aliases))
        with (output / "rtos-target-relationships.csv").open() as handle:
            targets = list(csv.DictReader(handle))
        raw_rust = " ".join(row["raw_rust_target_names"] for row in targets)
        self.assertIn("z_impl_k_msgq_put", raw_rust)
        self.assertFalse(any(row["match_status"] == "rust_only_rtos_api" for row in targets))
        target_names = {row["target_name"] for row in targets}
        for name in ("arch_irq_lock", "arch_irq_unlock", "arm_irq_enable", "arm_irq_disable", "k_current_get"):
            self.assertIn(name, target_names)
        self.assertNotIn("arm_irq_priority_set", target_names)
        with (output / "call-sites.csv").open() as handle:
            call_sites = list(csv.DictReader(handle))
        irq_runtime = [row for row in call_sites if row["target_name"] == "arm_irq_priority_set"]
        self.assertEqual(len(irq_runtime), 2)
        self.assertEqual({row["configuration"] for row in irq_runtime}, {"zephyr-stm32f4-c-benchmark", "zephyr-stm32f4-c-support-rust"})
        self.assertEqual({row["target_category"] for row in irq_runtime}, {"external_function"})
        self.assertEqual({row["callee_kind"] for row in irq_runtime}, {"external_function"})
        self.assertEqual({row["levels"] for row in irq_runtime}, {'["B"]'})
        self.assertEqual(len({row["source_occurrence_id"] for row in irq_runtime}), 1)
        with (output / "case-memberships.csv").open() as handle:
            membership_cases = {row["call_site_id"]: int(row["benchmark_case"]) for row in csv.DictReader(handle)}
        self.assertEqual({membership_cases[row["call_site_id"]] for row in irq_runtime}, {17})
        self.assertEqual(len({row["call_site_id"] for row in irq_runtime}), 2)
        with (output / "macro-sites.csv").open() as handle:
            macro_rows = list(csv.DictReader(handle))
        irq_macros = [row for row in macro_rows if row["macro"] == "IRQ_CONNECT"]
        self.assertEqual(len(irq_macros), 2)
        self.assertTrue(all(row["resolution_status"] == "static_registration" for row in irq_macros))
        self.assertTrue(all("runtime_arm_irq_priority_set_retained" in row["macro_effect"] for row in irq_macros))
        self.assertTrue(all(17 in json.loads(row["cases"]) for row in irq_macros))
        with (output / "path-coverage.csv").open() as handle:
            path17 = next(row for row in csv.DictReader(handle) if row["benchmark_case"] == "17")
        self.assertEqual(path17["external_entry_status"], "resolved")
        self.assertEqual(path17["external_isr_roots"], "1")
        profile17 = next(case for case in load_profile(REPO, "zephyr-stm32f4.json")["cases"] if case["thesis_case"] == 17)
        self.assertEqual(profile17["interrupt_roots"], ["trigger_isr"])
        self.assertIn("static void trigger_isr", (REPO / "firmware/c-kernels/zephyr/c/stm32f4/port.c").read_text())
        # External-entry status records a source registration, not runtime execution.
        with (output / "callback-registrations.csv").open() as handle:
            callbacks = list(csv.DictReader(handle))
        self.assertTrue(any(row["invocation_kind"] == "timer_expiry" and row["language"] == "c" for row in callbacks))
        self.assertTrue(any(row["invocation_kind"] == "thread_entry" and row["language"] == "rust" for row in callbacks))
        with (output / "case-coverage.csv").open() as handle:
            coverage = list(csv.DictReader(handle))
        self.assertEqual({int(row["thesis_case"]) for row in coverage}, {2, 9, 16, 17, 26})
        self.assertTrue(all(row["c_call_path_status"] == "resolved_within_model" and row["rust_call_path_status"] == "resolved_within_model" for row in coverage))

    def test_nested_rtos_calls_keep_profile_symbol_context(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-nested-rtos-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        csrc = root / "firmware/c-kernels/nuttx/c"
        csrc.mkdir(parents=True)
        include = root / "include"
        include.mkdir()
        (include / "rtos.h").write_text(
            "extern int pthread_getschedparam(int, int *, int *);\n"
            "extern int pthread_self(void);\n"
        )
        (csrc / "nested.c").write_text(
            "#include <rtos.h>\n"
            "int port_nested_probe(void) { int p = 0; return pthread_getschedparam(pthread_self(), &p, &p); }\n"
        )
        config = {
            "configuration": "nuttx-nested-rtos",
            "rust_mode": 0,
            "project_source_roots": ["firmware/c-kernels/nuttx/c"],
            "project_header_roots": ["firmware/c-kernels/nuttx/c"],
            "rtos_symbols": ["pthread_getschedparam", "pthread_self"],
            "translation_units": [{
                "path": "firmware/c-kernels/nuttx/c/nested.c",
                "abs_path": str(csrc / "nested.c"),
                "project_written": True,
                "clang_args": CLANG + ["-I", str(include)],
            }],
        }
        index = extract_c_index(root, config)
        calls = {row["target_name"]: row for row in index["calls"]}
        self.assertEqual(calls["pthread_self"]["target_kind"], "rtos_api")
        self.assertEqual(calls["pthread_getschedparam"]["target_kind"], "rtos_api")

    def test_allocator_size_classification_is_narrow(self):
        size_param = [{"name": "", "type": {"kind": "integer"}, "type_text": "size_t"}]
        explicit, _, _ = map_arguments("c", ["NX_FRAG_BYTES"], size_param, "rtos_api", "malloc", {"malloc": [0]})
        derived, _, _ = map_arguments("c", ["NX_FRAG_N * sizeof(void *)"], size_param, "rtos_api", "malloc", {"malloc": [0]})
        unrelated, _, _ = map_arguments("c", ["NX_FRAG_BYTES"], size_param, "external_function", "not_an_allocator", {"malloc": [0]})
        self.assertEqual(explicit[0]["role"], "explicit_size")
        self.assertEqual(derived[0]["role"], "derived_size")
        self.assertEqual(unrelated[0]["role"], "unknown")

    def test_allocator_sizes_and_macro_policy_in_production(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-size-policy-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "out"
        run_matched_api(REPO, output, profile_id="nuttx-stm32f4.json")
        comparison = {int(row["thesis_case"]): row for row in csv.DictReader((output / "case-comparison.csv").open())}
        self.assertEqual(int(comparison[26]["c_application_explicit_size_memberships"]), 0)
        self.assertGreaterEqual(int(comparison[26]["c_all_level_explicit_size_memberships"]), 2)
        self.assertGreaterEqual(int(comparison[26]["c_all_level_derived_size_memberships"]), 2)
        zoutput = Path(temporary.name) / "zout"
        run_matched_api(REPO, zoutput, profile_id="zephyr-stm32f4.json")
        zcomparison = {int(row["thesis_case"]): row for row in csv.DictReader((zoutput / "case-comparison.csv").open())}
        self.assertEqual(int(zcomparison[9]["c_application_explicit_size_memberships"]), 0)
        self.assertEqual(int(zcomparison[9]["rust_application_explicit_size_memberships"]), 0)
        self.assertEqual(int(zcomparison[9]["c_all_level_explicit_size_memberships"]), 4)
        self.assertEqual(int(zcomparison[9]["rust_all_level_explicit_size_memberships"]), 2)

    def test_availability_and_ninth_pointer(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-availability-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "out"
        result = run_matched_api(REPO, output, profile_id="nuttx-stm32f4.json")
        self.assertEqual(result["coverage"]["absent_case_count"], 3)
        self.assertEqual(result["coverage"]["implemented_unassessed_count"], 18)
        self.assertEqual(result["coverage"]["selected_cases"], 5)
        with (output / "case-availability.csv").open() as handle:
            availability = {int(row["thesis_case"]): row for row in csv.DictReader(handle)}
        self.assertEqual({case for case, row in availability.items() if row["source_status"] == "absent"}, {19, 24, 25})
        self.assertEqual({case for case, row in availability.items() if row["source_status"] == "not_assessed"}, {1, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 18, 20, 21, 22, 23})
        self.assertEqual(len(availability), 26)
        memberships = list(csv.DictReader((output / "case-memberships.csv").open()))
        self.assertFalse(any(int(row["benchmark_case"]) in {19, 24, 25} for row in memberships))
        args = list(csv.DictReader((output / "call-arguments.csv").open()))
        ninth = [row for row in args if 2 in json.loads(row["cases"]) and row["language"] == "c" and row["source_expression"] == "&received_payload"]
        self.assertEqual(len(ninth), 1)
        self.assertEqual(ninth[0]["raw_pointer"], "true")
        self.assertEqual(ninth[0]["output_pointer"], "false")
        self.assertEqual(ninth[0]["buffer_pointer"], "false")
        self.assertEqual(ninth[0]["parameter_type"], "uint32_t *")

    def test_nuttx_config_provenance(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-config-provenance-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "out"
        run_matched_api(REPO, output, profile_id="nuttx-stm32f4.json")
        sources = json.load(open(output / "sources.json"))
        provenance = sources["config_provenance"]
        self.assertEqual(provenance["external_path"], ".local/source/nuttx/boards/arm/stm32f4/stm32f4discovery/configs/bench/defconfig")
        actual = (REPO / provenance["external_path"]).read_bytes()
        self.assertEqual(provenance["external_sha256"], hashlib.sha256(actual).hexdigest())
        self.assertEqual(provenance["project_sha256"], provenance["external_sha256"])
        self.assertTrue(provenance["byte_identical"])

    def test_full_profiles_verify_twenty_three_cases(self):
        for name in ("nuttx-stm32f4-all.json", "zephyr-stm32f4-all.json"):
            profile = load_profile(REPO, name)
            self.assertEqual(len(profile["cases"]), 23)
            self.assertEqual({case["thesis_case"] for case in profile["cases"]}, set(range(1, 27)) - {19, 24, 25})
            self.assertEqual([item["thesis_case"] for item in profile["absent_cases"]], [19, 24, 25])
            verification = verify_profile_registry(REPO, SAFETY, profile)
            self.assertEqual(len(verification["rows"]), 23)
            self.assertTrue(all(row["match_status"] == "verified_from_registry" for row in verification["rows"]))

    def test_full_outputs_cover_all_implemented_cases(self):
        for profile_name in ("nuttx-stm32f4-all.json", "zephyr-stm32f4-all.json"):
            temporary = tempfile.TemporaryDirectory(prefix="uastw-full-")
            self.addCleanup(temporary.cleanup)
            output = Path(temporary.name) / "out"
            result = run_matched_api(REPO, output, profile_id=profile_name)
            self.assertEqual(result["validation"]["status"], "passed")
            self.assertEqual(result["coverage"]["selected_cases"], 23)
            self.assertEqual(result["coverage"]["implemented_unassessed_count"], 0)
            self.assertEqual(result["coverage"]["absent_case_count"], 3)
            with (output / "case-coverage.csv").open() as handle:
                coverage = {int(row["thesis_case"]): row for row in csv.DictReader(handle)}
            self.assertEqual(set(coverage), set(range(1, 27)) - {19, 24, 25})
            self.assertEqual(coverage[1]["c_root_status"], "common_harness_only")
            self.assertTrue(all(row["c_root_status"] == "resolved" and row["rust_root_status"] == "resolved" for case, row in coverage.items() if case != 1))
            self.assertTrue(all(row["exposure_status"] in ("comparable", "common_only") for row in coverage.values()))
            with (output / "call-sites.csv").open() as handle:
                sites = list(csv.DictReader(handle))
            self.assertFalse(any(row["resolution_status"] == "unresolved" for row in sites))
            with (output / "case-memberships.csv").open() as handle:
                memberships = list(csv.DictReader(handle))
            self.assertEqual(len(memberships), len({(row["call_site_id"], row["benchmark_case"]) for row in memberships}))
            self.assertFalse(any(int(row["benchmark_case"]) in {19, 24, 25} for row in memberships))
            with (output / "rtos-target-relationships.csv").open() as handle:
                targets = list(csv.DictReader(handle))
            self.assertFalse(any(row["match_status"] in ("c_only_rtos_api", "rust_only_rtos_api") for row in targets))
            self.assertTrue(all(row["match_status"] == "matched_rtos_api" for row in targets))
            with (output / "c-bridge-definitions.csv").open() as handle:
                bridges = list(csv.DictReader(handle))
            self.assertFalse({row["target_name"] for row in bridges} & {row["target_name"] for row in targets})
            if "nuttx" in profile_name:
                names = {row["target_name"] for row in targets}
                self.assertTrue({"sched_yield", "clock_nanosleep", "nanosleep"}.issubset(names))
            else:
                names = {row["target_name"] for row in targets}
                self.assertTrue({"k_yield", "k_sleep"}.issubset(names))
                self.assertNotIn("arm_irq_priority_set", names)


    def test_unmatched_target_representability(self):
        fixture = {
            "c_only": {"c": ["c-call-1"], "rust": [], "cross": []},
            "rust_only": {"c": [], "rust": ["rust-call-1"], "cross": []},
            "matched": {"c": ["c-call-2"], "rust": ["rust-call-2"], "cross": []},
            "cross_matched": {"c": ["c-call-3"], "rust": [], "cross": ["c-call-3"]},
        }
        self.assertEqual(rtos_match_status(fixture["c_only"]["c"], fixture["c_only"]["rust"], fixture["c_only"]["cross"]), "c_only_rtos_api")
        self.assertEqual(rtos_match_status(fixture["rust_only"]["c"], fixture["rust_only"]["rust"], fixture["rust_only"]["cross"]), "rust_only_rtos_api")
        self.assertEqual(rtos_match_status(fixture["matched"]["c"], fixture["matched"]["rust"], fixture["matched"]["cross"]), "matched_rtos_api")
        self.assertEqual(rtos_match_status(fixture["cross_matched"]["c"], fixture["cross_matched"]["rust"], fixture["cross_matched"]["cross"]), "matched_rtos_api")



if __name__ == "__main__":
    unittest.main()
