#!/usr/bin/env python3
"""Benchmark campaign driver and live status monitor.

Subcommands:
    run       Run the full board x RTOS x language campaign (resumable).
    append    Append replication runs in passes, one per combination per pass.
    status    Render live progress from markers, the log and running processes.

Usage:
    python3 scripts/benchmarks/campaign.py run [--runs N] [--only TEXT]
    python3 scripts/benchmarks/campaign.py append [--max-passes N]
    python3 scripts/benchmarks/campaign.py status [--watch [SECONDS]]

Run ``campaign.py <subcommand> --help`` for the complete option list.
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import platform
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CAMPAIGN_SCHEMA_VERSION = 2
PROJECT = Path(__file__).resolve().parents[2]
RESULTS = PROJECT.parent / "results" / "campaigns"
RTOS = PROJECT / "target" / "debug" / "rtos"
LOG = PROJECT / "campaign.log"
def result_relative(path: Path) -> str:
    """Return a results-relative path for both repo and test layouts."""
    resolved = Path(path).resolve()
    for base in (PROJECT, PROJECT.parent):
        try:
            return str(resolved.relative_to(base))
        except ValueError:
            continue
    return str(resolved)


def result_path(relative: str) -> Path:
    """Resolve a stored results path in the active project layout."""
    candidate = PROJECT / relative
    return candidate if candidate.exists() else PROJECT.parent / relative


DONE = PROJECT / ".campaign-done"
DONE.mkdir(exist_ok=True)
LIVE_DIR = PROJECT / ".campaign-live"
LIVE_DIR.mkdir(exist_ok=True)

RETRIES = int(os.environ.get("CAMPAIGN_RETRIES", "2"))
RETRY_DELAY = float(os.environ.get("CAMPAIGN_RETRY_DELAY", "5"))

# Hard per-run cap. The driver streams the child's stdout until EOF; without a
# deadline a UART-wedged board (a stalled J-Link VCOM or C6 USB-JTAG) blocks its
# board worker and therefore the whole campaign forever. On timeout the run's
# whole process group is killed and the unit is retried/failed normally.
RUN_TIMEOUT = float(os.environ.get("CAMPAIGN_RUN_TIMEOUT", "900"))

# Per-(rtos, board) console overrides for images that enumerate a different
# USB CDC descriptor. Empty by design: all current routes share the board port.
PORT_OVERRIDE = {}


def resolve_board_port(project: Path, board: str, *,
                       c6_matches: list[str] | None = None) -> str:
    """Resolve a board's console from its manifest, never an enumeration index.

    Boards with a verified ``serial.port_selector`` use it verbatim. The
    ESP32-C6 manifest deliberately leaves the selector empty: its only console
    is the USB-Serial/JTAG endpoint, so require exactly one stable by-id node
    rather than guessing ``ttyACM0``.
    """
    manifest = project / "boards" / board / "board.toml"
    if not manifest.is_file():
        raise ValueError(f"board manifest missing: {manifest}")
    with manifest.open("rb") as handle:
        data = tomllib.load(handle)
    selector = str(data.get("serial", {}).get("port_selector", "")).strip()
    if selector:
        if not Path(selector).exists():
            raise ValueError(f"{board}: configured console {selector} is absent")
        return selector
    if board == "esp32c6":
        matches = sorted(c6_matches if c6_matches is not None
                         else glob.glob(C6_BY_ID_GLOB))
        if len(matches) != 1:
            raise ValueError(
                f"esp32c6: expected exactly one USB-Serial/JTAG console, "
                f"found {len(matches)} ({matches})"
            )
        return matches[0]
    raise ValueError(f"{board}: manifest has no serial.port_selector")


def port_for(rtos: str, board: str) -> str:
    """Resolve the console device for a specific (rtos, board) run."""
    override = PORT_OVERRIDE.get((rtos, board))
    return override if override is not None else resolve_board_port(PROJECT, board)


# Per-board repetition override. Empty by design: every board/RTOS/language
# combination is measured with the same sample budget, with no per-board
# exception (nRF previously ran a reduced budget).
REPS_OVERRIDE = {}
DEFAULT_REPS = 1000
DEFAULT_WARMUP = 100


def reps_for(board: str) -> int:
    return REPS_OVERRIDE.get(board, DEFAULT_REPS)


# Per-port VBUS power control for the ESP32-C6's USB-Serial-JTAG. The C6 wedges
# after a flash+run and only a true power cycle clears it. The kernel's `disable`
# sysfs only cuts USB *data* (PORT_ENABLE) and left the chip powered, so use
# uhubctl's PORT_POWER switch on the downstream RTS5411 hub (bus 6, path 1.4.4,
# port 1), which is the C6's own USB port. Port 3 of hub 6-1.4 is the STM32H723
# CP2102 console, so cycling it caused H723 serial broken pipes and timeouts.
# sudo NOPASSWD is configured for this exact binary.
UHUBCTL = "/home/chronikle/.local/bin/uhubctl"
C6_HUB_PORT = "6-1.4.4"
C6_HUB_PORT_NUM = "1"
C6_POWER_OFF_S = 2.0          # hold VBUS off long enough for a full reset
C6_RESTORE_TIMEOUT_S = 20.0   # wait for the USB-Serial-JTAG to re-enumerate
C6_BY_ID_GLOB = "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*"


def _wait_for_port(port: str, timeout: float) -> bool:
    """Wait for one exact console path, not merely any matching C6 device."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(port).exists():
            return True
        time.sleep(0.5)
    return Path(port).exists()


def power_cycle_c6(port: str) -> bool:
    """Power-cycle the ESP32-C6's USB port (VBUS) to clear its USB-JTAG wedge.

    `uhubctl -a cycle` toggles VBUS for only a few milliseconds, which does not
    reliably reset the USB-Serial-JTAG peripheral; drive an explicit
    off/settle/on sequence and wait for the exact console node to re-enumerate.
    """
    def _switch(action: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sudo", "-n", UHUBCTL, "-l", C6_HUB_PORT, "-p", C6_HUB_PORT_NUM,
             "-a", action],
            capture_output=True, text=True, timeout=30,
        )

    if not Path(port).exists():
        log(f"[c6] console {port} is absent before power-cycle")
        return False
    try:
        # The hub also powers the H723 CP2102 on another port. Verify the
        # configured downstream port really owns 303a:1001 before switching it.
        status = _switch("status")
        if status.returncode != 0 or "303a:1001" not in status.stdout:
            log("[c6] refusing to cycle hub port: ESP32-C6 303a:1001 not present "
                f"on {C6_HUB_PORT} port {C6_HUB_PORT_NUM}")
            return False
        off = _switch("off")
        if off.returncode != 0:
            log(f"[c6] VBUS off failed rc={off.returncode}: {off.stderr.strip()}")
            return False
        # Wait for this endpoint to actually drop, then hold VBUS off.
        deadline = time.monotonic() + 5.0
        while Path(port).exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(C6_POWER_OFF_S)
        on = _switch("on")
        if on.returncode != 0:
            log(f"[c6] VBUS on failed rc={on.returncode}: {on.stderr.strip()}")
            return False
        if _wait_for_port(port, C6_RESTORE_TIMEOUT_S):
            log(f"[c6] power-cycled USB-Serial-JTAG (VBUS off {C6_POWER_OFF_S:.0f}s); "
                f"{port} re-enumerated")
            return True
        log(f"[c6] VBUS cycled but {port} did not re-enumerate within "
            f"{C6_RESTORE_TIMEOUT_S:.0f}s")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[c6] power-cycle failed: {e}")
        return False

# Full runnable combination set (rtos, board, language) for the distilled scope.
COMBOS = [
    ("freertos", "esp32c6", "c"),
    ("freertos", "esp32c6", "rust"),
    ("freertos", "nrf52840dk", "c"),
    ("freertos", "nrf52840dk", "rust"),
    ("freertos", "stm32f4discovery", "c"),
    ("freertos", "stm32f4discovery", "rust"),
    ("freertos-in-rust", "stm32f4discovery", "rust"),
    ("nuttx", "esp32c6", "c"),
    ("nuttx", "esp32c6", "rust"),
    ("nuttx", "nrf52840dk", "c"),
    ("nuttx", "nrf52840dk", "rust"),
    ("nuttx", "stm32f4discovery", "c"),
    ("nuttx", "stm32f4discovery", "rust"),
    ("rivet", "stm32f4discovery", "rust"),
    ("zephyr", "esp32c6", "c"),
    ("zephyr", "esp32c6", "rust"),
    ("zephyr", "nrf52840dk", "c"),
    ("zephyr", "nrf52840dk", "rust"),
    ("zephyr", "stm32f4discovery", "c"),
    ("zephyr", "stm32f4discovery", "rust"),
    # STM32H723 (FK723M1-ZGT6): common 520 MHz, USART1/CP2102 console for all.
    # Mirrors the STM32F4 language axis; Rivet is M4-only, so it is not run here.
    ("freertos", "stm32h723", "c"),
    ("freertos", "stm32h723", "rust"),
    ("freertos-in-rust", "stm32h723", "rust"),
    ("nuttx", "stm32h723", "c"),
    ("nuttx", "stm32h723", "rust"),
    ("zephyr", "stm32h723", "c"),
    ("zephyr", "stm32h723", "rust"),
]


