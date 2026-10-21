//! Compact CLI for explicit file flashing, GDB sessions and digital captures.
use clap::{Args, ValueEnum};
use rtos_core::{
    boards,
    error::{Error, Result},
    flash::{self, FlashBackend, FlashRequest, ImageFormat, ProbeRsFlash},
    gdb::{self, GdbRequest},
    logic_analyzer::{self, CaptureRequest, Kingst, LogicAnalyzer},
    monitor,
    process::CommandSpec,
    results,
    tooling::{self, ToolRun},
};
use std::{
    io::{self, BufReader},
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone, Copy, ValueEnum)]
pub enum Format {
    Elf,
    Bin,
    EspIdf,
}

#[derive(Args)]
pub struct FlashArgs {
    /// Already-built image; no build or UART verification is performed.
    #[arg(long)]
    file: PathBuf,
    /// Exact MCU: STM32F407VGTx or esp32c6.
    #[arg(long)]
    mcu: String,
    /// Explicit probe VID:PID[:serial]; ambiguous selection fails.
    #[arg(long)]
    probe: String,
    #[arg(long, value_enum, default_value = "elf")]
    format: Format,
    /// Required load address for raw STM32 binary, e.g. 0x08000000.
    #[arg(long, value_parser = address)]
    address: Option<u64>,
    #[arg(long)]
    bootloader: Option<PathBuf>,
    #[arg(long)]
    partition_table: Option<PathBuf>,
    /// Explicitly reset after successful programming.
    #[arg(long)]
    reset: bool,
    #[arg(long, default_value_t = 120)]
    timeout: u64,
}

#[derive(Args)]
pub struct CaptureArgs {
    /// USB bus.address from sigrok-cli -d kingst-la2016 --scan.
    #[arg(long)]
    connection: String,
    #[arg(long, value_delimiter = ',', value_parser = channel, default_value = "CH0,CH1")]
    channels: Vec<u8>,
    /// Samples per second, e.g. 10MHz, 500kHz or 10000000.
    #[arg(long, value_parser = rate, default_value = "10MHz")]
    samplerate: u64,
    #[arg(long, default_value_t = 100_000_000)]
    samples: u64,
    #[arg(long, default_value = "1.4")]
    threshold: String,
    /// For example CH0=r or CH0=r,CH1=1; timeout also bounds trigger wait.
    #[arg(long)]
    trigger: Option<String>,
    #[arg(long, default_value_t = 60)]
    timeout: u64,
    #[arg(long, default_value = "/usr/local/bin/sigrok-cli")]
    sigrok: PathBuf,
}

#[derive(Args)]
pub struct GdbArgs {
    #[arg(long)]
    elf: PathBuf,
    #[arg(long)]
    mcu: String,
    /// Existing GDB server bound to the intended MCU/probe, e.g. 127.0.0.1:1337.
    #[arg(long)]
    endpoint: String,
    /// GDB with support for the target architecture.
    #[arg(long, default_value = "gdb")]
    gdb: PathBuf,
    /// Newline-delimited actions; omit for a persistent stdin/stdout session.
    #[arg(long)]
    actions: Option<PathBuf>,
    /// Deadline per GDB operation.
    #[arg(long, default_value_t = 10)]
    timeout: u64,
}

#[derive(Args)]
pub struct ServerArgs {
    #[arg(long)]
    mcu: String,
    #[arg(long)]
    probe: String,
    #[arg(long, default_value = "127.0.0.1:1337")]
    endpoint: String,
    /// Maximum server lifetime; Ctrl-C stops it early and retains logs.
    #[arg(long, default_value_t = 1800)]
    timeout: u64,
}

#[derive(Args)]
pub struct MonitorArgs {
    /// Serial port device path; overrides the board manifest's port_selector.
    #[arg(long)]
    port: Option<PathBuf>,
    /// Baud rate; defaults to the board manifest's value, then 115200.
    #[arg(long)]
    baud: Option<u32>,
    /// Capture duration in seconds; 0 runs until the port reports EOF.
    #[arg(long, default_value_t = 10)]
    seconds: u64,
    /// Print bytes as a hex dump instead of raw text.
    #[arg(long)]
    hex: bool,
    /// Also tee the raw capture to this file.
    #[arg(long)]
    file: Option<PathBuf>,
}

