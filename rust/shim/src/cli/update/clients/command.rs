use serde_json::Value;
use std::{path::Path, process::Stdio, time::Duration};

pub struct Output {
    pub code: i32,
    pub bytes: Vec<u8>,
}
impl Output {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.bytes).into_owned()
    }
    pub fn json(&self) -> Option<Value> {
        let text = std::str::from_utf8(&self.bytes).ok()?;
        for (offset, _) in text.match_indices('{') {
            let mut stream =
                serde_json::Deserializer::from_str(&text[offset..]).into_iter::<Value>();
            if let Some(Ok(value)) = stream.next() {
                if value.is_object() {
                    return Some(value);
                }
            }
        }
        None
    }
}
pub trait Runner {
    fn run(
        &self,
        executable: &Path,
        args: &[&str],
        cwd: Option<&Path>,
        timeout: Duration,
        env: &[(&str, &str)],
    ) -> Output;
}
pub struct NativeRunner;
impl Runner for NativeRunner {
    fn run(
        &self,
        executable: &Path,
        args: &[&str],
        cwd: Option<&Path>,
        timeout: Duration,
        env: &[(&str, &str)],
    ) -> Output {
        let executable = executable.to_owned();
        let args: Vec<_> = args.iter().map(|s| s.to_string()).collect();
        let cwd = cwd.map(Path::to_owned);
        let env: Vec<_> = env
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        // Synchronous update dispatch can itself run under a Tokio runtime.
        // The dedicated thread owns the subprocess and cancellation cleanup.
        std::thread::spawn(move || {
            let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            else {
                return Output {
                    code: 1,
                    bytes: vec![],
                };
            };
            runtime.block_on(async move {
                use process_wrap::tokio::{CommandWrap, KillOnDrop};
                use tokio::io::AsyncReadExt;
                let mut command = CommandWrap::with_new(executable, |c| {
                    c.args(args)
                        .stdin(Stdio::null())
                        .stdout(Stdio::piped())
                        .stderr(Stdio::piped());
                    if let Some(cwd) = cwd {
                        c.current_dir(cwd);
                    }
                    c.env("GIT_TERMINAL_PROMPT", "0")
                        .env("GCM_INTERACTIVE", "never")
                        .env("GIT_SSH_COMMAND", "ssh -o BatchMode=yes");
                    for (key, _) in std::env::vars_os() {
                        if key
                            .to_string_lossy()
                            .to_ascii_uppercase()
                            .starts_with("PYTHON")
                        {
                            c.env_remove(key);
                        }
                    }
                    for (key, value) in env {
                        c.env(key, value);
                    }
                });
                command.wrap(KillOnDrop);
                #[cfg(windows)]
                command.wrap(process_wrap::tokio::JobObject);
                #[cfg(unix)]
                command.wrap(process_wrap::tokio::ProcessGroup::leader());
                let Ok(mut child) = command.spawn() else {
                    return Output {
                        code: 1,
                        bytes: vec![],
                    };
                };
                let stdout = child.stdout().take();
                let stderr = child.stderr().take();
                let read = |pipe: Option<tokio::process::ChildStdout>| async move {
                    let mut bytes = Vec::new();
                    if let Some(pipe) = pipe {
                        pipe.take(8 * 1024 * 1024 + 1)
                            .read_to_end(&mut bytes)
                            .await
                            .ok()?;
                    }
                    Some(bytes)
                };
                let out = tokio::spawn(read(stdout));
                let err = tokio::spawn(async move {
                    let mut bytes = Vec::new();
                    if let Some(pipe) = stderr {
                        if pipe
                            .take(8 * 1024 * 1024 + 1)
                            .read_to_end(&mut bytes)
                            .await
                            .is_err()
                        {
                            return None;
                        }
                    }
                    Some(bytes)
                });
                let wait = async {
                    let status = child.wait().await.ok()?;
                    let mut bytes = out.await.ok()??;
                    let stderr = err.await.ok()??;
                    if bytes.len() > 8 * 1024 * 1024 || stderr.len() > 8 * 1024 * 1024 {
                        return None;
                    }
                    bytes.extend(stderr);
                    Some(Output {
                        code: status.code().unwrap_or(1),
                        bytes,
                    })
                };
                match tokio::time::timeout(timeout, wait).await {
                    Ok(Some(output)) => output,
                    Ok(None) => Output {
                        code: 1,
                        bytes: vec![],
                    },
                    Err(_) => {
                        let _ = child.start_kill();
                        let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
                        Output {
                            code: 124,
                            bytes: vec![],
                        }
                    }
                }
            })
        })
        .join()
        .unwrap_or(Output {
            code: 1,
            bytes: vec![],
        })
    }
}
