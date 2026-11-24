#![forbid(unsafe_code)]
//! `tunnel`: authenticated profile setup, read-only answers, and refusals
//! that name their cause, change nothing and open no connection. Seeded profiles need
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

/// What the generic dispatcher deferral said; no `tunnel` argv prints it.
const DEFERRAL_MARK: &str = "is deferred in this candidate";
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

/// A refusal (exit 2) whose one line contains `cause`, never the old
/// generic deferral, changing nothing.
fn assert_refused_with(home: &Home, args: &[&str], env: &[(&str, &str)], cause: &str) {
    let before = tree(&home.0);
    let output = run(home, args, env);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(2), "{args:?}: {stderr}");
    assert!(output.stdout.is_empty(), "{args:?}");
    assert!(stderr.contains(cause), "{args:?}: {stderr}");
    assert!(!stderr.contains(DEFERRAL_MARK), "{args:?}: {stderr}");
    assert_eq!(stderr.trim_end().lines().count(), 1, "{args:?}: {stderr}");
    assert!(tree(&home.0) == before, "{args:?} changed the home");
}

fn assert_refused(home: &Home, args: &[&str], env: &[(&str, &str)], message: &str) {
    let before = tree(&home.0);
    let output = run(home, args, env);
    assert_eq!(output.status.code(), Some(2), "{args:?}");
    assert!(output.stdout.is_empty(), "{args:?}");
    assert_eq!(output.stderr, native_text(message), "{args:?}");
    assert!(tree(&home.0) == before, "{args:?} changed the home");
}

#[test]
fn bridge_readiness_protocol_query_is_local_and_inert() {
    let home = Home::new();
    let before = tree(&home.0);
    let answer = run(&home, &["shim", "--readiness-protocol"], &[]);
    assert!(answer.status.success());
    assert!(answer.stderr.is_empty());
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&answer.stdout).unwrap(),
        serde_json::json!({"version":1})
    );
    assert_eq!(tree(&home.0), before);
}

