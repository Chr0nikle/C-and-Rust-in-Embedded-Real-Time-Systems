//! CLI composition root: --plan shows routes; freertos builds and flashes for real.
mod hardware;
use clap::{Parser, Subcommand, ValueEnum};
use rtos_core::{
    debug::{self, DebugRequest},
    error::{Error, Result},
    plugin_interface::{Board, BuildRequest, Language, PluginRegistry},
    results, scope, tool_checker,
    workflow::{self, ExecutionRequest, Operation},
};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[derive(Parser)]
#[command(
    name = "rtos",
    version,
    about = "RTOS research tool: firmware workflows, file flashing, GDB and logic capture"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
    /// Explain the planned route without building, downloading or flashing.
    #[arg(long, global = true)]
    plan: bool,
    /// Target board.
    #[arg(long, global = true, value_enum)]
    board: Option<BoardArg>,
    /// Application language; C is the development default.
    #[arg(long, global = true, value_enum)]
    language: Option<LanguageArg>,
    /// Serial port device path (e.g. /dev/serial/by-id/...).
    #[arg(long, global = true)]
    port: Option<PathBuf>,
    /// Project root (defaults to the current directory's ancestor with config/rtos.toml).
    #[arg(long, global = true)]
    project: Option<PathBuf>,
    /// Path to probe-rs executable (defaults to auto-detection).
    #[arg(long, global = true)]
    probe_rs: Option<PathBuf>,
}

#[derive(Clone, Copy, ValueEnum)]
enum BoardArg {
    Esp32c6,
    Nrf52840dk,
    Stm32f4discovery,
    Stm32h723,
}

#[derive(Clone, Copy, ValueEnum)]
enum LanguageArg {
    C,
    Rust,
}

#[derive(Clone, Copy, PartialEq, ValueEnum)]
enum Platform {
    Freertos,
    Zephyr,
    Nuttx,
    FreertosInRust,
    Rivet,
}

impl Platform {
    fn id(self) -> &'static str {
        match self {
            Self::Freertos => "freertos",
            Self::Zephyr => "zephyr",
            Self::Nuttx => "nuttx",
            Self::FreertosInRust => "freertos-in-rust",
            Self::Rivet => "rivet",
        }
    }
}

