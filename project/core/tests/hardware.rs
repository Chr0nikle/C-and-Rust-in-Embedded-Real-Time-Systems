//! Host-only fixtures: no SDK, probe, GDB server or analyzer is contacted.
#![cfg(unix)]
use rtos_core::{
    flash::{self, FlashBackend, FlashRequest, ImageFormat, ProbeRsFlash},
    gdb::{self, GdbRequest},
    logic_analyzer::{self, CaptureRequest, Kingst, LogicAnalyzer},
    process::CommandSpec,
    tooling::ToolRun,
};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Cursor, Write},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "rtos hardware {} {}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
    fn script(&self, name: &str, body: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, body).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn elf(path: &Path, machine: u16, address: u32) {
    // Minimal executable ELF32 containing one four-byte PT_LOAD segment.
    let mut bytes = vec![0u8; 88];
    bytes[..7].copy_from_slice(b"\x7fELF\x01\x01\x01");
    bytes[16..18].copy_from_slice(&2u16.to_le_bytes());
    bytes[18..20].copy_from_slice(&machine.to_le_bytes());
    bytes[20..24].copy_from_slice(&1u32.to_le_bytes());
    bytes[24..28].copy_from_slice(&address.to_le_bytes());
    bytes[28..32].copy_from_slice(&52u32.to_le_bytes());
    bytes[40..42].copy_from_slice(&52u16.to_le_bytes());
    bytes[42..44].copy_from_slice(&32u16.to_le_bytes());
    bytes[44..46].copy_from_slice(&1u16.to_le_bytes());
    for (offset, value) in [
        (52, 1u32),
        (56, 84),
        (60, address),
        (64, address),
        (68, 4),
        (72, 4),
        (76, 5),
        (80, 4),
    ] {
        bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
    }
    fs::write(path, bytes).unwrap();
}

fn request(file: PathBuf) -> FlashRequest {
    FlashRequest {
        file,
        mcu: "STM32F407VGTx".into(),
        probe: "0483:3748:EXPLICIT".into(),
        format: ImageFormat::Elf,
        address: None,
        bootloader: None,
        partition_table: None,
        reset: false,
        timeout_seconds: 2,
    }
}

#[test]
fn flash_checks_cpu_load_ranges_and_esp_layout_before_execution() {
    let tmp = Temp::new();
    let path = tmp.0.join("image with spaces.elf");
    let backend = ProbeRsFlash {
        executable: "must-not-run".into(),
    };
    let mut req = request(path.clone());
    elf(&path, 40, 0x0800_0000);
    let args = backend.plan(&req).unwrap().args;
    assert!(args
        .windows(2)
        .any(|a| a == ["--probe", "0483:3748:EXPLICIT"]));
    assert!(args.iter().any(|a| a == "--verify"));
    assert!(!args.iter().any(|a| a == "--reset"));
    elf(&path, 243, 0x0800_0000);
    assert!(backend.plan(&req).is_err());
    elf(&path, 40, 0x2000_0000);
    assert!(backend.plan(&req).is_err());
    req.format = ImageFormat::Bin;
    assert!(backend.plan(&req).is_err());
    req.address = Some(0x0800_0000);
    assert!(backend.plan(&req).is_ok());
    req.address = Some(u64::MAX);
    assert!(backend.plan(&req).is_err());
    req.mcu = "esp32c6".into();
    req.address = None;
    req.format = ImageFormat::EspIdf;
    elf(&path, 243, 0x4200_0000);
    assert!(backend.plan(&req).is_err());
    req.bootloader = Some(path.clone());
    req.partition_table = Some(path);
    assert!(backend.plan(&req).is_ok());
    flash::validate_probe("303a:1001:40:4C:CA:40:8E:D4").unwrap();
}

