//! Load and validate `config/rtos.toml` (schema v1).
//!
//! Values here are defaults for operational runs; explicit CLI arguments win.
//! Paths are resolved relative to the config file, not the invoking terminal.

use crate::error::{Error, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};

/// The only schema version this build understands.
pub const SCHEMA_VERSION: u32 = 1;

/// Parsed `config/rtos.toml`.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Configuration {
    /// Schema version; must equal [`SCHEMA_VERSION`].
    pub schema_version: u32,
    /// Free-form provisioning status marker (e.g. "skeleton").
    #[serde(default)]
    pub status: String,
    /// Default application settings.
    #[serde(default)]
    pub defaults: Defaults,
    /// Project-relative directories.
    #[serde(default)]
    pub paths: Paths,
    /// Serial/UART settings.
    #[serde(default)]
    pub serial: SerialConfig,
}

/// Default application settings.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Defaults {
    /// Default language ("c" or "rust").
    #[serde(default = "default_language")]
    pub language: String,
    /// Default workload name.
    #[serde(default = "default_application")]
    pub application: String,
    /// Valid samples per benchmark case.
    #[serde(default = "default_repetitions")]
    pub repetitions: u32,
    /// Discarded warm-up iterations per case.
    #[serde(default = "default_warmup")]
    pub warmup: u32,
}

impl Default for Defaults {
    fn default() -> Self {
        Self {
            language: default_language(),
            application: default_application(),
            repetitions: default_repetitions(),
            warmup: default_warmup(),
        }
    }
}

/// Project-relative directories.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Paths {
    /// Board manifest directory.
    #[serde(default = "default_boards")]
    pub boards: String,
    /// Firmware source directory.
    #[serde(default = "default_firmware")]
    pub firmware: String,
    /// Evidence/results directory.
    #[serde(default = "default_results")]
    pub results: String,
}

impl Default for Paths {
    fn default() -> Self {
        Self {
            boards: default_boards(),
            firmware: default_firmware(),
            results: default_results(),
        }
    }
}

/// Serial/UART settings.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct SerialConfig {
    /// Baud rate.
    #[serde(default = "default_baud")]
    pub baud: u32,
    /// Framing, e.g. "8N1".
    #[serde(default = "default_framing")]
    pub framing: String,
    /// Milliseconds to wait for the boot window.
    #[serde(default = "default_startup_timeout")]
    pub startup_timeout_ms: u64,
    /// Milliseconds allowed for a full run.
    #[serde(default = "default_run_timeout")]
    pub run_timeout_ms: u64,
}

impl Default for SerialConfig {
    fn default() -> Self {
        Self {
            baud: default_baud(),
            framing: default_framing(),
            startup_timeout_ms: default_startup_timeout(),
            run_timeout_ms: default_run_timeout(),
        }
    }
}

fn default_language() -> String {
    "c".to_string()
}
fn default_application() -> String {
    "smoke".to_string()
}
fn default_repetitions() -> u32 {
    1000
}
fn default_warmup() -> u32 {
    100
}
fn default_boards() -> String {
    "../boards".to_string()
}
fn default_firmware() -> String {
    "../firmware".to_string()
}
fn default_results() -> String {
    "../results".to_string()
}
fn default_baud() -> u32 {
    115200
}
fn default_framing() -> String {
    "8N1".to_string()
}
fn default_startup_timeout() -> u64 {
    10_000
}
fn default_run_timeout() -> u64 {
    120_000
}

