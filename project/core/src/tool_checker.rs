//! Route-specific tool validation: executable resolution and host prerequisites
//! for the selected board/RTOS route, without provisioning anything.
//!
//! The plugins still perform their own deeper checks during `build`/`flash`;
//! this module is the shared, host-only preflight used by `rtos check` so a
//! missing tool produces an actionable report instead of a mid-build failure.

use crate::plugin_interface::Board;
use std::path::{Path, PathBuf};

/// Supported build routes for tool validation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Route {
    /// FreeRTOS kernel (ESP-IDF or STM32Cube build).
    Freertos,
    /// Zephyr via west.
    Zephyr,
    /// NuttX via CMake and its apps tree.
    Nuttx,
    /// freertos-in-rust (pure-Rust FreeRTOS translation) via cargo, stable
    /// toolchain, Cortex-M4F port (STM32F4 Discovery, Rust).
    FreertosInRust,
    /// Rivet RTOS (dual-tier Rust kernel) via cargo, stable toolchain,
    /// Cortex-M4 port (STM32F4 Discovery, Rust).
    Rivet,
}

/// One resolved tool or prerequisite.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolCheck {
    /// Human-readable tool or prerequisite name.
    pub name: String,
    /// Resolved path when the check found one.
    pub path: Option<PathBuf>,
    /// What was checked or what is missing.
    pub detail: String,
    /// Whether the route needs this tool.
    pub required: bool,
    /// Whether the check passed.
    pub ok: bool,
}

impl ToolCheck {
    /// A satisfied check for a resolved path.
    pub fn found(name: &str, path: PathBuf) -> Self {
        Self {
            name: name.to_string(),
            detail: path.display().to_string(),
            path: Some(path),
            required: true,
            ok: true,
        }
    }

    /// A check for a path that must exist.
    pub fn path(name: &str, path: &Path, hint: &str) -> Self {
        if path.exists() {
            Self::found(name, path.to_path_buf())
        } else {
            Self {
                name: name.to_string(),
                path: None,
                detail: format!("missing {}; {hint}", path.display()),
                required: true,
                ok: false,
            }
        }
    }

    /// A check for an executable that must resolve on `PATH`.
    pub fn executable(name: &str) -> Self {
        match find_executable(name) {
            Some(path) => Self::found(name, path),
            None => Self::missing(name, format!("{name} not found on PATH")),
        }
    }

    /// A required check that failed.
    pub fn missing(name: &str, detail: String) -> Self {
        Self {
            name: name.to_string(),
            path: None,
            detail,
            required: true,
            ok: false,
        }
    }
}

