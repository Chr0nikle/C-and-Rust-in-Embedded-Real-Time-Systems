//! Persistent GDB/MI sessions with explicit execution control and bounded replies.
mod input;
mod mi;
mod transport;
use crate::{
    error::{Error, Result},
    flash,
    tooling::{self, ToolRun},
};
use serde_json::{json, Value};
use std::{
    io::{BufRead, Read, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use transport::Transport;

/// GDB binary and a server already bound to an explicitly selected MCU/probe.
pub struct GdbRequest {
    pub executable: PathBuf,
    pub elf: PathBuf,
    pub mcu: String,
    pub endpoint: String,
    pub timeout: Duration,
}

/// One session owns one GDB process; state is never reconstructed from a file.
pub struct GdbSession {
    transport: Transport,
    token: u64,
    state: &'static str,
    stop: Value,
    timeout: Duration,
}

impl GdbSession {
    fn connect(req: &GdbRequest, logs: &Path) -> Result<Self> {
        let mut session = Self {
            transport: Transport::start(&req.executable, logs)?,
            token: 0,
            state: "unknown",
            stop: Value::Null,
            timeout: req.timeout,
        };
        for command in [
            "-gdb-set pagination off",
            "-gdb-set confirm off",
            "-gdb-set mi-async on",
            "-gdb-set print elements 32",
            "-gdb-set print repeats 8",
            "-gdb-set may-call-functions off",
        ] {
            session.request(command, false)?;
        }
        session.request(
            &format!(
                "-file-exec-and-symbols {}",
                quote(&req.elf.to_string_lossy())
            ),
            false,
        )?;
        session.request(&format!("-target-select remote {}", req.endpoint), false)?;
        // Remote attach can halt the target. Determine its state from GDB, not text guesses.
        let threads = session.request("-thread-info", false)?;
        if threads["threads"]
            .as_array()
            .is_some_and(|items| !items.is_empty() && items.iter().all(|v| v["state"] == "stopped"))
        {
            session.state = "stopped";
        }
        Ok(session)
    }

    fn observe(&mut self, record: &mi::Record) {
        if record.kind == b'*' {
            match record.class.as_str() {
                "stopped" => {
                    self.state = "stopped";
                    self.stop = record.fields.clone();
                }
                "running" => self.state = "running",
                _ => (),
            }
        }
        if record.kind == b'=' && record.class == "thread-group-exited" {
            self.state = "exited";
        }
    }

    fn request(&mut self, command: &str, wait_stop: bool) -> Result<Value> {
        // Drain preceding asynchronous events before assigning the new command token.
        self.drain()?;
        self.token += 1;
        let token = self.token;
        self.transport.send(&format!("{token}{command}"))?;
        let deadline = Instant::now() + self.timeout;
        let mut result = None;
        let mut stopped = false;
        loop {
            let record = match self.transport.record(deadline) {
                Ok(record) => record,
                Err(error) => {
                    self.state = "unknown";
                    return Err(error);
                }
            };
            self.observe(&record);
            stopped |= record.kind == b'*' && record.class == "stopped";
            if record.kind == b'^' && record.token == Some(token) {
                if record.class == "error" {
                    return Err(Error::Debug(
                        record.fields["msg"]
                            .as_str()
                            .unwrap_or("GDB command failed")
                            .chars()
                            .take(400)
                            .collect(),
                    ));
                }
                if record.class == "running" && !stopped {
                    self.state = "running";
                }
                result = Some(record.fields);
            }
            if result.is_some() && (!wait_stop || stopped) {
                return Ok(result.unwrap_or(Value::Null));
            }
        }
    }

    fn drain(&mut self) -> Result<()> {
        loop {
            match self.transport.try_record() {
                Ok(Some(record)) => self.observe(&record),
                Ok(None) => return Ok(()),
                Err(error) => {
                    self.state = "unknown";
                    return Err(error);
                }
            }
        }
    }

    /// Execute one small action. Execution commands are never implicit in inspection.
    pub fn action(&mut self, line: &str) -> Result<Value> {
        let line = line.trim();
        let (action, argument) = line.split_once(' ').unwrap_or((line, ""));
        let argument = argument.trim();
        self.drain()?;
        let (command, wait, needs_stop) = match action {
            "step" => ("-exec-step".into(), true, true),
            "next" => ("-exec-next".into(), true, true),
            "stepi" => ("-exec-step-instruction".into(), true, true),
            "continue" => ("-exec-continue".into(), false, true),
            "interrupt" => (
                "-exec-interrupt --all".into(),
                self.state != "stopped",
                false,
            ),
            "backtrace" => ("-stack-list-frames 0 15".into(), false, true),
            "locals" => ("-stack-list-locals --simple-values".into(), false, true),
            "registers" => ("-data-list-register-values x".into(), false, true),
            "break" if !argument.is_empty() => {
                (format!("-break-insert -- {}", quote(argument)), false, true)
            }
            "delete" if argument.parse::<u32>().is_ok() => {
                (format!("-break-delete {argument}"), false, true)
            }
            "print" if !argument.is_empty() => (
                format!("-data-evaluate-expression {}", quote(argument)),
                false,
                true,
            ),
            "read" => (memory_command(argument)?, false, true),
            "detach" => ("-target-detach".into(), false, false),
            "status" => return Ok(json!({"state": self.state, "last_stop": self.stop})),
            "help" => {
                return Ok(
                    json!({"actions": "status | break LOCATION | delete ID | step | next | stepi | continue | interrupt | backtrace | locals | registers | print EXPR | read HEX BYTES | detach | quit"}),
                )
            }
            _ => return Err(Error::Invalid("unknown/incomplete action; use help".into())),
        };
        if needs_stop && self.state != "stopped" {
            return Err(Error::Debug(format!(
                "action requires stopped target; state={}; use interrupt",
                self.state
            )));
        }
        if !matches!(action, "break" | "delete" | "print" | "read") && !argument.is_empty() {
            return Err(Error::Invalid("unexpected action arguments".into()));
        }
        let data = self.request(&command, wait)?;
        if action == "detach" {
            self.state = "detached";
        }
        Ok(json!({"action": action, "state": self.state, "result": data, "last_stop": self.stop}))
    }
}

fn quote(text: &str) -> String {
    Value::String(text.to_owned()).to_string()
}

fn memory_command(argument: &str) -> Result<String> {
    let fields: Vec<_> = argument.split_whitespace().collect();
    if fields.len() == 2 {
        if let (Ok(address), Ok(count)) = (
            u64::from_str_radix(fields[0].trim_start_matches("0x"), 16),
            fields[1].parse::<u16>(),
        ) {
            if (1..=256).contains(&count) {
                return Ok(format!("-data-read-memory-bytes 0x{address:x} {count}"));
            }
        }
    }
    Err(Error::Invalid(
        "read requires HEX_ADDRESS and 1..256 bytes".into(),
    ))
}

/// Validate inputs without opening a debug connection (also used by --plan).
pub fn validate(req: &GdbRequest) -> Result<()> {
    let (chip, _) = flash::target(&req.mcu)?;
    flash::validate_elf(&req.elf, chip)?;
    if req.endpoint.parse::<std::net::SocketAddr>().is_err() {
        return Err(Error::Invalid(
            "GDB endpoint must be IP:port (for example 127.0.0.1:1337)".into(),
        ));
    }
    if req.timeout.is_zero() || req.timeout > Duration::from_secs(300) {
        return Err(Error::Invalid(
            "GDB action timeout must be 1..300 seconds".into(),
        ));
    }
    Ok(())
}

/// Keep GDB alive across newline-delimited actions; emit one bounded JSON reply each.
/// EOF/quit closes the debugger; only explicit detach requests target resumption.
/// Disconnect semantics vary by server, so final target state is reported unknown.
pub fn run_session(
    req: &GdbRequest,
    results: &Path,
    input: &mut dyn BufRead,
    output: &mut dyn Write,
) -> Result<()> {
    validate(req)?;
    let mut run = ToolRun::create(
        results,
        "gdb",
        json!({"mcu": req.mcu, "endpoint": req.endpoint,
        "gdb": req.executable, "elf": req.elf, "timeout_seconds": req.timeout.as_secs()}),
    )?;
    let operation = (|| {
        let cancellation = tooling::Cancellation::new()?;
        let elf = run.directory.join("raw/symbols.elf");
        run.record("artifact", tooling::snapshot(&req.elf, &elf)?)?;
        let snapshot = GdbRequest {
            executable: req.executable.clone(),
            elf,
            mcu: req.mcu.clone(),
            endpoint: req.endpoint.clone(),
            timeout: req.timeout,
        };
        validate(&snapshot)?;
        let mut session = GdbSession::connect(&snapshot, &run.directory.join("logs"))?;
        reply(
            output,
            json!({"status": "connected", "state": session.state, "evidence": run.directory}),
        )?;
        let mut failed = false;
        loop {
            let mut line = String::new();
            let read = (&mut *input).take(4097).read_line(&mut line);
            cancellation.check()?;
            let bytes = read?;
            if bytes > 4096 {
                return Err(Error::Invalid("GDB action exceeds 4096 bytes".into()));
            }
            if bytes == 0 || line.trim() == "quit" {
                break;
            }
            if line.trim().is_empty() {
                continue;
            }
            match session.action(&line) {
                Ok(value) => reply(
                    output,
                    json!({"status": "ok", "data": value, "logs": run.directory.join("logs")}),
                )?,
                Err(error) => {
                    failed = true;
                    reply(
                        output,
                        json!({"status": "failed", "error": error.to_string(), "state": session.state,
                        "logs": run.directory.join("logs")}),
                    )?;
                    if matches!(
                        error,
                        Error::Timeout(_)
                            | Error::Io(_)
                            | Error::Interrupted(_)
                            | Error::Protocol(_)
                    ) {
                        return Err(error);
                    }
                }
            }
        }
        drop(session);
        reply(
            output,
            json!({"status": "closed", "target_state": "unknown_after_disconnect", "evidence": run.directory}),
        )?;
        if failed {
            return Err(Error::Debug(
                "one or more session actions failed; see action replies".into(),
            ));
        }
        Ok(())
    })();
    match operation {
        Ok(()) => run.finish(
            "complete",
            "session closed; target state after disconnect unknown",
        ),
        Err(error) => {
            run.finish(
                if matches!(error, Error::Interrupted(_)) {
                    "interrupted"
                } else {
                    "failed"
                },
                &error.to_string(),
            )?;
            Err(Error::Debug(format!(
                "{error}; evidence: {}",
                run.directory.display()
            )))
        }
    }
}

/// Interactive input with signal-aware waiting on Unix hosts.
pub fn run_stdio_session(req: &GdbRequest, results: &Path) -> Result<()> {
    let mut input = std::io::BufReader::new(input::Stdin::new()?);
    run_session(req, results, &mut input, &mut std::io::stdout().lock())
}

fn reply(output: &mut dyn Write, value: Value) -> Result<()> {
    let text = value.to_string();
    if text.len() <= 8192 {
        writeln!(output, "{text}")?;
    } else {
        writeln!(
            output,
            "{}",
            json!({"status": value["status"], "action": value["data"]["action"],
            "state": value["data"]["state"], "detail_truncated": true, "logs": value["logs"]})
        )?;
    }
    output.flush()?;
    Ok(())
}
