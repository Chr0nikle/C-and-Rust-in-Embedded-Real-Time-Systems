//! Workflow: resolve config -> validate -> build -> flash -> UART smoke.
//! Transfer serial ownership around flashing and keep partial results on every error.
use crate::{
    error::{Error, Result},
    plugin_interface::{Artifact, Board, BuildRequest, Device, Language, RtosPlugin},
    results,
    uart::{SerialTransport, Session},
};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const BOOT_TIMEOUT: Duration = Duration::from_secs(15);
const START_TIMEOUT: Duration = Duration::from_secs(10);
/// Fallback start-response budget for a full benchmark suite, used when the
/// configured `serial.run_timeout_ms` is zero. A 10-case run emits ~10 000
/// frames (~850 KB) and the Zephyr STM32F4 console is much slower than the
/// direct-register FreeRTOS path, so the default is generous.
const BENCH_TIMEOUT: Duration = Duration::from_secs(600);
const UART_BAUD: u32 = 115200;

/// Public workflow intent, kept independent of CLI argument types.
#[derive(Clone, Copy, Debug)]
pub enum Operation {
    /// Prepare, flash and verify the default smoke firmware.
    Flash,
    /// Build a firmware image without touching hardware.
    Build,
    /// Prepare, flash, start and collect the benchmark suite.
    Benchmark,
}

/// A plan is explanatory only: no SDK, connected-device, or implementation claims.
pub fn describe(
    plugin: &dyn RtosPlugin,
    board: Board,
    language: Language,
    operation: Operation,
) -> Result<String> {
    let route = plugin.route(board, language)?;
    Ok(format!("PLAN (not executable)\nRTOS: {}\nBackend: {}\nTarget: {}\nLanguage: {:?}\nWorkflow: {}\nNotes: {}\nRecipe: {}\n",
        plugin.id(), route.backend, route.target, language,
        match operation {
            Operation::Benchmark => "validate -> build -> flash -> UART ready -> start -> collect -> complete",
            Operation::Flash => "validate -> build smoke -> flash -> UART smoke handshake",
            Operation::Build => "validate -> clean stale build -> build",
        }, route.notes, route.recipe))
}

/// Resolved parameters for a workflow execution.
pub struct ExecutionRequest {
    /// Canonical project root.
    pub project: PathBuf,
    /// Selected physical target.
    pub board: Board,
    /// Selected application language.
    pub language: Language,
    /// The operation to perform.
    pub operation: Operation,
    /// Serial port device path (e.g. /dev/serial/by-id/...).
    pub port: Option<PathBuf>,
    /// Explicit run ID for the UART session.
    pub run_id: String,
    /// Explicit challenge token for the UART handshake.
    pub challenge: String,
    /// Benchmark case IDs; `None` uses the order advertised by the device.
    pub cases: Option<Vec<u32>>,
    /// Valid samples per benchmark case.
    pub repetitions: u32,
    /// Discarded warm-up iterations per benchmark case.
    pub warmup: u32,
    /// Reuse the artifact from the last successful build of this
    /// board/language/operation instead of rebuilding. Intended for repeated
    /// runs of one unchanged image; errors if no cached build exists.
    pub reuse_build: bool,
    /// Milliseconds allowed for the benchmark start/collection wait; `0` uses
    /// the built-in fallback. Sourced from `serial.run_timeout_ms`.
    pub serial_run_timeout_ms: u64,
}

/// Cached artifact record for `--reuse-build`, keyed by board/language/operation.
#[derive(serde::Deserialize, serde::Serialize)]
struct CachedArtifact {
    directory: PathBuf,
    build_id: String,
    images: Vec<PathBuf>,
    /// Git HEAD when the artifact was built; reuse is refused if it changed.
    head: String,
}

fn current_head(project: &Path) -> String {
    std::process::Command::new("git")
        .arg("-C")
        .arg(project)
        .args(["rev-parse", "--short=8", "HEAD"])
        .output()
        .ok()
        .filter(|out| out.status.success())
        .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_string())
        .unwrap_or_default()
}