#[test]
fn every_tunnel_argv_answers_or_refuses_with_its_cause_without_effect() {
    let home = Home::new();
    let dir = home.dir();
    for (args, cause) in [
        (
            &[][..],
            "tunnel needs a command: setup, status, verify, run, stop or update",
        ),
        (
            &["doctor"],
            "tunnel doctor is not part of the native client",
        ),
        (
            &["doctor", "--profile-dir", &dir],
            "tunnel doctor is not part of the native client",
        ),
        (
            &["start", "--profile-dir", &dir],
            "Run `pseudolife-mcp tunnel run` with the same --profile and --profile-dir in a terminal you keep open; `pseudolife-mcp tunnel stop` ends it",
        ),
        (
            &["service", "preview", "--profile-dir", &dir],
            "tunnel service is not part of the native client",
        ),
        (
            &["service", "install", "--profile-dir", &dir],
            "tunnel service is not part of the native client",
        ),
        (
            &["service", "status", "--profile-dir", &dir],
            "tunnel service is not part of the native client",
        ),
        (
            &["status", "--prof", "dot", "--profile-dir", &dir],
            "tunnel status does not take",
        ),
        (
            &["status", "--json", "--json", "--profile-dir", &dir],
            "--json is given twice",
        ),
        (
            &["status", "--profile", "-x", "--profile-dir", &dir],
            "--profile needs a value",
        ),
        (
            &["status", "--profile-dir", ""],
            "the tunnel directory cannot be opened safely",
        ),
        (
            &["verify", "--profile-dir", ""],
            "the tunnel directory cannot be opened safely",
        ),
        (
            &["setup", "--profile-dir", ""],
            "the tunnel directory cannot be opened safely",
        ),
        (
            &["update", "--json", "--profile-dir", &dir],
            "tunnel update does not take",
        ),
        (
            &["verify", "--accept-access", "--profile-dir", &dir],
            "tunnel verify does not take",
        ),
        (
            &["setup", "--start", "--profile-dir", &dir],
            "tunnel setup --start is not part of the native client",
        ),
        (
            &["setup", "--open-browser", "keys", "--profile-dir", &dir],
            "https://platform.openai.com/settings/organization/api-keys",
        ),
        (
            &["setup", "--catalog", "all", "--profile-dir", &dir],
            "--catalog takes current or full",
        ),
        (&["bogus", "--profile-dir", &dir], "unknown tunnel command"),
    ] {
        assert_refused_with(&home, args, &[], cause);
    }
    // Help answers on stdout and changes nothing.
    let before = tree(&home.0);
    for (args, first) in [
        (
            &["--help"][..],
            "usage: pseudolife-mcp tunnel <command> [options]",
        ),
        (
            &["status", "--help"],
            "usage: pseudolife-mcp tunnel status [options]",
        ),
        (
            &["setup", "-h"],
            "usage: pseudolife-mcp tunnel setup [options]",
        ),
    ] {
        let output = run(&home, args, &[]);
        assert_eq!(output.status.code(), Some(0), "{args:?}");
        assert!(output.stderr.is_empty(), "{args:?}");
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert_eq!(stdout.lines().next(), Some(first), "{args:?}: {stdout}");
    }
    assert!(tree(&home.0) == before, "help changed the home");
    // The joined form names the same profile and directory as the separate
    // one: a saved, non-default profile answers only when both are read.
    let store = home.path("tunnels");
    pseudolife_stdio::board::state::private_dir(&store).unwrap();
    pseudolife_stdio::board::state::atomic_create(
        &store.join("alpha.profile.json"),
        &serde_json::to_vec(&serde_json::json!({
            "name": "alpha", "daemon_url": "http://127.0.0.1:9", "token_file": home.path("token"),
            "state": "pending", "consent": true,
        }))
        .unwrap(),
    )
    .unwrap();
    let joined = run(
        &home,
        &["status", "--profile=dot", &format!("--profile-dir={dir}")],
        &[],
    );
    assert_eq!(joined.status.code(), Some(2));
    let joined = run(
        &home,
        &["status", "--profile=alpha", &format!("--profile-dir={dir}")],
        &[],
    );
    assert!(
        String::from_utf8_lossy(&joined.stdout).starts_with("Profile alpha: pending"),
        "{}",
        String::from_utf8_lossy(&joined.stderr)
    );
    let separate = run(
        &home,
        &["status", "--profile", "alpha", "--profile-dir", &dir],
        &[],
    );
    assert_eq!(joined.status.code(), separate.status.code());
    assert_eq!(joined.stdout, separate.stdout);
    assert_eq!(joined.stderr, separate.stderr);
}

#[test]
fn explicit_setup_with_a_missing_credential_refuses_and_discovery_names_the_flags() {
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
    // A token file from the environment, with the listener as the daemon
    // URL: the refusal gives the explicit command for that connection.
    assert_refused_with(
        &home,
        &["setup", "--profile-dir", &dir],
        &[("PSEUDOLIFE_MCP_TOKEN_FILE", token)],
        "guided setup is not part of the native client. To use the daemon this environment names, run tunnel setup with --daemon-url ",
    );
    // A Codex registration exists: its env block is not read natively.
    std::fs::create_dir_all(home.path(".codex")).unwrap();
    std::fs::write(home.path(".codex/config.toml"), b"[other]\n").unwrap();
    let registration = "this client does not read the daemon connection from a Claude Code or Codex registration; pass --daemon-url and --token-file";
    assert_refused_with(&home, &["setup", "--profile-dir", &dir], &[], registration);
    std::fs::remove_dir_all(home.path(".codex")).unwrap();
    // A Claude Code registration exists.
    std::fs::write(home.path(".claude.json"), b"{}").unwrap();
    assert_refused_with(&home, &["setup", "--profile-dir", &dir], &[], registration);
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
fn run_refuses_a_ready_profile_without_a_runtime_key_before_launch() {
    let home = setup_home();
    let daemon = Listener::new();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "state": "ready", "tunnel_id": "tunnel_abcd", "consent": true,
        }),
    );
    assert_refused(
        &home,
        &["run", "--profile-dir", &home.dir()],
        &[],
        "profile dot has no runtime key. Import its private key with tunnel setup before running it\n",
    );
    daemon.assert_untouched();
}

