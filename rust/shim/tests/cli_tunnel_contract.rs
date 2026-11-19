#![forbid(unsafe_code)]
//! `tunnel`: authenticated profile setup, read-only answers, and deferrals
//! that change nothing and open no connection. Seeded profiles need
//! owner-only files: the `seeded` cells run on Unix (chmod); the bridge's
//! `serve()` cell seeds through the native client's own private-file
//! helpers, so it runs on every platform. The differential harness row
//! covers both platforms against the Python oracle.
mod common;
use common::cli::{cleared_command, native_text};
use std::{
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const DEFERRED: &str = "pseudolife-stdio: mode 'tunnel' is deferred in this candidate\n";
const AMBIGUOUS: &str = "daemon connection is missing or ambiguous; pass --daemon-url and --token-file explicitly (an owner-only credential file)\n";
const UNAVAILABLE: &str = "private tunnel file is unavailable or not owner-only\n";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("tunnel-contract-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }
    fn path(&self, rel: &str) -> PathBuf {
        self.0.join(rel)
    }
    fn dir(&self) -> String {
        self.path("tunnels").to_str().unwrap().to_owned()
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// A loopback listener named as the daemon; any accepted connection fails.
struct Listener(TcpListener);
impl Listener {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        Self(listener)
    }
    fn url(&self) -> String {
        format!("http://{}", self.0.local_addr().unwrap())
    }
    fn assert_untouched(&self) {
        assert_eq!(
            self.0.accept().map(|_| ()).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock,
            "a ported or deferred shape connected to the daemon URL"
        );
    }
}

/// Cleared environment: no PATH, a disposable home, the listener as daemon.
fn command(home: &Home, listener: &Listener) -> Command {
    let mut command = cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .current_dir(&home.0)
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("APPDATA", home.path("roaming"))
        .env("LOCALAPPDATA", home.path("local"))
        .env("PSEUDOLIFE_MCP_DAEMON_URL", listener.url())
        .arg("tunnel");
    command
}

fn run(home: &Home, args: &[&str], env: &[(&str, &str)]) -> Output {
    let listener = Listener::new();
    let mut command = command(home, &listener);
    command.args(args);
    for (key, value) in env {
        command.env(key, value);
    }
    let output = command.output().unwrap();
    listener.assert_untouched();
    output
}

fn tree(root: &Path) -> Vec<(PathBuf, Vec<u8>)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path.clone());
                out.push((path, b"dir".to_vec()));
            } else {
                let data = std::fs::read(&path).unwrap();
                out.push((path, data));
            }
        }
    }
    out.sort();
    out
}

fn assert_deferred(home: &Home, args: &[&str], env: &[(&str, &str)]) {
    let before = tree(&home.0);
    let output = run(home, args, env);
    assert_eq!(output.status.code(), Some(1), "{args:?}");
    assert!(output.stdout.is_empty(), "{args:?}");
    assert_eq!(output.stderr, native_text(DEFERRED), "{args:?}");
    assert_eq!(tree(&home.0), before, "{args:?} changed the home");
}

fn assert_refused(home: &Home, args: &[&str], env: &[(&str, &str)], message: &str) {
    let before = tree(&home.0);
    let output = run(home, args, env);
    assert_eq!(output.status.code(), Some(2), "{args:?}");
    assert!(output.stdout.is_empty(), "{args:?}");
    assert_eq!(output.stderr, native_text(message), "{args:?}");
    assert_eq!(tree(&home.0), before, "{args:?} changed the home");
}

#[test]
fn unported_commands_and_argv_shapes_defer_without_effect() {
    let home = Home::new();
    let dir = home.dir();
    for args in [
        &[][..],
        &["doctor"],
        &["doctor", "--profile-dir", &dir],
        &["run", "--profile-dir", &dir],
        &["start", "--profile-dir", &dir],
        &["stop", "--profile-dir", &dir],
        &["service", "preview", "--profile-dir", &dir],
        &["service", "install", "--profile-dir", &dir],
        &["service", "status", "--profile-dir", &dir],
        &["status", "--help"],
        &["status", "--profile=dot", "--profile-dir", &dir],
        &["status", "--prof", "dot", "--profile-dir", &dir],
        &["status", "--json", "--json", "--profile-dir", &dir],
        &["status", "--profile", "-x", "--profile-dir", &dir],
        &["status", "--profile-dir", ""],
        &["update", "--json", "--profile-dir", &dir],
        &["verify", "--accept-access", "--profile-dir", &dir],
        &["setup", "--start", "--profile-dir", &dir],
        &["setup", "--open-browser", "keys", "--profile-dir", &dir],
        &["setup", "--catalog", "all", "--profile-dir", &dir],
    ] {
        assert_deferred(&home, args, &[]);
    }
}

#[test]
fn explicit_setup_with_a_missing_credential_refuses_and_discovery_defers() {
    let home = Home::new();
    let dir = home.dir();
    let token = home.path("token");
    let token = token.to_str().unwrap();
    for extra in [
        &[][..],
        &[
            "--tunnel-id",
            "tunnel_0123",
            "--organization-id",
            "org-Fixture",
        ],
        &[
            "--catalog",
            "full",
            "--key-expires-at",
            "2099-12-31T00:00:00Z",
        ],
    ] {
        let mut args = vec![
            "setup",
            "--profile-dir",
            &dir,
            "--accept-access",
            "--daemon-url",
            "http://127.0.0.1:9",
            "--token-file",
            token,
        ];
        args.extend_from_slice(extra);
        assert_refused(
            &home,
            &args,
            &[],
            "daemon credential file is unavailable or unsafe; repair its owner-only access and retry setup\n",
        );
    }
    // A token file from the environment, with the listener as the daemon URL.
    assert_deferred(
        &home,
        &["setup", "--profile-dir", &dir],
        &[("PSEUDOLIFE_MCP_TOKEN_FILE", token)],
    );
    // A Codex registration exists: its env block is not read natively.
    std::fs::create_dir_all(home.path(".codex")).unwrap();
    std::fs::write(home.path(".codex/config.toml"), b"[other]\n").unwrap();
    assert_deferred(&home, &["setup", "--profile-dir", &dir], &[]);
    std::fs::remove_dir_all(home.path(".codex")).unwrap();
    // A Claude Code registration exists.
    std::fs::write(home.path(".claude.json"), b"{}").unwrap();
    assert_deferred(&home, &["setup", "--profile-dir", &dir], &[]);
}

