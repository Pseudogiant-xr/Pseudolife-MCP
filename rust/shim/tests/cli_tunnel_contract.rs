#![forbid(unsafe_code)]
//! `tunnel`: file-only answers, refusals before any effect, and deferrals
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
    let token = home.path("token");
    let token = token.to_str().unwrap();
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
        &["setup", "--read-key", "--profile-dir", &dir],
        &["setup", "--key-file", token, "--profile-dir", &dir],
        &["setup", "--start", "--profile-dir", &dir],
        &["setup", "--open-browser", "keys", "--profile-dir", &dir],
        &["setup", "--catalog", "all", "--profile-dir", &dir],
    ] {
        assert_deferred(&home, args, &[]);
    }
}

#[test]
fn a_setup_that_validates_defers_before_the_daemon_handshake() {
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
        assert_deferred(&home, &args, &[]);
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

#[test]
fn setup_resuming_an_existing_profile_defers() {
    // The main real flow: anything saved under the name means load and
    // resume (handshake next), never a native answer.
    let home = Home::new();
    let dir = home.dir();
    std::fs::create_dir_all(home.path("tunnels")).unwrap();
    std::fs::write(home.path("tunnels/dot.profile.json"), b"{}").unwrap();
    assert_deferred(&home, &["setup", "--profile-dir", &dir], &[]);
    assert_deferred(
        &home,
        &["setup", "--profile-dir", &dir, "--tunnel-id", "nope"],
        &[],
    );
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
