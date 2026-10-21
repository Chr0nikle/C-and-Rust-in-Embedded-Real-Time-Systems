//! Host owns serial lifecycle, raw capture, framing, timeouts and run identity validation.
//! Implements the protocol v1 state machine with bounded frames.
use crate::error::{Error, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::time::Duration;

/// Replaceable transport for real UART and test fixtures. Board reset is a separate action.
pub trait Transport {
    /// Read bytes with a finite deadline; distinguish timeout, EOF and device loss.
    fn read(&mut self, bytes: &mut [u8], timeout: Duration) -> Result<usize>;
    /// Send a complete bounded control record with a deadline.
    fn write(&mut self, bytes: &[u8], timeout: Duration) -> Result<()>;
}

/// Serial transport over an OS device path, using the `serialport` crate.
///
/// `serial` 0.4 was replaced because `serial-unix` passes the asm-generic
/// `B115200` constant to `cfsetspeed`, which this host's glibc interprets as a
/// numeric rate (4800 baud on the CP2102). `serialport` applies the baud with
/// the correct numeric/BOTHER path.
pub struct SerialTransport {
    port: Box<dyn serialport::SerialPort>,
}

impl SerialTransport {
    /// Open a serial port at the given baud rate and set the read timeout.
    pub fn open(port_path: &std::path::Path, baud: u32, timeout: Duration) -> Result<Self> {
        let name = port_path
            .to_str()
            .ok_or_else(|| Error::Io(format!("non-UTF-8 serial path: {}", port_path.display())))?;
        let port = serialport::new(name, baud)
            .timeout(timeout)
            .open()
            .map_err(|e| Error::Io(format!("open {}: {e}", port_path.display())))?;
        Ok(Self { port })
    }
}

impl Transport for SerialTransport {
    fn read(&mut self, bytes: &mut [u8], timeout: Duration) -> Result<usize> {
        use std::io::Read;
        self.port
            .set_timeout(timeout)
            .map_err(|e| Error::Io(format!("set timeout: {e}")))?;
        self.port
            .read(bytes)
            .map_err(|e| Error::Io(format!("serial read: {e}")))
    }

    fn write(&mut self, bytes: &[u8], _timeout: Duration) -> Result<()> {
        use std::io::Write;
        // Pace host -> device control frames one byte at a time. A slow
        // USB-CDC console (originally the now-out-of-scope Tock rp2350 driver,
        // which completed a read only when its buffer was full and had just a
        // one-byte holding register) can overrun on a back-to-back frame before
        // the app re-arms its read. One byte time at 115200 baud is ~0.87 ms;
        // this delay only affects the few host -> device frames
        // (hello/start/cancel) and not the device -> host measurement stream,
        // so measured timings are unaffected.
        const INTER_BYTE_DELAY: Duration = Duration::from_millis(1);
        for (i, byte) in bytes.iter().enumerate() {
            self.port
                .write_all(std::slice::from_ref(byte))
                .map_err(|e| Error::Io(format!("serial write: {e}")))?;
            self.port
                .flush()
                .map_err(|e| Error::Io(format!("serial flush: {e}")))?;
            if i + 1 < bytes.len() {
                std::thread::sleep(INTER_BYTE_DELAY);
            }
        }
        Ok(())
    }
}

// ── Protocol v1 frame types ──────────────────────────────────────────────────

/// A single protocol v1 frame. Only `protocol == 1` frames with a recognized
/// `type` enter the session decoder.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Frame {
    /// Host → device: handshake with a fresh opaque challenge token.
    Hello { challenge: String },
    /// Device → host: identity and capability advertisement.
    /// Sent unsolicited at boot and as a response to `hello`.
    Ready {
        board: String,
        rtos: String,
        kernel: String,
        language: String,
        build_id: String,
        application: String,
        cases: Vec<u32>,
        #[serde(default)]
        challenge: Option<String>,
    },
    /// Host → device: request to begin a benchmark run.
    Start {
        run_id: String,
        build_id: String,
        cases: Vec<u32>,
        repetitions: u32,
        warmup: u32,
    },
    /// Device → host: acknowledgment that a run has begun.
    Started { run_id: String, build_id: String },
    /// Device → host: one measurement sample.
    Measurement {
        run_id: String,
        case: u32,
        index: u32,
        ticks: u64,
        ticks_per_second: u64,
        /// Protocol v2 optional discriminator within a case: payload bytes for
        /// case 18, offered load level for case 23. Absent/zero for plain cases.
        #[serde(default)]
        variant: u32,
    },
    /// Device → host: run finished with per-case sample counts.
    Complete {
        run_id: String,
        per_case_counts: Vec<u32>,
        /// Runtime minimum-ever-free stack for the benchmark task, bytes
        /// (protocol v1 optional; absent when the adapter cannot measure it).
        #[serde(default)]
        stack_high_water_bytes: Option<u64>,
        /// Minimum ever free RTOS heap at run end, bytes (protocol v1 optional).
        #[serde(default)]
        heap_min_free_bytes: Option<u64>,
        /// Free heap at run end, bytes (protocol v1 optional). Used where the
        /// kernel does not track a true minimum-ever figure.
        #[serde(default)]
        heap_free_bytes: Option<u64>,
        /// Allocator metadata + size-rounding overhead per allocation, bytes
        /// (protocol v1 optional; absent when the adapter cannot measure it).
        #[serde(default)]
        heap_overhead_bytes: Option<u64>,
        /// Kernel per-task RAM overhead (TCB + allocator metadata) beyond the
        /// task's stack, bytes (protocol v1 optional).
        #[serde(default)]
        per_task_ram_overhead_bytes: Option<u64>,
        /// Live tasks sampled for the per-task stack aggregate (protocol v1
        /// optional).
        #[serde(default)]
        task_count: Option<u32>,
        /// Tightest (smallest) free stack across sampled tasks, bytes = the
        /// worst per-task stack headroom (protocol v1 optional).
        #[serde(default)]
        worst_stack_headroom_bytes: Option<u64>,
        /// Largest free stack across sampled tasks, bytes (protocol v1 optional).
        #[serde(default)]
        best_stack_headroom_bytes: Option<u64>,
        status: String,
    },
    /// Either direction: an error condition.
    Error {
        #[serde(default)]
        run_id: Option<String>,
        code: String,
        message: String,
    },
    /// Host → device: cancel the active run.
    Cancel { run_id: String },
}