/// Resolve an executable on `PATH` without running it.
pub fn find_executable(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Project-local ESP RISC-V toolchain bin, if installed.
fn esp_riscv_toolchain(project: &Path) -> Option<PathBuf> {
    let root = project.join(".local/toolchains/esp-idf/tools/riscv32-esp-elf");
    let entries = std::fs::read_dir(root).ok()?;
    for entry in entries.flatten() {
        let bin = entry.path().join("riscv32-esp-elf/bin");
        if bin.join("riscv32-esp-elf-gcc").is_file() {
            return Some(bin);
        }
    }
    None
}

/// Project-local esptool, if installed.
fn esptool(project: &Path) -> Option<PathBuf> {
    [
        project.join(".local/venv/zephyr/bin/esptool"),
        project.join(".local/venv/nuttx/bin/esptool"),
    ]
    .into_iter()
    .find(|p| p.is_file())
}

/// Project-local Arm GNU toolchain bin, if installed.
fn arm_toolchain(project: &Path) -> Option<PathBuf> {
    let root = project.join(".local/toolchains/arm-none-eabi");
    for entry in std::fs::read_dir(root).ok()?.flatten() {
        let bin = entry.path().join("bin/arm-none-eabi-gcc");
        if bin.is_file() {
            return Some(bin);
        }
    }
    None
}

/// Validate the tools required by one board/RTOS route.
///
/// Returns one entry per check; callers decide how to report or gate on
/// `required && !ok`.
pub fn validate(project: &Path, board: Board, route: Route) -> Vec<ToolCheck> {
    let mut checks = vec![ToolCheck::executable("git")];
    match route {
        Route::Freertos => {
            checks.push(ToolCheck::executable("cmake"));
            match board {
                Board::Esp32c6 => {
                    // ESP-IDF is the ESP32-C6 build's kernel; only this board
                    // needs the SDK and RISC-V toolchain.
                    checks.push(ToolCheck::path(
                        "esp-idf",
                        &project.join(".local/sdk/esp-idf/export.sh"),
                        "provision ESP-IDF",
                    ));
                    checks.push(riscv_toolchain_check(project));
                    checks.push(esptool_check(project));
                }
                Board::Stm32f4Discovery => {
                    checks.push(arm_check(project));
                }
                Board::Nrf52840Dk => {
                    checks.push(arm_check(project));
                }
                Board::Stm32h723 => {
                    checks.push(arm_check(project));
                }
            }
        }
        Route::Zephyr => {
            checks.push(ToolCheck::path(
                "west",
                &project.join(".local/west/west"),
                "provision Zephyr per plugins/zephyr/BUILD.md",
            ));
            checks.push(ToolCheck::path(
                "zephyr",
                &project.join(".local/west/zephyr/CMakeLists.txt"),
                "provision Zephyr per plugins/zephyr/BUILD.md",
            ));
            checks.push(ToolCheck::executable("cmake"));
            match board {
                Board::Esp32c6 => {
                    checks.push(riscv_toolchain_check(project));
                    checks.push(esptool_check(project));
                }
                Board::Stm32f4Discovery => {
                    checks.push(arm_check(project));
                }
                Board::Nrf52840Dk => {
                    checks.push(arm_check(project));
                }
                Board::Stm32h723 => {
                    checks.push(arm_check(project));
                }
            }
        }
        Route::Nuttx => {
            checks.push(ToolCheck::path(
                "nuttx",
                &project.join(".local/source/nuttx"),
                "clone apache/nuttx per plugins/nuttx/BUILD.md",
            ));
            checks.push(ToolCheck::path(
                "nuttx-apps",
                &project.join(".local/source/nuttx-apps"),
                "clone apache/nuttx-apps per plugins/nuttx/BUILD.md",
            ));
            checks.push(ToolCheck::executable("cmake"));
            match board {
                Board::Esp32c6 => {
                    checks.push(riscv_toolchain_check(project));
                    checks.push(esptool_check(project));
                }
                Board::Stm32f4Discovery => {
                    checks.push(arm_check(project));
                }
                Board::Nrf52840Dk => {
                    checks.push(arm_check(project));
                }
                Board::Stm32h723 => {
                    checks.push(arm_check(project));
                }
            }
        }
        Route::FreertosInRust => {
            checks.push(ToolCheck::path(
                "freertos-in-rust crate",
                &project.join("firmware/rust-kernels/freertos-in-rust/rust/Cargo.toml"),
                "the freertos-in-rust crate ships in-tree",
            ));
            checks.push(ToolCheck::executable("cargo"));
        }
        Route::Rivet => {
            checks.push(ToolCheck::path(
                "rivet crate",
                &project.join("firmware/rust-kernels/rivet/rust/stm32f4/Cargo.toml"),
                "the rivet crate ships in-tree",
            ));
            checks.push(ToolCheck::path(
                "pinned Rivet source",
                &project.join(".local/source/rivet-rtos/rivet/Cargo.toml"),
                "pin the rivet-rtos workspace under .local/source/rivet-rtos",
            ));
            checks.push(ToolCheck::executable("cargo"));
        }
    }
    checks
}
fn riscv_toolchain_check(project: &Path) -> ToolCheck {
    match esp_riscv_toolchain(project) {
        Some(path) => ToolCheck::found("riscv32-esp-elf-gcc", path),
        None => ToolCheck::missing(
            "riscv32-esp-elf-gcc",
            "ESP RISC-V toolchain not found under .local/toolchains/esp-idf".into(),
        ),
    }
}

fn esptool_check(project: &Path) -> ToolCheck {
    match esptool(project) {
        Some(path) => ToolCheck::found("esptool", path),
        None => ToolCheck::missing("esptool", "esptool not found under .local/venv".into()),
    }
}

fn arm_check(project: &Path) -> ToolCheck {
    match arm_toolchain(project) {
        Some(path) => ToolCheck::found("arm-none-eabi-gcc", path),
        None => ToolCheck::executable("arm-none-eabi-gcc"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn find_executable_locates_on_path() {
        let dir = std::env::temp_dir().join(format!("rtos-tc-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let tool = dir.join("rtos-fake-tool");
        fs::write(&tool, b"#!/bin/sh\n").unwrap();
        let old = std::env::var_os("PATH");
        // SAFETY: single-threaded test process mutation; restored below.
        unsafe { std::env::set_var("PATH", &dir) };
        let found = find_executable("rtos-fake-tool");
        match old {
            Some(v) => unsafe { std::env::set_var("PATH", v) },
            None => unsafe { std::env::remove_var("PATH") },
        }
        fs::remove_dir_all(&dir).ok();
        assert_eq!(found, Some(tool));
    }

    #[test]
    fn missing_project_path_reports_hint() {
        let check = ToolCheck::path("nuttx", Path::new("/nonexistent/rtos/nuttx"), "clone it");
        assert!(!check.ok);
        assert!(check.detail.contains("clone it"));
    }

    #[test]
    fn validate_returns_a_check_per_route() {
        let checks = validate(
            Path::new("/tmp/rtos-no-project"),
            Board::Esp32c6,
            Route::Zephyr,
        );
        assert!(checks.iter().any(|c| c.name == "west" && !c.ok));
        assert!(checks.iter().any(|c| c.name == "git"));
    }
}