#[test]
fn stop_without_a_record_is_a_no_op() {
    let home = Home::new();
    let before = tree(&home.0);
    let output = run(&home, &["stop", "--profile-dir", &home.dir()], &[]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(output.stdout, native_text("Tunnel dot is stopped.\n"));
    assert!(output.stderr.is_empty());
    assert_eq!(tree(&home.0), before);
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

fn seed_profile_for_renewal(home: &Home, daemon_url: &str) -> bool {
    seed_setup_profile(
        home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon_url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
            "runtime_key_expires_at": "2020-01-01T00:00:00Z",
        }),
    );
    let source = home.path("old-key-fixture");
    pseudolife_stdio::board::state::atomic_create(&source, b"fixture-old-key").unwrap();
    let output = run(
        home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            source.to_str().unwrap(),
        ],
        &[],
    );
    #[cfg(windows)]
    if refused_dpapi_in_ssh_logon(&output) {
        eprintln!("DPAPI renewal is exercised in hosted Windows");
        return false;
    }
    assert_eq!(output.status.code(), Some(0));
    true
}

#[test]
fn setup_renews_a_stopped_profiles_private_key_and_expiry() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    if !seed_profile_for_renewal(&home, &daemon.url) {
        return;
    }
    let key = home.path("tunnels/dot.key");
    let replacement = home.path("replacement-key-fixture");
    pseudolife_stdio::board::state::atomic_create(&replacement, b"fixture-renewed-key").unwrap();
    let directory = home.dir();
    let expiry = "2099-12-01T00:00:00Z";
    let base = [
        "setup",
        "--profile-dir",
        &directory,
        "--key-file",
        replacement.to_str().unwrap(),
    ];
    let args = [&base[..], &["--key-expires-at", expiry]].concat();
    let old = std::fs::read(&key).unwrap();
    let profile_path = home.path("tunnels/dot.profile.json");
    let old_profile = std::fs::read(&profile_path).unwrap();
    let output = run(&home, &args, &[]);
    assert_eq!(output.status.code(), Some(0));
    let renewed = String::from_utf8_lossy(&output.stdout);
    assert!(renewed.contains("Runtime key renewed") && renewed.contains("Revoke it there"));
    let previous = home.path("tunnels/dot.key-previous");
    let retained = pseudolife_stdio::board::state::read(&previous, 131072).unwrap();
    assert!(retained.windows(old.len()).any(|part| part == old));
    #[cfg(windows)]
    assert!(
        !retained
            .windows(b"fixture-old-key".len())
            .any(|part| part == b"fixture-old-key")
    );
    let saved = std::fs::read(&key).unwrap();
    assert!(saved != old);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert!(saved == b"PLAIN\0fixture-renewed-key");
        assert_eq!(key.metadata().unwrap().permissions().mode() & 0o777, 0o600);
    }
    #[cfg(windows)]
    {
        assert!(saved.starts_with(b"DPAPI\0"));
        assert!(
            !saved
                .windows(b"fixture-renewed-key".len())
                .any(|part| part == b"fixture-renewed-key")
        );
    }
    let repeated = run(&home, &args, &[]);
    assert_eq!(repeated.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&repeated.stdout).contains("already configured"));
    assert!(std::fs::read(&key).unwrap() == saved);
    let read_profile = || {
        serde_json::from_slice::<serde_json::Value>(
            &std::fs::read(home.path("tunnels/dot.profile.json")).unwrap(),
        )
        .unwrap()
    };
    let profile = read_profile();
    assert_eq!(profile["runtime_key_expires_at"], expiry);
    assert_eq!(profile["tunnel_id"], "tunnel_abcd");
    let status = run(&home, &["status", "--profile-dir", &directory], &[]);
    assert_eq!(status.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&status.stdout).lines().any(|line| {
        line.contains("Previous key retained")
            && line.contains("--restore-key` if the replacement fails")
            && line.contains("--discard-previous-key")
            && line.contains("only after the replacement has worked")
    }));
    let refused = run(&home, &base, &[]);
    assert_eq!(refused.status.code(), Some(2));
    let error = String::from_utf8_lossy(&refused.stderr);
    assert!(error.contains("--restore-key") && !error.contains("--discard-previous-key"));
    assert_eq!(read_profile()["runtime_key_expires_at"], expiry);

    let restore = run(
        &home,
        &["setup", "--profile-dir", &directory, "--restore-key"],
        &[],
    );
    assert_eq!(restore.status.code(), Some(0));
    assert!(std::fs::read(&key).unwrap() == old);
    assert!(std::fs::read(&profile_path).unwrap() == old_profile);
    assert!(!previous.exists());
    let output = run(&home, &base, &[]);
    assert_eq!(output.status.code(), Some(0));
    assert!(read_profile()["runtime_key_expires_at"].is_null());
    assert!(!home.path("tunnels/dot.key-renewal").exists());
    let replacement_blob = std::fs::read(&key).unwrap();
    let discard = run(
        &home,
        &[
            "setup",
            "--profile-dir",
            &directory,
            "--discard-previous-key",
        ],
        &[],
    );
    assert_eq!(discard.status.code(), Some(0));
    assert!(!previous.exists());
    assert!(std::fs::read(&key).unwrap() == replacement_blob);
}