_LOG_LOCK = threading.Lock()


def _write_console(line: str) -> None:
    """Append a raw line to the console and campaign.log (thread-safe)."""
    with _LOG_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()
        with LOG.open("a") as handle:
            handle.write(line)
            handle.flush()


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_console(f"{stamp} {message}\n")


# Campaign board name -> firmware-directory token. FreeRTOS/NuttX/Zephyr name the
# STM32F4 Discovery directory `stm32f4`, while the CLI Board is `stm32f4discovery`.
BOARD_DIR_TOKEN = {
    "stm32f4discovery": "stm32f4",
    "nrf52840dk": "nrf52840dk",
    "esp32c6": "esp32c6",
}

# The ESP32-C6 FreeRTOS C firmware dir is the plugin base, so its build dir
# carries no board token and must be cleaned explicitly.
BOARDLESS_CLEAN = {
    ("freertos", "esp32c6"): ("firmware/c-kernels/freertos/c/build",),
}

# Native C kernels live under firmware/c-kernels/, native Rust kernels under
# firmware/rust-kernels/.
RUST_KERNELS = frozenset(
    {
        "ariel",
        "embassy",
        "freertos-in-rust",
        "hubris",
        "rivet",
        "rtic",
        "tock",
    }
)


def rtos_firmware_dir(rtos: str) -> Path:
    """Project-relative firmware root for an RTOS, by kernel language."""
    group = "rust-kernels" if rtos in RUST_KERNELS else "c-kernels"
    return PROJECT / "firmware" / group / rtos


def clean_rtos_builds(rtos: str, board: str) -> None:
    """Remove THIS board's stale build output before a clean rebuild.

    Only freertos/nuttx/rtthread honour the plugin `clean` flag; chibios,
    threadx, zephyr, riot and rtems silently reuse their previous build dir, so
    an incremental `make` after a source/toolchain change produces a stale image
    that no longer boots. Wiping `build`/`bin` under the RTOS tree forces the
    benchmark's own build step to compile from scratch.

    The wipe is scoped to the unit's board: wiping the whole RTOS tree also
    deletes the build dirs of same-RTOS combinations still in progress on other
    boards, whose later `--reuse-build` runs then fail with "build directory is
    gone".
    """
    root = rtos_firmware_dir(rtos)
    token = BOARD_DIR_TOKEN.get(board, board)
    targets: set[Path] = set()
    if root.is_dir():
        for name in ("build", "bin", "o-optimize"):
            for directory in root.rglob(name):
                if not directory.is_dir() or "target" in directory.parts:
                    continue
                if token in directory.parts:
                    targets.add(directory)
    for rel in BOARDLESS_CLEAN.get((rtos, board), ()):
        candidate = PROJECT / rel
        if candidate.is_dir():
            targets.add(candidate)
    # Remove deepest first so a parent wipe does not race a child.
    for directory in sorted(targets, key=lambda p: len(p.parts), reverse=True):
        shutil.rmtree(directory, ignore_errors=True)


