//! Bounded raw digital capture; PWM outputs are never configured or enabled.
use crate::{
    error::{Error, Result},
    process::CommandSpec,
    tooling::ToolRun,
};
use serde::Serialize;
use serde_json::json;
use std::{
    collections::BTreeSet,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    time::Duration,
};

/// Explicit capture settings. Channel wiring/meaning belongs in experiment notes.
#[derive(Clone, Debug, Serialize)]
pub struct CaptureRequest {
    pub connection: String,
    pub channels: Vec<u8>,
    pub samplerate: u64,
    pub samples: u64,
    pub threshold: String,
    pub trigger: Option<String>,
    pub timeout_seconds: u64,
}

/// Device-independent extension point for logic capture command construction.
pub trait LogicAnalyzer {
    fn plan(&self, request: &CaptureRequest, output: &Path) -> Result<CommandSpec>;
}

/// sigrok driver shared by Kingst LA1010/LA2016 family devices.
pub struct Kingst {
    pub executable: PathBuf,
}

/// Validated archive summary; raw overshoot is retained, never silently cropped.
#[derive(Debug)]
pub struct CaptureOutcome {
    pub directory: PathBuf,
    pub requested_samples: u64,
    pub captured_samples: u64,
    pub diagnostics_present: bool,
}

impl LogicAnalyzer for Kingst {
    fn plan(&self, req: &CaptureRequest, output: &Path) -> Result<CommandSpec> {
        let rates = [
            20_000,
            50_000,
            100_000,
            200_000,
            500_000,
            1_000_000,
            2_000_000,
            5_000_000,
            10_000_000,
            20_000_000,
            50_000_000,
            100_000_000,
        ];
        if !rates.contains(&req.samplerate) || req.samples == 0 || req.samples > 10_000_000_000 {
            return Err(Error::Invalid(
                "unsupported samplerate or samples outside 1..10000000000".into(),
            ));
        }
        if !["0.4", "0.6", "0.9", "1.2", "1.4", "2.0", "2.5", "4.0"]
            .contains(&req.threshold.as_str())
        {
            return Err(Error::Invalid("unsupported voltage threshold".into()));
        }
        let channels: BTreeSet<_> = req.channels.iter().copied().collect();
        if channels.is_empty()
            || channels.len() != req.channels.len()
            || channels.iter().any(|c| *c > 15)
        {
            return Err(Error::Invalid(
                "select unique digital channels CH0..CH15; PWM is excluded".into(),
            ));
        }
        let parts: Vec<_> = req.connection.split('.').collect();
        if parts.len() != 2
            || parts
                .iter()
                .any(|s| s.parse::<u16>().map_or(true, |n| n == 0))
        {
            return Err(Error::Invalid(
                "--connection requires explicit USB bus.address from sigrok-cli --scan".into(),
            ));
        }
        let acquisition_seconds = req.samples.div_ceil(req.samplerate);
        if req.timeout_seconds <= acquisition_seconds || req.timeout_seconds > 86400 {
            return Err(Error::Invalid(format!(
                "timeout must exceed acquisition ({acquisition_seconds}s) and be <=86400s"
            )));
        }
        if output.exists() {
            return Err(Error::Invalid(
                "capture output already exists; evidence cannot be overwritten".into(),
            ));
        }
        let channel_names = req
            .channels
            .iter()
            .map(|n| format!("CH{n}"))
            .collect::<Vec<_>>()
            .join(",");
        let mut args = vec![
            "--driver".into(),
            format!("kingst-la2016:conn={}", req.connection).into(),
            "--channels".into(),
            channel_names.into(),
            "--config".into(),
            format!(
                "samplerate={}:voltage_threshold={}-{}",
                req.samplerate, req.threshold, req.threshold
            )
            .into(),
            "--samples".into(),
            req.samples.to_string().into(),
            "--output-file".into(),
            output.as_os_str().into(),
        ];
        if let Some(trigger) = &req.trigger {
            let mut used = BTreeSet::new();
            for item in trigger.split(',') {
                let (channel, edge) = item
                    .split_once('=')
                    .ok_or_else(|| Error::Invalid("trigger syntax: CH0=r,CH1=1".into()))?;
                let index = channel
                    .strip_prefix("CH")
                    .and_then(|s| s.parse::<u8>().ok())
                    .ok_or_else(|| Error::Invalid("trigger requires CH0..CH15".into()))?;
                if !channels.contains(&index)
                    || !used.insert(index)
                    || !["0", "1", "r", "f"].contains(&edge)
                {
                    return Err(Error::Invalid(
                        "trigger must use selected channels once and 0/1/r/f".into(),
                    ));
                }
            }
            args.extend(["--triggers".into(), trigger.into(), "--wait-trigger".into()]);
        }
        Ok(CommandSpec {
            program: self.executable.clone(),
            args,
            cwd: std::env::current_dir()?,
            env: vec![],
            timeout: Duration::from_secs(req.timeout_seconds),
        })
    }
}