fn address(value: &str) -> std::result::Result<u64, String> {
    let parsed = if let Some(hex) = value.strip_prefix("0x") {
        u64::from_str_radix(hex, 16)
    } else {
        value.parse()
    };
    parsed.map_err(|e| e.to_string())
}
fn channel(value: &str) -> std::result::Result<u8, String> {
    value
        .strip_prefix("CH")
        .and_then(|v| v.parse::<u8>().ok())
        .filter(|v| *v <= 15)
        .ok_or_else(|| "digital channels must be CH0..CH15".into())
}
fn rate(value: &str) -> std::result::Result<u64, String> {
    let value = value.to_ascii_lowercase();
    let (number, multiplier) = if let Some(v) = value.strip_suffix("mhz") {
        (v, 1_000_000)
    } else if let Some(v) = value.strip_suffix("khz") {
        (v, 1_000)
    } else {
        (value.strip_suffix("hz").unwrap_or(&value), 1)
    };
    number
        .parse::<u64>()
        .ok()
        .and_then(|n| n.checked_mul(multiplier))
        .ok_or_else(|| "invalid sample rate".into())
}

pub fn flash(args: FlashArgs, root: &Path, tool: Option<PathBuf>, plan: bool) -> Result<()> {
    let backend = ProbeRsFlash {
        executable: tool.unwrap_or_else(|| "probe-rs".into()),
    };
    let request = FlashRequest {
        file: args.file,
        mcu: args.mcu,
        probe: args.probe,
        format: match args.format {
            Format::Elf => ImageFormat::Elf,
            Format::Bin => ImageFormat::Bin,
            Format::EspIdf => ImageFormat::EspIdf,
        },
        address: args.address,
        bootloader: args.bootloader,
        partition_table: args.partition_table,
        reset: args.reset,
        timeout_seconds: args.timeout,
    };
    if plan {
        println!("{}", tooling::describe(&backend.plan(&request)?));
        return Ok(());
    }
    let directory = flash::flash(&backend, &request, &results::campaigns_root(root))?;
    println!(
        "{}",
        serde_json::json!({"status": "programmed", "application_readiness": "not_checked", "evidence": directory})
    );
    Ok(())
}

pub fn capture(args: CaptureArgs, root: &Path, plan: bool) -> Result<()> {
    let backend = Kingst {
        executable: args.sigrok,
    };
    let request = CaptureRequest {
        connection: args.connection,
        channels: args.channels,
        samplerate: args.samplerate,
        samples: args.samples,
        threshold: args.threshold,
        trigger: args.trigger,
        timeout_seconds: args.timeout,
    };
    if plan {
        println!(
            "{}",
            tooling::describe(&backend.plan(&request, Path::new("<new-run>/raw/capture.sr"))?)
        );
        return Ok(());
    }
    let capture = logic_analyzer::capture(&backend, &request, &results::campaigns_root(root))?;
    println!(
        "{}",
        serde_json::json!({"status": "captured", "capture": capture.directory.join("raw/capture.sr"),
        "evidence": capture.directory, "requested_minimum_samples": capture.requested_samples,
        "captured_samples": capture.captured_samples, "diagnostics_present": capture.diagnostics_present,
        "signal_integrity": "unverified"})
    );
    Ok(())
}

pub fn gdb(args: GdbArgs, root: &Path, plan: bool) -> Result<()> {
    let request = GdbRequest {
        executable: args.gdb,
        elf: args.elf,
        mcu: args.mcu,
        endpoint: args.endpoint,
        timeout: Duration::from_secs(args.timeout),
    };
    gdb::validate(&request)?;
    if plan {
        println!(
            "{}",
            serde_json::json!({"operation": "gdb", "gdb": request.executable,
            "mcu": request.mcu, "elf": request.elf, "endpoint": request.endpoint,
            "actions": args.actions, "hardware_touched": false})
        );
        return Ok(());
    }
    match args.actions {
        Some(path) => gdb::run_session(
            &request,
            &results::campaigns_root(root),
            &mut BufReader::new(std::fs::File::open(path)?),
            &mut io::stdout().lock(),
        ),
        None => gdb::run_stdio_session(&request, &results::campaigns_root(root)),
    }
}

