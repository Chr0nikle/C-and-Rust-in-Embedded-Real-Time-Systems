//! Safety and maintainability analysis for the shared C and Rust firmware.
//!
//! Two independent, versioned analyses:
//! - `scope`: a deterministic line-based source scan of Rust `unsafe` usage and
//!   the C/Rust FFI boundary (no external tool, fully reproducible).
//! - `findings`: clang-tidy diagnostics over the host-compilable shared C
//!   translation units with a pinned check set and explicit compile flags.
//!
//! Neither analysis replaces a type-aware compiler. The report records the
//! settings, tool versions and explicit limitations so a result can never be
//! read as stronger than the method that produced it.

use crate::error::{Error, Result};
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Version of the analysis settings and report layout.
pub const SAFETY_SCHEMA_VERSION: u32 = 4;

/// Versioned inputs for one analysis run.
#[derive(Debug, Clone, Serialize)]
pub struct SafetySettings {
    /// Settings/report schema this run used.
    pub schema_version: u32,
    /// Rust source roots scanned for `unsafe`/FFI scope and effort.
    pub rust_roots: Vec<String>,
    /// Host-compilable C translation units analyzed with clang-tidy.
    pub c_files: Vec<String>,
    /// Include directories for the C translation units.
    pub c_include_dirs: Vec<String>,
    /// Compile flags passed after `--` to clang-tidy.
    pub c_compile_flags: Vec<String>,
    /// Pinned clang-tidy check selection.
    pub clang_tidy_checks: String,
    /// clang-tidy executable name resolved on `PATH`.
    pub clang_tidy: String,
    /// clang executable used for `--analyze` static analysis.
    pub clang: String,
    /// `cargo clippy` manifest (project-relative) for the Rust lint layer.
    pub clippy_manifest: String,
    /// clippy lint flags passed after `--` (unsafe/FFI focused).
    pub clippy_lints: Vec<String>,
}

impl Default for SafetySettings {
    fn default() -> Self {
        Self {
            schema_version: SAFETY_SCHEMA_VERSION,
            rust_roots: vec![
                "firmware/common/rust/src".into(),
                "firmware/c-kernels/freertos/rust/src".into(),
                "firmware/c-kernels/nuttx/rust/src".into(),
                "firmware/c-kernels/zephyr/rust/src".into(),
            ],
            c_files: vec![
                "firmware/common/c/src/runner.c".into(),
                "firmware/common/c/tests/runner.c".into(),
            ],
            c_include_dirs: vec!["firmware/common/c/include".into()],
            c_compile_flags: vec!["-std=c11".into(), "-Wall".into(), "-Wextra".into()],
            clang_tidy_checks: "-*,clang-analyzer-*,bugprone-*,cert-*".into(),
            clang_tidy: "clang-tidy".into(),
            clang: "clang".into(),
            clippy_manifest: "firmware/common/rust/Cargo.toml".into(),
            clippy_lints: vec![
                "-W".into(),
                "unsafe-op-in-unsafe-fn".into(),
                "-W".into(),
                "clippy::undocumented_unsafe_blocks".into(),
                "-W".into(),
                "clippy::missing_safety_doc".into(),
            ],
        }
    }
}

/// Default analysis settings.
pub fn default_settings() -> SafetySettings {
    SafetySettings::default()
}

/// One `unsafe`/FFI construct found by the source scan.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ScopeEntry {
    /// Pattern label, e.g. `unsafe_fn`, `extern_c_block`, `exported`.
    pub kind: String,
    /// Project-relative file path.
    pub file: String,
    /// 1-based line number.
    pub line: usize,
}

/// Aggregated `unsafe`/FFI scope.
#[derive(Debug, Clone, Serialize, Default)]
pub struct ScopeReport {
    /// Occurrence count per pattern label.
    pub counts: BTreeMap<String, usize>,
    /// Every occurrence, ordered by file then line.
    pub entries: Vec<ScopeEntry>,
    /// Rust files scanned.
    pub rust_files: usize,
    /// Total Rust lines scanned.
    pub rust_lines: usize,
}

