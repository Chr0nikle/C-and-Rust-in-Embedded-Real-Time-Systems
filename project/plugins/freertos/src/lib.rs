//! FreeRTOS host adapter: route, validate, and build.
//!
//! ESP32-C6 builds via ESP-IDF; STM32F4 Discovery builds via a plain
//! Makefile + arm-none-eabi-gcc cross toolchain. Flash and UART smoke live
//! in core::workflow because they need the project root for toolchain/SDK
//! environment resolution.
use rtos_core::{
    error::{Error, Result},
    plugin_interface::{Artifact, Board, BuildRequest, Device, Language, Route, RtosPlugin},
    process::{self, CommandSpec},
};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

const IDF_TIMEOUT: Duration = Duration::from_secs(600);
const STM32F4_BUILD_TIMEOUT: Duration = Duration::from_secs(300);
/// Project-local path to the arm-none-eabi toolchain bin directory.
fn stm32f4_toolchain_bin(project: &Path) -> PathBuf {
    project.join(
        ".local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin",
    )
}

/// Concrete adapter registered by the CLI, never by core.
pub struct FreeRtosPlugin;

mod environment;

/// Resolve the selected SDK's installed tool versions and Python environment.
pub fn resolve_idf(project: &Path) -> Result<(PathBuf, Vec<(String, String)>)> {
    environment::resolve(project)
}

/// Compute the build_id from the project's git short SHA.
pub fn compute_build_id(project: &Path) -> String {
    compute_c6_build_id(project, Language::C)
}

/// Language-aware ESP32-C6 (esp-idf) build_id; the tag keeps C and Rust
/// evidence separate while both are embedded by the same C session.
pub fn compute_c6_build_id(project: &Path, language: Language) -> String {
    let sha = git_short_sha(project);
    let tag = match language {
        Language::C => "bench",
        Language::Rust => "rust",
    };
    format!("freertos-c6-{tag}-{sha}")
}

/// Compute the STM32F4 Discovery build_id from the project's git short SHA.
///
/// The language tag keeps C and Rust evidence separate while both are embedded
/// identically by the firmware and expected by the host.
pub fn compute_stm32f4_build_id(project: &Path, _board: Board, language: Language) -> String {
    let sha = git_short_sha(project);
    let tag = match language {
        Language::C => "bench",
        Language::Rust => "rust",
    };
    format!("freertos-stm32f4-{tag}-{sha}")
}

/// Compute the STM32H723 FreeRTOS build_id (`freertos-h723-bench-<sha>` /
/// `freertos-h723-rust-<sha>`).
pub fn compute_stm32h723_build_id(project: &Path, _board: Board, language: Language) -> String {
    let sha = git_short_sha(project);
    let tag = match language {
        Language::C => "bench",
        Language::Rust => "rust",
    };
    format!("freertos-h723-{tag}-{sha}")
}

/// Compute the nRF52840 DK build_id from the project's git short SHA.
pub fn compute_nrf52840_build_id(project: &Path, language: Language) -> String {
    let sha = git_short_sha(project);
    let tag = match language {
        Language::C => "bench",
        Language::Rust => "rust",
    };
    format!("freertos-nrf52840-{tag}-{sha}")
}

/// Resolve the project's git short SHA, falling back to `nogit` when absent.
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

