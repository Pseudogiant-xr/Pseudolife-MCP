//! Bounded subprocess and HTTP seams for guided setup. No Python runtime.
use super::{Error, Result};
use serde_json::Value;
use std::{ffi::OsString, path::PathBuf, process::Stdio, time::Duration};

pub(super) const CONTAINER: &str = "pseudolife-mcp-daemon";
#[derive(Clone, Debug)]
pub(super) enum Tier {
    Local,
    Docker(OsString, String),
}

pub(super) fn which(name: &str) -> Option<PathBuf> {
    let paths = std::env::var_os("PATH")?;
    let suffixes = if cfg!(windows) {
        vec!["", ".exe", ".cmd", ".bat"]
    } else {
        vec![""]
    };
    for dir in std::env::split_paths(&paths) {
        for suffix in &suffixes {
            let path = dir.join(format!("{name}{suffix}"));
            if path.is_file() {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    if std::fs::metadata(&path).ok()?.permissions().mode() & 0o111 == 0 {
                        continue;
                    }
                }
                return Some(path);
            }
        }
    }
    None
}
pub(super) fn tailscale() -> Option<PathBuf> {
    which("tailscale").or_else(|| {
        if cfg!(windows) {
            std::env::var_os("ProgramW6432")
                .or_else(|| std::env::var_os("ProgramFiles"))
                .map(PathBuf::from)
                .map(|p| p.join("Tailscale").join("tailscale.exe"))
                .filter(|p| p.is_file())
        } else if cfg!(target_os = "macos") {
            let path = PathBuf::from("/Applications/Tailscale.app/Contents/MacOS/Tailscale");
            path.is_file().then_some(path)
        } else {
            None
        }
    })
}

pub(super) struct Output {
    pub code: i32,
    pub out: String,
    pub err: String,
}
impl Output {
    pub fn detail(&self) -> String {
        self.err
            .lines()
            .find(|v| !v.trim().is_empty())
            .map(|v| v.trim().into())
            .unwrap_or_else(|| format!("exit {}", self.code))
    }
}
pub(super) async fn run(command: &[OsString], seconds: u64) -> Output {
    use tokio::io::AsyncReadExt;
    let mut child = tokio::process::Command::new(&command[0]);
    child
        .args(&command[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let Ok(mut child) = child.spawn() else {
        return Output {
            code: 127,
            out: String::new(),
            err: "could not start the requested program".into(),
        };
    };
    type Bytes = std::sync::Arc<std::sync::Mutex<Vec<u8>>>;
    fn read(
        mut source: impl tokio::io::AsyncRead + Unpin + Send + 'static,
        bytes: Bytes,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let mut chunk = [0u8; 8192];
            while let Ok(count) = source.read(&mut chunk).await {
                if count == 0 {
                    break;
                }
                bytes
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .extend_from_slice(&chunk[..count]);
            }
        })
    }
    let out = Bytes::default();
    let err = Bytes::default();
    let mut stdout = read(child.stdout.take().unwrap(), out.clone());
    let mut stderr = read(child.stderr.take().unwrap(), err.clone());
    let result = tokio::time::timeout(Duration::from_secs(seconds), async {
        let status = child.wait().await;
        let _ = (&mut stdout).await;
        let _ = (&mut stderr).await;
        status
    })
    .await;
    let code = match result {
        Ok(Ok(status)) => status.code().unwrap_or(1),
        Ok(Err(_)) => 1,
        Err(_) => {
            // Preserve output already read, notably the tailnet admin enable
            // link printed before tailscale waits. Stop only this child.
            let _ = child.start_kill();
            let _ = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
            tokio::task::yield_now().await;
            124
        }
    };
    stdout.abort();
    stderr.abort();
    let decode = |bytes: Bytes| {
        String::from_utf8_lossy(
            &bytes
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        )
        .replace("\r\n", "\n")
        .replace('\r', "\n")
    };
    Output {
        code,
        out: decode(out),
        err: if code == 124 {
            format!("timed out after {seconds}s")
        } else {
            decode(err)
        },
    }
}

