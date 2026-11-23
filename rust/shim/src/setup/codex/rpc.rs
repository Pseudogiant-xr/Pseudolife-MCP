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
    /// The app-server's exit status, once `close` has seen it exit.
    status: Option<std::process::ExitStatus>,
    /// The command started (program and arguments), as the recovery names it.
    started: String,
}

const TIMEOUT: SetupError = SetupError("Codex did not respond within the setup timeout.");
// The client never reads the app-server's stderr (its error output can quote
// config.toml lines, a literal token included), so this names no cause: the
// next step is to run the app-server command where its own error is shown.
// Once a `Client` exists, `Client::exited` replaces this with the exact
// command it started and, when known, the exit status; this text is left
// only for an exit before then. Running bare `codex` would start its
// terminal UI, which shows neither an app-server failure nor an old Codex
// that lacks the subcommand.
const EXITED: SetupError = SetupError(
    "Codex exited before setup completed. To see why, run `codex app-server --stdio` in this terminal: it prints its error and exits (or, if it is healthy, waits for input; stop it then). If it reports an unknown command or option, update Codex; otherwise fix what it reports, then retry.",
);
// A reply the client cannot read means a protocol it does not speak, which
// is evidence of a version mismatch.
const UNREADABLE: SetupError =
    SetupError("Codex sent a setup reply this client does not understand; update Codex and retry.");
const MAX_MESSAGE: u64 = 4 * 1024 * 1024;

/// Make sure the Codex home exists before the app-server is started with
/// `CODEX_HOME` naming it. Codex refuses a `CODEX_HOME` that does not exist
/// (0.171.0: "CODEX_HOME points to ..., but that path does not exist"), and
/// a Codex installed but never started has no home folder yet (#1897).
/// The folder is created as Codex creates its own default home: an
/// ordinary folder, its mode from the umask on Unix and its access
/// inherited from the parent folder on Windows. A home the user named in
/// `CODEX_HOME` is created the same way, parents included.
fn ensure_home(home: &Path, named_by_variable: bool) -> Result<(), SetupError> {
    use std::io::ErrorKind;
    let shown = home.display();
    let origin = if named_by_variable {
        format!("CODEX_HOME names {shown}, which")
    } else {
        format!("The Codex home is {shown}, which")
    };
    let in_the_way = "move it aside or set CODEX_HOME to another folder, then retry.";
    match std::fs::metadata(home) {
        Ok(metadata) if metadata.is_dir() => return Ok(()),
        Ok(_) => {
            return Err(SetupError::detail(format!(
                "{origin} is not a folder; {in_the_way}"
            )));
        }
        // A file where a parent folder should be is NotADirectory on Unix;
        // creating the folder below then fails and says so.
        Err(error) if matches!(error.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {}
        Err(error) => {
            return Err(SetupError::detail(format!(
                "{origin} cannot be inspected ({}); check that path and its permissions, then retry.",
                error.kind()
            )));
        }
    }
    std::fs::create_dir_all(home).map_err(|error| {
        SetupError::detail(match error.kind() {
            // A file (or a link to nothing) holds a name on the path: on
            // Unix a parent that is a file is NotADirectory, on Windows the
            // name already taken is AlreadyExists.
            ErrorKind::NotADirectory | ErrorKind::AlreadyExists => format!(
                "{origin} could not be created: something that is not a folder is in the way on that path; {in_the_way}"
            ),
            ErrorKind::PermissionDenied => format!(
                "{origin} could not be created: this account may not create it; create it for this account, or set CODEX_HOME to a folder this account owns, then retry."
            ),
            kind => format!(
                "{origin} could not be created ({kind}); create that folder, then retry."
            ),
        })
    })
}

/// Whether `CODEX_HOME` names the home (Codex treats an empty value as unset).
fn home_named_by_variable() -> bool {
    std::env::var_os("CODEX_HOME").is_some_and(|value| !value.is_empty())
}

/// `codex app-server --stdio` for `home`, which exists once this returns.
fn app_server(
    executable: &Path,
    home: &Path,
    cwd: &Path,
) -> Result<tokio::process::Command, SetupError> {
    ensure_home(home, home_named_by_variable())?;
    let mut command = tokio::process::Command::new(executable);
    command
        .args(["app-server", "--stdio"])
        .current_dir(cwd)
        .env("CODEX_HOME", home);
    Ok(command)
}

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
        let mut command = app_server(executable, home, cwd)?;
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
        Self::spawn(app_server(executable, home, cwd)?, timeout).await
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
        let started = std::iter::once(command.as_std().get_program())
            .chain(command.as_std().get_args())
            .map(|part| part.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ");
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
            status: None,
            started,
        };
        let initialized = client.call("initialize", json!({"clientInfo":{"name":"pseudolife_hook_setup","version":"1"},"capabilities":{"experimentalApi":true}})).await;
        if let Err(error) = initialized {
            client.close().await;
            return Err(client.exited_early(error));
        }
        if let Err(error) = client.send(&json!({"method":"initialized"})).await {
            client.close().await;
            return Err(client.exited_early(error));
        }
        Ok(client)
    }

    /// An app-server that exited during the handshake: its exit status is
    /// known once `close` has waited for it, so the recovery names it.
    fn exited_early(&self, error: SetupError) -> SetupError {
        self.named(error)
    }

    /// `EXITED`, naming the command this client started (which may not be
    /// the `codex` on PATH) and the exit status when `close` has seen it.
    fn named(&self, error: SetupError) -> SetupError {
        if error != EXITED {
            return error;
        }
        let status = match self.status {
            Some(status) => match status.code() {
                Some(code) => format!(" (exit code {code})"),
                None => format!(" ({status})"),
            },
            None => String::new(),
        };
        SetupError::detail(format!(
            "Codex exited before setup completed{status}. To see why, run the command setup started, `{}`, in this terminal: it prints its error and exits (or, if it is healthy, waits for input; stop it then). If it reports an unknown command or option, update Codex; otherwise fix what it reports, then retry.",
            self.started
        ))
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
        let result = self.receive_raw(timeout).await;
        result.map_err(|error| self.named(error))
    }

    async fn receive_raw(&mut self, timeout: Duration) -> Result<Value, SetupError> {
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
                status=self.child.wait()=>{
                    self.status = status.ok();
                    break true;
                },
                message=read_message(&mut self.output)=>match message {
                    Ok((value,size))=>{
                        if self.retain_event(&value,size).is_err() {break false;}
                    },
                    _=>match tokio::time::timeout_at(deadline,self.child.wait()).await {
                        Ok(status)=>{
                            self.status = status.ok();
                            break true;
                        },
                        Err(_)=>break false,
                    },
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
        let result = self.call(method, params).await;
        result.map_err(|error| self.named(error))
    }
}

