//! Bounded raw UART monitor: dump serial bytes to stdout and optionally a file.
//! No protocol framing; intended for boot logs and free-running diagnostics.
use crate::error::{Error, Result};
use crate::uart::{SerialTransport, Transport};
use std::fs::File;
use std::io::Write;
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// Per-read poll interval.
const READ_TIMEOUT: Duration = Duration::from_millis(100);

/// Options for a bounded raw UART monitor session.
pub struct MonitorOptions {
    /// Serial port path to open.
    pub port: PathBuf,
    /// Baud rate.
    pub baud: u32,
    /// Duration in seconds; `0` means run until the port closes.
    pub seconds: u64,
    /// Print bytes as a hex dump instead of raw text.
    pub hex: bool,
    /// Optional file to tee the capture to.
    pub file: Option<PathBuf>,
}

/// Open the port and dump raw bytes until the duration elapses.
///
/// Bytes are printed to stdout as they arrive (hex or text) and, when
/// `file` is set, teed to that file. Returns after the deadline; a
/// zero `seconds` value runs until the read path reports EOF.
pub fn monitor(opts: &MonitorOptions) -> Result<()> {
    let mut transport = SerialTransport::open(&opts.port, opts.baud, READ_TIMEOUT)?;
    let mut file = match &opts.file {
        Some(path) => Some(File::create(path).map_err(Error::from)?),
        None => None,
    };
    let deadline = if opts.seconds == 0 {
        None
    } else {
        Some(Instant::now() + Duration::from_secs(opts.seconds))
    };

    let mut buf = [0u8; 1024];
    let mut total = 0usize;
    loop {
        if let Some(d) = deadline {
            let remaining = d.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
        }
        // A poll timeout (or transient read error) means "no data this tick";
        // keep polling until the deadline. Matches the protocol read loops.
        let n = transport.read(&mut buf, READ_TIMEOUT).unwrap_or(0);
        if n == 0 {
            continue;
        }
        total += n;
        if opts.hex {
            let mut line = String::with_capacity(n * 3);
            for b in &buf[..n] {
                line.push_str(&format!("{b:02x} "));
            }
            println!("{}", line.trim_end());
        } else {
            print!("{}", String::from_utf8_lossy(&buf[..n]));
            std::io::stdout().flush().ok();
        }
        if let Some(f) = file.as_mut() {
            f.write_all(&buf[..n]).map_err(Error::from)?;
        }
    }
    if let Some(f) = file.as_mut() {
        f.flush().ok();
        f.sync_all().ok();
    }
    eprintln!(
        "monitor: {} bytes captured from {}",
        total,
        opts.port.display()
    );
    Ok(())
}