/// Maximum buffered bytes for one line before it is dropped as malformed.
/// Frames are limited to 1024 bytes, so this leaves headroom for boot text.
const MAX_LINE_BYTES: usize = 4096;

/// Emit a throttled live progress line to stderr every N received samples, so a
/// long benchmark run can be watched without dumping all ~27k measurements.
const LIVE_EVERY: u32 = 100;

/// Serialize a frame to a JSON line (one object per newline).
pub fn encode_frame(frame: &Frame) -> Result<Vec<u8>> {
    // Protocol v1 frames carry `protocol: 1`; the tagged enum has no such field,
    // so inject it. FreeRTOS firmware ignores frames without it.
    let mut value =
        serde_json::to_value(frame).map_err(|e| Error::Protocol(format!("encode frame: {e}")))?;
    if let Some(object) = value.as_object_mut() {
        object.insert("protocol".into(), serde_json::Value::from(1));
    }
    let mut buf =
        serde_json::to_vec(&value).map_err(|e| Error::Protocol(format!("encode frame: {e}")))?;
    buf.push(b'\n');
    if buf.len() > 1024 {
        return Err(Error::Protocol(format!(
            "frame exceeds 1024-byte limit: {} bytes",
            buf.len()
        )));
    }
    Ok(buf)
}

/// Deserialize a single JSON line into a `Frame`.
/// Returns `None` for lines that are not protocol v1 (boot text, unrelated logs).
pub fn decode_line(line: &str) -> Option<Frame> {
    let trimmed = line.trim();
    if trimmed.is_empty() || !trimmed.starts_with('{') {
        return None;
    }
    let parsed: serde_json::Value = serde_json::from_str(trimmed).ok()?;
    if parsed.get("protocol")?.as_u64() != Some(1) {
        return None;
    }
    let frame: Frame = serde_json::from_value(parsed).ok()?;
    Some(frame)
}

// ── Session state machine ───────────────────────────────────────────────────

/// Required states; only Complete can produce successful benchmark exit status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionState {
    /// Waiting for matching board/kernel/application build identity.
    AwaitReady,
    /// Ready received; start request has not yet been acknowledged.
    AwaitStart,
    /// Accept only measurements for the active run and expected case/sample sequence.
    Running,
    /// Completion and sample-count checks passed.
    Complete,
    /// Retain partial evidence and release device access.
    Failed,
}

/// A one-line event recorded during a session, for the host log.
#[derive(Debug, Clone)]
pub struct SessionEvent {
    /// UTC timestamp.
    pub time: String,
    /// Direction: tx, rx, boot, session, or error.
    pub kind: String,
    /// Human-readable description.
    pub data: String,
}

/// One validated measurement sample.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sample {
    /// Case ID this sample belongs to.
    pub case: u32,
    /// Zero-based sample index within the case (restarts per variant).
    pub index: u32,
    /// Raw timer ticks.
    pub ticks: u64,
    /// Variant discriminator (protocol v2); 0 for plain cases, payload bytes for
    /// case 18, load level for case 23.
    pub variant: u32,
}

/// Validated result of one benchmark run.
#[derive(Debug, Clone)]
pub struct RunReport {
    /// Requested case IDs in execution order.
    pub cases: Vec<u32>,
    /// Samples collected per case.
    pub repetitions: u32,
    /// Timer ticks per second reported by the device: the common rate when every
    /// case uses one clock, otherwise the first requested case's rate. Per-case
    /// values are authoritative and always present in `case_rates`.
    pub ticks_per_second: u64,
    /// Timer ticks per second for each requested case ID. A run may mix clocks
    /// (for example a cycle-counter case alongside microsecond-based cases), so
    /// callers must normalize a case with its own entry, not the global value.
    pub case_rates: BTreeMap<u32, u64>,
    /// Samples in received order.
    pub samples: Vec<Sample>,
    /// Runtime memory high-water reported in `complete`, when provided.
    pub memory: Option<RunMemory>,
}

