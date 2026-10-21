//! NuttX host adapter: route, validate, build, and flash.
//!
//! STM32F4 Discovery builds via the NuttX CMake build system with the
//! `stm32f4discovery:bench` board configuration. The RTOS benchmark application
//! is registered as a built-in app via Kconfig/Make.defs/Makefile/CMakeLists.txt
//! in firmware/c-kernels/nuttx/c/ and enabled by a bench defconfig. Flash goes through
//! `rtos_core::flash` (probe-rs, same ST-Link probe as FreeRTOS/Zephyr).
use rtos_core::{
    error::{Error, Result},
    plugin_interface::{Artifact, Board, BuildRequest, Device, Language, Route, RtosPlugin},
    process::{self, CommandSpec},
};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

const NUTTX_BUILD_TIMEOUT: Duration = Duration::from_secs(600);
const FLASH_TIMEOUT: Duration = Duration::from_secs(120);

/// Project-local path to the arm-none-eabi toolchain bin directory.
fn toolchain_bin(project: &Path) -> PathBuf {
    project.join(
        ".local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin",
    )
}

/// Project-local path to the NuttX kernel source tree.
fn nuttx_dir(project: &Path) -> PathBuf {
    project.join(".local/source/nuttx")
}

/// Project-local path to the NuttX apps source tree.
fn nuttx_apps_dir(project: &Path) -> PathBuf {
    project.join(".local/source/nuttx-apps")
}

/// Project-local path to the NuttX Python venv (contains kconfiglib).
fn nuttx_venv(project: &Path) -> PathBuf {
    project.join(".local/venv/nuttx")
}

/// Project-local path to the NuttX firmware source directory.
fn firmware_dir(project: &Path) -> PathBuf {
    project.join("firmware/c-kernels/nuttx/c")
}

/// Project-local path to the ESP-IDF RISC-V toolchain bin directory.
///
/// The installed version directory is discovered rather than hardcoded so a
/// toolchain update does not silently break the route.
fn esp_toolchain_bin(project: &Path) -> Result<PathBuf> {
    let root = project.join(".local/toolchains/esp-idf/tools/riscv32-esp-elf");
    let entries = fs::read_dir(&root).map_err(|e| {
        Error::Build(format!(
            "ESP RISC-V toolchain root not found at {}: {e}",
            root.display()
        ))
    })?;
    for entry in entries.flatten() {
        let bin = entry.path().join("riscv32-esp-elf/bin");
        if bin.join("riscv32-esp-elf-gcc").is_file() {
            return Ok(bin);
        }
    }
    Err(Error::Build(format!(
        "no riscv32-esp-elf-gcc under {}",
        root.display()
    )))
}

/// Project-local path to the `riscv-none-elf-*` symlink shim.
///
/// NuttX's CMake toolchain file only looks for `riscv-none-elf`, `riscv32-unknown-elf`
/// or `riscv64-unknown-elf` and ignores `-DCMAKE_C_COMPILER`, so the ESP
/// `riscv32-esp-elf` toolchain is exposed under that name.
fn riscv_shim_dir(project: &Path) -> PathBuf {
    project.join(".local/toolchains/riscv-none-elf-shim/bin")
}

/// Create or repair one project-local toolchain symlink.
///
/// `Path::exists()` follows symlinks, so a stale shim whose target moved is
/// reported as absent and a blind `symlink` call then fails with EEXIST. Repair
/// dangling or retargeted symlinks, but never replace a regular file.
fn ensure_symlink(target: &Path, link: &Path) -> Result<()> {
    match fs::symlink_metadata(link) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            if fs::read_link(link).ok().as_deref() == Some(target) {
                return Ok(());
            }
            fs::remove_file(link).map_err(|e| {
                Error::Build(format!("remove stale symlink {}: {e}", link.display()))
            })?;
        }
        Ok(_) => {
            return Err(Error::Build(format!(
                "refusing to replace non-symlink toolchain path {}",
                link.display()
            )));
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            return Err(Error::Build(format!(
                "inspect toolchain path {}: {e}",
                link.display()
            )));
        }
    }
    std::os::unix::fs::symlink(target, link)
        .map_err(|e| Error::Build(format!("symlink {}: {e}", link.display())))
}

/// Create the `riscv-none-elf-*` symlinks for the ESP toolchain if needed.
fn ensure_riscv_shim(project: &Path) -> Result<PathBuf> {
    let src = esp_toolchain_bin(project)?;
    let shim = riscv_shim_dir(project);
    fs::create_dir_all(&shim).map_err(|e| Error::Build(format!("create riscv shim dir: {e}")))?;
    let entries =
        fs::read_dir(&src).map_err(|e| Error::Build(format!("read {}: {e}", src.display())))?;
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        let Some(suffix) = name.strip_prefix("riscv32-esp-elf-") else {
            continue;
        };
        let link = shim.join(format!("riscv-none-elf-{suffix}"));
        let target = fs::canonicalize(entry.path())
            .map_err(|e| Error::Build(format!("canonicalize {}: {e}", entry.path().display())))?;
        ensure_symlink(&target, &link)?;
    }
    Ok(shim)
}