impl RtosPlugin for FreeRtosPlugin {
    fn id(&self) -> &'static str {
        "freertos"
    }

    fn route(&self, board: Board, _language: Language) -> Result<Route> {
        let (backend, target, notes) = match board {
            Board::Esp32c6 => (
                "ESP-IDF FreeRTOS port",
                "esp32c6",
                "ESP32-C6 runs the pinned ESP-IDF, whose kernel is FreeRTOS; this is the project's ESP-IDF route (there is no separate esp-idf adapter). C default; Rust links a riscv32imac soft-float staticlib with -DBENCH_RUST=ON.",
            ),
            Board::Stm32f4Discovery => (
                "upstream FreeRTOS-Kernel + STM32CubeF4 BSP",
                "GCC ARM_CM4F",
                "Needs startup, linker script, clock/UART setup and FreeRTOSConfig.h; Rust FFI remains planned.",
            ),
            Board::Nrf52840Dk => (
                "upstream FreeRTOS-Kernel + nRF52840 board layer",
                "GCC ARM_CM4F",
                "Bespoke startup/linker and a polling UARTE0 console on the J-Link VCOM; no vendor HAL. C first; Rust staticlib planned.",
            ),
            Board::Stm32h723 => (
                "upstream FreeRTOS-Kernel + STM32CubeH7 HAL",
                "GCC ARM_CM7",
                "25 MHz HSE -> 520 MHz SYSCLK; USART1 PA9/PA10 (debug header) to a CP2102 at 115200. Rust staticlib target thumbv7em-none-eabihf.",
            ),
        };
        Ok(Route {
            backend,
            target,
            notes,
            recipe: "plugins/freertos/BUILD.md",
        })
    }

    fn validate(&self, request: &BuildRequest) -> Result<()> {
        let firmware_dir = request.project.join("firmware/c-kernels/freertos/c");
        if !firmware_dir.is_dir() {
            return Err(Error::Invalid(format!(
                "firmware directory not found: {}",
                firmware_dir.display()
            )));
        }
        if request.language == Language::Rust {
            let crate_manifest = request
                .project
                .join("firmware/c-kernels/freertos/rust/Cargo.toml");
            if !crate_manifest.is_file() {
                return Err(Error::Build(format!(
                    "Rust adapter not found at {}",
                    crate_manifest.display()
                )));
            }
        }
        match request.board {
            Board::Esp32c6 => {
                resolve_idf(&request.project)?;
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/freertos/rust/Cargo.toml");
                    if !crate_manifest.is_file() {
                        return Err(Error::Build(format!(
                            "Rust adapter not found at {}",
                            crate_manifest.display()
                        )));
                    }
                }
            }
            Board::Stm32f4Discovery => {
                let toolchain = stm32f4_toolchain_bin(&request.project);
                if !toolchain.is_dir() {
                    return Err(Error::Build(format!(
                        "arm-none-eabi toolchain not found at {}; run scripts/setup/provision-toolchain.sh",
                        toolchain.display()
                    )));
                }
                let makefile = firmware_dir.join("stm32f4/Makefile");
                if !makefile.is_file() {
                    return Err(Error::Build(format!(
                        "Makefile not found at {}",
                        makefile.display()
                    )));
                }
            }
            Board::Nrf52840Dk => {
                let toolchain = stm32f4_toolchain_bin(&request.project);
                if !toolchain.is_dir() {
                    return Err(Error::Build(format!(
                        "arm-none-eabi toolchain not found at {}; run scripts/setup/provision-toolchain.sh",
                        toolchain.display()
                    )));
                }
                let makefile = firmware_dir.join("nrf52840dk/Makefile");
                if !makefile.is_file() {
                    return Err(Error::Build(format!(
                        "Makefile not found at {}",
                        makefile.display()
                    )));
                }
            }
            Board::Stm32h723 => {
                let toolchain = stm32f4_toolchain_bin(&request.project);
                if !toolchain.is_dir() {
                    return Err(Error::Build(format!(
                        "arm-none-eabi toolchain not found at {}; run scripts/setup/provision-toolchain.sh",
                        toolchain.display()
                    )));
                }
                let makefile = firmware_dir.join("stm32h723/Makefile");
                if !makefile.is_file() {
                    return Err(Error::Build(format!(
                        "Makefile not found at {}",
                        makefile.display()
                    )));
                }
            }
        }
        Ok(())
    }

    fn build(&self, request: &BuildRequest) -> Result<Artifact> {
        match request.board {
            Board::Esp32c6 => FreeRtosPlugin::build_esp_idf(request),
            Board::Stm32f4Discovery => FreeRtosPlugin::build_stm32f4(request),
            // (Stm32h723 handled below.)
            Board::Nrf52840Dk => FreeRtosPlugin::build_nrf52840(request),
            Board::Stm32h723 => build_stm32h723(request),
        }
    }

    fn flash(&self, artifact: &Artifact, device: &Device) -> Result<()> {
        match artifact.board {
            Board::Esp32c6 => flash_esp32c6(artifact, device),
            Board::Stm32f4Discovery => flash_stm32f4(artifact, device),
            Board::Nrf52840Dk => flash_nrf52840(artifact, device),
            Board::Stm32h723 => flash_stm32h723(artifact, device),
        }
    }
}

