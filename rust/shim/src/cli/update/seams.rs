//! External effects are concentrated here so transaction tests need no host Docker.
use super::{Result, UpdateError};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    future::Future,
    io::Write,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

pub(crate) type Environment = BTreeMap<String, String>;

/// Canonical paths passed to PowerShell and Compose use ordinary Windows
/// drive/UNC spelling; PowerShell's authorization check rejects verbatim paths.
pub(crate) fn command_path(path: PathBuf) -> PathBuf {
    #[cfg(windows)]
    {
        use std::path::{Component, Prefix};
        let mut parts = path.components();
        let Some(Component::Prefix(prefix)) = parts.next() else {
            return path;
        };
        let mut plain = match prefix.kind() {
            Prefix::VerbatimDisk(drive) => PathBuf::from(format!("{}:\\", char::from(drive))),
            Prefix::VerbatimUNC(server, share) => PathBuf::from(r"\\").join(server).join(share),
            _ => return path,
        };
        for part in parts {
            if part != Component::RootDir {
                plain.push(part.as_os_str());
            }
        }
        plain
    }
    #[cfg(not(windows))]
    {
        path
    }
}

pub(crate) fn home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .or_else(dirs::home_dir)
        .unwrap_or_default()
}
pub(crate) fn data_dir() -> PathBuf {
    std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".pseudolife-mcp"))
}
pub(crate) fn block_on<T: Send>(future: impl Future<Output = T> + Send) -> T {
    // CLI entry is synchronous; a separate thread also permits use by the shim's
    // current-thread runtime without nesting runtimes or blocking its reactor.
    std::thread::scope(|scope| {
        scope
            .spawn(move || {
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .expect("update runtime")
                    .block_on(future)
            })
            .join()
            .expect("update worker")
    })
}
pub(crate) fn run_cli(
    argv: &[String],
    env: Option<&Environment>,
    timeout: u64,
    stream: bool,
) -> (i32, String) {
    #[cfg(unix)]
    let _signals = match super::signals::Guard::new() {
        Ok(guard) => guard,
        Err(_) => return (1, "cannot install command cancellation handlers".into()),
    };
    #[cfg(unix)]
    if super::signals::checkpoint() != 0 {
        return (
            128 + super::signals::received(),
            "command interrupted".into(),
        );
    }
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().flush();
    block_on(async {
        let Some(program) = argv.first() else {
            return (1, String::new());
        };
        use process_wrap::tokio::{CommandWrap, KillOnDrop};
        use tokio::io::AsyncReadExt;
        #[cfg(windows)]
        let job = match super::windows_job::Job::new() {
            Ok(job) => job,
            Err(_) => return (1, "cannot contain the requested command".into()),
        };
        let mut command = CommandWrap::with_new(program, |command| {
            command.args(&argv[1..]).stdin(Stdio::null());
            #[cfg(windows)]
            command.creation_flags(windows_sys::Win32::System::Threading::CREATE_SUSPENDED);
            if let Some(env) = env {
                command.env_clear().envs(env);
            }
            if stream {
                command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
            } else {
                command.stdout(Stdio::piped()).stderr(Stdio::piped());
            }
        });
        command.wrap(KillOnDrop);
        #[cfg(unix)]
        command.wrap(process_wrap::tokio::ProcessGroup::leader());
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(e) => {
                return (
                    1,
                    if e.kind() == std::io::ErrorKind::NotFound {
                        "missing executable"
                    } else {
                        "cannot start the requested command"
                    }
                    .into(),
                );
            }
        };
        #[cfg(windows)]
        if child
            .try_inner_child()
            .ok_or_else(|| std::io::Error::other("no native child"))
            .and_then(|child| job.assign(child))
            .is_err()
        {
            let _ = child.start_kill();
            let _ = child.wait().await;
            return (1, "cannot contain the requested command".into());
        }
        let mut stdout = child.stdout().take();
        let mut stderr = child.stderr().take();
        let (mut out, mut err) = (Vec::new(), Vec::new());
        #[cfg(unix)]
        let group = child.id();
        let wait = async {
            let (status, _, _) = tokio::try_join!(
                child.wait(),
                async {
                    if let Some(pipe) = &mut stdout {
                        pipe.read_to_end(&mut out).await?;
                    }
                    Ok::<_, std::io::Error>(())
                },
                async {
                    if let Some(pipe) = &mut stderr {
                        pipe.read_to_end(&mut err).await?;
                    }
                    Ok::<_, std::io::Error>(())
                }
            )?;
            Ok::<_, std::io::Error>(status)
        };
        #[cfg(unix)]
        let result = tokio::select! {
            biased;
            signal = super::signals::wait(group) => {
                // The terminal only signals our foreground group. Forward to
                // the isolated child group, give it a bounded cleanup window,
                // then kill any remaining descendants before releasing the lock.
                super::signals::terminate(&mut *child, signal).await;
                return (128 + signal, "command interrupted".into());
            }
            result = tokio::time::timeout(Duration::from_secs(timeout), wait) => result,
        };
        #[cfg(not(unix))]
        let result = tokio::time::timeout(Duration::from_secs(timeout), wait).await;
        match result {
            Ok(Ok(status)) => {
                #[cfg(windows)]
                if job.complete().is_err() {
                    return (1, "cannot release completed command helpers".into());
                }
                let code = status.code().unwrap_or(1);
                let bytes = if code != 0 && out.is_empty() {
                    &err
                } else {
                    &out
                };
                (code, String::from_utf8_lossy(bytes).replace("\r\n", "\n"))
            }
            // stderr may contain resolved credentials; never concatenate it into
            // Compose JSON or a public error. Stream only deliberate long jobs.
            result => {
                // Kill the owned group/job even if its leader exited while a
                // descendant kept a pipe open. Dropping Tokio Child alone only
                // kills that leader on Unix.
                #[cfg(windows)]
                drop(job);
                let _ = child.start_kill();
                let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
                (
                    1,
                    if result.is_err() {
                        "command timed out"
                    } else {
                        "command failed"
                    }
                    .into(),
                )
            }
        }
    })
}
pub(crate) fn fetch_health_json(url: &str, timeout: u64) -> Option<Value> {
    block_on(async {
        let reply =
            super::super::hook_http::get(url, "", None, Duration::from_secs(timeout), true).await?;
        if !(200..300).contains(&reply.status) {
            return None;
        }
        let result: Value = serde_json::from_slice(&reply.body).ok()?;
        result.is_object().then_some(result)
    })
}
pub(crate) fn fetch_json(url: &str, timeout: u64) -> Option<Value> {
    block_on(async {
        // Use the same environment-aware proxy policy as release downloads.
        // No daemon bearer or board credentials are attached to discovery.
        let client = reqwest::Client::builder()
            .user_agent("pseudolife-mcp")
            .timeout(Duration::from_secs(timeout))
            .build()
            .ok()?;
        let reply = client.get(url).send().await.ok()?.error_for_status().ok()?;
        let result: Value = reply.json().await.ok()?;
        result.is_object().then_some(result)
    })
}
pub(crate) fn authenticated_read(url: &str, token: &str) -> bool {
    block_on(async {
        let authorization = format!("Bearer {token}");
        let Some(reply) = super::super::hook_http::get(
            url.trim_end_matches('/'),
            "/api/facts?limit=1",
            Some(authorization.as_bytes()),
            Duration::from_secs(10),
            false,
        )
        .await
        else {
            return false;
        };
        (200..300).contains(&reply.status)
            && serde_json::from_slice::<Value>(&reply.body)
                .ok()
                .is_some_and(|v| v["entries"].is_array() && !super::truthy(&v["error"]))
    })
}
pub(crate) fn download(url: &str) -> Result<Option<Vec<u8>>> {
    block_on(async {
        let client = reqwest::Client::builder()
            .user_agent("pseudolife-mcp")
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|_| UpdateError::new("cannot initialize release download"))?;
        let response = client.get(url).send().await
            .map_err(|_| UpdateError::usage("cannot download the release asset; check the published version and retry; nothing was deployed"))?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let response = response.error_for_status()
            .map_err(|_| UpdateError::usage("cannot download the release asset; check the published version and retry; nothing was deployed"))?;
        if response
            .content_length()
            .is_some_and(|n| n > 16 * 1024 * 1024)
        {
            return Err(UpdateError::usage(
                "the Compose release archive exceeds its size limit; nothing was deployed",
            ));
        }
        use futures::StreamExt;
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| UpdateError::new("release download was interrupted"))?;
            if bytes.len() + chunk.len() > 16 * 1024 * 1024 {
                return Err(UpdateError::new("release asset exceeds its size limit"));
            }
            bytes.extend_from_slice(&chunk);
        }
        Ok(Some(bytes))
    })
}
pub(crate) fn env_file_sets_credentials(files: &[PathBuf]) -> bool {
    files
        .iter()
        .filter_map(|p| std::fs::read_to_string(p).ok())
        .any(|s| {
            s.lines().any(|line| {
                let line = line.trim().strip_prefix("export ").unwrap_or(line.trim());
                line.split_once('=').is_some_and(|(key, _)| {
                    matches!(key.trim(), "PSEUDOLIFE_MCP_TOKEN" | "PSEUDOLIFE_MCP_TOKENS")
                })
            })
        })
}
pub(crate) fn compose_environment(files: &[PathBuf], extra: &Environment) -> Environment {
    let mut env: Environment = std::env::vars().collect();
    if env_file_sets_credentials(files) {
        env.retain(|k, _| {
            !matches!(
                k.to_ascii_uppercase().as_str(),
                "PSEUDOLIFE_MCP_TOKEN" | "PSEUDOLIFE_MCP_TOKENS"
            )
        });
    }
    env.extend(extra.clone());
    env
}
pub(crate) fn compose_args(files: &[PathBuf], env_files: &[PathBuf]) -> Vec<String> {
    let mut args = Vec::new();
    for (flag, paths) in [("--env-file", env_files), ("-f", files)] {
        for p in paths.iter().filter(|p| p.is_file()) {
            args.extend([flag.into(), p.to_string_lossy().into_owned()]);
        }
    }
    args
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Tree {
    pub sha: String,
    pub dirty: String,
    pub lines: Vec<String>,
}
pub(crate) fn tree_state(repo: &Path) -> Tree {
    let prefix = vec![
        "git".into(),
        "-C".into(),
        repo.to_string_lossy().into_owned(),
    ];
    let call = |args: &[&str]| {
        let mut command = prefix.clone();
        command.extend(args.iter().map(|s| s.to_string()));
        run_cli(&command, None, 120, false)
    };
    let (code, sha) = call(&["rev-parse", "HEAD"]);
    let (status, lines) = call(&["status", "--porcelain", "--untracked-files=normal"]);
    if code != 0 || status != 0 {
        return Tree {
            sha: "unknown".into(),
            dirty: "unknown".into(),
            lines: vec![],
        };
    }
    let lines: Vec<_> = lines
        .lines()
        .filter(|s| !s.trim().is_empty())
        .map(str::to_owned)
        .collect();
    Tree {
        sha: sha.trim().into(),
        dirty: if lines.is_empty() { "false" } else { "true" }.into(),
        lines,
    }
}
pub(crate) fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    // Reuse the private-state writer: randomized create-new temporary, private
    // Windows owner/ACL, same-directory atomic replace and durable file sync.
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    crate::board::state::atomic_write(path, bytes, None).map_err(|_| {
        UpdateError::new("cannot save update state; restore free disk space and retry")
    })
}
pub(crate) fn stamp() -> String {
    chrono::Local::now().format("%Y%m%d-%H%M%S").to_string()
}
pub(crate) fn quote(s: &str) -> String {
    if cfg!(windows) {
        format!("'{}'", s.replace('\'', "''"))
    } else {
        format!("'{}'", s.replace('\'', "'\"'\"'"))
    }
}
pub(crate) fn display(args: &[String]) -> String {
    args.iter().map(|s| quote(s)).collect::<Vec<_>>().join(" ")
}