#[test]
fn a_rejected_renewal_preserves_both_saved_files() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    if !seed_profile_for_renewal(&home, &daemon.url) {
        return;
    }
    let profile = home.path("tunnels/dot.profile.json");
    let original = std::fs::read(&profile).unwrap();
    let saved = std::fs::read(home.path("tunnels/dot.key")).unwrap();
    let output = run(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            home.path("token").to_str().unwrap(),
            "--key-expires-at",
            "2099-12-01T00:00:00Z",
        ],
        &[],
    );
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("runtime key matches the daemon credential"));
    assert!(!stderr.contains(SETUP_BEARER));
    assert!(!String::from_utf8_lossy(&output.stdout).contains(SETUP_BEARER));
    assert!(std::fs::read(home.path("tunnels/dot.key")).unwrap() == saved);
    assert!(std::fs::read(profile).unwrap() == original);
}

#[test]
fn renewal_refuses_a_held_launch_lock_before_publishing() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let root = home.path("tunnels");
    let state = pseudolife_stdio::board::state::atomic_create;
    let key = root.join("dot.key");
    let replacement = home.path("new-key-fixture");
    state(&key, b"opaque prior fixture").unwrap();
    state(&replacement, b"fixture-replacement-key").unwrap();
    let lock = pseudolife_stdio::board::state::create(&root.join("dot.native-run.lock"))
        .unwrap()
        .file;
    lock.try_lock().unwrap();
    let output = run(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            replacement.to_str().unwrap(),
        ],
        &[],
    );
    assert_eq!(output.status.code(), Some(2));
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(
        ![
            SETUP_BEARER,
            "fixture-replacement-key",
            "opaque prior fixture"
        ]
        .iter()
        .any(|sample| error.contains(sample))
    );
    assert!(
        error.contains("stop the tunnel first"),
        "renewal refusal: {error}"
    );
    assert!(std::fs::read(key).unwrap() == b"opaque prior fixture");
    assert!(!root.join("dot.key-renewal").exists());
}

#[test]
fn pending_renewal_refuses_reads_and_names_the_recovery_command() {
    let home = setup_home();
    let daemon = Listener::new();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let marker = home.path("tunnels/dot.key-renewal");
    pseudolife_stdio::board::state::atomic_create(&marker, b"invalid journal fixture").unwrap();
    for mode in ["status", "run", "verify"] {
        let output = run(&home, &[mode, "--profile-dir", &home.dir()], &[]);
        assert_eq!(output.status.code(), Some(2));
        let error = String::from_utf8_lossy(&output.stderr);
        assert!(error.contains("dot.key-renewal") && error.contains("Retry tunnel setup"));
        assert!(output.stdout.is_empty());
    }
    let output = run(&home, &["setup", "--profile-dir", &home.dir()], &[]);
    assert_eq!(output.status.code(), Some(2));
    let error = String::from_utf8_lossy(&output.stderr).replace('\\', "/");
    assert!(
        error.contains("cannot recover runtime key renewal")
            && error.contains("saved key and profile expiry")
            && error.contains(&format!("remove {}", marker.display()).replace('\\', "/")),
        "{error}"
    );
    assert!(marker.exists());
    daemon.assert_untouched();
}