impl FreeRtosPlugin {
    fn build_esp_idf(request: &BuildRequest) -> Result<Artifact> {
        let (idf_dir, env) = resolve_idf(&request.project)?;
        let firmware_dir = request.project.join("firmware/c-kernels/freertos/c");

        let env_os: Vec<(std::ffi::OsString, std::ffi::OsString)> = env
            .iter()
            .map(|(k, v)| (k.clone().into(), v.clone().into()))
            .collect();

        let idf_py = idf_dir.join("tools/idf.py");
        let python = environment::python(&env)?;

        if request.clean {
            let clean_spec = CommandSpec {
                program: python.clone(),
                args: vec![idf_py.clone().into_os_string(), "fullclean".into()],
                cwd: firmware_dir.clone(),
                env: env_os.clone(),
                timeout: IDF_TIMEOUT,
            };
            eprintln!("[clean] idf.py fullclean in {}", firmware_dir.display());
            process::run_success(&clean_spec)?;
            eprintln!("[clean] idf.py fullclean complete");
        }

        let build_spec = CommandSpec {
            program: python,
            args: vec![
                idf_py.into_os_string(),
                if request.language == Language::Rust {
                    "-DBENCH_RUST=ON".into()
                } else {
                    "-DBENCH_RUST=OFF".into()
                },
                "build".into(),
            ],
            cwd: firmware_dir.clone(),
            env: env_os,
            timeout: IDF_TIMEOUT,
        };
        eprintln!("[build] running idf.py build in {}", firmware_dir.display());
        let output = process::run_success(&build_spec)?;
        eprintln!(
            "[build] idf.py build succeeded ({} bytes output)",
            output.stdout.len()
        );

        let build_dir = firmware_dir.join("build");
        if !build_dir.is_dir() {
            return Err(Error::Build(format!(
                "build directory not found after build: {}",
                build_dir.display()
            )));
        }
        let bin = build_dir.join("freertos-c6-bench.bin");
        if !bin.is_file() {
            return Err(Error::Build(format!(
                "firmware bin not found: {}",
                bin.display()
            )));
        }

        let build_id = compute_c6_build_id(&request.project, request.language);
        Ok(Artifact {
            images: vec![build_dir.join("freertos-c6-bench.elf")],
            directory: build_dir,
            board: request.board,
            build_id,
        })
    }

    fn build_stm32f4(request: &BuildRequest) -> Result<Artifact> {
        let firmware_dir = request
            .project
            .join("firmware/c-kernels/freertos/c/stm32f4");
        let toolchain_bin = stm32f4_toolchain_bin(&request.project);

        let build_id = compute_stm32f4_build_id(&request.project, request.board, request.language);

        let existing_path = std::env::var("PATH").unwrap_or_default();
        let parts: Vec<String> = vec![toolchain_bin.to_string_lossy().to_string(), existing_path];
        let mut env_os: Vec<(std::ffi::OsString, std::ffi::OsString)> =
            vec![("PATH".into(), parts.join(":").into())];
        // Inject the build_id into the firmware via the Makefile override.
        env_os.push(("BENCH_BUILD_ID".into(), build_id.clone().into()));
        // Rust builds link the adapter staticlib through `make RUST=1`.
        if request.language == Language::Rust {
            env_os.push(("RUST".into(), "1".into()));
        }

        if request.clean {
            let clean_spec = CommandSpec {
                program: PathBuf::from("make"),
                args: vec!["-C".into(), firmware_dir.as_os_str().into(), "clean".into()],
                cwd: firmware_dir.clone(),
                env: env_os.clone(),
                timeout: STM32F4_BUILD_TIMEOUT,
            };
            eprintln!("[clean] make clean in {}", firmware_dir.display());
            process::run_success(&clean_spec)?;
            eprintln!("[clean] make clean complete");
        }

        let build_spec = CommandSpec {
            program: PathBuf::from("make"),
            args: vec!["-C".into(), firmware_dir.as_os_str().into(), "all".into()],
            cwd: firmware_dir.clone(),
            env: env_os,
            timeout: STM32F4_BUILD_TIMEOUT,
        };
        eprintln!(
            "[build] running make (STM32F4) in {} (build_id={})",
            firmware_dir.display(),
            build_id
        );
        let output = process::run_success(&build_spec)?;
        eprintln!(
            "[build] make succeeded ({} bytes output, {} bytes stderr)",
            output.stdout.len(),
            output.stderr.len()
        );

        let build_dir = firmware_dir.join("build");
        if !build_dir.is_dir() {
            return Err(Error::Build(format!(
                "build directory not found after build: {}",
                build_dir.display()
            )));
        }
        let bin = build_dir.join("freertos-stm32f4-bench.bin");
        if !bin.is_file() {
            return Err(Error::Build(format!(
                "firmware bin not found: {}",
                bin.display()
            )));
        }

        Ok(Artifact {
            images: vec![build_dir.join("freertos-stm32f4-bench.elf")],
            directory: build_dir,
            board: request.board,
            build_id,
        })
    }