/// One clang-tidy diagnostic.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Finding {
    /// Project-relative file path.
    pub file: String,
    /// 1-based line.
    pub line: u32,
    /// 1-based column.
    pub column: u32,
    /// `warning` or `error`.
    pub severity: String,
    /// clang-tidy check name.
    pub check: String,
    /// Diagnostic text.
    pub message: String,
}

/// Source size and structural complexity as a development-effort proxy.
#[derive(Debug, Clone, Serialize, Default)]
pub struct EffortReport {
    /// Files counted.
    pub files: usize,
    /// Total lines across Rust and C sources.
    pub lines: usize,
    /// Rust lines.
    pub rust_lines: usize,
    /// C lines.
    pub c_lines: usize,
    /// Non-comment, non-blank lines.
    pub code_lines: usize,
    /// Comment lines (line and block comments).
    pub comment_lines: usize,
    /// Blank lines.
    pub blank_lines: usize,
    /// Cyclomatic decision points (`if`/`for`/`while`/`loop`/`match`/`case`/`&&`/`||`/`?`).
    pub decision_points: usize,
    /// Top-level function definitions counted.
    pub functions: usize,
    /// Cyclomatic decision points counted inside functions.
    pub function_decision_points: usize,
    /// Largest per-function decision-point count.
    pub max_function_decision_points: usize,
    /// Mean per-function decision-point count.
    pub mean_function_decision_points: f64,
    /// Mean Halstead volume per file (heuristic).
    pub halstead_volume_mean: f64,
    /// Mean per-file Maintainability Index (heuristic, 0-100).
    pub maintainability_index_mean: f64,
    /// Minimum per-file Maintainability Index (heuristic, 0-100).
    pub maintainability_index_min: f64,
}

/// Complete analysis result, serialized as the evidence report.
#[derive(Debug, Clone, Serialize)]
pub struct SafetyReport {
    /// Schema of this report.
    pub schema_version: u32,
    /// Inputs used, echoed for reproducibility.
    pub settings: SafetySettings,
    /// Tool name to version string (or `unavailable: ...`).
    pub tools: BTreeMap<String, String>,
    /// `unsafe`/FFI source scope.
    pub scope: ScopeReport,
    /// clang-tidy findings over the shared C translation units.
    pub findings: Vec<Finding>,
    /// Whether clang-tidy ran and produced the findings above.
    pub findings_ran: bool,
    /// clippy findings over the Rust manifest; empty when clippy was unavailable.
    pub clippy_findings: Vec<Finding>,
    /// Whether clippy ran and produced the findings above.
    pub clippy_ran: bool,
    /// clang static-analyzer findings over the shared C translation units.
    pub clang_analyze_findings: Vec<Finding>,
    /// Whether `clang --analyze` ran.
    pub clang_analyze_ran: bool,
    /// Development-effort proxy.
    pub effort: EffortReport,
    /// Explicit method limits, never omitted.
    pub limitations: Vec<String>,
}

/// Line patterns counted by the scope scan, in report order.
const PATTERNS: &[(&str, &str)] = &[
    ("unsafe_block", "unsafe {"),
    ("unsafe_fn", "unsafe fn "),
    ("unsafe_impl", "unsafe impl "),
    ("unsafe_trait", "unsafe trait "),
    ("unsafe_extern", "unsafe extern"),
    ("extern_c_block", "extern \"C\" {"),
    ("extern_c_fn", "extern \"C\" fn"),
    ("exported", "#[no_mangle]"),
];

fn relative(project: &Path, path: &Path) -> String {
    path.strip_prefix(project)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn collect(path: &Path, extension: &str, out: &mut Vec<PathBuf>) -> Result<()> {
    let entries = std::fs::read_dir(path)
        .map_err(|e| Error::Invalid(format!("read {}: {e}", path.display())))?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect(&path, extension, out)?;
        } else if path.extension().is_some_and(|e| e == extension) {
            out.push(path);
        }
    }
    Ok(())
}

