//! Bounded MI transport; full raw records stay on disk, not in tool replies.
use super::mi::{self, Record};
use crate::{
    error::{Error, Result},
    tooling::{spawn, Cancellation, ManagedChild},
};
use std::{
    fs::File,
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver, TryRecvError},
    time::Instant,
};

pub(super) struct Transport {
    _child: ManagedChild,
    stdin: ChildStdin,
    records: Option<Receiver<Result<Record>>>,
    reader: Option<std::thread::JoinHandle<()>>,
    commands: File,
    cancellation: Cancellation,
}

impl Transport {
    pub fn start(executable: &Path, logs: &Path) -> Result<Self> {
        let cancellation = Cancellation::new()?;
        let mut command = Command::new(executable);
        command
            .args([
                "--nx",
                "--nh",
                "--quiet",
                "--interpreter=mi2",
                "-iex",
                "set auto-load off",
                "-iex",
                "set debuginfod enabled off",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(File::create(logs.join("stderr.log"))?);
        let mut child = spawn(&mut command)?;
        let stdin = child
            .0
            .stdin
            .take()
            .ok_or_else(|| Error::Io("missing GDB stdin".into()))?;
        let stdout = child
            .0
            .stdout
            .take()
            .ok_or_else(|| Error::Io("missing GDB stdout".into()))?;
        let mut log = File::create(logs.join("mi.log"))?;
        let (sender, records) = mpsc::sync_channel(32);
        let reader_thread = std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let outcome = (|| -> Result<()> {
                loop {
                    let mut line = Vec::new();
                    loop {
                        let buffer = reader.fill_buf()?;
                        if buffer.is_empty() {
                            return Err(Error::Io("GDB disconnected".into()));
                        }
                        let length = buffer
                            .iter()
                            .position(|b| *b == b'\n')
                            .map_or(buffer.len(), |n| n + 1);
                        let ended = buffer[length - 1] == b'\n';
                        log.write_all(&buffer[..length])?;
                        if line.len() + length > 65536 {
                            reader.consume(length);
                            return Err(Error::Protocol(
                                "GDB record exceeds 64 KiB; see mi.log".into(),
                            ));
                        }
                        line.extend_from_slice(&buffer[..length]);
                        reader.consume(length);
                        if ended {
                            break;
                        }
                    }
                    if let Some(record) = mi::parse(&String::from_utf8_lossy(&line))? {
                        if sender.send(Ok(record)).is_err() {
                            return Ok(());
                        }
                    }
                }
            })();
            if let Err(error) = outcome {
                let _ = sender.send(Err(error));
            }
            // Keep all remaining bytes even after parser limits or receiver shutdown.
            let _ = std::io::copy(&mut reader, &mut log);
            let _ = log.sync_all();
        });
        Ok(Self {
            _child: child,
            stdin,
            records: Some(records),
            reader: Some(reader_thread),
            commands: File::create(logs.join("commands.log"))?,
            cancellation,
        })
    }

    pub fn send(&mut self, command: &str) -> Result<()> {
        writeln!(self.commands, "{command}")?;
        writeln!(self.stdin, "{command}")?;
        self.stdin.flush()?;
        Ok(())
    }

    pub fn try_record(&self) -> Result<Option<Record>> {
        match self
            .records
            .as_ref()
            .ok_or_else(|| Error::Io("GDB session closed".into()))?
            .try_recv()
        {
            Ok(record) => record.map(Some),
            Err(TryRecvError::Empty) => Ok(None),
            Err(TryRecvError::Disconnected) => Err(Error::Io("GDB reader disconnected".into())),
        }
    }

    pub fn record(&self, deadline: Instant) -> Result<Record> {
        loop {
            self.cancellation.check()?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(Error::Timeout(
                    "GDB action timed out; session aborted, target state unknown".into(),
                ));
            }
            match self
                .records
                .as_ref()
                .ok_or_else(|| Error::Io("GDB session closed".into()))?
                .recv_timeout(remaining.min(std::time::Duration::from_millis(50)))
            {
                Ok(record) => return record,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(Error::Io("GDB reader disconnected".into()))
                }
            }
        }
    }
}

impl Drop for Transport {
    fn drop(&mut self) {
        self._child.stop();
        self.records.take(); // Unblock a sender before joining the log-drain thread.
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}
