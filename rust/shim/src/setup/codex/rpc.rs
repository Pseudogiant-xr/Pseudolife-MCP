use super::SetupError;
use process_wrap::tokio::{ChildWrapper, CommandWrap, KillOnDrop};
use serde_json::{Value, json};
use std::{
    future::Future,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{ChildStdin, ChildStdout},
};

pub trait Rpc {
    fn rpc(
        &mut self,
        method: &str,
        params: Value,
    ) -> impl Future<Output = Result<Value, SetupError>>;
}

/// An owned app-server process; transport output never enters diagnostics.
pub struct Client {
    child: Box<dyn ChildWrapper>,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    counter: u64,
    timeout: Duration,
    events: Vec<Value>,
    event_bytes: usize,
}

const TIMEOUT: SetupError = SetupError("Codex did not respond within the setup timeout.");
const EXITED: SetupError =
    SetupError("Codex exited before setup completed; update Codex and retry.");
const MAX_MESSAGE: u64 = 4 * 1024 * 1024;

impl Client {
    pub async fn open(executable: &Path, home: &Path, cwd: &Path) -> Result<Self, SetupError> {
        Self::open_with_timeout(executable, home, cwd, Duration::from_secs(20)).await
    }

    /// Overrides become process arguments (`-c key=value`). They must contain
    /// configuration switches only; pass credentials through private files or
    /// the explicit child environment, never through this argument map.
    pub async fn open_with_overrides(
        executable: &Path,
        home: &Path,
        cwd: &Path,
        overrides: &Value,
    ) -> Result<Self, SetupError> {
        Self::open_with_environment(executable, home, cwd, overrides, &[]).await
    }

    /// `overrides` has the same non-secret argv contract as `open_with_overrides`.
    pub async fn open_with_environment(
        executable: &Path,
        home: &Path,
        cwd: &Path,
        overrides: &Value,
        environment: &[(String, Option<String>)],
    ) -> Result<Self, SetupError> {
        let mut command = tokio::process::Command::new(executable);
        command
            .args(["app-server", "--stdio"])
            .current_dir(cwd)
            .env("CODEX_HOME", home);
        for (key, value) in environment {
            match value {
                Some(value) => {
                    command.env(key, value);
                }
                None => {
                    command.env_remove(key);
                }
            }
        }
        for (key, value) in overrides
            .as_object()
            .ok_or(SetupError("Codex overrides must be a table."))?
        {
            command.args(["-c", &format!("{key}={}", toml_value(value)?)]);
        }
        Self::spawn(command, Duration::from_secs(20)).await
    }

    pub async fn open_with_timeout(
        executable: &Path,
        home: &Path,
        cwd: &Path,
        timeout: Duration,
    ) -> Result<Self, SetupError> {
        let mut command = tokio::process::Command::new(executable);
        command
            .args(["app-server", "--stdio"])
            .current_dir(cwd)
            .env("CODEX_HOME", home);
        Self::spawn(command, timeout).await
    }

    /// Start an explicit app-server command, including a bundled CLI launcher.
    /// The caller selects its isolated home and working directory.
    pub async fn spawn(
        mut command: tokio::process::Command,
        timeout: Duration,
    ) -> Result<Self, SetupError> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        command.creation_flags(0x0800_0000);
        let mut command = CommandWrap::from(command);
        command.wrap(KillOnDrop);
        #[cfg(windows)]
        command.wrap(process_wrap::tokio::JobObject);
        #[cfg(unix)]
        command.wrap(process_wrap::tokio::ProcessGroup::leader());
        let mut child = command.spawn().map_err(|_| {
            SetupError("Codex CLI could not start; install or update Codex and retry.")
        })?;
        let input = child.stdin().take().ok_or(EXITED)?;
        let output = BufReader::new(child.stdout().take().ok_or(EXITED)?);
        let mut client = Self {
            child,
            input: Some(input),
            output,
            counter: 0,
            timeout,
            events: vec![],
            event_bytes: 0,
        };
        let initialized = client.rpc("initialize", json!({"clientInfo":{"name":"pseudolife_hook_setup","version":"1"},"capabilities":{"experimentalApi":true}})).await;
        if let Err(error) = initialized {
            client.close().await;
            return Err(error);
        }
        if let Err(error) = client.send(&json!({"method":"initialized"})).await {
            client.close().await;
            return Err(error);
        }
        Ok(client)
    }

    async fn send(&mut self, value: &Value) -> Result<(), SetupError> {
        let mut bytes = serde_json::to_vec(value)
            .map_err(|_| SetupError("The Codex setup request is invalid."))?;
        bytes.push(b'\n');
        let input = self.input.as_mut().ok_or(EXITED)?;
        tokio::time::timeout(self.timeout, async {
            input.write_all(&bytes).await?;
            input.flush().await
        })
        .await
        .map_err(|_| TIMEOUT)?
        .map_err(|_| EXITED)
    }

    pub async fn read_config(&mut self, cwd: &Path) -> Result<Value, SetupError> {
        self.rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
            .await
    }

    pub fn events(&self) -> &[Value] {
        &self.events
    }

    pub async fn receive(&mut self, timeout: Duration) -> Result<Value, SetupError> {
        let (value, size) = tokio::time::timeout(timeout, read_message(&mut self.output))
            .await
            .map_err(|_| TIMEOUT)??;
        self.retain_event(&value, size)?;
        Ok(value)
    }

    fn retain_event(&mut self, value: &Value, size: usize) -> Result<(), SetupError> {
        // Each reply already has a per-message bound. Only notifications and
        // server requests belong to the hook event history, not RPC results.
        if value.get("method").is_none() {
            return Ok(());
        }
        if self.event_bytes + size > MAX_MESSAGE as usize {
            return Err(SetupError(
                "Codex setup event history exceeded its limit; retry with fewer active hooks.",
            ));
        }
        self.event_bytes += size;
        self.events.push(value.clone());
        Ok(())
    }

    pub async fn close(&mut self) {
        self.input.take();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
        let exited = loop {
            tokio::select! {
                _=tokio::time::sleep_until(deadline)=>break false,
                _=self.child.wait()=>break true,
                message=read_message(&mut self.output)=>match message {
                    Ok((value,size))=>{
                        if self.retain_event(&value,size).is_err() {break false;}
                    },
                    _=>break tokio::time::timeout_at(deadline,self.child.wait()).await.is_ok(),
                }
            }
        };
        if !exited {
            let _ = self.child.start_kill();
            let _ = tokio::time::timeout(Duration::from_secs(5), self.child.wait()).await;
        }
        // Consume events already buffered at process exit, without waiting on
        // a descendant which may still hold an inherited stdout handle.
        while let Ok(Ok((value, size))) =
            tokio::time::timeout(Duration::from_millis(20), read_message(&mut self.output)).await
        {
            if self.retain_event(&value, size).is_err() {
                break;
            }
        }
    }
}