/// Runtime memory high-water reported by the device in `complete`.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct RunMemory {
    /// Minimum-ever-free stack for the benchmark task, bytes.
    pub stack_high_water_bytes: Option<u64>,
    /// Minimum ever free RTOS heap at run end, bytes.
    pub heap_min_free_bytes: Option<u64>,
    /// Free heap at run end, bytes (used where there is no minimum-ever figure).
    pub heap_free_bytes: Option<u64>,
    /// Allocator metadata + rounding overhead per allocation, bytes.
    pub heap_overhead_bytes: Option<u64>,
    /// Kernel per-task RAM overhead (TCB + allocator metadata) beyond the
    /// task's stack, bytes.
    pub per_task_ram_overhead_bytes: Option<u64>,
    /// Live tasks sampled for the per-task stack aggregate.
    pub task_count: Option<u32>,
    /// Tightest free stack across sampled tasks, bytes.
    pub worst_stack_headroom_bytes: Option<u64>,
    /// Largest free stack across sampled tasks, bytes.
    pub best_stack_headroom_bytes: Option<u64>,
}

impl RunReport {
    /// Ticks for one case in received index order.
    pub fn case_samples(&self, case: u32) -> Vec<u64> {
        self.samples
            .iter()
            .filter(|s| s.case == case)
            .map(|s| s.ticks)
            .collect()
    }
}

/// The smoke/benchmark session driver.
///
/// Drives the protocol v1 state machine over any `Transport`:
///   1. boot pump — catch unsolicited `ready` after flash reset
///   2. `hello` — expect `ready` with challenge echoed
///   3. `start` — expect `started` (benchmark) or `error`/`unsupported` (smoke)
///   4. `cancel` — best-effort, log silence
///   5. malformed — expect `error`/`bad_json`
pub struct Session<T: Transport> {
    transport: T,
    build_id: String,
    run_id: String,
    challenge: String,
    state: SessionState,
    events: Vec<SessionEvent>,
    raw: Vec<u8>,
    ready_seen: bool,
    hello_acknowledged: bool,
    advertised_cases: Vec<u32>,
    #[allow(dead_code)]
    start_response: Option<Frame>,
}

impl<T: Transport> Session<T> {
    /// Create a new session over the given transport.
    pub fn new(
        transport: T,
        build_id: impl Into<String>,
        run_id: impl Into<String>,
        challenge: impl Into<String>,
    ) -> Self {
        Self {
            transport,
            build_id: build_id.into(),
            run_id: run_id.into(),
            challenge: challenge.into(),
            state: SessionState::AwaitReady,
            events: Vec::new(),
            raw: Vec::new(),
            ready_seen: false,
            hello_acknowledged: false,
            advertised_cases: Vec::new(),
            start_response: None,
        }
    }