pub(super) async fn tier(selected: Option<&str>) -> Result<Tier> {
    let container = selected.unwrap_or(CONTAINER);
    let scoped_refusal = || {
        Error::refused(format!(
            "cannot read running container {container}; use docker ps to find the daemon, then run pseudolife-mcp maintainer setup --container <name>"
        ))
    };
    // The in-image helpers never inspect or recurse into Docker.
    if selected.is_none()
        && std::env::var_os("PSEUDOLIFE_DAEMON_EXEC").is_some_and(|v| !v.is_empty())
    {
        return Ok(Tier::Local);
    }
    let docker = std::env::var_os("PSEUDOLIFE_DOCKER")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "docker".into());
    if docker == "docker" && which("docker").is_none() {
        if selected.is_some() {
            return Err(scoped_refusal());
        }
        return Ok(Tier::Local);
    }
    let output = run(&scoped_inspect(&docker, container, false), 60).await;
    if output.code == 0 {
        if output.out.trim() != "true" {
            if selected.is_some() {
                return Err(scoped_refusal());
            }
            return Err(Error::refused(format!(
                "the {CONTAINER} container is not running: start it first"
            )));
        }
        return Ok(Tier::Docker(docker, container.into()));
    }
    if selected.is_some() {
        return Err(scoped_refusal());
    }
    if format!("{}{}", output.out, output.err)
        .to_lowercase()
        .contains("no such")
    {
        return Ok(Tier::Local);
    }
    Err(Error::refused(format!(
        "docker did not answer ({}), so whether the daemon is the {CONTAINER} container cannot be told: start Docker (or run this as a user who can use it) and run this again",
        output.detail()
    )))
}
fn scoped_inspect(docker: &std::ffi::OsStr, container: &str, engine: bool) -> Vec<OsString> {
    let mut command = if engine {
        crate::pg::resolution::container_engine_inspect(docker)
    } else {
        crate::pg::resolution::container_inspect(docker)
    };
    *command.last_mut().expect("container inspect argument") = container.into();
    command
}

pub(super) fn command(tier: &Tier, args: &[&str]) -> Result<Vec<OsString>> {
    // Both image engines expose the same installed CLI. Setup's Rust path
    // never falls back to a Python interpreter, including on the host.
    let mut command = match tier {
        Tier::Local => vec![
            std::env::current_exe()
                .map_err(|_| Error::refused("cannot locate this CLI executable"))?
                .into_os_string(),
        ],
        Tier::Docker(docker, container) => vec![
            docker.clone(),
            "exec".into(),
            "-e".into(),
            "PSEUDOLIFE_DAEMON_EXEC=1".into(),
            container.into(),
            "pseudolife-mcp".into(),
        ],
    };
    command.extend(args.iter().map(OsString::from));
    Ok(command)
}

pub(super) async fn host(tier: &Tier, args: &[&str]) -> Result<Value> {
    if matches!(tier, Tier::Local) {
        return super::host(args).await.map_err(|error| {
            let reading = args[0] == "--host-state";
            Error::new(
                format!(
                    "{} the daemon's config failed: {}{error}",
                    if reading { "reading" } else { "writing" },
                    if reading { "" } else { "refused: " }
                ),
                if reading { 4 } else { 5 },
            )
        });
    }
    let Tier::Docker(docker, container) = tier else {
        unreachable!()
    };
    let engine = run(&scoped_inspect(docker, container, true), 60).await;
    let invocation = if engine.code == 0 && engine.out.trim() == "rust" {
        command(tier, &[&["maintainer", "setup"][..], args].concat())?
    } else {
        // Older and explicitly Python images expose the internal Python
        // helper, whose subcommands predate the native --host-* flags.
        let mut command = vec![
            docker.clone(),
            "exec".into(),
            container.into(),
            "python".into(),
            "-m".into(),
            "pseudolife_memory.maintainer_setup".into(),
            args[0].trim_start_matches("--host-").into(),
        ];
        command.extend(args[1..].iter().map(OsString::from));
        command
    };
    let output = run(&invocation, 120).await;
    if output.code != 0 {
        let message = if args[0] == "--host-state" {
            "reading"
        } else {
            "writing"
        };
        return Err(Error::new(
            format!("{message} the daemon's config failed: {}", output.detail()),
            if args[0] == "--host-state" { 4 } else { 5 },
        ));
    }
    let value: Value =
        serde_json::from_str(output.out.lines().last().unwrap_or("")).map_err(|_| {
            Error::refused("the daemon's environment answered something that is not JSON")
        })?;
    if !value.is_object() {
        return Err(Error::refused(
            "the daemon's environment answered an unexpected shape",
        ));
    }
    Ok(value)
}

