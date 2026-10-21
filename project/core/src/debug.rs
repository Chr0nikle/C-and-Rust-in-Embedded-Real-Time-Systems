//! Programmatic debugging via the probe-rs CLI.
//!
//! Halts the MCU over its debug probe (SWD for STM32, JTAG for ESP32),
//! inspects registers, memory, frames, variables, and backtrace, sets
//! breakpoints, and resumes execution. Each inspection is a separate
//! bounded `probe-rs debug` invocation with scripted `-c` commands;
//! the target resumes after every inspection unless a breakpoint is set.
use crate::error::Result;
use crate::plugin_interface::Board;
use crate::process::{run, CommandOutput, CommandSpec};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Per-inspection timeout for a single `probe-rs debug` invocation.
const INSPECT_TIMEOUT: Duration = Duration::from_secs(30);

/// Resolved parameters for a debug session.
#[derive(Debug)]
pub struct DebugRequest {
    /// probe-rs executable path.
    pub probe_rs: PathBuf,
    /// Debug probe selector, e.g. `0483:3748` for ST-LINK.
    pub probe: String,
    /// probe-rs chip name, e.g. `STM32F407VGTx`.
    pub chip: String,
    /// SWD or JTAG.
    pub protocol: String,
    /// Optional ELF for symbol resolution.
    pub elf: Option<PathBuf>,
}

impl DebugRequest {
    /// Resolve a debug request for a board.
    ///
    /// Probe identity and chip name come from the board manifest `[debug]`
    /// section when present; otherwise board-specific defaults are used.
    pub fn for_board(
        project: &Path,
        board: Board,
        probe_rs: PathBuf,
        elf: Option<PathBuf>,
    ) -> Result<Self> {
        let (probe, chip, protocol) = match crate::boards::load_board(project, board.manifest_id())
        {
            Ok(cfg) => {
                let probe = if cfg.debug.probe_serial.trim().is_empty() {
                    cfg.debug.probe.clone()
                } else {
                    format!("{}:{}", cfg.debug.probe, cfg.debug.probe_serial.trim())
                };
                (probe, cfg.debug.chip, cfg.debug.protocol)
            }
            Err(_) => match board {
                Board::Stm32f4Discovery => (
                    "0483:3748:52C3BF6D".to_string(),
                    "STM32F407VGTx".to_string(),
                    "swd".to_string(),
                ),
                Board::Esp32c6 => (
                    "303a:1001".to_string(),
                    "esp32c6".to_string(),
                    "jtag".to_string(),
                ),
                Board::Nrf52840Dk => (
                    "1366:1061:001050202809".to_string(),
                    "nRF52840_xxAA".to_string(),
                    "swd".to_string(),
                ),
                // The H723 core board has no onboard debugger: an external
                // ST-LINK/V2 on the SWD header, chip named explicitly because
                // probe-rs cannot auto-detect the H7.
                Board::Stm32h723 => (
                    "0483:3748:37C3BF71".to_string(),
                    "STM32H723ZG".to_string(),
                    "swd".to_string(),
                ),
            },
        };
        Ok(Self {
            probe_rs,
            probe,
            chip,
            protocol,
            elf,
        })
    }
}

/// Parsed result of a debug inspection.
#[derive(Debug)]
pub struct Inspection {
    /// The target halted at this address.
    pub halt_address: Option<u32>,
    /// Raw register listing text from `info reg`.
    pub registers: String,
    /// Raw frame info text (may say "Unimplemented").
    pub frame: String,
    /// Raw local variable listing text.
    pub locals: String,
    /// Backtrace YAML path, if `bt yaml` succeeded.
    pub backtrace_path: Option<PathBuf>,
    /// Backtrace frames parsed from YAML.
    pub backtrace: Vec<BacktraceFrame>,
    /// Whether the target was left halted (breakpoint hit) or resumed.
    pub resumed: bool,
}

/// A single frame in a backtrace, parsed from `bt yaml` output.
#[derive(Debug)]
pub struct BacktraceFrame {
    pub function_name: String,
    pub source_location: Option<String>,
    pub pc: u32,
    pub local_variables: Option<String>,
}

/// Build the argument vector for a `probe-rs debug` inspection.
fn inspect_args(req: &DebugRequest, commands: &[&str]) -> Vec<OsString> {
    let mut args: Vec<OsString> = vec![
        OsString::from("debug"),
        OsString::from("--probe"),
        OsString::from(&req.probe),
        OsString::from("--chip"),
        OsString::from(&req.chip),
        OsString::from("--non-interactive"),
    ];
    /* The STM32F4 target can hold SWD off depending on the running image, so
     * attach under reset for SWD (probe-rs otherwise fails to connect). */
    if req.protocol == "swd" {
        args.push(OsString::from("--connect-under-reset"));
    }
    if let Some(elf) = &req.elf {
        args.push(elf.clone().into_os_string());
    }
    for cmd in commands {
        args.push(OsString::from("-c"));
        args.push(OsString::from(*cmd));
    }
    args
}

