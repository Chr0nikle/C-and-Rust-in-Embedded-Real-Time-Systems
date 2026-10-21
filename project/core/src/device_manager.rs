//! USB probe and serial-endpoint discovery for `rtos list`.
//! Separate detected, identified, compatible, and ready; never infer firmware
//! from USB. Identification uses the board manifests under `boards/`.
use crate::{
    boards::{load_board, BoardCfg},
    error::Result,
    plugin_interface::Board,
};
use std::path::{Path, PathBuf};

/// A discovery observation; unknown boards still appear in `rtos list`.
/// Convert to `plugin_interface::Device` only after identification is unambiguous.
pub struct DetectedDevice {
    /// Stable probe/USB identity or serial selector; never an enumeration index.
    pub selector: String,
    /// Confirmed board identity, absent when the device is not yet identified.
    pub board: Option<Board>,
    /// Serial endpoint only when correlated with this physical device.
    pub uart: Option<PathBuf>,
    /// Identification, permission or mapping limitations to show to the user.
    pub diagnostics: Vec<String>,
}

/// One USB device as seen in sysfs.
struct UsbDevice {
    vid: String,
    pid: String,
    serial: Option<String>,
}

/// Enumerate debug probes (USB) and serial endpoints, correlating both with the
/// board manifests under `project_root/boards`. Returns real observations even
/// when a device is unknown; never an empty list for a missing scan.
pub fn discover(project_root: &Path) -> Result<Vec<DetectedDevice>> {
    let manifests = load_manifests(project_root);
    let mut out: Vec<DetectedDevice> = Vec::new();
    let mut matched_ports: Vec<String> = Vec::new();

    for dev in enumerate_usb() {
        let vid_pid = format!("{}:{}", dev.vid, dev.pid);
        let selector = match &dev.serial {
            Some(serial) if !serial.is_empty() => format!("{vid_pid}:{serial}"),
            _ => vid_pid.clone(),
        };
        let mut board = None;
        let mut uart = None;
        let mut diagnostics = Vec::new();

        if let Some(cfg) = match_probe(&manifests, &vid_pid, dev.serial.as_deref()) {
            diagnostics.push(format!("identified as {}", cfg.id));
            board = board_of(&cfg.id);
            if board.is_none() {
                diagnostics.push(format!("manifest {} has no board enum", cfg.id));
            }
            if let Some(port) = cfg.port() {
                if Path::new(port).exists() {
                    uart = Some(PathBuf::from(port));
                    matched_ports.push(port.to_string());
                } else {
                    diagnostics.push(format!("manifest port {port} is not present"));
                }
            }
        } else {
            diagnostics.push("no matching board manifest".into());
        }
        out.push(DetectedDevice {
            selector,
            board,
            uart,
            diagnostics,
        });
    }

    for port in enumerate_serial() {
        if matched_ports.iter().any(|p| p == &port) {
            continue;
        }
        let cfg = manifests.iter().find(|m| m.port() == Some(port.as_str()));
        let board = cfg.and_then(|m| board_of(&m.id));
        let diagnostics = match cfg {
            Some(m) => vec![format!("serial endpoint for {}", m.id)],
            None => vec!["serial endpoint with no matching board manifest".into()],
        };
        out.push(DetectedDevice {
            selector: port.clone(),
            board,
            uart: Some(PathBuf::from(port)),
            diagnostics,
        });
    }

    Ok(out)
}

/// Load every `boards/<id>/board.toml`; a missing directory yields no manifests
/// (devices are still listed, just unidentified).
fn load_manifests(project_root: &Path) -> Vec<BoardCfg> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(project_root.join("boards")) else {
        return out;
    };
    let mut ids: Vec<String> = entries
        .flatten()
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    ids.sort();
    for id in ids {
        if let Ok(cfg) = load_board(project_root, &id) {
            out.push(cfg);
        }
    }
    out
}

/// Find the manifest whose debug probe matches `vid:pid` and, when the manifest
/// pins a probe serial, the same serial.
fn match_probe<'a>(
    manifests: &'a [BoardCfg],
    vid_pid: &str,
    serial: Option<&str>,
) -> Option<&'a BoardCfg> {
    /* Prefer an exact probe-serial match. */
    if let Some(serial) = serial {
        if let Some(m) = manifests.iter().find(|m| {
            m.debug.probe.eq_ignore_ascii_case(vid_pid)
                && !m.debug.probe_serial.is_empty()
                && m.debug.probe_serial.eq_ignore_ascii_case(serial)
        }) {
            return Some(m);
        }
    }
    /* Fall back to a serial-less manifest only when it is unambiguous. */
    let mut generic = manifests
        .iter()
        .filter(|m| m.debug.probe.eq_ignore_ascii_case(vid_pid) && m.debug.probe_serial.is_empty());
    let first = generic.next()?;
    if generic.next().is_some() {
        None
    } else {
        Some(first)
    }
}

