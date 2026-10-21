//! RTOS-independent file programming. SDKs still own image construction.
use crate::{
    error::{Error, Result},
    process::CommandSpec,
    tooling::{self, ToolRun},
};
use object::{Object, ObjectSegment};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

/// Explicit image interpretation; never infer ESP boot layout from an ELF suffix.
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ImageFormat {
    Elf,
    Bin,
    EspIdf,
}

/// Fully selected programming operation, independent of kernel and language.
#[derive(Clone, Debug, Serialize)]
pub struct FlashRequest {
    pub file: PathBuf,
    pub mcu: String,
    pub probe: String,
    pub format: ImageFormat,
    pub address: Option<u64>,
    pub bootloader: Option<PathBuf>,
    pub partition_table: Option<PathBuf>,
    pub reset: bool,
    pub timeout_seconds: u64,
}

/// Backend extension point. Planning validates inputs without touching hardware.
pub trait FlashBackend {
    fn plan(&self, request: &FlashRequest) -> Result<CommandSpec>;
}

/// Explicit probe-rs adapter, initially validated on the two study MCU families.
pub struct ProbeRsFlash {
    pub executable: PathBuf,
}

/// Canonical chip and wire protocol for the currently implemented targets.
pub fn target(mcu: &str) -> Result<(&'static str, &'static str)> {
    match mcu.to_ascii_lowercase().as_str() {
        "stm32f407vgtx" | "stm32f407vg" => Ok(("STM32F407VGTx", "swd")),
        "stm32h723zgtx" | "stm32h723zg" | "stm32h723" => Ok(("STM32H723ZG", "swd")),
        "nrf52840_xxaa" | "nrf52840" => Ok(("nRF52840_xxAA", "swd")),
        "esp32c6" | "esp32-c6" => Ok(("esp32c6", "jtag")),
        _ => Err(Error::Unsupported(
            "MCU adapter unavailable; supported: STM32F407VGTx, STM32H723ZG, nRF52840_xxAA, esp32c6",
        )),
    }
}

/// Reject incomplete probe selectors rather than guessing a device.
pub fn validate_probe(probe: &str) -> Result<()> {
    let fields: Vec<_> = probe.splitn(3, ':').collect();
    if !(2..=3).contains(&fields.len())
        || fields[..2]
            .iter()
            .any(|s| s.len() != 4 || !s.chars().all(|c| c.is_ascii_hexdigit()))
        || fields
            .get(2)
            .is_some_and(|s| s.is_empty() || s.chars().any(char::is_whitespace))
    {
        return Err(Error::Invalid(
            "probe must be VID:PID[:serial]; use a serial when multiple probes match".into(),
        ));
    }
    Ok(())
}

/// Validate executable class, CPU and (for STM32) physical flash segments.
pub fn validate_elf(path: &Path, chip: &str) -> Result<()> {
    use object::read::elf::{ElfFile32, ProgramHeader};
    let bytes = std::fs::read(input_file(path)?)?;
    let elf = ElfFile32::<object::Endianness>::parse(bytes.as_slice())
        .map_err(|e| Error::Invalid(format!("invalid ELF32: {e}")))?;
    let expected = if chip.starts_with("STM32") || chip == "nRF52840_xxAA" {
        object::Architecture::Arm
    } else {
        object::Architecture::Riscv32
    };
    if elf.architecture() != expected
        || !elf.is_little_endian()
        || elf.kind() != object::ObjectKind::Executable
    {
        return Err(Error::Invalid(
            "ELF architecture/class/type does not match selected MCU".into(),
        ));
    }
    let mut loadable = false;
    for header in elf.elf_program_headers() {
        if header.p_type(elf.endian()) != object::elf::PT_LOAD || header.p_filesz(elf.endian()) == 0
        {
            continue;
        }
        loadable = true;
        let address = u64::from(header.p_paddr(elf.endian()));
        let size = u64::from(header.p_filesz(elf.endian()));
        let offset = u64::from(header.p_offset(elf.endian()));
        if offset + size > bytes.len() as u64 || size > u64::from(header.p_memsz(elf.endian())) {
            return Err(Error::Invalid(
                "ELF contains a truncated/invalid load segment".into(),
            ));
        }
        if chip == "STM32F407VGTx" && (address < 0x0800_0000 || address + size > 0x0810_0000) {
            return Err(Error::Invalid(
                "ELF load segment falls outside STM32F407VG flash".into(),
            ));
        }
        if chip == "STM32H723ZG" && (address < 0x0800_0000 || address + size > 0x0810_0000) {
            return Err(Error::Invalid(
                "ELF load segment falls outside STM32H723ZG flash".into(),
            ));
        }
        if chip == "nRF52840_xxAA" && address + size > 0x0010_0000 {
            return Err(Error::Invalid(
                "ELF load segment falls outside nRF52840 1 MiB flash".into(),
            ));
        }
    }
    if !loadable
        || elf
            .segments()
            .all(|s| s.data().map_or(true, |d| d.is_empty()))
    {
        return Err(Error::Invalid("ELF contains no loadable image".into()));
    }
    Ok(())
}