    fn emit(&mut self, kind: &str, data: impl Into<String>) {
        let time = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs().to_string())
            .unwrap_or_default();
        self.events.push(SessionEvent {
            time,
            kind: kind.to_string(),
            data: data.into(),
        });
    }

    /// Read and accumulate raw bytes for `duration`, looking for protocol frames.
    /// Returns the frames decoded from the accumulated buffer.
    fn pump(&mut self, duration: Duration) -> Vec<Frame> {
        let deadline = std::time::Instant::now() + duration;
        let mut line_buf = String::new();
        let mut discarding = false;
        let mut frames = Vec::new();
        let mut buf = [0u8; 512];

        while std::time::Instant::now() < deadline {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                break;
            }
            let n = self.transport.read(&mut buf, remaining).unwrap_or(0);
            if n == 0 {
                continue;
            }
            self.raw.extend_from_slice(&buf[..n]);
            let chunk = String::from_utf8_lossy(&buf[..n]).to_string();
            line_buf.push_str(&chunk);

            if discarding {
                if let Some(nl) = line_buf.find('\n') {
                    line_buf.drain(..=nl);
                    discarding = false;
                } else {
                    line_buf.clear();
                    continue;
                }
            } else if line_buf.len() > MAX_LINE_BYTES && line_buf.find('\n').is_none() {
                self.emit(
                    "dropped",
                    format!("dropped {}-byte line without newline", line_buf.len()),
                );
                line_buf.clear();
                discarding = true;
                continue;
            }

            // Extract complete lines.
            while let Some(nl) = line_buf.find('\n') {
                let line = line_buf[..nl].to_string();
                line_buf.drain(..=nl);
                if let Some(frame) = decode_line(&line) {
                    frames.push(frame);
                } else {
                    self.emit("boot", line.chars().take(200).collect::<String>());
                }
            }
        }

        // Flush any remaining partial line.
        if !line_buf.trim().is_empty() {
            if let Some(frame) = decode_line(&line_buf) {
                frames.push(frame);
            }
        }

        frames
    }

    /// Pump for the boot window, expecting an unsolicited `ready` frame.
    pub fn pump_boot(&mut self, duration: Duration) -> Result<()> {
        self.emit(
            "session",
            format!("boot pump for {}ms", duration.as_millis()),
        );
        for frame in self.pump(duration) {
            if let Frame::Ready {
                build_id, cases, ..
            } = &frame
            {
                if build_id == &self.build_id {
                    self.ready_seen = true;
                    self.advertised_cases = cases.clone();
                    self.state = SessionState::AwaitStart;
                    self.emit("ready", format!("boot ready build_id={build_id}"));
                } else {
                    self.emit(
                        "ready",
                        format!(
                            "boot ready build_id={build_id} (mismatch, expected {})",
                            self.build_id
                        ),
                    );
                }
            }
        }
        Ok(())
    }

    /// Send `hello` and expect a `ready` with the challenge echoed.
    pub fn send_hello(&mut self) -> Result<()> {
        let frame = Frame::Hello {
            challenge: self.challenge.clone(),
        };
        let bytes = encode_frame(&frame)?;
        self.emit("tx", String::from_utf8_lossy(&bytes));
        self.transport.write(&bytes, Duration::from_secs(1))?;

        // Slow-booting targets can take several seconds to reach their read
        // loop and answer; keep the window generous (boot pump is 15 s).
        for frame in self.pump(Duration::from_secs(10)) {
            if let Frame::Ready {
                challenge: Some(c),
                build_id,
                cases,
                ..
            } = &frame
            {
                if c == &self.challenge && build_id == &self.build_id {
                    self.hello_acknowledged = true;
                    self.advertised_cases = cases.clone();
                    self.state = SessionState::AwaitStart;
                    self.emit("ready", format!("hello acknowledged build_id={build_id}"));
                    return Ok(());
                }
                self.emit(
                    "ready",
                    format!("hello response mismatch: challenge={c} build_id={build_id}"),
                );
            }
        }
        Err(Error::Protocol(
            "no matching ready response to hello within timeout".into(),
        ))
    }

    /// Send `start` and collect the response.
    ///
    /// For smoke firmware, expects `error` with code `unsupported`.
    /// For benchmark firmware, expects `started` followed by measurements and `complete`.
    pub fn send_start(
        &mut self,
        cases: Vec<u32>,
        repetitions: u32,
        warmup: u32,
        run_timeout: Duration,
    ) -> Result<Vec<Frame>> {
        let frame = Frame::Start {
            run_id: self.run_id.clone(),
            build_id: self.build_id.clone(),
            cases: cases.clone(),
            repetitions,
            warmup,
        };
        let bytes = encode_frame(&frame)?;
        self.emit("tx", String::from_utf8_lossy(&bytes));
        self.transport.write(&bytes, Duration::from_secs(1))?;

        self.state = SessionState::Running;
        let mut all_frames = Vec::new();
        let mut completed = false;
        let mut errored = false;
        let mut live_samples: u32 = 0;

        let deadline = std::time::Instant::now() + run_timeout;
        let mut buf = [0u8; 512];
        let mut line_buf = String::new();
        let mut discarding = false;

        while std::time::Instant::now() < deadline && !completed && !errored {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                break;
            }
            let n = self.transport.read(&mut buf, remaining).unwrap_or(0);
            if n == 0 {
                continue;
            }
            self.raw.extend_from_slice(&buf[..n]);
            let chunk = String::from_utf8_lossy(&buf[..n]).to_string();
            line_buf.push_str(&chunk);

            if discarding {
                if let Some(nl) = line_buf.find('\n') {
                    line_buf.drain(..=nl);
                    discarding = false;
                } else {
                    line_buf.clear();
                    continue;
                }
            } else if line_buf.len() > MAX_LINE_BYTES && line_buf.find('\n').is_none() {
                self.emit(
                    "dropped",
                    format!("dropped {}-byte line without newline", line_buf.len()),
                );
                line_buf.clear();
                discarding = true;
                continue;
            }

            while let Some(nl) = line_buf.find('\n') {
                let line = line_buf[..nl].to_string();
                line_buf.drain(..=nl);
                if let Some(frame) = decode_line(&line) {
                    self.emit("rx", format!("{:?}", frame));
                    match &frame {
                        Frame::Started { run_id, build_id } => {
                            self.emit("started", format!("run={run_id} build={build_id}"));
                            eprintln!("[uart] run started run={run_id}");
                        }
                        Frame::Measurement { case, index, .. } => {
                            live_samples += 1;
                            if live_samples.is_multiple_of(LIVE_EVERY) {
                                eprintln!(
                                    "[uart] live progress: {live_samples} samples (case={case} sample={index})"
                                );
                            }
                        }
                        Frame::Complete {
                            run_id,
                            per_case_counts,
                            status,
                            ..
                        } => {
                            self.emit(
                                "complete",
                                format!("run={run_id} counts={per_case_counts:?} status={status}"),
                            );
                            eprintln!(
                                "[uart] run complete counts={per_case_counts:?} status={status}"
                            );
                            completed = true;
                        }
                        Frame::Error { code, message, .. } => {
                            self.emit("error", format!("code={code} message={message}"));
                            eprintln!("[uart] run error code={code} message={message}");
                            errored = true;
                        }
                        _ => {}
                    }
                    all_frames.push(frame);
                }
            }
        }

        if !completed && !errored {
            // protocol.md: a host timeout releases resources and marks the run
            // incomplete; ask the device to stop as a best-effort courtesy.
            self.emit(
                "timeout",
                "no complete or error within timeout; sending best-effort cancel",
            );
            let _ = self.send_cancel();
            self.state = SessionState::Failed;
            return Err(Error::Timeout(
                "start response: no complete or error within timeout".into(),
            ));
        }
        Ok(all_frames)
    }

    /// Validate the frames returned by [`Session::send_start`] against the suite.
    ///
    /// Enforces protocol.md: matching run/firmware identity, requested case order,
    /// per-case index sequence, exact sample counts, a fixed timer frequency and
    /// `complete` with `status == "ok"`. Sets the state to `Complete` on success
    /// and `Failed` on any violation.
    pub fn validate_run(
        &mut self,
        frames: &[Frame],
        cases: &[u32],
        repetitions: u32,
    ) -> Result<RunReport> {
        macro_rules! fail {
            ($($arg:tt)*) => {{
                self.state = SessionState::Failed;
                return Err(Error::Protocol(format!($($arg)*)));
            }};
        }

        if cases.is_empty() || repetitions == 0 {
            fail!("benchmark request requires cases and nonzero repetitions");
        }
        let mut started = false;
        let mut case_rates: BTreeMap<u32, u64> = BTreeMap::new();
        let mut samples = Vec::new();
        let mut complete = None;
        let mut run_memory = None;

        for frame in frames {
            match frame {
                Frame::Started { run_id, build_id } => {
                    if run_id != &self.run_id || build_id != &self.build_id {
                        fail!("started identity mismatch: run={run_id} build={build_id}");
                    }
                    if started {
                        fail!("duplicate started frame");
                    }
                    started = true;
                }
                Frame::Measurement {
                    run_id,
                    case,
                    index,
                    ticks,
                    ticks_per_second: frequency,
                    variant,
                } => {
                    if run_id != &self.run_id {
                        fail!("measurement for stale run {run_id}");
                    }
                    if !started {
                        fail!("measurement before started");
                    }
                    if *frequency == 0 {
                        fail!("measurement with zero ticks_per_second");
                    }
                    match case_rates.get(case) {
                        Some(value) if value != frequency => {
                            fail!("timer frequency changed mid-run for case {case}");
                        }
                        Some(_) => {}
                        None => {
                            case_rates.insert(*case, *frequency);
                        }
                    }
                    samples.push(Sample {
                        case: *case,
                        index: *index,
                        ticks: *ticks,
                        variant: *variant,
                    });
                }
                Frame::Complete {
                    run_id,
                    per_case_counts,
                    stack_high_water_bytes,
                    heap_min_free_bytes,
                    heap_free_bytes,
                    heap_overhead_bytes,
                    per_task_ram_overhead_bytes,
                    task_count,
                    worst_stack_headroom_bytes,
                    best_stack_headroom_bytes,
                    status,
                } => {
                    if run_id != &self.run_id {
                        fail!("complete for stale run {run_id}");
                    }
                    if complete.is_some() {
                        fail!("duplicate complete frame");
                    }
                    if stack_high_water_bytes.is_some()
                        || heap_min_free_bytes.is_some()
                        || heap_free_bytes.is_some()
                        || heap_overhead_bytes.is_some()
                        || per_task_ram_overhead_bytes.is_some()
                        || task_count.is_some()
                        || worst_stack_headroom_bytes.is_some()
                        || best_stack_headroom_bytes.is_some()
                    {
                        run_memory = Some(RunMemory {
                            stack_high_water_bytes: *stack_high_water_bytes,
                            heap_min_free_bytes: *heap_min_free_bytes,
                            heap_free_bytes: *heap_free_bytes,
                            heap_overhead_bytes: *heap_overhead_bytes,
                            per_task_ram_overhead_bytes: *per_task_ram_overhead_bytes,
                            task_count: *task_count,
                            worst_stack_headroom_bytes: *worst_stack_headroom_bytes,
                            best_stack_headroom_bytes: *best_stack_headroom_bytes,
                        });
                    }
                    complete = Some((per_case_counts.clone(), status.clone()));
                }
                Frame::Error {
                    run_id,
                    code,
                    message,
                } => {
                    fail!(
                        "device error run={} code={code}: {message}",
                        run_id.as_deref().unwrap_or("-")
                    );
                }
                _ => {}
            }
        }

        if !started {
            fail!("no started frame received");
        }
        let (reported_counts, status) = match complete {
            Some(value) => value,
            None => fail!("no complete frame received"),
        };
        if status != "ok" {
            fail!("complete status={status}, expected ok");
        }
        if reported_counts.len() != cases.len() {
            fail!(
                "complete reports {} cases, expected {}",
                reported_counts.len(),
                cases.len()
            );
        }

        let mut counts = vec![0u32; cases.len()];
        let mut position = 0usize;
        for sample in &samples {
            if position >= cases.len() {
                fail!("more samples than requested cases");
            }
            if sample.case != cases[position] {
                if counts[position] == repetitions
                    && position + 1 < cases.len()
                    && sample.case == cases[position + 1]
                {
                    position += 1;
                } else {
                    fail!(
                        "unexpected case {} at position {position} (expected {})",
                        sample.case,
                        cases[position]
                    );
                }
            }
            if sample.index != counts[position] {
                fail!(
                    "case {} index {} out of order (expected {})",
                    sample.case,
                    sample.index,
                    counts[position]
                );
            }
            counts[position] += 1;
            if counts[position] > repetitions {
                fail!("case {} exceeds {repetitions} samples", sample.case);
            }
        }
        for (i, case) in cases.iter().enumerate() {
            if counts[i] != repetitions {
                fail!(
                    "case {case} collected {} samples, expected {repetitions}",
                    counts[i]
                );
            }
            if reported_counts[i] != counts[i] {
                fail!(
                    "case {case} reported count {} != collected {}",
                    reported_counts[i],
                    counts[i]
                );
            }
        }

        self.state = SessionState::Complete;
        self.emit(
            "session",
            format!("run validated: {} samples/case", repetitions),
        );
        for case in cases {
            if !case_rates.contains_key(case) {
                fail!("no measurements received for case {case}");
            }
        }
        let ticks_per_second = case_rates
            .get(&cases[0])
            .copied()
            .ok_or_else(|| Error::Protocol("no measurements received".into()))?;
        Ok(RunReport {
            cases: cases.to_vec(),
            repetitions,
            ticks_per_second,
            case_rates,
            samples,
            memory: run_memory,
        })
    }

    /// Send `cancel` and log the response (silence is acceptable).
    pub fn send_cancel(&mut self) -> Result<()> {
        let frame = Frame::Cancel {
            run_id: self.run_id.clone(),
        };
        let bytes = encode_frame(&frame)?;
        self.emit("tx", String::from_utf8_lossy(&bytes));
        self.transport.write(&bytes, Duration::from_secs(1))?;
        self.pump(Duration::from_secs(2));
        self.emit("cancel", "sent; response logged");
        Ok(())
    }

    /// Send a malformed frame and expect `error`/`bad_json`.
    pub fn send_malformed(&mut self, malformed: &[u8]) -> Result<()> {
        self.emit(
            "tx",
            String::from_utf8_lossy(malformed).replace('\n', "\\n"),
        );
        self.transport.write(malformed, Duration::from_secs(1))?;
        for frame in self.pump(Duration::from_secs(2)) {
            if let Frame::Error { code, .. } = &frame {
                self.emit("error", format!("malformed response code={code}"));
                return Ok(());
            }
        }
        Ok(())
    }

    /// Finalize the session: set final state and return all events + raw bytes.
    pub fn finish(mut self) -> (Vec<SessionEvent>, Vec<u8>, SessionState) {
        if self.state != SessionState::Complete && self.state != SessionState::Failed {
            self.state = SessionState::Failed;
        }
        self.emit("session", "done");
        let events = self.events;
        let raw = self.raw;
        let state = self.state;
        (events, raw, state)
    }

    /// Get a reference to the current state.
    pub fn state(&self) -> SessionState {
        self.state
    }

    /// Case IDs advertised by the device's most recent `ready`.
    pub fn advertised_cases(&self) -> &[u32] {
        &self.advertised_cases
    }

    /// Get a reference to all recorded events.
    pub fn events(&self) -> &[SessionEvent] {
        &self.events
    }
}

