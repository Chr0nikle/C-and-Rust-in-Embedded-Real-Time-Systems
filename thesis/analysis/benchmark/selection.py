"""Explicit thesis campaign selection.

The thesis must never silently switch to the newest experimental campaign.
Update ``campaign.lock.json`` deliberately after reviewing a new campaign.
"""

from __future__ import annotations

import json
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
REPO_ROOT = ANALYSIS.parents[1]
LOCK = ANALYSIS / "campaign.lock.json"


def load_lock() -> dict:
    if not LOCK.is_file():
        raise SystemExit(f"campaign lock missing: {LOCK}")
    try:
        payload = json.loads(LOCK.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid campaign lock {LOCK}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise SystemExit(f"unsupported campaign lock schema in {LOCK}")
    return payload


def campaign_id() -> str:
    value = load_lock().get("benchmark_campaign")
    if not isinstance(value, str) or not value:
        raise SystemExit(f"campaign lock {LOCK} has no benchmark_campaign")
    return value


def campaign_bundle() -> Path:
    identifier = campaign_id()
    bundle = REPO_ROOT / "results" / "campaigns" / identifier
    if not bundle.is_dir():
        raise SystemExit(
            f"locked benchmark campaign not found: {bundle}\n"
            "Restore the campaign evidence or update thesis/analysis/campaign.lock.json deliberately."
        )
    return bundle.resolve()


def safety_run() -> str:
    value = load_lock().get("safety_run")
    if not isinstance(value, str) or not value:
        raise SystemExit(f"campaign lock {LOCK} has no safety_run")
    return value