#[test]
fn setup_points_first_key_with_expiry_to_two_native_steps() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let dir = home.dir();
    let expires = "2099-12-01T00:00:00Z";
    for key_args in [&["--read-key"][..], &["--key-file", "unused-fixture"]] {
        let mut args = vec!["setup", "--profile-dir", &dir, "--key-expires-at", expires];
        args.extend_from_slice(key_args);
        assert_refused(
            &home,
            &args,
            &[],
            "save the expiry with tunnel setup --key-expires-at first, then import the private key with --read-key or --key-file in a separate invocation\n",
        );
    }
    assert!(daemon.records.lock().unwrap().is_empty());
    let output = run(
        &home,
        &["setup", "--profile-dir", &dir, "--key-expires-at", expires],
        &[],
    );
    assert_eq!(output.status.code(), Some(0));
    let profile = std::fs::read(home.path("tunnels/dot.profile.json")).unwrap();
    let profile: serde_json::Value = serde_json::from_slice(&profile).unwrap();
    assert_eq!(profile["runtime_key_expires_at"], expires);
    assert!(!home.path("tunnels/dot.key").exists());
}

#[test]
fn status_points_to_native_renewal_and_expiry_alone_refuses() {
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
    assert!(output.lines().any(|line| line == "Renew privately: pseudolife-mcp tunnel setup --profile dot --read-key --key-expires-at <ISO timestamp>."));
    assert_refused(
        &home,
        &["setup", "--key-expires-at", expires],
        &[],
        "renew the runtime key with tunnel setup --read-key or --key-file and an optional new --key-expires-at\n",
    );
    let before = tree(&home.0);
    let same_expiry = run(
        &home,
        &["setup", "--key-expires-at", "2020-01-01T00:00:00Z"],
        &[],
    );
    assert_eq!(same_expiry.status.code(), Some(0));
    assert_eq!(tree(&home.0), before);
    daemon.assert_untouched();
}

#[test]
fn expiry_guidance_names_first_key_steps_when_no_key_is_saved() {
    for ready in [false, true] {
        let home = setup_home();
        let daemon = Listener::new();
        let mut profile = serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "state": "pending", "consent": true, "runtime_key_expires_at": "2020-01-01T00:00:00Z",
        });
        if ready {
            profile["state"] = serde_json::json!("ready");
            profile["tunnel_id"] = serde_json::json!("tunnel_abcd");
        }
        seed_setup_profile(&home, &profile);
        let status = run(&home, &["status", "--profile-dir", &home.dir()], &[]);
        assert_eq!(status.status.code(), Some(0));
        let stdout = String::from_utf8_lossy(&status.stdout);
        assert!(!stdout.contains("Renew privately:"));
        assert!(
            stdout
                .lines()
                .any(|line| line.starts_with("Save the first key's settings:")
                    && line.contains("--key-expires-at")
                    && line.contains("--tunnel-id") != ready)
        );
        assert!(
            stdout
                .lines()
                .any(|line| line.starts_with("Import the key privately:")
                    && line.contains("--read-key")
                    && !line.contains("--key-expires-at"))
        );
        daemon.assert_untouched();
    }
}

fn expect_setup_refusal_for_saved_authority(record: Option<&str>, autostart: bool, cause: &str) {
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
    assert_refused_with(
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
        cause,
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

/// A changed connection asks for consent again; a saved key does not block it.
const CONSENT_AGAIN: &str = "Rerun with --accept-access to consent to that access.";

#[test]
fn setup_repointing_the_daemon_of_a_keyed_profile_asks_for_consent_again() {
    expect_setup_refusal_for_saved_authority(Some("dot.key"), false, CONSENT_AGAIN);
}

#[test]
fn setup_refuses_changes_while_a_process_record_exists_and_names_it() {
    expect_setup_refusal_for_saved_authority(
        Some("dot.process.json"),
        false,
        "cannot change profile dot while a process record exists at ",
    );
}

#[test]
fn setup_changes_with_autostart_approved_ask_for_consent_again() {
    expect_setup_refusal_for_saved_authority(None, true, CONSENT_AGAIN);
}

#[test]
fn setup_changes_a_keyed_profile_and_keeps_its_key() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
            "autostart_consent": true,
        }),
    );
    let key = home.path("tunnels/dot.key");
    pseudolife_stdio::board::state::atomic_create(&key, b"opaque key fixture").unwrap();
    let ambient = Listener::new();
    let output = command(&home, &ambient)
        .args([
            "setup",
            "--profile-dir",
            &home.dir(),
            "--tunnel-id",
            "tunnel_ef01",
            "--accept-access",
        ])
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let saved = pseudolife_stdio::board::state::read(&home.path("tunnels/dot.profile.json"), 65536)
        .unwrap();
    let profile: serde_json::Value = serde_json::from_slice(&saved).unwrap();
    assert_eq!(profile["tunnel_id"], "tunnel_ef01");
    assert_eq!(profile["autostart_consent"], true);
    assert_eq!(std::fs::read(&key).unwrap(), b"opaque key fixture");
    ambient.assert_untouched();
}