#[derive(Subcommand)]
enum Commands {
    /// Program an explicit image and MCU, independent of RTOS (no build/startup check).
    Flash(hardware::FlashArgs),
    /// Capture raw digital samples from an explicitly selected Kingst analyzer.
    Capture(hardware::CaptureArgs),
    /// Persistent GDB stepping with short stdin actions and bounded JSON replies.
    Gdb(hardware::GdbArgs),
    /// Serve the selected MCU to GDB on loopback; attach may halt it.
    GdbServer(hardware::ServerArgs),
    /// Show connected USB probes and serial endpoints, identified via board manifests.
    List,
    /// Validate the tools required by a board/RTOS route without provisioning.
    Check {
        #[arg(value_enum)]
        rtos: Platform,
    },
    /// Report static RAM/flash footprint from a built ELF (builds if no --file).
    Memory {
        #[arg(value_enum)]
        rtos: Platform,
        /// ELF to analyze instead of building.
        #[arg(long)]
        file: Option<PathBuf>,
    },
    /// Static safety/maintainability analysis of the shared C and Rust firmware.
    Safety {
        /// Evidence root directory (default: <repo>/results).
        #[arg(long)]
        out: Option<PathBuf>,
    },
    /// Consolidate benchmark evidence into a normalized cross-path comparison.
    Analyze {
        /// Output directory (default: results/campaigns/comparison-<utc>).
        #[arg(long)]
        out: Option<PathBuf>,
        /// Comma-separated required case IDs; omit to use each path's newest run.
        #[arg(long)]
        cases: Option<String>,
    },
    /// Render plots and tables from a benchmark comparison.
    Report {
        /// Comparison directory (default: the newest results/campaigns/comparison-*).
        #[arg(long)]
        comparison: Option<PathBuf>,
        /// Output directory for plots.
        #[arg(long)]
        out: Option<PathBuf>,
    },
    /// Run a long-duration drift campaign (repeated benchmark chunks).
    Study {
        #[arg(value_enum)]
        rtos: Platform,
        /// Serial port device path (e.g. /dev/serial/by-id/...).
        #[arg(long)]
        port: PathBuf,
        /// Case IDs to run, comma-separated.
        #[arg(long)]
        cases: Option<String>,
        /// Valid samples per case per chunk.
        #[arg(long)]
        repetitions: Option<u32>,
        /// Number of benchmark chunks (default 3).
        #[arg(long)]
        chunks: Option<u32>,
        /// Validate arguments and print the plan without running.
        #[arg(long)]
        dry_run: bool,
    },
    /// Build, flash and run the smoke handshake for FreeRTOS (C: STM32F4 /
    /// ESP32-C6; Rust: STM32F4).
    Freertos,
    /// Build, flash and run the smoke handshake for Zephyr (C: STM32F4 / ESP32-C6).
    Zephyr,
    /// Build, flash and run the smoke handshake for NuttX (C: STM32F4 /
    /// ESP32-C6).
    Nuttx,
    /// Flash and execute the benchmark suite with application-owned UART capture.
    Benchmark {
        #[arg(value_enum)]
        rtos: Platform,
        /// Case IDs to run, comma-separated; defaults to the advertised suite.
        #[arg(long, value_delimiter = ',')]
        cases: Option<Vec<u32>>,
        /// Valid samples collected per case (default: config defaults.repetitions).
        #[arg(long)]
        repetitions: Option<u32>,
        /// Discarded warm-up iterations per case (default: config defaults.warmup).
        #[arg(long)]
        warmup: Option<u32>,
        /// Reuse the image from the last successful build of this board/language
        /// instead of rebuilding (for repeated runs of an unchanged image).
        #[arg(long)]
        reuse_build: bool,
    },
    /// Build firmware without a connected board.
    Build {
        #[arg(value_enum)]
        rtos: Platform,
    },
    /// Standalone UART capture without flashing or resetting.
    Monitor(hardware::MonitorArgs),
    /// Inspect a connected instrument (Rigol DS1000Z over USB-TMC).
    Scope(ScopeArgs),
    /// Halt the MCU, inspect registers/variables/backtrace, set breakpoints.
    Debug {
        /// Breakpoint address in hex (e.g. 0x080004bc).
        #[arg(long)]
        bp: Option<String>,
        /// Continue execution (resumes a halted target).
        #[arg(long)]
        cont: bool,
        /// Read memory words (with --read-addr and --read-words).
        #[arg(long)]
        read: bool,
        /// Memory address to read (hex, e.g. 0x20000000).
        #[arg(long)]
        read_addr: Option<String>,
        /// Number of 32-bit words to read (default 1).
        #[arg(long, default_value_t = 1)]
        read_words: u32,
        /// Path to ELF for symbol resolution.
        #[arg(long)]
        elf: Option<PathBuf>,
    },
}

fn registry() -> Result<PluginRegistry> {
    let mut registry = PluginRegistry::default();
    registry.register(Box::new(rtos_plugin_freertos::FreeRtosPlugin))?;
    registry.register(Box::new(rtos_plugin_zephyr::ZephyrPlugin))?;
    registry.register(Box::new(rtos_plugin_nuttx::NuttXPlugin))?;
    registry.register(Box::new(rtos_plugin_freertos_in_rust::FreertosInRustPlugin))?;
    registry.register(Box::new(rtos_plugin_rivet::RivetPlugin))?;
    Ok(registry)
}

/// Walk up from the current directory (or an explicit --project) to find the
/// project root, identified by the presence of config/rtos.toml.
fn resolve_project_root(explicit: Option<&PathBuf>) -> Result<PathBuf> {
    if let Some(p) = explicit {
        if !p.is_dir() {
            return Err(Error::Invalid(format!(
                "project directory not found: {}",
                p.display()
            )));
        }
        return Ok(p.clone());
    }
    let mut dir = std::env::current_dir().map_err(|e| Error::Io(format!("cwd: {e}")))?;
    loop {
        if dir.join("config/rtos.toml").is_file() {
            return Ok(dir);
        }
        if !dir.pop() {
            return Err(Error::Invalid(
                "could not find project root (no config/rtos.toml found); use --project".into(),
            ));
        }
    }
}