fn artifact_cache_path(project: &Path, plugin_id: &str, req: &ExecutionRequest) -> PathBuf {
    project.join(".local").join("cache").join(format!(
        "artifact-{plugin_id}-{:?}-{:?}-{:?}.json",
        req.board, req.language, req.operation
    ))
}

fn store_cached_artifact(
    project: &Path,
    plugin_id: &str,
    req: &ExecutionRequest,
    artifact: &Artifact,
) -> Result<()> {
    let path = artifact_cache_path(project, plugin_id, req);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let cached = CachedArtifact {
        directory: artifact.directory.clone(),
        build_id: artifact.build_id.clone(),
        images: artifact.images.clone(),
        head: current_head(project),
    };
    let body = serde_json::to_vec_pretty(&cached)
        .map_err(|e| Error::Protocol(format!("encode artifact cache: {e}")))?;
    std::fs::write(&path, body)?;
    Ok(())
}

/// True when a cached build must not be reused because the tree moved on.
fn head_is_stale(cached_head: &str, current: &str) -> bool {
    !current.is_empty() && !cached_head.is_empty() && cached_head != current
}

fn load_cached_artifact(
    project: &Path,
    plugin_id: &str,
    req: &ExecutionRequest,
) -> Result<Artifact> {
    let path = artifact_cache_path(project, plugin_id, req);
    let text = std::fs::read_to_string(&path).map_err(|e| {
        Error::Protocol(format!(
            "no cached build at {} (run once without --reuse-build): {e}",
            path.display()
        ))
    })?;
    let cached: CachedArtifact = serde_json::from_str(&text)
        .map_err(|e| Error::Protocol(format!("corrupt artifact cache {}: {e}", path.display())))?;
    let head = current_head(project);
    if head_is_stale(&cached.head, &head) {
        return Err(Error::Protocol(format!(
            "cached build is for HEAD {} but the tree is at {}; rebuild without --reuse-build",
            cached.head, head
        )));
    }
    if !cached.directory.is_dir() {
        return Err(Error::Protocol(format!(
            "cached build directory {} is gone; rebuild without --reuse-build",
            cached.directory.display()
        )));
    }
    Ok(Artifact {
        directory: cached.directory,
        board: req.board,
        build_id: cached.build_id,
        images: cached.images,
    })
}

/// Build the device record from the required serial port.
fn device_from(req: &ExecutionRequest) -> Result<Device> {
    let port = req
        .port
        .as_ref()
        .ok_or_else(|| Error::Invalid("--port required for this operation".into()))?;
    Ok(Device {
        selector: port.to_string_lossy().to_string(),
        board: req.board,
    })
}

/// Static image footprint from the built ELF(s); `None` when none can be parsed.
///
/// Recorded per build so the C-versus-Rust code/data size comparison keeps its
/// provenance instead of being recomputed from a build directory that a later
/// language build has already overwritten.
fn static_footprint(artifact: &Artifact) -> Option<serde_json::Value> {
    let images: Vec<_> = artifact
        .images
        .iter()
        .filter_map(|path| match crate::elf::section_sizes(path) {
            Ok(f) => Some(serde_json::json!({
                "image": path.file_name().and_then(|n| n.to_str()).unwrap_or("image"),
                "text": f.text,
                "rodata": f.rodata,
                "data": f.data,
                "bss": f.bss,
                "flash": f.flash(),
                "ram": f.ram(),
            })),
            Err(e) => {
                eprintln!(
                    "[workflow] static footprint skipped for {}: {e}",
                    path.display()
                );
                None
            }
        })
        .collect();
    if images.is_empty() {
        None
    } else {
        Some(serde_json::json!({ "images": images }))
    }
}

/// Persist the static footprint next to the build for provenance.
fn write_footprint(artifact: &Artifact, project: &Path) -> Result<()> {
    let Some(footprint) = static_footprint(artifact) else {
        return Ok(());
    };
    let directory = results::campaigns_root(project).join(&artifact.build_id);
    std::fs::create_dir_all(&directory)?;
    let mut body = serde_json::to_vec_pretty(&serde_json::json!({
        "kind": "static_footprint",
        "build_id": artifact.build_id,
        "footprint": footprint,
    }))
    .map_err(|e| Error::Protocol(format!("encode footprint: {e}")))?;
    body.push(b'\n');
    std::fs::write(directory.join("static-footprint.json"), body)?;
    Ok(())
}

