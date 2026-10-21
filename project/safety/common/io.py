"""Shared result helpers and deterministic serialisation."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

STATUSES = ("PASS", "FAIL", "UNKNOWN", "NOT_CHECKED", "NOT_APPLICABLE")

CONTRACT_FIELDS = (
    "check_id",
    "configuration",
    "language",
    "interface",
    "property",
    "status",
    "source_file",
    "source_line",
    "evidence",
    "assumptions",
    "tool",
    "tool_version",
    "reason",
    "applicability",
    "source_type_status",
    "abi_representation_status",
    "value_domain_status",
    "calling_convention_status",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact(value: Any) -> str:
    """Stable compact JSON for evidence/assumption cells."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract(
    *,
    check_id: str,
    configuration: str,
    language: str,
    interface: str,
    property_name: str,
    status: str,
    source_file: str | None,
    source_line: int | None,
    evidence: Any,
    assumptions: Any,
    tool: str,
    tool_version: str,
    reason: str,
    applicability: str | None = None,
    source_type_status: str | None = None,
    abi_representation_status: str | None = None,
    value_domain_status: str | None = None,
    calling_convention_status: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    return {
        "check_id": check_id,
        "configuration": configuration,
        "language": language,
        "interface": interface,
        "property": property_name,
        "status": status,
        "source_file": source_file,
        "source_line": source_line,
        "evidence": compact(evidence),
        "assumptions": compact(assumptions),
        "tool": tool,
        "tool_version": tool_version,
        "reason": reason,
        "applicability": applicability or ("APPLICABLE" if status in ("PASS", "FAIL", "UNKNOWN") else status),
        "source_type_status": source_type_status,
        "abi_representation_status": abi_representation_status,
        "value_domain_status": value_domain_status,
        "calling_convention_status": calling_convention_status,
    }


def write_csv(path: Path, fields: Iterable[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            serialised = {}
            for field in fields:
                value = row.get(field, "")
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                serialised[field] = value
            writer.writerow(serialised)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def summarize(rows: list[dict[str, Any]], check_id: str) -> dict[str, Any]:
    applicable = [row for row in rows if row.get("check_id") == check_id]
    counts = {status: 0 for status in STATUSES}
    for row in applicable:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    applicable_rows = [row for row in applicable if row.get("applicability") != "NOT_APPLICABLE"]
    return {
        "applicable_discovered": len(applicable_rows),
        "instances_checked": sum(counts[status] for status in ("PASS", "FAIL", "UNKNOWN")),
        **counts,
    }


def relative_to_repo(path: str | None, repo_root: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(repo_root))
    except (ValueError, OSError):
        return path