fn board_from_arg(arg: BoardArg) -> Board {
    match arg {
        BoardArg::Esp32c6 => Board::Esp32c6,
        BoardArg::Nrf52840dk => Board::Nrf52840Dk,
        BoardArg::Stm32f4discovery => Board::Stm32f4Discovery,
        BoardArg::Stm32h723 => Board::Stm32h723,
    }
}

/// Default board for a platform when `--board` is omitted. The STM32F4 ports
/// keep the Discovery default; the other routes use the ESP32-C6 development board.
fn default_board(platform: Platform) -> Board {
    match platform {
        Platform::FreertosInRust => Board::Stm32f4Discovery,
        Platform::Rivet => Board::Stm32f4Discovery,
        _ => Board::Esp32c6,
    }
}

fn language_from_arg(arg: LanguageArg) -> Language {
    match arg {
        LanguageArg::C => Language::C,
        LanguageArg::Rust => Language::Rust,
    }
}

/// Resolve the application language: an explicit flag wins; otherwise the
/// Rust-native kernels are Rust-only and every other route keeps the C default.
fn resolve_language(arg: Option<LanguageArg>, platform: Platform) -> Language {
    match arg {
        Some(a) => language_from_arg(a),
        None => match platform {
            Platform::FreertosInRust => Language::Rust,
            Platform::Rivet => Language::Rust,
            _ => Language::C,
        },
    }
}

fn parse_hex_addr(s: &str) -> Result<u32> {
    let s = s.trim().trim_start_matches("0x").trim_start_matches("0X");
    u32::from_str_radix(s, 16)
        .map_err(|e| Error::Invalid(format!("invalid hex address '{s}': {e}")))
}

/// Validate the tools required by one board/RTOS route and report each check.
fn run_check(project: Option<&PathBuf>, board: Option<BoardArg>, rtos: Platform) -> Result<()> {
    let project = resolve_project_root(project)?;
    let board = board
        .map(board_from_arg)
        .unwrap_or_else(|| default_board(rtos));
    let route = match rtos {
        Platform::Freertos => tool_checker::Route::Freertos,
        Platform::Zephyr => tool_checker::Route::Zephyr,
        Platform::Nuttx => tool_checker::Route::Nuttx,
        Platform::FreertosInRust => tool_checker::Route::FreertosInRust,
        Platform::Rivet => tool_checker::Route::Rivet,
    };
    let rtos_name = match rtos {
        Platform::Freertos => "freertos",
        Platform::Zephyr => "zephyr",
        Platform::Nuttx => "nuttx",
        Platform::FreertosInRust => "freertos-in-rust",
        Platform::Rivet => "rivet",
    };

    let checks = tool_checker::validate(&project, board, route);
    let mut missing = 0usize;
    for check in &checks {
        let status = if check.ok {
            "ok"
        } else if check.required {
            "MISSING"
        } else {
            "optional-missing"
        };
        println!("{:<20} {:<16} {}", check.name, status, check.detail);
        if check.required && !check.ok {
            missing += 1;
        }
    }
    if missing > 0 {
        return Err(Error::Invalid(format!(
            "{missing} required tool(s) missing for the {rtos_name} route"
        )));
    }
    println!("all required tools resolved for the {rtos_name} route");
    Ok(())
}

/// Locate the firmware ELF, preferring the plugin's declared image list.
fn find_elf(artifact: &rtos_core::plugin_interface::Artifact) -> Result<PathBuf> {
    for image in &artifact.images {
        if image.extension().map(|e| e == "elf").unwrap_or(false) && image.is_file() {
            return Ok(image.clone());
        }
    }
    // Fall back to scanning the build directory when the plugin reports none.
    let dir = &artifact.directory;
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().map(|e| e == "elf").unwrap_or(false) {
                return Ok(path);
            }
        }
    }
    Err(Error::Invalid(format!("no ELF found in {}", dir.display())))
}