/// A ready profile with a saved (opaque) key, served by a fixture daemon.
fn keyed_profile(home: &Home, daemon: &common::Fixture) {
    seed_setup_profile(
        home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
            "runtime_key_expires_at": "2099-01-01T00:00:00Z",
        }),
    );
    pseudolife_stdio::board::state::atomic_create(
        &home.path("tunnels").join("dot.key"),
        b"opaque key fixture",
    )
    .unwrap();
}

#[test]
fn setup_changes_the_expiry_of_a_saved_key_only_by_renewing_it() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    keyed_profile(&home, &daemon);
    assert_refused_with(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--catalog",
            "current",
            "--key-expires-at",
            "2099-12-31T00:00:00Z",
        ],
        &[],
        "renew the runtime key with tunnel setup --read-key or --key-file",
    );
}

#[test]
fn setup_waits_for_no_launch_before_changing_a_keyed_profile() {
    let home = setup_home();
    let daemon = common::Fixture::start();
    keyed_profile(&home, &daemon);
    // A launch preparing to start holds this lock before it writes a record.
    let lock_path = home.path("tunnels").join("dot.native-run.lock");
    pseudolife_stdio::board::state::atomic_create(&lock_path, b"").unwrap();
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)
        .unwrap();
    lock.try_lock().unwrap();
    let profile = home.path("tunnels").join("dot.profile.json");
    let before = std::fs::read(&profile).unwrap();
    let ambient = Listener::new();
    let output = command(&home, &ambient)
        .args([
            "setup",
            "--profile-dir",
            &home.dir(),
            "--tunnel-id",
            "tunnel_ef01",
            "--accept-access",
        ])
        .output()
        .unwrap();
    drop(lock);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(2), "{stderr}");
    assert!(
        stderr.contains("stop the tunnel first, then retry tunnel setup"),
        "{stderr}"
    );
    assert_eq!(std::fs::read(&profile).unwrap(), before);
    ambient.assert_untouched();
}

#[test]
fn setup_names_an_expiry_spelling_it_does_not_read_by_its_option() {
    let home = setup_home();
    let daemon = Listener::new();
    pending_setup_profile(&home, &daemon.url());
    assert_refused_with(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-expires-at",
            "2099-12-31T00:00:00.5Z",
        ],
        &[],
        "--key-expires-at is an expiry spelling this client does not read",
    );
    daemon.assert_untouched();
}

#[test]
fn setup_names_a_daemon_url_spelling_it_does_not_accept() {
    let home = setup_home();
    let token = home.path("token");
    assert_refused_with(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--daemon-url",
            "http://LOCALHOST:8123",
            "--token-file",
            token.to_str().unwrap(),
            "--accept-access",
        ],
        &[],
        "the URL in --daemon-url is a spelling this client does not accept",
    );
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
    assert_refused_with(
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
        "import the private key with --read-key or --key-file by itself",
    );
    let ambient = Listener::new();
    let mut setup = command(&home, &ambient);
    setup
        .args(["setup", "--profile-dir", &home.dir(), "--key-file"])
        .arg(&input);
    let output = setup.output().unwrap();
    let key = home.path("tunnels/dot.key");
    #[cfg(windows)]
    if refused_dpapi_in_ssh_logon(&output) {
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
    let repeat = setup.output().unwrap();
    assert_eq!(repeat.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&repeat.stdout).contains("already configured"));
    assert!(std::fs::read(&key).unwrap() == stored);
    ambient.assert_untouched();
}

