//! `pseudolife-mcp installer migrate-registrations`: the installers' move of
//! Python client registrations to the native launcher, in a private home.
mod native_binary_fixture;
use pseudolife_stdio::runtime::{self, BINARY, Layout, UpdateLock};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("installer-contract-{}", uuid::Uuid::new_v4()));
        pseudolife_stdio::board::state::private_dir(&path).unwrap();
        fs::create_dir_all(path.join("claude")).unwrap();
        fs::create_dir_all(path.join("codex")).unwrap();
        Self(path)
    }
    fn layout(&self) -> Layout {
        Layout {
            root: self.0.join("runtimes"),
            launcher: self.0.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        }
    }
    fn run(&self, args: &[&str]) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.env_clear();
        for key in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        let layout = self.layout();
        command
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("LOCALAPPDATA", self.0.join("Local"))
            .env("APPDATA", self.0.join("Roaming"))
            .env("CODEX_HOME", self.0.join("codex"))
            .env("CLAUDE_CONFIG_DIR", self.0.join("claude"))
            .env("PATH", self.0.join("empty-path"))
            .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
            .env("PSEUDOLIFE_SHIM_LAUNCHER", layout.launcher)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
            .arg("installer")
            .args(args)
            .current_dir(&self.0)
            .output()
            .unwrap()
    }
    fn install_native(&self) {
        let layout = self.layout();
        let candidate = native_binary_fixture::installable(
            Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
            &self.0,
        );
        let _lock = UpdateLock::acquire(&layout).unwrap();
        runtime::install_binary(&layout, "1.0.0", "pseudolife-mcp==1.0.0", None, &candidate)
            .unwrap();
    }
    /// A venv as pipx or the Python runtime installer leaves it; `editable`
    /// adds the PEP 660 marker a `pip install -e` writes.
    fn venv(&self, name: &str, editable: bool) -> PathBuf {
        let root = self.0.join(name);
        let (scripts, site) = if cfg!(windows) {
            (root.join("Scripts"), root.join("Lib").join("site-packages"))
        } else {
            (root.join("bin"), root.join("lib/python3.12/site-packages"))
        };
        fs::create_dir_all(&scripts).unwrap();
        fs::create_dir_all(site.join("pseudolife_mcp-0.23.1.dist-info")).unwrap();
        fs::write(root.join("pyvenv.cfg"), "home = /usr/bin\n").unwrap();
        if editable {
            fs::write(
                site.join("__editable__.pseudolife_mcp-0.23.1.pth"),
                "/src\n",
            )
            .unwrap();
        }
        let command = scripts.join(BINARY);
        fs::write(&command, b"python console script").unwrap();
        command
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn claude_config(command: &Path) -> Value {
    json!({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": command, "args": [],
        "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1", "PSEUDOLIFE_WRITER_ID": "claude-code"},
    }}, "other": {"kept": true}})
}

fn report(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| {
        panic!(
            "not JSON: {}\nstderr: {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

#[test]
fn refuses_without_the_native_client_and_changes_nothing() {
    let home = Home::new();
    let pipx = home.venv("pipx-venv", false);
    let file = home.0.join("claude").join(".claude.json");
    let before = serde_json::to_vec_pretty(&claude_config(&pipx)).unwrap();
    fs::write(&file, &before).unwrap();
    let output = home.run(&["migrate-registrations", "--json"]);
    assert_eq!(output.status.code(), Some(1));
    let report = report(&output);
    assert_eq!(report["state"], "failed");
    assert!(
        report["detail"]
            .as_str()
            .unwrap()
            .contains("nothing was changed"),
        "{report}"
    );
    assert_eq!(fs::read(&file).unwrap(), before);
}

#[test]
fn moves_python_registrations_keeps_editable_ones_and_names_the_old_client() {
    let home = Home::new();
    home.install_native();
    let layout = home.layout();
    // The old Python client the runtime installer made, beside the native one.
    let old_runtime = layout.root.join("000000");
    fs::create_dir_all(&old_runtime).unwrap();
    fs::write(old_runtime.join("pyvenv.cfg"), "home = /usr/bin\n").unwrap();
    let pipx = home.venv("pipx-venv", false);
    let editable = home.venv("checkout-venv", true);
    let claude = home.0.join("claude").join(".claude.json");
    fs::write(
        &claude,
        serde_json::to_vec_pretty(&claude_config(&pipx)).unwrap(),
    )
    .unwrap();
    let gemini_dir = home.0.join(".gemini");
    fs::create_dir_all(&gemini_dir).unwrap();
    let gemini = gemini_dir.join("settings.json");
    let gemini_before = serde_json::to_vec_pretty(&claude_config(&editable)).unwrap();
    fs::write(&gemini, &gemini_before).unwrap();
    let codex = home.0.join("codex").join("config.toml");
    fs::write(
        &codex,
        format!(
            "model = \"x\"\n\n[mcp_servers.pseudolife-memory]\ncommand = {}\nargs = []\nenv = {{ PSEUDOLIFE_MCP_NO_SPAWN = \"1\" }}\n",
            serde_json::to_string(&pipx).unwrap()
        ),
    )
    .unwrap();

    let output = home.run(&["migrate-registrations", "--json"]);
    let first = report(&output);
    assert_eq!(output.status.code(), Some(0), "{first}");
    let states: Vec<(String, String)> = first["registrations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["client"].as_str().unwrap().into(),
                r["state"].as_str().unwrap().into(),
            )
        })
        .collect();
    assert!(
        states.contains(&("claude-code".into(), "migrated".into())),
        "{first}"
    );
    assert!(
        states.contains(&("codex".into(), "migrated".into())),
        "{first}"
    );
    assert_eq!(first["left"][0]["client"], "gemini", "{first}");
    assert_eq!(first["left"][0]["state"], "editable", "{first}");
    assert!(
        first["python_runtimes"][0]
            .as_str()
            .unwrap()
            .ends_with("000000"),
        "{first}"
    );
    let moved: Value = serde_json::from_slice(&fs::read(&claude).unwrap()).unwrap();
    let entry = &moved["mcpServers"]["pseudolife-memory"];
    assert!(runtime::same_path(
        Path::new(entry["command"].as_str().unwrap()),
        &layout.launcher
    ));
    assert_eq!(entry["env"]["PSEUDOLIFE_WRITER_ID"], "claude-code");
    assert_eq!(moved["other"]["kept"], true);
    let codex_text = fs::read_to_string(&codex).unwrap();
    assert!(codex_text.starts_with("model = \"x\""), "{codex_text}");
    assert!(
        codex_text.contains(&serde_json::to_string(&layout.launcher).unwrap()),
        "{codex_text}"
    );
    assert_eq!(fs::read(&gemini).unwrap(), gemini_before);
    for row in first["registrations"].as_array().unwrap() {
        assert!(
            Path::new(row["backup"].as_str().unwrap()).is_file(),
            "{row}"
        );
    }

    // A rerun changes nothing and says so in one line.
    let text = home.run(&[
        "migrate-registrations",
        "--client",
        "claude-code",
        "--client",
        "codex",
    ]);
    assert_eq!(text.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&text.stdout).trim(),
        "claude-code, codex already run the native client; nothing changed"
    );
}

#[test]
fn usage_errors_exit_two_before_reading_anything() {
    let home = Home::new();
    for args in [
        &["migrate-registrations", "--client", "claude"][..],
        &["migrate-registrations", "--bogus"],
        &["unknown-step"],
        &[],
    ] {
        let output = home.run(args);
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert!(output.stdout.is_empty(), "{args:?}");
    }
}