/// Run a single `probe-rs debug` inspection with the given commands.
fn run_inspection(req: &DebugRequest, commands: &[&str]) -> Result<CommandOutput> {
    let args = inspect_args(req, commands);
    let spec = CommandSpec {
        program: req.probe_rs.clone(),
        args,
        cwd: PathBuf::from("."),
        env: Vec::new(),
        timeout: INSPECT_TIMEOUT,
    };
    run(&spec)
}

/// Parse the halt address from probe-rs output.
///
/// Looks for a line like:
/// `Core halted due to a user (debugger client) request @0x08003fcc.`
fn parse_halt_address(output: &str) -> Option<u32> {
    for line in output.lines() {
        if let Some(at) = line.find(" @0x") {
            let hex = line[at + 4..].trim();
            let hex = hex.trim_end_matches('.');
            if let Ok(addr) = u32::from_str_radix(hex, 16) {
                return Some(addr);
            }
        }
    }
    None
}

/// Parse the register listing from probe-rs output.
///
/// Extracts all lines that contain a `0x` hex value and start with an
/// alphanumeric register name (e.g. `R0`, `R15/PC`, `SP`, `xpsr`).
fn parse_registers(output: &str) -> String {
    let mut regs = Vec::new();
    for line in output.lines() {
        let trimmed = line.trim_start();
        if trimmed.contains("0x")
            && trimmed
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphanumeric())
        {
            regs.push(trimmed.to_string());
        }
    }
    regs.join("\n")
}

/// Parse the `info frame` output (may be "Unimplemented").
fn parse_frame(output: &str) -> String {
    for line in output.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("No frame")
            || trimmed.starts_with("Unimplemented")
            || trimmed.contains("frame")
        {
            return trimmed.to_string();
        }
    }
    String::new()
}

/// Parse the `info locals` output.
fn parse_locals(output: &str) -> String {
    for line in output.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("No variables")
            || trimmed.starts_with("Unimplemented")
            || (trimmed.contains('=') && !trimmed.starts_with('R'))
        {
            return trimmed.to_string();
        }
    }
    String::new()
}

/// Parse backtrace YAML into structured frames.
///
/// The YAML is a list of objects. Each frame starts with `- function_name:`.
/// The `pc` field is a nested mapping `pc:\n    U32: <decimal>`.
/// `source_location` and `local_variables` may be `~` (null).
fn parse_backtrace_yaml(yaml_text: &str) -> Vec<BacktraceFrame> {
    let mut frames = Vec::new();
    let mut current_fn = String::new();
    let mut current_loc: Option<String> = None;
    let mut current_pc: Option<u32> = None;
    let mut current_vars: Option<String> = None;
    let mut in_pc_block = false;

    for raw_line in yaml_text.lines() {
        let line = raw_line.trim_end();
        let trimmed = line.trim_start();

        // New frame starts at a list-item line with `function_name:`.
        if trimmed.starts_with("- function_name:") {
            if !current_fn.is_empty() {
                frames.push(BacktraceFrame {
                    function_name: std::mem::take(&mut current_fn),
                    source_location: current_loc.take(),
                    pc: current_pc.take().unwrap_or(0),
                    local_variables: current_vars.take(),
                });
            }
            current_loc = None;
            current_pc = None;
            current_vars = None;
            in_pc_block = false;
            current_fn = trimmed
                .strip_prefix("- function_name:")
                .unwrap_or("")
                .trim()
                .trim_matches('"')
                .to_string();
            continue;
        }

        // A `pc:` line with no inline value opens a nested U32 block.
        if trimmed == "pc:" {
            in_pc_block = true;
            continue;
        }

        // Inside the pc block, the next `U32:` line holds the value.
        if in_pc_block {
            if let Some(u32_val) = trimmed.strip_prefix("U32:") {
                if let Ok(val) = u32_val.trim().parse::<u32>() {
                    current_pc = Some(val);
                }
                in_pc_block = false;
            } else {
                in_pc_block = false;
            }
            continue;
        }

        if let Some(loc) = trimmed.strip_prefix("source_location:") {
            let loc = loc.trim();
            if !loc.is_empty() && loc != "~" {
                current_loc = Some(loc.trim_matches('"').to_string());
            }
            continue;
        }

        if let Some(vars) = trimmed.strip_prefix("local_variables:") {
            let vars = vars.trim();
            if !vars.is_empty() && vars != "~" {
                current_vars = Some(vars.trim_matches('"').to_string());
            }
        }
    }

    if !current_fn.is_empty() {
        frames.push(BacktraceFrame {
            function_name: current_fn,
            source_location: current_loc,
            pc: current_pc.unwrap_or(0),
            local_variables: current_vars,
        });
    }
    frames
}

