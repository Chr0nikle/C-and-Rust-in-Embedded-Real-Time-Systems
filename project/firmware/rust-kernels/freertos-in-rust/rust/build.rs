use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    let manifest = env::var("CARGO_MANIFEST_DIR").unwrap();
    let sha = Command::new("git")
        .args(["-C", &manifest, "rev-parse", "--short=8", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "nogit".into());
    let h723 = env::var("CARGO_FEATURE_STM32H723").is_ok();
    let build_id = env::var("BENCH_BUILD_ID").unwrap_or_else(|_| {
        if h723 {
            format!("freertos-in-rust-stm32h723-bench-{sha}")
        } else {
            format!("freertos-in-rust-stm32f4-bench-{sha}")
        }
    });
    println!("cargo:rustc-env=BENCH_BUILD_ID={build_id}");
    println!("cargo:rerun-if-env-changed=BENCH_BUILD_ID");

    // Case 20 positive control: disabling the higher-priority pend in both the
    // baseline and nested runs must make the nested-minus-baseline value ~0.
    println!("cargo::rustc-check-cfg=cfg(nested_control)");
    println!("cargo:rerun-if-env-changed=BENCH_NESTED_CONTROL");
    if env::var("BENCH_NESTED_CONTROL").is_ok() {
        println!("cargo:rustc-cfg=nested_control");
    }

    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    let memory: &[u8] = if h723 {
        include_bytes!("memory-h723.x")
    } else {
        include_bytes!("memory.x")
    };
    fs::write(out.join("memory.x"), memory).unwrap();
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=memory.x");
    println!("cargo:rerun-if-changed=memory-h723.x");
}