const SETUP_BEARER: &str = "fixture-setup-bearer";

fn setup_command(home: &Home, url: &str, token: &Path) -> (Command, Listener) {
    let ambient = Listener::new();
    let mut setup = command(home, &ambient);
    setup.args(["setup", "--profile-dir", &home.dir(), "--daemon-url", url]);
    setup.arg("--token-file").arg(token);
    (setup, ambient)
}

fn setup_against(home: &Home, url: &str, accept: bool) -> Output {
    setup_with(home, url, accept, &[])
}

fn setup_with(home: &Home, url: &str, accept: bool, extra: &[&str]) -> Output {
    let (mut setup, ambient) = setup_command(home, url, &home.path("token"));
    setup.args(extra);
    if accept {
        setup.arg("--accept-access");
    }
    let output = setup.output().unwrap();
    ambient.assert_untouched();
    for bytes in [&output.stdout, &output.stderr] {
        assert!(!String::from_utf8_lossy(bytes).contains(SETUP_BEARER));
    }
    output
}

fn setup_home() -> Home {
    let home = Home::new();
    pseudolife_stdio::board::state::private_dir(&home.0).unwrap();
    pseudolife_stdio::board::state::atomic_create(&home.path("token"), SETUP_BEARER.as_bytes())
        .unwrap();
    home
}

#[test]
fn setup_saves_an_authenticated_private_profile_and_reuses_it_unchanged() {
    let home = setup_home();
    let daemon = common::Fixture::start_with(common::Setup {
        lists: vec![common::Reply::Sse(serde_json::json!({
            "tools": [{"name": "fixture", "inputSchema": {"type": "object"}}],
        }))],
        ..Default::default()
    });
    let output = setup_against(&home, &daemon.url, true);
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    assert!(String::from_utf8_lossy(&output.stdout).contains("--tunnel-id <tunnel_id>"));
    let path = home.path("tunnels/dot.profile.json");
    let saved = pseudolife_stdio::board::state::read(&path, 65536).unwrap();
    let profile: serde_json::Value = serde_json::from_slice(&saved).unwrap();
    assert_eq!(profile["name"], "dot");
    assert_eq!(profile["state"], "pending");
    assert_eq!(profile["daemon_url"], daemon.url);
    assert_eq!(profile["consent"], true);
    assert!(!String::from_utf8_lossy(&saved).contains(SETUP_BEARER));

    let requests = daemon.records.lock().unwrap().clone();
    let auth = format!("Bearer {SETUP_BEARER}");
    for method in ["initialize", "tools/list"] {
        assert!(
            requests
                .iter()
                .any(|request| request.message["method"] == method
                    && request.headers.get("authorization") == Some(&auth))
        );
    }
    let before = tree(&home.0);
    let output = setup_against(&home, &daemon.url, true);
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&output.stdout).contains("already configured"));
    assert_eq!(tree(&home.0), before);
    assert_eq!(daemon.records.lock().unwrap().len(), requests.len());

    let contender = Listener::new();
    let output = setup_against(&home, &contender.url(), false);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--accept-access"));
    contender.assert_untouched();
    assert_eq!(tree(&home.0), before);
}

fn seed_setup_profile(home: &Home, profile: &serde_json::Value) {
    let root = home.path("tunnels");
    pseudolife_stdio::board::state::private_dir(&root).unwrap();
    pseudolife_stdio::board::state::atomic_create(
        &root.join("dot.profile.json"),
        &serde_json::to_vec_pretty(profile).unwrap(),
    )
    .unwrap();
}

#[test]
fn setup_reuses_an_equivalent_python_profile() {
    let home = setup_home();
    let daemon = Listener::new();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": null, "organization_id": null, "state": "pending", "consent": true,
            "autostart_consent": false, "runtime_version": null, "minimum_catalog": "current",
            "runtime_key_expires_at": null, "cloud_verification_file": null,
        }),
    );
    let before = tree(&home.0);
    let output = setup_against(&home, &daemon.url(), false);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        output.stdout,
        native_text(&format!(
            "Profile dot is already configured. Check it with `pseudolife-mcp tunnel status --profile dot --profile-dir {}`.\n",
            shell_quoted(&home.dir())
        ))
    );
    assert!(output.stderr.is_empty());
    daemon.assert_untouched();
    assert_eq!(tree(&home.0), before);
}

fn pending_setup_profile(home: &Home, url: &str) {
    seed_setup_profile(
        home,
        &serde_json::json!({
            "name": "dot", "daemon_url": url, "token_file": home.path("token"),
            "state": "pending", "consent": true,
        }),
    );
}

#[test]
fn setup_requires_a_tunnel_id_before_first_key_intake() {
    let home = setup_home();
    let daemon = Listener::new();
    pending_setup_profile(&home, &daemon.url());
    assert_refused(
        &home,
        &["setup", "--profile-dir", &home.dir(), "--read-key"],
        &[],
        "save a tunnel ID with tunnel setup --tunnel-id before importing its runtime key\n",
    );
    daemon.assert_untouched();
}

