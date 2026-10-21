//! File-backed diagnostics for bounded hardware-tool operations.
use crate::{
    error::{Error, Result},
    process::CommandSpec,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// Unique evidence directory. A started-only manifest never means success.
pub struct ToolRun {
    /// Absolute run directory with raw/ and logs/ children.
    pub directory: PathBuf,
    manifest: Value,
}

fn timestamp() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

impl ToolRun {
    /// Allocate a run without overwriting previous evidence.
    pub fn create(root: &Path, kind: &str, request: Value) -> Result<Self> {
        fs::create_dir_all(root)?;
        let root = root.canonicalize()?;
        for sequence in 0..1000 {
            let id = format!("{kind}-{}-{}-{sequence}", timestamp(), std::process::id());
            let directory = root.join(&id);
            match fs::create_dir(&directory) {
                Ok(()) => {
                    fs::create_dir(directory.join("raw"))?;
                    fs::create_dir(directory.join("logs"))?;
                    let run = Self {
                        directory,
                        manifest: json!({
                            "schema_version": 1, "run_id": id, "kind": kind,
                            "status": "running", "started_at_unix_ms": timestamp(),
                            "request": request, "host_version": env!("CARGO_PKG_VERSION"),
                            "application_readiness": "not_checked", "hardware_support": "unverified"
                        }),
                    };
                    run.save()?;
                    return Ok(run);
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e.into()),
            }
        }
        Err(Error::Io("cannot allocate unique tool run".into()))
    }

    fn save(&self) -> Result<()> {
        let bytes =
            serde_json::to_vec_pretty(&self.manifest).map_err(|e| Error::Io(e.to_string()))?;
        let temporary = self.directory.join("manifest.tmp");
        let mut file = File::create(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(temporary, self.directory.join("manifest.json"))?;
        Ok(())
    }

    /// Attach provenance without copying raw diagnostics into a response.
    pub fn record(&mut self, key: &str, value: Value) -> Result<()> {
        self.manifest[key] = value;
        self.save()
    }

    /// Preserve terminal status, including failed and interrupted operations.
    pub fn finish(&mut self, status: &str, detail: &str) -> Result<()> {
        self.manifest["status"] = json!(status);
        self.manifest["detail"] = json!(detail);
        self.manifest["finished_at_unix_ms"] = json!(timestamp());
        self.save()
    }

    /// Execute without streaming tool chatter into the agent context.
    pub fn execute(&mut self, spec: &CommandSpec) -> Result<()> {
        self.record("command", describe(spec))?;
        let outcome = execute_logged(spec, &self.directory.join("logs"));
        match outcome {
            Ok(()) => self.record("tool_status", json!("exited_successfully")),
            Err(error) => {
                self.finish(
                    if matches!(error, Error::Interrupted(_)) {
                        "interrupted"
                    } else {
                        "failed"
                    },
                    &error.to_string(),
                )?;
                let detail = format!("{error}; logs: {}", self.directory.join("logs").display());
                Err(match error {
                    Error::Interrupted(_) => Error::Interrupted(detail),
                    Error::Timeout(_) => Error::Timeout(detail),
                    _ => Error::Io(detail),
                })
            }
        }
    }
}

/// Render a plan as structured arguments, never a shell command.
pub fn describe(spec: &CommandSpec) -> Value {
    json!({"program": spec.program, "args": spec.args.iter().map(|s| s.to_string_lossy()).collect::<Vec<_>>(),
        "cwd": spec.cwd, "timeout_seconds": spec.timeout.as_secs()})
}

/// Copy an input once, record its SHA-256, and use that snapshot for execution.
pub fn snapshot(source: &Path, destination: &Path) -> Result<Value> {
    let mut input = File::open(source)?;
    let mut output = File::options()
        .create_new(true)
        .write(true)
        .open(destination)?;
    let mut hash = Sha256::new();
    let mut size = 0u64;
    let mut buffer = [0u8; 16384];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
        output.write_all(&buffer[..count])?;
        size += count as u64;
    }
    output.sync_all()?;
    Ok(
        json!({"source": source, "snapshot": destination, "bytes": size,
        "sha256": hash.finalize().iter().map(|byte| format!("{byte:02x}")).collect::<String>()}),
    )
}

/// Child ownership: deadline/error cleanup cannot leave our tool running.
pub(crate) struct ManagedChild(pub Child, bool);

impl ManagedChild {
    pub fn stop(&mut self) {
        if self.1 {
            return;
        }
        self.1 = true;
        #[cfg(unix)]
        // SAFETY: this child was spawned in its own process group by spawn();
        // the negative child PID targets that group, never the caller's group.
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
impl Drop for ManagedChild {
    fn drop(&mut self) {
        self.stop();
    }
}

pub(crate) fn spawn(command: &mut Command) -> Result<ManagedChild> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    Ok(ManagedChild(command.spawn()?, false))
}

fn execute_logged(spec: &CommandSpec, directory: &Path) -> Result<()> {
    if spec.timeout.is_zero() || spec.timeout > Duration::from_secs(86400) {
        return Err(Error::Invalid("timeout must be 1..86400 seconds".into()));
    }
    let cancellation = Cancellation::new()?;
    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .current_dir(&spec.cwd)
        .envs(spec.env.iter().cloned())
        .stdin(Stdio::null())
        .stdout(File::create(directory.join("stdout.log"))?)
        .stderr(File::create(directory.join("stderr.log"))?);
    let mut child = spawn(&mut command)
        .map_err(|e| Error::Io(format!("cannot start {}: {e}", spec.program.display())))?;
    let deadline = Instant::now() + spec.timeout;
    loop {
        cancellation.check()?;
        if let Some(status) = child.0.try_wait()? {
            return if status.success() {
                Ok(())
            } else {
                Err(Error::Io(format!(
                    "{} exited {status}: {}",
                    spec.program.display(),
                    excerpt(&directory.join("stderr.log"))?
                )))
            };
        }
        if Instant::now() >= deadline {
            return Err(Error::Timeout(format!(
                "{} exceeded {} seconds; target state unknown",
                spec.program.display(),
                spec.timeout.as_secs()
            )));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Scoped signal flags: handlers only set an atomic; normal code owns cleanup.
pub(crate) struct Cancellation {
    flag: std::sync::Arc<std::sync::atomic::AtomicBool>,
    #[cfg(unix)]
    handlers: Vec<signal_hook::SigId>,
}
impl Cancellation {
    pub fn new() -> Result<Self> {
        let flag = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        #[cfg(unix)]
        let handlers = [libc::SIGINT, libc::SIGTERM]
            .iter()
            .map(|sig| signal_hook::flag::register(*sig, flag.clone()))
            .collect::<std::io::Result<Vec<_>>>()?;
        Ok(Self {
            flag,
            #[cfg(unix)]
            handlers,
        })
    }
    pub fn check(&self) -> Result<()> {
        if self.flag.load(std::sync::atomic::Ordering::Relaxed) {
            Err(Error::Interrupted(
                "operation cancelled; target state unknown".into(),
            ))
        } else {
            Ok(())
        }
    }
}
impl Drop for Cancellation {
    fn drop(&mut self) {
        #[cfg(unix)]
        for id in &self.handlers {
            signal_hook::low_level::unregister(*id);
        }
    }
}

fn excerpt(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let length = file.metadata()?.len();
    file.seek(SeekFrom::Start(length.saturating_sub(1024)))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes)
        .chars()
        .filter(|c| !c.is_control() || *c == ' ')
        .take(400)
        .collect())
}
