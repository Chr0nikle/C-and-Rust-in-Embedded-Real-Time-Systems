#!/usr/bin/env python3
"""UASTW C/Rust safety analysis.

Run the complete pipeline with ``safety.py all``, or select a stage:

  analyze            run extraction, checks and established analysers
  inventory          run the operation inventory
  profiles           generate matched and native profiles
  matched-api        run the matched C/Rust API and FFI analysis
  profile-inventory  run the operation inventory for generated profiles
  thesis-safety      generate thesis safety tables and revalidation evidence
  test               run the isolated fixture test-suite

All outputs are written under ``project/safety/artifacts`` by default.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

SAFETY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SAFETY_DIR))

from common.paths import ARTIFACTS, THESIS_TYP  # noqa: E402

from checks.abi import check_01
from checks.analysers import check_05, run_clang_analyzer, run_clippy
from checks.arrays import check_03
from checks.c_extract import extract_c
from common.io import write_json
from common.config import discover, find_repo_root
from checks.layout import check_02
from checks.queues import check_04
from checks.reporting import (
    coverage,
    ffi_interfaces,
    source_manifest,
    structural_rows,
    write_manifest,
    write_standard_outputs,
)
from common.extractor import build_extractor, extract_rust


def _version_string(config: dict[str, Any]) -> str:
    tools = config.get("tools", {})
    clang = (tools.get("clang") or {}).get("version") or "clang unavailable"
    rustc = (tools.get("rustc") or {}).get("version") or "rustc unavailable"
    return f"{clang}; {rustc}; syn 2.0.119"


def _sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    clean = json.loads(json.dumps(config))
    for unit in clean.get("c", {}).get("translation_units", []):
        unit.pop("abs_path", None)
    for unit in clean.get("c", {}).get("all_compile_commands", []):
        unit.pop("abs_path", None)
    clean.get("c", {}).pop("toolchain_include", None)
    return clean


def _portable(value: Any, repo_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _portable(item, repo_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item, repo_root) for item in value]
    if isinstance(value, str):
        return value.replace(str(repo_root), ".")
    return value


def _validate_generated(output: Path, contracts: list[dict[str, Any]], coverage_doc: dict[str, Any], c_benchmark_rows: list[dict[str, Any]], c_support_rows: list[dict[str, Any]], rust_rows: list[dict[str, Any]], interfaces: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    for configuration, checks in coverage_doc["checks_by_configuration"].items():
        for check_id, summary in checks.items():
            selected = [row for row in contracts if row.get("configuration") == configuration and row.get("check_id") == check_id]
            counts = {status: sum(1 for row in selected if row.get("status") == status) for status in ("PASS", "FAIL", "UNKNOWN", "NOT_CHECKED", "NOT_APPLICABLE")}
            for status, count in counts.items():
                if summary.get(status, 0) != count:
                    errors.append(f"{configuration}/{check_id}: {status} summary={summary.get(status)} rows={count}")
            applicable = sum(1 for row in selected if row.get("status") in ("PASS", "FAIL", "UNKNOWN"))
            if check_id in ("CHECK-02", "CHECK-05"):
                applicable = 0
            if summary.get("applicable_discovered") != applicable:
                errors.append(f"{configuration}/{check_id}: applicable summary={summary.get('applicable_discovered')} rows={applicable}")
    for label, rows in (("c-benchmark", c_benchmark_rows), ("c-support", c_support_rows), ("rust", rust_rows)):
        if len(rows) != len({json.dumps(row, sort_keys=True) for row in rows}):
            errors.append(f"{label}: duplicate structural rows")
    if len(interfaces) != len({row["interface"] for row in interfaces}):
        errors.append("ffi: duplicate interface rows")
    for unit in config["configurations"]["freertos-stm32f4-c-benchmark"]["translation_units"]:
        if unit.get("has_bench_language_rust"):
            errors.append(f"c-benchmark: unexpected BENCH_LANGUAGE_RUST in {unit['path']}")
    for unit in config["configurations"]["freertos-stm32f4-c-support-rust"]["translation_units"]:
        if not unit.get("has_bench_language_rust"):
            errors.append(f"c-support: missing BENCH_LANGUAGE_RUST in {unit['path']}")
    if errors:
        raise SystemExit("generated evidence validation failed:\n" + "\n".join(errors))
    return {
        "schema": "uastw-safety-validation-v1",
        "status": "passed",
        "contract_rows": len(contracts),
        "structural": {"c_benchmark": len(c_benchmark_rows), "c_support": len(c_support_rows), "rust": len(rust_rows)},
        "ffi_interfaces": len(interfaces),
        "checks_by_configuration": coverage_doc["checks_by_configuration"],
        "checks": [
            "coverage counts equal contracts.csv",
            "structural rows are exact-unique within each scope",
            "C benchmark has no BENCH_LANGUAGE_RUST",
            "C support has BENCH_LANGUAGE_RUST",
            "FFI interface names are unique",
        ],
    }



def command_all(args: argparse.Namespace) -> int:
    """Run the complete safety analysis and write every artifact."""
    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    repo_root = find_repo_root(start)
    common = argparse.Namespace(
        repo_root=str(repo_root),
        output=None,
        skip_analysers=getattr(args, "skip_analysers", False),
        skip_clippy=getattr(args, "skip_clippy", False),
        profile=None,
        cases=None,
    )

    command_analyze(common)
    command_inventory(common)
    command_matched_api(common)

    if getattr(args, "regenerate_profiles", False):
        profile_args = argparse.Namespace(
            repo_root=str(repo_root),
            rtos=["freertos", "nuttx", "zephyr"],
            board=["stm32f4discovery", "stm32h723", "nrf52840dk", "esp32c6"],
            native=[
                "freertos-in-rust-stm32f4",
                "freertos-in-rust-stm32h723",
                "rivet-stm32f4",
            ],
            output=str(ARTIFACTS / "profiles"),
            check=False,
        )
        command_profiles(profile_args)

    profiles = sorted(str(path) for path in (ARTIFACTS / "profiles").glob("*-all.json"))
    if getattr(args, "regenerate_profiles", False):
        inventory_args = argparse.Namespace(
            repo_root=str(repo_root),
            profile=profiles,
            native=[
                "freertos-in-rust-stm32f4",
                "freertos-in-rust-stm32h723",
                "rivet-stm32f4",
            ],
            output=str(ARTIFACTS / "generated_inventory"),
        )
        command_profile_inventory(inventory_args)
    command_thesis_safety(argparse.Namespace(repo_root=str(repo_root)))
    print(json.dumps({"artifacts": str(ARTIFACTS)}, indent=2))
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    repo_root = find_repo_root(start)
    output = Path(args.output).resolve() if args.output else ARTIFACTS / "checks"
    if output in (repo_root, SAFETY_DIR):
        raise SystemExit("refusing to delete the repository root or the safety tooling directory as an output path")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    config = discover(repo_root)
    version = _version_string(config)
    c_bench_id = "freertos-stm32f4-c-benchmark"
    c_support_id = "freertos-stm32f4-c-support-rust"
    rust_id = "freertos-stm32f4-rust"
    c_bench_cfg = config["configurations"][c_bench_id]
    c_support_cfg = config["configurations"][c_support_id]
    c_bench_units = [{"path": unit["abs_path"], "abs_path": unit["abs_path"], "args": unit["clang_args"], "rel": unit["path"]} for unit in c_bench_cfg["translation_units"]]
    c_support_units = [{"path": unit["abs_path"], "abs_path": unit["abs_path"], "args": unit["clang_args"], "rel": unit["path"]} for unit in c_support_cfg["translation_units"]]
    c_benchmark = extract_c(repo_root, c_bench_units, [])
    c_support = extract_c(repo_root, c_support_units, [])
    extractor = build_extractor(repo_root)
    rust_facts = extract_rust(repo_root, config["configurations"][rust_id]["files"], config["configurations"][rust_id]["target"], extractor, config["configurations"][rust_id].get("features", []))

    contracts: list[dict[str, Any]] = []
    contracts.extend(check_01(c_support, rust_facts, configuration=c_support_id, pointer_width=int(config["configurations"][rust_id]["target"].get("pointer_width", 32)), tool_version=version))
    contracts.extend(check_02(c_support, rust_facts, repo_root=repo_root, configuration=c_support_id, tool_version=version))
    contracts.extend(check_03(c_support, rust_facts, configuration=c_support_id, tool_version=version))
    contracts.extend(check_04(c_benchmark, rust_facts, repo_root=repo_root, c_configuration=c_bench_id, rust_configuration=rust_id, tool_version=version))

    analysers: dict[str, Any] = {}
    if args.skip_analysers:
        analysers["clang_c_benchmark"] = {"configuration": c_bench_id, "status": "skipped_by_cli", "diagnostics": []}
        analysers["clang_c_support"] = {"configuration": c_support_id, "status": "skipped_by_cli", "diagnostics": []}
        analysers["clippy"] = {"configuration": rust_id, "status": "skipped_by_cli", "diagnostics": []}
    else:
        analysers["clang_c_benchmark"] = run_clang_analyzer(repo_root, c_bench_units, output_root=output / "raw", configuration=c_bench_id, report_name="clang-analyzer-c-benchmark.json")
        analysers["clang_c_support"] = run_clang_analyzer(repo_root, c_support_units, output_root=output / "raw", configuration=c_support_id, report_name="clang-analyzer-c-support.json")
        if args.skip_clippy:
            analysers["clippy"] = {"configuration": rust_id, "status": "skipped_by_cli", "diagnostics": []}
        else:
            try:
                analysers["clippy"] = run_clippy(repo_root, output_root=output / "raw")
            except Exception as error:
                analysers["clippy"] = {"configuration": rust_id, "status": "failed", "diagnostics": [], "error": str(error)}

    contracts.extend([
        check_05(analysers, configuration=c_bench_id, language="c", clang_key="clang_c_benchmark", clippy=False, tool_version=version),
        check_05(analysers, configuration=c_support_id, language="c", clang_key="clang_c_support", clippy=False, tool_version=version),
        check_05(analysers, configuration=rust_id, language="rust", clang_key=None, clippy=True, tool_version=version),
    ])
    contracts.sort(key=lambda row: (row["configuration"], row["check_id"], row["interface"], row["source_file"] or "", row["source_line"] or 0))

    c_benchmark_rows, c_support_rows, rust_rows = structural_rows(
        c_benchmark, c_support, rust_facts,
        c_benchmark_configuration=c_bench_id,
        c_support_configuration=c_support_id,
        rust_configuration=rust_id,
    )
    interfaces = ffi_interfaces(c_support, rust_facts, contracts, c_configuration=c_support_id, rust_configuration=rust_id)
    severity_names = {"0": "ignored", "1": "note", "2": "warning", "3": "error", "4": "fatal", "5": "fatal"}
    diagnostics = []
    for facts, configuration in ((c_benchmark, c_bench_id), (c_support, c_support_id)):
        diagnostics.extend([
            {
                "configuration": configuration,
                "tool": "clang-ast",
                "tool_version": ((config.get("tools") or {}).get("clang") or {}).get("version") or "clang",
                "severity": severity_names.get(str(item.get("severity")), str(item.get("severity"))),
                "rule_id": "parse-diagnostic",
                "file": item.get("file"),
                "line": item.get("line"),
                "column": None,
                "message": item.get("message"),
                "scope": "project" if item.get("file") else "unknown",
                "run_status": "parse",
            }
            for item in facts.get("diagnostics", [])
        ])
    for key in ("clang_c_benchmark", "clang_c_support", "clippy"):
        diagnostics.extend(analysers.get(key, {}).get("diagnostics", []))
    diagnostics.sort(key=lambda row: (row.get("configuration") or "", row.get("tool") or "", row.get("file") or "", row.get("line") or 0, row.get("rule_id") or ""))
    sources = source_manifest(repo_root, config, c_benchmark, c_support, rust_facts, tool_versions=config["tools"])
    coverage_document = coverage(c_benchmark, c_support, rust_facts, contracts, analysers, config)
    write_standard_outputs(
        output,
        structural_c_benchmark=c_benchmark_rows,
        structural_c_support=c_support_rows,
        structural_rust=rust_rows,
        interfaces=interfaces,
        contracts=contracts,
        diagnostics=diagnostics,
        sources=sources,
        coverage_document=coverage_document,
        config=_portable({key: value for key, value in config.items() if key != "all_compile_commands"}, repo_root),
        tool_versions=config["tools"],
    )
    validation = _validate_generated(output, contracts, coverage_document, c_benchmark_rows, c_support_rows, rust_rows, interfaces, config)
    write_json(output / "validation.json", validation)
    write_manifest(output, config=config, tool_versions=config["tools"], contracts=contracts)
    print(json.dumps({
        "output": str(output),
        "c_benchmark_files_parsed": len(c_benchmark.get("files", [])),
        "c_support_files_parsed": len(c_support.get("files", [])),
        "rust_files_parsed": len(rust_facts.get("files", [])),
        "c_benchmark_parse_failures": len(c_benchmark.get("parse_failures", [])),
        "c_support_parse_failures": len(c_support.get("parse_failures", [])),
        "rust_parse_failures": len(rust_facts.get("parse_failures", [])),
        "contracts": len(contracts),
        "checks_by_configuration": coverage_document["checks_by_configuration"],
        "diagnostics": coverage_document["diagnostic_counts_by_tool"],
        "validation": validation["status"],
    }, indent=2, sort_keys=True))
    return 0

def command_inventory(args: argparse.Namespace) -> int:
    from inventory.pipeline import run_inventory

    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    repo_root = find_repo_root(start)
    output = Path(args.output).resolve() if args.output else ARTIFACTS / "inventory"
    result = run_inventory(repo_root, output)
    print(json.dumps({
        "output": result["output"],
        "configuration_specific_operation_records": result["configuration_specific_operation_records"],
        "distinct_source_identities_all_scopes": result["distinct_source_identities_all_scopes"],
        "functions": result["functions"],
        "kernel_calls": result["kernel_calls"],
        "unsafe_contexts": result["unsafe_contexts"],
    }, indent=2, sort_keys=True))
    return 0


def command_matched_api(args: argparse.Namespace) -> int:
    from matched.pipeline import run_matched_api

    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    repo_root = find_repo_root(start)
    profile_id = getattr(args, "profile", None)
    if profile_id and not args.output:
        raise SystemExit("--output is required when using --profile; refusing to overwrite the frozen FreeRTOS output")
    output = Path(args.output).resolve() if args.output else ARTIFACTS / "matched"
    if profile_id and output == (ARTIFACTS / "matched").resolve():
        raise SystemExit("refusing to write a pilot profile into the frozen FreeRTOS output directory")
    selected_cases = None
    if getattr(args, "cases", None):
        selected_cases = [int(value.strip()) for value in args.cases.split(",") if value.strip()]
    result = run_matched_api(repo_root, output, selected_cases, profile_id)
    print(json.dumps({
        "output": result["output"],
        "selected_cases": result["coverage"]["selected_cases"],
        "verified_roots": result["coverage"]["verified_roots"],
        "call_site_occurrences": result["coverage"]["call_site_occurrences"],
        "distinct_source_call_sites": result["coverage"]["distinct_source_call_sites"],
        "per_case_call_memberships": result["coverage"]["per_case_call_memberships"],
        "common_harness_call_sites": result["coverage"]["common_harness_call_sites"],
        "matched_rtos_api_targets": result["coverage"]["matched_rtos_api_targets"],
        "selected_matched_c_bridge_relationships": result["coverage"]["selected_matched_c_bridge_relationships"],
        "selected_matched_distinct_c_bridge_definitions": result["coverage"]["selected_matched_distinct_c_bridge_definitions"],
        "selected_distinct_c_bridge_definitions": result["coverage"]["selected_distinct_c_bridge_definitions"],
        "selected_unused_c_bridge_definitions": result["coverage"]["selected_unused_c_bridge_definitions"],
        "c_bridge_inventory_scope": result["coverage"]["c_bridge_inventory_scope"],
        "rtos_api_c_only_targets": result["coverage"]["rtos_api_c_only_targets"],
        "rtos_api_rust_only_targets": result["coverage"]["rtos_api_rust_only_targets"],
        "check_occurrences": result["coverage"]["check_occurrences"],
        "common_harness_check_occurrences": result["coverage"]["common_harness_check_occurrences"],
        "mechanism_specific_check_occurrences": result["coverage"]["mechanism_specific_check_occurrences"],
        "c_application_explicit_size_memberships": result["coverage"]["c_application_explicit_size_memberships"],
        "rust_application_explicit_size_memberships": result["coverage"]["rust_application_explicit_size_memberships"],
        "c_application_derived_size_memberships": result["coverage"]["c_application_derived_size_memberships"],
        "rust_application_derived_size_memberships": result["coverage"]["rust_application_derived_size_memberships"],
        "c_all_level_explicit_size_occurrences": result["coverage"]["c_all_level_explicit_size_occurrences"],
        "rust_all_level_explicit_size_occurrences": result["coverage"]["rust_all_level_explicit_size_occurrences"],
        "c_all_level_explicit_size_memberships": result["coverage"]["c_all_level_explicit_size_memberships"],
        "rust_all_level_explicit_size_memberships": result["coverage"]["rust_all_level_explicit_size_memberships"],
        "c_all_level_derived_size_occurrences": result["coverage"]["c_all_level_derived_size_occurrences"],
        "rust_all_level_derived_size_occurrences": result["coverage"]["rust_all_level_derived_size_occurrences"],
        "unsafe_context_call_occurrences": result["coverage"]["unsafe_context_call_occurrences"],
        "unsafe_context_per_case_memberships": result["coverage"]["unsafe_context_per_case_memberships"],
        "callback_registrations": result["coverage"]["callback_registrations"],
        "case_coverage_partial": result["coverage"]["case_coverage_partial"],
        "external_entry_partial_cases": result["coverage"]["external_entry_partial_cases"],
        "validation": result["validation"]["status"],
    }, indent=2, sort_keys=True))
    return 0


def command_profiles(args: argparse.Namespace) -> int:
    from profiles.generator import main as generate_main

    start = Path(args.repo_root).resolve() if getattr(args, "repo_root", None) else Path.cwd()
    repo_root = find_repo_root(start)
    argv = ["--repo-root", str(repo_root)]
    if args.rtos:
        for rtos in args.rtos:
            argv.extend(["--rtos", rtos])
    if getattr(args, "board", None):
        for board in args.board:
            argv.extend(["--board", board])
    if getattr(args, "native", None):
        for native in args.native:
            argv.extend(["--native", native])
    argv.extend(["--output", args.output])
    if args.check:
        argv.append("--check")
    return generate_main(argv)


def command_profile_inventory(args: argparse.Namespace) -> int:
    from profiles.inventory import main as inventory_main

    start = Path(args.repo_root).resolve() if getattr(args, "repo_root", None) else Path.cwd()
    repo_root = find_repo_root(start)
    argv = ["--repo-root", str(repo_root)]
    for profile in getattr(args, "profile", None) or []:
        argv.extend(["--profile", profile])
    for native in getattr(args, "native", None) or []:
        argv.extend(["--native", native])
    argv.extend(["--output", args.output])
    return inventory_main(argv)


def command_thesis_safety(args: argparse.Namespace) -> int:
    from tables import generate_thesis_tables

    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    repo_root = find_repo_root(start)
    generate_thesis_tables(repo_root)
    print(json.dumps({
        "generated": str(THESIS_TYP),
        "revalidation": str(ARTIFACTS / "generated_outputs/analysis-tables/safety-revalidation.json"),
    }, indent=2))
    return 0


UNIT_TESTS = {
    "test_complete_case_coverage",
    "test_free_rtos_c6_default_wire_mapping",
    "test_board_configurations_are_not_collapsed",
    "test_board_specific_isr_roots_and_scopes",
    "test_board_rust_targets",
    "test_native_features_recorded",
    "test_profile_content_is_stable_across_revision_metadata",
    "test_native_profiles_are_separate",
    "test_source_backed_mappings",
    "test_registry_source_verification",
    "test_source_guards_and_change_detection",
    "test_unresolved_mapping_is_reported",
}

REPRO_TESTS = {
    "test_native_features_recorded",
    "test_profile_content_is_stable_across_revision_metadata",
    "test_allocator_sizes_and_macro_policy_in_production",
    "test_availability_and_ninth_pointer",
    "test_full_outputs_cover_all_implemented_cases",
    "test_nuttx_config_provenance",
    "test_nuttx_pilot_outputs_and_contract",
    "test_zephyr_pilot_outputs_and_aliases",
    "test_call_site_memberships_are_canonical",
    "test_production_exposure_views_and_common_counts",
    "test_production_pipeline_integration_and_manifest",
    "test_reporting_units_b01_b04",
    "test_unresolved_callback_pipeline_is_partial",
    "test_profile_availability_matches_campaign",
    "test_deterministic_generation",
    "test_inventory_covers_all_configurations",
    "test_free_rtos_trace_hook_boundaries",
    "test_native_per_case_traces",
    "test_no_implemented_case_has_incomplete_trace",
    "test_layer_rules_cover_all_matched_and_native_configurations",
    "test_thesis_safety_tables_are_reproducible",
    "test_analysis_tables_are_reproducible",
    "test_generated_profiles_validate_end_to_end",
}


def _test_tier(test: unittest.TestCase) -> str:
    name = test.id().rsplit(".", 1)[-1]
    if name in REPRO_TESTS:
        return "repro"
    if name in UNIT_TESTS:
        return "unit"
    return "integration"


def _filter_suite(suite: unittest.TestSuite, allowed: set[str]) -> unittest.TestSuite:
    selected = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            child = _filter_suite(test, allowed)
            if child.countTestCases():
                selected.addTest(child)
        elif _test_tier(test) in allowed:
            selected.addTest(test)
    return selected


def command_test(args: argparse.Namespace) -> int:
    import os

    if getattr(args, "repo_root", None):
        os.environ["UASTW_REPO_ROOT"] = str(Path(args.repo_root).resolve())
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([
        loader.discover(str(SAFETY_DIR / "tests"), pattern="test_*.py", top_level_dir=str(SAFETY_DIR)),
        loader.discover(str(SAFETY_DIR / "inventory" / "tests"), pattern="test_*.py", top_level_dir=str(SAFETY_DIR)),
        loader.discover(str(SAFETY_DIR / "matched" / "tests"), pattern="test_*.py", top_level_dir=str(SAFETY_DIR)),
        loader.discover(str(SAFETY_DIR / "profiles" / "tests"), pattern="test_*.py", top_level_dir=str(SAFETY_DIR)),
    ])
    tier = getattr(args, "tier", None)
    allowed = {"unit", "integration", "repro"} if tier == "all" else (
        {tier} if tier else {"unit", "integration"}
    )
    if "repro" in allowed and not (ARTIFACTS / "generated_inventory").is_dir():
        print(
            "error: reproducibility tests require results/safety/generated_inventory; "
            "run 'python3 safety.py all --regenerate-profiles' or restore the evidence first",
            file=sys.stderr,
        )
        return 2
    suite = _filter_suite(suite, allowed)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "schema": "uastw-safety-test-results-v1",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "successful": result.wasSuccessful(),
        "failure_details": [{"test": test.id(), "trace": trace} for test, trace in result.failures],
        "error_details": [{"test": test.id(), "trace": trace} for test, trace in result.errors],
    }
    if getattr(args, "results", None):
        write_json(Path(args.results), summary)
    print(json.dumps({key: summary[key] for key in ("tests_run", "failures", "errors", "skipped", "successful")}, indent=2, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    analyze = subparsers.add_parser("analyze", help="run the analysis on FreeRTOS STM32F4")
    analyze.add_argument("--repo-root", help="repository root (defaults to discovery from the current directory)")
    analyze.add_argument("--output", help="output directory (defaults to results/safety/checks)")
    analyze.add_argument("--skip-analysers", action="store_true", help="skip Clang Analyzer and Clippy")
    analyze.add_argument("--skip-clippy", action="store_true", help="skip only Clippy")
    analyze.set_defaults(func=command_analyze)
    inventory = subparsers.add_parser("inventory", help="produce the Stage 1 safety-relevant operation inventory")
    inventory.add_argument("--repo-root", help="repository root (defaults to discovery from the current directory)")
    inventory.add_argument("--output", help="output directory (defaults to results/safety/inventory)")
    inventory.set_defaults(func=command_inventory)
    matched = subparsers.add_parser("matched-api", help="produce the Stage 2 matched C/Rust API and FFI analysis")
    matched.add_argument("--repo-root", help="repository root (defaults to discovery from the current directory)")
    matched.add_argument("--output", help="output directory (defaults to results/safety/matched)")
    matched.add_argument("--cases", help="optional comma-separated thesis case numbers; default is the full 26-case registry")
    matched.add_argument("--profile", help="optional pilot profile name or path (for example nuttx-stm32f4.json)")
    matched.set_defaults(func=command_matched_api)
    profiles = subparsers.add_parser("profiles", help="generate matched-API profiles from source and build evidence")
    profiles.add_argument("--repo-root", help="repository root (defaults to discovery from the current directory)")
    profiles.add_argument("--rtos", action="append", choices=["freertos", "nuttx", "zephyr"], help="RTOS to generate (repeatable)")
    profiles.add_argument("--board", action="append", choices=["stm32f4discovery", "stm32h723", "nrf52840dk", "esp32c6"], help="board to generate (repeatable; default STM32F4)")
    profiles.add_argument("--native", action="append", choices=["freertos-in-rust-stm32f4", "freertos-in-rust-stm32h723", "rivet-stm32f4"], help="Rust-native configuration to generate (repeatable)")
    profiles.add_argument("--output", default=str(ARTIFACTS / "profiles"))
    profiles.add_argument("--check", action="store_true", help="compare generated profiles with the checked-in profiles")
    profiles.set_defaults(func=command_profiles)

    profile_inventory = subparsers.add_parser("profile-inventory", help="run Analysis 1 for generated matched and native profiles")
    profile_inventory.add_argument("--repo-root")
    profile_inventory.add_argument("--profile", action="append", help="profile path (repeatable; default all generated matched profiles)")
    profile_inventory.add_argument("--native", action="append", choices=["freertos-in-rust-stm32f4", "freertos-in-rust-stm32h723", "rivet-stm32f4"], help="native configuration (repeatable)")
    profile_inventory.add_argument("--output", default=str(ARTIFACTS / "generated_inventory"))
    profile_inventory.set_defaults(func=command_profile_inventory)

    thesis_safety = subparsers.add_parser("thesis-safety", help="generate thesis safety tables and revalidation evidence")
    thesis_safety.add_argument("--repo-root", help="repository root (defaults to discovery from the current directory)")
    thesis_safety.set_defaults(func=command_thesis_safety)

    all_command = subparsers.add_parser("all", help="run every safety-analysis stage")
    all_command.add_argument("--repo-root", help="repository root (defaults to discovery from the current directory)")
    all_command.add_argument("--skip-analysers", action="store_true", help="skip Clang Analyzer and Clippy")
    all_command.add_argument("--skip-clippy", action="store_true", help="skip only Clippy")
    all_command.add_argument("--regenerate-profiles", action="store_true",
                             help="regenerate matched-API profiles before the inventory")
    all_command.set_defaults(func=command_all)

    test = subparsers.add_parser("test", help="run fixture tests; optionally write an explicit results file")
    test.add_argument("--repo-root", help="repository root supplied explicitly to portability/end-to-end tests")
    test.add_argument("--results", help="optional explicit path for the test-results JSON (no repository evidence is written by default)")
    test.add_argument(
        "--tier",
        choices=("unit", "integration", "repro", "all"),
        help="run one test tier (default: unit + integration)",
    )
    test.set_defaults(func=command_test)
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