// ── Test transport ───────────────────────────────────────────────────────────

/// A scripted transport for unit tests. Returns pre-canned byte chunks.
pub struct ScriptedTransport {
    chunks: Vec<Vec<u8>>,
    index: usize,
    offset: usize,
    write_log: Vec<Vec<u8>>,
    #[allow(dead_code)]
    default_timeout: Duration,
}

impl ScriptedTransport {
    pub fn new(chunks: Vec<Vec<u8>>) -> Self {
        Self {
            chunks,
            index: 0,
            offset: 0,
            write_log: Vec::new(),
            default_timeout: Duration::from_millis(50),
        }
    }

    pub fn written(&self) -> &[Vec<u8>] {
        &self.write_log
    }
}

impl Transport for ScriptedTransport {
    fn read(&mut self, bytes: &mut [u8], _timeout: Duration) -> Result<usize> {
        while self.index < self.chunks.len() {
            let chunk = &self.chunks[self.index];
            if self.offset < chunk.len() {
                let n = std::cmp::min(chunk.len() - self.offset, bytes.len());
                bytes[..n].copy_from_slice(&chunk[self.offset..self.offset + n]);
                self.offset += n;
                return Ok(n);
            }
            self.index += 1;
            self.offset = 0;
        }
        Ok(0)
    }

