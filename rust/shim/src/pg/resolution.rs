//! Native operator resolution and container transport; embedded lifecycle is separate.
use super::{Dsn, Error};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
};

pub enum Target {
    Direct(Box<Dsn>),
    ExistingEmbedded(PathBuf),
    Container,
}
/// Preserve DSN -> existing embedded -> container order, never initialize a bank.
pub fn select(
    dsn: Option<&str>,
    data_dir: &Path,
    embedded_available: bool,
) -> Result<Target, Error> {
    if let Some(dsn) = dsn.filter(|dsn| !dsn.is_empty()) {
        return Dsn::parse(dsn).map(Box::new).map(Target::Direct);
    }
    let pgdata = data_dir.join("embedded_pg");
    Ok(
        if embedded_available && pgdata.join("PG_VERSION").exists() {
            Target::ExistingEmbedded(pgdata)
        } else {
            Target::Container
        },
    )
}
pub fn data_directory(
    explicit: Option<&Path>,
    lite_default: &Path,
    cwd: &Path,
    embedded_available: bool,
) -> PathBuf {
    explicit
        .filter(|path| !path.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| {
            if embedded_available && lite_default.exists() {
                lite_default.to_path_buf()
            } else {
                cwd.join("data")
            }
        })
}
pub fn container_inspect(docker: &std::ffi::OsStr) -> Vec<OsString> {
    [
        docker.to_owned(),
        "inspect".into(),
        "-f".into(),
        "{{.State.Running}}".into(),
        "pseudolife-mcp-daemon".into(),
    ]
    .into()
}
pub fn container_engine_inspect(docker: &std::ffi::OsStr) -> Vec<OsString> {
    [
        docker.to_owned(),
        "inspect".into(),
        "-f".into(),
        "{{index .Config.Labels \"org.pseudolife.engine\"}}".into(),
        "pseudolife-mcp-daemon".into(),
    ]
    .into()
}
/// The ordinary Python image's operator command (including unlabelled images).
pub fn container_exec(
    docker: &std::ffi::OsStr,
    terminal: bool,
    mode: &str,
    arguments: &[OsString],
) -> Vec<OsString> {
    container_exec_for_engine(docker, terminal, mode, arguments, false)
}

/// Preserve argv boundaries and the nesting guard on both image engines.
pub fn container_exec_for_engine(
    docker: &std::ffi::OsStr,
    terminal: bool,
    mode: &str,
    arguments: &[OsString],
    rust: bool,
) -> Vec<OsString> {
    let mut command: Vec<_> = [
        docker.to_owned(),
        "exec".into(),
        if terminal { "-it".into() } else { "-i".into() },
        "-e".into(),
        "PSEUDOLIFE_DAEMON_EXEC=1".into(),
        "pseudolife-mcp-daemon".into(),
    ]
    .into();
    if rust {
        command.push("pseudolife-mcp".into());
    } else {
        command.extend(["python".into(), "-m".into(), "pseudolife_memory.cli".into()]);
    }
    command.push(mode.into());
    command.extend_from_slice(arguments);
    command
}

/// Bounded, noninteractive Docker probes; failed labels retain the Python route.
pub async fn inspect(command: &[OsString]) -> Option<std::process::Output> {
    let child = tokio::process::Command::new(&command[0])
        .args(&command[1..])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .ok()?;
    tokio::time::timeout(std::time::Duration::from_secs(30), child.wait_with_output())
        .await
        .ok()?
        .ok()
}

/// Resolve a running local daemon without ever nesting a container invocation.
pub async fn daemon() -> Option<(OsString, bool)> {
    if std::env::var_os("PSEUDOLIFE_DAEMON_EXEC").is_some_and(|v| !v.is_empty()) {
        return None;
    }
    let docker = std::env::var_os("PSEUDOLIFE_DOCKER")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "docker".into());
    let output = inspect(&container_inspect(&docker)).await?;
    if !output.status.success() || String::from_utf8_lossy(&output.stdout).trim() != "true" {
        return None;
    }
    let rust = inspect(&container_engine_inspect(&docker))
        .await
        .is_some_and(|output| {
            output.status.success() && String::from_utf8_lossy(&output.stdout).trim() == "rust"
        });
    Some((docker, rust))
}

/// Python's SystemExit(subprocess.returncode), including POSIX signals.
pub fn exit_code(status: std::process::ExitStatus) -> i32 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status
            .code()
            .unwrap_or_else(|| (256 - status.signal().unwrap_or(1)) & 255)
    }
    #[cfg(not(unix))]
    {
        status.code().unwrap_or(1)
    }
}

/// Stream an operator's stdio through the selected image, returning its status.
/// Explicit streams disable TTY allocation, as in Python's daemon_exec.
pub async fn run_in_daemon(
    mode: &str,
    arguments: &[OsString],
    stdin: Option<std::process::Stdio>,
    stdout: Option<std::process::Stdio>,
) -> Option<i32> {
    use std::io::{IsTerminal, Write};
    let (docker, rust) = daemon().await?;
    let terminal = stdin.is_none()
        && stdout.is_none()
        && std::io::stdin().is_terminal()
        && std::io::stdout().is_terminal();
    let invocation = container_exec_for_engine(&docker, terminal, mode, arguments, rust);
    crate::stderrln!(
        "pseudolife-mcp {mode}: no bank in this shell; running it inside the pseudolife-mcp-daemon container"
    );
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().flush();
    let status = tokio::process::Command::new(&invocation[0])
        .args(&invocation[1..])
        .stdin(stdin.unwrap_or_else(std::process::Stdio::inherit))
        .stdout(stdout.unwrap_or_else(std::process::Stdio::inherit))
        .stderr(std::process::Stdio::inherit())
        .kill_on_drop(true)
        .status()
        .await
        .ok()?;
    Some(exit_code(status))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn engines_preserve_boundaries_guard_and_terminal() {
        let args = vec!["--".into(), "-id with spaces".into(), "$(literal)".into()];
        for rust in [false, true] {
            for terminal in [false, true] {
                let command = container_exec_for_engine(
                    "docker".as_ref(),
                    terminal,
                    "maintainer",
                    &args,
                    rust,
                );
                assert_eq!(command[2], if terminal { "-it" } else { "-i" });
                assert_eq!(command[4], "PSEUDOLIFE_DAEMON_EXEC=1");
                let index = if rust { 7 } else { 9 };
                assert_eq!(command[index], "maintainer");
                assert_eq!(command[index + 1..], args);
                assert_eq!(command[6], if rust { "pseudolife-mcp" } else { "python" });
            }
        }
    }
}