def is_error_line(line: str) -> bool:
    """True if a benchmark output line signals a failure."""
    stripped = line.strip()
    if stripped.startswith("rtos: "):
        return True
    low = stripped.lower()
    return any(
        key in low
        for key in ("failed", "timeout", "panic", "fatal", "error",
                    "no such file", "did not enumerate", "failed to connect")
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a benchmark's whole process group (rtos + probe-rs/esptool)."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def parse_cases(value: str | None) -> list[int] | None:
    """Parse a comma-separated case selection, or return None for the full suite."""
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise SystemExit("--cases must contain at least one case ID")
    try:
        cases = sorted({int(part) for part in parts})
    except ValueError as exc:
        raise SystemExit(f"invalid --cases value: {value!r}") from exc
    if cases[0] < 0 or cases[-1] > 28:
        raise SystemExit(f"--cases must use protocol IDs 0..28, got {cases}")
    return cases


def case_selection_error(data: dict, expected_cases: list[int] | None,
                         expected_warmup: int | None = None) -> str | None:
    """Reject a result whose case selection or sample budget is wrong."""
    if expected_cases is None:
        return None
    if not isinstance(data, dict):
        return "result is not an object"
    actual = data.get("cases")
    if not isinstance(actual, list) or sorted(actual) != sorted(expected_cases):
        return f"case selection mismatch: expected {expected_cases}, got {actual!r}"
    repetitions = data.get("repetitions")
    if not isinstance(repetitions, int) or repetitions <= 0:
        return "missing positive repetitions"
    if expected_warmup is not None and data.get("warmup") != expected_warmup:
        return f"warmup mismatch: expected {expected_warmup}, got {data.get('warmup')!r}"
    counts = {case: 0 for case in expected_cases}
    for sample in data.get("samples", []):
        if isinstance(sample, dict) and sample.get("case") in counts:
            counts[sample["case"]] += 1
    for case in expected_cases:
        if counts[case] != repetitions:
            return (f"case {case} sample count mismatch: "
                    f"expected {repetitions}, got {counts[case]}")
    return None


def run(rtos: str, board: str, language: str, run_index: int,
        reps: int, warmup: int, build_lock=None,
        cases: list[int] | None = None) -> tuple[bool, str]:
    port = port_for(rtos, board)
    if board == "esp32c6" and not power_cycle_c6(port):
        return False, "ESP32-C6 console did not recover after VBUS power-cycle"
    # The first run of a combination builds clean; later runs reuse the
    # unchanged image (the run itself is still an independent restart). Only a
    # BUILD touches shared state (the source tree, or a shared build dir such as
    # NuttX's), so the per-RTOS lock is held ONLY around builds. `--reuse-build`
    # runs (no compilation) and the flash+run phase use per-board probes,
    # consoles and build dirs, so they run in parallel across boards.
    reuse = run_index > 0
    cmd = [
        str(RTOS), "benchmark", rtos,
        "--board", board,
        "--language", language,
        "--port", port,
        "--repetitions", str(reps),
        "--warmup", str(warmup),
    ]
    if cases is not None:
        cmd += ["--cases", ",".join(str(case) for case in cases)]
    if reuse:
        cmd.append("--reuse-build")
    case_note = f" cases={cases}" if cases is not None else ""
    log(f"START {rtos}/{board}/{language} run={run_index} port={port} reps={reps} "
        f"warmup={warmup}{case_note}")

    def execute(argv: list[str]) -> tuple[bool, str]:
        started = time.monotonic()
        # Stream the benchmark's merged stdout/stderr live so workflow and UART
        # progress are visible in real time; only the per-case tick dumps
        # ("ticks=[...]", thousands of lines) are suppressed. A reader thread
        # keeps the pipe drained while the parent waits with a hard RUN_TIMEOUT.
        proc = subprocess.Popen(
            argv, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        assert proc.stdout is not None
        last_error = ""
        live_path = LIVE_DIR / f"{rtos}-{board}-{language}.log"
        live = live_path.open("w")

        def pump() -> None:
            nonlocal last_error
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                if "ticks=[" in line:
                    continue
                _write_console(line)
                live.write(line)
                live.flush()
                if is_error_line(line):
                    last_error = line.strip()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            rc = proc.wait(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            rc = proc.wait()
        reader.join(timeout=10)
        live.close()
        elapsed = time.monotonic() - started
        if timed_out:
            last_error = f"run exceeded {RUN_TIMEOUT:.0f}s and was killed"
            log(f"TIMEOUT {rtos}/{board}/{language} run={run_index} after {elapsed:.0f}s; killed")
        ok = rc == 0 and not timed_out
        log(f"{'PASS' if ok else 'FAIL'} {rtos}/{board}/{language} run={run_index} rc={rc} {elapsed:.0f}s")
        return ok, ("" if ok else (last_error or "unknown error"))

    # Only the building paths take the per-RTOS lock; reuse runs go straight
    # through so boards no longer serialise on the same RTOS.
    lock = build_lock if build_lock is not None else contextlib.nullcontext()
    if reuse:
        ok, reason = execute(cmd)
        if not ok:
            # A reused image can be stale or have had its cached dir removed by
            # a same-RTOS clean elsewhere (the ESP32-C6 shares one boardless
            # build dir; other boards' dirs are scoped). Fall back to a clean
            # rebuild (under the build lock) instead of failing the unit.
            log(f"RETRY {rtos}/{board}/{language} run={run_index}: "
                f"reuse failed ({reason}); rebuilding clean")
            with lock:
                clean_rtos_builds(rtos, board)
                ok, reason = execute([c for c in cmd if c != "--reuse-build"])
    else:
        with lock:
            clean_rtos_builds(rtos, board)
            ok, reason = execute(cmd)
    return ok, reason


def result_files() -> list[str]:
    """Every benchmark-run JSON under results/, including consolidated bundles.

    A completed campaign moves its build-id directories under
    ``results/campaigns/campaign-replication-*/benchmarks/``, so a single-level glob is no
    longer enough. The archive of removed integrations is excluded.
    """
    found = set(glob.glob(str(RESULTS / "*" / "benchmark-run-*.json")))
    found |= set(glob.glob(
        str(RESULTS / "campaign-replication-*" / "benchmarks" / "*" / "benchmark-run-*.json")
    ))
    return sorted(found)


def result_snapshot() -> set[str]:
    return set(result_files())


def result_index() -> dict:
    """Map (rtos, board, language) to the newest result file for that unit.

    Used to resolve a finished unit to its evidence when the live ``before``
    snapshot could not attribute the new file (for example a legacy ``ok``
    marker that stored no path).
    """
    index: dict = {}
    for path in result_files():
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        key = (data.get("rtos"), data.get("board"), data.get("language"))
        best = index.get(key)
        if best is None or os.path.getmtime(path) > os.path.getmtime(best):
            index[key] = path
    return index


def unit_result(before: set[str], rtos: str, board: str, language: str):
    """Return (relative_path, data) of the run this unit produced.

    New result files are filtered to the unit's own (rtos, board, language) so
    parallel units on other boards are never mis-attributed.
    """
    new = sorted(
        set(result_files()) - before,
        key=os.path.getmtime, reverse=True,
    )
    for path in new:
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if (data.get("rtos") == rtos and data.get("board") == board
                and data.get("language") == language):
            return result_relative(path), data
    return None, None


TP_CASE = 19
TP_MAX_VALID_TICKS = 1 << 63


def measurement_issues(data: dict) -> list[dict]:
    """Return case-specific invalidity diagnostics without dropping raw data.

    Protocol validity (``result_is_valid``) and scientific validity are
    separate. Case 19 transfers a fixed 1 MiB; a zero duration cannot describe
    that transfer, and a duration near 2**64 is the signature of an unsigned
    32-bit counter underflow, not a slow transfer. The threshold is not a
    performance cutoff: 2**63 ticks is millennia at these counter rates.
    """
    issues: list[dict] = []
    if not isinstance(data, dict):
        return issues
    samples = data.get("samples")
    if not isinstance(samples, list):
        return issues
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("case") != TP_CASE:
            continue
        ticks = sample.get("ticks")
        if not isinstance(ticks, int):
            continue
        if ticks <= 0:
            reason = "zero_duration"
        elif ticks >= TP_MAX_VALID_TICKS:
            reason = "wrapped_duration"
        else:
            continue
        issues.append({
            "case": TP_CASE,
            "index": sample.get("index"),
            "variant": sample.get("variant", 0),
            "ticks": ticks,
            "reason": reason,
        })
    return issues


def result_is_valid(data: dict, rtos: str, board: str, language: str) -> bool:
    """Reject an untrustworthy result file before it can count as a unit.

    The protocol layer validates the live stream; this guards campaign
    reconstruction against a missing, truncated or misattributed file. A unit
    is recorded only when identity, positive repetition/timebase and a sample
    array are present.
    """
    if not isinstance(data, dict):
        return False
    if (data.get("rtos"), data.get("board"), data.get("language")) != (
            rtos, board, language):
        return False
    if not isinstance(data.get("build_id"), str) or not data["build_id"]:
        return False
    repetitions = data.get("repetitions")
    ticks_per_second = data.get("ticks_per_second")
    if not isinstance(repetitions, int) or repetitions <= 0:
        return False
    if not isinstance(ticks_per_second, int) or ticks_per_second <= 0:
        return False
    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        return False
    for sample in samples:
        if not isinstance(sample, dict):
            return False
        if not all(isinstance(sample.get(field), int)
                   for field in ("case", "index", "ticks")):
            return False
        if sample["case"] < 0 or sample["index"] < 0 or sample["ticks"] < 0:
            return False
    return True


def collect_records(combos, runs: int, expected_cases: list[int] | None = None,
                    expected_warmup: int | None = None):
    """Build the record for every (combination, run) from its done marker.

    The marker stores the produced result path, so a bundle can be written from
    any invocation and still describe the whole campaign (including units that
    finished in an earlier invocation). A marker whose result is absent,
    truncated or misattributed becomes missing, never a recorded unit.
    """
    records = []
    missing = []
    index = None
    for rtos, board, language in combos:
        for k in range(runs):
            key = f"{rtos}/{board}/{language} run={k}"
            marker = DONE / f"{rtos}-{board}-{language}-run{k}"
            if not marker.exists():
                missing.append(key)
                continue
            rel = marker.read_text().strip()
            if not rel.startswith("results/"):
                # Legacy ``ok`` marker from an earlier campaign version: recover
                # the newest evidence for this unit so the bundle stays complete.
                if index is None:
                    index = result_index()
                fallback = index.get((rtos, board, language))
                if fallback:
                    rel = result_relative(fallback)
            data = {}
            if rel.startswith("results/"):
                try:
                    data = json.loads(result_path(rel).read_text())
                except (OSError, ValueError):
                    pass
            if not result_is_valid(data, rtos, board, language):
                missing.append(key)
                continue
            case_error = case_selection_error(data, expected_cases, expected_warmup)
            if case_error:
                missing.append(key)
                log(f"excluding {key}: {case_error}")
                continue
            issues = measurement_issues(data)
            records.append({
                "rtos": rtos, "board": board, "language": language, "run": k,
                "result": rel if rel.startswith("results/") else None,
                "build_id": data.get("build_id"),
                "repetitions": data.get("repetitions"),
                "warmup": data.get("warmup"),
                "ticks_per_second": data.get("ticks_per_second"),
                "cases": data.get("cases"),
                "measurement_status": ("invalid_samples" if issues else "valid"),
                "sample_issues": issues,
            })
    return records, missing


def consolidate_bundle(out: Path, records: list) -> None:
    """Move every referenced build-id directory into the campaign bundle.

    The campaign directory becomes self-contained: evidence physically lives
    under ``out/benchmarks/<build_id>/`` and the original ``results/campaigns/<build_id>/``
    directories are removed, so exactly one copy of each benchmark file remains.
    Record paths and resume markers are rewritten to the new location.
    """
    bench_root = out / "benchmarks"
    moved: dict[str, Path] = {}
    for record in records:
        rel = record.get("result")
        if not rel or not rel.startswith("results/campaigns/"):
            continue
        parts = Path(rel).parts
        if len(parts) != 4 or parts[2].startswith("campaign-replication"):
            continue
        build_id = parts[2]
        if build_id not in moved:
            src = RESULTS / build_id
            dest = bench_root / build_id
            if src.is_dir():
                bench_root.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.move(str(src), str(dest))
                log(f"collected results/{build_id} -> {dest}")
            moved[build_id] = dest
        record["result"] = str(
            Path("results/campaigns") / out.name / "benchmarks" / build_id / Path(rel).name
        )
        marker = DONE / f"{record['rtos']}-{record['board']}-{record['language']}-run{record['run']}"
        marker.write_text(record["result"] + "\n")




def _run_text(command: list[str], *, cwd: Path | None = None, timeout: float = 20) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = completed.stdout.strip()
    return text if completed.returncode == 0 and text else None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_paths(paths: list[Path], root: Path) -> str:
    lines = [
        f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    ]
    return _sha256_bytes(("\n".join(lines) + "\n").encode())


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git_repository_metadata(out: Path) -> dict:
    provenance = out / "provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    revision = _run_text(["git", "rev-parse", "HEAD"], cwd=PROJECT) or "unknown"
    branch = _run_text(["git", "branch", "--show-current"], cwd=PROJECT) or ""
    status = _run_text(["git", "status", "--porcelain=v1"], cwd=PROJECT) or ""
    (provenance / "git-revision.txt").write_text(revision + "\n", encoding="utf-8")
    (provenance / "git-status.txt").write_text(status + ("\n" if status else ""), encoding="utf-8")
    diff = _run_text(["git", "diff", "--binary", "--no-color"], cwd=PROJECT, timeout=60) or ""
    (provenance / "worktree.diff").write_text(diff, encoding="utf-8")
    return {
        "revision": revision,
        "branch": branch,
        "dirty": bool(status.strip()),
        "status_file": "provenance/git-status.txt",
        "worktree_diff": "provenance/worktree.diff",
    }


def _resolve_tool(name: str, patterns: list[str]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for pattern in patterns:
        matches = sorted(PROJECT.glob(pattern))
        if matches:
            return str(matches[-1])
    return None


def _toolchain_metadata(out: Path) -> dict:
    commands = {
        "python": ("python3", []),
        "rustc": ("rustc", []),
        "cargo": ("cargo", []),
        "typst": ("typst", []),
        "probe_rs": ("probe-rs", []),
        "esptool": ("esptool", [".local/toolchains/esp-idf/python_env/*/bin/esptool"]),
        "arm_gcc": ("arm-none-eabi-gcc", [".local/toolchains/arm-none-eabi/*/bin/arm-none-eabi-gcc"]),
        "riscv_gcc": ("riscv32-esp-elf-gcc", [".local/toolchains/esp-idf/tools/riscv32-esp-elf/*/riscv32-esp-elf/bin/riscv32-esp-elf-gcc"]),
        "west": ("west", [".local/west/west"]),
        "cmake": ("cmake", []),
    }
    toolchains = {}
    paths = {}
    for name, (executable, patterns) in commands.items():
        resolved = _resolve_tool(executable, patterns)
        if not resolved:
            continue
        value = _run_text([resolved, "--version"])
        if value:
            toolchains[name] = value.splitlines()[0]
            try:
                paths[name] = str(Path(resolved).resolve().relative_to(PROJECT))
            except ValueError:
                paths[name] = resolved
    rustup = _resolve_tool("rustup", [])
    payload = {
        "platform": platform.platform(),
        "python_executable": sys.executable,
        "toolchains": toolchains,
        "toolchain_paths": paths,
        "rust_active_toolchain": (
            _run_text([rustup, "show", "active-toolchain"]) if rustup else None
        ),
        "rust_targets": (
            _run_text([rustup, "target", "list", "--installed"])
            or ""
        ).splitlines() if rustup else [],
    }
    _write_json(out / "provenance" / "toolchains.json", payload)
    return payload


def _device_metadata(out: Path) -> str | None:
    if not RTOS.is_file():
        return None
    text = _run_text([str(RTOS), "list"], cwd=PROJECT, timeout=45)
    if text is None:
        return None
    (out / "provenance" / "devices.txt").write_text(text + "\n", encoding="utf-8")
    return "provenance/devices.txt"


def _preflight_metadata(out: Path, records: list) -> str | None:
    for candidate in (
        Path(os.environ.get("CAMPAIGN_WORK_DIR", "")) / "metadata" / "preflight-checks.json"
        if os.environ.get("CAMPAIGN_WORK_DIR") else None,
        PROJECT / "metadata" / "preflight-checks.json",
        Path.cwd() / "metadata" / "preflight-checks.json",
    ):
        if candidate and candidate.is_file():
            target = out / "provenance" / "preflight-checks.json"
            shutil.copyfile(candidate, target)
            return "provenance/preflight-checks.json"
    checks = []
    if RTOS.is_file():
        seen = set()
        for record in records:
            key = (record["rtos"], record["board"], record["language"])
            if key in seen:
                continue
            seen.add(key)
            completed = subprocess.run(
                [str(RTOS), "check", key[0], "--board", key[1], "--language", key[2]],
                cwd=PROJECT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=120, check=False,
            )
            checks.append({
                "rtos": key[0], "board": key[1], "language": key[2],
                "returncode": completed.returncode,
                "output": completed.stdout.strip(),
            })
    _write_json(
        out / "provenance" / "preflight-checks.json",
        {
            "status": "collected" if checks and all(c["returncode"] == 0 for c in checks) else "failed",
            "checks": checks,
        } if checks else {
            "status": "not_collected",
            "reason": "campaign driver could not run rtos check",
        },
    )
    return "provenance/preflight-checks.json"


def _campaign_config(out: Path, records: list, requested_cases: list[int] | None = None) -> str:
    candidates = []
    if os.environ.get("CAMPAIGN_WORK_DIR"):
        candidates.append(Path(os.environ["CAMPAIGN_WORK_DIR"]) / "metadata" / "campaign-config.json")
    candidates += [PROJECT / "metadata" / "campaign-config.json", Path.cwd() / "metadata" / "campaign-config.json"]
    for candidate in candidates:
        if candidate.is_file():
            shutil.copyfile(candidate, out / "campaign-config.json")
            return "campaign-config.json"
    targets = sorted({(record["rtos"], record["board"], record["language"]) for record in records})
    _write_json(
        out / "campaign-config.json",
        {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "requested_cases": requested_cases,
            "matched_combinations": [
                {"rtos": rtos, "board": board, "language": language}
                for rtos, board, language in targets
            ],
            "note": "generated from recorded units; supply metadata/campaign-config.json for full inputs",
        },
    )
    return "campaign-config.json"


def _input_manifest(out: Path) -> str:
    paths = [
        path for path in (out / "benchmarks").rglob("*")
        if path.is_file()
    ]
    config = out / "campaign-config.json"
    if config.is_file():
        paths.append(config)
    lines = [
        f"{_sha256_file(path)}  {path.relative_to(out).as_posix()}"
        for path in sorted(paths, key=lambda item: item.relative_to(out).as_posix())
    ]
    body = "\n".join(lines) + "\n"
    manifest = out / "provenance" / "input-manifest.sha256"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(body, encoding="utf-8")
    return _sha256_bytes(body.encode())


def _targets(records: list, missing: list, failures: list) -> list[dict]:
    targets = {(record["rtos"], record["board"], record["language"]) for record in records}
    for key in missing:
        parts = key.split(" run=")[0].split("/")
        if len(parts) == 3:
            targets.add(tuple(parts))
    for key, _reason, _attempts in failures:
        parts = key.split(" run=")[0].split("/")
        if len(parts) == 3:
            targets.add(tuple(parts))
    return [
        {"rtos": rtos, "board": board, "language": language}
        for rtos, board, language in sorted(targets)
    ]


SCHEMA_PATH = PROJECT / "benchmarks" / "campaign-schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _type_matches(value, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']}, got {value!r}")
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, item) for item in types):
            errors.append(f"{path}: expected type {expected}, got {type(value).__name__}")
            return errors
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
        if schema.get("format") == "date-time" and not re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", value
        ):
            errors.append(f"{path}: expected UTC date-time, got {value!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: needs at least {schema['minLength']} characters")
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required field {key!r}")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                errors.extend(_schema_errors(value[key], child, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def validate_campaign_payload(payload: dict, schema: dict | None = None) -> None:
    """Validate a campaign record against the checked-in schema."""
    errors = _schema_errors(payload, schema or _load_schema())
    if errors:
        raise ValueError("campaign record failed validation:\n" + "\n".join(errors))


