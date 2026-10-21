//! Zephyr host adapter: route, validate, build, and flash.
//!
//! STM32F4 Discovery builds via `west build` with the cross-compile toolchain
//! variant. STM32F4 flash goes through the shared probe-rs backend
//! (`rtos_core::flash`), matching the CLI; st-flash proved unreliable on this
//! bench. The ESP32-C6 variant flashes via esptool.
//! The `cross-compile` variant never sets `TOOLCHAIN_HOME`, so `CMAKE_OBJCOPY`,
//! `CMAKE_OBJDUMP`, and `CMAKE_READELF` must be passed explicitly.
//!
//! ESP32-C6 uses the Espressif RISC-V toolchain and flashes the single Zephyr
//! image at 0x0 over esptool; see the `esp32c6` submodule.
use rtos_core::{
    error::{Error, Result},
    plugin_interface::{Artifact, Board, BuildRequest, Device, Language, Route, RtosPlugin},
    process::{self, CommandSpec},
};
use std::path::{Path, PathBuf};
use std::time::Duration;

mod esp32c6;

const ZEPHYR_BUILD_TIMEOUT: Duration = Duration::from_secs(300);

/// Project-local path to the arm-none-eabi toolchain bin directory.
fn toolchain_bin(project: &Path) -> PathBuf {
    project.join(
        ".local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin",
    )
}

/// Project-local path to the west workspace root.
fn west_workspace(project: &Path) -> PathBuf {
    project.join(".local/west")
}

/// Project-local path to the Zephyr firmware source directory.
fn firmware_dir(project: &Path) -> PathBuf {
    project.join("firmware/c-kernels/zephyr/c")
}

/// Project-local path to the STM32F4 Zephyr application directory.
fn stm32f4_firmware_dir(project: &Path) -> PathBuf {
    project.join("firmware/c-kernels/zephyr/c/stm32f4")
}

/// Project-local path to the nRF52840 DK Zephyr application directory.
fn nrf52840dk_firmware_dir(project: &Path) -> PathBuf {
    project.join("firmware/c-kernels/zephyr/c/nrf52840dk")
}

/// Project-local path to the STM32H723 (FK723M1 core board) Zephyr application
/// directory.
fn stm32h723_firmware_dir(project: &Path) -> PathBuf {
    project.join("firmware/c-kernels/zephyr/c/stm32h723")
}

/// Language tag used in the STM32F4 Zephyr build_id.
fn language_tag(language: Language) -> &'static str {
    match language {
        Language::C => "bench",
        Language::Rust => "rust",
    }
}

/// Compute the STM32F4 Discovery Zephyr build_id from the project's git short
/// SHA. The language tag keeps C and Rust evidence separate while both are
/// embedded by the same C session.
pub fn compute_stm32f4_build_id(project: &Path, _board: Board, language: Language) -> String {
    let sha = git_short_sha(project);
    format!("zephyr-stm32f4-{}-{sha}", language_tag(language))
}

/// Backwards-compatible C build_id helper.
pub fn compute_build_id(project: &Path) -> String {
    compute_stm32f4_build_id(project, Board::Stm32f4Discovery, Language::C)
}

/// Compute the ESP32-C6 Zephyr build_id; the language tag keeps C and Rust
/// evidence separate (`zephyr-c6-bench-<sha>` / `zephyr-c6-rust-<sha>`).
pub fn compute_c6_build_id(project: &Path, language: Language) -> String {
    let sha = git_short_sha(project);
    format!("zephyr-c6-{}-{sha}", language_tag(language))
}

/// Compute the nRF52840 DK Zephyr build_id (C only for now).
pub fn compute_nrf52840dk_build_id(project: &Path, language: Language) -> String {
    let sha = git_short_sha(project);
    format!("zephyr-nrf52840dk-{}-{sha}", language_tag(language))
}

