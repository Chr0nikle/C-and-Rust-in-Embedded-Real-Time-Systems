//! Board manifest loading: serial and debug identity from `boards/<id>/board.toml`.
//! The manifest is the single source of truth for probe, chip, protocol and port.
use crate::error::{Error, Result};
use serde::Deserialize;
use std::path::Path;

/// Parsed board manifest (serial + debug identity).
#[derive(Debug, Clone, Deserialize)]
pub struct BoardCfg {
    /// Board id, e.g. `esp32c6`.
    pub id: String,
    /// MCU family, e.g. `ESP32-C6`.
    #[serde(default)]
    pub mcu: String,
    /// Serial channel identity.
    pub serial: SerialCfg,
    /// Debug probe identity.
    pub debug: DebugCfg,
}

/// `[serial]` section of a board manifest.
#[derive(Debug, Clone, Deserialize)]
pub struct SerialCfg {
    /// Physical transport, e.g. `usb-uart-cp2102`.
    pub transport: String,
    /// Baud rate.
    pub baud: u32,
    /// Stable device path selector; empty means unverified.
    #[serde(default)]
    pub port_selector: String,
    /// Whether bidirectional operation has been verified.
    #[serde(default)]
    pub verified: bool,
}

/// `[debug]` section of a board manifest.
#[derive(Debug, Clone, Deserialize)]
pub struct DebugCfg {
    /// USB `VID:PID` of the debug probe.
    pub probe: String,
    /// Probe serial number (may be empty).
    #[serde(default)]
    pub probe_serial: String,
    /// probe-rs chip name, e.g. `STM32F407VGTx`.
    pub chip: String,
    /// Debug protocol: `swd` or `jtag`.
    pub protocol: String,
    /// Debug tool name.
    #[serde(default)]
    pub tool: String,
    /// Whether the tool path has been verified on hardware.
    #[serde(default)]
    pub verified: bool,
}

impl BoardCfg {
    /// The serial port path, if the manifest has a verified selector.
    pub fn port(&self) -> Option<&str> {
        let selector = self.serial.port_selector.trim();
        if selector.is_empty() {
            None
        } else {
            Some(selector)
        }
    }
}

/// Load `boards/<id>/board.toml` under the project root.
pub fn load_board(project_root: &Path, id: &str) -> Result<BoardCfg> {
    let path = project_root.join("boards").join(id).join("board.toml");
    let text = std::fs::read_to_string(&path)
        .map_err(|e| Error::Io(format!("read {}: {e}", path.display())))?;
    toml::from_str(&text).map_err(|e| Error::Invalid(format!("parse {}: {e}", path.display())))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_serial_and_debug_sections() {
        let dir = std::env::temp_dir().join(format!("rtos-boards-test-{}", std::process::id()));
        let board_dir = dir.join("boards").join("testboard");
        std::fs::create_dir_all(&board_dir).unwrap();
        std::fs::write(
            board_dir.join("board.toml"),
            r#"
schema_version = 1
id = "testboard"
mcu = "Test"
[serial]
transport = "usb-uart"
baud = 115200
port_selector = "/dev/ttyTEST"
verified = true
[debug]
probe = "1234:abcd"
probe_serial = "SN"
chip = "TestChip"
protocol = "swd"
verified = true
"#,
        )
        .unwrap();
        let cfg = load_board(&dir, "testboard").expect("load_board");
        assert_eq!(cfg.id, "testboard");
        assert_eq!(cfg.serial.baud, 115200);
        assert_eq!(cfg.port(), Some("/dev/ttyTEST"));
        assert_eq!(cfg.debug.probe, "1234:abcd");
        assert_eq!(cfg.debug.chip, "TestChip");
        assert_eq!(cfg.debug.protocol, "swd");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn empty_port_selector_is_none() {
        let dir = std::env::temp_dir().join(format!("rtos-boards-test-{}", std::process::id()));
        let board_dir = dir.join("boards").join("nop");
        std::fs::create_dir_all(&board_dir).unwrap();
        std::fs::write(
            board_dir.join("board.toml"),
            r#"
id = "nop"
[serial]
transport = "unverified"
baud = 115200
port_selector = ""
[debug]
probe = "303a:1001"
chip = "esp32c6"
protocol = "jtag"
"#,
        )
        .unwrap();
        let cfg = load_board(&dir, "nop").expect("load_board");
        assert_eq!(cfg.port(), None);
        assert_eq!(cfg.debug.probe_serial, "");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn nrf52840dk_manifest_parses() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let cfg = load_board(root, "nrf52840dk").expect("load shipped nrf52840dk manifest");
        assert_eq!(cfg.id, "nrf52840dk");
        assert_eq!(cfg.mcu, "NRF52840");
    }
}