    fn build_nrf52840(request: &BuildRequest) -> Result<Artifact> {
        let firmware_dir = request
            .project
            .join("firmware/c-kernels/freertos/c/nrf52840dk");
        let toolchain_bin = stm32f4_toolchain_bin(&request.project);

        let build_id = compute_nrf52840_build_id(&request.project, request.language);

        let existing_path = std::env::var("PATH").unwrap_or_default();
        let parts: Vec<String> = vec![toolchain_bin.to_string_lossy().to_string(), existing_path];
        let mut env_os: Vec<(std::ffi::OsString, std::ffi::OsString)> =
            vec![("PATH".into(), parts.join(":").into())];
        env_os.push(("BENCH_BUILD_ID".into(), build_id.clone().into()));
        if request.language == Language::Rust {
            env_os.push(("RUST".into(), "1".into()));
        }

        if request.clean {
            let clean_spec = CommandSpec {
                program: PathBuf::from("make"),
                args: vec!["-C".into(), firmware_dir.as_os_str().into(), "clean".into()],
                cwd: firmware_dir.clone(),
                env: env_os.clone(),
                timeout: STM32F4_BUILD_TIMEOUT,
            };
            eprintln!("[clean] make clean in {}", firmware_dir.display());
            process::run_success(&clean_spec)?;
            eprintln!("[clean] make clean complete");
        }

        let build_spec = CommandSpec {
            program: PathBuf::from("make"),
            args: vec!["-C".into(), firmware_dir.as_os_str().into(), "all".into()],
            cwd: firmware_dir.clone(),
            env: env_os,
            timeout: STM32F4_BUILD_TIMEOUT,
        };
        eprintln!(
            "[build] running make (nRF52840) in {} (build_id={})",
            firmware_dir.display(),
            build_id
        );
        let output = process::run_success(&build_spec)?;
        eprintln!(
            "[build] make succeeded ({} bytes output, {} bytes stderr)",
            output.stdout.len(),
            output.stderr.len()
        );

        let build_dir = firmware_dir.join("build");
        let bin = build_dir.join("freertos-nrf52840-bench.bin");
        if !bin.is_file() {
            return Err(Error::Build(format!(
                "firmware bin not found: {}",
                bin.display()
            )));
        }

        Ok(Artifact {
            images: vec![build_dir.join("freertos-nrf52840-bench.elf")],
            directory: build_dir,
            board: request.board,
            build_id,
        })
    }
}

/// Walk up from `start` to the project root (the ancestor that holds
/// `config/rtos.toml`), without assuming the firmware tree's depth.
fn project_root_from(start: &Path) -> Option<PathBuf> {
    let mut cur = start;
    loop {
        if cur.join("config/rtos.toml").is_file() {
            return Some(cur.to_path_buf());
        }
        cur = cur.parent()?;
    }
}

