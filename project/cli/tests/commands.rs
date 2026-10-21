//! Test real executable boundaries; no SDK or board is used.
use std::process::Command;

fn invoke(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_rtos"))
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn plans_identify_backend_and_unsupported_board() {
    let plan = invoke(&["benchmark", "freertos", "--board", "esp32c6", "--plan"]);
    assert!(plan.status.success());
    let text = String::from_utf8(plan.stdout).unwrap();
    assert!(text.contains("ESP-IDF FreeRTOS port"));
    assert!(text.contains("start -> collect -> complete"));
    assert!(text.contains("not executable"));
    // The separate esp-idf route is cancelled; the C6 path is the freertos
    // route over the ESP-IDF kernel, so the subcommand must no longer exist.
    let removed = invoke(&["esp-idf", "--board", "stm32f4discovery", "--plan"]);
    assert!(!removed.status.success());
    assert!(String::from_utf8(removed.stderr)
        .unwrap()
        .contains("unrecognized subcommand"));
}
#[test]
fn hardware_commands_never_fake_success() {
    // `list` performs real discovery (sysfs + board manifests) and reports
    // whatever is present, so it succeeds even with no devices attached.
    let list = invoke(&["list"]);
    assert!(list.status.success());
    assert!(String::from_utf8(list.stdout)
        .unwrap()
        .contains("Detected devices"));
    for args in [vec!["freertos"], vec!["benchmark", "zephyr"]] {
        let output = invoke(&args);
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        // Without --port the CLI auto-selects the sole matching device and
        // refuses (rather than touching hardware) when none is connected.
        assert!(String::from_utf8(output.stderr)
            .unwrap()
            .contains("no connected"));
    }
    // Monitor is implemented; without a port or board it still refuses to
    // touch hardware and reports the missing requirement.
    let output = invoke(&["monitor"]);
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("requires --port"));
}
#[test]
fn plan_requires_explicit_board_and_build_does_not_plan_flash() {
    assert!(!invoke(&["zephyr", "--plan"]).status.success());
    let output = invoke(&["build", "nuttx", "--board", "stm32f4discovery", "--plan"]);
    assert!(output.status.success());
    assert!(!String::from_utf8(output.stdout)
        .unwrap()
        .contains("-> flash"));
}

#[test]
fn check_reports_route_tools_without_touching_hardware() {
    let output = invoke(&["check", "zephyr", "--board", "esp32c6"]);
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("git"), "{text}");
    assert!(text.contains("west"), "{text}");
    assert!(text.contains("esptool"), "{text}");
}

#[test]
fn benchmark_rejects_zero_repetitions() {
    let output = invoke(&["benchmark", "zephyr", "--repetitions", "0"]);
    assert!(!output.status.success());
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("nonzero"));
}

#[test]
fn safety_analysis_reports_scope_and_persists_evidence() {
    let dir = std::env::temp_dir().join(format!("rtos-safety-cli-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let output = invoke(&["safety", "--out", dir.to_str().unwrap()]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("schema_version=4"), "{text}");
    assert!(text.contains("findings_ran="), "{text}");
    assert!(text.contains("scope unsafe_block="), "{text}");
    assert!(text.contains("clippy_ran="), "{text}");
    assert!(text.contains("decision_points="), "{text}");
    let report = std::fs::read_dir(&dir)
        .unwrap()
        .flatten()
        .map(|entry| entry.path().join("safety.json"))
        .find(|path| path.is_file());
    assert!(report.is_some(), "no safety.json under {}", dir.display());
    std::fs::remove_dir_all(&dir).ok();
}
