"""Locations for the safety analysis tooling and its generated artifacts."""

from pathlib import Path

SAFETY_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SAFETY_DIR.parents[1]
ARTIFACTS = REPO_ROOT / "results" / "safety"
THESIS_GENERATED = REPO_ROOT / "thesis" / "generated"
THESIS_TYP = THESIS_GENERATED / "typ"
