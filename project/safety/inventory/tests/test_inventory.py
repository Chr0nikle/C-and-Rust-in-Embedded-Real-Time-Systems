"""Regression tests for the Stage 1 operation inventory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from inventory.c_ops import extract_c_scope
from common.layers import LayerClassifier
from inventory.pipeline import _dedup, _summary
from inventory.rust_ops import extract_rust_scope
from common.config import resolve_repo_root, tool_versions
from common.extractor import build_extractor

HERE = Path(__file__).resolve().parent
SAFETY = HERE.parent.parent
FIXTURES = HERE / "fixtures"
CLANG = ["-target", "arm-none-eabi", "-mcpu=cortex-m4", "-mthumb"]
TARGET = {"arch": "arm", "os": "none", "family": "none", "pointer_width": "32"}
REPO_ROOT_ENV = "UASTW_REPO_ROOT"
ISOLATION_GUARD_ENV = "UASTW_ISOLATED_SUITE"


def _repo_root() -> Path:
    return resolve_repo_root(SAFETY)


def _staged_sources() -> bool:
    try:
        return (_repo_root() / ".local" / "source").is_dir()
    except RuntimeError:
        return False


STAGED_SOURCES = _staged_sources()


def _operating_rows(operations, function):
    return [row for row in operations if row.get("function") == function]


def _global_categories(operations, function, name):
    return Counter(
        row["category"]
        for row in operations
        if row.get("function") == function and (row.get("details") or {}).get("global") == name
    )


def _first_json_difference(left, right, path="") -> str | None:
    if type(left) is not type(right):
        return f"{path or '$'}: type {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{path}.{key}: missing on one side"
            found = _first_json_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_json_difference(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if left == right else f"{path}: {left!r} != {right!r}"


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extractor = build_extractor(SAFETY)
        cls.classifier = LayerClassifier(SAFETY / "specs" / "layers.json")
        cls.spec = json.loads((SAFETY / "specs" / "counting-spec.json").read_text())

    def c_repo(self, source="ops_a.c", main_source="ops_b.c", extra_args=None):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-inventory-c-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        src = root / "firmware/c-kernels/freertos/c/stm32f4/src"
        inc = root / "firmware/common/c/include"
        third_party = root / ".local/thirdparty/include"
        src.mkdir(parents=True)
        inc.mkdir(parents=True)
        third_party.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "c/ops_fixture.h", inc / "ops_fixture.h")
        shutil.copyfile(FIXTURES / "c/third_party_macros.h", third_party / "third_party_macros.h")
        shutil.copyfile(FIXTURES / "c" / source, src / "bench.c")
        clang_args = CLANG + ["-I", str(inc), "-I", str(third_party)] + list(extra_args or [])
        units = [
            {"path": "firmware/c-kernels/freertos/c/stm32f4/src/bench.c", "abs_path": str(src / "bench.c"), "clang_args": clang_args},
        ]
        if main_source:
            shutil.copyfile(FIXTURES / "c" / main_source, src / "main.c")
            units.append({"path": "firmware/c-kernels/freertos/c/stm32f4/src/main.c", "abs_path": str(src / "main.c"), "clang_args": clang_args})
        config = {"configuration": "freertos-stm32f4-c-benchmark", "rust_mode": 0, "translation_units": units}
        return root, config

    def rust_repo(self, source="ops_fixture.rs"):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-inventory-rust-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        src = root / "firmware/c-kernels/freertos/rust/src"
        src.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "rust" / source, src / "boundary.rs")
        config = {"configuration": "freertos-stm32f4-rust", "files": ["firmware/c-kernels/freertos/rust/src/boundary.rs"], "target": TARGET}
        return root, config

    def test_c_categories_and_locations(self):
        root, config = self.c_repo()
        operations, functions, kernel_calls, meta = extract_c_scope(root, config)
        categories = {row["category"] for row in operations}
        for category in (
            "pointer_dereference", "pointer_field_access", "pointer_arithmetic",
            "pointer_conversion", "macro_pointer_conversion", "array_index",
            "mutable_global_read", "mutable_global_write", "mutable_global_readwrite",
            "mutable_global_address", "union_field_access", "dynamic_allocation",
            "deallocation", "kernel_api_call",
        ):
            self.assertIn(category, categories, category)
        self.assertTrue(all(row["source_file"] and row["source_line"] for row in operations))
        self.assertTrue(all(row.get("source_identity_key") for row in operations))
        self.assertTrue(any(row["function"] == "pointer_ops" for row in operations))
        self.assertEqual(meta["parse_failures"], [])
        self.assertTrue(any(call["callee"] == "xQueueGenericCreate" for call in kernel_calls))

    def test_c_declarations_are_not_definitions_and_header_is_shared(self):
        root, config = self.c_repo()
        operations, functions, kernel_calls, meta = extract_c_scope(root, config)
        declarations = [row for row in functions if row["name"] == "pointer_ops" and not row["is_definition"]]
        definitions = [row for row in functions if row["name"] == "pointer_ops" and row["is_definition"]]
        self.assertTrue(declarations)
        self.assertTrue(definitions)
        merged = _dedup(functions, ("configuration", "language", "name", "file", "line", "column", "is_definition"))
        self.assertLessEqual(len(merged), len(functions))
        self.assertEqual(len({row["occurrence_id"] for row in operations}), len(operations))

    def test_c_pointer_declaration_is_not_dereference(self):
        root, config = self.c_repo()
        operations, _, _, _ = extract_c_scope(root, config)
        derefs = [row for row in operations if row["category"] == "pointer_dereference"]
        declarations = [row for row in operations if row["category"] == "pointer_declaration"]
        self.assertFalse(declarations)
        self.assertTrue(derefs)

    def test_c_pointer_conversion_provenance(self):
        root, config = self.c_repo()
        operations, _, _, _ = extract_c_scope(root, config)
        rows = _operating_rows(operations, "conversion_ops")
        explicit = [row for row in rows if row["category"] == "pointer_conversion"]
        macro = [row for row in rows if row["category"] == "macro_pointer_conversion"]
        self.assertEqual(len(explicit), 7)
        self.assertTrue(all(row["provenance"] == "explicit_source" for row in explicit))
        directions = Counter(row["details"]["direction"] for row in explicit)
        self.assertEqual(directions["pointer_to_pointer"], 5)
        self.assertEqual(directions["integer_to_pointer"], 2)
        self.assertEqual(len(macro), 2)
        self.assertEqual(
            Counter(row["provenance"] for row in macro),
            Counter({"project_macro_expansion": 1, "third_party_macro_expansion": 1}),
        )
        # No macro-expanded cast may leak into the explicit category.
        self.assertFalse(any(row["provenance"] != "explicit_source" for row in explicit))
        # The inactive preprocessor branch must not be parsed as an operation.
        self.assertEqual(len(explicit) + len(macro), 9)
        self.assertEqual(len({row["source_identity_key"] for row in explicit + macro}), 9)

    def test_c_explicit_cast_in_macro_argument(self):
        root, config = self.c_repo(source="macro_argument.c", main_source=None)
        operations, _, _, _ = extract_c_scope(root, config)
        explicit = [row for row in operations if row["category"] == "pointer_conversion"]
        macro = [row for row in operations if row["category"] == "macro_pointer_conversion"]
        self.assertEqual(len(explicit), 1)
        self.assertEqual(len(macro), 1)
        cast = explicit[0]
        self.assertEqual(cast["provenance"], "explicit_macro_argument")
        self.assertEqual(cast["details"]["macro_definition_status"], "argument_not_definition")
        self.assertEqual(cast["details"]["macro_invocation"], "ID")
        self.assertNotIn("macro_definition_file", cast["details"])
        # The operation points at the cast expression, not at the macro name.
        self.assertEqual(cast["details"]["macro_argument_line"], cast["source_line"])
        self.assertEqual(cast["details"]["macro_argument_column"], cast["source_column"])
        source_line = (root / cast["source_file"]).read_text().splitlines()[cast["source_line"] - 1]
        self.assertIn("(char *)p", source_line)
        generated = macro[0]
        self.assertEqual(generated["provenance"], "project_macro_expansion")
        self.assertEqual(generated["details"]["macro_definition_status"], "definition")
        self.assertEqual(generated["details"]["macro_definition_file"], generated["source_file"])
        self.assertEqual(generated["details"]["macro_definition_line"], 4)
        self.assertNotEqual(generated["source_column"], cast["source_column"])
        self.assertEqual(len({row["source_identity_key"] for row in explicit + macro}), 2)

    def test_c_macro_argument_collapses_macro_body_casts(self):
        root, config = self.c_repo(source="macro_argument_collapse.c", main_source=None)
        operations, _, _, _ = extract_c_scope(root, config)
        explicit = [row for row in operations if row["category"] == "pointer_conversion"]
        macro = [row for row in operations if row["category"] == "macro_pointer_conversion"]
        self.assertEqual(len(explicit), 3)
        self.assertEqual(len(macro), 0)
        self.assertTrue(all(row["provenance"] == "explicit_macro_argument" for row in explicit))
        both = [row for row in explicit if row["function"] == "both"]
        two_args = [row for row in explicit if row["function"] == "two_args"]
        self.assertEqual(len(both), 1)
        self.assertEqual(len(two_args), 2)
        self.assertGreaterEqual(len(both[0]["details"].get("collapsed_macro_pointer_conversions", [])), 1)
        self.assertEqual(len({row["source_identity_key"] for row in explicit}), 3)

    def test_c_unresolved_provenance_cast_is_visible(self):
        root, config = self.c_repo(
            source="unresolved_provenance.c",
            main_source=None,
            extra_args=["-DCLI_CAST(x)=((void*)(x))"],
        )
        operations, _, _, _ = extract_c_scope(root, config)
        unresolved = [row for row in operations if row["category"] == "unresolved_pointer_conversion"]
        self.assertEqual(len(unresolved), 1)
        row = unresolved[0]
        self.assertEqual(row["provenance"], "unresolved_provenance")
        self.assertEqual(row["resolution_status"], "unresolved")
        self.assertEqual(row["details"]["macro_definition_status"], "unresolved")
        self.assertEqual(row["details"]["direction"], "pointer_to_pointer")
        self.assertEqual([item for item in operations if item["category"] in ("pointer_conversion", "macro_pointer_conversion")], [])
        summary = _summary(operations, [], self.spec)
        self.assertEqual(summary["pointer_conversions"]["unresolved_provenance_records"], 1)
        self.assertEqual(summary["pointer_conversions"]["explicit_source_records"], 0)

    def test_c_nested_macro_cast_deduplication(self):
        root, config = self.c_repo(source="macro_nested.c", main_source=None)
        operations, _, _, _ = extract_c_scope(root, config)
        macro = [row for row in operations if row["category"] == "macro_pointer_conversion"]
        self.assertEqual(len(macro), 1)
        row = macro[0]
        self.assertEqual(row["provenance"], "project_macro_expansion")
        self.assertEqual(row["details"]["macro_definition_status"], "definition")
        self.assertIn("CSTYLE_CAST_EXPR", row["expression"])
        self.assertEqual([item for item in operations if item["category"] == "pointer_conversion"], [])

    def test_c_postfix_pointer_arithmetic(self):
        root, config = self.c_repo()
        operations, _, _, _ = extract_c_scope(root, config)
        rows = [row for row in _operating_rows(operations, "arithmetic_ops") if row["category"] == "pointer_arithmetic"]
        self.assertEqual(len(rows), 8)
        forms = Counter(row["details"]["form"] for row in rows)
        self.assertEqual(forms, Counter({"postfix": 2, "prefix": 2, "compound": 2, "binary": 2}))
        expressions = {row["expression"].replace(" ", "") for row in rows}
        for expected in ("p++", "++p", "p--", "--p", "p+=2", "p-=2", "p+1", "p-1"):
            self.assertIn(expected, expressions)
        # Ordinary integer increments are not pointer arithmetic.
        self.assertFalse(any(row["expression"].replace(" ", "").startswith("n") for row in rows))
        # Array indexing remains a separate category.
        self.assertTrue(any(row["category"] == "array_index" for row in _operating_rows(operations, "arithmetic_ops")))

    def test_compound_expression_can_yield_overlapping_records(self):
        root, config = self.c_repo()
        operations, _, _, _ = extract_c_scope(root, config)
        rows = _operating_rows(operations, "compound_overlap")
        categories = Counter(row["category"] for row in rows)
        self.assertEqual(categories["pointer_arithmetic"], 1)
        self.assertEqual(categories["mutable_global_readwrite"], 1)
        self.assertEqual(len(rows), 2)

    def test_c_global_access_context(self):
        root, config = self.c_repo()
        operations, _, _, _ = extract_c_scope(root, config)
        rows = _operating_rows(operations, "global_ops")
        self.assertEqual(
            _global_categories(rows, "global_ops", "g_count"),
            Counter({"mutable_global_write": 1, "mutable_global_read": 1, "mutable_global_readwrite": 3, "mutable_global_address": 1}),
        )
        self.assertEqual(
            _global_categories(rows, "global_ops", "s_line_len"),
            Counter({"mutable_global_read": 1, "mutable_global_readwrite": 1}),
        )
        self.assertEqual(_global_categories(rows, "global_ops", "s_line"), Counter({"mutable_global_write": 2}))
        self.assertEqual(_global_categories(rows, "global_ops", "g_ptr"), Counter({"mutable_global_read": 2, "mutable_global_unresolved": 2}))
        self.assertEqual(_global_categories(rows, "global_ops", "g_node"), Counter({"mutable_global_read": 1, "mutable_global_unresolved": 1}))
        self.assertEqual(_global_categories(rows, "global_ops", "g_struct"), Counter({"mutable_global_write": 2}))
        self.assertEqual(_global_categories(rows, "global_ops", "g_array"), Counter({"mutable_global_write": 2, "mutable_global_read": 1}))
        self.assertEqual(_global_categories(rows, "global_ops", "g_ptr_array"), Counter({"mutable_global_read": 1, "mutable_global_unresolved": 1}))
        # Address-taking is distinct from a value read.
        address = [row for row in rows if row["category"] == "mutable_global_address"]
        self.assertEqual(len(address), 1)
        self.assertEqual(address[0]["details"]["access"], "address_of")
        # Writing through a global pointer does not write the pointer object.
        pointer_writes = [row for row in rows if row["details"].get("global") in ("g_ptr", "g_node", "g_ptr_array") and "write" in row["category"]]
        self.assertEqual(pointer_writes, [])
        # Index reads are not array-element writes.
        s_line_len_writes = [row for row in rows if row["details"].get("global") == "s_line_len" and row["category"] == "mutable_global_write"]
        self.assertEqual(s_line_len_writes, [])

    def test_rust_categories_and_safe_reference(self):
        root, config = self.rust_repo()
        operations, functions, kernel_calls, unsafe, meta = extract_rust_scope(root, config, self.extractor, self.classifier)
        categories = {row["category"] for row in operations}
        for category in (
            "raw_pointer_dereference", "pointer_arithmetic", "pointer_conversion",
            "dynamic_allocation", "deallocation", "mutable_global_read",
            "mutable_global_write", "mutable_global_readwrite", "mutable_global_address",
            "mutable_global_unresolved", "kernel_api_call", "unresolved_macro",
        ):
            self.assertIn(category, categories, category)
        safe_derefs = [row for row in operations if row["function"] == "safe_reference" and row["category"] == "raw_pointer_dereference"]
        self.assertEqual(safe_derefs, [])
        self.assertTrue(any(row["context_type"] == "unsafe_block" for row in unsafe))
        self.assertTrue(any(row["context_type"] == "unsafe_function" for row in unsafe))
        self.assertTrue(all(row.get("source_identity_key") for row in operations))

    def test_rust_inactive_operations_are_not_active(self):
        root, config = self.rust_repo()
        operations, _, _, unsafe, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        inactive = [row for row in operations if row["function"] in ("inactive_riscv", "inactive_unsafe_block")]
        self.assertTrue(inactive)
        self.assertTrue(all(row["activity_status"] == "false" for row in inactive))
        inactive_contexts = [row for row in unsafe if row["function"] == "inactive_unsafe_block" and row["context_type"] == "unsafe_block"]
        self.assertTrue(inactive_contexts)
        self.assertTrue(all(row["activity_status"] == "false" for row in inactive_contexts))

    def test_rust_nested_modules_and_qualified_identities(self):
        root, config = self.rust_repo(source="nested_modules.rs")
        operations, functions, _, contexts, meta = extract_rust_scope(root, config, self.extractor, self.classifier)
        by_name = {function["name"]: function for function in functions}
        for name in (
            "active_mod::nested_read",
            "active_mod::nested_unsafe",
            "active_mod::nested_block",
            "active_mod::inner::check",
            "other::check",
            "inactive_parent::child_without_cfg",
            "inactive_parent::inner::deeper",
            "arm_active::active_child",
            "arm_active::cfg_off_child",
        ):
            self.assertIn(name, by_name)
        self.assertTrue(by_name["active_mod::nested_read"]["active"] == "true")
        self.assertTrue(by_name["active_mod::inner::check"]["active"] == "true")
        self.assertTrue(by_name["other::check"]["active"] == "true")
        self.assertNotEqual("active_mod::inner::check", "other::check")
        self.assertEqual(by_name["inactive_parent::child_without_cfg"]["active"], "false")
        self.assertEqual(by_name["inactive_parent::inner::deeper"]["active"], "false")
        self.assertEqual(by_name["arm_active::active_child"]["active"], "true")
        self.assertEqual(by_name["arm_active::cfg_off_child"]["active"], "false")

        nested_read = [row for row in _operating_rows(operations, "active_mod::nested_read") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(nested_read), 1)
        self.assertEqual(nested_read[0]["activity_status"], "true")
        self.assertEqual(nested_read[0]["in_unsafe_block"], "true")

        nested_inner = [row for row in _operating_rows(operations, "active_mod::inner::nested_inner_read") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(nested_inner), 1)
        self.assertEqual(nested_inner[0]["activity_status"], "true")

        nested_unsafe = [row for row in _operating_rows(operations, "active_mod::nested_unsafe") if row["category"] == "raw_pointer_dereference"]
        self.assertEqual(len(nested_unsafe), 1)
        self.assertEqual(nested_unsafe[0]["in_unsafe_function"], "true")

        nested_block = [row for row in _operating_rows(operations, "active_mod::nested_block") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(nested_block), 1)
        self.assertEqual(nested_block[0]["in_unsafe_block"], "true")

        inactive_macros = [row for row in _operating_rows(operations, "inactive_parent::inactive_macro") if row["category"] == "unresolved_macro"]
        self.assertEqual(len(inactive_macros), 2)
        self.assertTrue(all(row["activity_status"] == "false" for row in inactive_macros))

        unresolved_modules = [item for item in meta["unsupported"] if item["category"] == "out_of_line_module"]
        self.assertTrue(any(item["module"] == "declared_only" for item in unresolved_modules))
        self.assertFalse(any(row["function"] == "declared_only" for row in operations))

    def test_unsafe_context_ids_reference_and_contain(self):
        root, config = self.rust_repo(source="nested_modules.rs")
        operations, _, _, contexts, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        by_id = {context["context_id"]: context for context in contexts}
        self.assertEqual(len(by_id), len(contexts))

        def contains(line, column, context):
            start_line = context["start_line"]
            start_column = context["start_column"]
            end_line = context["end_line"]
            end_column = context["end_column"]
            if start_line is None or end_line is None:
                return False
            if line < start_line or line > end_line:
                return False
            if line == start_line and column < start_column:
                return False
            if line == end_line and column > end_column:
                return False
            return True

        for operation in operations:
            if operation["language"] != "rust":
                continue
            line = operation["source_line"]
            column = operation["source_column"]
            ids = list(operation["unsafe_context_ids"])
            for context_id in ids:
                self.assertIn(context_id, by_id)
                context = by_id[context_id]
                self.assertEqual(context["source_file"], operation["source_file"])
                self.assertEqual(context["function"], operation["function"])
                self.assertTrue(contains(line, column, context))
                if operation["activity_status"] == "true":
                    self.assertEqual(context["activity_status"], "true")
            containing_blocks = {
                context["context_id"]
                for context in contexts
                if context["context_type"] == "unsafe_block"
                and context["source_file"] == operation["source_file"]
                and context["function"] == operation["function"]
                and contains(line, column, context)
            }
            self.assertEqual(
                {context_id for context_id in ids if by_id[context_id]["context_type"] == "unsafe_block"},
                containing_blocks,
            )
            function_context = [
                context for context in contexts
                if context["context_type"] == "unsafe_function"
                and context["source_file"] == operation["source_file"]
                and context["function"] == operation["function"]
                and operation["in_unsafe_function"] == "true"
            ]
            if operation["in_unsafe_function"] == "true":
                self.assertEqual(len(function_context), 1)
                self.assertIn(function_context[0]["context_id"], ids)
            else:
                self.assertFalse(any(context_id in ids and by_id[context_id]["context_type"] == "unsafe_function" for context_id in ids))

    def test_layer_aggregation_recomputed(self):
        root, config = self.rust_repo(source="nested_modules.rs")
        operations, _, _, contexts, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        summary = _summary(operations, contexts, self.spec)
        active_resolved = [row for row in operations if row["activity_status"] == "true" and row["resolution_status"] == "resolved"]
        self.assertEqual(
            summary["scopes"]["rust"]["active_resolved_by_layer"],
            dict(sorted(Counter(row["layer"] for row in active_resolved).items())),
        )
        self.assertEqual(
            summary["scopes"]["rust"]["active_resolved_by_category"],
            dict(sorted(Counter(row["category"] for row in active_resolved).items())),
        )

    def test_source_column_conventions(self):
        c_root, c_config = self.c_repo()
        c_operations, _, _, _ = extract_c_scope(c_root, c_config)
        c_row = next(row for row in c_operations if row["function"] == "global_ops" and row["category"] == "mutable_global_read" and (row["details"] or {}).get("global") == "g_count")
        c_source = (c_root / c_row["source_file"]).read_text().splitlines()[c_row["source_line"] - 1]
        self.assertTrue(c_source[c_row["source_column"] - 1:].startswith("g_count"))

        r_root, r_config = self.rust_repo()
        r_operations, _, _, _, _ = extract_rust_scope(r_root, r_config, self.extractor, self.classifier)
        r_row = next(row for row in r_operations if row["function"] == "compound_updates" and row["category"] == "mutable_global_read" and (row["details"] or {}).get("global") == "S_COUNT")
        r_source = (r_root / r_row["source_file"]).read_text().splitlines()[r_row["source_line"] - 1]
        self.assertTrue(r_source[r_row["source_column"]:].startswith("S_COUNT"))

    @unittest.skipUnless(STAGED_SOURCES, "staged RTOS sources unavailable; run scripts/setup/install.sh")
    def test_tool_version_metadata(self):
        versions = tool_versions(_repo_root())
        self.assertTrue(versions["clang"]["available"])
        self.assertIn("clang version", versions["clang"]["version"])
        self.assertTrue(versions["rustc"]["available"])
        self.assertIn("rustc", versions["rustc"]["version"])
        self.assertTrue(versions["cargo"]["available"])
        self.assertIn("cargo", versions["cargo"]["version"])
        self.assertTrue(versions["python"]["available"])
        self.assertIn("Python", versions["python"]["version"])
        self.assertTrue(versions["clang_tidy"]["available"])
        self.assertIn("LLVM version", versions["clang_tidy"]["version"])
        self.assertNotIn("/", versions["clang_tidy"]["version"])

    def test_rust_compound_static_mut_updates(self):
        root, config = self.rust_repo()
        operations, _, _, _, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        rows = _operating_rows(operations, "compound_updates")
        self.assertEqual(
            _global_categories(rows, "compound_updates", "S_COUNT"),
            Counter({"mutable_global_write": 1, "mutable_global_read": 1, "mutable_global_readwrite": 3}),
        )
        self.assertEqual(
            _global_categories(rows, "compound_updates", "S_ARR"),
            Counter({"mutable_global_write": 1, "mutable_global_readwrite": 1}),
        )
        self.assertEqual(
            _global_categories(rows, "compound_updates", "S_STRUCT"),
            Counter({"mutable_global_write": 1, "mutable_global_readwrite": 1}),
        )
        self.assertEqual(
            _global_categories(rows, "compound_updates", "S_PTR"),
            Counter({"mutable_global_read": 2, "mutable_global_unresolved": 2}),
        )
        self.assertFalse(any(row["category"] == "mutable_global_write" and row["details"].get("global") == "S_PTR" for row in rows))

    def test_rust_address_taking_and_addr_of_modelling(self):
        root, config = self.rust_repo()
        operations, _, _, _, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        rows = _operating_rows(operations, "address_taking")
        addresses = [row for row in rows if row["category"] == "mutable_global_address"]
        self.assertEqual(len(addresses), 4)
        self.assertEqual(Counter(row["provenance"] for row in addresses), Counter({"explicit_source": 2, "known_macro_model": 2}))
        macros = [row for row in rows if row["category"] == "unresolved_macro"]
        self.assertEqual(len(macros), 1)
        self.assertEqual(macros[0]["details"]["macro"], "unknown_macro")
        self.assertFalse(any(row["details"].get("macro") in ("addr_of", "addr_of_mut") for row in macros))

    def test_rust_pointer_return_resolution(self):
        root, config = self.rust_repo()
        operations, _, _, _, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        expected = {
            "use_project_ptr": ("resolved", 1),
            "use_project_ref": (None, 0),
            "function_alias": ("unresolved", 1),
            "use_ambiguous_ptr": ("unresolved", 1),
            "use_conditional_ptr": ("resolved", 1),
            "use_alias_return": ("unresolved", 1),
        }
        for function, (resolution, count) in expected.items():
            rows = [row for row in _operating_rows(operations, function) if row["category"] == "raw_pointer_dereference"]
            self.assertEqual(len(rows), count, function)
            if rows:
                self.assertEqual(rows[0]["resolution_status"], resolution, function)

    def test_rust_unsafe_context_association(self):
        root, config = self.rust_repo()
        operations, _, _, contexts, _ = extract_rust_scope(root, config, self.extractor, self.classifier)

        block_in_safe = [row for row in _operating_rows(operations, "unsafe_block_in_safe_fn") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(block_in_safe), 1)
        self.assertEqual(block_in_safe[0]["in_unsafe_block"], "true")
        self.assertEqual(block_in_safe[0]["in_unsafe_function"], "false")
        self.assertEqual(block_in_safe[0]["in_unsafe_context"], "true")

        function_without_block = [row for row in _operating_rows(operations, "unsafe_fn_without_block") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(function_without_block), 1)
        self.assertEqual(function_without_block[0]["in_unsafe_block"], "false")
        self.assertEqual(function_without_block[0]["in_unsafe_function"], "true")

        nested = [row for row in _operating_rows(operations, "nested_unsafe_blocks") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["in_unsafe_block"], "true")
        self.assertEqual(len(nested[0]["unsafe_context_ids"]), 2)

        both = [row for row in _operating_rows(operations, "unsafe_fn_with_block") if row["category"] == "mutable_global_read"]
        self.assertEqual(len(both), 1)
        self.assertEqual(both[0]["in_unsafe_block"], "true")
        self.assertEqual(both[0]["in_unsafe_function"], "true")

        safe_op = [row for row in _operating_rows(operations, "safe_operation_in_unsafe_block") if row["category"] == "mutable_global_address"]
        self.assertEqual(len(safe_op), 1)
        self.assertEqual(safe_op[0]["in_unsafe_context"], "true")
        self.assertEqual(safe_op[0]["unsafe_context_status"], "resolved")

        ffi = [row for row in _operating_rows(operations, "unsafe_ffi_call") if row["category"] == "kernel_api_call"]
        self.assertEqual(len(ffi), 1)
        self.assertEqual(ffi[0]["in_unsafe_context"], "true")

        outside = [row for row in _operating_rows(operations, "safe_calls_safe_wrapper")]
        self.assertTrue(outside)
        self.assertTrue(all(row["in_unsafe_context"] == "false" for row in outside))
        # The call to the safe wrapper is not a kernel/FFI operation and is not
        # labelled unsafe merely because it is called near unsafe code.
        self.assertFalse(any(row["category"] == "kernel_api_call" for row in outside))

        inactive = [row for row in _operating_rows(operations, "inactive_unsafe_block")]
        self.assertTrue(inactive)
        self.assertTrue(all(row["activity_status"] == "false" for row in inactive))
        self.assertTrue(inactive[0]["unsafe_context_ids"])
        inactive_contexts = [row for row in contexts if row["function"] == "inactive_unsafe_block" and row["context_type"] == "unsafe_block"]
        self.assertTrue(inactive_contexts)
        self.assertTrue(all(row["activity_status"] == "false" for row in inactive_contexts))

    def test_source_identity_across_configurations(self):
        root, benchmark = self.c_repo()
        support = dict(benchmark)
        support["configuration"] = "freertos-stm32f4-c-support-rust"
        support["rust_mode"] = 1
        benchmark_ops, _, _, _ = extract_c_scope(root, benchmark)
        support_ops, _, _, _ = extract_c_scope(root, support)
        benchmark_keys = {row["source_identity_key"] for row in benchmark_ops}
        support_keys = {row["source_identity_key"] for row in support_ops}
        self.assertEqual(benchmark_keys, support_keys)
        self.assertNotEqual(
            {row["occurrence_id"] for row in benchmark_ops},
            {row["occurrence_id"] for row in support_ops},
        )

        conversion = [row for row in benchmark_ops if row["function"] == "conversion_ops" and row["category"] == "pointer_conversion"]
        same_line = [group for group in {row["source_line"] for row in conversion} if sum(1 for row in conversion if row["source_line"] == group) > 1]
        self.assertTrue(same_line, "fixture must contain two conversions on one line")
        line = same_line[0]
        line_rows = [row for row in conversion if row["source_line"] == line]
        self.assertEqual(len(line_rows), 2)
        self.assertEqual(len({row["source_identity_key"] for row in line_rows}), 2)

        combined = [*benchmark_ops, *support_ops]
        for row in combined:
            row["scope"] = "c-benchmark" if row["configuration"] == benchmark["configuration"] else "c-support"
            row["scope_label"] = "C benchmark" if row["scope"] == "c-benchmark" else "C support linked with Rust"
        summary = _summary(combined, [], self.spec)
        self.assertEqual(summary["counting_units"]["configuration_specific_operation_records"], len(combined))
        self.assertEqual(summary["counting_units"]["distinct_source_identities_all_scopes"], len(benchmark_keys))
        self.assertEqual(summary["counting_units"]["shared_source_identities_c_benchmark_and_c_support"], len(benchmark_keys))
        self.assertLess(summary["counting_units"]["distinct_source_identities_all_scopes"], len(combined))

    def test_kernel_call_kind_breakdown(self):
        c_root, c_config = self.c_repo()
        c_ops, _, _, _ = extract_c_scope(c_root, c_config)
        for row in c_ops:
            row["scope"] = "c-benchmark"
        c_summary = _summary(c_ops, [], self.spec)
        c_kinds = c_summary["kernel_calls"][c_config["configuration"]]["by_callee_kind"]
        self.assertGreaterEqual(c_kinds.get("rtos_api", 0), 1)
        self.assertGreaterEqual(c_kinds.get("project_wrapper_or_ffi", 0), 1)
        self.assertGreaterEqual(c_kinds.get("indirect_or_unresolved", 0), 1)

        rust_root, rust_config = self.rust_repo()
        rust_ops, _, _, _, _ = extract_rust_scope(rust_root, rust_config, self.extractor, self.classifier)
        rust_summary = _summary(rust_ops, [], self.spec)
        rust_kinds = rust_summary["kernel_calls"][rust_config["configuration"]]["by_callee_kind"]
        self.assertGreaterEqual(rust_kinds.get("rust_ffi_declaration", 0), 1)
        self.assertGreaterEqual(rust_kinds.get("project_wrapper", 0), 1)
        self.assertNotIn("rtos_api", {key for key, value in rust_kinds.items() if value})

    def test_rust_layer_and_summary_recomputation(self):
        root, config = self.rust_repo()
        operations, functions, kernel_calls, unsafe, meta = extract_rust_scope(root, config, self.extractor, self.classifier)
        self.assertTrue(any(row["layer"] == "rust_ffi_boundary" for row in operations))
        summary = _summary(operations, unsafe, self.spec)
        self.assertEqual(summary["scopes"]["rust"]["configuration_specific_operation_records"], len(operations))
        self.assertEqual(summary["scopes"]["rust"]["active_resolved"], sum(1 for row in operations if row["activity_status"] == "true" and row["resolution_status"] == "resolved"))
        self.assertEqual(summary["unsafe_context_association"]["operations_considered"], sum(1 for row in operations if row["activity_status"] == "true"))
        self.assertGreater(summary["unsafe_context_association"]["in_unsafe_context_total"], 0)

    @unittest.skipUnless(STAGED_SOURCES, "staged RTOS sources unavailable; run scripts/setup/install.sh")
    def test_end_to_end_inventory_and_determinism(self):
        root = _repo_root()
        temporary = tempfile.TemporaryDirectory(prefix="uastw-inventory-e2e-")
        self.addCleanup(temporary.cleanup)
        first = Path(temporary.name) / "a"
        second = Path(temporary.name) / "b"
        command = [sys.executable, str(SAFETY / "safety.py"), "inventory", "--repo-root", str(root), "--output"]
        completed = subprocess.run(command + [str(first)], cwd=SAFETY, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        completed2 = subprocess.run(command + [str(second)], cwd=SAFETY, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed2.returncode, 0, completed2.stderr)
        for path in sorted(first.rglob("*")):
            if path.is_file():
                relative = path.relative_to(first)
                self.assertEqual(path.read_bytes(), (second / relative).read_bytes(), f"nondeterministic {relative}")
        summary = json.loads((first / "summary.json").read_text())
        coverage = json.loads((first / "coverage.json").read_text())
        manifest = json.loads((first / "manifest.json").read_text())
        self.assertGreater(summary["scopes"]["c-benchmark"]["active_resolved"], 0)
        self.assertGreater(summary["scopes"]["rust"]["active_resolved"], 0)
        self.assertGreater(summary["scopes"]["c-support"]["active_resolved"], 0)
        self.assertEqual(summary["counting_units"]["configuration_specific_operation_records"], sum(scope["configuration_specific_operation_records"] for scope in summary["scopes"].values()))
        self.assertLess(summary["counting_units"]["distinct_source_identities_all_scopes"], summary["counting_units"]["configuration_specific_operation_records"])
        self.assertGreater(summary["counting_units"]["shared_source_identities_c_benchmark_and_c_support"], 0)
        self.assertIn("configuration_specific_operation_records", manifest)
        self.assertIn("distinct_source_identities_all_scopes", manifest)
        self.assertNotIn("operations", manifest)
        self.assertGreater(manifest["configuration_specific_operation_records"], manifest["distinct_source_identities_all_scopes"])
        self.assertIn("source_column_conventions", manifest)
        self.assertIn("macro_counting_unit", manifest)
        self.assertIn("known_source_scope_limitations", manifest)
        self.assertIn("out_of_line_modules", manifest)
        for item in manifest["outputs"]:
            output_path = first / item["path"]
            self.assertTrue(output_path.is_file(), item["path"])
            self.assertEqual(hashlib.sha256(output_path.read_bytes()).hexdigest(), item["sha256"], item["path"])
            self.assertEqual(output_path.stat().st_size, item["bytes"], item["path"])
        self.assertIn("macro_counting_unit", summary["pointer_conversions"])
        self.assertIn("source_column_conventions", summary)
        self.assertIn("source_scope_limitations", summary)
        shims = manifest["known_source_scope_limitations"]["c_benchmark_bench_rust_shims"]
        self.assertGreater(shims["operation_records"], 0)
        self.assertGreater(shims["function_count"], 0)
        unresolved_modules = {item["name"] for item in manifest["out_of_line_modules"]}
        self.assertIn("boundary", unresolved_modules)
        self.assertIn("arena", unresolved_modules)
        self.assertGreater(coverage["configurations"]["freertos-stm32f4-c-benchmark"]["configuration_specific_operation_records"], 0)
        self.assertEqual(coverage["configurations"]["freertos-stm32f4-c-benchmark"]["parse_failures"], 0)
        self.assertGreater(summary["pointer_conversions"]["explicit_source_records"], 0)
        self.assertGreater(summary["pointer_conversions"]["macro_expanded_records"], 0)
        self.assertGreater(summary["unsafe_context_association"]["in_unsafe_context_total"], 0)
        with (first / "operations.csv").open() as handle:
            operations = list(__import__("csv").DictReader(handle))
        self.assertTrue(all(row["source_file"] and row["source_line"] for row in operations))
        self.assertTrue(all(not row["source_file"].startswith(".local/") for row in operations))
        self.assertTrue(all(row["occurrence_id"] and row["source_identity_key"] for row in operations))
        self.assertTrue(all(row["provenance"] for row in operations))
        self.assertTrue((first / "validation.json").is_file())
        sources = json.loads((first / "sources.json").read_text())
        self.assertEqual(sources["configurations"]["freertos-stm32f4-c-benchmark"]["make_argv"][-2:], ["RUST=0", "all"])
        self.assertEqual(sources["configurations"]["freertos-stm32f4-c-support-rust"]["make_argv"][-2:], ["RUST=1", "all"])
        self.assertEqual(sources["configurations"]["freertos-stm32f4-rust"]["target"]["target"], "thumbv7em-none-eabihf")

    def test_layer_classification_is_deterministic(self):
        self.assertEqual(self.classifier.classify("c-benchmark", "firmware/c-kernels/freertos/c/stm32f4/src/bench.c"), "c_benchmark_rtos_adapter")
        self.assertEqual(self.classifier.classify("c-support", "firmware/c-kernels/freertos/c/stm32f4/src/bench.c"), "c_support_linked_with_rust")
        self.assertEqual(self.classifier.classify("rust", "firmware/c-kernels/freertos/rust/src/boundary.rs"), "rust_ffi_boundary")
        self.assertEqual(self.classifier.classify("rust", "firmware/common/rust/src/arena.rs"), "shared_infrastructure")
        self.assertEqual(self.classifier.classify("c-benchmark", "firmware/c-kernels/zephyr/c/esp32c6/main.c"), "benchmark_application")
        self.assertEqual(self.classifier.classify("c-benchmark", "firmware/c-kernels/nuttx/c/isr.c"), "c_benchmark_rtos_adapter")
        self.assertEqual(self.classifier.classify("c-support", "firmware/c-kernels/nuttx/c/port.c"), "c_support_linked_with_rust")
        self.assertEqual(self.classifier.classify("rust", "firmware/rust-kernels/rivet/rust/stm32f4/src/main.rs"), "rust_native_application")
        self.assertEqual(self.classifier.classify("rust", ".local/source/nuttx/arch/foo.c"), "external_component")
        self.assertEqual(self.classifier.classify("rust", "firmware/unknown/source.rs"), "unclassified")

    def test_missing_type_information_is_unresolved_not_silently_counted(self):
        root, config = self.rust_repo()
        operations, _, _, _, _ = extract_rust_scope(root, config, self.extractor, self.classifier)
        unresolved = [row for row in operations if row["resolution_status"] == "unresolved"]
        self.assertTrue(unresolved)
        self.assertTrue(all(row["resolution_status"] != "resolved" for row in unresolved))
        self.assertTrue(any(row["category"] == "unresolved_macro" for row in unresolved))

    def test_isolated_root_injection_and_execution(self):
        temporary = tempfile.TemporaryDirectory(prefix="uastw-inventory-root-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "fake-repo"
        (root / ".git").mkdir(parents=True)
        (root / "firmware").mkdir()
        nested = root / "deep" / "nested"
        nested.mkdir(parents=True)
        previous = os.environ.get(REPO_ROOT_ENV)
        try:
            os.environ[REPO_ROOT_ENV] = str(root)
            self.assertEqual(_repo_root(), root.resolve())
        finally:
            if previous is None:
                os.environ.pop(REPO_ROOT_ENV, None)
            else:
                os.environ[REPO_ROOT_ENV] = previous
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SAFETY) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        environment[REPO_ROOT_ENV] = str(root)
        completed = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; from common.config import resolve_repo_root; print(resolve_repo_root(Path('.')))"],
            cwd=nested,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(Path(completed.stdout.strip()).resolve(), root.resolve())

    def test_isolated_tooling_copy_runs_with_explicit_root(self):
        # The nested complete-suite run includes this test.  Return after the
        # guard is set so the isolation check is not recursively re-entered;
        # every other test, including the shared four, still runs in the copy.
        if os.environ.get(ISOLATION_GUARD_ENV) == "1":
            return
        temporary = tempfile.TemporaryDirectory(prefix="uastw-inventory-copy-")
        self.addCleanup(temporary.cleanup)
        copy_root = Path(temporary.name) / "safety"
        shutil.copytree(
            SAFETY,
            copy_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "target"),
        )
        results = Path(temporary.name) / "results.json"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(copy_root)
        environment[REPO_ROOT_ENV] = str(_repo_root())
        environment[ISOLATION_GUARD_ENV] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                str(copy_root / "safety.py"),
                "test",
                "--repo-root",
                str(_repo_root()),
                "--results",
                str(results),
            ],
            cwd=copy_root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("tests.test_checks.SafetyCheckTests", completed.stderr)
        self.assertIn("inventory.tests.test_inventory", completed.stderr)
        summary = json.loads(results.read_text())
        self.assertGreaterEqual(summary["tests_run"], 60)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["errors"], 0)
        self.assertTrue(summary["successful"])


if __name__ == "__main__":
    unittest.main()