impl Configuration {
    /// Validate cross-field invariants after deserialization.
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != SCHEMA_VERSION {
            return Err(Error::Invalid(format!(
                "unsupported config schema_version {}: expected {SCHEMA_VERSION}",
                self.schema_version
            )));
        }
        if self.defaults.repetitions == 0 {
            return Err(Error::Invalid(
                "config defaults.repetitions must be nonzero".into(),
            ));
        }
        if self.serial.baud == 0 {
            return Err(Error::Invalid("config serial.baud must be nonzero".into()));
        }
        if self.serial.framing != "8N1" {
            return Err(Error::Invalid(format!(
                "config serial.framing must be 8N1, got {}",
                self.serial.framing
            )));
        }
        if self.serial.run_timeout_ms == 0 || self.serial.startup_timeout_ms == 0 {
            return Err(Error::Invalid(
                "config serial timeouts must be nonzero".into(),
            ));
        }
        Ok(())
    }

    /// Resolve `paths.results` relative to the config file's project root.
    pub fn results_dir(&self, project_root: &Path) -> PathBuf {
        project_root.join("config").join(&self.paths.results)
    }

    /// Resolve `paths.boards` relative to the config file's project root.
    pub fn boards_dir(&self, project_root: &Path) -> PathBuf {
        project_root.join("config").join(&self.paths.boards)
    }

    /// Resolve `paths.firmware` relative to the config file's project root.
    pub fn firmware_dir(&self, project_root: &Path) -> PathBuf {
        project_root.join("config").join(&self.paths.firmware)
    }
}

/// Walk up from the current directory to find `config/rtos.toml`.
pub fn find_project_root() -> Result<PathBuf> {
    let mut dir = std::env::current_dir().map_err(|e| Error::Io(format!("cwd: {e}")))?;
    loop {
        if dir.join("config/rtos.toml").is_file() {
            return Ok(dir);
        }
        if !dir.pop() {
            return Err(Error::Invalid(
                "could not find project root (no config/rtos.toml); pass --project".into(),
            ));
        }
    }
}

/// Load and validate the project configuration.
///
/// `project` overrides root discovery. An absent/invalid file is an error: the
/// caller asked for configuration and must not silently fall back to constants.
pub fn load(project: Option<PathBuf>) -> Result<Configuration> {
    let root = match project {
        Some(root) => {
            if !root.is_dir() {
                return Err(Error::Invalid(format!(
                    "project directory not found: {}",
                    root.display()
                )));
            }
            root
        }
        None => find_project_root()?,
    };
    let path = root.join("config/rtos.toml");
    let text = std::fs::read_to_string(&path)
        .map_err(|e| Error::Invalid(format!("read {}: {e}", path.display())))?;
    let config: Configuration = toml::from_str(&text)
        .map_err(|e| Error::Invalid(format!("parse {}: {e}", path.display())))?;
    config.validate()?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
schema_version = 1
status = "skeleton"
[defaults]
language = "c"
application = "smoke"
repetitions = 1000
warmup = 100
[paths]
boards = "../boards"
firmware = "../firmware"
results = "../../results/campaigns"
[serial]
baud = 115200
framing = "8N1"
startup_timeout_ms = 10000
run_timeout_ms = 120000
"#;

    #[test]
    fn parses_and_validates_sample() {
        let config: Configuration = toml::from_str(SAMPLE).unwrap();
        config.validate().unwrap();
        assert_eq!(config.defaults.repetitions, 1000);
        assert_eq!(config.serial.baud, 115200);
    }

    #[test]
    fn rejects_wrong_schema_and_zero_repetitions() {
        let mut config: Configuration = toml::from_str(SAMPLE).unwrap();
        config.schema_version = 2;
        assert!(config.validate().is_err());

        let mut config: Configuration = toml::from_str(SAMPLE).unwrap();
        config.defaults.repetitions = 0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_bad_framing() {
        let mut config: Configuration = toml::from_str(SAMPLE).unwrap();
        config.serial.framing = "7E1".into();
        assert!(config.validate().is_err());
    }

    #[test]
    fn resolves_paths_against_project_root() {
        let config: Configuration = toml::from_str(SAMPLE).unwrap();
        let root = Path::new("/proj");
        assert_eq!(
            config.results_dir(root),
            PathBuf::from("/proj/config/../../results/campaigns")
        );
    }
}