/// Report static memory footprint, building the route when no file is given.
fn run_memory(
    project: Option<&PathBuf>,
    board: Option<BoardArg>,
    language: Option<LanguageArg>,
    rtos: Platform,
    file: Option<PathBuf>,
) -> Result<()> {
    let project = resolve_project_root(project)?;
    let board = board
        .map(board_from_arg)
        .unwrap_or_else(|| default_board(rtos));
    let language = resolve_language(language, rtos);

    let (elf, build_id) = match file {
        Some(path) => (path, None),
        None => {
            let registry = registry()?;
            let plugin = registry.get(rtos.id())?;
            let request = BuildRequest {
                project: project.clone(),
                board,
                language,
                workload: "memory".to_string(),
                output: project.join("firmware").join(plugin.id()),
                clean: false,
            };
            let artifact = plugin.build(&request)?;
            (find_elf(&artifact)?, Some(artifact.build_id))
        }
    };

    let report = rtos_core::memory::analyze_elf(&elf)?;
    println!("{}", elf.display());
    for section in &report.sections {
        println!("{:<24} {:>10} {}", section.name, section.size, section.kind);
    }
    println!(
        "flash_bytes={} ram_bytes={}",
        report.flash_bytes, report.ram_bytes
    );

    if let Some(build_id) = build_id {
        let dir = results::campaigns_root(&project).join(&build_id);
        std::fs::create_dir_all(&dir)?;
        let evidence = serde_json::json!({
            "kind": "memory",
            "build_id": build_id,
            "elf": elf.to_string_lossy(),
            "flash_bytes": report.flash_bytes,
            "ram_bytes": report.ram_bytes,
            "sections": report
                .sections
                .iter()
                .map(|s| serde_json::json!({"name": s.name, "size": s.size, "kind": s.kind}))
                .collect::<Vec<_>>(),
        });
        let mut json = serde_json::to_vec_pretty(&evidence)
            .map_err(|e| Error::Invalid(format!("encode memory evidence: {e}")))?;
        json.push(b'\n');
        std::fs::write(dir.join("memory.json"), json)?;
        println!("saved {}", dir.join("memory.json").display());
    }
    Ok(())
}

/// Run the safety/maintainability analysis and persist its evidence report.
fn run_safety(project: Option<&PathBuf>, out: Option<PathBuf>) -> Result<()> {
    let project = resolve_project_root(project)?;
    let settings = rtos_core::safety::default_settings();
    let report = rtos_core::safety::analyze(&project, &settings)?;

    println!("schema_version={}", report.schema_version);
    println!(
        "rust_files={} rust_lines={}",
        report.scope.rust_files, report.scope.rust_lines
    );
    for (kind, count) in &report.scope.counts {
        println!("scope {kind}={count}");
    }
    println!(
        "effort files={} lines={} rust={} c={} code={} comment={} blank={} decision_points={} functions={} max_function_decision_points={} mean_function_decision_points={:.2} halstead_volume_mean={:.1} maintainability_index_mean={:.1} maintainability_index_min={:.1}",
        report.effort.files,
        report.effort.lines,
        report.effort.rust_lines,
        report.effort.c_lines,
        report.effort.code_lines,
        report.effort.comment_lines,
        report.effort.blank_lines,
        report.effort.decision_points,
        report.effort.functions,
        report.effort.max_function_decision_points,
        report.effort.mean_function_decision_points,
        report.effort.halstead_volume_mean,
        report.effort.maintainability_index_mean,
        report.effort.maintainability_index_min
    );
    let mut by_check = std::collections::BTreeMap::new();
    for finding in &report.findings {
        *by_check.entry(finding.check.clone()).or_insert(0usize) += 1;
    }
    println!(
        "findings_ran={} findings={}",
        report.findings_ran,
        report.findings.len()
    );
    for (check, count) in &by_check {
        println!("finding {check}={count}");
    }
    let mut clippy_by_check = std::collections::BTreeMap::new();
    for finding in &report.clippy_findings {
        *clippy_by_check
            .entry(finding.check.clone())
            .or_insert(0usize) += 1;
    }
    println!(
        "clippy_ran={} clippy_findings={}",
        report.clippy_ran,
        report.clippy_findings.len()
    );
    for (check, count) in &clippy_by_check {
        println!("clippy {check}={count}");
    }
    println!(
        "clang_analyze_ran={} clang_analyze_findings={}",
        report.clang_analyze_ran,
        report.clang_analyze_findings.len()
    );
    for (name, version) in &report.tools {
        println!("tool {name}: {version}");
    }

    let results = out.unwrap_or_else(|| results::campaigns_root(&project));
    let mut run = rtos_core::tooling::ToolRun::create(
        &results,
        "safety",
        serde_json::json!({"schema_version": report.schema_version}),
    )?;
    let mut json = serde_json::to_vec_pretty(&report)
        .map_err(|e| Error::Invalid(format!("encode safety evidence: {e}")))?;
    json.push(b'\n');
    std::fs::write(run.directory.join("safety.json"), json)?;
    run.record(
        "safety",
        serde_json::to_value(&report).map_err(|e| Error::Invalid(e.to_string()))?,
    )?;
    run.finish("complete", "host-only analysis; see safety.json")?;
    println!("saved {}", run.directory.join("safety.json").display());
    Ok(())
}