    fn write(&mut self, bytes: &[u8], _timeout: Duration) -> Result<()> {
        self.write_log.push(bytes.to_vec());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn new_session() -> Session<ScriptedTransport> {
        Session::new(ScriptedTransport::new(vec![]), "bench-1", "run-1", "chal-1")
    }

    fn started() -> Frame {
        Frame::Started {
            run_id: "run-1".into(),
            build_id: "bench-1".into(),
        }
    }

    fn measurement(case: u32, index: u32, ticks: u64) -> Frame {
        measurement_hz(case, index, ticks, 1_000_000)
    }

    fn measurement_hz(case: u32, index: u32, ticks: u64, ticks_per_second: u64) -> Frame {
        Frame::Measurement {
            run_id: "run-1".into(),
            case,
            index,
            ticks,
            ticks_per_second,
            variant: 0,
        }
    }

    fn complete(counts: Vec<u32>, status: &str) -> Frame {
        Frame::Complete {
            run_id: "run-1".into(),
            per_case_counts: counts,
            stack_high_water_bytes: None,
            heap_min_free_bytes: None,
            heap_free_bytes: None,
            heap_overhead_bytes: None,
            per_task_ram_overhead_bytes: None,
            task_count: None,
            worst_stack_headroom_bytes: None,
            best_stack_headroom_bytes: None,
            status: status.into(),
        }
    }

    #[test]
    fn validate_run_accepts_ordered_suite() {
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement(0, 0, 10),
            measurement(0, 1, 11),
            measurement(1, 0, 20),
            measurement(1, 1, 21),
            complete(vec![2, 2], "ok"),
        ];
        let report = session.validate_run(&frames, &[0, 1], 2).unwrap();
        assert_eq!(session.state(), SessionState::Complete);
        assert_eq!(report.repetitions, 2);
        assert_eq!(report.ticks_per_second, 1_000_000);
        assert_eq!(report.case_samples(1), vec![20, 21]);
        assert_eq!(report.memory, None);
    }