/// Compute the STM32H723 Zephyr build_id; the language tag keeps C and Rust
/// evidence separate (`zephyr-h723-bench-<sha>` / `zephyr-h723-rust-<sha>`).
pub fn compute_stm32h723_build_id(project: &Path, language: Language) -> String {
    let sha = git_short_sha(project);
    format!("zephyr-h723-{}-{sha}", language_tag(language))
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

/// Concrete adapter registered by the CLI, never by core.
pub struct ZephyrPlugin;

impl RtosPlugin for ZephyrPlugin {
    fn id(&self) -> &'static str {
        "zephyr"
    }

    fn route(&self, board: Board, _language: Language) -> Result<Route> {
        let (backend, target, notes) = match board {
            Board::Esp32c6 => (
                "Zephyr",
                "esp32c6_devkitc/esp32c6/hpcore",
                "Confirm DevKitC carrier; west workspace + hal_espressif. C default; Rust links a riscv32imac soft-float staticlib with -DBENCH_RUST=ON.",
            ),
            Board::Stm32f4Discovery => (
                "Zephyr",
                "stm32f4_disco",
                "west + cross-compile ARM toolchain; USART2 PA2/PA3 @115200. C default; Rust links a soft-float staticlib with -DBENCH_RUST=ON.",
            ),
            Board::Nrf52840Dk => (
                "Zephyr",
                "nrf52840dk/nrf52840",
                "west + cross-compile ARM toolchain; console on the J-Link VCOM UART0 @115200. Flash and debug via the onboard J-Link (probe-rs, chip nRF52840_xxAA). C only for now.",
            ),
            Board::Stm32h723 => (
                "Zephyr",
                "fk723m1_zgt6",
                "west + cross-compile ARM toolchain; builds the upstream fanke/fk723m1_zgt6 board (this exact hardware, 25 MHz HSE) with an app overlay setting a 520 MHz PLL. Console is USART1 PA9/PA10 on the CP2102 (no USB CDC). Flash over the external ST-LINK (probe-rs, chip STM32H723ZG). C default; Rust links a soft-float staticlib with -DBENCH_RUST=ON.",
            ),
        };
        Ok(Route {
            backend,
            target,
            notes,
            recipe: "plugins/zephyr/BUILD.md",
        })
    }

    fn validate(&self, request: &BuildRequest) -> Result<()> {
        let fw = firmware_dir(&request.project);
        if !fw.is_dir() {
            return Err(Error::Invalid(format!(
                "firmware directory not found: {}",
                fw.display()
            )));
        }
        match request.board {
            Board::Esp32c6 => {
                esp32c6::validate(&request.project)?;
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/zephyr/rust/Cargo.toml");
                    if !crate_manifest.is_file() {
                        return Err(Error::Build(format!(
                            "Rust crate manifest not found: {}",
                            crate_manifest.display()
                        )));
                    }
                }
                Ok(())
            }
            Board::Stm32f4Discovery => {
                let fw = stm32f4_firmware_dir(&request.project);
                let ws = west_workspace(&request.project);
                if !ws.is_dir() {
                    return Err(Error::Build(format!(
                        "west workspace not found at {}; run scripts/setup/provision-zephyr.sh",
                        ws.display()
                    )));
                }
                let tc = toolchain_bin(&request.project);
                if !tc.is_dir() {
                    return Err(Error::Build(format!(
                        "arm-none-eabi toolchain not found at {}",
                        tc.display()
                    )));
                }
                let cmake = fw.join("CMakeLists.txt");
                if !cmake.is_file() {
                    return Err(Error::Build(format!(
                        "CMakeLists.txt not found at {}",
                        cmake.display()
                    )));
                }
                let prj = fw.join("prj.conf");
                if !prj.is_file() {
                    return Err(Error::Build(format!(
                        "prj.conf not found at {}",
                        prj.display()
                    )));
                }
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/zephyr/rust/Cargo.toml");
                    if !crate_manifest.is_file() {
                        return Err(Error::Build(format!(
                            "Rust crate manifest not found: {}",
                            crate_manifest.display()
                        )));
                    }
                }
                Ok(())
            }
            Board::Nrf52840Dk => {
                let fw = nrf52840dk_firmware_dir(&request.project);
                let ws = west_workspace(&request.project);
                if !ws.is_dir() {
                    return Err(Error::Build(format!(
                        "west workspace not found at {}; run scripts/setup/provision-zephyr.sh",
                        ws.display()
                    )));
                }
                let tc = toolchain_bin(&request.project);
                if !tc.is_dir() {
                    return Err(Error::Build(format!(
                        "arm-none-eabi toolchain not found at {}",
                        tc.display()
                    )));
                }
                let cmake = fw.join("CMakeLists.txt");
                if !cmake.is_file() {
                    return Err(Error::Build(format!(
                        "CMakeLists.txt not found at {}",
                        cmake.display()
                    )));
                }
                let prj = fw.join("prj.conf");
                if !prj.is_file() {
                    return Err(Error::Build(format!(
                        "prj.conf not found at {}",
                        prj.display()
                    )));
                }
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/zephyr/rust/Cargo.toml");
                    if !crate_manifest.is_file() {
                        return Err(Error::Build(format!(
                            "Rust crate manifest not found: {}",
                            crate_manifest.display()
                        )));
                    }
                }
                Ok(())
            }
            Board::Stm32h723 => {
                let fw = stm32h723_firmware_dir(&request.project);
                let ws = west_workspace(&request.project);
                if !ws.is_dir() {
                    return Err(Error::Build(format!(
                        "west workspace not found at {}; run scripts/setup/provision-zephyr.sh",
                        ws.display()
                    )));
                }
                let tc = toolchain_bin(&request.project);
                if !tc.is_dir() {
                    return Err(Error::Build(format!(
                        "arm-none-eabi toolchain not found at {}",
                        tc.display()
                    )));
                }
                let cmake = fw.join("CMakeLists.txt");
                if !cmake.is_file() {
                    return Err(Error::Build(format!(
                        "CMakeLists.txt not found at {}",
                        cmake.display()
                    )));
                }
                let prj = fw.join("prj.conf");
                if !prj.is_file() {
                    return Err(Error::Build(format!(
                        "prj.conf not found at {}",
                        prj.display()
                    )));
                }
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/zephyr/rust/Cargo.toml");
                    if !crate_manifest.is_file() {
                        return Err(Error::Build(format!(
                            "Rust crate manifest not found: {}",
                            crate_manifest.display()
                        )));
                    }
                }
                Ok(())
            }
        }
    }

    fn build(&self, request: &BuildRequest) -> Result<Artifact> {
        match request.board {
            Board::Esp32c6 => esp32c6::build(request),
            Board::Stm32f4Discovery => build_stm32f4(request),
            Board::Nrf52840Dk => build_nrf52840dk(request),
            Board::Stm32h723 => build_stm32h723(request),
        }
    }

    fn flash(&self, artifact: &Artifact, device: &Device) -> Result<()> {
        match artifact.board {
            Board::Esp32c6 => esp32c6::flash(artifact, device),
            Board::Stm32f4Discovery => flash_stm32f4(artifact, device),
            Board::Nrf52840Dk => flash_nrf52840dk(artifact, device),
            Board::Stm32h723 => flash_stm32h723(artifact, device),
        }
    }
}

