//! Verified ESP32-C6 native build and single-image esptool route.
use rtos_core::{
    error::{Error, Result},
    plugin_interface::{Artifact, Board, BuildRequest, Device, Language},
    process::{self, CommandSpec},
    results,
    tooling::{snapshot, ToolRun},
};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

const TARGET: &str = "esp32c6_devkitc/esp32c6/hpcore";
const TOOLS: &[(&str, &str)] = &[
    ("C_COMPILER", "gcc"),
    ("CXX_COMPILER", "g++"),
    ("OBJCOPY", "objcopy"),
    ("READELF", "readelf"),
    ("OBJDUMP", "objdump"),
    ("NM", "nm"),
    ("AR", "ar"),
    ("RANLIB", "ranlib"),
    ("SIZE", "size"),
    ("STRIP", "strip"),
];

fn prefix(project: &Path) -> PathBuf {
    project.join(".local/toolchains/esp-idf/tools/riscv32-esp-elf/esp-14.2.0_20260121/riscv32-esp-elf/bin/riscv32-esp-elf-")
}

fn executable(prefix: &Path, suffix: &str) -> PathBuf {
    let mut name = prefix.as_os_str().to_os_string();
    name.push(suffix);
    name.into()
}

pub(super) fn validate(project: &Path) -> Result<()> {
    for path in [
        ".local/west/west",
        ".local/west/zephyr/CMakeLists.txt",
        ".local/venv/zephyr/bin/esptool",
        "firmware/c-kernels/zephyr/c/esp32c6/CMakeLists.txt",
        "firmware/c-kernels/zephyr/c/esp32c6/prj.conf",
    ] {
        if !project.join(path).is_file() {
            return Err(Error::Build(format!(
                "C6 Zephyr prerequisite missing: {}",
                project.join(path).display()
            )));
        }
    }
    for (_, suffix) in TOOLS {
        let path = executable(&prefix(project), suffix);
        if !path.is_file() {
            return Err(Error::Build(format!("C6 tool missing: {}", path.display())));
        }
    }
    Ok(())
}

fn build_spec(project: &Path, language: Language) -> Result<CommandSpec> {
    let ws = project.join(".local/west");
    let prefix = prefix(project);
    let mut paths = vec![project.join(".local/venv/zephyr/bin")];
    paths.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    let path = std::env::join_paths(paths).map_err(|e| Error::Build(e.to_string()))?;
    let mut args = vec![
        "build".into(),
        "-p".into(),
        "always".into(),
        "-b".into(),
        TARGET.into(),
        "-d".into(),
        ws.join("build-zephyr-c6-bench").into_os_string(),
        project
            .join("firmware/c-kernels/zephyr/c/esp32c6")
            .into_os_string(),
        "--".into(),
    ];
    for (key, suffix) in TOOLS {
        args.push(format!("-DCMAKE_{key}={}", executable(&prefix, suffix).display()).into());
    }
    if language == Language::Rust {
        args.push("-DBENCH_RUST=ON".into());
    }
    Ok(CommandSpec {
        program: ws.join("west"),
        args,
        cwd: ws.clone(),
        env: vec![
            ("PATH".into(), path),
            ("ZEPHYR_BASE".into(), ws.join("zephyr").into_os_string()),
            ("ZEPHYR_TOOLCHAIN_VARIANT".into(), "cross-compile".into()),
            ("CROSS_COMPILE".into(), prefix.into_os_string()),
        ],
        timeout: Duration::from_secs(600),
    })
}

pub(super) fn build(request: &BuildRequest) -> Result<Artifact> {
    let project = request.project.canonicalize()?;
    validate(&project)?;
    let mut run = ToolRun::create(
        &results::campaigns_root(&project),
        "build-zephyr-c6",
        Default::default(),
    )?;
    let result = (|| {
        run.execute(&build_spec(&project, request.language)?)?;
        let directory = project.join(".local/west/build-zephyr-c6-bench/zephyr");
        for name in ["zephyr.bin", "zephyr.elf", "runners.yaml", ".config"] {
            let identity = snapshot(&directory.join(name), &run.directory.join(name))?;
            run.record(name, identity)?;
        }
        let build_id = super::compute_c6_build_id(&project, request.language);
        run.record("build_id", build_id.clone().into())?;
        Ok(Artifact {
            images: vec![directory.join("zephyr.elf")],
            directory,
            board: Board::Esp32c6,
            build_id,
        })
    })();
    match &result {
        Ok(_) => run.finish("complete", "native build complete; hardware not checked")?,
        Err(e) => run.finish("failed", &format!("{e}"))?,
    }
    eprintln!("[build] evidence: {}", run.directory.display());
    result
}