/// Execute a full workflow: validate -> build -> flash -> UART smoke.
pub fn execute(plugin: &dyn RtosPlugin, req: &ExecutionRequest) -> Result<()> {
    eprintln!(
        "[workflow] plugin={} board={:?} op={:?}",
        plugin.id(),
        req.board,
        req.operation
    );

    // 1. Validate
    let build_req = BuildRequest {
        project: req.project.clone(),
        board: req.board,
        language: req.language,
        workload: match req.operation {
            Operation::Benchmark => "benchmark".to_string(),
            _ => "smoke".to_string(),
        },
        output: req.project.join("firmware").join(plugin.id()),
        // Standalone builds are always pristine; flash keeps incremental builds.
        clean: matches!(req.operation, Operation::Build),
    };
    plugin.validate(&build_req)?;
    eprintln!("[workflow] validation passed");

    // 2. Build, or reuse the cached artifact for repeated runs of one image.
    let artifact = if req.reuse_build {
        if matches!(req.operation, Operation::Build) {
            return Err(Error::Invalid(
                "--reuse-build cannot be combined with build".into(),
            ));
        }
        let artifact = load_cached_artifact(&req.project, plugin.id(), req)?;
        eprintln!(
            "[workflow] reusing build: {} -> {}",
            artifact.build_id,
            artifact.directory.display()
        );
        artifact
    } else {
        let artifact = plugin.build(&build_req)?;
        store_cached_artifact(&req.project, plugin.id(), req, &artifact)?;
        eprintln!(
            "[workflow] build complete: {} -> {}",
            artifact.build_id,
            artifact.directory.display()
        );
        artifact
    };

    match req.operation {
        Operation::Build => {
            write_footprint(&artifact, &req.project)?;
            eprintln!("[workflow] build-only operation complete");
            Ok(())
        }
        Operation::Flash => {
            let device = device_from(req)?;
            plugin.flash(&artifact, &device)?;
            eprintln!("[workflow] flash complete");

            // 3. UART smoke handshake
            run_smoke_session(
                &device,
                &artifact,
                &req.project,
                &req.run_id,
                &req.challenge,
            )?;
            eprintln!("[workflow] smoke handshake complete");
            Ok(())
        }
        Operation::Benchmark => {
            let device = device_from(req)?;
            plugin.flash(&artifact, &device)?;
            eprintln!("[workflow] flash complete");
            run_benchmark_session(
                plugin.id(),
                &device,
                &artifact,
                &req.project,
                req.language,
                &req.run_id,
                &req.challenge,
                req.cases.as_deref(),
                req.repetitions,
                req.warmup,
                req.serial_run_timeout_ms,
            )?;
            eprintln!("[workflow] benchmark complete");
            Ok(())
        }
    }
}

/// Open the serial port, tolerating a brief USB re-enumeration gap after a
/// flash/reset. A native USB CDC console (e.g. the STM32H723 Zephyr target)
/// drops its by-id node for a moment while it re-enumerates, so retry the open
/// for a bounded window instead of failing on the first `ENOENT`.
fn open_with_reenumeration(port_path: &Path) -> Result<SerialTransport> {
    const REOPEN_WINDOW: Duration = Duration::from_secs(10);
    const RETRY_INTERVAL: Duration = Duration::from_millis(250);
    let deadline = std::time::Instant::now() + REOPEN_WINDOW;
    loop {
        match SerialTransport::open(port_path, UART_BAUD, Duration::from_millis(100)) {
            Ok(transport) => return Ok(transport),
            Err(e) => {
                if std::time::Instant::now() >= deadline {
                    return Err(e);
                }
                eprintln!("[uart] serial not ready ({e}); retrying");
                std::thread::sleep(RETRY_INTERVAL);
            }
        }
    }
}