#[test]
fn setup_repoints_a_moved_credential_after_explicit_consent() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    pending_setup_profile(&home, &daemon.url);
    let moved = home.path("moved-token");
    std::fs::rename(home.path("token"), &moved).unwrap();
    let path = home.path("tunnels/dot.profile.json");
    let before = std::fs::read(&path).unwrap();
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--token-file",
            moved.to_str().unwrap(),
        ],
        &[],
        "pass both --daemon-url and --token-file to change the saved daemon connection\n",
    );
    let (mut cmd, ambient) = setup_command(&home, &daemon.url, &moved);
    let refused = cmd.output().unwrap();
    assert_eq!(refused.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("--accept-access"));
    assert_eq!(std::fs::read(&path).unwrap(), before);
    assert!(daemon.records.lock().unwrap().is_empty());
    assert_eq!(
        cmd.arg("--accept-access").output().unwrap().status.code(),
        Some(0)
    );
    let saved: serde_json::Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(saved["token_file"], moved.to_str().unwrap());
    ambient.assert_untouched();
}

#[test]
fn setup_refuses_native_renewal_and_status_points_to_python() {
    let home = setup_home();
    let daemon = Listener::new();
    let root = home.path(".pseudolife-mcp/tunnel");
    let state = pseudolife_stdio::board::state::atomic_create;
    pseudolife_stdio::board::state::private_dir(&root).unwrap();
    state(
        &root.join("dot.profile.json"),
        &serde_json::to_vec(&serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
            "runtime_key_expires_at": "2020-01-01T00:00:00Z",
        }))
        .unwrap(),
    )
    .unwrap();
    // The fixture represents an occupied key slot. The refusal must precede
    // any native attempt to open or decrypt that key.
    state(&root.join("dot.key"), b"opaque key fixture").unwrap();

    let status = run(&home, &["status"], &[]);
    assert_eq!(status.status.code(), Some(0));
    let output = String::from_utf8_lossy(&status.stdout);
    let expires = "2099-12-01T00:00:00Z";
    let guidance = "Runtime key renewal requires the Python client. Rerun the release installer with PSEUDOLIFE_SHIM_ENGINE=python, then use that client's tunnel setup with --read-key and --key-expires-at.";
    for key_args in [&["--read-key"][..], &["--key-file", "unused-fixture"]] {
        let mut args = vec!["setup", "--profile", "dot", "--key-expires-at", expires];
        args.extend_from_slice(key_args);
        assert_refused(&home, &args, &[], &format!("{guidance}\n"));
    }
    assert!(output.lines().any(|line| line == guidance));
    assert_deferred(&home, &["setup", "--key-expires-at", expires], &[]);
    assert_deferred(&home, &["setup", "--read-key"], &[]);
    daemon.assert_untouched();
}

fn expect_setup_deferral_for_saved_authority(record: Option<&str>, autostart: bool) {
    let home = setup_home();
    let daemon = Listener::new();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
            "autostart_consent": autostart,
        }),
    );
    if let Some(record) = record {
        let bytes = if record.ends_with(".json") {
            br#"{"pid":4294967295}"#.as_slice()
        } else {
            b"opaque key fixture"
        };
        pseudolife_stdio::board::state::atomic_create(&home.path("tunnels").join(record), bytes)
            .unwrap();
    }
    let dir = home.dir();
    let token = home.path("moved-token");
    assert_deferred(
        &home,
        &[
            "setup",
            "--profile-dir",
            &dir,
            "--daemon-url",
            &daemon.url(),
            "--token-file",
            token.to_str().unwrap(),
        ],
        &[],
    );
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &dir,
            "--token-file",
            token.to_str().unwrap(),
        ],
        &[],
        "pass both --daemon-url and --token-file to change the saved daemon connection\n",
    );
    daemon.assert_untouched();
}

#[test]
fn setup_defers_repointing_the_daemon_of_a_keyed_profile() {
    expect_setup_deferral_for_saved_authority(Some("dot.key"), false);
}

#[test]
fn setup_defers_changes_when_a_stopped_tunnel_has_a_process_record() {
    expect_setup_deferral_for_saved_authority(Some("dot.process.json"), false);
}

#[test]
fn setup_defers_changes_when_autostart_is_approved() {
    expect_setup_deferral_for_saved_authority(None, true);
}

#[test]
fn setup_refuses_mixed_settings_and_key_input_for_a_fresh_name() {
    let home = setup_home();
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--read-key",
            "--tunnel-id",
            "tunnel_1111",
        ],
        &[],
        "save profile settings first, then use --read-key or --key-file in a separate setup invocation\n",
    );
}

#[test]
fn setup_and_status_name_an_unpublished_stage_without_exposing_it() {
    let home = setup_home();
    let daemon = Listener::new();
    pending_setup_profile(&home, &daemon.url());
    let stage = ".tunnel-setup-0123456789abcdef0123456789abcdef";
    let secret = "synthetic-stage-contents";
    let path = home.path("tunnels").join(stage);
    pseudolife_stdio::board::state::atomic_create(&path, secret.as_bytes()).unwrap();
    let unrelated = home.path("tunnels/.tunnel-setup-note");
    pseudolife_stdio::board::state::atomic_create(&unrelated, b"unrelated fixture").unwrap();
    let before = tree(&home.0);
    for args in [
        vec!["status", "--json"],
        vec!["setup", "--catalog", "current"],
    ] {
        let dir = home.dir();
        let mut args = args;
        args.extend(["--profile-dir", &dir]);
        let output = run(&home, &args, &[]);
        assert_eq!(output.status.code(), Some(0));
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains(&path.display().to_string()));
        assert!(stderr.contains("remove this file"));
        assert!(!stderr.contains(&unrelated.display().to_string()));
        assert!(!stderr.contains(secret));
        assert!(!String::from_utf8_lossy(&output.stdout).contains(secret));
        if args[0] == "status" {
            assert!(serde_json::from_slice::<serde_json::Value>(&output.stdout).is_ok());
        }
    }
    assert_eq!(tree(&home.0), before);
    daemon.assert_untouched();
}