/// Program an immutable input snapshot and preserve a truthful programming result.
/// This does not build firmware or claim a matching UART startup record.
pub fn flash(backend: &dyn FlashBackend, req: &FlashRequest, results: &Path) -> Result<PathBuf> {
    backend.plan(req)?;
    let mut run = ToolRun::create(results, "flash", json!(req))?;
    let mut snapshot = req.clone();
    let outcome = (|| {
        for (name, source) in [
            ("image", Some(&req.file)),
            ("bootloader", req.bootloader.as_ref()),
            ("partition-table", req.partition_table.as_ref()),
        ] {
            if let Some(source) = source {
                let destination = run.directory.join("raw").join(name);
                run.record(name, tooling::snapshot(source, &destination)?)?;
                match name {
                    "image" => snapshot.file = destination,
                    "bootloader" => snapshot.bootloader = Some(destination),
                    _ => snapshot.partition_table = Some(destination),
                }
            }
        }
        run.execute(&backend.plan(&snapshot)?)?;
        run.finish(
            "complete",
            "programmed and verified by backend; application readiness not checked",
        )
    })();
    if let Err(error) = outcome {
        run.finish(
            if matches!(error, Error::Interrupted(_)) {
                "interrupted"
            } else {
                "failed"
            },
            &error.to_string(),
        )?;
        return Err(Error::Flash(format!(
            "{error}; evidence: {}",
            run.directory.display()
        )));
    }
    Ok(run.directory)
}

/// Require an existing nonempty bounded input before any device access.
pub fn input_file(path: &Path) -> Result<PathBuf> {
    let path = path
        .canonicalize()
        .map_err(|e| Error::Invalid(format!("{}: {e}", path.display())))?;
    let metadata = path.metadata()?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 128 * 1024 * 1024 {
        return Err(Error::Invalid(
            "input must be a nonempty regular file <=128 MiB".into(),
        ));
    }
    Ok(path)
}

impl FlashBackend for ProbeRsFlash {
    fn plan(&self, req: &FlashRequest) -> Result<CommandSpec> {
        let (chip, protocol) = target(&req.mcu)?;
        validate_probe(&req.probe)?;
        if !(1..=86400).contains(&req.timeout_seconds) {
            return Err(Error::Invalid("timeout must be 1..86400 seconds".into()));
        }
        let file = input_file(&req.file)?;
        let stm32 = chip.starts_with("STM32");
        let arm = stm32 || chip == "nRF52840_xxAA";
        let mut args = vec![
            "download".into(),
            "--chip".into(),
            chip.into(),
            "--probe".into(),
            req.probe.clone().into(),
            "--protocol".into(),
            protocol.into(),
            "--non-interactive".into(),
            "--verify".into(),
            "--disable-progressbars".into(),
        ];
        if chip == "STM32F407VGTx" {
            // The running firmware can hold the SWD pins, so a normal attach
            // fails with JtagNoDeviceConnected on this probe; halting the core
            // under reset first is required (st-flash does the same).
            args.push("--connect-under-reset".into());
        }
        match req.format {
            ImageFormat::Bin if arm => {
                let address = req.address.ok_or_else(|| Error::Invalid("raw binary requires --address".into()))?;
                let end = address.checked_add(file.metadata()?.len());
                let (lo, hi, what) = if stm32 {
                    (0x0800_0000u64, 0x0810_0000u64, "STM32 1 MiB flash")
                } else {
                    (0x0000_0000u64, 0x0010_0000u64, "nRF52840 1 MiB flash")
                };
                if address < lo || end.is_none_or(|n| n > hi) {
                    return Err(Error::Invalid(format!("binary falls outside {what}")));
                }
                args.extend(["--binary-format".into(), "bin".into(), "--base-address".into(), format!("0x{address:x}").into()]);
            }
            ImageFormat::Elf if arm => {
                validate_elf(&file, chip)?;
                args.extend(["--binary-format".into(), "elf".into()]);
            }
            ImageFormat::EspIdf if !arm => {
                validate_elf(&file, chip)?;
                let boot = req.bootloader.as_ref().ok_or_else(|| Error::Invalid("ESP-IDF requires --bootloader".into()))?;
                let table = req.partition_table.as_ref().ok_or_else(|| Error::Invalid("ESP-IDF requires --partition-table".into()))?;
                args.extend(["--binary-format".into(), "idf".into(), "--idf-bootloader".into(), input_file(boot)?.into_os_string(),
                    "--idf-partition-table".into(), input_file(table)?.into_os_string()]);
            }
            _ => return Err(Error::Unsupported("image/MCU mismatch: STM32 accepts elf/bin; ESP32-C6 requires esp-idf ELF plus bootloader and partition table")),
        }
        if !matches!(req.format, ImageFormat::Bin) && req.address.is_some() {
            return Err(Error::Invalid("--address is only for raw bin".into()));
        }
        if !matches!(req.format, ImageFormat::EspIdf)
            && (req.bootloader.is_some() || req.partition_table.is_some())
        {
            return Err(Error::Invalid(
                "bootloader/partition table require esp-idf format".into(),
            ));
        }
        if req.reset {
            args.push("--reset".into());
        }
        args.push(file.into_os_string());
        Ok(CommandSpec {
            program: self.executable.clone(),
            args,
            cwd: std::env::current_dir()?,
            env: vec![],
            timeout: Duration::from_secs(req.timeout_seconds),
        })
    }
}