pub fn server(args: ServerArgs, root: &Path, tool: Option<PathBuf>, plan: bool) -> Result<()> {
    let (chip, protocol) = flash::target(&args.mcu)?;
    flash::validate_probe(&args.probe)?;
    let endpoint: std::net::SocketAddr = args
        .endpoint
        .parse()
        .map_err(|_| Error::Invalid("server endpoint must be a loopback IP:port".into()))?;
    if !endpoint.ip().is_loopback() || endpoint.port() == 0 || !(1..=86400).contains(&args.timeout)
    {
        return Err(Error::Invalid(
            "server requires a loopback IP, nonzero port and timeout 1..86400s".into(),
        ));
    }
    let spec = CommandSpec {
        program: tool.unwrap_or_else(|| "probe-rs".into()),
        args: vec![
            "gdb".into(),
            "--chip".into(),
            chip.into(),
            "--probe".into(),
            args.probe.into(),
            "--protocol".into(),
            protocol.into(),
            "--non-interactive".into(),
            "--gdb-connection-string".into(),
            args.endpoint.clone().into(),
        ],
        cwd: root.to_path_buf(),
        env: vec![],
        timeout: Duration::from_secs(args.timeout),
    };
    if plan {
        println!("{}", tooling::describe(&spec));
        return Ok(());
    }
    let mut run = ToolRun::create(
        &results::campaigns_root(root),
        "gdb-server",
        serde_json::json!({"mcu": chip, "endpoint": args.endpoint}),
    )?;
    println!(
        "{}",
        serde_json::json!({"status": "starting", "endpoint": args.endpoint, "evidence": run.directory,
        "note": "attach may halt MCU; readiness is established by GDB connection"})
    );
    run.execute(&spec)?;
    run.finish(
        "complete",
        "GDB server exited; target state after disconnect unknown",
    )
}

pub fn monitor(
    args: MonitorArgs,
    root: &Path,
    board_id: Option<&str>,
    fallback_port: Option<&PathBuf>,
    plan: bool,
) -> Result<()> {
    let (port, baud) =
        resolve_monitor_port(board_id, args.port.as_ref(), fallback_port, args.baud, root)?;
    if plan {
        println!(
            "{}",
            serde_json::json!({"operation": "monitor", "port": port, "baud": baud,
            "seconds": args.seconds, "hex": args.hex, "file": args.file,
            "hardware_touched": false})
        );
        return Ok(());
    }
    let options = monitor::MonitorOptions {
        port,
        baud,
        seconds: args.seconds,
        hex: args.hex,
        file: args.file,
    };
    monitor::monitor(&options)
}

/// Resolve the monitor port and baud rate.
///
/// Port priority: `--port` -> global `--port` -> board manifest `port_selector`.
/// Baud priority: `--baud` -> board manifest `baud` -> 115200.
fn resolve_monitor_port(
    board_id: Option<&str>,
    explicit: Option<&PathBuf>,
    fallback: Option<&PathBuf>,
    baud: Option<u32>,
    root: &Path,
) -> Result<(PathBuf, u32)> {
    let board = board_id.and_then(|id| boards::load_board(root, id).ok());
    let baud = baud
        .or_else(|| board.as_ref().map(|b| b.serial.baud))
        .unwrap_or(115200);
    if let Some(port) = explicit.or(fallback) {
        return Ok((port.clone(), baud));
    }
    if let Some(board) = board {
        if let Some(selector) = board.port() {
            return Ok((PathBuf::from(selector), baud));
        }
        return Err(Error::Invalid(format!(
            "board {} has no verified serial port_selector; pass --port",
            board_id.unwrap_or("unknown")
        )));
    }
    Err(Error::Invalid(
        "monitor requires --port or --board (with a verified serial port_selector)".into(),
    ))
}