/// Capture into an exclusive run directory and verify the session archive.
pub fn capture(
    backend: &dyn LogicAnalyzer,
    req: &CaptureRequest,
    results: &Path,
) -> Result<CaptureOutcome> {
    // Validation before allocating a run; the actual output is always newly owned.
    backend.plan(req, &results.join("capture-plan-only.sr"))?;
    let mut run = ToolRun::create(results, "logic", json!(req))?;
    let output = run.directory.join("raw/capture.sr");
    let outcome = (|| {
        run.execute(&backend.plan(req, &output)?)?;
        let archive = inspect_capture(&output)?;
        let samples = archive.samples;
        if samples < req.samples {
            return Err(Error::Io(format!(
                "incomplete capture: requested at least {}, archive has {samples} samples",
                req.samples
            )));
        }
        if archive.samplerate != req.samplerate
            || archive.channels != req.channels.iter().copied().collect()
        {
            return Err(Error::Io(
                "archive samplerate/channels do not match capture request".into(),
            ));
        }
        let diagnostics = run.directory.join("logs/stderr.log").metadata()?.len() > 0;
        run.record("capture", json!({"path": output, "requested_minimum_samples": req.samples,
            "captured_samples": samples, "overshoot_samples": samples - req.samples,
            "samplerate_hz": archive.samplerate, "channels": archive.channels,
            "nominal_duration_seconds": samples as f64 / archive.samplerate as f64,
            "diagnostics_present": diagnostics, "signal_integrity": "unverified", "protocol": "raw-digital"}))?;
        run.finish(
            "complete",
            "archive/configuration checked; signal/timing integrity not verified",
        )?;
        Ok(CaptureOutcome {
            directory: run.directory.clone(),
            requested_samples: req.samples,
            captured_samples: samples,
            diagnostics_present: diagnostics,
        })
    })();
    match outcome {
        Ok(capture) => Ok(capture),
        Err(error) => {
            run.finish(
                if matches!(error, Error::Interrupted(_)) {
                    "interrupted"
                } else {
                    "failed"
                },
                &error.to_string(),
            )?;
            Err(Error::Io(format!(
                "capture failed: {error}; evidence: {}",
                run.directory.display()
            )))
        }
    }
}

/// Validate CRCs and count stored samples without loading the capture into RAM.
pub fn validate_capture(path: &Path) -> Result<u64> {
    Ok(inspect_capture(path)?.samples)
}

struct CaptureInfo {
    samples: u64,
    samplerate: u64,
    channels: BTreeSet<u8>,
}

fn inspect_capture(path: &Path) -> Result<CaptureInfo> {
    let invalid = |e: zip::result::ZipError| Error::Io(format!("invalid sigrok archive: {e}"));
    let mut archive = zip::ZipArchive::new(File::open(path)?).map_err(invalid)?;
    if archive.len() > 100_000 {
        return Err(Error::Invalid("too many capture chunks".into()));
    }
    let mut version = String::new();
    archive
        .by_name("version")
        .map_err(invalid)?
        .take(32)
        .read_to_string(&mut version)?;
    if version.trim() != "2" {
        return Err(Error::Unsupported("sigrok archive version must be 2"));
    }
    let mut metadata = String::new();
    archive
        .by_name("metadata")
        .map_err(invalid)?
        .take(65536)
        .read_to_string(&mut metadata)?;
    let mut in_device = false;
    let mut unit = None;
    let mut samplerate = None;
    let mut channels = BTreeSet::new();
    for line in metadata.lines().map(str::trim) {
        if line.starts_with('[') {
            in_device = line == "[device 1]";
        }
        if in_device {
            if let Some((key, value)) = line.split_once('=') {
                if key.trim() == "unitsize" {
                    unit = value.trim().parse::<u64>().ok();
                }
                if key.trim() == "samplerate" {
                    let compact = value
                        .chars()
                        .filter(|c| !c.is_whitespace())
                        .collect::<String>()
                        .to_ascii_lowercase();
                    for (suffix, factor) in [("mhz", 1_000_000), ("khz", 1000), ("hz", 1)] {
                        if let Some(n) = compact
                            .strip_suffix(suffix)
                            .and_then(|n| n.parse::<u64>().ok())
                        {
                            samplerate = n.checked_mul(factor);
                            break;
                        }
                    }
                }
                if key
                    .trim()
                    .strip_prefix("probe")
                    .is_some_and(|n| n.parse::<u8>().is_ok())
                {
                    let channel = value
                        .trim()
                        .strip_prefix("CH")
                        .and_then(|n| n.parse::<u8>().ok())
                        .filter(|n| *n <= 15)
                        .ok_or_else(|| Error::Io("unexpected capture channel name".into()))?;
                    channels.insert(channel);
                }
            }
        }
    }
    let unit = unit
        .filter(|u| (1..=2).contains(u))
        .ok_or_else(|| Error::Io("missing/invalid digital sample unitsize".into()))?;
    let mut total = 0u64;
    let mut chunks = BTreeSet::new();
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(invalid)?;
        let Some(suffix) = entry.name().strip_prefix("logic-1-") else {
            continue;
        };
        let number = suffix
            .parse::<u32>()
            .map_err(|_| Error::Io("invalid logic chunk name".into()))?;
        if number == 0 || !chunks.insert(number) || entry.size() % unit != 0 {
            return Err(Error::Io("invalid/duplicate capture chunk".into()));
        }
        if entry.size() > 20_000_000_000u64.saturating_sub(total) {
            return Err(Error::Io("capture exceeds sample bound".into()));
        }
        total += std::io::copy(&mut entry, &mut std::io::sink())?;
    }
    if total == 0 || chunks.last().copied() != Some(chunks.len() as u32) {
        return Err(Error::Io("empty/incomplete capture archive".into()));
    }
    Ok(CaptureInfo {
        samples: total / unit,
        samplerate: samplerate
            .filter(|n| *n > 0)
            .ok_or_else(|| Error::Io("missing/invalid samplerate".into()))?,
        channels,
    })
}