/// Instrument inspection arguments.
#[derive(clap::Args)]
struct ScopeArgs {
    /// Action to perform.
    #[arg(value_enum, default_value_t = ScopeAction::Identify)]
    action: ScopeAction,
    /// USB-TMC device node created by the usbtmc kernel driver.
    #[arg(long, default_value = scope::DEFAULT_DEVICE)]
    device: String,
    /// Oscilloscope channel (1-4).
    #[arg(long, default_value_t = 1)]
    channel: u8,
    /// Measurement items, comma-separated (vpp,vavg,vrms,freq,per,vmax,vmin).
    #[arg(long, value_delimiter = ',', default_value = "vpp,vavg,vrms,freq")]
    items: Vec<String>,
}

#[derive(Clone, Copy, ValueEnum)]
enum ScopeAction {
    /// Read the instrument identification string (`*IDN?`).
    Identify,
    /// Read measurement items from one channel.
    Measure,
}

fn run_scope(args: ScopeArgs) -> Result<()> {
    let mut instrument = scope::Scope::open(&args.device)?;
    match args.action {
        ScopeAction::Identify => println!("{}", instrument.identify()?),
        ScopeAction::Measure => {
            for item in &args.items {
                match instrument.measure(args.channel, item) {
                    Ok(value) => println!("chan{} {:<5} = {value:.6}", args.channel, item),
                    Err(e) => println!("chan{} {:<5} = unavailable ({e})", args.channel, item),
                }
            }
        }
    }
    Ok(())
}

/// Resolve a serial port for flash/benchmark/study: an explicit `--port` wins;
/// otherwise select the sole discovered device matching the requested board.
fn resolve_port(explicit: Option<PathBuf>, project: &Path, board: Board) -> Result<PathBuf> {
    if let Some(port) = explicit {
        return Ok(port);
    }
    let devices = rtos_core::device_manager::discover(project)?;
    let candidates: Vec<_> = devices
        .iter()
        .filter(|d| d.board == Some(board) && d.uart.is_some())
        .collect();
    match candidates.as_slice() {
        [one] => {
            let port = one.uart.clone().expect("filtered on uart");
            eprintln!(
                "[rtos] auto-selected sole {board:?} device: {}",
                port.display()
            );
            Ok(port)
        }
        [] => Err(Error::Invalid(format!(
            "no connected {board:?} device found; pass --port or connect the board"
        ))),
        _ => Err(Error::Invalid(format!(
            "multiple {board:?} devices found; pass --port to disambiguate"
        ))),
    }
}

