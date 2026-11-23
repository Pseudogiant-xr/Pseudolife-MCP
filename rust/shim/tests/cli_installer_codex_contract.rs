//! `pseudolife-mcp installer codex-credentials`, `codex-runtime-defaults`
//! and `codex-hooks`: the real binary in a throwaway home, talking to a fake
//! `codex app-server` (this test binary, started through `CODEX_CLI_PATH`).
//! Never the real home, never a real Codex configuration.
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, Write},
    path::PathBuf,
    process::{Command, Output, Stdio},
};

const STATE: &str = "W7_FAKE_CODEX_STATE";
/// When set, the fake app-server exits at once with this status.
const EXIT: &str = "W95_FAKE_CODEX_EXIT";
const SERVER: &str = "pseudolife-memory";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("w7-installer-codex-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(path.join("codex")).unwrap();
        let path = fs::canonicalize(&path).unwrap();
        let path = PathBuf::from(
            path.to_string_lossy()
                .strip_prefix(r"\\?\")
                .map(str::to_owned)
                .unwrap_or_else(|| path.to_string_lossy().into_owned()),
        );
        fs::write(path.join("codex/config.toml"), "# fixture\n").unwrap();
        Self(path)
    }
    fn codex(&self) -> PathBuf {
        self.0.join("codex")
    }
    /// The fake app-server's state: the user layer and its version.
    fn seed(&self, user: Value) {
        let state = json!({
            "user": user, "overlay": {}, "version": 1,
            "file": self.codex().join("config.toml"),
        });
        fs::write(self.0.join("fake-state.json"), state.to_string()).unwrap();
    }
    fn state(&self) -> Value {
        serde_json::from_slice(&fs::read(self.0.join("fake-state.json")).unwrap()).unwrap()
    }
    fn launcher(&self) -> PathBuf {
        let exe = std::env::current_exe().unwrap();
        if cfg!(windows) {
            let path = self.0.join("codex-fake.cmd");
            fs::write(
                &path,
                format!(
                    "@\"{}\" --exact fake_codex_app_server --nocapture --test-threads 1\r\n",
                    exe.display()
                ),
            )
            .unwrap();
            path
        } else {
            let path = self.0.join("codex-fake");
            fs::write(
                &path,
                format!(
                    "#!/bin/sh\nexec '{}' --exact fake_codex_app_server --nocapture --test-threads 1\n",
                    exe.display()
                ),
            )
            .unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
            }
            path
        }
    }
    fn run(&self, args: &[&str], stdin: Option<&[u8]>) -> Output {
        self.run_with(args, stdin, &[])
    }
    fn run_with(&self, args: &[&str], stdin: Option<&[u8]>, extra: &[(&str, &str)]) -> Output {
        self.run_env(args, stdin, extra, &[])
    }
    /// `run_with`, with the variables in `removed` unset in the binary's
    /// environment (and so in the fake app-server's).
    fn run_env(
        &self,
        args: &[&str],
        stdin: Option<&[u8]>,
        extra: &[(&str, &str)],
        removed: &[&str],
    ) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.env_clear();
        for key in ["SystemRoot", "WINDIR", "TEMP", "TMP", "PATHEXT", "ComSpec"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        command
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("LOCALAPPDATA", self.0.join("Local"))
            .env("APPDATA", self.0.join("Roaming"))
            .env("CODEX_HOME", self.codex())
            .env("CODEX_CLI_PATH", self.launcher())
            .env(STATE, self.0.join("fake-state.json"))
            .env("PATH", self.0.join("empty-path"))
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9")
            .envs(extra.iter().copied());
        for key in removed {
            command.env_remove(key);
        }
        command
            .arg("installer")
            .args(args)
            .current_dir(&self.0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn().unwrap();
        let mut input = child.stdin.take().unwrap();
        if let Some(bytes) = stdin {
            input.write_all(bytes).unwrap();
        }
        drop(input);
        child.wait_with_output().unwrap()
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn report(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| {
        panic!(
            "not one JSON report: {}\nstderr: {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

fn registered(env: Value) -> Value {
    json!({"mcp_servers": {SERVER: {"command": "/opt/pseudolife-mcp", "args": [], "env": env}}})
}

#[test]
fn runtime_defaults_configure_then_preserve_then_refuse_unregistered() {
    let home = Home::new();
    home.seed(registered(json!({})));
    let first = home.run(&["codex-runtime-defaults"], None);
    let body = report(&first);
    assert_eq!(first.status.code(), Some(0), "{body}");
    assert_eq!(body["status"], "ready");
    assert_eq!(body["runtime_defaults"], "configured");
    let server = &home.state()["user"]["mcp_servers"][SERVER];
    assert_eq!(server["startup_timeout_sec"], json!(240.0));
    assert_eq!(server["tool_timeout_sec"], json!(240.0));
    assert_eq!(server["required"], json!(true));
    let again = home.run(&["codex-runtime-defaults"], None);
    assert_eq!(report(&again)["runtime_defaults"], "preserved");
    assert_eq!(again.status.code(), Some(0));

    let empty = Home::new();
    empty.seed(json!({}));
    let refused = empty.run(&["codex-runtime-defaults"], None);
    let body = report(&refused);
    assert_eq!(refused.status.code(), Some(1), "{body}");
    assert_eq!(body["status"], "needs-configuration");
    assert!(
        body["recovery"]
            .as_str()
            .unwrap()
            .contains("not registered")
    );
}

#[test]
fn an_empty_codex_home_means_the_default_folder_under_the_profile() {
    let home = Home::new();
    // The default Codex home is <profile>/.codex: make that the fixture's.
    let default = home.0.join(".codex");
    fs::create_dir_all(&default).unwrap();
    fs::write(
        default.join("config.toml"),
        "# fixture
",
    )
    .unwrap();
    let state = json!({
        "user": registered(json!({})), "overlay": {}, "version": 1,
        "file": default.join("config.toml"),
    });
    fs::write(home.0.join("fake-state.json"), state.to_string()).unwrap();
    let output = home.run_with(&["codex-runtime-defaults"], None, &[("CODEX_HOME", "")]);
    let body = report(&output);
    assert_eq!(output.status.code(), Some(0), "{body}");
    assert_eq!(body["runtime_defaults"], "configured");
}

#[test]
fn credentials_without_a_token_record_only_the_connection() {
    let home = Home::new();
    home.seed(json!({}));
    let output = home.run(
        &[
            "codex-credentials",
            "--installer-daemon-url",
            "http://127.0.0.1:9",
        ],
        None,
    );
    let body = report(&output);
    assert_eq!(output.status.code(), Some(0), "{body}");
    assert_eq!(body["status"], "tokenless");
    assert_eq!(body["credential_file_configured"], false);
    assert_eq!(body["connection_configured"], true);
    assert_eq!(body["daemon_url"], "http://127.0.0.1:9");
    assert!(home.codex().join("pseudolife/connection.json").is_file());
}

#[test]
fn credentials_move_a_literal_token_into_a_private_file() {
    let home = Home::new();
    home.seed(registered(json!({
        "PSEUDOLIFE_MCP_TOKEN": "fixture-token-abc",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9",
    })));
    let output = home.run(&["codex-credentials"], None);
    let body = report(&output);
    assert_eq!(output.status.code(), Some(0), "{body}");
    assert_eq!(body["status"], "ready");
    assert_eq!(body["migrated_literal"], true);
    let file = PathBuf::from(body["credential_file_path"].as_str().unwrap());
    assert_eq!(fs::read(&file).unwrap(), b"fixture-token-abc");
    let env = &home.state()["user"]["mcp_servers"][SERVER]["env"];
    assert!(env.get("PSEUDOLIFE_MCP_TOKEN").is_none(), "{env}");
    assert_eq!(env["PSEUDOLIFE_MCP_TOKEN_FILE"], json!(file));
    // The token value never reaches the report.
    assert!(!String::from_utf8_lossy(&output.stdout).contains("fixture-token-abc"));
}

#[test]
fn credentials_refuse_a_bad_installer_token_and_write_nothing() {
    let home = Home::new();
    home.seed(json!({}));
    let output = home.run(
        &[
            "codex-credentials",
            "--installer-token-stdin",
            "--installer-daemon-url",
            "http://127.0.0.1:9",
        ],
        Some(b"two\nlines\n"),
    );
    let body = report(&output);
    assert_eq!(output.status.code(), Some(1), "{body}");
    assert_eq!(body["status"], "needs-configuration");
    assert!(!home.codex().join("pseudolife").exists());
    // A usage error is exit 2 with no report.
    let usage = home.run(&["codex-credentials", "--installer-token-stdin"], None);
    assert_eq!(usage.status.code(), Some(2));
    assert!(usage.stdout.is_empty());
}

/// Seed the fake with a registration whose literal token setup moves into a
/// private file below `codex`, a Codex home that does not exist yet.
fn seed_uncreated(home: &Home, codex: &std::path::Path) {
    let state = json!({
        "user": registered(json!({
            "PSEUDOLIFE_MCP_TOKEN": "fixture-token-abc",
            "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9",
        })),
        "overlay": {}, "version": 1,
        "file": codex.join("config.toml"),
    });
    fs::write(home.0.join("fake-state.json"), state.to_string()).unwrap();
}

/// #1897: Codex installed but never started has no `~/.codex` yet, and
/// Codex refuses a CODEX_HOME naming a missing folder. Setup creates the
/// folder before it starts the app-server, and finishes there.
#[test]
fn credentials_finish_in_a_default_codex_home_that_was_never_created() {
    let home = Home::new();
    let default = home.0.join(".codex");
    assert!(!default.exists());
    seed_uncreated(&home, &default);
    let output = home.run_env(&["codex-credentials"], None, &[], &["CODEX_HOME"]);
    let body = report(&output);
    assert_eq!(output.status.code(), Some(0), "{body}");
    assert_eq!(body["status"], "ready", "{body}");
    assert!(default.is_dir());
    let file = PathBuf::from(body["credential_file_path"].as_str().unwrap());
    assert!(file.starts_with(&default), "{body}");
    assert_eq!(fs::read(&file).unwrap(), b"fixture-token-abc");
}

/// A CODEX_HOME the user set to a folder that does not exist yet is the
/// home they chose for Codex: setup creates it (parents included) as it
/// creates the default one.
#[test]
fn credentials_finish_in_a_chosen_codex_home_that_does_not_exist_yet() {
    let home = Home::new();
    let chosen = home.0.join("config-root").join("codex");
    assert!(!chosen.parent().unwrap().exists());
    seed_uncreated(&home, &chosen);
    let output = home.run_with(
        &["codex-credentials"],
        None,
        &[("CODEX_HOME", chosen.to_str().unwrap())],
    );
    let body = report(&output);
    assert_eq!(output.status.code(), Some(0), "{body}");
    assert_eq!(body["status"], "ready", "{body}");
    assert!(chosen.is_dir());
    let file = PathBuf::from(body["credential_file_path"].as_str().unwrap());
    assert!(file.starts_with(&chosen), "{body}");
}

/// A CODEX_HOME naming a file cannot become a Codex home: setup refuses
/// before Codex starts, naming the variable and the path, and leaves the
/// file as it was.
#[test]
fn a_codex_home_that_is_a_file_is_refused_naming_the_variable() {
    let home = Home::new();
    let file = home.0.join("codex-home-file");
    fs::write(&file, "not a folder").unwrap();
    seed_uncreated(&home, &file);
    let output = home.run_with(
        &["codex-credentials"],
        None,
        &[("CODEX_HOME", file.to_str().unwrap())],
    );
    let body = report(&output);
    assert_eq!(output.status.code(), Some(1), "{body}");
    assert_eq!(body["status"], "needs-configuration", "{body}");
    let recovery = body["recovery"].as_str().unwrap();
    assert!(
        recovery.starts_with(&format!("CODEX_HOME names {}", file.display()))
            && recovery.contains("not a folder"),
        "{recovery}"
    );
    assert_eq!(fs::read(&file).unwrap(), b"not a folder");
}

/// An app-server that stops before setup for another reason: the recovery
/// says what is known (it exited, its exit code, and the command started,
/// here the CODEX_CLI_PATH launcher rather than a `codex` on PATH, with the
/// app-server arguments) and blames the Codex version only if that command
/// reports an unknown command or option.
#[test]
fn an_app_server_that_exits_early_is_reported_with_its_exit_code() {
    let home = Home::new();
    home.seed(registered(json!({})));
    let launcher = home.launcher().display().to_string();
    for step in ["codex-credentials", "codex-runtime-defaults"] {
        let output = home.run_with(&[step], None, &[(EXIT, "3")]);
        let body = report(&output);
        assert_eq!(output.status.code(), Some(1), "{step}: {body}");
        assert_eq!(body["status"], "needs-configuration", "{step}: {body}");
        let recovery = body["recovery"].as_str().unwrap();
        assert!(
            recovery.starts_with("Codex exited before setup completed (exit code 3). ")
                && recovery.contains(&format!(
                    "run the command setup started, `{launcher} app-server --stdio`, in this terminal"
                ))
                && recovery.contains("If it reports an unknown command or option, update Codex;"),
            "{step}: {recovery}"
        );
        assert!(
            !recovery.contains("update Codex and retry"),
            "{step}: {recovery}"
        );
    }
}

fn checkout() -> String {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .to_string_lossy()
        .into_owned()
}

#[test]
fn hooks_skip_appends_the_memory_block_once_without_starting_codex() {
    let home = Home::new();
    // No fake state: a Codex start would fail the run.
    let checkout = checkout();
    let args = [
        "codex-hooks",
        "--checkout",
        &checkout,
        "--source",
        "skip",
        "--instructions",
        "append",
        "--non-interactive",
    ];
    let first = home.run(&args, None);
    let body = report(&first);
    assert_eq!(first.status.code(), Some(0), "{body}");
    assert_eq!(body["status"], "skipped");
    assert_eq!(body["source"], "skip");
    assert_eq!(body["instructions"], "appended");
    let agents = fs::read_to_string(home.codex().join("AGENTS.md")).unwrap();
    assert!(agents.contains("pseudolife-memory"));
    let again = home.run(&args, None);
    assert_eq!(report(&again)["instructions"], "present");
    assert_eq!(
        fs::read_to_string(home.codex().join("AGENTS.md")).unwrap(),
        agents
    );
}

#[test]
fn hooks_without_approval_install_nothing_and_report_pending() {
    let home = Home::new();
    home.seed(registered(json!({})));
    let checkout = checkout();
    let output = home.run(
        &[
            "codex-hooks",
            "--checkout",
            &checkout,
            "--source",
            "manual",
            "--trust",
            "no",
            "--instructions",
            "auto",
            "--non-interactive",
        ],
        None,
    );
    let body = report(&output);
    assert_eq!(output.status.code(), Some(1), "{body}");
    assert_eq!(body["status"], "pending");
    assert_eq!(body["source"], "manual");
    assert_eq!(body["instructions"], "skipped");
    assert!(
        body["recovery"].as_str().unwrap().contains("not installed"),
        "{body}"
    );
    assert!(!home.codex().join("hooks.json").exists());
    assert!(!home.codex().join("pseudolife/hooks").exists());
    // A usage error: exit 2 and no report.
    let usage = home.run(&["codex-hooks", "--source", "manual"], None);
    assert_eq!(usage.status.code(), Some(2));
    assert!(usage.stdout.is_empty());
}

/// The fake app-server: one JSON-RPC message per line on stdin/stdout,
/// state in the file `W7_FAKE_CODEX_STATE` names. Without that variable it
/// is an ordinary (empty) test.
#[test]
fn fake_codex_app_server() {
    let Some(path) = std::env::var_os(STATE) else {
        return;
    };
    let path = PathBuf::from(path);
    // As Codex 0.171.0 does: a CODEX_HOME naming a folder that does not
    // exist stops the app-server before it reads a request. An unset or
    // empty CODEX_HOME is Codex's default home, which Codex creates itself.
    if let Some(home) = std::env::var_os("CODEX_HOME").filter(|home| !home.is_empty())
        && !PathBuf::from(&home).is_dir()
    {
        eprintln!("Error: CODEX_HOME points to {home:?}, but that path does not exist");
        std::process::exit(1);
    }
    // An app-server that stops at once for some other reason.
    if let Some(code) = std::env::var(EXIT).ok().and_then(|code| code.parse().ok()) {
        std::process::exit(code);
    }
    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    // libtest has printed "test fake_codex_app_server ... " without a newline;
    // end that line so the first reply is a line of its own.
    writeln!(out).unwrap();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let Some(id) = message.get("id").cloned() else {
            continue;
        };
        let mut state: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let result = handle(
            &mut state,
            message["method"].as_str().unwrap_or(""),
            &message["params"],
        );
        fs::write(&path, state.to_string()).unwrap();
        let reply = match result {
            Ok(result) => json!({"id": id, "result": result}),
            Err(code) => json!({"id": id, "error": {"code": code, "message": "fixture"}}),
        };
        writeln!(out, "{reply}").unwrap();
        out.flush().unwrap();
    }
}

fn handle(state: &mut Value, method: &str, params: &Value) -> Result<Value, i64> {
    match method {
        "initialize" => Ok(json!({"userAgent": "fake"})),
        "config/read" => {
            let mut effective = state["user"].clone();
            merge(&mut effective, &state["overlay"]);
            Ok(json!({"config": effective, "layers": [{
                "name": {"type": "user", "file": state["file"], "profile": null},
                "version": format!("v{}", state["version"]),
                "config": state["user"],
            }]}))
        }
        "config/batchWrite" => {
            if params["expectedVersion"] != json!(format!("v{}", state["version"])) {
                return Err(-32000);
            }
            for edit in params["edits"].as_array().unwrap() {
                let keys = key_path(edit["keyPath"].as_str().unwrap());
                set(&mut state["user"], &keys, edit["value"].clone());
            }
            state["version"] = json!(state["version"].as_u64().unwrap() + 1);
            Ok(json!({"status": "ok", "version": format!("v{}", state["version"])}))
        }
        "hooks/list" => Ok(json!({"data": [{"cwd": "fixture", "hooks": [], "errors": []}]})),
        _ => Err(-32601),
    }
}

fn merge(base: &mut Value, overlay: &Value) {
    if let (Some(base), Some(overlay)) = (base.as_object_mut(), overlay.as_object()) {
        for (key, value) in overlay {
            match base.get_mut(key) {
                Some(existing) if existing.is_object() && value.is_object() => {
                    merge(existing, value)
                }
                _ => {
                    base.insert(key.clone(), value.clone());
                }
            }
        }
    }
}

fn set(root: &mut Value, keys: &[String], value: Value) {
    if !root.is_object() {
        *root = json!({});
    }
    let map = root.as_object_mut().unwrap();
    if keys.len() == 1 {
        map.insert(keys[0].clone(), value);
    } else {
        set(
            map.entry(keys[0].clone()).or_insert(json!({})),
            &keys[1..],
            value,
        );
    }
}

fn key_path(dotted: &str) -> Vec<String> {
    let mut stream = serde_json::Deserializer::from_str(dotted).into_iter::<String>();
    let Some(Ok(first)) = stream.next() else {
        return vec![];
    };
    let rest = &dotted[stream.byte_offset()..];
    let mut parts = vec![first];
    if let Some(rest) = rest.strip_prefix('.') {
        parts.extend(key_path(rest));
    }
    parts
}