/// Flash the ESP32-C6 image over USB-Serial/JTAG via `idf.py flash`.
///
/// The artifact directory is `<project>/firmware/c-kernels/freertos/c/build`; the
/// project root is walked back from it so the ESP-IDF environment resolves.
fn flash_esp32c6(artifact: &Artifact, device: &Device) -> Result<()> {
    // Locate the project root by marker rather than a fixed number of parents:
    // the firmware tree has gained levels (e.g. `c-kernels/`), so a hard-coded
    // walk-back landed on `firmware/` and looked for `firmware/.local` (the
    // ESP-IDF tools are provisioned at the project root's `.local`).
    let project_root = project_root_from(&artifact.directory).ok_or_else(|| {
        Error::Flash("cannot resolve project root from artifact directory".into())
    })?;
    let firmware_dir = project_root.join("firmware/c-kernels/freertos/c");
    let (idf_dir, env) = resolve_idf(&project_root)?;

    let mut args: Vec<OsString> = vec![
        idf_dir.join("tools/idf.py").into_os_string(),
        OsString::from("flash"),
    ];
    if !device.selector.trim().is_empty() {
        args.push(OsString::from("--port"));
        args.push(device.selector.trim().to_string().into());
    }

    let env_os: Vec<(OsString, OsString)> = env
        .iter()
        .map(|(k, v)| (k.clone().into(), v.clone().into()))
        .collect();

    eprintln!(
        "[flash] idf.py flash (artifact={}, port={})",
        artifact.build_id,
        if device.selector.trim().is_empty() {
            "auto-detect"
        } else {
            device.selector.trim()
        }
    );

    let spec = CommandSpec {
        program: environment::python(&env)?,
        args,
        cwd: firmware_dir,
        env: env_os,
        timeout: IDF_TIMEOUT,
    };
    let output = process::run_success(&spec)?;
    eprintln!(
        "[flash] idf.py flash succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );
    Ok(())
}

/// Flash the STM32F4 image through the shared probe-rs backend.
fn build_stm32h723(request: &BuildRequest) -> Result<Artifact> {
    let firmware_dir = request
        .project
        .join("firmware/c-kernels/freertos/c/stm32h723");
    let toolchain_bin = stm32f4_toolchain_bin(&request.project);

    let build_id = compute_stm32h723_build_id(&request.project, request.board, request.language);

    let existing_path = std::env::var("PATH").unwrap_or_default();
    let parts: Vec<String> = vec![toolchain_bin.to_string_lossy().to_string(), existing_path];
    let mut env_os: Vec<(std::ffi::OsString, std::ffi::OsString)> =
        vec![("PATH".into(), parts.join(":").into())];
    env_os.push(("BENCH_BUILD_ID".into(), build_id.clone().into()));
    if request.language == Language::Rust {
        env_os.push(("RUST".into(), "1".into()));
    }

    if request.clean {
        let clean_spec = CommandSpec {
            program: PathBuf::from("make"),
            args: vec!["-C".into(), firmware_dir.as_os_str().into(), "clean".into()],
            cwd: firmware_dir.clone(),
            env: env_os.clone(),
            timeout: STM32F4_BUILD_TIMEOUT,
        };
        eprintln!("[clean] make clean in {}", firmware_dir.display());
        process::run_success(&clean_spec)?;
        eprintln!("[clean] make clean complete");
    }

    let build_spec = CommandSpec {
        program: PathBuf::from("make"),
        args: vec!["-C".into(), firmware_dir.as_os_str().into(), "all".into()],
        cwd: firmware_dir.clone(),
        env: env_os,
        timeout: STM32F4_BUILD_TIMEOUT,
    };
    eprintln!(
        "[build] running make (STM32H723) in {} (build_id={})",
        firmware_dir.display(),
        build_id
    );
    let output = process::run_success(&build_spec)?;
    eprintln!(
        "[build] make succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let build_dir = firmware_dir.join("build");
    if !build_dir.is_dir() {
        return Err(Error::Build(format!(
            "build directory not found after build: {}",
            build_dir.display()
        )));
    }
    let bin = build_dir.join("freertos-stm32h723-bench.bin");
    if !bin.is_file() {
        return Err(Error::Build(format!(
            "firmware bin not found: {}",
            bin.display()
        )));
    }

    Ok(Artifact {
        images: vec![build_dir.join("freertos-stm32h723-bench.elf")],
        directory: build_dir,
        board: request.board,
        build_id,
    })
}

/// Flash the STM32H723 FreeRTOS binary at 0x0800_0000 over the external
/// ST-LINK/V2 (serial pinned; the Discovery uses 52C3BF6D).
fn flash_stm32h723(artifact: &Artifact, _device: &Device) -> Result<()> {
    let bin = artifact.directory.join("freertos-stm32h723-bench.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "firmware bin not found: {}",
            bin.display()
        )));
    }
    eprintln!(
        "[flash] probe-rs download {} (artifact={}, probe=0483:3748:37C3BF71)",
        bin.display(),
        artifact.build_id
    );

    let executable = if PathBuf::from("/home/chronikle/.cargo/bin/probe-rs").is_file() {
        PathBuf::from("/home/chronikle/.cargo/bin/probe-rs")
    } else {
        PathBuf::from("probe-rs")
    };
    let request = rtos_core::flash::FlashRequest {
        file: bin,
        mcu: "STM32H723ZG".into(),
        probe: "0483:3748:37C3BF71".into(),
        format: rtos_core::flash::ImageFormat::Bin,
        address: Some(0x0800_0000),
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

fn flash_stm32f4(artifact: &Artifact, _device: &Device) -> Result<()> {
    let bin = artifact.directory.join("freertos-stm32f4-bench.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "firmware bin not found: {}",
            bin.display()
        )));
    }
    eprintln!(
        "[flash] probe-rs download {} (artifact={}, probe=0483:3748:52C3BF6D)",
        bin.display(),
        artifact.build_id
    );

    let executable = if PathBuf::from("/home/chronikle/.cargo/bin/probe-rs").is_file() {
        PathBuf::from("/home/chronikle/.cargo/bin/probe-rs")
    } else {
        PathBuf::from("probe-rs")
    };
    let request = rtos_core::flash::FlashRequest {
        file: bin,
        mcu: "STM32F407VGTx".into(),
        /* Pin the ST-Link serial (boards/stm32f4discovery/board.toml and the
         * other plugins do the same): a second 0483:3748 probe makes the
         * serial-less selector ambiguous. */
        probe: "0483:3748:52C3BF6D".into(),
        format: rtos_core::flash::ImageFormat::Bin,
        address: Some(0x0800_0000),
        bootloader: None,
        partition_table: None,
        reset: true,
        timeout_seconds: 120,
    };
    let backend = rtos_core::flash::ProbeRsFlash { executable };
    let results = artifact.directory.join("flash-results");
    let dir = rtos_core::flash::flash(&backend, &request, &results)?;
    eprintln!(
        "[flash] probe-rs download succeeded; evidence {}",
        dir.display()
    );
    Ok(())
}

