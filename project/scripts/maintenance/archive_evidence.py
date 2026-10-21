#!/usr/bin/env python3
"""Create a deterministic, self-describing evidence archive.

The archive contains the locked campaign, selected safety evidence, analysis
provenance, publication inputs and the lock file. Original evidence is never
modified or deleted. Raw evidence that is already missing is reported, not
synthesized.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / "thesis" / "analysis" / "campaign.lock.json"
DEFAULT_OUT = ROOT / "results" / "archive"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(lock: dict) -> tuple[list[Path], list[str]]:
    campaign = ROOT / "results" / "campaigns" / lock["benchmark_campaign"]
    if not campaign.is_dir():
        raise SystemExit(f"locked campaign missing: {campaign}")
    paths = [campaign]
    missing = []
    for entry in lock.get("supplementary_evidence", []):
        bundle = ROOT / "results" / "campaigns" / entry["campaign"]
        if bundle.is_dir():
            paths.append(bundle)
        else:
            missing.append(f"supplementary bundle {entry['campaign']} ({entry.get('status', 'unknown')})")
    safety = lock.get("safety_evidence", {})
    for key in ("generated_inventory", "generated_outputs"):
        path = ROOT / safety.get(key, "")
        if path.is_dir():
            paths.append(path)
        else:
            missing.append(f"safety evidence {key} at {path}")
    analyses = ROOT / "results" / "processing" / lock["benchmark_campaign"]
    if analyses.is_dir():
        paths.append(analyses)
    for extra in (ROOT / "thesis" / "generated", ROOT / "thesis" / "analysis" / "campaign.lock.json"):
        if extra.is_dir():
            paths.append(extra)
        elif extra.is_file():
            paths.append(extra)
        else:
            missing.append(f"publication input {extra.relative_to(ROOT)}")
    return paths, missing


def walk(paths: list[Path]) -> list[Path]:
    files = set()
    for path in paths:
        if path.is_file():
            files.add(path)
        else:
            files.update(item for item in path.rglob("*") if item.is_file())
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())


def manifest(files: list[Path]) -> str:
    lines = [f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}" for path in files]
    return "\n".join(lines) + "\n"


def add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    info = tar.gettarinfo(str(path), arcname=arcname)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
    with path.open("rb") as handle:
        tar.addfile(info, handle)


def write_archive(files: list[Path], output: Path, lock: dict, missing: list[str], dry_run: bool, partial: bool = False) -> None:
    text = manifest(files)
    status = {
        "campaign": lock["benchmark_campaign"],
        "complete": not missing and not partial,
        "missing": missing,
        "files": len(files),
        "manifest_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    if dry_run:
        print(f"dry run: {len(files)} files, {sum(p.stat().st_size for p in files) / 1e6:.1f} MB")
        print(f"manifest sha256: {status['manifest_sha256']}")
        for item in missing:
            print(f"missing: {item}")
        return
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing archive: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    title = "PARTIAL thesis evidence archive" if (partial or missing) else "Thesis evidence archive"
    readme = (
        f"# {title}\n\n"
        f"Campaign: {lock['benchmark_campaign']}\n"
        f"Created: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"Complete: {'no' if (partial or missing) else 'yes'}\n\n"
        "Verify with:\n\n"
        "    python3 project/scripts/maintenance/archive_evidence.py --verify ARCHIVE\n\n"
        "EVIDENCE-MANIFEST.sha256 lists every payload file with its SHA-256.\n"
    )
    if missing:
        readme += "\n## Missing evidence\n\n" + "\n".join(f"- {item}" for item in missing) + "\n"
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                status_bytes = (json.dumps(status, indent=2, sort_keys=True) + "\n").encode()
                for name, data in (("EVIDENCE-MANIFEST.sha256", text.encode()),
                                   ("EVIDENCE-README.md", readme.encode()),
                                   ("EVIDENCE-STATUS.json", status_bytes)):
                    info = tarfile.TarInfo(name)
                    info.size = len(data); info.mtime = 0; info.mode = 0o644
                    info.uid = info.gid = 0; info.uname = info.gname = ""
                    tar.addfile(info, __import__("io").BytesIO(data))
                for path in files:
                    add_file(tar, path, path.relative_to(ROOT).as_posix())
    print(f"wrote {output} ({output.stat().st_size / 1e6:.1f} MB)")
    print(f"manifest sha256: {hashlib.sha256(text.encode()).hexdigest()}")


def verify(archive: Path) -> int:
    expected = {}
    with tarfile.open(archive, mode="r:gz") as tar:
        manifest_member = tar.extractfile("EVIDENCE-MANIFEST.sha256")
        if manifest_member is None:
            raise SystemExit("archive has no EVIDENCE-MANIFEST.sha256")
        for line in manifest_member.read().decode().splitlines():
            digest, name = line.split("  ", 1)
            expected[name] = digest
        actual = {}
        for member in tar:
            if member.isfile() and member.name not in {"EVIDENCE-MANIFEST.sha256", "EVIDENCE-README.md", "EVIDENCE-STATUS.json"}:
                data = tar.extractfile(member).read()
                actual[member.name] = hashlib.sha256(data).hexdigest()
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
        print(f"archive mismatch: missing={missing[:5]} extra={extra[:5]} changed={changed[:5]}", file=sys.stderr)
        return 1
    print(f"archive verified: {len(actual)} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="archive path (default: results/archive/<campaign>-evidence.tar.gz)")
    parser.add_argument("--dry-run", action="store_true", help="list contents without writing")
    parser.add_argument("--partial", action="store_true", help="explicitly write an incomplete archive")
    parser.add_argument("--inventory", type=Path, help="write a machine-readable inventory JSON")
    parser.add_argument("--verify", type=Path, metavar="ARCHIVE", help="verify an existing archive")
    args = parser.parse_args(argv)
    if args.verify:
        return verify(args.verify)
    lock = json.loads(LOCK.read_text())
    paths, missing = collect(lock)
    files = walk(paths)
    if missing and not args.partial and not args.dry_run:
        for item in missing:
            print(f"missing: {item}", file=sys.stderr)
        raise SystemExit("refusing to create a complete archive; use --partial to record an incomplete archive")
    suffix = ".partial.tar.gz" if args.partial or missing else ".tar.gz"
    output = args.output or DEFAULT_OUT / f"{lock['benchmark_campaign']}-evidence{suffix}"
    write_archive(files, output, lock, missing, args.dry_run, partial=args.partial)
    if args.inventory and not args.dry_run:
        payload = {
            "campaign": lock["benchmark_campaign"],
            "complete": False if (args.partial or missing) else True,
            "missing": missing,
            "archive": str(output),
            "files": [
                {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}
                for path in files
            ],
        }
        args.inventory.parent.mkdir(parents=True, exist_ok=True)
        args.inventory.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote inventory {args.inventory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
