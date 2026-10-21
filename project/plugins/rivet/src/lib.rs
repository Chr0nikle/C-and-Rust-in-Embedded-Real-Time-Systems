//! Rivet RTOS host adapter: route, validate, build and flash.
//!
//! Rivet (`rivet-rtos` 0.2.0, dual-tier Rust kernel) built with the stable
//! toolchain for `thumbv7em-none-eabi` and flashed through probe-rs. Console
//! is USART2 (PA2/PA3) through the external CP2102 on the STM32F4 Discovery.
//! The firmware crate depends on the pinned Rivet workspace under
//! `.local/source/rivet-rtos`.
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
        _ => Err(Error::Unsupported(
            "rivet has no board mapping for this board",
        )),
    }
}

fn firmware_dir(project: &Path, board: Board) -> Result<PathBuf> {
    match board {
        Board::Stm32f4Discovery => Ok(project.join("firmware/rust-kernels/rivet/rust/stm32f4")),
        _ => Err(Error::Unsupported(
            "rivet has no firmware directory for this board",
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
        Board::Stm32f4Discovery => "rivet-stm32f4",
        _ => return Err(Error::Unsupported("rivet has no build id for this board")),
    };
    Ok(format!("{tag}-bench-{}", git_short_sha(project)))
}

/// Concrete adapter registered by the CLI, never by core.
pub struct RivetPlugin;

impl RtosPlugin for RivetPlugin {
    fn id(&self) -> &'static str {
        "rivet"
    }

    fn route(&self, board: Board, language: Language) -> Result<Route> {
        let target = board_target(board)?;
        if language != Language::Rust {
            return Err(Error::Unsupported("rivet adapter is Rust-only"));
        }
        let notes = match board {
            Board::Stm32f4Discovery => {
                "cargo (stable) for thumbv7em-none-eabi; USART2 PA2/PA3 @115200 via \
                 the external CP2102, HSI 16 MHz. Dual-tier Rust kernel (rivet-rtos \
                 0.2.0, Cortex-M4). Cases mapped to the preemptive tier (PriorityMutex, \
                 priorities, yield) and the cooperative tier (async Semaphore/Channel)."
            }
            _ => unreachable!(),
        };
        Ok(Route {
            backend: "rivet-rtos (dual-tier Rust kernel)",
            target,
            notes,
            recipe: "plugins/rivet/BUILD.md",
        })
    }

    fn validate(&self, request: &BuildRequest) -> Result<()> {
        if request.language != Language::Rust {
            return Err(Error::Unsupported("rivet supports only Rust"));
        }
        let fw = firmware_dir(&request.project, request.board)?;
        if !fw.join("Cargo.toml").is_file() {
            return Err(Error::Build(format!(
                "rivet crate not found at {}",
                fw.display()
            )));
        }
        let pinned = request
            .project
            .join(".local/source/rivet-rtos/rivet/Cargo.toml");
        if !pinned.is_file() {
            return Err(Error::Build(format!(
                "pinned Rivet source not found at {} (run the setup step)",
                pinned.display()
            )));
        }
        Ok(())
    }

    fn build(&self, request: &BuildRequest) -> Result<Artifact> {
        let fw = firmware_dir(&request.project, request.board)?;
        let target = board_target(request.board)?;
        let build_id = compute_build_id(&request.project, request.board)?;
        let spec = CommandSpec {
            program: PathBuf::from("cargo"),
            args: vec!["build".into(), "--release".into()],
            cwd: fw.clone(),
            env: vec![("BENCH_BUILD_ID".into(), build_id.clone().into())],
            timeout: BUILD_TIMEOUT,
        };
        eprintln!(
            "[build] cargo build --release (rivet {target}) in {} (build_id={})",
            fw.display(),
            build_id
        );
        process::run_success(&spec)?;

        let out = fw.join("target/thumbv7em-none-eabi/release");
        let elf = out.join("rtos-bench-rivet");
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

    fn flash(&self, artifact: &Artifact, _device: &Device) -> Result<()> {
        let elf = artifact
            .images
            .first()
            .cloned()
            .ok_or_else(|| Error::Flash("artifact has no image".into()))?;
        let (mcu, probe) = match artifact.board {
            Board::Stm32f4Discovery => ("STM32F407VGTx", "0483:3748:52C3BF6D"),
            other => {
                return Err(Error::Flash(format!(
                    "rivet flash has no route for {other:?}"
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