#[test]
fn setup_resumes_a_pending_profile_with_its_saved_connection() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    pending_setup_profile(&home, &daemon.url);
    let ambient = Listener::new();
    let output = command(&home, &ambient)
        .args([
            "setup",
            "--profile-dir",
            &home.dir(),
            "--tunnel-id",
            "tunnel_abcd",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    let saved = pseudolife_stdio::board::state::read(&home.path("tunnels/dot.profile.json"), 65536)
        .unwrap();
    let profile: serde_json::Value = serde_json::from_slice(&saved).unwrap();
    assert_eq!(profile["tunnel_id"], "tunnel_abcd");
    assert_eq!(profile["state"], "ready");
    assert_eq!(profile["daemon_url"], daemon.url);
    assert!(String::from_utf8_lossy(&output.stdout).contains("--read-key"));
    ambient.assert_untouched();
}

#[test]
fn setup_imports_an_initial_private_key_without_rewriting_the_profile() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let input = home.path("key-input");
    let synthetic = b"fixture-runtime-key";
    pseudolife_stdio::board::state::atomic_create(&input, synthetic).unwrap();
    let profile = home.path("tunnels/dot.profile.json");
    let original = std::fs::read(&profile).unwrap();
    assert_deferred(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            input.to_str().unwrap(),
            "--tunnel-id",
            "tunnel_1111",
        ],
        &[],
    );
    let ambient = Listener::new();
    let mut setup = command(&home, &ambient);
    setup
        .args(["setup", "--profile-dir", &home.dir(), "--key-file"])
        .arg(&input);
    let output = setup.output().unwrap();
    let key = home.path("tunnels/dot.key");
    #[cfg(windows)]
    if output.status.code() == Some(2)
        && std::env::var_os("SSH_CONNECTION").is_some()
        && std::env::var_os("GITHUB_ACTIONS").is_none()
        && String::from_utf8_lossy(&output.stderr).contains("Windows could not protect")
    {
        assert!(!key.exists());
        assert_eq!(std::fs::read(&profile).unwrap(), original);
        ambient.assert_untouched();
        eprintln!(
            "Native DPAPI refusal verified in the key-based SSH logon. Hosted CI must save and read back the key."
        );
        return;
    }
    assert_eq!(output.status.code(), Some(0));
    let stored = std::fs::read(&key).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert!(stored.strip_prefix(b"PLAIN\0") == Some(synthetic));
        assert_eq!(key.metadata().unwrap().permissions().mode() & 0o777, 0o600);
    }
    #[cfg(windows)]
    {
        assert!(stored.starts_with(b"DPAPI\0"));
        assert!(
            !stored
                .windows(synthetic.len())
                .any(|bytes| bytes == synthetic)
        );
    }
    let status = run(
        &home,
        &["status", "--json", "--profile-dir", &home.dir()],
        &[],
    );
    assert_eq!(status.status.code(), Some(0));
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&status.stdout).unwrap()["private_key_present"],
        true
    );
    assert_eq!(std::fs::read(&profile).unwrap(), original);
    assert!(!String::from_utf8_lossy(&output.stdout).contains("fixture-runtime-key"));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("fixture-runtime-key"));
    let request_count = daemon.records.lock().unwrap().len();
    let repeat = setup.output().unwrap();
    assert_eq!(repeat.status.code(), Some(1));
    assert_eq!(repeat.stderr, native_text(DEFERRED));
    assert_eq!(daemon.records.lock().unwrap().len(), request_count);
    assert!(std::fs::read(&key).unwrap() == stored);
    ambient.assert_untouched();
}

#[test]
fn setup_refuses_to_import_the_daemon_bearer_as_a_runtime_key() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            home.path("token").to_str().unwrap(),
        ],
        &[],
        "the runtime key matches the daemon credential; supply the separate tunnel runtime key\n",
    );
}

#[test]
fn setup_rejects_invalid_runtime_key_bytes_without_printing_them() {
    let home = setup_home();
    let daemon = Listener::new();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let input = home.path("key-input");
    pseudolife_stdio::board::state::atomic_create(&input, b"invalid\0synthetic").unwrap();
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            input.to_str().unwrap(),
        ],
        &[],
        "invalid private tunnel runtime key; check the key and retry\n",
    );
    daemon.assert_untouched();
}

#[cfg(unix)]
#[test]
fn setup_refuses_a_runtime_key_file_that_other_users_can_read() {
    use std::os::unix::fs::PermissionsExt;
    let home = setup_home();
    let daemon = Listener::new();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let input = home.path("shared-input");
    pseudolife_stdio::board::state::atomic_create(&input, b"fixture-runtime-key").unwrap();
    std::fs::set_permissions(&input, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            input.to_str().unwrap(),
        ],
        &[],
        "could not read a private runtime key; supply --key-file with an owner-only file or use --read-key at a terminal\n",
    );
    daemon.assert_untouched();
}

