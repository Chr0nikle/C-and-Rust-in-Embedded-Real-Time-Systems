//! Resolve installed tools through the selected SDK, without shell evaluation.
use rtos_core::{
    error::{Error, Result},
    process::{self, CommandSpec},
};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

pub(crate) fn python(env: &[(String, String)]) -> Result<PathBuf> {
    env.iter()
        .find(|(key, _)| key == "IDF_PYTHON_ENV_PATH")
        .map(|(_, value)| Path::new(value).join("bin/python"))
        .ok_or_else(|| Error::Build("ESP-IDF Python environment missing".into()))
}

pub(crate) fn resolve(project: &Path) -> Result<(PathBuf, Vec<(String, String)>)> {
    let project = project.canonicalize()?;
    let sdk = project.join(".local/sdk/esp-idf");
    let script = sdk.join("tools/idf_tools.py");
    if !script.is_file() {
        return Err(Error::Build(format!(
            "ESP-IDF tools missing at {}; run scripts/setup/provision-esp-idf.sh",
            script.display()
        )));
    }
    let mut env = vec![
        ("IDF_PATH".into(), sdk.to_string_lossy().into_owned()),
        (
            "IDF_TOOLS_PATH".into(),
            project
                .join(".local/toolchains/esp-idf")
                .to_string_lossy()
                .into_owned(),
        ),
    ];
    let mut export_env: Vec<_> = env
        .iter()
        .map(|(k, v): &(String, String)| (k.into(), v.into()))
        .collect();
    // Do not reuse another checkout's activated Python environment/version.
    export_env.extend([
        ("IDF_PYTHON_ENV_PATH".into(), "".into()),
        ("ESP_IDF_VERSION".into(), "".into()),
    ]);
    let output = process::run_success(&CommandSpec {
        program: "python3".into(),
        args: vec![
            script.into_os_string(),
            "export".into(),
            "--format".into(),
            "key-value".into(),
        ],
        cwd: sdk.clone(),
        env: export_env,
        timeout: Duration::from_secs(30),
    })?;
    env.extend(parse_export(
        &output.stdout,
        &std::env::var("PATH").unwrap_or_default(),
    )?);
    let python_env = env
        .iter()
        .find(|(k, _)| k == "IDF_PYTHON_ENV_PATH")
        .ok_or_else(|| {
            Error::Build(
                "SDK did not resolve an installed Python environment; run provision-esp-idf.sh"
                    .into(),
            )
        })?;
    if !Path::new(&python_env.1).join("bin/python").is_file() {
        return Err(Error::Build(
            "resolved ESP-IDF Python executable is missing".into(),
        ));
    }
    Ok((sdk, env))
}

fn parse_export(text: &str, inherited_path: &str) -> Result<Vec<(String, String)>> {
    let mut env = Vec::new();
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let (key, value) = line
            .split_once('=')
            .ok_or_else(|| Error::Build("invalid SDK environment export".into()))?;
        if key.is_empty()
            || !key
                .bytes()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_')
        {
            return Err(Error::Build("invalid SDK environment key".into()));
        }
        // Shell activation bookkeeping is not needed by a child process.
        if key == "IDF_DEACTIVATE_FILE_PATH" {
            continue;
        }
        let value = if key == "PATH" {
            let prefix = value
                .strip_suffix("$PATH")
                .ok_or_else(|| Error::Build("SDK PATH export must end in $PATH".into()))?;
            format!("{prefix}{inherited_path}")
        } else {
            value.to_owned()
        };
        env.push((key.to_owned(), value));
    }
    if !env.iter().any(|(k, _)| k == "PATH") {
        return Err(Error::Build("SDK environment export omitted PATH".into()));
    }
    Ok(env)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn export_preserves_spaces_equals_and_literal_shell_text() {
        let env = parse_export("IDF_PYTHON_ENV_PATH=/project with spaces/venv\nPATH=/tools with spaces:$PATH\nVALUE=a=b$(ignored)\nIDF_DEACTIVATE_FILE_PATH=/tmp/x\n", "/usr/bin").unwrap();
        assert_eq!(env[0].1, "/project with spaces/venv");
        assert_eq!(env[1].1, "/tools with spaces:/usr/bin");
        assert_eq!(env[2].1, "a=b$(ignored)");
        assert_eq!(env.len(), 3);
    }
    #[test]
    fn incomplete_or_malformed_export_fails() {
        for text in ["", "noise", "PATH=/wrong", "bad-key=x\nPATH=$PATH"] {
            assert!(parse_export(text, "/bin").is_err(), "{text}");
        }
    }
}