/// Build the STM32F4 Zephyr firmware via `west build`.
fn build_stm32f4(request: &BuildRequest) -> Result<Artifact> {
    let fw = stm32f4_firmware_dir(&request.project);
    let build_id = compute_stm32f4_build_id(&request.project, request.board, request.language);
    build_zephyr_arm(request, &fw, "stm32f4_disco", "build-test", build_id)
}

/// Build the nRF52840 DK (Cortex-M4F) Zephyr firmware via `west build`. The
/// console is UART0 on the default DK pins, exposed to the host by the J-Link
/// OB as a CDC ACM VCOM.
fn build_nrf52840dk(request: &BuildRequest) -> Result<Artifact> {
    let fw = nrf52840dk_firmware_dir(&request.project);
    let build_id = compute_nrf52840dk_build_id(&request.project, request.language);
    build_zephyr_arm(
        request,
        &fw,
        "nrf52840dk/nrf52840",
        "build-nrf52840dk",
        build_id,
    )
}

/// Build the STM32H723 (FK723M1 core board) Zephyr firmware via `west build` for
/// the Nucleo H723ZG target plus an app overlay for the board's 25 MHz HSE.
fn build_stm32h723(request: &BuildRequest) -> Result<Artifact> {
    let fw = stm32h723_firmware_dir(&request.project);
    let build_id = compute_stm32h723_build_id(&request.project, request.language);
    build_zephyr_arm(request, &fw, "fk723m1_zgt6", "build-stm32h723", build_id)
}

