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
    assert!(
        !home.layout().root.exists(),
        "a refusal created the runtimes directory"
    );
}

#[test]
fn refuses_when_the_launcher_is_not_the_native_binary() {
    let home = Home::new();
    home.install_native();
    // A Python updater rewrote the stable launcher; the native runtime stayed.
    fs::write(&home.layout().launcher, b"python launcher").unwrap();
    let pipx = home.venv("pipx-venv", false);
    let file = home.0.join("claude").join(".claude.json");
    let before = serde_json::to_vec_pretty(&claude_config(&pipx)).unwrap();
    fs::write(&file, &before).unwrap();
    let output = home.run(&["migrate-registrations", "--json"]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(report(&output)["state"], "failed");
    assert_eq!(fs::read(&file).unwrap(), before);
}

#[test]
fn a_held_update_lock_refuses_and_changes_nothing() {
    let home = Home::new();
    home.install_native();
    let pipx = home.venv("pipx-venv", false);
    let file = home.0.join("claude").join(".claude.json");
    let before = serde_json::to_vec_pretty(&claude_config(&pipx)).unwrap();
    fs::write(&file, &before).unwrap();
    let held = UpdateLock::acquire(&home.layout()).unwrap();
    let output = home.run(&["migrate-registrations", "--json"]);
    drop(held);
    assert_eq!(
        output.status.code(),
        Some(1),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(report(&output)["state"], "failed");
    assert_eq!(fs::read(&file).unwrap(), before);
}

#[test]
fn a_self_spawning_registration_is_left_alone() {
    let home = Home::new();
    home.install_native();
    let pipx = home.venv("pipx-venv", false);
    let file = home.0.join("claude").join(".claude.json");
    // No PSEUDOLIFE_MCP_NO_SPAWN: this client starts its own daemon.
    let config = json!({"mcpServers": {"pseudolife-memory": {"command": pipx, "args": []}}});
    let before = serde_json::to_vec_pretty(&config).unwrap();
    fs::write(&file, &before).unwrap();
    let output = home.run(&["migrate-registrations", "--json"]);
    let moved = report(&output);
    assert_eq!(output.status.code(), Some(0), "{moved}");
    assert_eq!(moved["left"][0]["state"], "spawning", "{moved}");
    assert_eq!(moved["registrations"], json!([]), "{moved}");
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
    let unregistered: Vec<&str> = first["old_unregistered"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p.as_str().unwrap())
        .collect();
    assert_eq!(unregistered.len(), 2, "{first}");
    assert!(
        unregistered.iter().any(|p| p.ends_with("000000")),
        "{first}"
    );
    assert!(
        unregistered
            .iter()
            .any(|p| Path::new(p) == pipx.parent().unwrap().parent().unwrap()),
        "{first}"
    );
    assert_eq!(first["old_in_use"], json!([]), "{first}");
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
        format!(
            "claude-code, codex already run the native client; nothing changed (the old Python client is still on disk, unregistered: {})",
            layout.root.join("000000").display()
        )
    );
    // Unfiltered, the editable Gemini registration joins the same line.
    let all = home.run(&["migrate-registrations"]);
    let all = String::from_utf8_lossy(&all.stdout);
    assert_eq!(all.trim().lines().count(), 1, "{all}");
    assert!(all.contains("gemini still runs"), "{all}");
}

#[test]
fn a_shared_python_install_still_in_use_is_never_called_unregistered() {
    let home = Home::new();
    home.install_native();
    let pipx = home.venv("pipx-venv", false);
    let claude = home.0.join("claude").join(".claude.json");
    let claude_before = serde_json::to_vec_pretty(&claude_config(&pipx)).unwrap();
    fs::write(&claude, &claude_before).unwrap();
    fs::write(
        home.0.join("codex").join("config.toml"),
        format!(
            "[mcp_servers.pseudolife-memory]
command = {}
env = {{ PSEUDOLIFE_MCP_NO_SPAWN = \"1\" }}
",
            serde_json::to_string(&pipx).unwrap()
        ),
    )
    .unwrap();
    let output = home.run(&["migrate-registrations", "--client", "codex", "--json"]);
    let moved = report(&output);
    assert_eq!(output.status.code(), Some(0), "{moved}");
    assert_eq!(moved["registrations"][0]["state"], "migrated", "{moved}");
    assert_eq!(moved["old_unregistered"], json!([]), "{moved}");
    assert_eq!(
        Path::new(moved["old_in_use"][0].as_str().unwrap()),
        pipx.parent().unwrap().parent().unwrap(),
        "{moved}"
    );
    assert_eq!(fs::read(&claude).unwrap(), claude_before);
}

#[test]
fn an_unreadable_unselected_client_does_not_block_the_selected_one() {
    let home = Home::new();
    home.install_native();
    let pipx = home.venv("pipx-venv", false);
    fs::write(home.0.join("claude").join(".claude.json"), b"{ not json").unwrap();
    fs::write(
        home.0.join("codex").join("config.toml"),
        format!(
            "[mcp_servers.pseudolife-memory]
command = {}
env = {{ PSEUDOLIFE_MCP_NO_SPAWN = \"1\" }}
",
            serde_json::to_string(&pipx).unwrap()
        ),
    )
    .unwrap();
    let output = home.run(&["migrate-registrations", "--client", "codex", "--json"]);
    let moved = report(&output);
    assert_eq!(output.status.code(), Some(0), "{moved}");
    assert_eq!(moved["registrations"][0]["state"], "migrated", "{moved}");
    // The broken Claude file means nobody can confirm the old install is unused.
    assert_eq!(moved["old_unconfirmed"], true, "{moved}");
    assert_eq!(moved["old_unregistered"], json!([]), "{moved}");
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

#[test]
fn expose_links_the_launcher_once_and_reruns_quietly() {
    let home = Home::new();
    home.install_native();
    let bin = home.0.join("user-bin");
    let run = || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.env_clear();
        for key in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        let layout = home.layout();
        command
            .env("HOME", &home.0)
            .env("USERPROFILE", &home.0)
            .env("PATH", home.0.join("empty-path"))
            .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
            .env("PSEUDOLIFE_SHIM_LAUNCHER", layout.launcher)
            .env("PSEUDOLIFE_SHIM_USER_BIN", &bin)
            .args(["installer", "expose", "--json"])
            .output()
            .unwrap()
    };
    let first = run();
    let first_report = report(&first);
    assert_eq!(first.status.code(), Some(0), "{first_report}");
    let second = report(&run());
    if cfg!(unix) {
        assert_eq!(first_report["state"], "linked", "{first_report}");
        assert_eq!(second["state"], "current", "{second}");
    } else {
        assert_ne!(first_report["state"], "failed", "{first_report}");
    }
}

#[cfg(unix)]
#[test]
fn expose_moves_the_old_python_entry_aside_and_leaves_a_foreign_one() {
    for (content, moved) in [
        (
            "#!/usr/bin/python3\nimport sys\nfrom pseudolife_memory.cli import main\nsys.exit(main())\n",
            true,
        ),
        ("#!/bin/sh\necho another tool\n", false),
    ] {
        let home = Home::new();
        home.install_native();
        let bin = home.0.join("user-bin");
        fs::create_dir_all(&bin).unwrap();
        let entry = bin.join(BINARY);
        fs::write(&entry, content).unwrap();
        let layout = home.layout();
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command
            .env_clear()
            .env("HOME", &home.0)
            .env("PATH", &bin)
            .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
            .env("PSEUDOLIFE_SHIM_LAUNCHER", &layout.launcher)
            .env("PSEUDOLIFE_SHIM_USER_BIN", &bin)
            .args(["installer", "expose", "--json"]);
        let output = command.output().unwrap();
        let result = report(&output);
        assert_eq!(output.status.code(), Some(0), "{result}");
        let asides: Vec<_> = fs::read_dir(&bin)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".python-"))
            .collect();
        if moved {
            assert_eq!(result["state"], "replaced", "{result}");
            assert_eq!(asides.len(), 1, "{result}");
            assert_eq!(fs::read_to_string(asides[0].path()).unwrap(), content);
            assert_eq!(
                fs::canonicalize(&entry).unwrap(),
                fs::canonicalize(&layout.launcher).unwrap()
            );
        } else {
            assert_eq!(result["state"], "left", "{result}");
            assert!(asides.is_empty(), "{result}");
            assert_eq!(fs::read_to_string(&entry).unwrap(), content);
        }
    }
}

#[cfg(unix)]
#[test]
fn expose_keeps_the_old_entry_while_a_self_spawning_registration_runs_it() {
    let home = Home::new();
    home.install_native();
    let bin = home.0.join("user-bin");
    fs::create_dir_all(&bin).unwrap();
    let entry = bin.join(BINARY);
    let content = "#!/usr/bin/python3\nfrom pseudolife_memory.cli import main\nmain()\n";
    fs::write(&entry, content).unwrap();
    // No PSEUDOLIFE_MCP_NO_SPAWN: this client starts its own daemon through the entry.
    fs::write(
        home.0.join("claude").join(".claude.json"),
        serde_json::to_vec(
            &json!({"mcpServers": {"pseudolife-memory": {"command": entry, "args": []}}}),
        )
        .unwrap(),
    )
    .unwrap();
    let layout = home.layout();
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .env_clear()
        .env("HOME", &home.0)
        .env("CLAUDE_CONFIG_DIR", home.0.join("claude"))
        .env("CODEX_HOME", home.0.join("codex"))
        .env("PATH", &bin)
        .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
        .env("PSEUDOLIFE_SHIM_LAUNCHER", &layout.launcher)
        .env("PSEUDOLIFE_SHIM_USER_BIN", &bin)
        .args(["installer", "expose", "--json"])
        .output()
        .unwrap();
    let result = report(&output);
    assert_eq!(output.status.code(), Some(0), "{result}");
    assert_eq!(result["state"], "left", "{result}");
    assert!(
        result["detail"]
            .as_str()
            .unwrap()
            .contains("claude-code registration still runs it"),
        "{result}"
    );
    assert_eq!(fs::read_to_string(&entry).unwrap(), content);
}