/// Open the serial port, pump the boot window and complete the hello handshake.
fn handshake(
    device: &Device,
    artifact: &Artifact,
    run_id: &str,
    challenge: &str,
) -> Result<Session<SerialTransport>> {
    let port_path = PathBuf::from(&device.selector);
    eprintln!("[uart] opening {} at {UART_BAUD} baud", port_path.display());
    let transport = open_with_reenumeration(&port_path)?;
    let mut session = Session::new(
        transport,
        artifact.build_id.clone(),
        run_id.to_string(),
        challenge.to_string(),
    );

    eprintln!("[uart] boot pump ({}s)", BOOT_TIMEOUT.as_secs());
    session.pump_boot(BOOT_TIMEOUT)?;
    eprintln!("[uart] state after boot: {:?}", session.state());

    // If the device already sent a boot ready with the matching build_id, skip
    // the hello (it is already in AwaitStart).
    if session.state() == crate::uart::SessionState::AwaitStart {
        eprintln!("[uart] boot ready already acknowledged, skipping hello");
    } else {
        // Retry: the ESP32-C6 USB-Serial-JTAG bridge can drop the boot window
        // and answer late, so a single hello would miss a valid ready.
        for attempt in 1..=3 {
            eprintln!("[uart] sending hello (attempt {attempt})");
            match session.send_hello() {
                Ok(()) => {
                    eprintln!("[uart] hello acknowledged");
                    break;
                }
                Err(e) => eprintln!("[uart] hello failed: {e}"),
            }
        }
    }
    Ok(session)
}

/// Run the protocol v1 smoke handshake over UART.
///
/// Sequence:
///   1. boot pump — catch unsolicited `ready` after flash reset
///   2. `hello` — expect `ready` with challenge echoed
///   3. `start` — expect `error`/`unsupported` (smoke firmware)
///   4. `cancel` — best-effort, log silence
fn run_smoke_session(
    device: &Device,
    artifact: &Artifact,
    project: &Path,
    run_id: &str,
    challenge: &str,
) -> Result<()> {
    let mut session = handshake(device, artifact, run_id, challenge)?;

    // 3. Start (smoke firmware rejects with error/unsupported)
    eprintln!("[uart] sending start (smoke expects error/unsupported)");
    match session.send_start(vec![0, 1], 3, 1, START_TIMEOUT) {
        Ok(frames) => eprintln!("[uart] start response: {} frames", frames.len()),
        Err(e) => eprintln!("[uart] start response: {e} (expected for smoke firmware)"),
    }
    eprintln!("[uart] state after start: {:?}", session.state());

    // 4. Cancel (best-effort)
    eprintln!("[uart] sending cancel");
    match session.send_cancel() {
        Ok(()) => eprintln!("[uart] cancel acknowledged"),
        Err(e) => eprintln!("[uart] cancel failed: {e} (best-effort)"),
    }

    // Finalize
    let (events, raw, final_state) = session.finish();
    eprintln!(
        "[uart] session complete: state={:?} events={} raw_bytes={}",
        final_state,
        events.len(),
        raw.len()
    );

    // Print the event log.
    for event in &events {
        eprintln!("[uart:{}] {}", event.kind, event.data);
    }

    // Save the raw capture and event log under results/.
    let results_dir = results::campaigns_root(project);
    let run_dir = results_dir.join(&artifact.build_id);
    std::fs::create_dir_all(&run_dir)?;
    let raw_path = run_dir.join("uart_smoke.bin");
    let log_path = run_dir.join("uart_smoke.log");
    std::fs::write(&raw_path, &raw)?;
    let log_text: String = events
        .iter()
        .map(|e| format!("[{}] {}: {}", e.time, e.kind, e.data))
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&log_path, log_text + "\n")?;
    eprintln!(
        "[uart] saved raw={} log={}",
        raw_path.display(),
        log_path.display()
    );

    Ok(())
}