pub(super) async fn get(url: &str, path: &str, token: Option<&str>) -> Option<(u16, Value)> {
    let authorization = token.map(|v| format!("Bearer {v}"));
    let reply = crate::cli::hook_http::get(
        url,
        path,
        authorization.as_deref().map(str::as_bytes),
        Duration::from_secs(if path == "/health" { 3 } else { 2 }),
        false,
    )
    .await?;
    Some((reply.status, serde_json::from_slice(&reply.body).ok()?))
}
pub(super) async fn health(port: u16) -> Option<Value> {
    get(&format!("http://127.0.0.1:{port}"), "/health", None)
        .await
        .filter(|(status, _)| !(300..400).contains(status))
        .map(|(_, v)| v)
        .filter(Value::is_object)
}
pub(super) async fn running_name(port: u16) -> Option<(String, String)> {
    let provider = crate::credentials::CredentialProvider::from_environment().ok()?;
    let snapshot = provider.snapshot().ok()?;
    let token = snapshot.token()?;
    let (status, value) = get(
        &format!("http://127.0.0.1:{port}"),
        "/api/maintainer",
        Some(token),
    )
    .await?;
    if status == 200 {
        return Some((
            value["rp_id"].as_str().unwrap_or("").into(),
            value["origin"].as_str().unwrap_or("").into(),
        ));
    }
    if status == 409 && value["error"] == "maintainer_https_required" {
        return Some((String::new(), String::new()));
    }
    None
}

pub(super) fn setup_command(tier: &Tier) -> String {
    super::setup_command(match tier {
        Tier::Docker(_, container) => Some(container),
        Tier::Local => None,
    })
}

pub(super) async fn restart(tier: &Tier, port: u16) -> Result<()> {
    let resume = setup_command(tier);
    let Tier::Docker(docker, container) = tier else {
        return Ok(());
    };
    let output = run(&[docker.clone(), "restart".into(), container.into()], 300).await;
    if output.code != 0 {
        return Err(Error::new(
            format!(
                "the daemon restart failed ({}); the config is written: run `docker restart {container}`, then `{resume}` again",
                output.detail()
            ),
            5,
        ));
    }
    println!("restarted the daemon; waiting for it to answer...");
    // Matches the oracle's design bound: a cold embedder loads before health.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(180);
    loop {
        if health(port).await.is_some_and(|v| v["status"] == "ok") {
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(Error::new(
                format!(
                    "the daemon did not report healthy within 180 s of its restart: check `docker logs {container}`, then run `{resume}` again"
                ),
                5,
            ));
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timeout_output_fixture() {
        let name = "cli::maintainer::setup::system::tests::timeout_output_fixture";
        let args: Vec<_> = std::env::args().collect();
        if !args.windows(2).any(|v| v[0] == "--exact" && v[1] == name) {
            return;
        }
        println!("https://login.tailscale.com/fixture-enable");
        std::thread::sleep(Duration::from_secs(60));
    }
    #[tokio::test]
    async fn timeout_preserves_the_enable_link_and_reaps_its_child() {
        let command = vec![
            std::env::current_exe().unwrap().into_os_string(),
            "--exact".into(),
            "cli::maintainer::setup::system::tests::timeout_output_fixture".into(),
            "--nocapture".into(),
        ];
        let output = run(&command, 2).await;
        assert_eq!(output.code, 124);
        assert!(
            output
                .out
                .contains("https://login.tailscale.com/fixture-enable")
        );
        assert_eq!(output.err, "timed out after 2s");
    }
}
