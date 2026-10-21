//! Public command boundaries, using plans and invalid input only: no hardware.
use std::process::Command;

fn invoke(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_rtos"))
        .args(args)
        .output()
        .unwrap()
}

#[test]
fn hardware_help_and_plans_work_without_tools() {
    for command in ["flash", "gdb", "gdb-server", "capture"] {
        let output = invoke(&[command, "--help"]);
        assert!(output.status.success());
    }
    let output = invoke(&[
        "capture",
        "--connection",
        "6.25",
        "--channels",
        "CH0,CH15",
        "--samplerate",
        "10MHz",
        "--samples",
        "100000000",
        "--plan",
        "--sigrok",
        "/missing/tool",
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let plan: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(plan["program"], "/missing/tool");
    assert!(plan["args"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v == "CH0,CH15"));
    let output = invoke(&[
        "gdb-server",
        "--mcu",
        "esp32c6",
        "--probe",
        "303a:1001:40:4C:CA:40:8E:D4",
        "--plan",
    ]);
    assert!(output.status.success());
}

#[test]
fn invalid_hardware_inputs_and_legacy_debug_plan_never_execute() {
    for args in [
        vec![
            "capture",
            "--connection",
            "6.25",
            "--channels",
            "PWM1",
            "--plan",
        ],
        vec![
            "capture",
            "--connection",
            "6.25",
            "--samplerate",
            "3MHz",
            "--plan",
        ],
        vec!["capture", "--connection", "auto", "--plan"],
        vec![
            "gdb-server",
            "--mcu",
            "unknown",
            "--probe",
            "0483:3748",
            "--plan",
        ],
        vec![
            "gdb-server",
            "--mcu",
            "esp32c6",
            "--probe",
            "303a:1001",
            "--endpoint",
            "0.0.0.0:1337",
            "--plan",
        ],
        vec!["debug", "--board", "stm32f4discovery", "--plan"],
        vec![
            "flash",
            "--file",
            "/missing/image",
            "--mcu",
            "STM32F407VGTx",
            "--probe",
            "0483:3748",
            "--plan",
        ],
    ] {
        let output = invoke(&args);
        assert!(!output.status.success(), "{args:?}");
        assert!(output.stdout.is_empty(), "{args:?}");
    }
}