/// Run the benchmark suite and persist validated protocol evidence.
#[allow(clippy::too_many_arguments)]
fn run_benchmark_session(
    plugin_id: &str,
    device: &Device,
    artifact: &Artifact,
    project: &Path,
    language: Language,
    run_id: &str,
    challenge: &str,
    cases: Option<&[u32]>,
    repetitions: u32,
    warmup: u32,
    serial_run_timeout_ms: u64,
) -> Result<()> {
    let run_timeout = if serial_run_timeout_ms == 0 {
        BENCH_TIMEOUT
    } else {
        Duration::from_millis(serial_run_timeout_ms)
    };
    let mut session = handshake(device, artifact, run_id, challenge)?;
    if session.state() == crate::uart::SessionState::AwaitReady {
        return Err(Error::Protocol(
            "device did not report ready; not starting benchmark".into(),
        ));
    }
    let advertised = session.advertised_cases();
    let requested: Vec<u32> = match cases {
        Some(list) => list.to_vec(),
        None => advertised.to_vec(),
    };
    if requested.is_empty() {
        return Err(Error::Protocol(
            "device advertised no benchmark cases".into(),
        ));
    }
    for case in &requested {
        if !advertised.contains(case) {
            return Err(Error::Protocol(format!(
                "case {case} is not advertised by the device (advertised: {advertised:?})"
            )));
        }
    }

    eprintln!("[uart] sending start cases={requested:?} repetitions={repetitions} warmup={warmup}");
    let frames = match session.send_start(requested.clone(), repetitions, warmup, run_timeout) {
        Ok(frames) => {
            eprintln!("[uart] start response: {} frames", frames.len());
            frames
        }
        Err(e) => {
            let (events, raw, final_state) = session.finish();
            let run_dir = results::campaigns_root(project).join(&artifact.build_id);
            let raw_path = run_dir.join(format!("uart_benchmark-{run_id}-failed.bin"));
            let log_path = run_dir.join(format!("uart_benchmark-{run_id}-failed.log"));
            let log_text: String = events
                .iter()
                .map(|e| format!("[{}] {}: {}", e.time, e.kind, e.data))
                .collect::<Vec<_>>()
                .join("\n");
            // Best-effort capture: a persistence failure must not mask the
            // original benchmark error, which still propagates below.
            match std::fs::create_dir_all(&run_dir)
                .and_then(|_| std::fs::write(&raw_path, &raw))
                .and_then(|_| std::fs::write(&log_path, log_text + "\n"))
            {
                Ok(()) => eprintln!(
                    "[uart] start failed ({e}); state={final_state:?} raw={} log={}",
                    raw_path.display(),
                    log_path.display()
                ),
                Err(werr) => eprintln!(
                    "[uart] start failed ({e}); state={final_state:?} capture error: {werr}"
                ),
            }
            if device.board == crate::plugin_interface::Board::Esp32c6 {
                eprintln!(
                    "[uart] hint: the ESP32-C6 USB-Serial-JTAG bridge can wedge and drop a run; \
                     reset/power-cycle the device and retry"
                );
            }
            return Err(e);
        }
    };
    let report = session.validate_run(&frames, &requested, repetitions)?;
    eprintln!(
        "[uart] run validated: cases={:?} reps={} ticks_per_second={}",
        report.cases, report.repetitions, report.ticks_per_second
    );
    for case in &requested {
        let ticks = report.case_samples(*case);
        eprintln!(
            "[uart] case {case}: {} samples ticks={ticks:?}",
            ticks.len()
        );
    }
    if let Some(memory) = &report.memory {
        if let Some(stack) = memory.stack_high_water_bytes {
            eprintln!("[uart] stack_high_water_bytes={stack}");
        }
        if let Some(heap) = memory.heap_min_free_bytes {
            eprintln!("[uart] heap_min_free_bytes={heap}");
        }
        if let Some(heap) = memory.heap_free_bytes {
            eprintln!("[uart] heap_free_bytes={heap}");
        }
    }

    let (events, raw, final_state) = session.finish();
    if final_state != crate::uart::SessionState::Complete {
        return Err(Error::Protocol(format!(
            "benchmark run ended in state {final_state:?}"
        )));
    }

    let results_dir = results::ensure_run_dir(project, &artifact.build_id)?;
    let raw_path = results_dir.join(format!("uart_benchmark-{run_id}.bin"));
    let log_path = results_dir.join(format!("uart_benchmark-{run_id}.log"));
    let json_path = results_dir.join(format!("benchmark-{run_id}.json"));
    std::fs::write(&raw_path, &raw)?;
    let log_text: String = events
        .iter()
        .map(|e| format!("[{}] {}: {}", e.time, e.kind, e.data))
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&log_path, log_text + "\n")?;

    let samples: Vec<_> = report
        .samples
        .iter()
        .map(|s| serde_json::json!({"case": s.case, "index": s.index, "ticks": s.ticks, "variant": s.variant}))
        .collect();

    // Provenance required by rules.md section 6: identity, source state, device
    // selector, timestamps and artifact hashes, in addition to the samples. The
    // comparison scripts read `rtos`/`board`/`language` instead of guessing them
    // from the build id.
    let language_str = match language {
        Language::C => "c",
        Language::Rust => "rust",
    };
    let artifact_hashes: serde_json::Map<String, serde_json::Value> = artifact
        .images
        .iter()
        .filter_map(|path| {
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("image")
                .to_string();
            match results::sha256_file(path) {
                Ok(hash) => Some((name, serde_json::json!(hash))),
                Err(e) => {
                    eprintln!(
                        "[workflow] artifact hash skipped for {}: {e}",
                        path.display()
                    );
                    None
                }
            }
        })
        .collect();
    let started_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let evidence = serde_json::json!({
        "protocol": 1,
        "kind": "benchmark",
        "run_id": run_id,
        "build_id": artifact.build_id,
        "rtos": plugin_id,
        "board": device.board.manifest_id(),
        "language": language_str,
        "device": device.selector,
        "source": results::source_state(project),
        "started_at_unix_s": started_at,
        "tool_versions": results::tool_versions(project),
        "build_profile": results::build_profile(plugin_id, language_str),
        "artifact_hashes": artifact_hashes,
        "cases": report.cases,
        "repetitions": report.repetitions,
        "warmup": warmup,
        "ticks_per_second": report.ticks_per_second,
        "ticks_per_second_by_case": report.case_rates,
        "memory_high_water": report.memory.map(|m| serde_json::json!({
            "stack_high_water_bytes": m.stack_high_water_bytes,
            "heap_min_free_bytes": m.heap_min_free_bytes,
            "heap_free_bytes": m.heap_free_bytes,
            "heap_overhead_bytes": m.heap_overhead_bytes,
            "per_task_ram_overhead_bytes": m.per_task_ram_overhead_bytes,
            "task_count": m.task_count,
            "worst_stack_headroom_bytes": m.worst_stack_headroom_bytes,
            "best_stack_headroom_bytes": m.best_stack_headroom_bytes,
        })),
        "static_footprint": static_footprint(artifact),
        "samples": samples,
    });
    let mut json = serde_json::to_vec_pretty(&evidence)
        .map_err(|e| Error::Protocol(format!("encode benchmark evidence: {e}")))?;
    json.push(b'\n');
    std::fs::write(&json_path, json)?;
    results::write_manifest(&results_dir, &evidence)?;
    eprintln!(
        "[uart] saved raw={} log={} json={} manifest={}",
        raw_path.display(),
        log_path.display(),
        json_path.display(),
        results_dir.join("manifest.json").display()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(project: &Path) -> ExecutionRequest {
        ExecutionRequest {
            project: project.to_path_buf(),
            board: Board::Stm32f4Discovery,
            language: Language::C,
            operation: Operation::Benchmark,
            port: None,
            run_id: "run-test".into(),
            challenge: "chal-test".into(),
            cases: None,
            repetitions: 1,
            warmup: 0,
            reuse_build: true,
            serial_run_timeout_ms: 300_000,
        }
    }

    #[test]
    fn reuse_head_check_rejects_only_a_real_change() {
        assert!(head_is_stale("abc12345", "def67890"));
        assert!(!head_is_stale("abc12345", "abc12345"));
        // An unavailable HEAD (not a git tree, or a cache written before the
        // check existed) must not block reuse.
        assert!(!head_is_stale("abc12345", ""));
        assert!(!head_is_stale("", "def67890"));
    }

    #[test]
    fn reuse_without_a_cache_is_an_error() {
        let dir = std::env::temp_dir().join(format!("rtos-cache-missing-{}", std::process::id()));
        let req = request(&dir);
        let err = load_cached_artifact(&dir, "test-plugin", &req).unwrap_err();
        assert!(
            format!("{err}").contains("no cached build"),
            "unexpected: {err}"
        );
    }
}
