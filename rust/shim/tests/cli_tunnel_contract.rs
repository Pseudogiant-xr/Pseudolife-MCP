#![forbid(unsafe_code)]
//! `tunnel`: file-only answers, refusals before any effect, and deferrals
//! that change nothing and open no connection. Seeded profiles need
//! owner-only files: those cells run on Unix (chmod); the differential
//! harness row covers both platforms against the Python oracle.
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
        &["shim", "--profile-dir", &dir],
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
    // A client registration exists: its env block is not read natively.
    std::fs::write(home.path(".claude.json"), b"{}").unwrap();
    assert_deferred(&home, &["setup", "--profile-dir", &dir], &[]);
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
            "http://127.0.0.1:8765".into(),
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
                "{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:8765\", \"token_file\": \"{}\", \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true}}",
                token.display()
            )
        } else {
            format!(
                "{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:8765\", \"token_file\": \"{}\"}}",
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
    fn verify_and_update_refuse_or_defer_by_profile_readiness() {
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
        let output = run(&home, &["update", "--profile-dir", &dir], &[]);
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "{\"state\": \"current\", \"needs_attention\": false, \"detail\": \"2 saved tunnel profiles checked; local client registrations preserved\", \"profiles\": [{\"profile\": \"alpha\", \"state\": \"pending\"}, {\"profile\": \"dot\", \"state\": \"pending\"}]}\n"
        );
        private(&home.path("tunnels/dot.key"), b"PLAIN\0fixture-tunnel-key");
        assert_refused(&home, &["verify", "--profile-dir", &dir], &[], UNAVAILABLE);
        assert_deferred(&home, &["update", "--profile-dir", &dir], &[]);
        private(&home.path("tunnels/dot.process.json"), b"{}");
        assert_deferred(&home, &["verify", "--profile-dir", &dir], &[]);
        assert_deferred(&home, &["status", "--profile-dir", &dir], &[]);
    }
}