#[test]
fn setup_full_catalog_describes_consent_and_hands_off_private_key_intake() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    let flags = ["--catalog", "full", "--tunnel-id", "tunnel_abc123"];
    let output = setup_with(&home, &daemon.url, false, &flags);
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("host and daemon must stay running"));
    assert!(stderr.contains("other sessions"));
    assert!(daemon.records.lock().unwrap().is_empty());
    assert!(!home.path("tunnels").exists());

    let output = setup_with(&home, &daemon.url, true, &flags);
    assert_eq!(output.status.code(), Some(0));
    let stdout = String::from_utf8_lossy(&output.stdout);
    let saved = stdout.find("Profile dot saved").unwrap();
    assert!(stdout[..saved].contains("host and daemon must stay running"));
    assert!(stdout[..saved].contains("other sessions"));
    assert!(stdout.contains("tunnel setup --profile dot"));
    assert!(stdout.contains("--read-key"));
    assert_refused(
        &home,
        &[
            "setup",
            "--profile",
            "dot",
            "--profile-dir",
            &home.dir(),
            "--read-key",
        ],
        &[],
        "--read-key needs a terminal; use --key-file with an owner-only file for unattended setup\n",
    );
}

#[test]
fn setup_never_uses_an_ambient_proxy() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    let proxy = common::Fixture::start();
    let (mut setup, ambient) = setup_command(&home, &daemon.url, &home.path("token"));
    let output = setup
        .arg("--accept-access")
        .env("HTTP_PROXY", &proxy.url)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(proxy.records.lock().unwrap().is_empty());
    ambient.assert_untouched();
}

#[test]
fn setup_refuses_if_the_daemon_credential_changes_during_the_handshake() {
    let home = setup_home();
    let gate = std::sync::Arc::new(common::ResponseGate::default());
    let daemon = common::Fixture::start_with(common::Setup {
        lists: vec![common::Reply::Gated(
            gate.clone(),
            Box::new(common::Reply::Result(
                serde_json::json!({"tools":[{"name":"fixture","inputSchema":{"type":"object"}}]}),
            )),
        )],
        ..Default::default()
    });
    let (mut setup, ambient) = setup_command(&home, &daemon.url, &home.path("token"));
    let child = setup
        .arg("--accept-access")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let reached = wait_for_setup_tools_list(&daemon);
    if reached {
        pseudolife_stdio::board::state::atomic_write(
            &home.path("token"),
            b"replacement-fixture",
            None,
        )
        .unwrap();
    }
    gate.release();
    let output = child.wait_with_output().unwrap();
    assert!(reached);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("credential changed during setup"));
    assert!(!home.path("tunnels").exists());
    ambient.assert_untouched();
}

#[test]
fn setup_refuses_a_process_record_created_during_the_handshake() {
    expect_setup_refusal_for_a_late_runtime_file("dot.process.json", "process record");
}

#[test]
fn setup_refuses_a_key_created_during_the_handshake() {
    expect_setup_refusal_for_a_late_runtime_file("dot.key", "runtime key");
}

fn expect_setup_refusal_for_a_late_runtime_file(file: &str, kind: &str) {
    let home = setup_home();
    let gate = std::sync::Arc::new(common::ResponseGate::default());
    let daemon = common::Fixture::start_with(common::Setup {
        lists: vec![common::Reply::Gated(
            gate.clone(),
            Box::new(common::Reply::Result(
                serde_json::json!({"tools":[{"name":"fixture","inputSchema":{"type":"object"}}]}),
            )),
        )],
        ..Default::default()
    });
    pending_setup_profile(&home, &daemon.url);
    let profile = home.path("tunnels/dot.profile.json");
    let before = std::fs::read(&profile).unwrap();
    let path = home.path("tunnels").join(file);
    let (mut setup, ambient) = setup_command(&home, &daemon.url, &home.path("token"));
    let child = setup
        .args(["--catalog", "full", "--accept-access"])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let reached = wait_for_setup_tools_list(&daemon);
    if reached {
        pseudolife_stdio::board::state::atomic_create(&path, b"new runtime fixture").unwrap();
    }
    gate.release();
    let output = child.wait_with_output().unwrap();
    assert!(reached, "setup never reached tools/list");
    assert_eq!(output.status.code(), Some(2), "{file}");
    assert_eq!(
        output.stderr,
        native_text(&format!(
            "a {kind} appeared during setup at {}. Check `pseudolife-mcp tunnel status --profile dot --profile-dir {}` before retrying\n",
            path.display(),
            shell_quoted(&home.dir())
        ))
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("full"));
    assert!(!stdout.contains("Profile dot saved"));
    assert!(home.path("tunnels/.tunnel-setup.lock").is_file());
    assert_eq!(std::fs::read(profile).unwrap(), before);
    assert_eq!(std::fs::read(path).unwrap(), b"new runtime fixture");
    ambient.assert_untouched();
}

fn wait_for_setup_tools_list(daemon: &common::Fixture) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
    loop {
        if daemon
            .records
            .lock()
            .unwrap()
            .iter()
            .any(|record| record.message["method"] == "tools/list")
        {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}

#[test]
fn setup_refuses_orphan_keys_and_process_records_before_handshake() {
    for file in ["dot.key", "dot.process.json"] {
        let home = setup_home();
        let daemon = Listener::new();
        let root = home.path("tunnels");
        pseudolife_stdio::board::state::private_dir(&root).unwrap();
        pseudolife_stdio::board::state::atomic_create(&root.join(file), b"orphan fixture").unwrap();
        let before = tree(&home.0);
        let output = setup_against(&home, &daemon.url(), true);
        assert_eq!(output.status.code(), Some(2));
        let message = String::from_utf8_lossy(&output.stderr);
        assert!(
            message.contains(file)
                && message.contains("dot.profile.json")
                && message.contains("from a backup")
        );
        daemon.assert_untouched();
        assert_eq!(tree(&home.0), before);
    }
}

#[test]
fn setup_requires_consent_and_refuses_a_redirect_without_saving() {
    let home = setup_home();
    let redirected = Listener::new();
    let daemon = common::Fixture::start_with(common::Setup {
        initialize: vec![common::Reply::Redirect(format!("{}/mcp", redirected.url()))],
        ..Default::default()
    });
    let output = setup_against(&home, &daemon.url, false);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--accept-access"));
    assert!(daemon.records.lock().unwrap().is_empty());
    assert!(!home.path("tunnels").exists());

    let output = setup_against(&home, &daemon.url, true);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("MCP handshake failed"));
    redirected.assert_untouched();
    assert!(!home.path("tunnels").exists());
}