/// Halt the target and capture a full state snapshot:
/// registers, frame info, local variables, and backtrace.
///
/// The target is resumed after the inspection.
pub fn halt_and_inspect(req: &DebugRequest, work_dir: &Path) -> Result<Inspection> {
    let bt_path = work_dir.join("backtrace.yaml");
    let bt_str = bt_path.to_str().unwrap_or("backtrace.yaml");

    let commands: Vec<String> = vec![
        "break".to_string(),
        "info reg".to_string(),
        "info frame".to_string(),
        "info locals".to_string(),
        format!("bt yaml {bt_str}"),
        "c".to_string(),
    ];
    let cmd_refs: Vec<&str> = commands.iter().map(|s| s.as_str()).collect();
    let output = run_inspection(req, &cmd_refs)?;
    let stdout = &output.stdout;
    let stderr = &output.stderr;

    let resumed = !stderr.contains("Core is running") && !stderr.contains("The target is running");

    let bt_text = std::fs::read_to_string(&bt_path).unwrap_or_default();
    let backtrace = parse_backtrace_yaml(&bt_text);

    Ok(Inspection {
        halt_address: parse_halt_address(stdout),
        registers: parse_registers(stdout),
        frame: parse_frame(stdout),
        locals: parse_locals(stdout),
        backtrace_path: Some(bt_path),
        backtrace,
        resumed,
    })
}

/// Set a breakpoint at a memory address and continue.
///
/// The target runs until it hits the breakpoint.
pub fn break_at(req: &DebugRequest, address: u32) -> Result<CommandOutput> {
    let cmd = format!("break *{address:#x}");
    let commands: Vec<String> = vec![cmd, "c".to_string()];
    let cmd_refs: Vec<&str> = commands.iter().map(|s| s.as_str()).collect();
    run_inspection(req, &cmd_refs)
}

/// Continue execution from the current state.
pub fn continue_running(req: &DebugRequest) -> Result<CommandOutput> {
    let commands: Vec<String> = vec!["c".to_string()];
    let cmd_refs: Vec<&str> = commands.iter().map(|s| s.as_str()).collect();
    run_inspection(req, &cmd_refs)
}

/// Read N 32-bit words from a target memory address.
///
/// Uses `probe-rs read b32 <addr> <count>` which reads from the
/// target's memory while it is running (no halt needed).
pub fn read_memory(req: &DebugRequest, address: u32, words: u32) -> Result<CommandOutput> {
    let mut args: Vec<OsString> = vec![
        OsString::from("read"),
        OsString::from("b32"),
        OsString::from(format!("{address:#x}")),
        OsString::from(words.to_string()),
        OsString::from("--probe"),
        OsString::from(&req.probe),
        OsString::from("--chip"),
        OsString::from(&req.chip),
        OsString::from("--non-interactive"),
    ];
    if req.protocol == "swd" {
        args.push(OsString::from("--connect-under-reset"));
    }
    let spec = CommandSpec {
        program: req.probe_rs.clone(),
        args,
        cwd: PathBuf::from("."),
        env: Vec::new(),
        timeout: INSPECT_TIMEOUT,
    };
    run(&spec)
}

/// Format a full inspection result for display.
pub fn format_inspection(insp: &Inspection) -> String {
    let mut out = String::new();
    if let Some(addr) = insp.halt_address {
        out.push_str(&format!("Halted at {addr:#010x}\n\n"));
    }
    if !insp.registers.is_empty() {
        out.push_str("Registers:\n");
        out.push_str(&insp.registers);
        out.push_str("\n\n");
    }
    if !insp.frame.is_empty() {
        out.push_str("Frame: ");
        out.push_str(&insp.frame);
        out.push('\n');
    }
    if !insp.locals.is_empty() {
        out.push_str("Locals: ");
        out.push_str(&insp.locals);
        out.push('\n');
    }
    if !insp.backtrace.is_empty() {
        out.push_str("Backtrace:\n");
        for (i, frame) in insp.backtrace.iter().enumerate() {
            out.push_str(&format!("  #{i} {} @ {:#x}", frame.function_name, frame.pc));
            if let Some(loc) = &frame.source_location {
                out.push_str(&format!(" ({loc})"));
            }
            if let Some(vars) = &frame.local_variables {
                out.push_str(&format!(" vars={vars}"));
            }
            out.push('\n');
        }
    } else {
        out.push_str("Backtrace: (none or unavailable)\n");
    }
    out.push_str(&format!("Target resumed: {}\n", insp.resumed));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_probe_uses_manifest_serial_when_available() {
        let root = std::env::temp_dir().join(format!("rtos-debug-test-{}", std::process::id()));
        let board = root.join("boards/stm32f4discovery");
        std::fs::create_dir_all(&board).unwrap();
        std::fs::write(
            board.join("board.toml"),
            r#"
id = "stm32f4discovery"
[serial]
transport = "usb-uart"
baud = 115200
port_selector = ""
[debug]
probe = "0483:3748"
probe_serial = "52C3BF6D"
chip = "STM32F407VGTx"
protocol = "swd"
"#,
        )
        .unwrap();
        let request = DebugRequest::for_board(
            &root,
            Board::Stm32f4Discovery,
            PathBuf::from("probe-rs"),
            None,
        )
        .unwrap();
        assert_eq!(request.probe, "0483:3748:52C3BF6D");
        assert_eq!(request.chip, "STM32F407VGTx");
        std::fs::remove_dir_all(&root).ok();
    }
}
