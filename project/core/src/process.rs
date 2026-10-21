//! Bounded process execution with stdout/stderr capture and exit checking.
//! Uses Command with argument arrays; no shell-string interpolation.
use crate::error::{Error, Result};
use std::io::Read;
use std::process::Stdio;
use std::{ffi::OsString, path::PathBuf, time::Duration};

/// Reproducible child-process invocation; record it in the run provenance.
pub struct CommandSpec {
    /// Executable path resolved by tool checker.
    pub program: PathBuf,
    /// Separate arguments, preserving spaces.
    pub args: Vec<OsString>,
    /// Explicit working directory.
    pub cwd: PathBuf,
    /// Explicit environment additions.
    pub env: Vec<(OsString, OsString)>,
    /// Maximum allowed duration.
    pub timeout: Duration,
}

/// Captured output from a finished child process.
pub struct CommandOutput {
    /// The child's exit status.
    pub status: std::process::ExitStatus,
    /// Captured stdout (UTF-8 lossy).
    pub stdout: String,
    /// Captured stderr (UTF-8 lossy).
    pub stderr: String,
}

/// Run a child process with bounded timeout, capturing stdout and stderr.
///
/// Returns `Error::Timeout` if the process exceeds the deadline.
/// Returns `Error::Io` if the process cannot be spawned.
/// The caller inspects `CommandOutput.status` to check for nonzero exit.
pub fn run(command: &CommandSpec) -> Result<CommandOutput> {
    let mut cmd = std::process::Command::new(&command.program);
    cmd.args(&command.args)
        .current_dir(&command.cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in &command.env {
        cmd.env(k, v);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| Error::Io(format!("spawn {}: {e}", command.program.display())))?;

    let deadline = std::time::Instant::now() + command.timeout;
    let stdout_pipe = child.stdout.take().ok_or_else(|| {
        Error::Io(format!(
            "stdout pipe lost for {}",
            command.program.display()
        ))
    })?;
    let stderr_pipe = child.stderr.take().ok_or_else(|| {
        Error::Io(format!(
            "stderr pipe lost for {}",
            command.program.display()
        ))
    })?;

    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let out_tx = std::sync::mpsc::channel();
        let err_tx = std::sync::mpsc::channel();
        let t1 = std::thread::spawn(move || {
            let mut r = stdout_pipe;
            let mut out = Vec::new();
            let _ = r.read_to_end(&mut out);
            let _ = out_tx.0.send(out);
        });
        let t2 = std::thread::spawn(move || {
            let mut r = stderr_pipe;
            let mut err = Vec::new();
            let _ = r.read_to_end(&mut err);
            let _ = err_tx.0.send(err);
        });
        let _ = t1.join();
        let _ = t2.join();
        let out = out_tx.1.recv().unwrap_or_default();
        let err = err_tx.1.recv().unwrap_or_default();
        let _ = tx.send((out, err));
    });

    let status = loop {
        if let Ok(Some(status)) = child.try_wait() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(Error::Timeout(format!(
                "process {} exceeded {}ms timeout",
                command.program.display(),
                command.timeout.as_millis()
            )));
        }
        std::thread::sleep(Duration::from_millis(50));
    };

    let (stdout, stderr) = rx
        .recv()
        .map_err(|e| Error::Io(format!("output reader thread failed: {e}")))?;

    Ok(CommandOutput {
        status,
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

/// Run a child process with inherited stdio, blocking until it exits.
///
/// For interactive tools (e.g. GDB) that must own the terminal.
/// The spec's `timeout` is ignored; the session ends when the child exits.
/// `CommandOutput.stdout`/`stderr` are empty because output went to the terminal.
pub fn run_interactive(command: &CommandSpec) -> Result<CommandOutput> {
    let mut cmd = std::process::Command::new(&command.program);
    cmd.args(&command.args)
        .current_dir(&command.cwd)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    for (k, v) in &command.env {
        cmd.env(k, v);
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| Error::Io(format!("spawn {}: {e}", command.program.display())))?;
    let status = child
        .wait()
        .map_err(|e| Error::Io(format!("wait {}: {e}", command.program.display())))?;
    Ok(CommandOutput {
        status,
        stdout: String::new(),
        stderr: String::new(),
    })
}

/// Run a child process and require a zero exit status.
///
/// On nonzero exit, the captured stdout/stderr are included in the error message.
pub fn run_success(command: &CommandSpec) -> Result<CommandOutput> {
    let output = run(command)?;
    if !output.status.success() {
        return Err(Error::Io(format!(
            "process {} exited with {}; stderr: {}",
            command.program.display(),
            output.status,
            output
                .stderr
                .lines()
                .take(20)
                .collect::<Vec<_>>()
                .join("\n")
        )));
    }
    Ok(output)
}