impl Rpc for Client {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        self.counter += 1;
        let id = self.counter;
        let deadline = tokio::time::Instant::now() + self.timeout;
        self.send(&json!({"id":id,"method":method,"params":params}))
            .await?;
        tokio::time::timeout_at(deadline, async {
            loop {
                let message=self.receive(deadline.saturating_duration_since(tokio::time::Instant::now())).await?;
                if message.get("id") != Some(&json!(id)) { continue; }
                if let Some(error) = message.get("error") {
                    if error.get("code").and_then(Value::as_i64).is_some() {
                        return Err(SetupError::rejected("Codex rejected the setup request; update Codex or review its configuration before retrying."));
                    }
                    return Err(EXITED);
                }
                return message.get("result").cloned().ok_or(EXITED);
            }
        }).await.map_err(|_|TIMEOUT)?
    }
}

async fn read_message(output: &mut BufReader<ChildStdout>) -> Result<(Value, usize), SetupError> {
    loop {
        let mut line = Vec::new();
        let count = (&mut *output)
            .take(MAX_MESSAGE + 1)
            .read_until(b'\n', &mut line)
            .await
            .map_err(|_| EXITED)?;
        if count == 0 {
            return Err(EXITED);
        }
        if count as u64 > MAX_MESSAGE {
            return Err(SetupError(
                "Codex sent an oversized setup response; update Codex and retry.",
            ));
        }
        if let Ok(value) = serde_json::from_slice::<Value>(&line) {
            return Ok((value, count));
        }
    }
}

fn toml_value(value: &Value) -> Result<String, SetupError> {
    match value {
        Value::Object(map) => Ok(format!(
            "{{{}}}",
            map.iter()
                .map(|(key, value)| Ok(format!(
                    "{} = {}",
                    serde_json::to_string(key).expect("string"),
                    toml_value(value)?
                )))
                .collect::<Result<Vec<_>, SetupError>>()?
                .join(", ")
        )),
        Value::Array(values) => Ok(format!(
            "[{}]",
            values
                .iter()
                .map(toml_value)
                .collect::<Result<Vec<_>, _>>()?
                .join(", ")
        )),
        Value::Null => Err(SetupError("A Codex CLI override cannot be null.")),
        _ => Ok(value.to_string()),
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.child.start_kill();
    }
}

pub fn resolve_codex() -> Result<PathBuf, SetupError> {
    if let Some(path) = std::env::var_os("CODEX_CLI_PATH")
        .map(PathBuf::from)
        .filter(|p| p.is_file())
    {
        return Ok(path);
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&paths) {
            #[cfg(windows)]
            let names = std::env::var("PATHEXT")
                .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into())
                .split(';')
                .map(|ext| ext.trim().to_ascii_lowercase())
                .filter(|ext| matches!(ext.as_str(), ".com" | ".exe" | ".bat" | ".cmd"))
                .map(|ext| format!("codex{ext}"))
                .collect::<Vec<_>>();
            #[cfg(not(windows))]
            let names = vec!["codex".to_owned()];
            for name in names {
                let file = directory.join(name);
                if file.is_file() {
                    // std::process handles .cmd/.bat quoting; never construct a
                    // shell command string from configuration or override values.
                    return Ok(file);
                }
            }
        }
    }
    #[cfg(windows)]
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        let root = PathBuf::from(local).join("OpenAI/Codex/bin");
        let mut candidates = std::fs::read_dir(root)
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .map(|entry| entry.path().join("codex.exe"))
            .filter(|p| p.is_file())
            .collect::<Vec<_>>();
        candidates.sort_by_key(|p| p.metadata().ok().and_then(|m| m.modified().ok()));
        if let Some(path) = candidates.pop() {
            return Ok(path);
        }
    }
    Err(SetupError(
        "Codex CLI is unavailable; install or update Codex, then rerun setup.",
    ))
}
