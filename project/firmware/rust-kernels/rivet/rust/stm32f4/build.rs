use std::path::PathBuf;

fn main() {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let script = manifest_dir.join("link-stm32f407.ld");
    // `-T <script>` via the standard link-arg directive (the `cargo:linker-script`
    // spelling some Rivet BSPs use is not a recognised build-script key and was
    // silently dropping the script, producing a default-layout ELF).
    println!("cargo:rustc-link-arg=-T{}", script.display());
    println!("cargo:rerun-if-changed={}", script.display());
    if let Ok(id) = std::env::var("BENCH_BUILD_ID") {
        println!("cargo:rustc-env=BENCH_BUILD_ID={id}");
    }
    println!("cargo:rerun-if-env-changed=BENCH_BUILD_ID");
    // Case 26 deliberate-inversion control: when set, the priority-ordered
    // tasks sleep before appending so the recorded order is ascending and
    // `prio_measure` must report 0.
    println!("cargo::rustc-check-cfg=cfg(prio_invert)");
    println!("cargo::rustc-check-cfg=cfg(maxdis_control)");
    println!("cargo::rustc-check-cfg=cfg(pjit_load_off)");
    println!("cargo:rerun-if-env-changed=RIVET_PJIT_LOAD_OFF");
    if std::env::var("RIVET_PJIT_LOAD_OFF").is_ok() {
        println!("cargo:rustc-cfg=pjit_load_off");
    }
    println!("cargo:rerun-if-env-changed=RIVET_PRIO_INVERT");
    if std::env::var("RIVET_PRIO_INVERT").is_ok() {
        println!("cargo:rustc-cfg=prio_invert");
    }
    // Case 25 positive control: mask interrupts for ~80 us after arming the
    // probe, so the measured delay must rise to ~30 us.
    println!("cargo:rerun-if-env-changed=RIVET_MAXDIS_CONTROL");
    if std::env::var("RIVET_MAXDIS_CONTROL").is_ok() {
        println!("cargo:rustc-cfg=maxdis_control");
    }
    // Case 20 positive control: disabling the higher-priority pend in both runs
    // must make the nested-minus-baseline value ~0.
    println!("cargo::rustc-check-cfg=cfg(nested_control)");
    println!("cargo:rerun-if-env-changed=BENCH_NESTED_CONTROL");
    if std::env::var("BENCH_NESTED_CONTROL").is_ok() {
        println!("cargo:rustc-cfg=nested_control");
    }
}
