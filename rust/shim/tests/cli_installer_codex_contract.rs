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
const SERVER: &str = "pseudolife-memory";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("w7-installer-codex-{}", uuid::Uuid::new_v4()));
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
    assert!(body["recovery"].as_str().unwrap().contains("not registered"));
}

#[test]
fn credentials_without_a_token_record_only_the_connection() {
    let home = Home::new();
    home.seed(json!({}));
    let output = home.run(
        &["codex-credentials", "--installer-daemon-url", "http://127.0.0.1:9"],
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

/// The fake app-server: one JSON-RPC message per line on stdin/stdout,
/// state in the file `W7_FAKE_CODEX_STATE` names. Without that variable it
/// is an ordinary (empty) test.
#[test]
fn fake_codex_app_server() {
    let Some(path) = std::env::var_os(STATE) else {
        return;
    };
    let path = PathBuf::from(path);
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
        let result = handle(&mut state, message["method"].as_str().unwrap_or(""), &message["params"]);
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
        _ => Err(-32601),
    }
}

fn merge(base: &mut Value, overlay: &Value) {
    if let (Some(base), Some(overlay)) = (base.as_object_mut(), overlay.as_object()) {
        for (key, value) in overlay {
            match base.get_mut(key) {
                Some(existing) if existing.is_object() && value.is_object() => merge(existing, value),
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
        set(map.entry(keys[0].clone()).or_insert(json!({})), &keys[1..], value);
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