def _validate_payload(payload: dict) -> None:
    if payload.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        raise ValueError("unexpected campaign schema version")
    validate_campaign_payload(payload)


def _preserve_failure_logs(out: Path, failures: list) -> None:
    """Copy the last live log for every failed unit into the bundle."""
    if not failures:
        return
    destination = out / "provenance" / "failures"
    for key, _reason, _attempts in failures:
        parts = key.split(" run=")[0].split("/")
        if len(parts) != 3:
            continue
        source = LIVE_DIR / f"{parts[0]}-{parts[1]}-{parts[2]}.log"
        if not source.is_file():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{parts[0]}-{parts[1]}-{parts[2]}.log"
        shutil.copyfile(source, target)


def write_bundle(runs: int, records: list, missing: list, failures: list,
                 complete: bool, requested_cases: list[int] | None = None,
                 out: Path | None = None, consolidate: bool = True) -> Path:
    complete = bool(complete and not missing and not failures)
    sample_issues = [
        {
            "unit": f"{record['rtos']}/{record['board']}/{record['language']} run={record['run']}",
            "build_id": record.get("build_id"),
            **issue,
        }
        for record in records
        for issue in record.get("sample_issues", [])
    ]
    scientific_status = "invalid_samples" if sample_issues else "valid"
    publication_ready = bool(complete and not sample_issues)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if out is None:
        out = RESULTS / f"campaign-replication-{stamp}"
        try:
            out.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise SystemExit(f"refusing to overwrite completed campaign: {out}") from exc
    elif not out.is_dir():
        raise SystemExit(f"campaign bundle does not exist: {out}")
    if complete and consolidate:
        consolidate_bundle(out, records)

    provenance = out / "provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    repository = _git_repository_metadata(out)
    environment = _toolchain_metadata(out)
    devices_file = _device_metadata(out)
    preflight_file = _preflight_metadata(out, records)
    config_file = _campaign_config(out, records, requested_cases)
    _preserve_failure_logs(out, failures)
    if LOG.is_file():
        shutil.copyfile(LOG, out / "provenance" / "campaign.log")
    manifest_sha = _input_manifest(out)

    for record in records:
        record["validation_status"] = "recorded" if record.get("result") else "missing"
    payload = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": out.name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs_per_combo": runs,
        "units_recorded": len(records),
        "units_missing": len(missing),
        "units_failed": len(failures),
        "complete": complete,
        "repository": repository,
        "requested_cases": requested_cases,
        "software": {
            "requested_cases": requested_cases,
            "suite_sha256": _sha256_paths(
                [PROJECT / "benchmarks" / "suite.toml", PROJECT / "benchmarks" / "specifications.md"],
                PROJECT,
            ),
            "config_sha256": _sha256_paths(
                sorted((PROJECT / "config").glob("*.toml")) + [PROJECT / "benchmarks" / "suite.toml"],
                PROJECT,
            ),
            "config_file": config_file,
            "runs_per_combo": runs,
        },
        "environment": {
            **environment,
            "devices_file": devices_file,
            "preflight_file": preflight_file,
            "input_manifest": manifest_sha,
        },
        "targets": _targets(records, missing, failures),
        "units": records,
        "missing": missing,
        "failures": [
            {"unit": key, "reason": reason, "attempts": attempts}
            for key, reason, attempts in failures
        ],
        "validation": {
            "status": "complete" if complete else "incomplete",
            "units_recorded": len(records),
            "units_missing": len(missing),
            "units_failed": len(failures),
            "scientific_status": scientific_status,
            "publication_ready": publication_ready,
            "invalid_samples": len(sample_issues),
            "sample_issues": sample_issues,
        },
    }
    _validate_payload(payload)
    (out / "campaign.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _write_json(
        out / "VALIDATION.json",
        {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": out.name,
            "status": payload["validation"]["status"],
            "units_recorded": len(records),
            "units_missing": len(missing),
            "units_failed": len(failures),
            "missing": missing,
            "failures": payload["failures"],
            "input_manifest": manifest_sha,
            "scientific_status": scientific_status,
            "publication_ready": publication_ready,
            "invalid_samples": len(sample_issues),
            "sample_issues": sample_issues,
        },
    )
    lines = [
        f"# Replication campaign {stamp}",
        "",
        f"- independent runs per combination: {runs}",
        f"- units recorded: {len(records)}",
        f"- units missing: {len(missing)}",
        f"- units failed this session: {len(failures)}",
        f"- finished: {'yes' if complete else 'NO (incomplete)'}",
        f"- scientific status: {scientific_status}",
        f"- publication ready: {'yes' if publication_ready else 'no'}",
        f"- case-specific invalid samples: {len(sample_issues)}",
        f"- requested cases: {requested_cases if requested_cases is not None else 'full advertised suite'}",
        f"- git revision: {repository['revision']}",
        f"- dirty worktree: {'yes' if repository['dirty'] else 'no'}",
        f"- input manifest: `provenance/input-manifest.sha256` ({manifest_sha})",
        "",
        "| RTOS | Board | Lang | Run | Result |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in records:
        lines.append(
            f"| {r['rtos']} | {r['board']} | {r['language']} | {r['run']} | "
            f"`{r['result'] or '(unresolved)'}` |"
        )
    if sample_issues:
        lines += ["", "## Invalid measurements", ""]
        lines += [
            f"- {issue['unit']}: case {issue['case']} sample {issue['index']} "
            f"ticks={issue['ticks']} ({issue['reason']})"
            for issue in sample_issues
        ]
    if missing:
        lines += ["", "## Missing units", ""]
        lines += [f"- {m}" for m in missing]
    (out / "campaign.md").write_text("\n".join(lines) + "\n")
    (out / ("COMPLETE" if complete else "INCOMPLETE")).write_text(
        "all units recorded\n" if complete else
        f"{len(missing)} missing, {len(failures)} failed this session\n"
    )
    return out

def clear_resume_markers(combos: list[tuple[str, str, str]]) -> int:
    """Remove resume markers for exactly the selected combinations."""
    removed = 0
    for rtos, board, language in combos:
        for path in DONE.glob(f"{rtos}-{board}-{language}-run*"):
            if path.is_file():
                path.unlink()
                removed += 1
        live = LIVE_DIR / f"{rtos}-{board}-{language}.log"
        if live.is_file():
            live.unlink()
    return removed


def select_combinations(combos: list[tuple[str, str, str]], only: str | None = None,
                        combinations: list[str] | None = None) -> list[tuple[str, str, str]]:
    selected = combos
    if only:
        selected = [combo for combo in selected if only in combo[0] or only in combo[1]]
    if combinations:
        wanted = set(combinations)
        selected = [combo for combo in selected if f"{combo[0]}/{combo[1]}/{combo[2]}" in wanted]
        if not selected:
            raise SystemExit(f"no campaign combinations match {sorted(wanted)}")
    return selected


def campaign_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=int, default=1,
        help="independent restarted runs per combination; the per-case sample "
             "budget is split evenly across them (default 1)",
    )
    parser.add_argument(
        "--repetitions", type=int, default=None,
        help="total measured samples per case, split across --runs "
             "(default: 1000 per board, 100 on nRF)",
    )
    parser.add_argument(
        "--warmup", type=int, default=DEFAULT_WARMUP,
        help="total warm-up samples per case, split across --runs (default 100)",
    )
    parser.add_argument(
        "--cases", default=None,
        help="comma-separated protocol case IDs; defaults to the advertised suite",
    )
    parser.add_argument(
        "--only", default=None,
        help="restrict to combinations whose RTOS or board contains this substring",
    )
    parser.add_argument(
        "--combination", action="append", default=[],
        metavar="RTOS/BOARD/LANGUAGE",
        help="restrict to an exact combination (repeatable); useful for a minimal validation run",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list the planned (combination, run) units and exit without running",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="ignore/remove resume markers for the selected combinations before starting",
    )
    args = parser.parse_args(argv)

    combos = select_combinations(COMBOS, args.only, args.combination)
    cases = parse_cases(args.cases)

    runs = max(1, args.runs)
    if not RTOS.is_file():
        log(f"FATAL rtos binary missing: {RTOS}")
        return 2

    # Resolve every console before clearing resume markers or touching any
    # board. A missing or ambiguous path must stop the campaign, not select a
    # different physical device.
    if not args.dry_run:
        try:
            seen_boards = set()
            for _rtos, board, _language in combos:
                if board not in seen_boards:
                    seen_boards.add(board)
                    log(f"console {board}: {port_for(_rtos, board)}")
        except ValueError as error:
            log(f"FATAL console routing: {error}")
            return 2

    if args.fresh:
        if args.dry_run:
            log("fresh campaign: dry-run, resume markers left unchanged")
        else:
            removed = clear_resume_markers(combos)
            log(f"fresh campaign: cleared {removed} resume marker(s) for selected combinations")

    # Build the list of pending (combination, run) units. Each is an independent
    # restart with its share of the sample budget; a unit with an existing done
    # marker is skipped, so stopping and restarting the process resumes exactly
    # where it left off.
    units = []
    skipped = 0
    for rtos, board, language in combos:
        budget = args.repetitions if args.repetitions is not None else reps_for(board)
        per_run_reps = max(1, budget // runs)
        per_run_warmup = max(0, args.warmup // runs)
        for k in range(runs):
            if (DONE / f"{rtos}-{board}-{language}-run{k}").exists():
                skipped += 1
                continue
            units.append((rtos, board, language, k, per_run_reps, per_run_warmup))

    total = len(combos) * runs
    log(f"=== campaign start: {total} units ({len(combos)} combos x {runs} runs), "
        f"pending={len(units)} skip={skipped} retries={RETRIES} ===")

    if args.dry_run:
        for rtos, board, language, k, reps, warmup in units:
            log(f"PLAN {rtos}/{board}/{language} run={k} reps={reps} "
                f"warmup={warmup} cases={cases if cases is not None else 'advertised-suite'}"
                f"{' reuse-build' if k else ''}")
        return 0

    # Group pending units by board: each board runs its units sequentially;
    # different boards run in parallel. Same-RTOS units on different boards share
    # build state (clean/build dirs), so a per-RTOS lock serializes them.
    by_board = {}
    for unit in units:
        by_board.setdefault(unit[1], []).append(unit)

    # Stagger each board's RTOS order so concurrent boards are usually working on
    # DIFFERENT RTOSes. The per-RTOS lock now guards only BUILDS (shared source
    # tree / shared build dirs); `--reuse-build` runs are unlocked. Still,
    # starting boards on distinct RTOSes avoids two boards trying to build the
    # same RTOS at once. COMBOS lists each RTOS's units together, so without this
    # every board would begin with a build on the same RTOS. Rotating each
    # board's RTOS group by its index keeps the within-RTOS run order but spreads
    # the RTOSes across boards.
    board_names = sorted(by_board)
    used_starts: set[str] = set()
    for i, board in enumerate(board_names):
        bus = by_board[board]
        order = []
        for u in bus:
            if u[0] not in order:
                order.append(u[0])
        if len(order) > 1:
            # Prefer a starting RTOS no other board has claimed, so the boards
            # begin on distinct RTOSes (own different build locks) and run fully
            # in parallel; fall back to rotation if every RTOS is taken.
            start = next((r for r in order if r not in used_starts), None)
            if start is None:
                start = order[i % len(order)]
            used_starts.add(start)
            k = order.index(start)
            rank = {r: (idx - k) % len(order) for idx, r in enumerate(order)}
            bus.sort(key=lambda u: rank[u[0]])
            log(f"stagger {board}: {' > '.join(order[k:] + order[:k])}")

    rtos_locks = {}
    locks_guard = threading.Lock()

    def lock_for(rtos: str) -> threading.Lock:
        with locks_guard:
            return rtos_locks.setdefault(rtos, threading.Lock())

    stats = {"passes": 0, "fails": 0}
    stats_lock = threading.Lock()
    failures = []
    failures_lock = threading.Lock()

    def run_unit(rtos: str, board: str, language: str, run_index: int,
                 reps: int, warmup: int, build_lock=None):
        key = f"{rtos}/{board}/{language}"
        ok = False
        reason = "unexpected failure"
        attempts = 0
        for attempt in range(1, RETRIES + 1):
            attempts = attempt
            if attempt > 1:
                log(f"RETRY {key} run={run_index} attempt {attempt}/{RETRIES}")
                time.sleep(RETRY_DELAY)
            ok, reason = run(rtos, board, language, run_index, reps, warmup,
                              build_lock, cases)
            if ok:
                break
        return ok, reason, attempts

    def run_board(board: str, board_units) -> None:
        for rtos, b, language, k, reps, warmup in board_units:
            before = result_snapshot()
            ok, reason, attempts = run_unit(rtos, b, language, k, reps, warmup,
                                            lock_for(rtos))
            result_rel, result_data = (
                unit_result(before, rtos, b, language) if ok else (None, None)
            )
            if ok and not result_is_valid(result_data, rtos, b, language):
                # Never recover an older campaign's file: a fresh run that did
                # not leave a valid result is a failed unit.
                ok = False
                reason = "benchmark exited 0 but produced no valid new result file"
                log(f"!!! FAILED {rtos}/{b}/{language} run={k}: {reason}")
            case_error = case_selection_error(result_data, cases, warmup) if ok else None
            if ok and case_error:
                ok = False
                reason = f"result failed case-selection validation: {case_error}"
                log(f"!!! FAILED {rtos}/{b}/{language} run={k}: {reason}")
            if ok:
                # Store the produced result path in the marker so a finished
                # bundle can be reconstructed from any later invocation.
                (DONE / f"{rtos}-{b}-{language}-run{k}").write_text(
                    (result_rel or "ok") + "\n"
                )
                with stats_lock:
                    stats["passes"] += 1
            else:
                with stats_lock:
                    stats["fails"] += 1
                with failures_lock:
                    failures.append((f"{rtos}/{b}/{language} run={k}", reason, attempts))
                log(f"!!! FAILED {rtos}/{b}/{language} run={k} "
                    f"after {attempts} attempt(s): {reason}")

    log(f"parallel across {len(by_board)} boards: "
        f"{', '.join(sorted(by_board))}")
    with ThreadPoolExecutor(max_workers=max(1, len(by_board))) as ex:
        futures = [
            ex.submit(run_board, board, board_units)
            for board, board_units in by_board.items()
        ]
        for fut in as_completed(futures):
            fut.result()  # propagate any worker exception

    passes = stats["passes"]
    fails = stats["fails"]
    records, missing = collect_records(
        combos, runs, cases, max(0, args.warmup // runs) if cases else None
    )
    complete = not missing
    bundle = write_bundle(runs, records, missing, failures, complete, cases)
    log(f"=== campaign end: pass={passes} fail={fails} skip={skipped} "
        f"recorded={len(records)} missing={len(missing)} -> {bundle} ===")
    if failures:
        log("=== failed units ===")
        for key, reason, attempts in sorted(failures):
            log(f"  {key} ({attempts} attempt(s)): {reason}")
    return 0 if complete else 1


STOP = False


def _handle_stop(signum, _frame) -> None:
    global STOP
    STOP = True
    log(f"append: signal {signum}; finishing the in-flight unit(s), then stopping")


def next_run_index(rtos: str, board: str, language: str) -> int:
    """Next free run index for a combination, from its ``-run<k>`` markers."""
    prefix = f"{rtos}-{board}-{language}-run"
    indices = []
    for path in DONE.glob(prefix + "*"):
        suffix = path.name[len(prefix):]
        if suffix.isdigit():
            indices.append(int(suffix))
    return max(indices) + 1 if indices else 0


class State:
    """Shared per-run configuration, counters and the per-RTOS build lock."""

    def __init__(self, reps: int, warmup: int) -> None:
        self.reps = reps
        self.warmup = warmup
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.failures: list[tuple[str, str, int]] = []
        self._fail_lock = threading.Lock()
        self.appended = 0
        self._stats_lock = threading.Lock()

    def rtos_lock(self, rtos: str) -> threading.Lock:
        """Per-RTOS lock: same-RTOS units on different boards share build state."""
        with self._locks_guard:
            return self._locks.setdefault(rtos, threading.Lock())

    def record(self, unit: str, info: str) -> None:
        with self._fail_lock:
            self.failures.append((unit, info, RETRIES))

    def note_appended(self) -> None:
        with self._stats_lock:
            self.appended += 1


def run_unit(rtos: str, board: str, language: str, run_index: int,
             state: State) -> tuple[bool, str | None]:
    """Run one benchmark with retries under the RTOS build lock; return (ok, info)."""
    key = f"{rtos}/{board}/{language}"
    reason = "unexpected failure"
    with state.rtos_lock(rtos):
        for attempt in range(1, RETRIES + 1):
            if attempt > 1:
                log(f"RETRY {key} run={run_index} attempt {attempt}/{RETRIES}")
                time.sleep(RETRY_DELAY)
            before = result_snapshot()
            ok, reason = run(rtos, board, language, run_index,
                                state.reps, state.warmup)
            if ok:
                rel, data = unit_result(before, rtos, board, language)
                if rel is not None and result_is_valid(
                        data, rtos, board, language):
                    return True, rel
                reason = "benchmark exited 0 but produced no valid new result file"
                log(f"!!! FAILED {rtos}/{board}/{language} run={run_index}: {reason}")
    return False, reason


def run_board(board: str, board_combos, state: State) -> None:
    """Run one new run for every combination on this board, sequentially."""
    for rtos, _b, language in board_combos:
        if STOP:
            return
        k = next_run_index(rtos, board, language)
        ok, info = run_unit(rtos, board, language, k, state)
        if ok:
            (DONE / f"{rtos}-{board}-{language}-run{k}").write_text(
                (info or "ok") + "\n"
            )
            log(f"APPENDED {rtos}/{board}/{language} run={k} -> {info}")
            state.note_appended()
        else:
            log(f"!!! APPEND FAILED {rtos}/{board}/{language} run={k}: {info}")
            state.record(f"{rtos}/{board}/{language} run={k}", str(info))


def group_by_board(combos):
    by_board: dict[str, list] = {}
    for combo in combos:
        by_board.setdefault(combo[1], []).append(combo)
    return by_board


def run_pass(combos, state: State, serial: bool) -> tuple[int, int]:
    """One pass: every combination gets one new run; barrier before returning."""
    by_board = group_by_board(combos)
    before_fail = len(state.failures)
    before_ok = state.appended
    if serial or len(by_board) <= 1:
        for board, board_combos in by_board.items():
            run_board(board, board_combos, state)
    else:
        log(f"parallel across {len(by_board)} boards: "
               f"{', '.join(sorted(by_board))}")
        with ThreadPoolExecutor(max_workers=len(by_board)) as ex:
            futures = [ex.submit(run_board, b, cs, state)
                       for b, cs in by_board.items()]
            for fut in futures:
                fut.result()  # barrier: wait for every board
    return state.appended - before_ok, len(state.failures) - before_fail


def append_write_bundle(combos, state: State) -> None:
    """Write one complete campaign bundle at the common (uniform) run count."""
    counts = [next_run_index(*combo) for combo in combos]
    runs = min(counts) if counts else 0
    if runs < 1:
        raise SystemExit("append: no complete run is available for every combination")
    records, missing = collect_records(combos, runs)
    complete = not missing and not state.failures
    out = write_bundle(runs, records, missing, state.failures, complete)
    log(f"append: bundle runs={runs} recorded={len(records)} "
        f"missing={len(missing)} failed={len(state.failures)} -> {out}")


def append_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repetitions", type=int, default=100,
        help="measured samples per case per run (default 100)",
    )
    parser.add_argument(
        "--warmup", type=int, default=10,
        help="discarded warm-up samples per case per run (default 10)",
    )
    parser.add_argument(
        "--only", default=None,
        help="restrict to combinations whose RTOS or board contains this substring",
    )
    parser.add_argument(
        "--combination", action="append", default=[],
        metavar="RTOS/BOARD/LANGUAGE",
        help="restrict to an exact combination (repeatable); useful for a minimal validation run",
    )
    parser.add_argument(
        "--serial", action="store_true",
        help="run one combination at a time (no per-board parallelism)",
    )
    parser.add_argument(
        "--max-passes", type=int, default=0,
        help="stop after this many passes (default 0 = run until interrupted)",
    )
    parser.add_argument(
        "--no-bundle", action="store_true",
        help="skip writing a campaign bundle when the loop stops",
    )
    args = parser.parse_args(argv)

    RUN_TIMEOUT = float(os.environ.get("CAMPAIGN_RUN_TIMEOUT", RUN_TIMEOUT))

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    combos = COMBOS
    if args.only:
        combos = [c for c in combos if args.only in c[0] or args.only in c[1]]

    if not RTOS.is_file():
        log(f"FATAL rtos binary missing: {RTOS}")
        return 2

    state = State(args.repetitions, args.warmup)
    mode = "serial" if args.serial else "per-board parallel"
    log(f"=== append start: {len(combos)} combos, {mode}, "
           f"reps={args.repetitions} warmup={args.warmup} "
           f"timeout={RUN_TIMEOUT:.0f}s ===")

    passes = 0
    while not STOP:
        passes += 1
        log(f"=== append pass {passes} start ===")
        ok, failed = run_pass(combos, state, args.serial)
        log(f"=== append pass {passes} end: appended={ok} failed={failed} ===")
        if args.max_passes and passes >= args.max_passes:
            break

    log(f"=== append stopped after {passes} pass(es) ===")
    if not args.no_bundle:
        try:
            append_write_bundle(combos, state)
        except Exception as exc:  # bundle is a convenience; never mask the stop
            log(f"append: bundle write failed: {exc}")
    return 0


def configure_campaign(work_dir) -> None:
    """Point the monitor at a supervisor work directory.

    The external layout is ``<work-dir>/campaign.log``,
    ``<work-dir>/state/{done,live}`` and optionally
    ``<work-dir>/metadata/campaign-config.json``. The config's
    ``matched_combinations`` replaces the repository-wide COMBOS list.
    """
    global DONE, LOG, LIVE_DIR, COMBOS
    if work_dir is None:
        DONE = PROJECT / ".campaign-done"
        LOG = PROJECT / "campaign.log"
        LIVE_DIR = PROJECT / ".campaign-live"
        return
    work = Path(work_dir).expanduser().resolve()
    if not work.is_dir():
        raise SystemExit(f"campaign directory not found: {work}")
    DONE = work / "state/done"
    LOG = work / "campaign.log"
    LIVE_DIR = work / "state/live"
    config = work / "metadata/campaign-config.json"
    if not config.is_file():
        return
    payload = json.loads(config.read_text())
    rows = payload.get("matched_combinations")
    if not rows:
        return
    combos = []
    for row in rows:
        if isinstance(row, dict):
            combo = (row.get("rtos"), row.get("board"), row.get("language"))
        else:
            combo = tuple(row)
        if len(combo) != 3 or not all(combo):
            raise SystemExit(f"invalid matched combination in {config}: {row!r}")
        combos.append(combo)
    COMBOS = combos


RTOS_NAMES = {
    "freertos": "FreeRTOS",
    "freertos-in-rust": "FreeRTOS-in-Rust",
    "rivet": "Rivet",
    "zephyr": "Zephyr",
    "nuttx": "NuttX",
}

BOARD_NAMES = {
    "stm32f4discovery": "STM32F4 Discovery",
    "nrf52840dk": "nRF52840 DK",
    "esp32c6": "ESP32-C6",
    "stm32h723": "STM32H723",
}

CASE_NAMES = {
    0: "timer overhead", 1: "queue roundtrip", 2: "task handoff",
    3: "periodic jitter", 4: "ISR-to-task", 5: "yield roundtrip",
    6: "preempt latency", 7: "IPC ping-pong", 8: "mutex priority",
    9: "timer callback", 10: "trigger-to-ISR", 11: "jitter (loaded)",
    12: "semaphore", 13: "mutex uncontended", 14: "alloc latency",
    15: "sleep accuracy", 16: "scheduler overhead", 17: "notification",
    18: "IPC payload sweep", 19: "IPC throughput", 20: "nested IRQ",
    21: "tick ISR", 22: "idle CPU", 23: "CPU under load",
    24: "heap fragmentation", 25: "max int disabled",
    26: "prio scheduling", 27: "zero-copy IPC", 28: "arena fragmentation",
}


def marker_name(rtos: str, board: str, lang: str) -> str:
    return f"{rtos}-{board}-{lang}"


def running_combos():
    """Return a list of (rtos, board, lang) for live benchmarks."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", r"target/debug/rtos benchmark"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    combos = []
    for line in out.splitlines():
        m = re.search(r"rtos benchmark (\S+) --board (\S+) --language (\S+)", line)
        if m:
            combo = (m.group(1), m.group(2), m.group(3))
            if combo not in combos:
                combos.append(combo)
    return combos


def _log_lines():
    if not LOG.is_file():
        return []
    return LOG.read_text().splitlines()


def _find_last(lines, pattern):
    """Return the regex match of the last line matching `pattern`, or None."""
    for line in reversed(lines):
        m = re.search(pattern, line)
        if m:
            return m
    return None


def last_event() -> dict:
    events = {}
    for line in _log_lines():
        m = re.search(r" (PASS|FAIL) (\S+/\S+/\S+)", line)
        if m:
            events[m.group(2)] = m.group(1)
    return events


def failure_reasons() -> dict:
    reasons = {}
    for line in _log_lines():
        m = re.search(r"!!! FAILED (\S+/\S+/\S+) after \d+ attempt\(s\): (.*)", line)
        if m:
            reasons[m.group(1)] = m.group(2)
    return reasons


def campaign_runs() -> int:
    """Expected runs per combination, from the latest ``campaign start`` line.

    ``campaign.py run`` records each finished unit as a ``-run{k}`` marker and
    no longer writes a bare combination marker, so completion has to be derived
    from the run markers. Return 0 when no start line can be found.
    """
    for line in reversed(_log_lines()):
        m = re.search(r"campaign start:.*\((\d+) combos x (\d+) runs\)", line)
        if m:
            return int(m.group(2))
    return 0


def combo_done(key: str, markers: set, runs: int) -> bool:
    """True when a combination's units are all marked done.

    Legacy single-run campaigns wrote a bare combination marker; the current
    replication campaign marks each run and expects ``runs`` of them. Accept
    either so old and new marker sets both read as done.
    """
    if key in markers:
        return True
    return runs > 0 and all(f"{key}-run{k}" in markers for k in range(runs))


def _bar(frac: float, width: int = 30) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(width * frac))
    return "█" * filled + "░" * (width - filled)


def combo_progress(rtos: str, board: str, lang: str):
    """Parse a combo's live log into its run progress.

    Returns ``None`` when the combo has no live log, ``{"phase": "setup"}``
    before the case list is known, or a dict with ``run_frac`` and the current
    task (case) identity once the run is streaming.
    """
    path = LIVE_DIR / f"{rtos}-{board}-{lang}.log"
    if not path.is_file():
        return None
    lines = path.read_text().splitlines()
    start = _find_last(lines, r"sending start cases=\[([^\]]*)\] repetitions=(\d+)")
    live = _find_last(lines, r"live progress: (\d+) samples \(case=(\d+) sample=(\d+)\)")
    if not (start and live):
        return {"phase": "setup"}
    cases = [int(x.strip()) for x in start.group(1).split(",") if x.strip()]
    reps = int(start.group(2))
    total_samples = len(cases) * reps
    samples = int(live.group(1))
    case_id = int(live.group(2))
    sample = int(live.group(3))
    case_frac = (sample + 1) / reps if reps else 0.0
    run_frac = samples / total_samples if total_samples else 0.0
    case_index = cases.index(case_id) + 1 if case_id in cases else 0
    return {
        "phase": "run",
        "case_id": case_id,
        "case_name": CASE_NAMES.get(case_id, "?"),
        "case_index": case_index,
        "case_total": len(cases),
        "case_frac": case_frac,
        "run_frac": run_frac,
    }


def current_driver():
    """Which driver's start line is most recent: 'append', 'campaign' or None."""
    last = None
    for line in _log_lines():
        if "append start:" in line:
            last = "append"
        elif "campaign start:" in line:
            last = "campaign"
    return last


def append_pid():
    """PID of the running replication-append driver, or None."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", "campaign.py append"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "campaign.py append" in line and "pgrep" not in line:
            first = line.split(None, 1)[0]
            if first.isdigit():
                return int(first)
    return None


def append_header():
    """(ncombos, mode, pass_no) from the most recent append start line."""
    ncombos = mode = pass_no = None
    for line in _log_lines():
        m = re.search(r"append start: (\d+) combos, (serial|per-board parallel)", line)
        if m:
            ncombos, mode = int(m.group(1)), m.group(2)
        m = re.search(r"append pass (\d+) start", line)
        if m:
            pass_no = int(m.group(1))
    return ncombos, mode, pass_no


def combo_run_counts():
    """Map (rtos, board, lang) -> recorded runs, for the campaign combinations."""
    counts = {}
    for rtos, board, lang in COMBOS:
        key = f"{rtos}-{board}-{lang}"
        best = 0
        for path in DONE.glob(key + "-run*"):
            suffix = path.name[len(key) + len("-run"):]
            if suffix.isdigit():
                best = max(best, int(suffix) + 1)
        counts[(rtos, board, lang)] = best
    return counts


def last_started_per_board():
    """Most recent 'START rtos/board/lang run=k' per board, since the last
    append-session start, from campaign.log."""
    lines = _log_lines()
    start = 0
    for i, line in enumerate(lines):
        if "append start:" in line:
            start = i
    latest = {}
    for line in lines[start:]:
        m = re.search(r"START (\S+)/(\S+)/(\S+) run=(\d+)", line)
        if m:
            latest[m.group(2)] = (m.group(1), m.group(3), int(m.group(4)))
    return latest


def render_append() -> int:
    """Live view of the serial/per-board replication-append driver."""
    pid = append_pid()
    ncombos, mode, pass_no = append_header()
    counts = combo_run_counts()
    values = list(counts.values())
    with_markers = sum(1 for v in values if v > 0)
    mn = min(values) if values else 0
    mx = max(values) if values else 0

    head = f"Replication append  [{'RUNNING' if pid else 'stopped'}]"
    if pid:
        head += f"  pid {pid}"
    if mode:
        head += f"  mode={mode}"
    if ncombos:
        head += f"  combos={ncombos}"
    if pass_no:
        head += f"  pass {pass_no}"
    print(head)
    print(f"  combos with run markers: {with_markers}/{len(COMBOS)}   "
          f"runs per combo: min={mn} max={mx}")

    live = {board: (rtos, board, lang) for rtos, board, lang in running_combos()}
    latest = last_started_per_board()

    boards = []
    for rtos, board, lang in COMBOS:
        if board not in boards:
            boards.append(board)

    print("\n  boards:")
    for board in boards:
        name = BOARD_NAMES.get(board, board)
        if board in live:
            rtos, _b, lang = live[board]
            label = RTOS_NAMES.get(rtos, rtos)
            runs = counts.get((rtos, board, lang), 0)
            print(f"    - {name}: {label} / {lang}  (running, {runs} runs recorded)")
            prog = combo_progress(rtos, board, lang)
            if prog and prog.get("phase") == "run":
                print(f"        run [{_bar(prog['run_frac'])}] "
                      f"{prog['run_frac'] * 100:.0f}%  "
                      f"task {prog['case_index']}/{prog['case_total']} "
                      f"(case {prog['case_id']}: {prog['case_name']})")
        elif board in latest:
            rtos, lang, k = latest[board]
            label = RTOS_NAMES.get(rtos, rtos)
            print(f"    - {name}: {label} / {lang}  run={k}  (build/flash/idle gap)")
        else:
            print(f"    - {name}: idle")
    return 0


def _done_units(markers) -> int:
    return sum(1 for m in markers if re.search(r"-run\d+$", m))


def combo_run_index_map() -> dict:
    """Last ``START rtos/board/lang run=k`` index seen per combination."""
    latest = {}
    for line in _log_lines():
        m = re.search(r"START (\S+)/(\S+)/(\S+) run=(\d+)", line)
        if m:
            latest[(m.group(1), m.group(2), m.group(3))] = int(m.group(4))
    return latest


def render_once() -> int:
    if current_driver() == "append":
        return render_append()
    markers = ({p.name for p in DONE.iterdir() if p.is_file()}
               if DONE.is_dir() else set())
    runs = campaign_runs()
    running = set(running_combos())
    events = last_event()
    reasons = failure_reasons()
    run_idx = combo_run_index_map()

    n_done = n_run = n_pend = 0
    states = []
    for rtos, board, lang in COMBOS:
        key = marker_name(rtos, board, lang)
        if combo_done(key, markers, runs):
            state, n_done = "done", n_done + 1
        elif (rtos, board, lang) in running:
            state, n_run = "running", n_run + 1
        else:
            state, n_pend = "pending", n_pend + 1
        states.append((state, key, (rtos, board, lang)))

    total = len(COMBOS)
    progressed = n_done + n_run
    pct = 100.0 * progressed / total if total else 0.0
    done_units = _done_units(markers)
    total_units = len(COMBOS) * runs if runs else 0

    print(f"Benchmark campaign  [{_bar(progressed / total, 40)}]  "
          f"{n_done}/{total} combos ({pct:.0f}%)")
    if total_units:
        print(f"  tasks: {done_units}/{total_units} runs done   "
              f"combos: done={n_done} running={n_run} pending={n_pend}")
    else:
        print(f"  done={n_done}  running={n_run}  pending={n_pend}")

    # Per-board state, from each combo's own live log (parallel boards).
    if running:
        print(f"\n  running ({n_run}):")
        for rtos, board, lang in sorted(running):
            name = f"{RTOS_NAMES.get(rtos, rtos)} / {BOARD_NAMES.get(board, board)} / {lang}"
            k = run_idx.get((rtos, board, lang))
            run_label = f"run {k + 1}/{runs}" if (k is not None and runs) else "run ?"
            prog = combo_progress(rtos, board, lang)
            if prog is None:
                print(f"    - {name}  {run_label}  (queued)")
                continue
            if prog.get("phase") != "run":
                print(f"    - {name}  {run_label}  (build/flash/boot)")
            else:
                print(f"    - {name}  {run_label}  "
                      f"[{_bar(prog['run_frac'])}] {prog['run_frac'] * 100:.0f}%  "
                      f"task {prog['case_index']}/{prog['case_total']} "
                      f"(case {prog['case_id']}: {prog['case_name']})")
    elif n_pend == 0:
        print("\n  finished")

    failed = [
        (rtos, board, lang) for state, key, (rtos, board, lang) in states
        if state == "pending" and events.get(f"{rtos}/{board}/{lang}") == "FAIL"
    ]
    if failed:
        print("\n  failed (will retry):")
        for rtos, board, lang in failed:
            reason = reasons.get(f"{rtos}/{board}/{lang}", "")
            line = f"    - {rtos}/{board}/{lang}"
            if reason:
                line += f": {reason}"
            print(line)

    return 0


def status_main(argv: list[str] | None = None) -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Live benchmark campaign status.")
    parser.add_argument(
        "--watch", nargs="?", type=float, const=2.0, default=None,
        help="auto-refresh every N seconds (default 2); omit for one shot",
    )
    parser.add_argument(
        "--campaign-dir",
        help="supervisor work directory containing campaign.log, state/ and metadata/",
    )
    args = parser.parse_args(argv)
    configure_campaign(args.campaign_dir)

    if args.watch is None:
        return render_once()
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")  # clear screen, cursor home
            render_once()
            sys.stdout.flush()
            time.sleep(max(0.2, args.watch))
    except KeyboardInterrupt:
        return 0



def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "run":
        return campaign_main(rest)
    if command == "append":
        return append_main(rest)
    if command == "status":
        return status_main(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