/// Fresh opaque challenge token for the UART handshake.
fn fresh_challenge() -> String {
    use std::io::Read;
    let mut buf = [0u8; 8];
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        if f.read_exact(&mut buf).is_ok() {
            return buf.iter().map(|b| format!("{b:02x}")).collect();
        }
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{:x}{:x}", nanos, std::process::id())
}

/// Invoke a project analysis script with `python3`, streaming its output.
fn run_python_script(project: &Path, script: &str, args: &[&str]) -> Result<()> {
    let script_path = project.join("scripts").join("benchmarks").join(script);
    let status = std::process::Command::new("python3")
        .arg(&script_path)
        .args(args)
        .current_dir(project)
        .status()
        .map_err(|e| Error::Io(format!("run python3 {script}: {e}")))?;
    if status.success() {
        Ok(())
    } else {
        Err(Error::Io(format!("python3 {script} exited with {status}")))
    }
}

/// `rtos analyze`: consolidate benchmark evidence into a comparison.
fn run_analyze(project: Option<&PathBuf>, out: Option<PathBuf>, cases: Option<&str>) -> Result<()> {
    let project = resolve_project_root(project)?;
    let mut args: Vec<&str> = vec!["compare"];
    let out_arg;
    let cases_arg;
    if let Some(o) = out {
        out_arg = o.to_string_lossy().into_owned();
        args.push("--out");
        args.push(&out_arg);
    }
    if let Some(c) = cases {
        cases_arg = c.to_string();
        args.push("--cases");
        args.push(&cases_arg);
    }
    run_python_script(&project, "analyze.py", &args)
}

/// `rtos report`: render plots from the newest (or given) comparison.
fn run_report(
    project: Option<&PathBuf>,
    comparison: Option<PathBuf>,
    out: Option<PathBuf>,
) -> Result<()> {
    let project = resolve_project_root(project)?;
    let mut args: Vec<&str> = vec!["report"];
    let comp_arg;
    let out_arg;
    if let Some(c) = comparison {
        comp_arg = c.to_string_lossy().into_owned();
        args.push(&comp_arg);
    }
    if let Some(o) = out {
        out_arg = o.to_string_lossy().into_owned();
        args.push("--out");
        args.push(&out_arg);
    }
    run_python_script(&project, "analyze.py", &args)
}

/// `rtos study`: run a long-duration drift campaign (repeated benchmark chunks).
#[allow(clippy::too_many_arguments)]
fn run_study(
    project: Option<&PathBuf>,
    board: Option<BoardArg>,
    language: Option<LanguageArg>,
    rtos: Platform,
    port: PathBuf,
    cases: Option<&str>,
    repetitions: Option<u32>,
    chunks: Option<u32>,
    dry_run: bool,
) -> Result<()> {
    let project = resolve_project_root(project)?;
    let board = board
        .map(board_from_arg)
        .unwrap_or_else(|| default_board(rtos));
    let language = resolve_language(language, rtos);
    let mut args: Vec<String> = vec![
        "--rtos".into(),
        rtos.id().into(),
        "--board".into(),
        board.manifest_id().into(),
        "--language".into(),
        match language {
            Language::C => "c".into(),
            Language::Rust => "rust".into(),
        },
        "--port".into(),
        port.to_string_lossy().into_owned(),
    ];
    if let Some(c) = cases {
        args.push("--cases".into());
        args.push(c.to_string());
    }
    if let Some(r) = repetitions {
        args.push("--repetitions".into());
        args.push(r.to_string());
    }
    if let Some(n) = chunks {
        args.push("--chunks".into());
        args.push(n.to_string());
    }
    if dry_run {
        args.push("--dry-run".into());
    }
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    run_python_script(&project, "study.py", &refs)
}