/// Project-local esptool used to build the ESP image and flash it.
fn esptool_bin(project: &Path) -> Result<PathBuf> {
    for candidate in [
        project.join(".local/venv/zephyr/bin/esptool"),
        project.join(".local/venv/nuttx/bin/esptool"),
    ] {
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err(Error::Build(format!(
        "esptool not found; expected {}/.local/venv/zephyr/bin/esptool",
        project.display()
    )))
}

fn project_root(artifact: &Artifact) -> Result<PathBuf> {
    artifact
        .directory
        .ancestors()
        .find(|dir| dir.join(".local/source/nuttx").is_dir())
        .map(Path::to_path_buf)
        .ok_or_else(|| {
            Error::Flash(format!(
                "project root not found above {}",
                artifact.directory.display()
            ))
        })
}

/// Compute the NuttX build_id from the project's git short SHA.
pub fn compute_build_id(project: &Path, board: Board) -> String {
    compute_build_id_lang(project, board, Language::C)
}

/// Language-aware NuttX build_id; adds a `-rust-` tag so C and Rust evidence
/// stay separate.
pub fn compute_build_id_lang(project: &Path, board: Board, language: Language) -> String {
    let sha = git_short_sha(project);
    let prefix = match board {
        Board::Esp32c6 => match language {
            Language::C => "nuttx-c6-bench",
            Language::Rust => "nuttx-c6-rust",
        },
        Board::Stm32f4Discovery => match language {
            Language::C => "nuttx-stm32f4-bench",
            Language::Rust => "nuttx-stm32f4-rust",
        },
        Board::Nrf52840Dk => match language {
            Language::C => "nuttx-nrf52840dk-bench",
            Language::Rust => "nuttx-nrf52840dk-rust",
        },
        Board::Stm32h723 => match language {
            Language::C => "nuttx-h723-bench",
            Language::Rust => "nuttx-h723-rust",
        },
    };
    format!("{prefix}-{sha}")
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
pub struct NuttXPlugin;

impl RtosPlugin for NuttXPlugin {
    fn id(&self) -> &'static str {
        "nuttx"
    }

    fn route(&self, board: Board, _language: Language) -> Result<Route> {
        let (backend, target, notes) = match board {
            Board::Esp32c6 => (
                "NuttX",
                "esp32c6-devkitc:nsh",
                "NSH is an upstream bring-up baseline; register our application separately. C default; Rust links a riscv32imac soft-float staticlib with -DBENCH_RUST=ON.",
            ),
            Board::Stm32f4Discovery => (
                "NuttX",
                "stm32f4discovery:bench",
                "CMake build with bench defconfig; built-in app over USART2 console. C default; Rust links a soft-float staticlib with -DBENCH_RUST=ON.",
            ),
            Board::Nrf52840Dk => (
                "NuttX",
                "nrf52840-dk:bench",
                "CMake build with bench defconfig; built-in app over UART0 (J-Link VCOM), 64 MHz DWT timestamps. C only for now.",
            ),
            Board::Stm32h723 => (
                "NuttX",
                "nucleo-h723zg:bench",
                "CMake build with a bench defconfig for the FK723M1 core board; USART1 PA9/PA10 to a CP2102 at 115200. C default; Rust links a soft-float staticlib with -DBENCH_RUST=ON.",
            ),
        };
        Ok(Route {
            backend,
            target,
            notes,
            recipe: "plugins/nuttx/BUILD.md",
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
                let nuttx = nuttx_dir(&request.project);
                if !nuttx.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX source not found at {}; clone apache/nuttx per plugins/nuttx/BUILD.md",
                        nuttx.display()
                    )));
                }
                let apps = nuttx_apps_dir(&request.project);
                if !apps.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX apps source not found at {}; clone apache/nuttx-apps per plugins/nuttx/BUILD.md",
                        apps.display()
                    )));
                }
                let defconfig = fw.join("bench_c6_defconfig");
                if !defconfig.is_file() {
                    return Err(Error::Build(format!(
                        "bench_c6_defconfig not found at {}",
                        defconfig.display()
                    )));
                }
                // Resolve the toolchain and esptool now so a missing prerequisite
                // fails validation rather than halfway through the build.
                let _ = esp_toolchain_bin(&request.project)?;
                let _ = esptool_bin(&request.project)?;
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/nuttx/rust/Cargo.toml");
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
                let nuttx = nuttx_dir(&request.project);
                if !nuttx.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX source not found at {}; clone apache/nuttx per plugins/nuttx/BUILD.md",
                        nuttx.display()
                    )));
                }
                let apps = nuttx_apps_dir(&request.project);
                if !apps.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX apps source not found at {}; clone apache/nuttx-apps per plugins/nuttx/BUILD.md",
                        apps.display()
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
                let defconfig = fw.join("bench_defconfig");
                if !defconfig.is_file() {
                    return Err(Error::Build(format!(
                        "bench_defconfig not found at {}",
                        defconfig.display()
                    )));
                }
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/nuttx/rust/Cargo.toml");
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
                let nuttx = nuttx_dir(&request.project);
                if !nuttx.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX source not found at {}; clone apache/nuttx per plugins/nuttx/BUILD.md",
                        nuttx.display()
                    )));
                }
                let apps = nuttx_apps_dir(&request.project);
                if !apps.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX apps source not found at {}; clone apache/nuttx-apps per plugins/nuttx/BUILD.md",
                        apps.display()
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
                let defconfig = fw.join("bench_nrf52840dk_defconfig");
                if !defconfig.is_file() {
                    return Err(Error::Build(format!(
                        "bench_nrf52840dk_defconfig not found at {}",
                        defconfig.display()
                    )));
                }
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/nuttx/rust/Cargo.toml");
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
                let nuttx = nuttx_dir(&request.project);
                if !nuttx.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX source not found at {}; clone apache/nuttx per plugins/nuttx/BUILD.md",
                        nuttx.display()
                    )));
                }
                let apps = nuttx_apps_dir(&request.project);
                if !apps.is_dir() {
                    return Err(Error::Build(format!(
                        "NuttX apps source not found at {}; clone apache/nuttx-apps per plugins/nuttx/BUILD.md",
                        apps.display()
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
                let defconfig = fw.join("bench_h723_defconfig");
                if !defconfig.is_file() {
                    return Err(Error::Build(format!(
                        "bench_h723_defconfig not found at {}",
                        defconfig.display()
                    )));
                }
                if request.language == Language::Rust {
                    let crate_manifest = request
                        .project
                        .join("firmware/c-kernels/nuttx/rust/Cargo.toml");
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
            Board::Esp32c6 => build_c6(request),
            Board::Stm32f4Discovery => build_stm32f4(request),
            Board::Nrf52840Dk => build_nrf52840dk(request),
            Board::Stm32h723 => build_h723(request),
        }
    }

    fn flash(&self, artifact: &Artifact, device: &Device) -> Result<()> {
        match artifact.board {
            Board::Esp32c6 => flash_c6(artifact, device),
            Board::Stm32f4Discovery => flash_stm32f4(artifact, device),
            Board::Nrf52840Dk => flash_nrf52840dk(artifact, device),
            Board::Stm32h723 => flash_h723(artifact, device),
        }
    }
}

/// Prepare the NuttX build workspace: wire the bench app into the apps tree
/// and the bench defconfig into the board config directory.
///
/// The NuttX CMake build discovers apps through `add_subdirectory(NUTTX_APPS_DIR
/// apps)`, which recurses via `nuttx_add_subdirectory()`. That function only
/// descends into subdirectories containing a `CMakeLists.txt`, so the bench
/// app is wired in by creating a `bench` subdirectory inside nuttx-apps that
/// contains a single `add_subdirectory(<abs path>)` pointing at
/// `firmware/c-kernels/nuttx/c`. The bench defconfig is copied into the board config
/// directory as `bench/defconfig` so it can be addressed as
/// `stm32f4discovery:bench`.
/// Raise the ESP32-C6 USB-Serial-JTAG tty ring buffer in the vendored NuttX
/// driver.
///
/// `esp_usbserial.c` hardcodes `ESP_USBCDC_BUFFERSIZE 64`, so the NuttX tty
/// ring buffer holds only 63 bytes. A host frame of 64 bytes or more (the
/// protocol allows up to 1024, and the `start` frame is ~134) fills the ring;
/// `uart_recvchars` then leaves the tail in the hardware FIFO, but the
/// `SERIAL_OUT_RECV_PKT` interrupt is edge-like, so the remainder is never
/// drained until another host packet arrives. The frame is truncated and the
/// device never sees a complete line. Raise the buffer to 2048 so a full
/// protocol frame fits.
///
/// Idempotent: once the replacement is present the file is left untouched; an
/// unexpected file without either form is an error rather than a silent no-op.
fn patch_esp_usbserial_buffer(nuttx: &Path) -> Result<()> {
    let path = nuttx.join("arch/risc-v/src/common/espressif/esp_usbserial.c");
    let src = fs::read_to_string(&path)
        .map_err(|e| Error::Build(format!("read {}: {e}", path.display())))?;
    if src.contains("ESP_USBCDC_BUFFERSIZE 2048") {
        return Ok(());
    }
    if !src.contains("#define ESP_USBCDC_BUFFERSIZE 64") {
        return Err(Error::Build(format!(
            "esp_usbserial.c does not define ESP_USBCDC_BUFFERSIZE 64 ({}); \
             the vendored driver changed, review the buffer patch",
            path.display()
        )));
    }
    let patched = src.replace(
        "#define ESP_USBCDC_BUFFERSIZE 64",
        "#define ESP_USBCDC_BUFFERSIZE 2048",
    );
    fs::write(&path, patched)
        .map_err(|e| Error::Build(format!("patch {}: {e}", path.display())))?;
    Ok(())
}

fn prepare_build_workspace(project: &Path, board: Board) -> Result<()> {
    let nuttx = nuttx_dir(project);
    let apps = nuttx_apps_dir(project);
    let fw = firmware_dir(project);

    patch_esp_usbserial_buffer(&nuttx)?;

    // Wire the bench app into the apps tree via a two-level shim.
    //
    // The nuttx-apps top-level CMakeLists.txt uses explicit add_subdirectory
    // calls (no GLOB), so we must add our shim under `external/`, which is
    // conditionally included:
    //   if(EXISTS ${CMAKE_CURRENT_LIST_DIR}/external)
    //     add_subdirectory(external)
    //   endif()
    //
    // Kconfig discovery (nuttx_generate_kconfig at the apps level) GLOBs for
    // directories containing a Kconfig or CMakeLists.txt. It only discovers
    // directories with CMakeLists.txt (not Kconfig-only dirs), so we create
    // an intermediate `bench_rtos` subdirectory that contains only a
    // CMakeLists.txt with `add_subdirectory(fw)`. The Kconfig file lives
    // directly in fw/, and nuttx_generate_kconfig finds it via the
    // EXTERNAL_DIRECTORIES path (CMAKE_CURRENT_SOURCE_DIR).
    let external_shim = apps.join("external");
    fs::create_dir_all(&external_shim)
        .map_err(|e| Error::Build(format!("create external shim dir: {e}")))?;
    // external/CMakeLists.txt: required by the nuttx-apps top-level
    // `add_subdirectory(external)`. Uses nuttx_add_subdirectory to GLOB
    // for subdirectories containing a CMakeLists.txt.
    let external_cml = external_shim.join("CMakeLists.txt");
    fs::write(&external_cml, "nuttx_add_subdirectory()\n")
        .map_err(|e| Error::Build(format!("write external CMakeLists: {e}")))?;

    // bench_rtos/: intermediate shim discovered by nuttx_add_subdirectory's
    // GLOB. Contains a CMakeLists.txt (add_subdirectory to fw) and a Kconfig
    // (source to fw/Kconfig by absolute path).
    let bench_rtos_dir = external_shim.join("bench_rtos");
    fs::create_dir_all(&bench_rtos_dir)
        .map_err(|e| Error::Build(format!("create bench_rtos shim dir: {e}")))?;

    // external/Kconfig: the NuttX Kconfig discovery at the apps level
    // requires a Kconfig file in each first-level directory to include it
    // in the root Kconfig. We source the bench_rtos Kconfig from here by
    // absolute path (the Kconfig `source` directive does not support
    // relative paths that resolve outside the Kconfig file's directory).
    let bench_rtos_kconfig = bench_rtos_dir.join("Kconfig");
    let external_kconfig = external_shim.join("Kconfig");
    let ext_kconfig_content = format!("source \"{}\"\n", bench_rtos_kconfig.to_string_lossy());
    fs::write(&external_kconfig, ext_kconfig_content)
        .map_err(|e| Error::Build(format!("write external Kconfig: {e}")))?;
    let shim_cml = bench_rtos_dir.join("CMakeLists.txt");
    let shim_content = format!(
        "add_subdirectory({} bench_rtos_app)\n",
        fw.to_string_lossy()
    );
    fs::write(&shim_cml, shim_content)
        .map_err(|e| Error::Build(format!("write bench_rtos shim CMakeLists: {e}")))?;

    // bench_rtos/Kconfig: the NuttX Kconfig discovery at the apps level
    // GLOBs for directories containing a Kconfig or CMakeLists.txt. The
    // Kconfig file in our firmware dir is outside the apps tree, so we
    // create a Kconfig here that sources it by absolute path. The Kconfig
    // `source` directive does not support relative paths that escape the
    // apps tree, so we use an absolute path.
    let fw_kconfig = fw.join("Kconfig");
    let shim_kconfig = bench_rtos_dir.join("Kconfig");
    let kconfig_content = format!("source \"{}\"\n", fw_kconfig.to_string_lossy());
    fs::write(&shim_kconfig, kconfig_content)
        .map_err(|e| Error::Build(format!("write bench_rtos shim Kconfig: {e}")))?;

    // Copy the board's bench defconfig into its config directory.
    let (board_cfg_dir, defconfig_name) = match board {
        Board::Esp32c6 => (
            nuttx.join("boards/risc-v/esp32c6/esp32c6-devkitc/configs/bench"),
            "bench_c6_defconfig",
        ),
        Board::Stm32f4Discovery => (
            nuttx.join("boards/arm/stm32f4/stm32f4discovery/configs/bench"),
            "bench_defconfig",
        ),
        Board::Nrf52840Dk => (
            nuttx.join("boards/arm/nrf52/nrf52840-dk/configs/bench"),
            "bench_nrf52840dk_defconfig",
        ),
        Board::Stm32h723 => (
            nuttx.join("boards/arm/stm32h7/nucleo-h723zg/configs/bench"),
            "bench_h723_defconfig",
        ),
    };
    fs::create_dir_all(&board_cfg_dir)
        .map_err(|e| Error::Build(format!("create bench config dir: {e}")))?;
    let src_defconfig = fw.join(defconfig_name);
    let dst_defconfig = board_cfg_dir.join("defconfig");
    fs::copy(&src_defconfig, &dst_defconfig)
        .map_err(|e| Error::Build(format!("copy bench defconfig: {e}")))?;

    Ok(())
}

/// Build the STM32F4 NuttX firmware via CMake with the bench defconfig.
fn build_stm32f4(request: &BuildRequest) -> Result<Artifact> {
    let project = &request.project;
    let nuttx = nuttx_dir(project);
    let apps = nuttx_apps_dir(project);
    let tc = toolchain_bin(project);
    let venv = nuttx_venv(project);
    let build_id = compute_build_id_lang(project, request.board, request.language);

    prepare_build_workspace(project, request.board)?;

    let build_dir = nuttx.join("build-bench");

    if request.clean && build_dir.is_dir() {
        eprintln!(
            "[clean] removing stale NuttX build dir {}",
            build_dir.display()
        );
        fs::remove_dir_all(&build_dir)
            .map_err(|e| Error::Build(format!("remove stale build dir: {e}")))?;
    }

    let existing_path = std::env::var("PATH").unwrap_or_default();
    let path_value = [
        tc.to_string_lossy().to_string(),
        venv.join("bin").to_string_lossy().to_string(),
        existing_path,
    ]
    .join(":");

    let env: Vec<(std::ffi::OsString, std::ffi::OsString)> = vec![
        ("PATH".into(), path_value.into()),
        (
            "NUTTX_APPS_DIR".into(),
            apps.to_string_lossy().to_string().into(),
        ),
    ];

    let cmake_configure = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "-S".into(),
            nuttx.as_os_str().into(),
            "-B".into(),
            build_dir.as_os_str().into(),
            "-DCMAKE_BUILD_TYPE=Release".into(),
            "-DBOARD_CONFIG=boards/arm/stm32f4/stm32f4discovery/configs/bench".into(),
            format!("-DBENCH_BUILD_ID={build_id}").into(),
            format!("-DNUTTX_APPS_DIR={}", apps.to_string_lossy()).into(),
            if request.language == Language::Rust {
                "-DBENCH_RUST=ON".into()
            } else {
                "-DBENCH_RUST=OFF".into()
            },
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!(
        "[build] cmake configure (NuttX stm32f4discovery:bench) in {} (build_id={})",
        build_dir.display(),
        build_id
    );
    let output = process::run_success(&cmake_configure)?;
    eprintln!(
        "[build] cmake configure succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let nproc = std::thread::available_parallelism()
        .map(|n| n.get().to_string())
        .unwrap_or_else(|_| "4".to_string());
    let cmake_build = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "--build".into(),
            build_dir.as_os_str().into(),
            format!("-j{}", nproc).into(),
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!("[build] cmake build (NuttX stm32f4discovery:bench)");
    let output = process::run_success(&cmake_build)?;
    eprintln!(
        "[build] cmake build succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let elf = build_dir.join("nuttx");
    if !elf.is_file() {
        return Err(Error::Build(format!(
            "nuttx ELF not found: {}",
            elf.display()
        )));
    }
    let bin = build_dir.join("nuttx.bin");
    if !bin.is_file() {
        return Err(Error::Build(format!(
            "nuttx.bin not found: {}",
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

/// Build the STM32H723 (FK723M1 core board) NuttX firmware via CMake with the
/// bench defconfig. Console is USB CDC ACM (no hardware UART on this board).
fn build_h723(request: &BuildRequest) -> Result<Artifact> {
    let project = &request.project;
    let nuttx = nuttx_dir(project);
    let apps = nuttx_apps_dir(project);
    let tc = toolchain_bin(project);
    let venv = nuttx_venv(project);
    let build_id = compute_build_id_lang(project, request.board, request.language);

    prepare_build_workspace(project, request.board)?;

    let build_dir = nuttx.join("build-bench-h723");

    if request.clean && build_dir.is_dir() {
        eprintln!(
            "[clean] removing stale NuttX build dir {}",
            build_dir.display()
        );
        fs::remove_dir_all(&build_dir)
            .map_err(|e| Error::Build(format!("remove stale build dir: {e}")))?;
    }

    let existing_path = std::env::var("PATH").unwrap_or_default();
    let path_value = [
        tc.to_string_lossy().to_string(),
        venv.join("bin").to_string_lossy().to_string(),
        existing_path,
    ]
    .join(":");

    let env: Vec<(std::ffi::OsString, std::ffi::OsString)> = vec![
        ("PATH".into(), path_value.into()),
        (
            "NUTTX_APPS_DIR".into(),
            apps.to_string_lossy().to_string().into(),
        ),
    ];

    let cmake_configure = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "-S".into(),
            nuttx.as_os_str().into(),
            "-B".into(),
            build_dir.as_os_str().into(),
            "-DCMAKE_BUILD_TYPE=Release".into(),
            "-DBOARD_CONFIG=boards/arm/stm32h7/nucleo-h723zg/configs/bench".into(),
            format!("-DBENCH_BUILD_ID={build_id}").into(),
            format!("-DNUTTX_APPS_DIR={}", apps.to_string_lossy()).into(),
            if request.language == Language::Rust {
                "-DBENCH_RUST=ON".into()
            } else {
                "-DBENCH_RUST=OFF".into()
            },
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!(
        "[build] cmake configure (NuttX nucleo-h723zg:bench) in {} (build_id={})",
        build_dir.display(),
        build_id
    );
    let output = process::run_success(&cmake_configure)?;
    eprintln!(
        "[build] cmake configure succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let nproc = std::thread::available_parallelism()
        .map(|n| n.get().to_string())
        .unwrap_or_else(|_| "4".to_string());
    let cmake_build = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "--build".into(),
            build_dir.as_os_str().into(),
            format!("-j{}", nproc).into(),
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!("[build] cmake build (NuttX nucleo-h723zg:bench)");
    let output = process::run_success(&cmake_build)?;
    eprintln!(
        "[build] cmake build succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let elf = build_dir.join("nuttx");
    if !elf.is_file() {
        return Err(Error::Build(format!(
            "nuttx ELF not found: {}",
            elf.display()
        )));
    }
    let bin = build_dir.join("nuttx.bin");
    if !bin.is_file() {
        return Err(Error::Build(format!(
            "nuttx.bin not found: {}",
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

/// Build the nRF52840 DK NuttX firmware via CMake with the bench defconfig.
fn build_nrf52840dk(request: &BuildRequest) -> Result<Artifact> {
    let project = &request.project;
    let nuttx = nuttx_dir(project);
    let apps = nuttx_apps_dir(project);
    let tc = toolchain_bin(project);
    let venv = nuttx_venv(project);
    let build_id = compute_build_id_lang(project, Board::Nrf52840Dk, request.language);

    prepare_build_workspace(project, Board::Nrf52840Dk)?;

    let build_dir = nuttx.join("build-nrf52840dk-bench");
    if request.clean && build_dir.is_dir() {
        eprintln!(
            "[clean] removing stale NuttX nRF52840 build dir {}",
            build_dir.display()
        );
        fs::remove_dir_all(&build_dir)
            .map_err(|e| Error::Build(format!("remove stale build dir: {e}")))?;
    }

    let existing_path = std::env::var("PATH").unwrap_or_default();
    let path_value = [
        tc.to_string_lossy().to_string(),
        venv.join("bin").to_string_lossy().to_string(),
        existing_path,
    ]
    .join(":");

    let env: Vec<(std::ffi::OsString, std::ffi::OsString)> = vec![
        ("PATH".into(), path_value.into()),
        (
            "NUTTX_APPS_DIR".into(),
            apps.to_string_lossy().to_string().into(),
        ),
    ];

    let cmake_configure = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "-S".into(),
            nuttx.as_os_str().into(),
            "-B".into(),
            build_dir.as_os_str().into(),
            "-DCMAKE_BUILD_TYPE=Release".into(),
            "-DBOARD_CONFIG=boards/arm/nrf52/nrf52840-dk/configs/bench".into(),
            format!("-DBENCH_BUILD_ID={build_id}").into(),
            format!("-DNUTTX_APPS_DIR={}", apps.to_string_lossy()).into(),
            if request.language == Language::Rust {
                "-DBENCH_RUST=ON".into()
            } else {
                "-DBENCH_RUST=OFF".into()
            },
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!(
        "[build] cmake configure (NuttX nrf52840-dk:bench) in {} (build_id={})",
        build_dir.display(),
        build_id
    );
    let output = process::run_success(&cmake_configure)?;
    eprintln!(
        "[build] cmake configure succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let nproc = std::thread::available_parallelism()
        .map(|n| n.get().to_string())
        .unwrap_or_else(|_| "4".to_string());
    let cmake_build = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "--build".into(),
            build_dir.as_os_str().into(),
            format!("-j{}", nproc).into(),
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!("[build] cmake build (NuttX nrf52840-dk:bench)");
    let output = process::run_success(&cmake_build)?;
    eprintln!(
        "[build] cmake build succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let elf = build_dir.join("nuttx");
    if !elf.is_file() {
        return Err(Error::Build(format!(
            "nuttx ELF not found: {}",
            elf.display()
        )));
    }

    Ok(Artifact {
        directory: build_dir,
        board: request.board,
        build_id,
        images: vec![elf],
    })
}

/// Build the Pico 2 (RP2350) NuttX firmware via CMake with the bench defconfig.
/// Build the ESP32-C6 NuttX firmware via CMake with the bench defconfig.
fn build_c6(request: &BuildRequest) -> Result<Artifact> {
    let project = &request.project;
    let nuttx = nuttx_dir(project);
    let apps = nuttx_apps_dir(project);
    let venv = nuttx_venv(project);
    let shim = ensure_riscv_shim(project)?;
    let tc = esp_toolchain_bin(project)?;
    let esptool = esptool_bin(project)?;
    let build_id = compute_build_id_lang(project, Board::Esp32c6, request.language);

    prepare_build_workspace(project, Board::Esp32c6)?;

    let build_dir = nuttx.join("build-c6-bench");
    if request.clean && build_dir.is_dir() {
        eprintln!(
            "[clean] removing stale NuttX C6 build dir {}",
            build_dir.display()
        );
        fs::remove_dir_all(&build_dir)
            .map_err(|e| Error::Build(format!("remove stale build dir: {e}")))?;
    }

    let existing_path = std::env::var("PATH").unwrap_or_default();
    let esptool_dir = esptool.parent().map(Path::to_path_buf).unwrap_or_default();
    let path_value = [
        shim.to_string_lossy().to_string(),
        tc.to_string_lossy().to_string(),
        esptool_dir.to_string_lossy().to_string(),
        venv.join("bin").to_string_lossy().to_string(),
        existing_path,
    ]
    .join(":");

    let env: Vec<(std::ffi::OsString, std::ffi::OsString)> = vec![
        ("PATH".into(), path_value.into()),
        (
            "NUTTX_APPS_DIR".into(),
            apps.to_string_lossy().to_string().into(),
        ),
    ];

    let board_config = "boards/risc-v/esp32c6/esp32c6-devkitc/configs/bench";
    let cmake_configure = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "-S".into(),
            nuttx.as_os_str().into(),
            "-B".into(),
            build_dir.as_os_str().into(),
            "-DCMAKE_BUILD_TYPE=Release".into(),
            format!("-DBOARD_CONFIG={board_config}").into(),
            format!("-DNUTTX_APPS_DIR={}", apps.to_string_lossy()).into(),
            format!("-DESPTOOL={}", esptool.to_string_lossy()).into(),
            format!("-DBENCH_BUILD_ID={build_id}").into(),
            if request.language == Language::Rust {
                "-DBENCH_RUST=ON".into()
            } else {
                "-DBENCH_RUST=OFF".into()
            },
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!(
        "[build] cmake configure (NuttX esp32c6-devkitc:bench) in {} (build_id={})",
        build_dir.display(),
        build_id
    );
    let output = process::run_success(&cmake_configure)?;
    eprintln!(
        "[build] cmake configure succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let nproc = std::thread::available_parallelism()
        .map(|n| n.get().to_string())
        .unwrap_or_else(|_| "4".to_string());
    let cmake_build = CommandSpec {
        program: PathBuf::from("cmake"),
        args: vec![
            "--build".into(),
            build_dir.as_os_str().into(),
            format!("-j{nproc}").into(),
        ],
        cwd: nuttx.clone(),
        env: env.clone(),
        timeout: NUTTX_BUILD_TIMEOUT,
    };
    eprintln!("[build] cmake build (NuttX esp32c6-devkitc:bench)");
    let output = process::run_success(&cmake_build)?;
    eprintln!(
        "[build] cmake build succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );

    let elf = build_dir.join("nuttx");
    if !elf.is_file() {
        return Err(Error::Build(format!(
            "nuttx ELF not found: {}",
            elf.display()
        )));
    }
    let bin = build_dir.join("nuttx.bin");
    if !bin.is_file() {
        return Err(Error::Build(format!(
            "nuttx.bin not found: {}",
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

/// Flash the ESP32-C6 NuttX image via esptool as a single image at 0x0.
fn flash_c6(artifact: &Artifact, device: &Device) -> Result<()> {
    let project = project_root(artifact)?;
    let esptool = esptool_bin(&project)?;
    let bin = artifact.directory.join("nuttx.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "nuttx.bin not found: {}",
            bin.display()
        )));
    }
    if device.selector.is_empty() {
        return Err(Error::Invalid("--port required for C6 NuttX flash".into()));
    }
    eprintln!(
        "[flash] esptool write-flash {} -> 0x0 (artifact={}, port={})",
        bin.display(),
        artifact.build_id,
        device.selector
    );
    let spec = CommandSpec {
        program: esptool,
        args: vec![
            "--chip".into(),
            "esp32c6".into(),
            "--port".into(),
            device.selector.clone().into(),
            "--baud".into(),
            "921600".into(),
            "--before".into(),
            "default-reset".into(),
            "--after".into(),
            "hard-reset".into(),
            "write-flash".into(),
            "-u".into(),
            "--flash-mode".into(),
            "dio".into(),
            "--flash-freq".into(),
            "80m".into(),
            "--flash-size".into(),
            "4MB".into(),
            "0x0".into(),
            bin.as_os_str().into(),
        ],
        cwd: project,
        env: vec![],
        timeout: FLASH_TIMEOUT,
    };
    let output = process::run_success(&spec)?;
    eprintln!(
        "[flash] esptool write-flash succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );
    Ok(())
}

/// Flash the NuttX STM32F4 image through the shared probe-rs backend.
/// Flash the STM32H723 (FK723M1) NuttX binary at 0x0800_0000 over the external
/// ST-LINK/V2. The ST-LINK serial is pinned because two 0483:3748 probes are
/// attached (the Discovery uses 52C3BF6D).
fn flash_h723(artifact: &Artifact, _device: &Device) -> Result<()> {
    let bin = artifact.directory.join("nuttx.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "nuttx.bin not found: {}",
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
        /* Pin the ST-Link serial (two 0483:3748 probes are attached); see
         * boards/stm32h723/board.toml. */
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
    let bin = artifact.directory.join("nuttx.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "nuttx.bin not found: {}",
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
        /* Pin the ST-Link serial (two 0483:3748 probes are attached); see
         * boards/stm32f4discovery/board.toml. */
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

/// Flash the nRF52840 DK NuttX ELF over its onboard J-Link and reset.
fn flash_nrf52840dk(artifact: &Artifact, _device: &Device) -> Result<()> {
    let elf = artifact.directory.join("nuttx");
    if !elf.is_file() {
        return Err(Error::Flash(format!(
            "nuttx ELF not found: {}",
            elf.display()
        )));
    }
    eprintln!(
        "[flash] probe-rs download {} (artifact={}, mcu=nRF52840_xxAA, probe=1366:1061:001050202809)",
        elf.display(),
        artifact.build_id
    );

    let executable = if PathBuf::from("/home/chronikle/.cargo/bin/probe-rs").is_file() {
        PathBuf::from("/home/chronikle/.cargo/bin/probe-rs")
    } else {
        PathBuf::from("probe-rs")
    };
    let request = rtos_core::flash::FlashRequest {
        file: elf,
        mcu: "nRF52840_xxAA".into(),
        probe: "1366:1061:001050202809".into(),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_nuttx_plugin_id() {
        let plugin = NuttXPlugin;
        assert_eq!(plugin.id(), "nuttx");
    }

    #[test]
    fn test_route_for_stm32f4discovery() {
        let plugin = NuttXPlugin;
        let route = plugin.route(Board::Stm32f4Discovery, Language::C).unwrap();
        assert_eq!(route.backend, "NuttX");
        assert_eq!(route.target, "stm32f4discovery:bench");
        assert_eq!(route.recipe, "plugins/nuttx/BUILD.md");
    }

    #[test]
    fn test_route_for_esp32c6() {
        let plugin = NuttXPlugin;
        let route = plugin.route(Board::Esp32c6, Language::C).unwrap();
        assert_eq!(route.backend, "NuttX");
        assert_eq!(route.target, "esp32c6-devkitc:nsh");
        assert_eq!(route.recipe, "plugins/nuttx/BUILD.md");
    }

    #[test]
    fn test_validate_rejects_rust() {
        let plugin = NuttXPlugin;
        let request = BuildRequest {
            project: PathBuf::from("/tmp/test"),
            board: Board::Stm32f4Discovery,
            language: Language::Rust,
            workload: "smoke".to_string(),
            output: PathBuf::from("/tmp/output"),
            clean: false,
        };
        assert!(plugin.validate(&request).is_err());
    }

    #[test]
    fn test_compute_build_id_nogit() {
        let id = compute_build_id(Path::new("/tmp/test"), Board::Stm32f4Discovery);
        assert!(
            id.starts_with("nuttx-stm32f4-bench-"),
            "unexpected build_id: {id}"
        );
        let id = compute_build_id(Path::new("/tmp/test"), Board::Esp32c6);
        assert!(
            id.starts_with("nuttx-c6-bench-"),
            "unexpected build_id: {id}"
        );
    }

    #[test]
    fn test_path_helpers() {
        let p = nuttx_dir(Path::new("/proj"));
        assert_eq!(p, PathBuf::from("/proj/.local/source/nuttx"));
        let p = nuttx_apps_dir(Path::new("/proj"));
        assert_eq!(p, PathBuf::from("/proj/.local/source/nuttx-apps"));
        let p = toolchain_bin(Path::new("/proj"));
        assert_eq!(
            p,
            PathBuf::from(
                "/proj/.local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin"
            )
        );
        let p = firmware_dir(Path::new("/proj"));
        assert_eq!(p, PathBuf::from("/proj/firmware/c-kernels/nuttx/c"));
    }

    #[test]
    fn test_riscv_shim_repairs_dangling_and_retargeted_links() {
        let root = std::env::temp_dir().join(format!("nuttx-shim-{}", std::process::id()));
        let old = root.join("old-tool");
        let new = root.join("new-tool");
        let link = root.join("shim/riscv-none-elf-gcc");
        fs::create_dir_all(link.parent().unwrap()).unwrap();
        fs::write(&old, "old").unwrap();
        fs::write(&new, "new").unwrap();

        std::os::unix::fs::symlink(&old, &link).unwrap();
        ensure_symlink(&new, &link).unwrap();
        assert_eq!(fs::read_link(&link).unwrap(), new);
        ensure_symlink(&new, &link).unwrap();

        fs::remove_file(&old).unwrap();
        let dangling = root.join("shim/riscv-none-elf-objcopy");
        std::os::unix::fs::symlink(&old, &dangling).unwrap();
        ensure_symlink(&new, &dangling).unwrap();
        assert_eq!(fs::read_link(&dangling).unwrap(), new);

        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn test_patch_esp_usbserial_buffer_is_idempotent() {
        let root = std::env::temp_dir().join(format!("nuttx-usbpatch-{}", std::process::id()));
        let dir = root.join("arch/risc-v/src/common/espressif");
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join("esp_usbserial.c");
        fs::write(&file, "#define ESP_USBCDC_BUFFERSIZE 64\n").unwrap();

        patch_esp_usbserial_buffer(&root).unwrap();
        let once = fs::read_to_string(&file).unwrap();
        assert!(once.contains("#define ESP_USBCDC_BUFFERSIZE 2048"));

        patch_esp_usbserial_buffer(&root).unwrap();
        assert_eq!(fs::read_to_string(&file).unwrap(), once);

        fs::write(&file, "no buffer macro here\n").unwrap();
        assert!(patch_esp_usbserial_buffer(&root).is_err());

        fs::remove_dir_all(&root).unwrap();
    }
}