/// Scan Rust sources for `unsafe`/FFI constructs.
pub fn scan_rust_scope(project: &Path, roots: &[String]) -> Result<ScopeReport> {
    let mut files = Vec::new();
    for root in roots {
        let dir = project.join(root);
        if dir.is_dir() {
            collect(&dir, "rs", &mut files)?;
        }
    }
    files.sort();

    let mut report = ScopeReport::default();
    for path in &files {
        let text = std::fs::read_to_string(path)
            .map_err(|e| Error::Invalid(format!("read {}: {e}", path.display())))?;
        report.rust_files += 1;
        let file = relative(project, path);
        for (index, line) in text.lines().enumerate() {
            report.rust_lines += 1;
            for (kind, needle) in PATTERNS {
                let count = line.matches(needle).count();
                if count > 0 {
                    *report.counts.entry((*kind).into()).or_default() += count;
                    report.entries.push(ScopeEntry {
                        kind: (*kind).into(),
                        file: file.clone(),
                        line: index + 1,
                    });
                }
            }
        }
    }
    report.entries.sort_by(|a, b| {
        a.file
            .cmp(&b.file)
            .then(a.line.cmp(&b.line))
            .then(a.kind.cmp(&b.kind))
    });
    Ok(report)
}

/// Classify one source file into (code, comment, blank) lines and count
/// cyclomatic decision points. `rust` selects the keyword set (`loop`/`match`
/// for Rust, `case` for C). The block-comment state is approximate; this is a
/// deterministic token-level proxy, not a parser.
fn classify(text: &str, rust: bool) -> (usize, usize, usize, usize) {
    let (mut code, mut comment, mut blank, mut decisions) = (0, 0, 0, 0);
    let mut in_block = false;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            blank += 1;
            continue;
        }
        if in_block {
            comment += 1;
            if trimmed.contains("*/") {
                in_block = false;
            }
            continue;
        }
        if trimmed.starts_with("//") {
            comment += 1;
            continue;
        }
        if trimmed.starts_with("/*") {
            comment += 1;
            if !trimmed.contains("*/") {
                in_block = true;
            }
            continue;
        }
        code += 1;
        for word in trimmed.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_')) {
            let decision = if rust {
                matches!(word, "if" | "for" | "while" | "loop" | "match")
            } else {
                matches!(word, "if" | "for" | "while" | "case")
            };
            if decision {
                decisions += 1;
            }
        }
        decisions += trimmed.matches("&&").count()
            + trimmed.matches("||").count()
            + trimmed.matches('?').count();
    }
    (code, comment, blank, decisions)
}

/// Count cyclomatic decision points on one line, matching `classify`'s rule.
fn count_decisions(line: &str, rust: bool) -> usize {
    let mut decisions = 0;
    for word in line.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_')) {
        let decision = if rust {
            matches!(word, "if" | "for" | "while" | "loop" | "match")
        } else {
            matches!(word, "if" | "for" | "while" | "case")
        };
        if decision {
            decisions += 1;
        }
    }
    decisions + line.matches("&&").count() + line.matches("||").count() + line.matches('?').count()
}

/// Net brace depth change on one line, skipping double-quoted strings and
/// trailing line comments (char literals and Rust lifetimes are left alone).
fn brace_delta(line: &str) -> i64 {
    let mut delta = 0i64;
    let mut in_str = false;
    let mut escape = false;
    let chars: Vec<char> = line.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_str {
            if escape {
                escape = false;
            } else if c == '\\' {
                escape = true;
            } else if c == '"' {
                in_str = false;
            }
            i += 1;
            continue;
        }
        if c == '"' {
            in_str = true;
        } else if c == '/' && chars.get(i + 1) == Some(&'/') {
            break;
        } else if c == '{' {
            delta += 1;
        } else if c == '}' {
            delta -= 1;
        }
        i += 1;
    }
    delta
}

/// Heuristic test for the first line of a top-level function definition.
///
/// This is a deterministic token/brace proxy, not a parser: a trait method
/// signature ending in `;` is rejected, and C preprocessor lines and
/// declarations ending in `;` are rejected.
fn is_function_start(line: &str, rust: bool) -> bool {
    if !line
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
    {
        return false;
    }
    if rust {
        let has_fn = line
            .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
            .any(|word| word == "fn");
        has_fn && line.contains('(') && !line.ends_with(';')
    } else {
        if line.starts_with('#') || line.ends_with(';') {
            return false;
        }
        let first = line
            .split(|c: char| c == '(' || c.is_whitespace())
            .find(|s| !s.is_empty())
            .unwrap_or("");
        let control = [
            "if", "for", "while", "switch", "else", "do", "return", "sizeof", "typedef", "struct",
            "enum", "union",
        ];
        !control.contains(&first) && line.contains('(') && line.contains(')')
    }
}

