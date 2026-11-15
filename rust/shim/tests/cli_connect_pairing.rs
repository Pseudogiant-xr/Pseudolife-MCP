//! `connect --code` / `--read-code` end to end against a stand-in daemon in
//! a throwaway home: the code is redeemed only on an applying run, after
//! the plan; the paired token file is owner-only and kept whatever happens
//! next; refusals change nothing.
mod common;

use common::cli::{cleared_command, native_text};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Output, Stdio};
use std::sync::{Arc, Mutex};

const CODE: &str = "abcd-efgh-jkmn";

/// `connect --yes` re-points a Windows User-scope daemon URL it finds and
/// re-creates an unattended-update scheduled task it finds, and these tests
/// run the real binary against the real registry and Task Scheduler: never
/// on a host that has either.
fn refuse_a_host_with_a_user_scope_url() {
    #[cfg(windows)]
    {
        let reg = PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32")
            .join("reg.exe");
        for (key, scope) in [
            (r"HKCU\Environment", "User"),
            (
                r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                "Machine",
            ),
        ] {
            let found = std::process::Command::new(&reg)
                .args(["query", key, "/v", "PSEUDOLIFE_MCP_DAEMON_URL"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .unwrap();
            assert!(
                !found.success(),
                "this host has a {scope}-scope PSEUDOLIFE_MCP_DAEMON_URL: connect would re-point a User-scope one and report a Machine-scope one, so these runs need a host without either"
            );
        }
        let schtasks = PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32")
            .join("schtasks.exe");
        for task in ["Pseudolife Unattended Update", "Pseudolife Client Update"] {
            let found = std::process::Command::new(&schtasks)
                .args(["/Query", "/TN", task])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .unwrap();
            assert!(
                !found.success(),
                "this host has the scheduled task '{task}', which connect would re-create; these runs need a host without one"
            );
        }
    }
}

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        refuse_a_host_with_a_user_scope_url();
        let path = std::env::temp_dir().join(format!("connect-pairing-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(path.join("cwd")).unwrap();
        Self(path)
    }
    fn run(&self, args: &[&str], stdin: &[u8]) -> Output {
        let mut command = cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
        let home = &self.0;
        command
            .current_dir(home.join("cwd"))
            .env("HOME", home)
            .env("USERPROFILE", home)
            .env("APPDATA", home.join("AppData").join("Roaming"))
            .env("LOCALAPPDATA", home.join("AppData").join("Local"))
            .env("CODEX_HOME", home.join(".codex"))
            .env("XDG_CONFIG_HOME", home.join(".config"))
            .env("XDG_DATA_HOME", home.join(".local").join("share"))
            .arg("connect")
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn().unwrap();
        child.stdin.take().unwrap().write_all(stdin).unwrap();
        child.wait_with_output().unwrap()
    }
    fn claude(&self, url: &str) -> PathBuf {
        let path = self.0.join(".claude.json");
        let command = serde_json::to_string(self.0.join("shim-bin").to_str().unwrap()).unwrap();
        std::fs::write(
            &path,
            format!(
                "{{\n  \"mcpServers\": {{\n    \"pseudolife-memory\": {{\n      \"command\": {command},\n      \"env\": {{\n        \"PSEUDOLIFE_MCP_DAEMON_URL\": \"{url}\"\n      }}\n    }}\n  }}\n}}\n"
            ),
        )
        .unwrap();
        path
    }
    fn tree(&self) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
        fn walk(dir: &Path, out: &mut BTreeMap<PathBuf, Option<Vec<u8>>>) {
            for entry in std::fs::read_dir(dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    out.insert(path.clone(), None);
                    walk(&path, out);
                } else {
                    out.insert(path.clone(), Some(std::fs::read(&path).unwrap()));
                }
            }
        }
        let mut out = BTreeMap::new();
        walk(&self.0, &mut out);
        out
    }
    fn token_files(&self) -> Vec<PathBuf> {
        let mut found: Vec<PathBuf> = std::fs::read_dir(self.0.join(".pseudolife-mcp"))
            .map(|entries| entries.map(|e| e.unwrap().path()).collect())
            .unwrap_or_default();
        found.retain(|p| p.extension().is_some_and(|e| e == "token"));
        found
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// `/health`, `POST /api/pair` (accepting any code once) and the
/// bearer-checked episodes probe; every request is recorded.
struct Daemon {
    url: String,
    seen: Arc<Mutex<Vec<(String, String)>>>,
}
fn daemon(health: &'static str) -> Daemon {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let record = Arc::clone(&seen);
    std::thread::spawn(move || {
        let mut paired: Option<String> = None;
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            if reader.read_line(&mut line).is_err() {
                continue;
            }
            let mut parts = line.split_whitespace();
            let (method, path) = (
                parts.next().unwrap_or("").to_owned(),
                parts.next().unwrap_or("").to_owned(),
            );
            let (mut length, mut authorization) = (0, String::new());
            loop {
                let mut header = String::new();
                if reader.read_line(&mut header).unwrap_or(0) == 0 {
                    break;
                }
                let header = header.trim_end();
                if header.is_empty() {
                    break;
                }
                if let Some((name, value)) = header.split_once(':') {
                    match name.to_ascii_lowercase().as_str() {
                        "content-length" => length = value.trim().parse().unwrap_or(0),
                        "authorization" => authorization = value.trim().to_owned(),
                        _ => {}
                    }
                }
            }
            let mut body = vec![0; length];
            let _ = reader.read_exact(&mut body);
            let body = String::from_utf8_lossy(&body).into_owned();
            record.lock().unwrap().push((method.clone(), path.clone()));
            let (status, payload) = match (method.as_str(), path.as_str()) {
                ("GET", "/health") => (200, health.to_owned()),
                ("POST", "/api/pair") => {
                    let hash = body.split('"').nth(7).unwrap_or("").to_owned();
                    paired.get_or_insert(hash);
                    (
                        200,
                        r#"{"principal": "laptop", "tier": "writer", "bank": "b1"}"#.to_owned(),
                    )
                }
                ("GET", "/api/episodes?limit=1") => {
                    let token = authorization.trim_start_matches("Bearer ");
                    let hash: String = Sha256::digest(token.as_bytes())
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect();
                    if paired.as_deref() == Some(hash.as_str()) {
                        (200, r#"{"episodes": []}"#.to_owned())
                    } else {
                        (401, r#"{"error": "unauthorized"}"#.to_owned())
                    }
                }
                _ => (404, r#"{"error": "not_found"}"#.to_owned()),
            };
            let mut stream = stream;
            let _ = write!(
                stream,
                "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{payload}",
                payload.len()
            );
        }
    });
    Daemon { url, seen }
}
impl Daemon {
    fn paths(&self) -> Vec<String> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .map(|(m, p)| format!("{m} {p}"))
            .collect()
    }
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).replace("\r\n", "\n")
}

#[test]
fn a_dry_run_plans_the_paired_token_and_never_redeems_the_code() {
    let home = Home::new();
    let fixture = daemon(r#"{"status": "ok", "auth": true, "version": "0.0.0-fixture"}"#);
    home.claude("http://100.64.0.1:8765");
    let before = home.tree();
    for (args, stdin) in [
        (vec!["--code", CODE], &b""[..]),
        (vec!["--read-code"], &b"ABCD-EFGH-JKMN\n"[..]),
    ] {
        let mut argv = vec![fixture.url.as_str(), "--client", "claude-code", "--dry-run"];
        argv.extend(args);
        let output = home.run(&argv, stdin);
        let stdout = text(&output.stdout);
        assert_eq!(output.status.code(), Some(0), "{output:?}");
        assert!(
            stdout.contains("PSEUDOLIFE_MCP_TOKEN_FILE: (unset) -> ~/.pseudolife-mcp/<principal>.token (name from the daemon)"),
            "{stdout}"
        );
        assert!(stdout.ends_with("dry run: nothing was written and no token was sent; the credentials are verified only on a real run; the pairing code was not redeemed\n"), "{stdout}");
        // The code itself is never shown, in any spelling.
        let shown = format!("{stdout}{}", text(&output.stderr)).to_ascii_lowercase();
        assert!(
            !shown.contains(CODE) && !shown.contains("abcdefghjkmn"),
            "{shown}"
        );
    }
    let json = home.run(
        &[
            &fixture.url,
            "--client",
            "claude-code",
            "--dry-run",
            "--json",
            "--code",
            CODE,
        ],
        b"",
    );
    let shown = format!("{}{}", text(&json.stdout), text(&json.stderr)).to_ascii_lowercase();
    assert!(
        !shown.contains(CODE) && !shown.contains("abcdefghjkmn"),
        "{shown}"
    );
    assert!(
        fixture.paths().iter().all(|p| p == "GET /health"),
        "{:?}",
        fixture.paths()
    );
    assert_eq!(home.tree(), before);
}

#[test]
fn an_applying_run_redeems_once_keeps_the_token_and_names_it_on_a_later_refusal() {
    let home = Home::new();
    let fixture = daemon(r#"{"status": "ok", "auth": true, "version": "0.0.0-fixture"}"#);
    let config = home.claude("http://100.64.0.1:8765");
    let before = std::fs::read(&config).unwrap();
    let output = home.run(
        &[
            &fixture.url,
            "--code",
            CODE,
            "--client",
            "claude-code",
            "--yes",
        ],
        b"",
    );
    let stdout = text(&output.stdout);
    let stderr = text(&output.stderr);
    // The stand-in answers no MCP handshake, so verification refuses after
    // pairing: nothing is written, and the paired token file is kept.
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    let files = home.token_files();
    assert_eq!(files.len(), 1, "{files:?}");
    let paired = files[0].to_str().unwrap().to_owned();
    assert!(
        stdout.contains(&format!(
            "paired: laptop on {}; token file {paired}\n",
            fixture.url
        )),
        "{stdout}"
    );
    assert!(
        stderr.contains(&format!("Nothing was written. The paired token file {paired} is kept: the code is spent, and the daemon accepts this token")),
        "{stderr}"
    );
    assert_eq!(std::fs::read(&config).unwrap(), before);
    assert_eq!(
        fixture
            .paths()
            .iter()
            .filter(|p| *p == "POST /api/pair")
            .count(),
        1
    );
    // Owner-only, as the shim's own reader requires.
    let provider =
        pseudolife_stdio::credentials::CredentialProvider::new(None, Some(files[0].clone()))
            .unwrap();
    assert!(provider.snapshot().is_ok());
}

#[test]
fn pairing_needs_an_authenticating_daemon() {
    let home = Home::new();
    let fixture = daemon(r#"{"status": "ok", "auth": false, "version": "0.0.0-fixture"}"#);
    home.claude("http://100.64.0.1:8765");
    let before = home.tree();
    let output = home.run(&[&fixture.url, "--code", CODE, "--yes"], b"");
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert_eq!(
        text(&output.stderr),
        text(&native_text(&format!(
            "connect: the daemon at {} does not report authentication, and pairing needs a daemon with a bearer token. Nothing was changed\n",
            fixture.url
        )))
    );
    assert_eq!(home.tree(), before);
    assert!(fixture.paths().iter().all(|p| p == "GET /health"));
}

#[test]
fn pairing_usage_refusals_change_nothing_and_send_nothing() {
    let home = Home::new();
    let fixture = daemon(r#"{"status": "ok", "auth": true}"#);
    let existing = home.0.join("existing.token");
    std::fs::write(&existing, "x").unwrap();
    let existing_text = existing.to_str().unwrap().to_owned();
    let before = home.tree();
    for (args, stdin, message) in [
        (
            vec!["--code", CODE, "--read-token", "--token-file", "t.token"],
            &b""[..],
            "--code/--read-code pair a new token and --read-token stores one you already have; pass one of them".to_owned(),
        ),
        (
            vec!["--code", CODE, "--read-code"],
            &b""[..],
            "give the pairing code with --code or --read-code, not both".to_owned(),
        ),
        (
            vec!["--code", "not-a-code"],
            &b""[..],
            "that is not a pairing code: it has 12 letters and digits, shown as XXXX-XXXX-XXXX (nothing was changed)".to_owned(),
        ),
        (
            vec!["--read-code"],
            &b"\n"[..],
            "that is not a pairing code: it has 12 letters and digits, shown as XXXX-XXXX-XXXX (nothing was changed)".to_owned(),
        ),
        (
            vec!["--code", CODE, "--token-file", existing_text.as_str()],
            &b""[..],
            format!("{existing_text} already exists; pairing creates a token file but never replaces one"),
        ),
    ] {
        let mut argv = vec![fixture.url.as_str(), "--yes"];
        argv.extend(args);
        let output = home.run(&argv, stdin);
        assert_eq!(output.status.code(), Some(2), "{argv:?}: {output:?}");
        assert_eq!(text(&output.stderr), text(&native_text(&format!("connect: {message}\n"))), "{argv:?}");
    }
    assert_eq!(home.tree(), before);
    assert!(fixture.paths().is_empty(), "{:?}", fixture.paths());
}