#[test]
fn flash_uses_preserved_snapshot_and_reports_tool_failure() {
    let tmp = Temp::new();
    let path = tmp.0.join("firmware.elf");
    elf(&path, 40, 0x0800_0000);
    let tool = tmp.script(
        "flasher",
        "#!/usr/bin/python3\nimport sys\nprint(repr(sys.argv))\n",
    );
    let backend = ProbeRsFlash { executable: tool };
    let run = flash::flash(&backend, &request(path.clone()), &tmp.0.join("runs")).unwrap();
    assert_eq!(
        fs::read(&path).unwrap(),
        fs::read(run.join("raw/image")).unwrap()
    );
    let manifest: Value =
        serde_json::from_slice(&fs::read(run.join("manifest.json")).unwrap()).unwrap();
    assert_eq!(manifest["status"], "complete");
    assert_eq!(manifest["image"]["sha256"].as_str().unwrap().len(), 64);
    assert!(manifest["command"]["args"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .as_str()
        .unwrap()
        .ends_with("raw/image"));
    let failing = ProbeRsFlash { executable: tmp.script("failure", "#!/usr/bin/python3\nimport sys\nprint('probe unavailable', file=sys.stderr)\nsys.exit(7)\n") };
    let error = flash::flash(&failing, &request(path), &tmp.0.join("runs")).unwrap_err();
    assert!(error.to_string().contains("probe unavailable"));
    assert!(error.to_string().contains("evidence:"));
}

fn capture_request() -> CaptureRequest {
    CaptureRequest {
        connection: "6.25".into(),
        channels: vec![0, 1],
        samplerate: 10_000_000,
        samples: 100,
        threshold: "1.4".into(),
        trigger: None,
        timeout_seconds: 2,
    }
}

fn archive(path: &Path, samples: usize) {
    let mut zip = zip::ZipWriter::new(fs::File::create(path).unwrap());
    let options = zip::write::SimpleFileOptions::default();
    for (name, bytes) in [
        ("version", b"2".to_vec()),
        (
            "metadata",
            b"[device 1]\nunitsize = 1\nsamplerate = 10 MHz\nprobe1=CH0\nprobe2=CH1\n".to_vec(),
        ),
        ("logic-1-1", vec![0xa5; samples]),
    ] {
        zip.start_file(name, options).unwrap();
        zip.write_all(&bytes).unwrap();
    }
    zip.finish().unwrap();
}

#[test]
fn capture_validates_channels_trigger_timeout_and_actual_samples() {
    let tmp = Temp::new();
    archive(&tmp.0.join("fixture.sr"), 100);
    let tool = tmp.script("capture", "#!/usr/bin/python3\nimport shutil, sys\nfrom pathlib import Path\nshutil.copyfile(Path(__file__).with_name('fixture.sr'), sys.argv[sys.argv.index('--output-file')+1])\n");
    let backend = Kingst { executable: tool };
    let mut request = capture_request();
    request.channels.push(16);
    assert!(backend.plan(&request, &tmp.0.join("out.sr")).is_err());
    request.channels.pop();
    request.trigger = Some("CH2=r".into());
    assert!(backend.plan(&request, &tmp.0.join("out.sr")).is_err());
    request.trigger = Some("CH0=r".into());
    let plan = backend.plan(&request, &tmp.0.join("out.sr")).unwrap();
    assert!(plan.args.iter().any(|a| a == "--wait-trigger"));
    assert!(plan
        .args
        .iter()
        .any(|a| a == "samplerate=10000000:voltage_threshold=1.4-1.4"));
    request.timeout_seconds = 0;
    assert!(backend.plan(&request, &tmp.0.join("out.sr")).is_err());
    request.timeout_seconds = 2;
    let run = logic_analyzer::capture(&backend, &request, &tmp.0.join("runs")).unwrap();
    assert_eq!(
        logic_analyzer::validate_capture(&run.directory.join("raw/capture.sr")).unwrap(),
        100
    );
    request.samples = 99;
    let overshoot = logic_analyzer::capture(&backend, &request, &tmp.0.join("runs")).unwrap();
    assert_eq!(overshoot.captured_samples, 100);
    request.samples = 101;
    assert!(
        logic_analyzer::capture(&backend, &request, &tmp.0.join("runs"))
            .unwrap_err()
            .to_string()
            .contains("incomplete capture")
    );
    fs::write(tmp.0.join("fixture.sr"), "truncated").unwrap();
    assert!(logic_analyzer::capture(&backend, &request, &tmp.0.join("runs")).is_err());
}

#[test]
fn tool_output_is_file_backed_and_timeout_kills_child() {
    let tmp = Temp::new();
    let tool = tmp.script("noisy", "#!/usr/bin/python3\nimport sys\nsys.stdout.write('x'*2000000)\nsys.stderr.write('diagnostic'*100000)\nsys.exit(3)\n");
    let spec = CommandSpec {
        program: tool,
        args: vec![],
        cwd: tmp.0.clone(),
        env: vec![],
        timeout: Duration::from_secs(3),
    };
    let mut run = ToolRun::create(&tmp.0.join("runs"), "test", json!({"synthetic": true})).unwrap();
    let error = run.execute(&spec).unwrap_err().to_string();
    assert!(error.len() < 1000);
    assert_eq!(
        fs::metadata(run.directory.join("logs/stdout.log"))
            .unwrap()
            .len(),
        2_000_000
    );
    let sleeper = tmp.script("sleeper", "#!/usr/bin/python3\nimport os, time\nfrom pathlib import Path\nPath(__file__).with_suffix('.pid').write_text(str(os.getpid()))\nprint('partial', flush=True)\ntime.sleep(30)\n");
    let spec = CommandSpec {
        program: sleeper,
        args: vec![],
        cwd: tmp.0.clone(),
        env: vec![],
        timeout: Duration::from_millis(100),
    };
    let mut run = ToolRun::create(&tmp.0.join("runs"), "test", json!({})).unwrap();
    let start = Instant::now();
    assert!(run
        .execute(&spec)
        .unwrap_err()
        .to_string()
        .contains("timeout"));
    assert!(start.elapsed() < Duration::from_secs(2));
    let pid = fs::read_to_string(tmp.0.join("sleeper.pid")).unwrap();
    assert!(!Path::new("/proc").join(pid).exists());
    assert!(fs::read_to_string(run.directory.join("logs/stdout.log"))
        .unwrap()
        .contains("partial"));
}

fn gdb_request(tmp: &Temp) -> GdbRequest {
    let executable = tmp.script("fake-gdb", include_str!("fixtures/gdb_fake.py"));
    let path = tmp.0.join("symbols with spaces.elf");
    elf(&path, 40, 0x0800_0000);
    GdbRequest {
        executable,
        elf: path,
        mcu: "STM32F407VGTx".into(),
        endpoint: "127.0.0.1:1337".into(),
        timeout: Duration::from_millis(200),
    }
}

#[test]
fn gdb_keeps_one_connection_and_waits_for_step_stop_events() {
    let tmp = Temp::new();
    let req = gdb_request(&tmp);
    let mut output = Vec::new();
    let mut actions =
        Cursor::new(b"break main\nstep\nbacktrace\ncontinue\ninterrupt\nread 0x20000000 8\nquit\n");
    gdb::run_session(&req, &tmp.0.join("runs"), &mut actions, &mut output).unwrap();
    let replies: Vec<Value> = String::from_utf8(output)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let step = replies
        .iter()
        .find(|r| r["data"]["action"] == "step")
        .unwrap();
    assert_eq!(step["data"]["state"], "stopped");
    assert_eq!(step["data"]["last_stop"]["frame"]["line"], "42");
    let run = fs::read_dir(tmp.0.join("runs"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let commands = fs::read_to_string(run.join("logs/commands.log")).unwrap();
    assert_eq!(commands.matches("-target-select").count(), 1);
    assert_eq!(commands.matches("-exec-continue").count(), 1);
    assert!(!commands.contains("target-download"));
    let pid = fs::read_to_string(tmp.0.join("fake-gdb.pid")).unwrap();
    assert!(!Path::new("/proc").join(pid).exists());
}

#[test]
fn gdb_error_is_not_success_and_missing_stop_times_out() {
    let tmp = Temp::new();
    let req = gdb_request(&tmp);
    let mut output = Vec::new();
    assert!(gdb::run_session(
        &req,
        &tmp.0.join("runs"),
        &mut Cursor::new(b"print missing\nbacktrace\nquit\n"),
        &mut output
    )
    .is_err());
    let text = String::from_utf8(output).unwrap();
    assert!(text.contains("No symbol"));
    assert!(text.contains("backtrace"));
    let started = Instant::now();
    let error = gdb::run_session(
        &req,
        &tmp.0.join("runs"),
        &mut Cursor::new(b"next\n"),
        &mut Vec::new(),
    )
    .unwrap_err();
    assert!(error.to_string().contains("timed out"));
    assert!(started.elapsed() < Duration::from_secs(2));
}