/// Count top-level functions and their cyclomatic complexity: returns
/// `(functions, max decision points in one function, total inside functions)`.
fn function_complexity(text: &str, rust: bool) -> (usize, usize, usize) {
    let (mut functions, mut max, mut total) = (0usize, 0usize, 0usize);
    let mut depth: i64 = 0;
    let mut in_fn = false;
    let mut entered = false;
    let mut fn_depth: i64 = 0;
    let mut fn_decisions = 0usize;
    let mut in_block = false;
    for raw in text.lines() {
        let line = raw.trim();
        if in_block {
            if line.contains("*/") {
                in_block = false;
            }
            continue;
        }
        if line.starts_with("/*") {
            if !line.contains("*/") {
                in_block = true;
            }
            continue;
        }
        if line.starts_with("//") {
            continue;
        }
        let delta = brace_delta(line);
        if !in_fn {
            if depth == 0 && is_function_start(line, rust) {
                in_fn = true;
                entered = false;
                fn_depth = 0;
                fn_decisions = 0;
                functions += 1;
            } else {
                depth += delta;
                continue;
            }
        }
        if line.contains('{') {
            entered = true;
        }
        fn_decisions += count_decisions(line, rust);
        fn_depth += delta;
        depth += delta;
        if entered && fn_depth <= 0 {
            total += fn_decisions;
            if fn_decisions > max {
                max = fn_decisions;
            }
            in_fn = false;
        }
    }
    (functions, max, total)
}

/// Halstead operator/operand counts for one source text.
///
/// Heuristic, deterministic token classification: language keywords and
/// punctuation runs are operators, other identifiers and literals are operands;
/// comments and string/char literals are skipped. Returns
/// `(distinct operators, distinct operands, total operators, total operands)`.
fn halstead(text: &str, rust: bool) -> (usize, usize, usize, usize) {
    use std::collections::HashSet;
    let keywords: &[&str] = if rust {
        &[
            "fn", "let", "mut", "if", "else", "match", "for", "while", "loop", "return", "break",
            "continue", "impl", "trait", "struct", "enum", "pub", "use", "mod", "const", "static",
            "unsafe", "extern", "as", "in", "ref", "move", "where", "self", "Self", "super",
            "crate", "dyn", "async", "await", "true", "false",
        ]
    } else {
        &[
            "if", "else", "for", "while", "do", "switch", "case", "default", "return", "break",
            "continue", "struct", "union", "enum", "typedef", "static", "const", "void", "int",
            "char", "long", "short", "unsigned", "signed", "float", "double", "sizeof", "goto",
            "extern", "inline", "volatile", "register", "auto", "NULL",
        ]
    };
    let mut ops: HashSet<String> = HashSet::new();
    let mut oprs: HashSet<String> = HashSet::new();
    let (mut total_ops, mut total_oprs) = (0usize, 0usize);
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    let mut in_block = false;
    while i < chars.len() {
        let c = chars[i];
        if in_block {
            if c == '*' && chars.get(i + 1) == Some(&'/') {
                in_block = false;
                i += 2;
            } else {
                i += 1;
            }
            continue;
        }
        if c == '/' && chars.get(i + 1) == Some(&'/') {
            while i < chars.len() && chars[i] != '\n' {
                i += 1;
            }
            continue;
        }
        if c == '/' && chars.get(i + 1) == Some(&'*') {
            in_block = true;
            i += 2;
            continue;
        }
        if c == '"' || c == '\'' {
            let quote = c;
            i += 1;
            while i < chars.len() {
                if chars[i] == '\\' {
                    i += 2;
                    continue;
                }
                if chars[i] == quote {
                    i += 1;
                    break;
                }
                i += 1;
            }
            total_oprs += 1;
            oprs.insert("<literal>".to_string());
            continue;
        }
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        if c.is_ascii_alphanumeric() || c == '_' {
            let start = i;
            while i < chars.len() && (chars[i].is_ascii_alphanumeric() || chars[i] == '_') {
                i += 1;
            }
            let token: String = chars[start..i].iter().collect();
            if keywords.contains(&token.as_str()) {
                total_ops += 1;
                ops.insert(token);
            } else {
                total_oprs += 1;
                oprs.insert(token);
            }
            continue;
        }
        let start = i;
        while i < chars.len() {
            let d = chars[i];
            if d.is_ascii_alphanumeric() || d == '_' || d.is_whitespace() || d == '"' || d == '\'' {
                break;
            }
            if d == '/' && (chars.get(i + 1) == Some(&'/') || chars.get(i + 1) == Some(&'*')) {
                break;
            }
            i += 1;
        }
        let token: String = chars[start..i].iter().collect();
        total_ops += 1;
        ops.insert(token);
    }
    (ops.len(), oprs.len(), total_ops, total_oprs)
}