impl Client {
    /// One request and its correlated reply, with `EXITED` left unnamed so
    /// the handshake can add the exit status after `close`.
    async fn call(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        self.counter += 1;
        let id = self.counter;
        let deadline = tokio::time::Instant::now() + self.timeout;
        self.send(&json!({"id":id,"method":method,"params":params}))
            .await?;
        tokio::time::timeout_at(deadline, async {
            loop {
                let message=self.receive_raw(deadline.saturating_duration_since(tokio::time::Instant::now())).await?;
                if message.get("id") != Some(&json!(id)) { continue; }
                if let Some(error) = message.get("error") {
                    if error.get("code").and_then(Value::as_i64).is_some() {
                        return Err(SetupError::rejected("Codex rejected the setup request; update Codex or review its configuration before retrying."));
                    }
                    return Err(UNREADABLE);
                }
                return message.get("result").cloned().ok_or(UNREADABLE);
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
    // `codex_connection.resolve_codex`: `program_lookup.find_program("codex")`,
    // only an absolute PATH directory, never the current directory.
    // std::process handles .cmd/.bat quoting; never construct a shell command
    // string from configuration or override values.
    if let Some(found) = crate::cli::program_lookup::find_program_path("codex") {
        return Ok(found);
    }
    #[cfg(windows)]
    if let Some(local) = std::env::var_os("LOCALAPPDATA")
        .and_then(|value| value.into_string().ok())
        .map(|value| value.trim().trim_matches('"').to_owned())
        // Only an absolute LOCALAPPDATA: an unset or relative one would
        // search the current directory.
        .filter(|value| crate::cli::program_lookup::absolute_dir(value))
    {
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

#[cfg(test)]
mod tests {
    use super::ensure_home;
    use std::path::PathBuf;

    struct Scratch(PathBuf);
    impl Scratch {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("w95-codex-home-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn a_missing_home_is_created_with_its_parents_and_an_existing_one_is_kept() {
        let scratch = Scratch::new();
        let home = scratch.0.join("profile").join(".codex");
        ensure_home(&home, false).unwrap();
        assert!(home.is_dir());
        std::fs::write(home.join("config.toml"), "# kept\n").unwrap();
        ensure_home(&home, true).unwrap();
        assert_eq!(
            std::fs::read(home.join("config.toml")).unwrap(),
            b"# kept\n"
        );
    }

    #[test]
    fn a_file_at_the_home_path_is_refused_naming_where_it_came_from() {
        let scratch = Scratch::new();
        let home = scratch.0.join(".codex");
        std::fs::write(&home, "not a folder").unwrap();
        let default = ensure_home(&home, false).unwrap_err().to_string();
        assert!(
            default.starts_with(&format!("The Codex home is {}, which", home.display()))
                && default.contains("not a folder"),
            "{default}"
        );
        let named = ensure_home(&home, true).unwrap_err().to_string();
        assert!(
            named.starts_with(&format!("CODEX_HOME names {}", home.display())),
            "{named}"
        );
        assert_eq!(std::fs::read(&home).unwrap(), b"not a folder");
    }

    #[test]
    fn a_home_that_cannot_be_created_is_refused_and_nothing_is_left() {
        let scratch = Scratch::new();
        // A file where a parent folder would have to be.
        std::fs::write(scratch.0.join("blocker"), "").unwrap();
        let home = scratch.0.join("blocker").join("codex");
        let error = ensure_home(&home, true).unwrap_err().to_string();
        assert!(
            error.starts_with(&format!("CODEX_HOME names {}", home.display()))
                && error.contains("could not be created")
                && error.contains("in the way"),
            "{error}"
        );
        assert!(scratch.0.join("blocker").is_file());
    }

    #[cfg(unix)]
    #[test]
    fn a_home_that_cannot_be_inspected_is_refused_without_creating_anything() {
        let scratch = Scratch::new();
        // A link to itself: inspecting it fails with a loop, not NotFound.
        let home = scratch.0.join("loop");
        std::os::unix::fs::symlink(&home, &home).unwrap();
        let error = ensure_home(&home, false).unwrap_err().to_string();
        assert!(
            error.starts_with(&format!("The Codex home is {}, which", home.display()))
                && error.contains("cannot be inspected"),
            "{error}"
        );
        assert!(
            std::fs::symlink_metadata(&home)
                .unwrap()
                .file_type()
                .is_symlink()
        );
    }
}