/// Shared ARM `west build` for a Cortex-M Zephyr app: sets the cross-compile
/// toolchain environment, builds into `<west>/<build_subdir>`, and returns the
/// resulting `zephyr.elf` artifact.
fn build_zephyr_arm(
    request: &BuildRequest,
    fw: &Path,
    target: &str,
    build_subdir: &str,
    build_id: String,
) -> Result<Artifact> {
    let project = &request.project;
    let ws = west_workspace(project);
    let tc = toolchain_bin(project);

    let cross_compile = format!("{}/arm-none-eabi-", tc.to_string_lossy());
    let objcopy = format!("{}/arm-none-eabi-objcopy", tc.to_string_lossy());
    let objdump = format!("{}/arm-none-eabi-objdump", tc.to_string_lossy());
    let readelf = format!("{}/arm-none-eabi-readelf", tc.to_string_lossy());

    let mut cmake_args: Vec<std::ffi::OsString> = vec![
        format!("-DCMAKE_OBJCOPY={}", objcopy).into(),
        format!("-DCMAKE_OBJDUMP={}", objdump).into(),
        format!("-DCMAKE_READELF={}", readelf).into(),
    ];
    if request.language == Language::Rust {
        cmake_args.push("-DBENCH_RUST=ON".into());
    }

    let existing_path = std::env::var("PATH").unwrap_or_default();
    let parts: Vec<String> = vec![
        tc.to_string_lossy().to_string(),
        project.join(".local/bin").to_string_lossy().to_string(),
        existing_path,
    ];

    let env_os: Vec<(std::ffi::OsString, std::ffi::OsString)> = vec![
        ("PATH".into(), parts.join(":").into()),
        ("ZEPHYR_TOOLCHAIN_VARIANT".into(), "cross-compile".into()),
        ("CROSS_COMPILE".into(), cross_compile.into()),
    ];

    let west = ws.join("west");
    if !west.is_file() {
        return Err(Error::Build(format!(
            "west not found at {}",
            west.display()
        )));
    }

    let build_spec = CommandSpec {
        program: west,
        args: {
            let mut args: Vec<std::ffi::OsString> = vec![
                "build".into(),
                "-p".into(),
                "always".into(),
                "-d".into(),
                build_subdir.into(),
                "-b".into(),
                target.into(),
                fw.as_os_str().into(),
                "--".into(),
            ];
            args.extend(cmake_args.iter().cloned());
            args
        },
        cwd: ws.clone(),
        env: env_os,
        timeout: ZEPHYR_BUILD_TIMEOUT,
    };
    eprintln!(
        "[build] running west build (Zephyr {target}) in {} (build_id={})",
        ws.display(),
        build_id
    );
    let output = process::run_success(&build_spec)?;
    eprintln!(
        "[build] west build succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let build_dir = ws.join(build_subdir).join("zephyr");
    if !build_dir.is_dir() {
        return Err(Error::Build(format!(
            "build directory not found after build: {}",
            build_dir.display()
        )));
    }
    let elf = build_dir.join("zephyr.elf");
    if !elf.is_file() {
        return Err(Error::Build(format!(
            "zephyr.elf not found: {}",
            elf.display()
        )));
    }
    let bin = build_dir.join("zephyr.bin");
    if !bin.is_file() {
        return Err(Error::Build(format!(
            "zephyr.bin not found: {}",
            bin.display()
        )));
    }

    Ok(Artifact {
        directory: build_dir,
        board: request.board,
        build_id,
        images: vec![elf],
    })
}

/// Flash a Cortex-M Zephyr ELF through the shared probe-rs backend.
fn flash_probe_rs(artifact: &Artifact, mcu: &str, probe: &str, label: &str) -> Result<()> {
    let elf = artifact.directory.join("zephyr.elf");
    if !elf.is_file() {
        return Err(Error::Flash(format!(
            "zephyr.elf not found: {}",
            elf.display()
        )));
    }
    eprintln!(
        "[flash] probe-rs download {} (artifact={}, mcu={mcu}, probe={probe}, {label})",
        elf.display(),
        artifact.build_id
    );

    // Use the shared probe-rs backend (flash through the CLI's probe-rs path,
    // not st-flash directly). st-flash proved unreliable here.
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
    eprintln!(
        "[flash] probe-rs download succeeded; evidence {}",
        dir.display()
    );
    Ok(())
}

/// Flash the STM32F4 Zephyr ELF over the shared probe-rs ST-Link path.
fn flash_stm32f4(artifact: &Artifact, _device: &Device) -> Result<()> {
    /* Pin the ST-Link serial (two 0483:3748 probes are attached); see
     * boards/stm32f4discovery/board.toml. */
    flash_probe_rs(artifact, "STM32F407VGTx", "0483:3748:52C3BF6D", "stlink")
}

/// Flash the nRF52840 DK Zephyr ELF over its onboard J-Link and reset.
fn flash_nrf52840dk(artifact: &Artifact, _device: &Device) -> Result<()> {
    flash_probe_rs(artifact, "nRF52840_xxAA", "1366:1061:001050202809", "jlink")
}

/// Flash the STM32H723 (FK723M1) Zephyr ELF over the external ST-LINK/V2. The
/// ST-LINK serial is pinned because two 0483:3748 probes are attached (the
/// Discovery uses 52C3BF6D); see boards/stm32h723/board.toml.
fn flash_stm32h723(artifact: &Artifact, _device: &Device) -> Result<()> {
    flash_probe_rs(artifact, "STM32H723ZG", "0483:3748:37C3BF71", "stlink")
}

#[cfg(test)]
mod tests {
    use super::*;
    use rtos_core::plugin_interface::{Board, BuildRequest, Language};

    #[test]
    fn test_zephyr_plugin_id() {
        let plugin = ZephyrPlugin;
        assert_eq!(plugin.id(), "zephyr");
    }

    #[test]
    fn test_route_for_stm32f4discovery() {
        let plugin = ZephyrPlugin;
        let route = plugin.route(Board::Stm32f4Discovery, Language::C).unwrap();
        assert_eq!(route.backend, "Zephyr");
        assert_eq!(route.target, "stm32f4_disco");
        assert_eq!(route.recipe, "plugins/zephyr/BUILD.md");
    }

    #[test]
    fn test_route_for_esp32c6() {
        let plugin = ZephyrPlugin;
        let route = plugin.route(Board::Esp32c6, Language::C).unwrap();
        assert_eq!(route.backend, "Zephyr");
        assert_eq!(route.target, "esp32c6_devkitc/esp32c6/hpcore");
        assert_eq!(route.recipe, "plugins/zephyr/BUILD.md");
    }

    #[test]
    fn test_validate_allows_rust_for_stm32f4_language() {
        // Rust is allowed for the STM32F4 route; the project root is missing, so
        // validation still fails, but not because of the language.
        let plugin = ZephyrPlugin;
        let request = BuildRequest {
            project: PathBuf::from("/tmp/test"),
            board: Board::Stm32f4Discovery,
            language: Language::Rust,
            workload: "smoke".to_string(),
            output: PathBuf::from("/tmp/output"),
            clean: false,
        };
        let err = plugin.validate(&request).unwrap_err();
        assert!(
            !err.to_string().contains("only C"),
            "unexpected language rejection: {err}"
        );
    }

    #[test]
    fn test_validate_rejects_rust_for_esp32c6() {
        let plugin = ZephyrPlugin;
        let request = BuildRequest {
            project: PathBuf::from("/tmp/test"),
            board: Board::Esp32c6,
            language: Language::Rust,
            workload: "smoke".to_string(),
            output: PathBuf::from("/tmp/output"),
            clean: false,
        };
        assert!(plugin.validate(&request).is_err());
    }

    #[test]
    fn test_validate_rejects_missing_firmware_dir() {
        let plugin = ZephyrPlugin;
        let request = BuildRequest {
            project: PathBuf::from("/tmp/test"),
            board: Board::Stm32f4Discovery,
            language: Language::C,
            workload: "smoke".to_string(),
            output: PathBuf::from("/tmp/output"),
            clean: false,
        };
        assert!(plugin.validate(&request).is_err());
    }

    #[test]
    fn test_compute_build_id_nogit() {
        let id = compute_build_id(Path::new("/tmp/test"));
        assert!(
            id.starts_with("zephyr-stm32f4-bench-"),
            "unexpected build_id: {id}"
        );
    }

    #[test]
    fn test_compute_stm32f4_build_id_rust_tag() {
        let id = compute_stm32f4_build_id(
            Path::new("/tmp/test"),
            Board::Stm32f4Discovery,
            Language::Rust,
        );
        assert!(
            id.starts_with("zephyr-stm32f4-rust-"),
            "unexpected build_id: {id}"
        );
    }

    #[test]
    fn test_compute_c6_build_id_tags() {
        let c = compute_c6_build_id(Path::new("/tmp/test"), Language::C);
        assert!(
            c.starts_with("zephyr-c6-bench-"),
            "unexpected build_id: {c}"
        );
        let rust = compute_c6_build_id(Path::new("/tmp/test"), Language::Rust);
        assert!(
            rust.starts_with("zephyr-c6-rust-"),
            "unexpected build_id: {rust}"
        );
    }

    #[test]
    fn test_toolchain_bin_path() {
        let p = toolchain_bin(Path::new("/proj"));
        assert_eq!(
            p,
            PathBuf::from(
                "/proj/.local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin"
            )
        );
    }

    #[test]
    fn test_west_workspace_path() {
        let p = west_workspace(Path::new("/proj"));
        assert_eq!(p, PathBuf::from("/proj/.local/west"));
    }

    #[test]
    fn test_firmware_dir_path() {
        let p = firmware_dir(Path::new("/proj"));
        assert_eq!(p, PathBuf::from("/proj/firmware/c-kernels/zephyr/c"));
    }

    #[test]
    fn test_route_for_stm32h723() {
        let plugin = ZephyrPlugin;
        let route = plugin.route(Board::Stm32h723, Language::C).unwrap();
        assert_eq!(route.backend, "Zephyr");
        assert_eq!(route.target, "fk723m1_zgt6");
        assert_eq!(route.recipe, "plugins/zephyr/BUILD.md");
    }

    #[test]
    fn test_compute_stm32h723_build_id_tags() {
        let c = compute_stm32h723_build_id(Path::new("/tmp/test"), Language::C);
        assert!(
            c.starts_with("zephyr-h723-bench-"),
            "unexpected build_id: {c}"
        );
        let rust = compute_stm32h723_build_id(Path::new("/tmp/test"), Language::Rust);
        assert!(
            rust.starts_with("zephyr-h723-rust-"),
            "unexpected build_id: {rust}"
        );
    }

    #[test]
    fn test_stm32h723_firmware_dir_path() {
        let p = stm32h723_firmware_dir(Path::new("/proj"));
        assert_eq!(
            p,
            PathBuf::from("/proj/firmware/c-kernels/zephyr/c/stm32h723")
        );
    }
}