/// Halstead program volume `N * log2(n)` from the four counts.
fn halstead_volume(n1: usize, n2: usize, total_ops: usize, total_oprs: usize) -> f64 {
    let vocabulary = (n1 + n2) as f64;
    let length = (total_ops + total_oprs) as f64;
    if vocabulary <= 1.0 {
        0.0
    } else {
        length * vocabulary.log2()
    }
}

/// Maintainability Index (0-100) from Halstead volume `v`, total cyclomatic
/// complexity `g` and lines of code `loc`. Heuristic; the classic formula
/// normalised to the 0-100 range.
fn maintainability_index(v: f64, g: f64, loc: f64) -> f64 {
    if v <= 0.0 || loc <= 0.0 {
        return 0.0;
    }
    let raw = 171.0 - 5.2 * v.ln() - 0.23 * g - 16.2 * loc.ln();
    (raw * 100.0 / 171.0).clamp(0.0, 100.0)
}

/// Count files and lines across the configured Rust and C sources.
pub fn scan_effort(project: &Path, settings: &SafetySettings) -> Result<EffortReport> {
    let mut rust = Vec::new();
    for root in &settings.rust_roots {
        let dir = project.join(root);
        if dir.is_dir() {
            collect(&dir, "rs", &mut rust)?;
        }
    }
    let mut report = EffortReport::default();
    let mut vol_sum = 0.0f64;
    let mut mi_sum = 0.0f64;
    let mut mi_min: Option<f64> = None;
    let mut mi_files = 0usize;
    for path in &rust {
        let text = std::fs::read_to_string(path)
            .map_err(|e| Error::Invalid(format!("read {}: {e}", path.display())))?;
        report.files += 1;
        report.rust_lines += text.lines().count();
        let (code, comment, blank, decisions) = classify(&text, true);
        report.code_lines += code;
        report.comment_lines += comment;
        report.blank_lines += blank;
        report.decision_points += decisions;
        let (functions, max_fn, total_fn) = function_complexity(&text, true);
        report.functions += functions;
        report.function_decision_points += total_fn;
        report.max_function_decision_points = report.max_function_decision_points.max(max_fn);
        let (n1, n2, total_ops, total_oprs) = halstead(&text, true);
        if code > 0 {
            let volume = halstead_volume(n1, n2, total_ops, total_oprs);
            let g = (decisions + functions) as f64;
            let mi = maintainability_index(volume, g, code as f64);
            vol_sum += volume;
            mi_sum += mi;
            mi_min = Some(mi_min.map_or(mi, |m| m.min(mi)));
            mi_files += 1;
        }
    }
    for file in &settings.c_files {
        let path = project.join(file);
        let text = std::fs::read_to_string(&path)
            .map_err(|e| Error::Invalid(format!("read {}: {e}", path.display())))?;
        report.files += 1;
        report.c_lines += text.lines().count();
        let (code, comment, blank, decisions) = classify(&text, false);
        report.code_lines += code;
        report.comment_lines += comment;
        report.blank_lines += blank;
        report.decision_points += decisions;
        let (functions, max_fn, total_fn) = function_complexity(&text, false);
        report.functions += functions;
        report.function_decision_points += total_fn;
        report.max_function_decision_points = report.max_function_decision_points.max(max_fn);
        let (n1, n2, total_ops, total_oprs) = halstead(&text, false);
        if code > 0 {
            let volume = halstead_volume(n1, n2, total_ops, total_oprs);
            let g = (decisions + functions) as f64;
            let mi = maintainability_index(volume, g, code as f64);
            vol_sum += volume;
            mi_sum += mi;
            mi_min = Some(mi_min.map_or(mi, |m| m.min(mi)));
            mi_files += 1;
        }
    }
    report.lines = report.rust_lines + report.c_lines;
    if report.functions > 0 {
        report.mean_function_decision_points =
            report.function_decision_points as f64 / report.functions as f64;
    }
    if mi_files > 0 {
        report.halstead_volume_mean = vol_sum / mi_files as f64;
        report.maintainability_index_mean = mi_sum / mi_files as f64;
        report.maintainability_index_min = mi_min.unwrap_or(0.0);
    }
    Ok(report)
}