/// Locate the project root by walking up from the build directory to `.local/west`.
fn project_root(artifact: &Artifact) -> Result<PathBuf> {
    artifact
        .directory
        .ancestors()
        .find(|dir| dir.join(".local/west").is_dir())
        .map(Path::to_path_buf)
        .ok_or_else(|| {
            Error::Flash(format!(
                "project root not found above {}",
                artifact.directory.display()
            ))
        })
}

/// The verified single-image esptool invocation: dio/80 MHz/8 MB at 0x0.
fn flash_spec(esptool: PathBuf, project: &Path, port: &str, bin: &Path) -> CommandSpec {
    CommandSpec {
        program: esptool,
        args: vec![
            "--port".into(),
            port.into(),
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
            "8MB".into(),
            "0x0".into(),
            bin.as_os_str().into(),
        ],
        cwd: project.to_path_buf(),
        env: vec![],
        timeout: Duration::from_secs(120),
    }
}

pub(super) fn flash(artifact: &Artifact, device: &Device) -> Result<()> {
    if device.board != Board::Esp32c6 {
        return Err(Error::Flash(
            "C6 Zephyr flash requires an esp32c6 device".into(),
        ));
    }
    if device.selector.is_empty() {
        return Err(Error::Invalid("--port required for C6 Zephyr flash".into()));
    }
    let bin = artifact.directory.join("zephyr.bin");
    if !bin.is_file() {
        return Err(Error::Flash(format!(
            "zephyr.bin not found: {}",
            bin.display()
        )));
    }
    let project = project_root(artifact)?;
    let esptool = project.join(".local/venv/zephyr/bin/esptool");
    if !esptool.is_file() {
        return Err(Error::Flash(format!(
            "esptool not found at {}; run scripts/setup/provision-zephyr.sh",
            esptool.display()
        )));
    }
    eprintln!(
        "[flash] esptool write-flash {} -> 0x0 (artifact={}, port={})",
        bin.display(),
        artifact.build_id,
        device.selector
    );
    let output = process::run_success(&flash_spec(esptool, &project, &device.selector, &bin))?;
    eprintln!(
        "[flash] esptool write-flash succeeded ({} bytes output, {} bytes stderr)",
        output.stdout.len(),
        output.stderr.len()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rtos_core::plugin_interface::Board;

    fn artifact(directory: &str) -> Artifact {
        Artifact {
            directory: PathBuf::from(directory),
            board: Board::Esp32c6,
            build_id: "zephyr-c6-bench-test".into(),
            images: Vec::new(),
        }
    }

    #[test]
    fn flash_spec_uses_verified_single_image_route() {
        let spec = flash_spec(
            PathBuf::from("/p/esptool"),
            Path::new("/proj"),
            "/dev/ttyACM0",
            Path::new("/proj/build/zephyr.bin"),
        );
        assert_eq!(spec.program, PathBuf::from("/p/esptool"));
        assert_eq!(spec.cwd, PathBuf::from("/proj"));
        let args: Vec<String> = spec
            .args
            .iter()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            args,
            vec![
                "--port",
                "/dev/ttyACM0",
                "--baud",
                "921600",
                "--before",
                "default-reset",
                "--after",
                "hard-reset",
                "write-flash",
                "-u",
                "--flash-mode",
                "dio",
                "--flash-freq",
                "80m",
                "--flash-size",
                "8MB",
                "0x0",
                "/proj/build/zephyr.bin",
            ]
        );
    }

    #[test]
    fn project_root_walks_up_to_local_west() {
        let art = artifact("/proj/.local/west/build-zephyr-c6-bench/zephyr");
        // No real directory on disk, so discovery fails rather than guessing.
        assert!(project_root(&art).is_err());
    }

    #[test]
    fn flash_rejects_missing_binary() {
        let art = artifact("/nonexistent/zephyr");
        let device = Device {
            selector: "/dev/ttyACM0".into(),
            board: Board::Esp32c6,
        };
        assert!(flash(&art, &device).is_err());
    }
}