#[cfg(windows)]
fn refused_dpapi_in_ssh_logon(output: &Output) -> bool {
    output.status.code() == Some(2)
        && std::env::var_os("SSH_CONNECTION").is_some()
        && std::env::var_os("GITHUB_ACTIONS").is_none()
        && String::from_utf8_lossy(&output.stderr).contains("Windows could not protect")
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
    let (daemon, gate) = gated_setup_daemon();
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
    let (daemon, gate) = gated_setup_daemon();
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
    assert_late_conflict(&home, &path, kind, &output);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("full"));
    assert!(!stdout.contains("Profile dot saved"));
    assert!(home.path("tunnels/.tunnel-setup.lock").is_file());
    assert_eq!(std::fs::read(profile).unwrap(), before);
    assert_eq!(std::fs::read(path).unwrap(), b"new runtime fixture");
    ambient.assert_untouched();
}

#[test]
fn setup_refuses_a_process_record_created_during_first_key_import() {
    let home = setup_home();
    let (daemon, gate) = gated_setup_daemon();
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url, "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
        }),
    );
    let profile = home.path("tunnels/dot.profile.json");
    let before = std::fs::read(&profile).unwrap();
    let record = home.path("tunnels").join("dot.process.json");
    let key_file = home.path("private-key-fixture");
    pseudolife_stdio::board::state::atomic_create(&key_file, b"fixture-runtime-key").unwrap();
    let ambient = Listener::new();
    let child = command(&home, &ambient)
        .args(["setup", "--profile-dir", &home.dir(), "--key-file"])
        .arg(&key_file)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let reached = wait_for_setup_tools_list(&daemon);
    if reached {
        pseudolife_stdio::board::state::atomic_create(&record, b"new runtime fixture").unwrap();
    }
    gate.release();
    let output = child.wait_with_output().unwrap();
    assert!(reached, "setup never reached tools/list");
    assert!(output.stdout.is_empty());
    assert!(!home.path("tunnels/dot.key").exists());
    assert_eq!(std::fs::read(profile).unwrap(), before);
    assert_eq!(std::fs::read(&record).unwrap(), b"new runtime fixture");
    ambient.assert_untouched();
    assert_late_conflict(&home, &record, "process record", &output);
    assert!(home.path("tunnels/.tunnel-setup.lock").is_file());
}

fn assert_late_conflict(home: &Home, path: &Path, kind: &str, output: &Output) {
    assert_eq!(output.status.code(), Some(2), "{}", path.display());
    assert_eq!(
        output.stderr,
        native_text(&format!(
            "a {kind} appeared during setup at {}. Check `pseudolife-mcp tunnel status --profile dot --profile-dir {}` before retrying\n",
            path.display(),
            shell_quoted(&home.dir())
        ))
    );
}