#[test]
fn setup_refuses_an_oversized_daemon_response_without_saving() {
    let home = setup_home();
    let daemon = common::Fixture::start_with(common::Setup {
        initialize: vec![common::Reply::Result(serde_json::json!({
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
            "instructions": "x".repeat(4 * 1024 * 1024),
        }))],
        ..Default::default()
    });
    let output = setup_against(&home, &daemon.url, true);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("MCP handshake failed"));
    assert!(!home.path("tunnels").exists());
}

#[test]
fn setup_resuming_an_existing_profile_defers() {
    let home = Home::new();
    let dir = home.dir();
    seed_setup_profile(&home, &serde_json::json!({}));
    assert_deferred(&home, &["setup", "--profile-dir", &dir], &[]);
    assert_deferred(
        &home,
        &["setup", "--profile-dir", &dir, "--tunnel-id", "nope"],
        &[],
    );
    let daemon = Listener::new();
    assert_deferred(
        &home,
        &[
            "setup",
            "--profile-dir",
            &dir,
            "--daemon-url",
            &daemon.url(),
            "--token-file",
            home.path("token").to_str().unwrap(),
        ],
        &[],
    );
    daemon.assert_untouched();
}

const SHIM_USAGE: &str = "tunnel shim is started by the tunnel runtime, not by hand: it needs --profile <name>, and --profile-dir <directory> for a store other than the default
";

#[test]
fn the_hidden_bridge_refuses_plainly_without_what_it_needs() {
    let home = Home::new();
    let dir = home.dir();
    // Not offered anywhere in the help.
    let help = cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")))
        .arg("--help")
        .output()
        .unwrap();
    assert!(help.status.success());
    assert!(!String::from_utf8_lossy(&help.stdout).contains("tunnel shim"));
    // Started by hand, or without its profile: one line naming what it
    // needs, nothing written, no daemon contacted, no shim started.
    for args in [
        &["shim"][..],
        &["shim", "--profile-dir", &dir],
        &["shim", "--profile"],
        &["shim", "--profile", "dot", "--profile", "dot"],
        &["shim", "--profile", "dot", "--json"],
    ] {
        assert_refused(&home, args, &[], SHIM_USAGE);
    }
    // A profile that is not saved is refused by the store, before any spawn.
    assert_refused(
        &home,
        &["shim", "--profile", "dot", "--profile-dir", &dir],
        &[],
        &format!(
            "{}; `pseudolife-mcp tunnel status --profile dot --profile-dir {}` names the cause\n",
            UNAVAILABLE.trim_end(),
            shell_quoted(&dir)
        ),
    );
}

/// A ready profile, written as the native client writes private files so
/// the store's owner-only checks accept it on every platform.
fn seed_ready_profile(home: &Home) {
    let root = home.path("tunnels");
    pseudolife_stdio::board::state::private_dir(&root).unwrap();
    let token = serde_json::to_string(&home.path("token")).unwrap();
    let body = format!(
        "{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {token}, \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true}}"
    );
    pseudolife_stdio::board::state::atomic_create(&root.join("dot.profile.json"), body.as_bytes())
        .unwrap();
}

/// The bridge for real: it starts the native shim as its child, which
/// answers `initialize` on its own (the profile's daemon, port 9, is
/// down) and ends when the client's input does. The child talks to the
/// profile's daemon, never to the address in the caller's environment
/// (the listener, which stays untouched), and nothing reaches stderr.
#[test]
fn the_bridge_serves_through_the_native_shim_and_the_profiles_daemon() {
    use std::io::{BufRead, BufReader, Write};
    let home = Home::new();
    let dir = home.dir();
    seed_ready_profile(&home);
    let listener = Listener::new();
    let mut command = command(&home, &listener);
    command
        .args(["shim", "--profile", "dot", "--profile-dir", &dir])
        .env(
            "PSEUDOLIFE_MCP_TOKEN",
            "an-ambient-token-the-child-never-sees",
        )
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut child = command.spawn().unwrap();
    let mut stdin = child.stdin.take().unwrap();
    stdin
        .write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"contract\",\"version\":\"1\"}}}\n")
        .unwrap();
    let stdout = child.stdout.take().unwrap();
    let (sender, answer) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let _ = BufReader::new(stdout).read_line(&mut line);
        let _ = sender.send(line);
    });
    let line = answer
        .recv_timeout(std::time::Duration::from_secs(60))
        .expect("the bridge answered initialize");
    let answer: serde_json::Value = serde_json::from_str(&line).unwrap();
    assert_eq!(answer["id"], 1, "{line}");
    assert_eq!(
        answer["result"]["serverInfo"]["name"], "pseudolife-memory",
        "{line}"
    );
    drop(stdin);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() > deadline {
            let _ = child.kill();
            panic!("the bridge outlived its client");
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    };
    let mut stderr = String::new();
    std::io::Read::read_to_string(&mut child.stderr.take().unwrap(), &mut stderr).unwrap();
    assert_eq!(status.code(), Some(0), "{stderr}");
    assert!(stderr.is_empty(), "{stderr}");
    listener.assert_untouched();
}

/// The single-quoted spelling the native client's advice uses for a path
/// (`update::seams::quote`).
fn shell_quoted(text: &str) -> String {
    if cfg!(windows) {
        format!("'{}'", text.replace('\'', "''"))
    } else {
        format!("'{}'", text.replace('\'', "'\"'\"'"))
    }
}

