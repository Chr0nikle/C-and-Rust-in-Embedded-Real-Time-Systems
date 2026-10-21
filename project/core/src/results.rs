//! Result-session persistence: run directory, provenance manifest, and evidence.
//!
//! Benchmark evidence is written under `results/campaigns/<build_id>/` (the build id is
//! the stable run key, matching the comparison scripts and existing evidence).
//! Each run directory carries a `manifest.json` with the provenance required by
//! rules.md section 6, alongside the raw UART capture and the parsed samples.
use crate::error::{Error, Result};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

/// Repository-level evidence directory (sibling of the project folder).
pub fn results_root(project: &Path) -> PathBuf {
    project
        .parent()
        .map_or_else(|| project.join("results"), |root| root.join("results"))
}

/// Campaign evidence directory (all benchmark runs and bundles).
pub fn campaigns_root(project: &Path) -> PathBuf {
    results_root(project).join("campaigns")
}

/// Ensure the per-build result directory exists and return it.
pub fn ensure_run_dir(project: &Path, build_id: &str) -> Result<PathBuf> {
    let dir = campaigns_root(project).join(build_id);
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

/// SHA-256 of a file (lowercase hex), for artifact provenance.
pub fn sha256_file(path: &Path) -> Result<String> {
    use std::io::Read;
    let mut file = std::fs::File::open(path)
        .map_err(|e| Error::Io(format!("open {}: {e}", path.display())))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 16384];
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| Error::Io(format!("read {}: {e}", path.display())))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// Write a provenance manifest to a run directory (atomic replace).
pub fn write_manifest(dir: &Path, manifest: &serde_json::Value) -> Result<()> {
    let mut bytes = serde_json::to_vec_pretty(manifest)
        .map_err(|e| Error::Protocol(format!("encode manifest: {e}")))?;
    bytes.push(b'\n');
    let temporary = dir.join("manifest.tmp");
    std::fs::write(&temporary, &bytes)?;
    std::fs::rename(temporary, dir.join("manifest.json"))?;
    Ok(())
}

/// SHA-256 of a byte slice (lowercase hex).
pub fn sha256_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Current git HEAD, dirty state, and hashes of the tracked diff and status.
///
/// `head` stays the short 8-character form used by existing build IDs.
/// `head_full`, `diff_sha256` and `status_sha256` strengthen provenance:
/// the diff hash covers tracked changes; the status hash also captures the
/// names of untracked files. Untracked file *contents* are not stored, so a
/// clean commit is still required for a fully reproducible campaign.
pub fn source_state(project: &Path) -> serde_json::Value {
    let git = |args: &[&str]| -> Option<Vec<u8>> {
        std::process::Command::new("git")
            .arg("-C")
            .arg(project)
            .args(args)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| o.stdout)
    };
    let head_full = git(&["rev-parse", "HEAD"])
        .map(|b| String::from_utf8_lossy(&b).trim().to_string())
        .unwrap_or_default();
    let head = head_full.chars().take(8).collect::<String>();
    let status = git(&["status", "--porcelain=v1", "-z"]).unwrap_or_default();
    let diff = git(&["diff", "--binary", "HEAD"]).unwrap_or_default();
    let dirty = !status.is_empty();
    serde_json::json!({
        "head": head,
        "head_full": head_full,
        "dirty": dirty,
        "diff_sha256": sha256_bytes(&diff),
        "status_sha256": sha256_bytes(&status),
    })
}

fn first_line(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .lines()
        .next()
        .unwrap_or("")
        .trim()
        .to_string()
}

fn version_of(program: &std::path::Path, args: &[&str]) -> String {
    std::process::Command::new(program)
        .args(args)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| {
            let text = first_line(&o.stdout);
            if text.is_empty() {
                first_line(&o.stderr)
            } else {
                text
            }
        })
        .unwrap_or_else(|| "unavailable".into())
}

fn arm_gcc(project: &Path) -> std::path::PathBuf {
    let root = project.join(".local/toolchains/arm-none-eabi");
    if let Ok(entries) = std::fs::read_dir(&root) {
        for entry in entries.flatten() {
            let candidate = entry.path().join("bin/arm-none-eabi-gcc");
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    std::path::PathBuf::from("arm-none-eabi-gcc")
}

/// Toolchain versions relevant to the firmware builds. Best-effort: missing
/// tools are recorded as `unavailable`, never as a run failure.
pub fn tool_versions(project: &Path) -> serde_json::Value {
    let west = {
        let local = project.join(".local/west/west");
        if local.is_file() {
            local
        } else {
            std::path::PathBuf::from("west")
        }
    };
    serde_json::json!({
        "host": env!("CARGO_PKG_VERSION"),
        "rustc": version_of(std::path::Path::new("rustc"), &["--version"]),
        "cargo": version_of(std::path::Path::new("cargo"), &["--version"]),
        "arm_none_eabi_gcc": version_of(&arm_gcc(project), &["--version"]),
        "west": version_of(&west, &["--version"]),
        "cmake": version_of(std::path::Path::new("cmake"), &["--version"]),
        "probe_rs": version_of(std::path::Path::new("probe-rs"), &["--version"]),
        "python3": version_of(std::path::Path::new("python3"), &["--version"]),
        "source_pins": {
            "path": "config/sources.toml",
            "sha256": sha256_file(&project.join("config/sources.toml"))
                .unwrap_or_else(|_| "unavailable".into()),
        },
    })
}

/// Descriptive build profile. The authoritative flags live in the build files
/// at the recorded source revision; this field makes the expected policy
/// visible in every manifest.
pub fn build_profile(plugin_id: &str, language: &str) -> serde_json::Value {
    let c_optimization = match plugin_id {
        "freertos" => "-O2",
        "zephyr" => "CONFIG_SIZE_OPTIMIZATIONS (-Os)",
        "nuttx" => "CONFIG_DEBUG_FULLOPT",
        _ => "n/a",
    };
    let rust_profile = match plugin_id {
        "freertos-in-rust" => "release, opt-level=s",
        "rivet" => "release, opt-level=z",
        _ => "release, opt-level=3 (workspace LTO)",
    };
    serde_json::json!({
        "language": language,
        "c_optimization": c_optimization,
        "rust_profile": rust_profile,
        "note": "descriptive policy; effective flags are defined by the build files at source.head_full"
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_of_known_content_is_stable() {
        let dir = std::env::temp_dir().join(format!("rtos-sha-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a.bin");
        std::fs::write(&path, b"abc").unwrap();
        // sha256("abc")
        assert_eq!(
            sha256_file(&path).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        std::fs::remove_dir_all(&dir).ok();
    }
}
