//! Minimal USB-TMC/SCPI client for a Rigol DS1000Z-series oscilloscope.
//!
//! The Linux `usbtmc` kernel driver exposes the instrument as a character
//! device; a SCPI query is a write of the command followed by a blocking read
//! of the reply. Reads run on a worker thread so a silent instrument cannot
//! hang the CLI. This first piece is read-only (identity); channel measurements
//! and power/timing use are added later.
use crate::error::{Error, Result};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::mpsc,
    time::Duration,
};

/// Default USB-TMC device node created by the `usbtmc` kernel driver.
pub const DEFAULT_DEVICE: &str = "/dev/usbtmc0";
/// Replies are short; a longer wait means the instrument or wiring is wrong.
const READ_TIMEOUT: Duration = Duration::from_secs(3);

/// Open SCPI connection to an instrument on a USB-TMC character device.
#[derive(Debug)]
pub struct Scope {
    file: File,
    path: PathBuf,
}

impl Scope {
    /// Open the USB-TMC device at `path`.
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|e| Error::Io(format!("open {}: {e}", path.display())))?;
        Ok(Self { file, path })
    }

    /// Open the default USB-TMC device node.
    pub fn open_default() -> Result<Self> {
        Self::open(DEFAULT_DEVICE)
    }

    /// Device node this connection uses.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Send one SCPI query and return the trimmed single-line reply.
    pub fn query(&mut self, command: &str) -> Result<String> {
        let mut line = command.trim_end().to_string();
        if line.is_empty() {
            return Err(Error::Invalid("empty SCPI command".into()));
        }
        line.push('\n');
        self.file
            .write_all(line.as_bytes())
            .map_err(|e| Error::Io(format!("scope write: {e}")))?;
        let _ = self.file.flush();
        self.read_reply(READ_TIMEOUT)
    }

    /// Identification string from `*IDN?`.
    pub fn identify(&mut self) -> Result<String> {
        self.query("*IDN?")
    }

    /// Read one Rigol measurement item (e.g. `vpp`, `vavg`, `vrms`, `freq`,
    /// `per`, `vmax`, `vmin`) for a channel. A channel with no valid signal
    /// returns the overflow sentinel and is reported as an error.
    pub fn measure(&mut self, channel: u8, item: &str) -> Result<f64> {
        let item = item.to_ascii_uppercase();
        let reply = self.query(&format!(":MEAS:{item}? CHAN{channel}"))?;
        parse_measurement(&item, &reply)
    }

    fn read_reply(&mut self, timeout: Duration) -> Result<String> {
        let mut file = self
            .file
            .try_clone()
            .map_err(|e| Error::Io(format!("scope clone: {e}")))?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut buf = [0u8; 512];
            let outcome = match file.read(&mut buf) {
                Ok(n) => Ok(String::from_utf8_lossy(&buf[..n]).trim().to_string()),
                Err(e) => Err(Error::Io(format!("scope read: {e}"))),
            };
            let _ = tx.send(outcome);
        });
        match rx.recv_timeout(timeout) {
            Ok(outcome) => outcome,
            Err(_) => Err(Error::Timeout(format!(
                "scope: no reply within {}s",
                timeout.as_secs()
            ))),
        }
    }
}

/// Rigol returns `9.9E37` when a measurement item has no valid result.
const MEASUREMENT_OVERFLOW: f64 = 9.0e37;

fn parse_measurement(item: &str, reply: &str) -> Result<f64> {
    let value = reply
        .trim()
        .parse::<f64>()
        .map_err(|_| Error::Protocol(format!("scope: cannot parse {item} reply {reply:?}")))?;
    if value.abs() >= MEASUREMENT_OVERFLOW {
        return Err(Error::Invalid(format!(
            "{item}: no valid measurement (instrument reports overflow)"
        )));
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn measurement_parsing_handles_values_and_overflow() {
        assert!((parse_measurement("VPP", "8.000000e-02").unwrap() - 0.08).abs() < 1e-9);
        assert!(parse_measurement("FREQ", "9.9E37").is_err());
        assert!(parse_measurement("VPP", "****").is_err());
    }

    #[test]
    fn open_reports_a_missing_device() {
        let err = Scope::open("/dev/definitely-not-a-scope").unwrap_err();
        assert!(format!("{err}").contains("open"), "unexpected: {err}");
    }

    #[test]
    fn query_rejects_an_empty_command() {
        // Construct without touching hardware by opening a scratch file.
        let dir = std::env::temp_dir().join(format!("rtos-scope-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("tmc");
        std::fs::write(&path, b"").unwrap();
        let mut scope = Scope::open(&path).unwrap();
        let err = scope.query("  ").unwrap_err();
        assert!(format!("{err}").contains("empty"), "unexpected: {err}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
