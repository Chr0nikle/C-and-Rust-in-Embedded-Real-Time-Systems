#!/usr/bin/env python3
"""Long-duration stability campaign (host-side; no firmware change).

Runs the advertised benchmark suite repeatedly for one board x RTOS x language
path and records a checkpoint after every chunk, then reports per-case drift
between the first and last chunk and the trend across chunks. This is the
`long_duration_campaign` planned feature: it re-uses raw samples from
`rtos benchmark` and adds no protocol case ID.

Non-latency cases (19, 22, 23, 24, 27, 28) are reported as raw values; their
`drift` is a value drift, not a latency drift.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NON_LATENCY_CASES = {19, 22, 23, 24, 27, 28}


def case_medians(data: dict) -> dict[int, float]:
    by_case: dict[int, list[int]] = {}
    for sample in data.get("samples", []):
        by_case.setdefault(int(sample["case"]), []).append(int(sample["ticks"]))
    return {c: float(statistics.median(v)) for c, v in by_case.items()}


def newest_new_result(before: set[str], results: Path) -> Path | None:
    after = set(glob.glob(str(results / "*" / "benchmark-run-*.json")))
    after |= set(glob.glob(
        str(results / "campaign-replication-*" / "benchmarks" / "*" / "benchmark-run-*.json")
    ))
    new = after - before
    if not new:
        return None
    return Path(max(new, key=lambda p: Path(p).stat().st_mtime))


def trend_slope(series: list[float]) -> float | None:
    """Least-squares slope over chunk index; None with fewer than two points."""
    n = len(series)
    if n < 2:
        return None
    xs = list(range(n))
    mx = statistics.fmean(xs)
    my = statistics.fmean(series)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, series)) / denom


def summarise(chunks: list[dict]) -> dict:
    per_case: dict[int, list[tuple[int, float]]] = {}
    for chunk in chunks:
        for case, median in chunk["medians"].items():
            per_case.setdefault(case, []).append((chunk["index"], median))
    summary = {}
    for case, points in sorted(per_case.items()):
        points.sort()
        values = [v for _, v in points]
        first, last = values[0], values[-1]
        summary[case] = {
            "chunks_present": len(values),
            "first": first,
            "last": last,
            "min": min(values),
            "max": max(values),
            "median": float(statistics.median(values)),
            "drift": last - first,
            "drift_ratio": (last - first) / first if first else None,
            "slope_per_chunk": trend_slope(values),
            "series": values,
            "non_latency": case in NON_LATENCY_CASES,
        }
    return summary


def render_markdown(rtos: str, board: str, language: str, chunks: list[dict], summary: dict,
                    created: str) -> str:
    lines = [
        f"# Long-duration campaign: {rtos}/{board}/{language}",
        "",
        f"Created {created}; {len(chunks)} chunk(s). A chunk is one full advertised "
        "suite run (or the `--cases` subset). `drift` = last − first chunk median; "
        "`slope` = least-squares change per chunk. Non-latency cases (19/22/23/24/27/28) "
        "are raw values, not microseconds.",
        "",
        "| case | chunks | first | last | min | max | median | drift | drift_ratio | slope/chunk | non-latency |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case, s in summary.items():
        ratio = "-" if s["drift_ratio"] is None else f"{s['drift_ratio']:+.4f}"
        slope = "-" if s["slope_per_chunk"] is None else f"{s['slope_per_chunk']:+.3f}"
        lines.append(
            f"| {case} | {s['chunks_present']} | {s['first']:.3f} | {s['last']:.3f} | "
            f"{s['min']:.3f} | {s['max']:.3f} | {s['median']:.3f} | {s['drift']:+.3f} | "
            f"{ratio} | {slope} | {'yes' if s['non_latency'] else 'no'} |"
        )
    lines.append("")
    lines.append("Chunk sources:")
    lines.append("")
    for chunk in chunks:
        lines.append(
            f"- {chunk['index']}: `{chunk['result']}` (build `{chunk['build_id']}`, "
            f"{chunk['repetitions']} reps)"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtos", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--cases", default=None, help="comma-separated case IDs")
    parser.add_argument("--repetitions", type=int, default=None)
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--gap", type=float, default=0.0, help="seconds between chunks")
    parser.add_argument("--project", type=Path,
                        default=Path(__file__).resolve().parents[2])
    parser.add_argument("--rtos-bin", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project = args.project.resolve()
    rtos_bin = args.rtos_bin or (project / "target" / "debug" / "rtos")
    results = project.parent / "results" / "campaigns"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    created = datetime.now(timezone.utc).isoformat()
    out_dir = args.out or (
        results / f"campaign-{args.rtos}-{args.board}-{args.language}-{stamp}"
    )

    if not args.dry_run and not out_dir.exists():
        out_dir.mkdir(parents=True)

    chunks: list[dict] = []
    for i in range(args.chunks):
        before = set(glob.glob(str(results / "*" / "benchmark-run-*.json")))
        before |= set(glob.glob(
            str(results / "campaign-replication-*" / "benchmarks" / "*" / "benchmark-run-*.json")
        ))
        cmd = [str(rtos_bin), "benchmark", args.rtos, "--board", args.board,
               "--language", args.language, "--port", args.port]
        if args.cases:
            cmd += ["--cases", args.cases]
        if args.repetitions is not None:
            cmd += ["--repetitions", str(args.repetitions)]
        if args.warmup is not None:
            cmd += ["--warmup", str(args.warmup)]
        if args.dry_run:
            print("DRY-RUN:", " ".join(cmd))
            continue
        print(f"[campaign] chunk {i + 1}/{args.chunks}: {' '.join(cmd)}", flush=True)
        completed = subprocess.run(cmd, cwd=project)
        if completed.returncode != 0:
            print(f"[campaign] chunk {i} failed (exit {completed.returncode}); stopping")
            break
        result_path = newest_new_result(before, results)
        if result_path is None:
            print(f"[campaign] chunk {i}: no new result JSON found; stopping")
            break
        data = json.loads(result_path.read_text())
        chunk = {
            "index": i,
            "result": str(result_path.relative_to(project)),
            "build_id": data.get("build_id"),
            "ticks_per_second": data.get("ticks_per_second"),
            "repetitions": data.get("repetitions"),
            "medians": case_medians(data),
            "memory_high_water": data.get("memory_high_water"),
        }
        chunks.append(chunk)
        print(f"[campaign] chunk {i}: {result_path.name} cases={len(chunk['medians'])}",
              flush=True)
        # Checkpoint after every chunk so a long campaign survives interruption.
        payload = {"created": created, "rtos": args.rtos, "board": args.board,
                   "language": args.language, "chunks": chunks}
        (out_dir / "campaign.json").write_text(json.dumps(payload, indent=2))
        (out_dir / "campaign.md").write_text(
            render_markdown(args.rtos, args.board, args.language, chunks,
                            summarise(chunks), created)
        )
        if args.gap and i + 1 < args.chunks:
            time.sleep(args.gap)

    if args.dry_run:
        return 0
    if not chunks:
        print("[campaign] no successful chunks; nothing written")
        return 1
    summary = summarise(chunks)
    payload = {"created": created, "rtos": args.rtos, "board": args.board,
               "language": args.language, "chunks": chunks, "summary": {
                   str(k): v for k, v in summary.items()
               }}
    (out_dir / "campaign.json").write_text(json.dumps(payload, indent=2))
    (out_dir / "campaign.md").write_text(
        render_markdown(args.rtos, args.board, args.language, chunks, summary, created)
    )
    print(f"[campaign] wrote {out_dir / 'campaign.json'} and campaign.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