/// Map a manifest id to the typed board, if one exists.
fn board_of(id: &str) -> Option<Board> {
    match id {
        "esp32c6" => Some(Board::Esp32c6),
        "nrf52840dk" => Some(Board::Nrf52840Dk),
        "stm32f4discovery" => Some(Board::Stm32f4Discovery),
        "stm32h723" => Some(Board::Stm32h723),
        _ => None,
    }
}

/// Enumerate USB devices from sysfs (world-readable; no external tool).
fn enumerate_usb() -> Vec<UsbDevice> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir("/sys/bus/usb/devices") else {
        return out;
    };
    for entry in entries.flatten() {
        let dir = entry.path();
        let vid = read_trim(&dir.join("idVendor"));
        let pid = read_trim(&dir.join("idProduct"));
        if vid.is_empty() || pid.is_empty() {
            continue;
        }
        /* USB hubs (class 09) are not candidate benchmark targets; skip the
         * root/internal hubs so `rtos list` shows probes and devices. */
        if read_trim(&dir.join("bDeviceClass")) == "09" {
            continue;
        }
        let serial = read_serial(&dir.join("serial"));
        out.push(UsbDevice { vid, pid, serial });
    }
    out.sort_by(|a, b| (&a.vid, &a.pid, &a.serial).cmp(&(&b.vid, &b.pid, &b.serial)));
    out
}

/// Stable serial paths from `/dev/serial/by-id` and `/dev/serial/by-path`.
fn enumerate_serial() -> Vec<String> {
    let mut out = Vec::new();
    for dir in ["/dev/serial/by-id", "/dev/serial/by-path"] {
        let Ok(entries) = std::fs::read_dir(dir) else {
            continue;
        };
        for entry in entries.flatten() {
            out.push(format!("{dir}/{}", entry.file_name().to_string_lossy()));
        }
    }
    out.sort();
    out.dedup();
    out
}

fn read_trim(path: &Path) -> String {
    std::fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// Read a USB serial descriptor. Some probes (e.g. ST-Link/V2) expose a binary
/// descriptor that probe-rs renders as uppercase hex, while others (J-Link)
/// expose printable ASCII; normalize to whichever form probe-rs would show.
fn read_serial(path: &Path) -> Option<String> {
    let raw = std::fs::read(path).ok()?;
    let end = raw.iter().rposition(|b| !b.is_ascii_whitespace())?;
    let raw: &[u8] = &raw[..=end];
    if raw.is_empty() {
        return None;
    }
    if raw.iter().all(|b| (0x20..=0x7e).contains(b)) {
        return Some(String::from_utf8_lossy(raw).to_string());
    }
    let mut hex = String::with_capacity(raw.len() * 2);
    for b in raw {
        hex.push_str(&format!("{b:02X}"));
    }
    Some(hex)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::boards::{DebugCfg, SerialCfg};

    fn cfg(id: &str, probe: &str, probe_serial: &str, port: &str) -> BoardCfg {
        BoardCfg {
            id: id.into(),
            mcu: String::new(),
            serial: SerialCfg {
                transport: "usb-uart".into(),
                baud: 115200,
                port_selector: port.into(),
                verified: false,
            },
            debug: DebugCfg {
                probe: probe.into(),
                probe_serial: probe_serial.into(),
                chip: String::new(),
                protocol: "swd".into(),
                tool: String::new(),
                verified: false,
            },
        }
    }

    #[test]
    fn probe_matches_vid_pid_and_pinned_serial() {
        let manifests = vec![cfg("stm32f4discovery", "0483:3748", "52C3BF6D", "/dev/x")];
        assert!(match_probe(&manifests, "0483:3748", Some("52c3bf6d")).is_some());
        assert!(match_probe(&manifests, "0483:3748", Some("other")).is_none());
        assert!(match_probe(&manifests, "9999:0001", Some("52C3BF6D")).is_none());
    }

    #[test]
    fn probe_matches_when_serial_not_pinned() {
        let manifests = vec![cfg("nrf52840dk", "1366:1061", "", "/dev/y")];
        assert!(match_probe(&manifests, "1366:1061", Some("any")).is_some());
        assert!(match_probe(&manifests, "1366:1061", None).is_some());
    }

    #[test]
    fn ambiguous_serial_less_probe_is_not_identified() {
        let manifests = vec![
            cfg("board_a", "0483:3748", "", ""),
            cfg("board_b", "0483:3748", "", ""),
        ];
        assert!(match_probe(&manifests, "0483:3748", Some("R\u{ff}m")).is_none());
    }

    #[test]
    fn unknown_board_id_is_not_inferred() {
        assert_eq!(board_of("nope"), None);
        assert_eq!(board_of("stm32f4discovery"), Some(Board::Stm32f4Discovery));
    }
}
