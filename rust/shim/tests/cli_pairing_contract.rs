#![forbid(unsafe_code)]
//! `invite` and `pair` without a database: canonical refusals pinned to the
//! oracle's text, deferrals before any effect, and `pair` end to end against
//! an in-test stand-in daemon (the owner-only token file, the hash-only
//! redemption, the kept file of an unknown outcome). Database-backed
//! `invite` behaviour is proven by the differential harness
//! (`rust/cli_harness/rows/invite.py`).
#[path = "common/cli.rs"]
mod cli;
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::{Arc, Mutex};

const DEFER_INVITE: &str = "pseudolife-stdio: mode 'invite' is deferred in this candidate\n";
const DEFER_PAIR: &str = "pseudolife-stdio: mode 'pair' is deferred in this candidate\n";

/// An argv and the environment variables a deferral case adds.
type Shape<'a> = (Vec<&'a str>, Vec<(&'a str, PathBuf)>);

struct Home(PathBuf);
impl Home {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!("pairing-{label}-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn entries(&self) -> Vec<String> {
        let mut found = Vec::new();
        let mut stack = vec![self.0.clone()];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                found.push(path.strip_prefix(&self.0).unwrap().display().to_string());
                if path.is_dir() {
                    stack.push(path);
                }
            }
        }
        found.sort();
        found
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn command(home: &Path, mode: &str) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg(mode)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PATH", home.join("no-tools"))
        .env("PSEUDOLIFE_MCP_DATA_DIR", home.join("no-bank"))
        .env("ProgramW6432", home.join("no-programs"))
        .current_dir(home);
    command
}

fn run(mut command: Command, stdin: &[u8]) -> Output {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().unwrap();
    child.stdin.take().unwrap().write_all(stdin).unwrap();
    child.wait_with_output().unwrap()
}

fn assert_output(output: &Output, code: i32, stdout: &str, stderr: &str) {
    assert_eq!(
        (
            output.status.code(),
            String::from_utf8_lossy(&output.stdout).into_owned(),
            String::from_utf8_lossy(&output.stderr).into_owned()
        ),
        (
            Some(code),
            String::from_utf8(cli::native_text(stdout)).unwrap(),
            String::from_utf8(cli::native_text(stderr)).unwrap()
        )
    );
}

#[test]
fn invite_refusals_before_the_database_match_the_oracle() {
    let home = Home::new("invite-refusals");
    let cases: [(&[&str], i32, &str, &str); 6] = [
        (
            &[],
            2,
            "",
            "invite: give one of: a name to invite, --list, or --revoke NAME\n",
        ),
        (
            &["Laptop"],
            2,
            "",
            "invite: 'Laptop' is not a principal name: lowercase letters, digits, '.', '_' and '-', starting with a letter or digit, at most 64 characters\n",
        ),
        (
            &["daemon"],
            4,
            "",
            "invite: 'daemon' is reserved; choose another name\n",
        ),
        (
            &["--revoke", "alpha"],
            2,
            "",
            "invite: not interactive: re-run with --yes to revoke\n",
        ),
        (&["--version-check"], 0, "pseudolife-mcp invite 1\n", ""),
        (
            &["--list", "--revoke", "a", "--json"],
            2,
            "{\n  \"error\": \"give one of: a name to invite, --list, or --revoke NAME\",\n  \"notes\": [],\n  \"exit\": 2\n}\n",
            "",
        ),
    ];
    for (argv, code, stdout, stderr) in cases {
        let output = run(
            {
                let mut command = command(&home.0, "invite");
                command.args(argv);
                command
            },
            b"",
        );
        assert_output(&output, code, stdout, stderr);
    }
    let mut tokens = command(&home.0, "invite");
    tokens
        .args(["laptop", "--url", "u"])
        .env("PSEUDOLIFE_MCP_TOKENS", "tok:Laptop");
    assert_output(
        &run(tokens, b""),
        4,
        "",
        "invite: laptop already has a token in PSEUDOLIFE_MCP_TOKENS; an environment principal keeps working as it is, and a stored one of the same name would be shadowed by it\n",
    );
    assert_eq!(home.entries(), Vec::<String>::new());
}