/// Parse clang-tidy diagnostics into findings (notes are dropped).
pub fn parse_clang_tidy(output: &str, project: &Path) -> Vec<Finding> {
    let mut findings = Vec::new();
    for line in output.lines() {
        let mut fields = line.splitn(5, ':');
        let (Some(file), Some(line_no), Some(column), Some(severity), Some(rest)) = (
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
        ) else {
            continue;
        };
        if !matches!(severity.trim(), "warning" | "error") {
            continue;
        }
        let (Ok(line_no), Ok(column)) = (line_no.trim().parse(), column.trim().parse()) else {
            continue;
        };
        let message = rest.trim();
        let (message, check) = match message.rsplit_once('[') {
            Some((text, check)) if check.ends_with(']') => {
                (text.trim(), check.trim_end_matches(']'))
            }
            _ => (message, ""),
        };
        let file = Path::new(file.trim());
        findings.push(Finding {
            file: relative(project, file),
            line: line_no,
            column,
            severity: severity.trim().to_string(),
            check: check.to_string(),
            message: message.to_string(),
        });
    }
    findings
}

fn tool_version(program: &str, argument: &str) -> String {
    match Command::new(program).arg(argument).output() {
        Ok(output) if output.status.success() => {
            let text = String::from_utf8_lossy(&output.stdout);
            text.lines()
                .map(str::trim)
                .find(|line| line.chars().any(|c| c.is_ascii_digit()))
                .unwrap_or("")
                .to_string()
        }
        _ => format!("unavailable: {program} {argument} failed"),
    }
}

/// Run the clang static analyzer (`clang --analyze`) over the host-compilable C
/// translation units and parse its text diagnostics.
fn run_clang_analyze(project: &Path, settings: &SafetySettings) -> (Vec<Finding>, bool) {
    let mut findings = Vec::new();
    let mut ran = true;
    for file in &settings.c_files {
        let mut args: Vec<String> = vec![
            "--analyze".into(),
            "-Xanalyzer".into(),
            "-analyzer-output=text".into(),
        ];
        args.extend(settings.c_compile_flags.iter().cloned());
        for dir in &settings.c_include_dirs {
            args.push(format!("-I{dir}"));
        }
        args.push(project.join(file).to_string_lossy().into_owned());
        match Command::new(&settings.clang)
            .args(&args)
            .current_dir(project)
            .output()
        {
            Ok(out) => {
                let text = format!(
                    "{}{}",
                    String::from_utf8_lossy(&out.stdout),
                    String::from_utf8_lossy(&out.stderr)
                );
                findings.extend(parse_clang_tidy(&text, project));
            }
            Err(_) => ran = false,
        }
    }
    (findings, ran)
}