#[test]
fn file_free_answers_and_refusals_match_the_oracle_text() {
    let home = Home::new();
    let dir = home.dir();
    let token = home.path("token");
    let token = token.to_str().unwrap();
    let output = run(&home, &["update", "--profile-dir", &dir], &[]);
    assert_eq!(output.status.code(), Some(3));
    assert_eq!(
        output.stdout,
        native_text("No saved tunnel profiles; update does not start new setup.\n")
    );
    assert!(output.stderr.is_empty());
    assert!(!home.path("tunnels").exists());
    let output = run(&home, &["update"], &[]);
    assert_eq!(output.status.code(), Some(3));
    for args in [
        &["status", "--profile-dir", &dir][..],
        &["status", "--json"],
        &["verify", "--profile-dir", &dir],
    ] {
        assert_refused(&home, args, &[], UNAVAILABLE);
    }
    for args in [
        &["status", "--profile", "bad.name", "--profile-dir", &dir][..],
        &["verify", "--profile", "CON", "--profile-dir", &dir],
        &["setup", "--profile", "", "--profile-dir", &dir],
    ] {
        assert_refused(&home, args, &[], "invalid tunnel profile name\n");
    }
    assert_refused(
        &home,
        &["setup", "--profile-dir", &dir, "--accept-access"],
        &[("PSEUDOLIFE_MCP_TOKEN", "fixture-literal")],
        AMBIGUOUS,
    );
    assert_refused(&home, &["setup", "--profile-dir", &dir], &[], AMBIGUOUS);
    assert_refused(
        &home,
        &["setup", "--profile-dir", &dir, "--token-file", token],
        &[],
        AMBIGUOUS,
    );
    let explicit = |extra: &[&'static str]| {
        let mut args = vec![
            "setup".to_owned(),
            "--profile-dir".into(),
            dir.clone(),
            "--daemon-url".into(),
            "http://127.0.0.1:9".into(),
            "--token-file".into(),
            token.to_owned(),
        ];
        args.extend(extra.iter().map(|s| s.to_string()));
        args
    };
    for (extra, message) in [
        (
            &["--tunnel-id", "tunnel_xyz"][..],
            "invalid tunnel identifier\n",
        ),
        (&["--tunnel-id", ""], "invalid tunnel identifier\n"),
        (
            &["--organization-id", "acme"],
            "invalid organization identifier\n",
        ),
        (
            &["--key-expires-at", "2026-12-01T00:00:00"],
            "key expiry requires an ISO date or timestamp with timezone\n",
        ),
        (
            &["--key-expires-at", "soon"],
            "key expiry requires an ISO date or timestamp with timezone\n",
        ),
    ] {
        let args = explicit(extra);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        assert_refused(&home, &args, &[], message);
    }
    assert_refused(
        &home,
        &[
            "setup",
            "--profile-dir",
            &dir,
            "--daemon-url",
            "ftp://127.0.0.1",
            "--token-file",
            token,
        ],
        &[],
        "endpoint requires HTTPS, or loopback HTTP for the local daemon\n",
    );
    assert!(!home.path("tunnels").exists());
}

#[cfg(unix)]
mod seeded {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn private(path: &Path, data: &[u8]) {
        let parent = path.parent().unwrap();
        std::fs::create_dir_all(parent).unwrap();
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(path, data).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn profile(home: &Home, name: &str, ready: bool) {
        let token = home.path("token");
        let body = if ready {
            format!(
                "{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": \"{}\", \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true}}",
                token.display()
            )
        } else {
            format!(
                "{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": \"{}\"}}",
                token.display()
            )
        };
        private(
            &home.path(&format!("tunnels/{name}.profile.json")),
            body.as_bytes(),
        );
    }

    #[test]
    fn status_reports_a_seeded_profile_and_its_plain_key() {
        let home = Home::new();
        let dir = home.dir();
        profile(&home, "dot", true);
        let output = run(&home, &["status", "--profile-dir", &dir, "--json"], &[]);
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "{\"profile\": \"dot\", \"state\": \"ready\", \"catalog\": \"current\", \"private_key_present\": false, \"key_expiry\": {\"known\": false, \"expired\": null}, \"runtime\": {\"running\": false, \"profile\": \"dot\", \"ready\": false, \"key_expiry\": \"unknown\"}, \"cloud\": {\"verified\": false, \"successful_calls\": 0, \"challenge_current\": false}, \"update\": {\"state\": \"none\", \"needs_attention\": false}}\n"
        );
        private(&home.path("tunnels/dot.key"), b"PLAIN\0fixture-tunnel-key");
        let output = run(&home, &["status", "--profile-dir", &dir, "--json"], &[]);
        assert!(
            String::from_utf8(output.stdout)
                .unwrap()
                .contains("\"private_key_present\": true")
        );
        // A Windows-protected key on a POSIX host is not this host's key.
        private(&home.path("tunnels/dot.key"), b"DPAPI\0sealed");
        let output = run(&home, &["status", "--profile-dir", &dir, "--json"], &[]);
        assert!(
            String::from_utf8(output.stdout)
                .unwrap()
                .contains("\"private_key_present\": false")
        );
    }

