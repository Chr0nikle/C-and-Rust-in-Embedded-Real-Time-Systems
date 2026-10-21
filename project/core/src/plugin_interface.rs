//! Plugins describe build routes; core owns serial capture and workflow sequencing.
use crate::error::{Error, Result};
use std::{collections::BTreeMap, path::PathBuf};

/// Physical study target. Exact carrier revision still comes from its board manifest.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Board {
    /// ESP32-C6 high-performance core.
    Esp32c6,
    /// Nordic nRF52840 DK (PCA10056); J-Link SWD debug and VCOM console.
    Nrf52840Dk,
    /// STM32F4 Discovery; verify STM32F407 part and board revision before flashing.
    Stm32f4Discovery,
    /// STM32H723ZGT6 core board (FK723M1); external ST-LINK, USART1 console.
    Stm32h723,
}

impl Board {
    /// Directory name of the board manifest under `boards/`.
    pub fn manifest_id(self) -> &'static str {
        match self {
            Self::Esp32c6 => "esp32c6",
            Self::Nrf52840Dk => "nrf52840dk",
            Self::Stm32f4Discovery => "stm32f4discovery",
            Self::Stm32h723 => "stm32h723",
        }
    }
}
/// Application language, distinct from the kernel language.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Language {
    /// Native C workload.
    C,
    /// Rust workload using the same kernel services through a binding.
    Rust,
}
/// Typed selection passed down from the workflow layer.
#[derive(Debug)]
pub struct BuildRequest {
    /// Resolved project root; never the executable's directory.
    pub project: PathBuf,
    /// Selected physical target.
    pub board: Board,
    /// Selected application language.
    pub language: Language,
    /// Smoke or a named benchmark suite.
    pub workload: String,
    /// Isolated build directory keyed by inputs.
    pub output: PathBuf,
    /// Remove stale build artifacts before building (pristine build).
    pub clean: bool,
}
/// Validated build output; extend with hashes and full source/configuration provenance.
#[derive(Debug)]
pub struct Artifact {
    /// Executable image and accompanying images belong in this directory.
    pub directory: PathBuf,
    /// Board for which this artifact was built.
    pub board: Board,
    /// Stable build identity embedded into UART startup records.
    pub build_id: String,
    /// Executable ELF image(s) for static size analysis; empty when none exist.
    pub images: Vec<PathBuf>,
}
/// Resolved hardware selector; serial port is a separate mapping.
#[derive(Debug)]
pub struct Device {
    /// Probe or USB serial identity, not an enumeration index.
    pub selector: String,
    /// Confirmed board identity.
    pub board: Board,
}
/// Documentation-backed route. This is not a support or readiness claim.
#[derive(Debug)]
pub struct Route {
    /// Actual kernel/SDK backend; distinguishes ESP-IDF's FreeRTOS from upstream.
    pub backend: &'static str,
    /// Build-system target or configuration.
    pub target: &'static str,
    /// Board/toolchain and language-specific implementation notes.
    pub notes: &'static str,
    /// Repo-relative recipe to implement next.
    pub recipe: &'static str,
}
/// Host adapter contract; concrete implementations live only in plugins/.
pub trait RtosPlugin {
    /// Stable CLI name.
    fn id(&self) -> &'static str;
    /// Identify a prospective route; reject inherently incompatible targets.
    fn route(&self, board: Board, language: Language) -> Result<Route>;
    /// TODO: check source pins, exact board, SDK, compiler, language binding, workload.
    fn validate(&self, _request: &BuildRequest) -> Result<()> {
        Err(Error::NotImplemented(
            "plugin validation; follow its BUILD.md",
        ))
    }
    /// TODO: invoke native build system through ProcessRunner and identify all images.
    fn build(&self, _request: &BuildRequest) -> Result<Artifact> {
        Err(Error::NotImplemented("firmware build; follow its BUILD.md"))
    }
    /// TODO: validate artifact/device provenance, program, and verify written bytes.
    /// Startup verification is a separate UART workflow step.
    fn flash(&self, _artifact: &Artifact, _device: &Device) -> Result<()> {
        Err(Error::NotImplemented(
            "flash adapter; no device was touched",
        ))
    }
}
/// Deterministic registry. Registration must not imply that a plugin is implemented.
#[derive(Default)]
pub struct PluginRegistry {
    plugins: BTreeMap<&'static str, Box<dyn RtosPlugin>>,
}
impl PluginRegistry {
    /// Register a plugin exactly once; preserve the existing plugin on duplicate IDs.
    pub fn register(&mut self, plugin: Box<dyn RtosPlugin>) -> Result<()> {
        if self.plugins.contains_key(plugin.id()) {
            return Err(Error::Invalid(format!("duplicate plugin: {}", plugin.id())));
        }
        self.plugins.insert(plugin.id(), plugin);
        Ok(())
    }
    /// Resolve a canonical RTOS name.
    pub fn get(&self, id: &str) -> Result<&dyn RtosPlugin> {
        self.plugins
            .get(id)
            .map(Box::as_ref)
            .ok_or_else(|| Error::Invalid(format!("unknown RTOS: {id}")))
    }
}