/// Run `cargo clippy` with the unsafe/FFI lints over the Rust manifest and parse
/// its short-format diagnostics.
fn run_clippy(project: &Path, settings: &SafetySettings) -> (Vec<Finding>, bool) {
    let mut args: Vec<String> = vec!["clippy".into()];
    args.push("--manifest-path".into());
    args.push(
        project
            .join(&settings.clippy_manifest)
            .to_string_lossy()
            .into_owned(),
    );
    args.push("--message-format=short".into());
    args.push("--".into());
    args.extend(settings.clippy_lints.iter().cloned());
    match Command::new("cargo")
        .args(&args)
        .current_dir(project)
        .output()
    {
        Ok(out) => {
            let text = format!(
                "{}{}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            );
            (parse_clang_tidy(&text, project), true)
        }
        Err(_) => (Vec::new(), false),
    }
}

/// Run the complete analysis. clang-tidy absence degrades to scope+effort with
/// an explicit limitation and `findings_ran = false`.
pub fn analyze(project: &Path, settings: &SafetySettings) -> Result<SafetyReport> {
    if settings.schema_version != SAFETY_SCHEMA_VERSION {
        return Err(Error::Invalid(format!(
            "unsupported safety settings schema {}; expected {SAFETY_SCHEMA_VERSION}",
            settings.schema_version
        )));
    }
    let mut tools = BTreeMap::new();
    tools.insert("rustc".into(), tool_version("rustc", "--version"));
    tools.insert(
        "clang-tidy".into(),
        tool_version(&settings.clang_tidy, "--version"),
    );
    tools.insert("clang".into(), tool_version(&settings.clang, "--version"));
    tools.insert("clippy".into(), tool_version("cargo-clippy", "--version"));

    let mut args: Vec<String> = vec![format!("--checks={}", settings.clang_tidy_checks)];
    args.extend(settings.c_files.iter().cloned());
    args.push("--".into());
    args.extend(settings.c_compile_flags.iter().cloned());
    for dir in &settings.c_include_dirs {
        args.push(format!("-I{dir}"));
    }
    let output = Command::new(&settings.clang_tidy)
        .args(&args)
        .current_dir(project)
        .output();
    let (findings, findings_ran) = match output {
        Ok(out) => {
            let text = format!(
                "{}{}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            );
            (parse_clang_tidy(&text, project), true)
        }
        Err(_) => (Vec::new(), false),
    };

    let (clang_analyze_findings, clang_analyze_ran) = run_clang_analyze(project, settings);
    let (clippy_findings, clippy_ran) = run_clippy(project, settings);

    let scope = scan_rust_scope(project, &settings.rust_roots)?;
    let effort = scan_effort(project, settings)?;
    let mut limitations = vec![
        "findings cover only host-compilable shared C translation units; ARM/CMSIS and RTOS adapter sources need a cross toolchain and are excluded".into(),
        "unsafe/FFI scope is a line-based source scan, not a compiler- or type-aware analysis; it covers the project's Rust adapters but not the vendored kernels or per-RTOS C ports".into(),
        "structural complexity is a token-level cyclomatic decision-point proxy (if/for/while/loop/match/case/&&/||/?), not a parser-verified McCabe count".into(),
    ];
    if !findings_ran {
        limitations.push(format!(
            "clang-tidy was unavailable, so findings are empty rather than verified clean: {}",
            tools.get("clang-tidy").cloned().unwrap_or_default()
        ));
    }
    if !clang_analyze_ran {
        limitations.push(format!(
            "clang --analyze was unavailable: {}",
            tools.get("clang").cloned().unwrap_or_default()
        ));
    }
    if !clippy_ran {
        limitations.push(format!(
            "clippy was unavailable, so clippy_findings are empty rather than verified clean: {}",
            tools.get("clippy").cloned().unwrap_or_default()
        ));
    }
    Ok(SafetyReport {
        schema_version: SAFETY_SCHEMA_VERSION,
        settings: settings.clone(),
        tools,
        scope,
        findings,
        findings_ran,
        clippy_findings,
        clippy_ran,
        clang_analyze_findings,
        clang_analyze_ran,
        effort,
        limitations,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patterns_cover_unsafe_and_ffi() {
        let labels: Vec<_> = PATTERNS.iter().map(|(k, _)| *k).collect();
        assert!(labels.contains(&"unsafe_block"));
        assert!(labels.contains(&"extern_c_block"));
        assert!(labels.contains(&"exported"));
    }

    #[test]
    fn parses_warning_and_drops_notes() {
        let output = "\
firmware/common/c/src/runner.c:4:49: warning: swappable parameters [bugprone-easily-swappable-parameters]
firmware/common/c/src/runner.c:4:60: note: the first parameter is 'id'
[1/2] Processing file /tmp/runner.c.\n";
        let findings = parse_clang_tidy(output, Path::new("/tmp"));
        assert_eq!(findings.len(), 1);
        let finding = &findings[0];
        assert_eq!(finding.line, 4);
        assert_eq!(finding.column, 49);
        assert_eq!(finding.severity, "warning");
        assert_eq!(finding.check, "bugprone-easily-swappable-parameters");
        assert_eq!(finding.file, "firmware/common/c/src/runner.c");
    }

    #[test]
    fn scope_scan_counts_each_construct() {
        let dir = std::env::temp_dir().join(format!("rtos-safety-{}", std::process::id()));
        let root = dir.join("rust");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
            root.join("a.rs"),
            "unsafe { go(); }\nunsafe fn f() {}\n#[no_mangle]\nextern \"C\" fn g() {}\n",
        )
        .unwrap();
        let scope = scan_rust_scope(&dir, &["rust".into()]).unwrap();
        assert_eq!(scope.rust_files, 1);
        assert_eq!(scope.rust_lines, 4);
        assert_eq!(scope.counts.get("unsafe_block"), Some(&1));
        assert_eq!(scope.counts.get("unsafe_fn"), Some(&1));
        assert_eq!(scope.counts.get("exported"), Some(&1));
        assert_eq!(scope.counts.get("extern_c_fn"), Some(&1));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn effort_sums_rust_and_c() {
        let dir = std::env::temp_dir().join(format!("rtos-effort-{}", std::process::id()));
        let root = dir.join("rust");
        let csrc = dir.join("c");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&csrc).unwrap();
        std::fs::write(root.join("a.rs"), "a\nb\nc\n").unwrap();
        std::fs::write(csrc.join("r.c"), "x\ny\n").unwrap();
        let settings = SafetySettings {
            rust_roots: vec!["rust".into()],
            c_files: vec!["c/r.c".into()],
            ..SafetySettings::default()
        };
        let effort = scan_effort(&dir, &settings).unwrap();
        assert_eq!(effort.files, 2);
        assert_eq!(effort.rust_lines, 3);
        assert_eq!(effort.c_lines, 2);
        assert_eq!(effort.lines, 5);
        assert_eq!(effort.code_lines, 5);
        assert_eq!(effort.comment_lines, 0);
        assert_eq!(effort.blank_lines, 0);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn classify_separates_code_comment_blank_and_counts_decisions() {
        let c = "int f(int x) {\n  if (x > 0 && x < 10) {\n    // guard\n    while (x--) {}\n  }\n  /* block\n     comment */\n  return x ? 1 : 0;\n}\n";
        let (code, comment, blank, decisions) = classify(c, false);
        assert_eq!(code, 6);
        assert_eq!(comment, 3);
        assert_eq!(blank, 0);
        // if + while + && + ternary(?) = 4 decision points.
        assert_eq!(decisions, 4);
    }

    #[test]
    fn function_complexity_counts_top_level_functions_and_their_decisions() {
        let rust = "fn a() {\n    if x { }\n    for y in z { }\n}\n\npub fn b() {\n    match v { _ => {} }\n}\n";
        let (functions, max, total) = function_complexity(rust, true);
        assert_eq!(functions, 2);
        assert_eq!(total, 3); // a: if + for; b: match
        assert_eq!(max, 2);

        let c = "int f(int x) {\n    if (x) {\n        return 1;\n    }\n    return 0;\n}\n";
        let (functions, max, total) = function_complexity(c, false);
        assert_eq!(functions, 1);
        assert_eq!(total, 1);
        assert_eq!(max, 1);

        // A trait-style signature without a body is not counted.
        assert_eq!(function_complexity("fn f(&self) -> u32;\n", true).0, 0);

        // Braces inside strings and trailing comments do not affect depth.
        assert_eq!(brace_delta("let s = \"{\"; // }"), 0);
    }

    #[test]
    fn halstead_volume_and_mi_are_finite() {
        let src = "fn f(x: u32) -> u32 {\n    if x > 0 { x + 1 } else { 0 }\n}\n";
        let (n1, n2, total_ops, total_oprs) = halstead(src, true);
        assert!(n1 > 0 && n2 > 0 && total_ops > 0 && total_oprs > 0);
        let v = halstead_volume(n1, n2, total_ops, total_oprs);
        assert!(v > 0.0);
        let mi = maintainability_index(v, 2.0, 3.0);
        assert!((0.0..=100.0).contains(&mi));
        assert_eq!(maintainability_index(0.0, 1.0, 10.0), 0.0);
    }
}
