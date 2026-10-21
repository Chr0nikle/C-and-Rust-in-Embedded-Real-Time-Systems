"""Write deterministic provenance for thesis processing runs.

Generated tables and figures stay in ``thesis/generated`` so the thesis can be
compiled from tracked inputs. This module records which evidence and processing
implementation produced them under the ignored ``results/processing`` tree.

A processing identity is derived from the input evidence, lock, implementation
sources and output content. Volatile generation timestamps are excluded from the
identity so repeated equivalent runs reuse the same record.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from selection import campaign_bundle, campaign_id, load_lock

ANALYSIS = Path(__file__).resolve().parents[1]
REPO_ROOT = ANALYSIS.parents[1]
GENERATED = REPO_ROOT / "thesis" / "generated"
PROCESSING = REPO_ROOT / "results" / "processing"
ANALYSIS_VERSION = "thesis-analysis-v2"
VOLATILE_KEYS = {"generated_at", "generated_utc", "created_at", "created_utc", "timestamp"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _manifest(root: Path, paths: list[Path]) -> tuple[str, list[str]]:
    lines = [
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    ]
    body = "\n".join(lines) + "\n"
    return _sha256_bytes(body.encode()), lines


def _normalized_json(value):
    if isinstance(value, dict):
        return {key: _normalized_json(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_normalized_json(item) for item in value]
    return value


def _stable_bytes(path: Path) -> bytes:
    if path.suffix == ".svg":
        text = path.read_text()
        text = re.sub(r"<metadata>.*?</metadata>", "", text, flags=re.DOTALL)
        identifiers = {}

        def replace_identifier(match):
            identifier = match.group(1)
            if identifier not in identifiers:
                identifiers[identifier] = f"id{len(identifiers)}"
            return f'id="{identifiers[identifier]}"'

        text = re.sub(r'id="([A-Za-z]+[0-9a-f]{8,})"', replace_identifier, text)
        for old, new in identifiers.items():
            text = text.replace(f"#{old}", f"#{new}")
        return text.encode()
    if path.suffix == ".json":
        try:
            return json.dumps(
                _normalized_json(json.loads(path.read_text())),
                sort_keys=True, separators=(",", ":"),
            ).encode()
        except (OSError, json.JSONDecodeError):
            pass
    return path.read_bytes()


def _stable_output_digest(paths: list[Path]) -> str:
    lines = [
        f"{_sha256_bytes(_stable_bytes(path))}  {path.relative_to(GENERATED).as_posix()}"
        for path in sorted(paths, key=lambda item: item.relative_to(GENERATED).as_posix())
    ]
    return _sha256_bytes(("\n".join(lines) + "\n").encode())


def _implementation_digest() -> str:
    paths = [
        path for path in ANALYSIS.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    ]
    lines = [
        f"{_sha256(path)}  {path.relative_to(ANALYSIS).as_posix()}"
        for path in sorted(paths, key=lambda item: item.relative_to(ANALYSIS).as_posix())
    ]
    return _sha256_bytes(("\n".join(lines) + "\n").encode())


def processing_identity(
    input_digest: str,
    output_identity_digest: str,
    lock_digest: str,
    implementation_digest: str,
) -> str:
    material = "\0".join(
        (ANALYSIS_VERSION, input_digest, output_identity_digest, lock_digest, implementation_digest)
    )
    return f"{ANALYSIS_VERSION}-{_sha256_bytes(material.encode())[:16]}"


def _git_state() -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO_ROOT, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            ).stdout.strip()
        except OSError:
            return ""

    status = git("status", "--porcelain=v1")
    return {
        "revision": git("rev-parse", "HEAD") or "unknown",
        "dirty": bool(status),
        "branch": git("branch", "--show-current"),
    }


def record_analysis_provenance(command: str) -> Path:
    bundle = campaign_bundle()
    campaign_name = campaign_id()
    campaign_paths = [path for path in (bundle / "benchmarks").rglob("*") if path.is_file()]
    for name in ("campaign.json", "campaign-config.json", "VALIDATION.json", "COMPLETE"):
        path = bundle / name
        if path.is_file():
            campaign_paths.append(path)
    campaign_paths += [path for path in (bundle / "provenance").rglob("*") if path.is_file()]
    campaign_digest, campaign_lines = _manifest(bundle, campaign_paths)

    output_paths = [path for path in GENERATED.rglob("*") if path.is_file()]
    output_raw_digest, output_lines = _manifest(GENERATED, output_paths)
    output_identity_digest = _stable_output_digest(output_paths)

    git = _git_state()
    lock = load_lock()
    lock_path = ANALYSIS / "campaign.lock.json"
    lock_digest = _sha256(lock_path)
    implementation_digest = _implementation_digest()
    analysis_id = processing_identity(
        campaign_digest, output_identity_digest, lock_digest, implementation_digest
    )
    target = PROCESSING / campaign_name / analysis_id
    payload = {
        "schema_version": 1,
        "analysis_id": analysis_id,
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": command,
        "analyzer": {
            "revision": git["revision"],
            "dirty": git["dirty"],
            "branch": git["branch"],
        },
        "implementation_sha256": implementation_digest,
        "input_campaign": campaign_name,
        "campaign_lock_sha256": lock_digest,
        "supplementary_evidence": lock.get("supplementary_evidence", []),
        "safety_evidence": lock.get("safety_evidence", {}),
        "input_manifest_sha256": campaign_digest,
        "input_manifest_file": "inputs.sha256",
        "outputs_manifest_sha256": output_raw_digest,
        "outputs_identity_sha256": output_identity_digest,
        "outputs_manifest": "outputs.sha256",
        "identity_normalization": [
            "JSON keys: generated_at, generated_utc, created_at, created_utc, timestamp",
            "SVG: metadata blocks and generated element identifiers",
        ],
    }
    if target.is_dir():
        existing_path = target / "provenance.json"
        try:
            existing = json.loads(existing_path.read_text())
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(f"processing record is incomplete and will not be overwritten: {target}")
        equivalent = all(
            existing.get(key) == payload[key]
            for key in (
                "analysis_version", "campaign_lock_sha256", "input_manifest_sha256",
                "outputs_identity_sha256", "implementation_sha256",
            )
        )
        if equivalent:
            if existing.get("outputs_manifest_sha256") != output_raw_digest:
                print(
                    f"reusing processing provenance {target.relative_to(REPO_ROOT)} "
                    "(raw output bytes differ only in normalized metadata; existing hash manifests preserved)"
                )
            else:
                print(f"reusing processing provenance {target.relative_to(REPO_ROOT)}")
            return target
        raise RuntimeError(f"processing identity collision; refusing to overwrite {target}")

    target.mkdir(parents=True)
    (target / "inputs.sha256").write_text("\n".join(campaign_lines) + "\n", encoding="utf-8")
    (target / "outputs.sha256").write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    (target / "provenance.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote processing provenance {target.relative_to(REPO_ROOT)}")
    return target