fn gated_setup_daemon() -> (common::Fixture, std::sync::Arc<common::ResponseGate>) {
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
    (daemon, gate)
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
fn setup_resuming_an_existing_profile_names_the_option_or_the_broken_file() {
    let home = Home::new();
    let dir = home.dir();
    seed_setup_profile(&home, &serde_json::json!({}));
    assert_refused_with(
        &home,
        &["setup", "--profile-dir", &dir],
        &[],
        "profile dot is saved; guided setup is not part of the native client.",
    );
    let broken = format!(
        "{}; fix that file or move it aside and save the profile again with `pseudolife-mcp tunnel setup --daemon-url <url> --token-file <owner-only file> --accept-access`",
        home.path("tunnels").join("dot.profile.json").display()
    );
    assert_refused_with(
        &home,
        &["setup", "--profile-dir", &dir, "--tunnel-id", "nope"],
        &[],
        &broken,
    );
    let daemon = Listener::new();
    assert_refused_with(
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
        &broken,
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

#[test]
fn setup_names_an_absolute_token_path_and_a_profile_not_ready_for_its_key() {
    let home = setup_home();
    let daemon = Listener::new();
    assert_refused_with(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--daemon-url",
            &daemon.url(),
            "--token-file",
            "~/token",
            "--accept-access",
        ],
        &[],
        "pass the daemon credential file as an absolute path",
    );
    seed_setup_profile(
        &home,
        &serde_json::json!({
            "name": "dot", "daemon_url": daemon.url(), "token_file": home.path("token"),
            "tunnel_id": "tunnel_abcd", "state": "pending", "consent": true,
        }),
    );
    let input = home.path("key-input");
    pseudolife_stdio::board::state::atomic_create(&input, b"fixture-runtime-key").unwrap();
    assert_refused_with(
        &home,
        &[
            "setup",
            "--profile-dir",
            &home.dir(),
            "--key-file",
            input.to_str().unwrap(),
        ],
        &[],
        "profile dot is not ready to take a runtime key (its saved state is pending)",
    );
    daemon.assert_untouched();
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
        assert_refused_with(
            &home,
            &["verify", "--profile-dir", &dir],
            &[],
            "cannot tell which launch the process record ",
        );
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
        // The same deep record as a private regular file: named.
        std::fs::remove_file(verification.join("challenge.json")).unwrap();
        private(
            &verification.join("challenge.json"),
            "[".repeat(150).as_bytes(),
        );
        assert_refused_with(
            &home,
            &["status", "--profile-dir", &dir],
            &[],
            "is nested too deeply to be one this client wrote; move that file aside",
        );
        // A FIFO is named, without blocking here.
        std::fs::remove_file(verification.join("challenge.json")).unwrap();
        let made = Command::new("mkfifo")
            .arg(verification.join("challenge.json"))
            .status();
        if made.is_ok_and(|status| status.success()) {
            let output = run_bounded(&home, &["status", "--profile-dir", &dir]);
            assert_eq!(output.status.code(), Some(2));
            assert!(
                String::from_utf8_lossy(&output.stderr)
                    .contains("cannot read the verification challenge")
            );
        }
    }

    #[test]
    fn a_registration_path_it_cannot_inspect_is_named() {
        let home = Home::new();
        let dir = home.dir();
        // CODEX_HOME names a regular file, so its config.toml cannot be
        // inspected: the refusal names that path, never the tunnel store.
        let codex = home.path("codex-file");
        std::fs::write(&codex, b"not a directory").unwrap();
        let config = codex.join("config.toml");
        assert_refused_with(
            &home,
            &["setup", "--profile-dir", &dir],
            &[("CODEX_HOME", codex.to_str().unwrap())],
            &format!(
                "cannot inspect {} to look for a client registration",
                config.display()
            ),
        );
    }

    #[test]
    fn a_key_file_in_a_form_it_does_not_read_is_named() {
        let home = Home::new();
        let dir = home.dir();
        profile(&home, "dot", true);
        let key = home.path("tunnels/dot.key");
        let made = Command::new("mkfifo").arg(&key).status();
        if !made.is_ok_and(|status| status.success()) {
            return; // no mkfifo on this host
        }
        let output = run_bounded(&home, &["status", "--profile-dir", &dir]);
        assert_eq!(output.status.code(), Some(2));
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains(&format!("cannot read the key file {}", key.display())),
            "{stderr}"
        );
    }

    #[test]
    fn non_text_arguments_and_environment_are_refused_with_the_cause() {
        use std::os::unix::ffi::OsStringExt;
        let home = Home::new();
        let dir = home.dir();
        let listener = Listener::new();
        let before = tree(&home.0);
        let output = command(&home, &listener)
            .arg("status")
            .arg(std::ffi::OsString::from_vec(vec![b'-', b'-', 0xff]))
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(
            String::from_utf8_lossy(&output.stderr)
                .starts_with("tunnel arguments must be valid text"),
        );
        let output = command(&home, &listener)
            .args(["setup", "--profile-dir", &dir])
            .env(
                "PSEUDOLIFE_MCP_TOKEN_FILE",
                std::ffi::OsString::from_vec(vec![b'/', 0xff]),
            )
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(
            String::from_utf8_lossy(&output.stderr)
                .contains("the environment variable PSEUDOLIFE_MCP_TOKEN_FILE is not valid text"),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(tree(&home.0) == before);
        listener.assert_untouched();
    }
}