/// Flash the nRF52840 DK image through the shared probe-rs backend (J-Link).
fn flash_nrf52840(artifact: &Artifact, _device: &Device) -> Result<()> {
    let bin = artifact.directory.join("freertos-nrf52840-bench.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "firmware bin not found: {}",
            bin.display()
        )));
    }
    eprintln!(
        "[flash] probe-rs download {} (artifact={}, probe=1366:1061:001050202809)",
        bin.display(),
        artifact.build_id
    );

    let executable = if PathBuf::from("/home/chronikle/.cargo/bin/probe-rs").is_file() {
        PathBuf::from("/home/chronikle/.cargo/bin/probe-rs")
    } else {
        PathBuf::from("probe-rs")
    };
    let request = rtos_core::flash::FlashRequest {
        file: bin,
        mcu: "nRF52840_xxAA".into(),
        probe: "1366:1061:001050202809".into(),
        format: rtos_core::flash::ImageFormat::Bin,
        address: Some(0x0000_0000),
        bootloader: None,
        partition_table: None,
        reset: true,
        timeout_seconds: 120,
    };
    let backend = rtos_core::flash::ProbeRsFlash { executable };
    let results = artifact.directory.join("flash-results");
    let dir = rtos_core::flash::flash(&backend, &request, &results)?;
    eprintln!(
        "[flash] probe-rs download succeeded; evidence {}",
        dir.display()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rtos_core::plugin_interface::{Board, BuildRequest, Language};
    use std::path::PathBuf;

    #[test]
    fn test_freertos_plugin_id() {
        let plugin = FreeRtosPlugin;
        assert_eq!(plugin.id(), "freertos");
    }

    #[test]
    fn test_route_for_esp32c6() {
        let plugin = FreeRtosPlugin;
        let route = plugin.route(Board::Esp32c6, Language::C).unwrap();

        assert_eq!(route.backend, "ESP-IDF FreeRTOS port");
        assert_eq!(route.target, "esp32c6");
        assert_eq!(route.recipe, "plugins/freertos/BUILD.md");
    }

    #[test]
    fn test_route_for_stm32f4discovery() {
        let plugin = FreeRtosPlugin;
        let route = plugin.route(Board::Stm32f4Discovery, Language::C).unwrap();

        assert_eq!(route.backend, "upstream FreeRTOS-Kernel + STM32CubeF4 BSP");
        assert_eq!(route.target, "GCC ARM_CM4F");
        assert_eq!(route.recipe, "plugins/freertos/BUILD.md");
    }

    #[test]
    fn test_validate_allows_rust_on_stm32f4_route() {
        let plugin = FreeRtosPlugin;
        let request = BuildRequest {
            project: PathBuf::from("/tmp/test"),
            board: Board::Stm32f4Discovery,
            language: Language::Rust,
            workload: "smoke".to_string(),
            output: PathBuf::from("/tmp/output"),
            clean: false,
        };

        // The route no longer rejects Rust by language; this project root fails
        // later on the missing firmware directory.
        let err = plugin.validate(&request).unwrap_err();
        assert!(
            !matches!(err, Error::Unsupported(_)),
            "Rust must not be rejected by language for stm32f4: {err}"
        );
    }

    #[test]
    fn test_validate_allows_rust_for_esp32c6() {
        let plugin = FreeRtosPlugin;
        let request = BuildRequest {
            project: PathBuf::from("/tmp/test"),
            board: Board::Esp32c6,
            language: Language::Rust,
            workload: "smoke".to_string(),
            output: PathBuf::from("/tmp/output"),
            clean: false,
        };

        // Rust is allowed for the C6 route; this project root fails later on the
        // missing firmware directory, not on the language.
        let err = plugin.validate(&request).unwrap_err();
        assert!(
            !matches!(err, Error::Unsupported(_)),
            "Rust must not be rejected by language for esp32c6: {err}"
        );
    }

    #[test]
    fn test_compute_c6_build_id_tags() {
        let c = compute_c6_build_id(Path::new("/tmp/test"), Language::C);
        assert!(
            c.starts_with("freertos-c6-bench-"),
            "unexpected build_id: {c}"
        );
        let rust = compute_c6_build_id(Path::new("/tmp/test"), Language::Rust);
        assert!(
            rust.starts_with("freertos-c6-rust-"),
            "unexpected build_id: {rust}"
        );
    }

    #[test]
    fn test_compute_stm32f4_build_id_nogit() {
        let id =
            compute_stm32f4_build_id(Path::new("/tmp/test"), Board::Stm32f4Discovery, Language::C);
        assert!(
            id.starts_with("freertos-stm32f4-bench-"),
            "unexpected build_id: {id}"
        );
        let id = compute_stm32f4_build_id(
            Path::new("/tmp/test"),
            Board::Stm32f4Discovery,
            Language::Rust,
        );
        assert!(
            id.starts_with("freertos-stm32f4-rust-"),
            "unexpected build_id: {id}"
        );
    }

    #[test]
    fn test_stm32f4_toolchain_bin_path() {
        let p = stm32f4_toolchain_bin(Path::new("/proj"));
        assert_eq!(
            p,
            PathBuf::from(
                "/proj/.local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin"
            )
        );
    }
}
