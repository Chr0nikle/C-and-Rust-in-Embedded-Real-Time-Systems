//! FreeRTOS-in-Rust host adapter: route, validate, build and flash.
//!
//! `no_std` pure-Rust FreeRTOS translation (`freertos-in-rust` 0.3,
//! Cortex-M4F port) built with the stable toolchain for
//! `thumbv7em-none-eabihf` and flashed through probe-rs. Console is USART2
//! (PA2/PA3) through the external CP2102 on the STM32F4 Discovery.
use rtos_core::{
    error::{Error, Result},
    plugin_interface::{Artifact, Board, BuildRequest, Device, Language, Route, RtosPlugin},
    process::{self, CommandSpec},
};
use std::path::{Path, PathBuf};
use std::time::Duration;

const BUILD_TIMEOUT: Duration = Duration::from_secs(900);

fn board_target(board: Board) -> Result<&'static str> {
    match board {
        Board::Stm32f4Discovery => Ok("stm32f4discovery"),
        Board::Stm32h723 => Ok("stm32h723"),
        _ => Err(Error::Unsupported(
            "freertos-in-rust has no board mapping for this board",
        )),
    }
}

fn firmware_dir(project: &Path, board: Board) -> Result<PathBuf> {
    match board {
        Board::Stm32f4Discovery => Ok(project.join("firmware/rust-kernels/freertos-in-rust/rust")),
        Board::Stm32h723 => Ok(project.join("firmware/rust-kernels/freertos-in-rust/rust")),
        _ => Err(Error::Unsupported(
            "freertos-in-rust has no firmware directory for this board",
        )),
    }
}

fn git_short_sha(project: &Path) -> String {
    std::process::Command::new("git")
        .args([
            "-C",
            &project.to_string_lossy(),
            "rev-parse",
            "--short=8",
            "HEAD",
        ])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "nogit".into())
}

fn compute_build_id(project: &Path, board: Board) -> Result<String> {
    let tag = match board {
        Board::Stm32f4Discovery => "freertos-in-rust-stm32f4",
        Board::Stm32h723 => "freertos-in-rust-stm32h723",
        _ => {
            return Err(Error::Unsupported(
                "freertos-in-rust has no build id for this board",
            ))
        }
    };
    Ok(format!("{tag}-bench-{}", git_short_sha(project)))
}

/// Concrete adapter registered by the CLI, never by core.
pub struct FreertosInRustPlugin;

impl RtosPlugin for FreertosInRustPlugin {
    fn id(&self) -> &'static str {
        "freertos-in-rust"
    }

    fn route(&self, board: Board, language: Language) -> Result<Route> {
        let target = board_target(board)?;
        if language != Language::Rust {
            return Err(Error::Unsupported("freertos-in-rust adapter is Rust-only"));
        }
        let notes = match board {
            Board::Stm32f4Discovery => {
                "cargo (stable) for thumbv7em-none-eabihf; USART2 PA2/PA3 @115200 via \
                 the external CP2102. Pure-Rust FreeRTOS kernel (0.3, Cortex-M4F). \
                 Cases 0,1,2,7,12,13,17,26,28 advertised."
            }
            Board::Stm32h723 => {
                "cargo (stable) for thumbv7em-none-eabihf; USART1 PA9/PA10 @115200 via \
                 a CP2102. Pure-Rust FreeRTOS kernel (0.3, Cortex-M7 port)."
            }
            _ => unreachable!(),
        };
        Ok(Route {
            backend: "freertos-in-rust (pure-Rust FreeRTOS translation)",
            target,
            notes,
            recipe: "plugins/freertos-in-rust/BUILD.md",
        })
    }

    fn validate(&self, request: &BuildRequest) -> Result<()> {
        if request.language != Language::Rust {
            return Err(Error::Unsupported("freertos-in-rust supports only Rust"));
        }
        let fw = firmware_dir(&request.project, request.board)?;
        if !fw.join("Cargo.toml").is_file() {
            return Err(Error::Build(format!(
                "freertos-in-rust crate not found at {}",
                fw.display()
            )));
        }
        Ok(())
    }

    fn build(&self, request: &BuildRequest) -> Result<Artifact> {
        self.build_rust(request)
    }

    fn flash(&self, artifact: &Artifact, _device: &Device) -> Result<()> {
        let elf = artifact
            .images
            .first()
            .cloned()
            .ok_or_else(|| Error::Flash("artifact has no image".into()))?;
        let (mcu, probe) = match artifact.board {
            Board::Stm32f4Discovery => ("STM32F407VGTx", "0483:3748:52C3BF6D"),
            Board::Stm32h723 => ("STM32H723ZG", "0483:3748:37C3BF71"),
            other => {
                return Err(Error::Flash(format!(
                    "freertos-in-rust flash has no route for {other:?}"
                )))
            }
        };
        eprintln!(
            "[flash] probe-rs download {} (artifact={}, probe={})",
            elf.display(),
            artifact.build_id,
            probe
        );
        let executable = if PathBuf::from("/home/chronikle/.cargo/bin/probe-rs").is_file() {
            PathBuf::from("/home/chronikle/.cargo/bin/probe-rs")
        } else {
            PathBuf::from("probe-rs")
        };
        let request = rtos_core::flash::FlashRequest {
            file: elf,
            mcu: mcu.into(),
            probe: probe.into(),
            format: rtos_core::flash::ImageFormat::Elf,
            address: None,
            bootloader: None,
            partition_table: None,
            reset: true,
            timeout_seconds: 120,
        };
        let backend = rtos_core::flash::ProbeRsFlash { executable };
        let results = artifact.directory.join("flash-results");
        let dir = rtos_core::flash::flash(&backend, &request, &results)?;
        eprintln!("[flash] succeeded; evidence {}", dir.display());
        Ok(())
    }
}

impl FreertosInRustPlugin {
    fn build_rust(&self, request: &BuildRequest) -> Result<Artifact> {
        let fw = firmware_dir(&request.project, request.board)?;
        let target = board_target(request.board)?;
        let build_id = compute_build_id(&request.project, request.board)?;
        // The H7 needs the Cortex-M7 kernel port + stm32h7xx-hal (non-default
        // board feature); the F4 uses the default (M4F + stm32f4xx-hal).
        let mut args: Vec<std::ffi::OsString> = vec!["build".into(), "--release".into()];
        if request.board == Board::Stm32h723 {
            args.push("--no-default-features".into());
            args.push("--features".into());
            args.push("stm32h723".into());
        }
        let spec = CommandSpec {
            program: PathBuf::from("cargo"),
            args,
            cwd: fw.clone(),
            env: vec![("BENCH_BUILD_ID".into(), build_id.clone().into())],
            timeout: BUILD_TIMEOUT,
        };
        eprintln!(
            "[build] cargo build --release (freertos-in-rust {target}) in {} (build_id={})",
            fw.display(),
            build_id
        );
        process::run_success(&spec)?;

        let out = fw.join("target/thumbv7em-none-eabihf/release");
        let elf = out.join("rtos-bench-freertos-in-rust");
        if !elf.is_file() {
            return Err(Error::Build(format!("ELF not found: {}", elf.display())));
        }
        Ok(Artifact {
            directory: out,
            board: request.board,
            build_id,
            images: vec![elf],
        })
    }
}
