"""Parser-backed tests for the Stage 2 matched API pilot."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from common.layers import LayerClassifier
from matched.c_extract import application_calls, extract_c_index, reachable_edges
from matched.classify import classify_check, map_arguments, return_handling
from matched.pipeline import run_matched_api
import matched.pipeline as pipeline_module
from matched.r_extract import application_method_calls, build_rust_index, ffi_rows, reachable_rust_edges, reachable_rust_functions
from matched.registry import load_cases, verify_registry
from common.config import resolve_repo_root

HERE = Path(__file__).resolve().parent
SAFETY = HERE.parent.parent
REPO = resolve_repo_root(SAFETY)
FIXTURES = HERE / "fixtures"
CLANG = ["-target", "arm-none-eabi", "-mcpu=cortex-m4", "-mthumb"]
TARGET = {"arch": "arm", "os": "none", "family": "none", "pointer_width": "32"}


class MatchedApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classifier = LayerClassifier(SAFETY / "specs/layers.json")
        cls.extractor = SAFETY / "rust_extractor/target/release/uastw-rust-extract"

    def c_repo(self, extra_args=None):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-stage2-c-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        src = root / "firmware/c-kernels/freertos/c/stm32f4/src"
        third = root / ".local/thirdparty/include"
        src.mkdir(parents=True)
        third.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "c/fake_freertos.h", third / "fake_freertos.h")
        shutil.copyfile(FIXTURES / "c/fixture.c", src / "main.c")
        shutil.copyfile(FIXTURES / "c/bridge.c", src / "bridge.c")
        for name in ("amb_a.h", "amb_b.h", "amb_a.c", "amb_b.c"):
            shutil.copyfile(FIXTURES / "c" / name, src / name)
        common = CLANG + ["-I", str(third), "-I", str(src)] + list(extra_args or [])
        main_unit = {"path": "firmware/c-kernels/freertos/c/stm32f4/src/main.c", "abs_path": str(src / "main.c"), "clang_args": common}
        bridge_units = [
            {"path": "firmware/c-kernels/freertos/c/stm32f4/src/bridge.c", "abs_path": str(src / "bridge.c"), "clang_args": common},
            {"path": "firmware/c-kernels/freertos/c/stm32f4/src/amb_a.c", "abs_path": str(src / "amb_a.c"), "clang_args": common},
            {"path": "firmware/c-kernels/freertos/c/stm32f4/src/amb_b.c", "abs_path": str(src / "amb_b.c"), "clang_args": common},
        ]
        main_config = {"configuration": "fixture-c", "rust_mode": 0, "translation_units": [main_unit]}
        bridge_config = {"configuration": "fixture-c-support", "rust_mode": 1, "translation_units": bridge_units}
        return root, main_config, bridge_config

    def r_repo(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-stage2-rust-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        src = root / "firmware/c-kernels/freertos/rust/src"
        src.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "rust/fixture.rs", src / "boundary.rs")
        config = {"configuration": "fixture-rust", "files": ["firmware/c-kernels/freertos/rust/src/boundary.rs"], "target": TARGET}
        return root, config

    def test_registry_verifies_all_26_thesis_cases(self):
        cases = load_cases(REPO, SAFETY)
        verification = verify_registry(REPO, SAFETY, cases)
        self.assertEqual(len(verification["rows"]), 26)
        self.assertEqual({row["thesis_case"] for row in verification["rows"]}, set(range(1, 27)))
        self.assertTrue(all(row["match_status"] == "verified_from_registry" for row in verification["rows"]))
        baseline = {2: "queue_roundtrip", 5: "alloc_latency", 6: "task_handoff", 16: "isr_to_task", 26: "heap_fragmentation"}
        self.assertEqual({row["thesis_case"]: row["thesis_name"] for row in verification["rows"] if row["thesis_case"] in baseline}, baseline)
        self.assertTrue(all(row["checks"]["wire_ids_agree"] for row in verification["rows"]))
        self.assertTrue(all(row["checks"]["c_enum_value"] for row in verification["rows"]))
        self.assertTrue(all(row["checks"]["rust_discriminant"] for row in verification["rows"]))
        self.assertTrue(all(row["checks"]["c_roots"] for row in verification["rows"]))
        self.assertTrue(all(row["checks"]["rust_methods"] for row in verification["rows"]))
        self.assertEqual(
            {row["thesis_case"]: row["rust_discriminant"] for row in verification["rows"]},
            {1: 0, 2: 1, 3: 12, 4: 13, 5: 14, 6: 2, 7: 5, 8: 6, 9: 8, 10: 17, 11: 16, 12: 7, 13: 22, 14: 23, 15: 24, 16: 4, 17: 10, 18: 21, 19: 19, 20: 20, 21: 3, 22: 11, 23: 15, 24: 25, 25: 26, 26: 27},
        )

    def test_c_direct_wrapper_macro_and_arguments(self):
        root, config, _ = self.c_repo()
        index = extract_c_index(root, config)
        edges, roots = reachable_edges(index, ["wrapper_create", "wrapper_send", "wrapper_receive", "wrapper_destroy"])
        self.assertTrue(all(row["status"] == "resolved_root" for row in roots))
        by_name = {(edge["caller"], edge["source_name"]): edge for edge in edges}
        send = by_name[("wrapper_send", "xQueueSend")]
        self.assertEqual(send["target_name"], "xQueueGenericSend")
        self.assertEqual(send["target_kind"], "rtos_api")
        self.assertEqual(send["macro_provenance"], "third_party_macro_expansion")
        self.assertEqual(send["source_args"], ["q", "&value", "0"])
        self.assertGreater(len(send["target_params"]), len(send["source_args"]))
        args, mapping, unmapped = map_arguments("c", send["source_args"], send["target_params"], send["target_kind"])
        self.assertTrue(mapping.startswith("positional"))
        self.assertEqual(args[0]["role"], "resource_handle")
        self.assertTrue(args[1]["address_of"])
        self.assertTrue(args[1]["buffer_pointer"])
        self.assertTrue(unmapped)
        create = by_name[("wrapper_create", "xQueueCreate")]
        self.assertEqual(create["target_name"], "xQueueGenericCreate")
        create_args, _, _ = map_arguments("c", create["source_args"], create["target_params"], create["target_kind"])
        self.assertTrue(create_args[0]["explicit_size"])
        self.assertTrue(create_args[1]["derived_size"])
        self.assertFalse(create_args[1]["buffer_pointer"])
        self.assertEqual(return_handling(send, index["checks"]), "checked_in_condition")

    def test_c_cast_in_macro_argument_and_cfg(self):
        root, config, _ = self.c_repo()
        index = extract_c_index(root, config)
        edges, _ = reachable_edges(index, ["cast_send"])
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["target_name"], "xQueueGenericSend")
        args, _, _ = map_arguments("c", edge["source_args"], edge["target_params"], edge["target_kind"])
        self.assertTrue(any(arg["explicit_cast"] for arg in args))
        self.assertTrue(any(arg["address_of"] for arg in args))

        root_cfg, config_cfg, _ = self.c_repo(extra_args=["-DALT_CFG=1"])
        cfg_index = extract_c_index(root_cfg, config_cfg)
        off_edges, off_roots = reachable_edges(index, ["cfg_only"])
        self.assertEqual(off_edges, [])
        self.assertEqual(off_roots[0]["status"], "unresolved_root")
        on_edges, on_roots = reachable_edges(cfg_index, ["cfg_only"])
        self.assertEqual(on_roots[0]["status"], "resolved_root")
        self.assertTrue(any(edge["target_name"] == "xQueueGenericSend" for edge in on_edges))

    def test_c_checks_and_application_call_sites(self):
        root, config, _ = self.c_repo()
        index = extract_c_index(root, config)
        checks = [classify_check(check) for check in index["checks"] if check["function"] in ("wrapper_send", "wrapper_receive")]
        self.assertTrue(any(check["null_check"] for check in checks))
        self.assertTrue(any(check["status_or_error_check"] for check in checks))
        app = application_calls(index, {"queue_send": "wrapper_send"}, 2)
        self.assertEqual(len(app), 1)
        self.assertEqual(app[0]["caller"], "sample")
        self.assertEqual(app[0]["target_name"], "wrapper_send")
        self.assertEqual(app[0]["source_args"], ["0", "1"])
        definitions = index["definitions_by_name"]["wrapper_send"]
        self.assertEqual(len(definitions), 1)

    def test_c_indirect_unreachable_and_shared_target(self):
        root, config, _ = self.c_repo()
        index = extract_c_index(root, config)
        edges, _ = reachable_edges(index, ["indirect_call", "same_api_a", "same_api_b"])
        indirect = [edge for edge in edges if edge["caller"] == "indirect_call"]
        self.assertEqual(len(indirect), 1)
        self.assertTrue(any(edge["target_kind"] in ("indirect_or_unresolved", "function_pointer_member") for edge in indirect))
        shared = [edge for edge in edges if edge["caller"] in ("same_api_a", "same_api_b") and edge["target_name"] == "xQueueGenericSend"]
        self.assertEqual(len(shared), 2)
        self.assertEqual(len({edge["call_site_id"] for edge in shared}), 2)
        unreachable = [edge for edge in edges if edge["caller"] == "unreferenced"]
        self.assertEqual(unreachable, [])

    def test_rust_safe_and_unsafe_wrapper_interfaces(self):
        root, config = self.r_repo()
        index = build_rust_index(root, config, self.extractor, self.classifier)
        edges, roots = reachable_rust_edges(index, "Port", ["safe_send", "unsafe_send", "direct_ffi"])
        self.assertTrue(all(row["status"] == "resolved_root" for row in roots))
        by_caller = {}
        for edge in edges:
            by_caller.setdefault(edge["caller"], []).append(edge)
        safe_ffi = [edge for edge in by_caller["safe_send"] if edge["target_kind"] == "rust_ffi"]
        self.assertEqual(len(safe_ffi), 1)
        self.assertEqual(safe_ffi[0]["target_name"], "xQueueGenericSend")
        self.assertEqual(safe_ffi[0]["caller_kind"], "safe_fn")
        self.assertEqual(safe_ffi[0]["in_unsafe_context"], "true")
        unsafe_ffi = [edge for edge in by_caller["unsafe_send"] if edge["target_kind"] == "rust_ffi"]
        self.assertEqual(len(unsafe_ffi), 1)
        self.assertEqual(unsafe_ffi[0]["caller_kind"], "unsafe_fn")
        direct_ffi = [edge for edge in by_caller["direct_ffi"] if edge["target_kind"] == "rust_ffi"]
        self.assertEqual(len(direct_ffi), 1)
        self.assertEqual(direct_ffi[0]["caller_kind"], "safe_fn")
        self.assertEqual(direct_ffi[0]["in_unsafe_context"], "true")

        app = application_method_calls(index, "Port", ["safe_send"], 2)
        self.assertEqual(len(app), 1)
        self.assertEqual(app[0]["caller"], "sample")
        self.assertEqual(app[0]["target_name"], "safe_send")
        self.assertEqual(app[0]["level"], "A_application")

    def test_rust_arguments_checks_and_return_handling(self):
        root, config = self.r_repo()
        index = build_rust_index(root, config, self.extractor, self.classifier)
        edges, _ = reachable_rust_edges(index, "Port", ["safe_send", "unsafe_send", "direct_ffi", "bridge_alloc"])
        safe_ffi = next(edge for edge in edges if edge["caller"] == "safe_send" and edge["target_kind"] == "rust_ffi")
        args, mapping, _ = map_arguments("rust", safe_ffi["source_args"], safe_ffi["target_params"], safe_ffi["target_kind"])
        self.assertEqual(mapping, "positional")
        self.assertTrue(args[1]["raw_pointer"])
        self.assertTrue(args[2]["derived_size"])
        unsafe_ffi = next(edge for edge in edges if edge["caller"] == "unsafe_send" and edge["target_kind"] == "rust_ffi")
        unsafe_args, _, _ = map_arguments("rust", unsafe_ffi["source_args"], unsafe_ffi["target_params"], unsafe_ffi["target_kind"])
        self.assertTrue(unsafe_args[0]["raw_pointer"])
        self.assertTrue(unsafe_args[2]["explicit_size"])
        alloc = next(edge for edge in edges if edge["caller"] == "bridge_alloc" and edge["target_kind"] == "rust_ffi")
        alloc_args, _, _ = map_arguments("rust", alloc["source_args"], alloc["target_params"], alloc["target_kind"])
        self.assertTrue(alloc_args[0]["explicit_size"])

        checks = []
        for function in index["facts"]["functions"]:
            if function.get("name") in ("safe_send", "unsafe_send", "direct_ffi"):
                checks.extend(classify_check(check) for check in function.get("checks", []))
        self.assertTrue(checks)
        self.assertTrue(any(check["check_status"] == "observed" for check in checks))
        self.assertTrue(any(check["size_or_bound_check"] for check in checks))
        self.assertEqual(return_handling({"call_context": "returned"}, []), "propagated")
        self.assertEqual(return_handling({"call_context": "function_tail"}, []), "propagated")
        self.assertEqual(return_handling({"call_context": "discarded"}, []), "discarded")
        self.assertEqual(return_handling({"call_context": "argument"}, []), "used_as_argument")
        self.assertEqual(return_handling({"call_context": "unclassified"}, []), "unresolved")

    def test_rust_unresolved_ambiguous_macro_inactive_and_nested(self):
        root, config = self.r_repo()
        index = build_rust_index(root, config, self.extractor, self.classifier)
        edges, _ = reachable_rust_edges(index, "Port", ["missing", "ambiguous", "macro_use"])
        self.assertTrue(any(edge["target_name"] == "missing_symbol" and edge["target_kind"] == "rust_ffi" for edge in edges))
        self.assertTrue(any(edge["target_name"] == "ambiguous" and edge["target_kind"] == "ambiguous_project_function" for edge in edges))
        macro_function = next(function for function in index["facts"]["functions"] if function.get("name") == "macro_use")
        self.assertTrue(any(macro.get("macro") == "unknown_macro" for macro in macro_function.get("macro_uses", [])))
        inactive_function = next(function for function in index["facts"]["functions"] if function.get("name") == "inactive")
        self.assertEqual(inactive_function.get("active"), "false")
        self.assertTrue(all(call.get("active") == "false" for call in inactive_function.get("calls", [])))
        self.assertEqual([edge for edge in edges if edge["caller"] == "inactive"], [])
        self.assertIn("a::ambiguous", {function.get("name") for function in index["facts"]["functions"]})
        self.assertIn("b::ambiguous", {function.get("name") for function in index["facts"]["functions"]})

    def test_ffi_symbol_mapping_bridge_external_unresolved_and_ambiguous(self):
        c_root, _, bridge_config = self.c_repo()
        r_root, rust_config = self.r_repo()
        c_index = extract_c_index(c_root, bridge_config)
        rust_index = build_rust_index(r_root, rust_config, self.extractor, self.classifier)
        rows = {row["rust_name"]: row for row in ffi_rows(r_root, rust_index, c_index)}
        self.assertEqual(rows["bridge_send"]["c_target_kind"], "c_bridge_function")
        self.assertEqual(rows["bridge_send"]["resolution_status"], "resolved_project_definition")
        self.assertEqual(rows["xQueueGenericSend"]["c_target_kind"], "rtos_api")
        self.assertEqual(rows["xQueueGenericSendFromISR"]["c_target_kind"], "rtos_api")
        self.assertNotEqual(rows["xQueueGenericSend"]["c_target_line"], rows["xQueueGenericSendFromISR"]["c_target_line"])
        self.assertEqual(rows["missing_symbol"]["resolution_status"], "unresolved")
        self.assertEqual(rows["ambiguous_symbol"]["resolution_status"], "ambiguous_c_symbol")
        bridge_edges, _ = reachable_edges(c_index, ["bridge_send"])
        self.assertTrue(any(edge["target_name"] == "xQueueGenericSend" for edge in bridge_edges))

    def test_c_callback_loop_roles_and_subscripts(self):
        root, config, _ = self.c_repo()
        index = extract_c_index(root, config)
        registrations = [row for row in index["callback_registrations"] if row["caller"] == "c_create_with_callback"]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0]["callback_symbol"], "c_task_entry")
        self.assertEqual(registrations[0]["resolution_status"], "resolved_callback_definition")
        unresolved = [row for row in index["callback_registrations"] if row["caller"] == "c_create_unresolved_callback"]
        self.assertEqual(unresolved[0]["resolution_status"], "unresolved_callback_definition")
        callback_edges, _ = reachable_edges(index, ["c_task_entry"])
        self.assertTrue(any(edge["target_name"] == "xQueueGenericSend" for edge in callback_edges))

        loop_edges, _ = reachable_edges(index, ["c_loop_check"])
        loop_call = next(edge for edge in loop_edges if edge["target_name"] == "xPortGetFreeHeapSize")
        self.assertEqual(loop_call["call_context"], "condition_check")
        loop_checks = [classify_check(check) for check in index["checks"] if check["function"] == "c_loop_check"]
        self.assertTrue(any(check["size_or_bound_check"] for check in loop_checks))

        wanted, _ = reachable_edges(index, ["c_wanted_size"])
        wanted_args, _, _ = map_arguments("c", wanted[0]["source_args"], wanted[0]["target_params"], wanted[0]["target_kind"])
        self.assertTrue(wanted_args[0]["explicit_size"])

        stack, _ = reachable_edges(index, ["c_create_with_stack_depth"])
        stack_call = next(edge for edge in stack if edge["target_name"] == "xTaskCreate")
        stack_args, _, _ = map_arguments("c", stack_call["source_args"], stack_call["target_params"], stack_call["target_kind"])
        self.assertTrue(stack_args[2]["explicit_size"])
        self.assertTrue(stack_args[0]["callback"])

        subscript, _ = reachable_edges(index, ["c_subscript_pointer"])
        sub_args, _, _ = map_arguments("c", subscript[0]["source_args"], subscript[0]["target_params"], subscript[0]["target_kind"])
        self.assertTrue(sub_args[0]["raw_data_pointer"])
        self.assertFalse(sub_args[0]["derived_size"])

        create, _ = reachable_edges(index, ["c_create_checked"])
        create_call = next(edge for edge in create if edge["target_name"] == "xTaskCreate")
        check = next(check for check in index["checks"] if check["function"] == "c_create_checked")
        self.assertEqual(classify_check(check)["check_kind"], "status_or_error_check")

    def test_rust_queue_callback_loop_tail_and_assignment(self):
        root, config = self.r_repo()
        index = build_rust_index(root, config, self.extractor, self.classifier)
        edges, _ = reachable_rust_edges(index, "Port", ["queue_create", "loop_check", "assigned_call", "safe_send"])
        queue = next(edge for edge in edges if edge["caller"] == "queue_create" and edge["target_name"] == "xQueueGenericCreate")
        args, _, _ = map_arguments("rust", queue["source_args"], queue["target_params"], queue["target_kind"])
        self.assertEqual(args[0]["role"], "explicit_size")
        self.assertEqual(args[1]["role"], "derived_size")
        self.assertEqual(args[2]["role"], "queue_type_or_flags")
        self.assertTrue(args[1]["parameter_type"])
        unknown_args, _, _ = map_arguments("rust", ["x"], [{"name": "x", "type": {"kind": "unknown"}}], "rust_ffi")
        self.assertTrue(unknown_args[0]["unknown_role"])
        loop = next(edge for edge in edges if edge["caller"] == "loop_check" and edge["target_name"] == "xPortGetFreeHeapSize")
        self.assertEqual(loop["call_context"], "condition_check")
        loop_checks = [classify_check(check) for check in next(f for f in index["facts"]["functions"] if f.get("name") == "loop_check")["checks"]]
        self.assertTrue(any(check["size_or_bound_check"] for check in loop_checks))
        assigned = next(edge for edge in edges if edge["caller"] == "assigned_call" and edge["target_name"] == "xQueueGenericSend")
        self.assertEqual(assigned["call_context"], "stored")
        tail = next(edge for edge in edges if edge["caller"] == "safe_send" and edge["target_name"] == "xQueueGenericSend")
        self.assertEqual(tail["call_context"], "function_tail")
        self.assertEqual(return_handling(tail, []), "propagated")
        registrations = [row for row in index["callback_registrations"] if row["caller"] == "create_with_callback"]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0]["callback_symbol"], "callback_entry")
        callback_edges, _ = reachable_rust_functions(index, ["callback_entry"])
        self.assertTrue(any(edge["target_name"] == "xQueueGenericSend" for edge in callback_edges))

    def test_production_exposure_views_and_common_counts(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-stage2-prod-views-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output"
        run_matched_api(REPO, output)
        comparison = list(csv.DictReader((output / "exposure-comparison.csv").open()))
        c_common = [row for row in comparison if row["view"] == "common_harness" and row["level"] == "A" and row["language"] == "C"]
        rust_common = [row for row in comparison if row["view"] == "common_harness" and row["level"] == "A" and row["language"] == "Rust"]
        self.assertTrue(c_common and rust_common)
        self.assertTrue(all(int(row["dispatch_context_arguments"]) > 0 for row in c_common))
        self.assertTrue(all(int(row["dispatch_context_arguments"]) == 0 for row in rust_common))
        coverage = json.loads((output / "coverage.json").read_text())
        self.assertGreater(coverage["common_harness_call_sites"], 0)
        self.assertGreater(coverage["common_harness_check_occurrences"], 0)
        self.assertLess(coverage["common_harness_check_occurrences"], coverage["check_occurrences"])
        call_sites = list(csv.DictReader((output / "call-sites.csv").open()))
        common_now = [row for row in call_sites if row["common_harness"] == "true" and row["target_name"] in ("now_ticks", "port_now_ticks")]
        self.assertTrue(common_now)
        self.assertLessEqual(len({row["source_occurrence_id"] for row in common_now}), 4)
        checks = list(csv.DictReader((output / "checks.csv").open()))
        self.assertLessEqual(len({row["source_check_id"] for row in checks}), len(checks))

    def test_call_site_memberships_are_canonical(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-membership-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output"
        run_matched_api(REPO, output)
        fields = list(csv.DictReader((output / "call-sites.csv").open()).fieldnames)
        self.assertNotIn("cases", fields)
        self.assertIn("case_count", fields)
        sites = list(csv.DictReader((output / "call-sites.csv").open()))
        memberships = list(csv.DictReader((output / "case-memberships.csv").open()))
        self.assertEqual(len(sites), len({row["call_site_id"] for row in sites}))
        self.assertEqual(len(memberships), sum(int(row["case_count"]) for row in sites))
        coverage = json.loads((output / "coverage.json").read_text())
        self.assertEqual(len(memberships), coverage["per_case_call_memberships"])
        common = [row for row in sites if row["common_harness"] == "true"]
        self.assertEqual(len(common), 4)
        self.assertTrue(all(row["case_count"] == "26" for row in common))
        for row in common:
            self.assertEqual(sum(1 for member in memberships if member["call_site_id"] == row["call_site_id"]), 26)

    def test_unresolved_callback_pipeline_is_partial(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-unresolved-callback-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output"
        real_extract = pipeline_module.extract_c_index

        def fake_extract(repo_root, config):
            index = real_extract(repo_root, config)
            if config["configuration"] == "freertos-stm32f4-c-benchmark":
                for registration in index.get("callback_registrations", []):
                    if registration.get("caller") == "port_handoff_create":
                        registration["resolution_status"] = "unresolved_callback_definition"
                        registration["callback_symbol"] = "missing_callback"
            return index

        with mock.patch.object(pipeline_module, "extract_c_index", side_effect=fake_extract):
            result = run_matched_api(REPO, output)
        case6 = next(row for row in csv.DictReader((output / "case-coverage.csv").open()) if row["thesis_case"] == "6")
        self.assertEqual(case6["callback_status"], "partial")
        self.assertEqual(case6["external_entry_status"], "partial")
        self.assertEqual(case6["exposure_status"], "partial")
        self.assertIn("callback", case6["unresolved_evidence"].lower())
        self.assertTrue((output / "manifest.json").is_file())
        self.assertTrue((output / "validation.json").is_file())
        self.assertGreaterEqual(result["coverage"]["partial_callback_registrations"], 1)

    def test_reporting_units_b01_b04(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-reporting-units-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output"
        result = run_matched_api(REPO, output)
        coverage = result["coverage"]
        with (output / "call-arguments.csv").open() as handle:
            args = list(csv.DictReader(handle))
        with (output / "case-comparison.csv").open() as handle:
            comparison = list(csv.DictReader(handle))
        with (output / "checks.csv").open() as handle:
            checks = list(csv.DictReader(handle))
        with (output / "c-bridge-definitions.csv").open() as handle:
            bridges = list(csv.DictReader(handle))

        def memberships(language, key, level=None):
            rows = [row for row in args if row["language"] == language and row[key] == "true"]
            if level:
                rows = [row for row in rows if level in json.loads(row["levels"])]
            return sum(len(json.loads(row["cases"])) for row in rows)

        def occurrences(language, key, level=None):
            rows = [row for row in args if row["language"] == language and row[key] == "true"]
            if level:
                rows = [row for row in rows if level in json.loads(row["levels"])]
            return len(rows)

        self.assertEqual(coverage["c_application_explicit_size_memberships"], memberships("c", "explicit_size", "A"))
        self.assertEqual(coverage["rust_application_explicit_size_memberships"], memberships("rust", "explicit_size", "A"))
        self.assertEqual(coverage["c_application_derived_size_memberships"], 0)
        self.assertEqual(coverage["rust_application_derived_size_memberships"], 0)
        self.assertEqual(coverage["c_all_level_explicit_size_occurrences"], occurrences("c", "explicit_size"))
        self.assertEqual(coverage["rust_all_level_explicit_size_occurrences"], occurrences("rust", "explicit_size"))
        self.assertEqual(coverage["c_all_level_explicit_size_occurrences"], 46)
        self.assertEqual(coverage["rust_all_level_explicit_size_occurrences"], 30)
        self.assertEqual(coverage["c_all_level_explicit_size_memberships"], 48)
        self.assertEqual(coverage["rust_all_level_explicit_size_memberships"], 47)
        self.assertEqual(coverage["c_all_level_derived_size_occurrences"], 5)
        self.assertEqual(coverage["rust_all_level_derived_size_occurrences"], 1)
        self.assertNotIn("explicit_size_arguments", comparison[0])
        self.assertNotIn("derived_size_arguments", comparison[0])
        self.assertEqual(sum(int(row["c_application_explicit_size_memberships"]) for row in comparison), 2)
        self.assertEqual(sum(int(row["rust_application_explicit_size_memberships"]) for row in comparison), 3)
        self.assertEqual(sum(int(row["c_all_level_explicit_size_memberships"]) for row in comparison), 48)
        self.assertEqual(sum(int(row["rust_all_level_explicit_size_memberships"]) for row in comparison), 47)

        self.assertEqual(coverage["c_bridge_inventory_scope"], "selected/configured/reached Stage 2 bridge definitions")
        self.assertFalse(coverage["c_bridge_inventory_includes_all_c_support_definitions"])
        self.assertTrue(all(row["inventory_scope"] == "selected_configured_reached" for row in bridges))
        self.assertNotIn("bench_trigger_isr_measure", {row["target_name"] for row in bridges})
        self.assertEqual(coverage["selected_unused_c_bridge_definitions"], 1)

        self.assertEqual(coverage["unsafe_context_call_occurrences"], 250)
        self.assertEqual(coverage["unsafe_context_per_case_memberships"], 288)
        self.assertEqual(coverage["unsafe_context_distinct_source_identities"], 250)
        self.assertEqual(sum(int(row["unsafe_context_memberships"]) for row in comparison), 288)
        self.assertEqual(coverage["check_occurrences"], 484)
        self.assertEqual(coverage["mechanism_specific_check_occurrences"], 445)
        self.assertEqual(coverage["common_harness_check_occurrences"], 39)
        self.assertEqual(sum(int(row["mechanism_specific_check_memberships"]) for row in comparison), 490)
        self.assertEqual(sum(int(row["common_harness_check_memberships"]) for row in comparison), 1014)
        common = [row for row in checks if row["common_harness"] == "true"]
        self.assertEqual(len(common), 39)
        self.assertFalse(any(row["mechanism_specific"] == "true" for row in common))

    def test_production_pipeline_integration_and_manifest(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-stage2-prod-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output"
        result = run_matched_api(REPO, output)
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertEqual(result["coverage"]["verified_roots"], 26)
        self.assertEqual(result["coverage"]["selected_cases"], 26)
        self.assertGreater(result["coverage"]["matched_rtos_api_targets"], 0)
        self.assertEqual(result["coverage"]["selected_unused_c_bridge_definitions"], 1)
        self.assertTrue((output / "benchmark-roots.csv").is_file())
        roots = list(csv.DictReader((output / "benchmark-roots.csv").open()))
        self.assertEqual({row["benchmark_case"] for row in roots}, {str(i) for i in range(1, 27)})
        self.assertTrue((output / "matched-operations.csv").is_file())
        self.assertTrue((output / "exposure-summary.csv").is_file())
        self.assertTrue((output / "exposure-comparison.csv").is_file())
        self.assertTrue((output / "rtos-target-relationships.csv").is_file())
        self.assertTrue((output / "c-bridge-definitions.csv").is_file())
        self.assertTrue((output / "callback-registrations.csv").is_file())
        self.assertTrue((output / "path-coverage.csv").is_file())
        self.assertTrue((output / "macro-sites.csv").is_file())
        rtos = list(csv.DictReader((output / "rtos-target-relationships.csv").open()))
        bridges = list(csv.DictReader((output / "c-bridge-definitions.csv").open()))
        self.assertFalse({row["target_name"] for row in rtos} & {row["target_name"] for row in bridges})
        unused = [row for row in bridges if row["match_status"] == "unused_c_bridge_definition"]
        self.assertEqual([row["target_name"] for row in unused], ["bench_frag_measure"])
        self.assertFalse(any(row["body_traversed"] == "true" or row["c_call_site_ids"] != "[]" for row in unused))
        self.assertEqual(result["coverage"]["rtos_api_c_only_targets"], 0)
        self.assertEqual(result["coverage"]["selected_cases"], 26)
        self.assertEqual(len({row["benchmark_case"] for row in csv.DictReader((output / "benchmark-roots.csv").open())}), 26)
        case_coverage = list(csv.DictReader((output / "case-coverage.csv").open()))
        self.assertEqual(len(case_coverage), 26)
        self.assertEqual({row["thesis_case"] for row in case_coverage}, {str(i) for i in range(1, 27)})
        case_comparison = list(csv.DictReader((output / "case-comparison.csv").open()))
        self.assertEqual(len(case_comparison), 26)
        comparison = list(csv.DictReader((output / "exposure-comparison.csv").open()))
        for case_number in ("2", "5", "6", "16", "26"):
            c_level_c = [row for row in comparison if row["benchmark_case"] == case_number and row["view"] == "combined" and row["level"] == "C" and row["language"] == "C"]
            self.assertTrue(c_level_c)
            self.assertTrue(any(int(row["underlying_rtos_calls"]) > 0 for row in c_level_c))
            if case_number != "16":
                rust_c = [row for row in comparison if row["benchmark_case"] == case_number and row["view"] == "combined" and row["level"] == "C" and row["language"] == "Rust"]
                self.assertTrue(any(int(row["underlying_rtos_calls"]) > 0 for row in rust_c))
            rust_b = [row for row in comparison if row["benchmark_case"] == case_number and row["view"] == "combined" and row["level"] == "B" and row["language"] == "Rust"]
            self.assertTrue(rust_b)
        rtos_rows = list(csv.DictReader((output / "rtos-target-relationships.csv").open()))
        self.assertTrue(any(("c_bridge" in row["rust_path_origins"] or "shared_c_isr" in row["rust_path_origins"]) for row in rtos_rows))
        self.assertTrue(any(row["rust_cross_language_path_ids"] != "[]" for row in rtos_rows))
        checks = list(csv.DictReader((output / "checks.csv").open()))
        self.assertLess(len({row["source_check_id"] for row in checks}), len(checks))
        self.assertTrue(any(row["common_harness"] == "true" for row in checks))
        self.assertTrue(any(row["mechanism_specific"] == "true" for row in checks))
        self.assertTrue(any(row["ordering_check"] == "true" and "wake <" in row["condition"] for row in checks))
        self.assertTrue(any(row["call_result_check"] == "true" and "frag_alloc" in row["condition"] for row in checks))
        self.assertTrue(any("xTaskCreate" in row["condition"] and row["check_kind"] == "status_or_error_check" for row in checks))
        macros = list(csv.DictReader((output / "macro-sites.csv").open()))
        self.assertEqual(len(macros), result["coverage"]["macro_sites_observed"])
        self.assertEqual(sum(1 for row in macros if row["macro_effect"] == "unreachable_match_arm"), 23)
        self.assertTrue(any(row["macro_effect"] == "unexpanded_macro" for row in macros))
        self.assertTrue(all(row["resolution_status"] == "unresolved" for row in macros))
        callbacks = list(csv.DictReader((output / "callback-registrations.csv").open()))
        self.assertGreaterEqual(len(callbacks), 2)
        self.assertTrue(all(row["resolution_status"] == "resolved_callback_definition" for row in callbacks))
        self.assertTrue(any(row["benchmark_case"] == "6" and row["callback_symbol"] == "peer_entry" for row in callbacks))
        self.assertTrue(any(row["benchmark_case"] == "6" and row["callback_symbol"] == "handoff_peer_entry" for row in callbacks))
        manifest = json.loads((output / "manifest.json").read_text())
        for item in manifest["outputs"]:
            path = output / item["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"], item["path"])
            self.assertEqual(path.stat().st_size, item["bytes"], item["path"])
        validation = json.loads((output / "validation.json").read_text())
        self.assertTrue(validation["stage1_manifest_verified"])
        self.assertTrue(validation["all_26_roots_verified"])
        self.assertTrue(validation["source_revision_recorded"])
        for path in output.iterdir():
            if path.is_file():
                self.assertNotIn(str(REPO), path.read_text(errors="replace"))


if __name__ == "__main__":
    unittest.main()