fn run(cli: Cli) -> Result<()> {
    let mut benchmark_cases: Option<Vec<u32>> = None;
    let mut benchmark_repetitions: Option<u32> = None;
    let mut benchmark_warmup: Option<u32> = None;
    let mut benchmark_reuse_build = false;
    let (platform, operation) = match cli.command {
        Commands::Flash(args) => {
            return hardware::flash(
                args,
                &resolve_project_root(cli.project.as_ref())?,
                cli.probe_rs,
                cli.plan,
            )
        }
        Commands::Capture(args) => {
            return hardware::capture(args, &resolve_project_root(cli.project.as_ref())?, cli.plan)
        }
        Commands::Scope(args) => return run_scope(args),
        Commands::Gdb(args) => {
            return hardware::gdb(args, &resolve_project_root(cli.project.as_ref())?, cli.plan)
        }
        Commands::GdbServer(args) => {
            return hardware::server(
                args,
                &resolve_project_root(cli.project.as_ref())?,
                cli.probe_rs,
                cli.plan,
            )
        }
        Commands::Check { rtos } => {
            return run_check(cli.project.as_ref(), cli.board, rtos);
        }
        Commands::Safety { out } => {
            return run_safety(cli.project.as_ref(), out);
        }
        Commands::Memory { rtos, file } => {
            return run_memory(cli.project.as_ref(), cli.board, cli.language, rtos, file);
        }
        Commands::Analyze { out, cases } => {
            return run_analyze(cli.project.as_ref(), out, cases.as_deref());
        }
        Commands::Report { comparison, out } => {
            return run_report(cli.project.as_ref(), comparison, out);
        }
        Commands::Study {
            rtos,
            port,
            cases,
            repetitions,
            chunks,
            dry_run,
        } => {
            return run_study(
                cli.project.as_ref(),
                cli.board,
                cli.language,
                rtos,
                port,
                cases.as_deref(),
                repetitions,
                chunks,
                dry_run,
            );
        }
        Commands::Freertos => (Platform::Freertos, Operation::Flash),
        Commands::Zephyr => (Platform::Zephyr, Operation::Flash),
        Commands::Nuttx => (Platform::Nuttx, Operation::Flash),
        Commands::Benchmark {
            rtos,
            cases,
            repetitions,
            warmup,
            reuse_build,
        } => {
            benchmark_cases = cases;
            benchmark_repetitions = repetitions;
            benchmark_warmup = warmup;
            benchmark_reuse_build = reuse_build;
            (rtos, Operation::Benchmark)
        }
        Commands::Build { rtos } => (rtos, Operation::Build),
        Commands::List => {
            use std::io::Write as _;
            let project = resolve_project_root(cli.project.as_ref())?;
            let devices = rtos_core::device_manager::discover(&project)?;
            // Ignore write errors: piping into `head` closes stdout early.
            let mut out = std::io::stdout().lock();
            let _ = writeln!(out, "Detected devices ({}):", devices.len());
            for d in &devices {
                let board = d.board.map(|b| b.manifest_id()).unwrap_or("unknown");
                let uart = d
                    .uart
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "-".into());
                let notes = if d.diagnostics.is_empty() {
                    String::new()
                } else {
                    format!("  [{}]", d.diagnostics.join("; "))
                };
                let _ = writeln!(out, "  {}  board={board}  uart={uart}{notes}", d.selector);
            }
            return Ok(());
        }
        Commands::Monitor(args) => {
            let project = resolve_project_root(cli.project.as_ref())?;
            let board_id = cli.board.map(|b| board_from_arg(b).manifest_id());
            return hardware::monitor(args, &project, board_id, cli.port.as_ref(), cli.plan);
        }
        Commands::Debug {
            bp: break_addr,
            cont: continue_run,
            read: read_mem,
            read_addr,
            read_words,
            elf,
        } => {
            if cli.plan {
                return Err(Error::Invalid(
                    "legacy debug does not support --plan; use gdb --plan".into(),
                ));
            }
            let board = match cli.board {
                Some(b) => board_from_arg(b),
                None => return Err(Error::Invalid("--board required for debug".into())),
            };
            let project = resolve_project_root(cli.project.as_ref())?;
            let probe_rs = match cli.probe_rs.clone() {
                Some(p) => p,
                None => {
                    let candidate = project.join(".cargo/bin/probe-rs");
                    if candidate.exists() {
                        candidate
                    } else {
                        PathBuf::from("probe-rs")
                    }
                }
            };
            let req = DebugRequest::for_board(&project, board, probe_rs, elf)?;
            let work_dir = results::campaigns_root(&project).join(format!(
                "debug-{}",
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0)
            ));
            std::fs::create_dir_all(&work_dir)?;

            if let Some(addr_str) = break_addr {
                let addr = parse_hex_addr(&addr_str)?;
                eprintln!("[debug] setting breakpoint at {addr:#x}");
                let output = debug::break_at(&req, addr)?;
                println!("{}", output.stdout);
                return Ok(());
            }
            if continue_run {
                eprintln!("[debug] continuing execution");
                let output = debug::continue_running(&req)?;
                println!("{}", output.stdout);
                return Ok(());
            }
            if read_mem {
                let addr = read_addr
                    .as_deref()
                    .and_then(|s| parse_hex_addr(s).ok())
                    .ok_or_else(|| Error::Invalid("--read requires --read-addr".into()))?;
                eprintln!("[debug] reading {read_words} words at {addr:#x}");
                let output = debug::read_memory(&req, addr, read_words)?;
                println!("{}", output.stdout);
                return Ok(());
            }
            eprintln!("[debug] halting and inspecting");
            let insp = debug::halt_and_inspect(&req, &work_dir)?;
            println!("{}", debug::format_inspection(&insp));
            return Ok(());
        }
    };

    // --plan: show the route without executing.
    if cli.plan {
        let board = match cli.board {
            Some(b) => board_from_arg(b),
            None => {
                // STM32F4-only routes keep the Discovery; others default to
                // the ESP32-C6 development board.
                if platform == Platform::Freertos
                    || platform == Platform::FreertosInRust
                    || platform == Platform::Rivet
                {
                    default_board(platform)
                } else {
                    return Err(Error::Invalid(
                        "--plan requires --board for this RTOS".into(),
                    ));
                }
            }
        };
        let language = resolve_language(cli.language, platform);
        let registry = registry()?;
        let description =
            workflow::describe(registry.get(platform.id())?, board, language, operation)?;
        print!("{description}");
        return Ok(());
    }

    // Real execution (FreeRTOS, Zephyr, NuttX).
    let project = resolve_project_root(cli.project.as_ref())?;
    let board = cli
        .board
        .map(board_from_arg)
        .unwrap_or_else(|| default_board(platform));
    let language = resolve_language(cli.language, platform);

    // Benchmark parameters default to config/rtos.toml; explicit flags win.
    let config = rtos_core::config_manager::load(Some(project.clone()))?;
    let (benchmark_repetitions, benchmark_warmup) = if matches!(operation, Operation::Benchmark) {
        let repetitions = benchmark_repetitions.unwrap_or(config.defaults.repetitions);
        if repetitions == 0 {
            return Err(Error::Invalid("--repetitions must be nonzero".into()));
        }
        if let Some(cases) = &benchmark_cases {
            if cases.is_empty() {
                return Err(Error::Invalid("--cases must not be empty".into()));
            }
        }
        (
            repetitions,
            benchmark_warmup.unwrap_or(config.defaults.warmup),
        )
    } else {
        (
            benchmark_repetitions.unwrap_or(5),
            benchmark_warmup.unwrap_or(1),
        )
    };

    // Flash and Benchmark operations need a serial port; auto-select the sole
    // matching device when --port is omitted.
    let port = match operation {
        Operation::Build => None,
        _ => Some(resolve_port(cli.port.clone(), &project, board)?),
    };

    let registry = registry()?;
    let plugin = registry.get(platform.id())?;

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let run_id = format!("run-{}-{:06x}", now.as_secs(), now.subsec_nanos());
    let challenge = fresh_challenge();

    let req = ExecutionRequest {
        project,
        board,
        language,
        operation,
        port,
        run_id,
        challenge,
        cases: benchmark_cases,
        repetitions: benchmark_repetitions,
        warmup: benchmark_warmup,
        reuse_build: benchmark_reuse_build,
        serial_run_timeout_ms: config.serial.run_timeout_ms,
    };

    workflow::execute(plugin, &req)
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("rtos: {error}");
            ExitCode::FAILURE
        }
    }
}