    #[test]
    fn validate_run_accepts_per_case_rates() {
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement_hz(0, 0, 10, 168_000_000),
            measurement_hz(0, 1, 11, 168_000_000),
            measurement_hz(4, 0, 20, 1_000_000),
            measurement_hz(4, 1, 21, 1_000_000),
            complete(vec![2, 2], "ok"),
        ];
        let report = session.validate_run(&frames, &[0, 4], 2).unwrap();
        assert_eq!(report.ticks_per_second, 168_000_000);
        assert_eq!(report.case_rates.get(&0), Some(&168_000_000));
        assert_eq!(report.case_rates.get(&4), Some(&1_000_000));
    }

    #[test]
    fn validate_run_rejects_rate_change_within_a_case() {
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement_hz(0, 0, 10, 168_000_000),
            measurement_hz(0, 1, 11, 1_000_000),
            complete(vec![2], "ok"),
        ];
        assert!(session.validate_run(&frames, &[0], 2).is_err());
    }

    #[test]
    fn validate_run_reports_optional_memory_high_water() {
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement(0, 0, 10),
            Frame::Complete {
                run_id: "run-1".into(),
                per_case_counts: vec![1],
                stack_high_water_bytes: Some(2048),
                heap_min_free_bytes: Some(4096),
                heap_free_bytes: Some(3072),
                heap_overhead_bytes: Some(15),
                per_task_ram_overhead_bytes: Some(96),
                task_count: Some(4),
                worst_stack_headroom_bytes: Some(512),
                best_stack_headroom_bytes: Some(15256),
                status: "ok".into(),
            },
        ];
        let report = session.validate_run(&frames, &[0], 1).unwrap();
        let memory = report.memory.expect("memory high-water present");
        assert_eq!(memory.stack_high_water_bytes, Some(2048));
        assert_eq!(memory.heap_min_free_bytes, Some(4096));
        assert_eq!(memory.heap_free_bytes, Some(3072));
        assert_eq!(memory.heap_overhead_bytes, Some(15));
        assert_eq!(memory.per_task_ram_overhead_bytes, Some(96));
        assert_eq!(memory.task_count, Some(4));
        assert_eq!(memory.worst_stack_headroom_bytes, Some(512));
        assert_eq!(memory.best_stack_headroom_bytes, Some(15256));
    }

    #[test]
    fn complete_frame_accepts_heap_free_without_min_free() {
        // NuttX/Zephyr report free-at-completion, not a minimum-ever figure.
        let line = concat!(
            "{\"protocol\":1,\"type\":\"complete\",\"run_id\":\"run-1\",",
            "\"per_case_counts\":[1],\"status\":\"ok\",",
            "\"stack_high_water_bytes\":1024,\"heap_free_bytes\":8192}"
        );
        match decode_line(line).expect("decodes as protocol v1") {
            Frame::Complete {
                stack_high_water_bytes,
                heap_min_free_bytes,
                heap_free_bytes,
                ..
            } => {
                assert_eq!(stack_high_water_bytes, Some(1024));
                assert_eq!(heap_min_free_bytes, None);
                assert_eq!(heap_free_bytes, Some(8192));
            }
            other => panic!("unexpected frame: {other:?}"),
        }
    }

    #[test]
    fn validate_run_rejects_malformed_runs() {
        // Out-of-order per-case index.
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement(0, 1, 10),
            measurement(0, 0, 11),
            measurement(1, 0, 20),
            measurement(1, 1, 21),
            complete(vec![2, 2], "ok"),
        ];
        assert!(session.validate_run(&frames, &[0, 1], 2).is_err());
        assert_eq!(session.state(), SessionState::Failed);

        // Missing sample: collected counts disagree with the completion counts.
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement(0, 0, 10),
            measurement(0, 1, 11),
            measurement(1, 0, 20),
            complete(vec![2, 2], "ok"),
        ];
        assert!(session.validate_run(&frames, &[0, 1], 2).is_err());

        // Stale run identity.
        let mut session = new_session();
        let frames = vec![
            Frame::Started {
                run_id: "other".into(),
                build_id: "bench-1".into(),
            },
            complete(vec![1, 1], "ok"),
        ];
        assert!(session.validate_run(&frames, &[0, 1], 1).is_err());

        // Non-ok completion status.
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement(0, 0, 10),
            measurement(1, 0, 20),
            complete(vec![1, 1], "error"),
        ];
        assert!(session.validate_run(&frames, &[0, 1], 1).is_err());

        // Device error frame.
        let mut session = new_session();
        let frames = vec![
            started(),
            Frame::Error {
                run_id: Some("run-1".into()),
                code: "busy".into(),
                message: "busy".into(),
            },
        ];
        assert!(session.validate_run(&frames, &[0, 1], 1).is_err());
    }

    #[test]
    fn hello_transitions_to_await_start_and_records_cases() {
        let ready = serde_json::json!({
            "protocol": 1,
            "type": "ready",
            "board": "esp32c6",
            "rtos": "zephyr",
            "kernel": "zephyr-4.4.2",
            "language": "c",
            "build_id": "bench-1",
            "application": "zephyr-c6-bench",
            "cases": [0, 1],
            "challenge": "chal-1",
        });
        let mut bytes = serde_json::to_vec(&ready).unwrap();
        bytes.push(b'\n');
        let mut session = Session::new(
            ScriptedTransport::new(vec![bytes]),
            "bench-1",
            "run-1",
            "chal-1",
        );
        session.send_hello().unwrap();
        assert_eq!(session.state(), SessionState::AwaitStart);
        assert_eq!(session.advertised_cases(), &[0, 1]);
    }

    #[test]
    fn decode_line_rejects_malformed_input() {
        assert!(decode_line("").is_none());
        assert!(decode_line("not json").is_none());
        assert!(decode_line("[]").is_none());
        assert!(decode_line("{\"type\":\"ready\"}").is_none());
        assert!(decode_line("{\"protocol\":2,\"type\":\"ready\"}").is_none());
        assert!(decode_line("{\"protocol\":1,\"type\":\"nope\"}").is_none());
        assert!(decode_line("{\"protocol\":1,\"type\":\"ready\"").is_none());
    }

    #[test]
    fn pump_drops_oversized_line_and_keeps_following_frame() {
        let big = vec![b'x'; 5000];
        let mut ready = serde_json::to_vec(&serde_json::json!({
            "protocol": 1,
            "type": "ready",
            "board": "esp32c6",
            "rtos": "zephyr",
            "kernel": "zephyr-4.4.2",
            "language": "c",
            "build_id": "bench-1",
            "application": "app",
            "cases": [0, 1],
        }))
        .unwrap();
        ready.push(b'\n');
        let mut session = Session::new(
            ScriptedTransport::new(vec![big, b"\n".to_vec(), ready]),
            "bench-1",
            "run-1",
            "chal-1",
        );
        let frames = session.pump(Duration::from_millis(50));
        assert!(frames.iter().any(|f| matches!(f, Frame::Ready { .. })));
    }

    #[test]
    fn encode_frame_includes_protocol_one() {
        let bytes = encode_frame(&Frame::Hello {
            challenge: "c".into(),
        })
        .unwrap();
        let text = String::from_utf8(bytes).unwrap();
        assert!(text.contains("\"protocol\":1"), "{text}");
        assert!(text.contains("\"type\":\"hello\""), "{text}");
        assert!(
            decode_line(&text).is_some(),
            "must round-trip through decoder"
        );
    }

    #[test]
    fn validate_run_rejects_frequency_change() {
        let mut session = new_session();
        let frames = vec![
            started(),
            measurement(0, 0, 10),
            Frame::Measurement {
                run_id: "run-1".into(),
                case: 0,
                index: 1,
                ticks: 11,
                ticks_per_second: 2_000_000,
                variant: 0,
            },
            measurement(1, 0, 20),
            measurement(1, 1, 21),
            complete(vec![2, 2], "ok"),
        ];
        assert!(session.validate_run(&frames, &[0, 1], 2).is_err());
    }

    #[test]
    fn start_timeout_sends_best_effort_cancel() {
        let mut session =
            Session::new(ScriptedTransport::new(vec![]), "bench-1", "run-1", "chal-1");
        let err = session
            .send_start(vec![0, 1], 1, 0, Duration::from_millis(10))
            .unwrap_err();
        assert!(matches!(err, Error::Timeout(_)));
        assert_eq!(session.state(), SessionState::Failed);
        let wrote_cancel = session
            .transport
            .write_log
            .iter()
            .any(|w| String::from_utf8_lossy(w).contains("\"type\":\"cancel\""));
        assert!(wrote_cancel, "timeout must send a best-effort cancel");
    }
}