#[test]
fn invite_defers_before_any_effect() {
    let home = Home::new("invite-defer");
    // An address nothing listens on: a deferral must come before any use.
    let dsn = "postgresql://nobody:nothing@127.0.0.1:9/pl_cf_unused";
    std::fs::create_dir(home.0.join("tools")).unwrap();
    std::fs::write(home.0.join("tools").join("tailscale"), b"stand-in").unwrap();
    std::fs::create_dir(home.0.join("data")).unwrap();
    std::fs::write(
        home.0.join("data").join("config.yaml"),
        b"coordination: {}\n",
    )
    .unwrap();
    let before = home.entries();
    let shapes: Vec<Shape> = vec![
        (vec!["laptop", "--url", "u"], vec![]),
        (vec!["--list"], vec![]),
        (vec!["--help"], vec![]),
        (vec!["laptop", "--expires", "30s"], vec![]),
        (vec!["laptop", "--tier", "writer"], vec![]),
        (vec!["laptop", "--url=u"], vec![]),
        (vec!["laptop", "--json", "--json"], vec![]),
        (vec!["laptop"], vec![("PATH", home.0.join("tools"))]),
        (
            vec!["laptop", "--url", "u"],
            vec![("PSEUDOLIFE_MCP_DATA_DIR", home.0.join("data"))],
        ),
    ];
    for (index, (argv, extra)) in shapes.into_iter().enumerate() {
        let mut command = command(&home.0, "invite");
        command.args(&argv);
        if index > 0 {
            command.env("PSEUDOLIFE_MCP_DATABASE_URL", dsn);
        }
        for (name, value) in extra {
            command.env(name, value);
        }
        assert_output(&run(command, b""), 1, "", DEFER_INVITE);
    }
    let mut malformed = command(&home.0, "invite");
    malformed
        .args(["laptop", "--url", "u"])
        .env("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
        .env("PSEUDOLIFE_MCP_TOKENS", "no-separator");
    assert_output(&run(malformed, b""), 1, "", DEFER_INVITE);
    assert_eq!(home.entries(), before);
}

/// A stand-in daemon: `/health`, `POST /api/pair` (answers consumed in
/// order: "pair", "drop" or a status) and the bearer-checked episodes probe.
struct Daemon {
    url: String,
    seen: Arc<Mutex<Vec<(String, String, String)>>>,
}

fn daemon(health: &'static str, answers: Vec<&'static str>) -> Daemon {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let record = Arc::clone(&seen);
    std::thread::spawn(move || {
        let mut answers = answers.into_iter();
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
                reader.read_line(&mut header).unwrap();
                let header = header.trim_end();
                if header.is_empty() {
                    break;
                }
                let (name, value) = header.split_once(':').unwrap();
                match name.to_ascii_lowercase().as_str() {
                    "content-length" => length = value.trim().parse().unwrap(),
                    "authorization" => authorization = value.trim().to_owned(),
                    "origin" => panic!("pair sent an Origin header"),
                    _ => {}
                }
            }
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            let body = String::from_utf8(body).unwrap();
            record
                .lock()
                .unwrap()
                .push((method.clone(), path.clone(), body.clone()));
            let (status, payload) = match (method.as_str(), path.as_str()) {
                ("GET", "/health") => (200, health.to_owned()),
                ("POST", "/api/pair") => match answers.next().unwrap_or("pair") {
                    "drop" => continue,
                    "pair" => {
                        let hash = body.split('"').nth(7).unwrap().to_owned();
                        paired.get_or_insert(hash);
                        (
                            200,
                            r#"{"principal": "laptop", "tier": "writer", "bank": "b1"}"#.to_owned(),
                        )
                    }
                    status => (status.parse().unwrap(), r#"{"error": "x"}"#.to_owned()),
                },
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

fn token_files(home: &Path) -> Vec<PathBuf> {
    let directory = home.join(".pseudolife-mcp");
    let mut found: Vec<PathBuf> = std::fs::read_dir(&directory)
        .map(|entries| entries.map(|e| e.unwrap().path()).collect())
        .unwrap_or_default();
    found.sort();
    found
}

fn assert_owner_only(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    // The shim's own reader refuses a file that is not owner-only.
    let provider =
        pseudolife_stdio::credentials::CredentialProvider::new(None, Some(path.to_path_buf()))
            .unwrap();
    assert!(provider.snapshot().is_ok());
}

#[test]
fn pair_mints_an_owner_only_token_and_sends_only_its_hash() {
    let home = Home::new("pair-success");
    let fixture = daemon(r#"{"status": "ok", "auth": true}"#, vec!["pair"]);
    let mut pair = command(&home.0, "pair");
    pair.args([fixture.url.as_str(), "abcd-efgh-jkmn"]);
    let output = run(pair, b"");
    let file = home.0.join(".pseudolife-mcp").join("laptop.token");
    assert_output(
        &output,
        0,
        &format!(
            "paired: laptop (tier writer) on {}\ntoken file: {}\n",
            fixture.url,
            file.display()
        ),
        "",
    );
    assert_eq!(token_files(&home.0), vec![file.clone()]);
    let token = std::fs::read_to_string(&file).unwrap();
    assert_eq!(token.len(), 43);
    assert_owner_only(&file);
    let seen = fixture.seen.lock().unwrap().clone();
    let hash: String = Sha256::digest(token.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(
        seen.iter()
            .map(|(m, p, b)| (m.as_str(), p.as_str(), b.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("GET", "/health", ""),
            (
                "POST",
                "/api/pair",
                format!("{{\"code\": \"ABCDEFGHJKMN\", \"token_sha256\": \"{hash}\"}}").as_str()
            ),
            ("GET", "/api/episodes?limit=1", ""),
        ]
    );
}

#[test]
fn pair_refusals_remove_the_file_and_unknown_outcomes_keep_it() {
    let home = Home::new("pair-refused");
    let fixture = daemon(r#"{"status": "ok", "auth": true}"#, vec!["429"]);
    let mut pair = command(&home.0, "pair");
    pair.args([fixture.url.as_str(), "--read-code", "--json"]);
    let output = run(pair, b"abcd-efgh-jkmn\n");
    assert_output(
        &output,
        4,
        &format!(
            "{{\n  \"url\": \"{}\",\n  \"state\": \"rate_limited\",\n  \"principal\": null,\n  \"tier\": null,\n  \"bank\": null,\n  \"token_file\": null,\n  \"warnings\": [],\n  \"error\": \"pairing is rate-limited on the daemon, try again in a minute\",\n  \"exit\": 4\n}}\n",
            fixture.url
        ),
        "",
    );
    assert!(token_files(&home.0).is_empty());

    let fixture = daemon(r#"{"status": "ok", "auth": true}"#, vec!["drop", "400"]);
    let mut pair = command(&home.0, "pair");
    pair.args([fixture.url.as_str(), "abcd-efgh-jkmn"]);
    let output = run(pair, b"");
    let kept = token_files(&home.0);
    assert_eq!(kept.len(), 1);
    let name = kept[0].file_name().unwrap().to_str().unwrap().to_owned();
    assert!(name.starts_with("pairing-") && name.ends_with(".token") && name.len() == 22);
    let path = kept[0].display().to_string();
    assert_output(
        &output,
        5,
        "",
        &format!(
            "pair: the daemon then answered HTTP 400, after an attempt whose answer was lost. {path} is kept: the code may already be spent, and this file is the only copy of a token the daemon may now accept. Re-run `pseudolife-mcp connect <url> --token-file {path}` once the daemon answers; delete the file only if the operator re-invites this machine\n"
        ),
    );
    assert_owner_only(&kept[0]);
}

#[test]
fn pair_refusals_before_any_change_match_the_oracle() {
    let home = Home::new("pair-usage");
    let fixture = daemon(r#"{"status": "ok", "auth": "yes"}"#, vec![]);
    let cases: Vec<(Vec<String>, &[u8], i32, String)> = vec![
        (
            vec!["ftp://user:pw@Host:21/x".into(), "c".into()],
            b"",
            2,
            "pair: the daemon URL given (ftp://host:21) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials\n".into(),
        ),
        (
            vec![fixture.url.clone()],
            b"",
            2,
            "pair: no pairing code: give it as an argument, or pass --read-code and write it on stdin\n".into(),
        ),
        (
            vec![fixture.url.clone(), "--read-code".into()],
            b" \n",
            2,
            "pair: --read-code read no code on stdin\n".into(),
        ),
        (
            vec![fixture.url.clone(), "ABCD-EFGH-JKMU".into()],
            b"",
            2,
            "pair: that is not a pairing code: it has 12 letters and digits, shown as XXXX-XXXX-XXXX\n".into(),
        ),
        (
            vec![fixture.url.clone(), "abcd-efgh-jkmn".into()],
            b"",
            4,
            format!("pair: the daemon at {} does not report authentication (auth: \"yes\"); pairing needs a daemon with a bearer token. Nothing was changed\n", fixture.url),
        ),
    ];
    for (argv, stdin, code, stderr) in cases {
        let mut pair = command(&home.0, "pair");
        pair.args(&argv);
        assert_output(&run(pair, stdin), code, "", &stderr);
    }
    assert!(token_files(&home.0).is_empty());
}

#[test]
fn pair_defers_before_any_effect() {
    let home = Home::new("pair-defer");
    let fixture = daemon(r#"{"status": "ok", "auth": true}"#, vec![]);
    let shapes: Vec<Vec<String>> = vec![
        vec![],
        vec!["--help".into()],
        vec![
            fixture.url.clone(),
            "--json".into(),
            "abcd-efgh-jkmn".into(),
        ],
        vec![
            fixture.url.clone(),
            "abcd-efgh-jkmn".into(),
            "--token-file=x".into(),
        ],
        vec![
            fixture.url.replace("http:", "https:"),
            "abcd-efgh-jkmn".into(),
        ],
        vec![fixture.url.clone(), "abcd-efgh-jkm\u{f1}".into()],
    ];
    for argv in shapes {
        let mut pair = command(&home.0, "pair");
        pair.args(&argv);
        assert_output(&run(pair, b""), 1, "", DEFER_PAIR);
    }
    let mut proxied = command(&home.0, "pair");
    proxied
        .args([fixture.url.as_str(), "abcd-efgh-jkmn"])
        .env("HTTPS_PROXY", "http://127.0.0.1:9");
    assert_output(&run(proxied, b""), 1, "", DEFER_PAIR);
    assert!(fixture.seen.lock().unwrap().is_empty());
    assert_eq!(home.entries(), Vec::<String>::new());
}