    #[test]
    fn verify_refuses_by_readiness_and_update_reports_each_profile() {
        let home = Home::new();
        let dir = home.dir();
        profile(&home, "alpha", false);
        profile(&home, "dot", true);
        assert_refused(
            &home,
            &["verify", "--profile-dir", &dir, "--profile", "alpha"],
            &[],
            "finish tunnel setup before requesting cloud verification\n",
        );
        // A ready profile is not running with or without its key; only an
        // unfinished one is pending.
        let not_running = "{\"state\": \"current\", \"needs_attention\": false, \"detail\": \"2 saved tunnel profiles checked; local client registrations preserved\", \"profiles\": [{\"profile\": \"alpha\", \"state\": \"pending\"}, {\"profile\": \"dot\", \"state\": \"not-running\", \"running\": false}]}\n";
        assert_eq!(
            answered(&home, &["update", "--profile-dir", &dir]),
            not_running
        );
        private(&home.path("tunnels/dot.key"), b"PLAIN\0fixture-tunnel-key");
        assert_refused(&home, &["verify", "--profile-dir", &dir], &[], UNAVAILABLE);
        // With its key and no record: still not running, never a deferral.
        assert_eq!(
            answered(&home, &["update", "--profile-dir", &dir]),
            not_running
        );
        private(&home.path("tunnels/dot.process.json"), b"{}");
        assert_deferred(&home, &["verify", "--profile-dir", &dir], &[]);
        // A record naming no process id proves nothing: unknown, never
        // called deletable, and status says so.
        let status = answered(&home, &["status", "--profile-dir", &dir]);
        assert!(
            status.starts_with("Profile dot: ready; runtime running=unknown, ready=False; cloud verification not checked by this client.\nWhether tunnel dot runs is unknown: its process record ")
                && status.contains("names no process id")
                && !status.contains("can be deleted"),
            "{status}"
        );
        let update = answered(&home, &["update", "--profile-dir", &dir]);
        assert!(
            update.contains("\"state\": \"stale\", \"running\": \"unknown\", \"detail\": \"whether tunnel dot runs is unknown: its process record ")
                && !update.contains("can be deleted"),
            "{update}"
        );
    }

    /// A command that answers on stdout with exit 0 and changes nothing.
    fn answered(home: &Home, args: &[&str]) -> String {
        let before = tree(&home.0);
        // The macOS process table is read through ps, found on PATH.
        let output = run(home, args, &[("PATH", "/usr/bin:/bin")]);
        assert_eq!(output.status.code(), Some(0), "{args:?}: {output:?}");
        assert!(output.stderr.is_empty(), "{args:?}: {output:?}");
        assert_eq!(tree(&home.0), before, "{args:?} changed the home");
        String::from_utf8(output.stdout).unwrap()
    }

    #[test]
    fn a_dead_or_orphan_record_never_stops_update() {
        let home = Home::new();
        let dir = home.dir();
        profile(&home, "dot", true);
        private(&home.path("tunnels/dot.key"), b"PLAIN\0fixture-tunnel-key");
        // A pid no process table holds (above Linux's pid_max ceiling).
        let dead = br#"{"pid": 4294967001, "created": 0.0, "exe": "/nonexistent/tunnel-client", "argv": ["/nonexistent/tunnel-client"]}"#;
        private(&home.path("tunnels/dot.process.json"), dead);
        private(&home.path("tunnels/old.process.json"), dead);
        let report = answered(&home, &["update", "--profile-dir", &dir]);
        let report: serde_json::Value = serde_json::from_str(&report).unwrap();
        // Nothing runs: the update is current; the orphan is a leftover.
        assert_eq!(report["state"], "current", "{report}");
        assert_eq!(report["profiles"][0]["state"], "not-running", "{report}");
        assert_eq!(report["profiles"][1]["record"], "old.process.json");
        assert_eq!(report["profiles"][1]["state"], "leftover", "{report}");
        assert_eq!(report["profiles"][1]["running"], false, "{report}");
    }

    /// Run with a deadline; a hung child is killed by its own PID.
    fn run_bounded(home: &Home, args: &[&str]) -> Output {
        let listener = Listener::new();
        let mut command = command(home, &listener);
        command
            .args(args)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        let mut child = command.spawn().unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while child.try_wait().unwrap().is_none() {
            if std::time::Instant::now() > deadline {
                let _ = child.kill();
                let _ = child.wait();
                panic!("tunnel {args:?} did not exit within 10 s");
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let output = child.wait_with_output().unwrap();
        listener.assert_untouched();
        output
    }

    #[test]
    fn a_redirected_challenge_is_no_challenge_and_a_fifo_never_hangs() {
        let home = Home::new();
        let dir = home.dir();
        profile(&home, "dot", true);
        let normal = run(&home, &["status", "--profile-dir", &dir, "--json"], &[]);
        assert_eq!(normal.status.code(), Some(0));
        // A deep record behind a symlink: Python's checked_path refuses the
        // link, so verification_status reads no challenge at all.
        private(&home.path("deep.json"), "[".repeat(150).as_bytes());
        let verification = home.path("tunnels/dot-verification");
        std::fs::create_dir_all(&verification).unwrap();
        std::os::unix::fs::symlink(home.path("deep.json"), verification.join("challenge.json"))
            .unwrap();
        let output = run_bounded(&home, &["status", "--profile-dir", &dir, "--json"]);
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(output.stdout, normal.stdout);
        // The same deep record as a private regular file: deferred.
        std::fs::remove_file(verification.join("challenge.json")).unwrap();
        private(
            &verification.join("challenge.json"),
            "[".repeat(150).as_bytes(),
        );
        assert_deferred(&home, &["status", "--profile-dir", &dir], &[]);
        // A FIFO would block Python's open: deferred, without blocking here.
        std::fs::remove_file(verification.join("challenge.json")).unwrap();
        let made = Command::new("mkfifo")
            .arg(verification.join("challenge.json"))
            .status();
        if made.is_ok_and(|status| status.success()) {
            let output = run_bounded(&home, &["status", "--profile-dir", &dir]);
            assert_eq!(output.status.code(), Some(1));
            assert_eq!(output.stderr, native_text(DEFERRED));
        }
    }
}
