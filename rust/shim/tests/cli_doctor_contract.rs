#![forbid(unsafe_code)]
mod common;
use common::{LeaseHome as Home, cli::native_text};
use std::{
    ffi::OsStr,
    process::{Command, Output},
};

const REFUSAL: &str = "{\"ok\": false, \"error\": \"ExplicitDisposableDatabaseRequired\", \"recovery\": \"Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used.\"}\n";

fn command(home: &Home) -> Command {
    let mut command = home.command();
    command
        .env("COLUMNS", "80")
        .env("CODEX_HOME", home.0.join("codex"))
        .env("CLAUDE_CONFIG_DIR", home.0.join("claude"))
        .env("XDG_CONFIG_HOME", home.0.join("config"));
    command
}

fn refusal(output: Output) {
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(output.stdout, native_text(REFUSAL));
    assert!(output.stderr.is_empty());
}

fn deferred(output: Output) {
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native_text("pseudolife-stdio: mode 'doctor' is deferred in this candidate\n")
    );
}

#[test]
fn original_absent_and_empty_disposable_dsn_cells() {
    let home = Home::new();
    for dsn in [None, Some(OsStr::new(""))] {
        let mut command = command(&home);
        if let Some(dsn) = dsn {
            command.env("PSEUDOLIFE_TEST_DATABASE_URL", dsn);
        }
        refusal(
            command
                .args(["doctor", "--disposable-proof"])
                .output()
                .unwrap(),
        );
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
}

#[test]
fn saved_state_proof_requests_are_usage_errors_without_mutation() {
    let home = Home::new();
    let path = home.0.join("owned-state.json");
    let sentinel = b"owned state sentinel\n";
    std::fs::write(&path, sentinel).unwrap();
    for dsn in ["", "postgresql://fixture.invalid/disposable"] {
        for proof_first in [false, true] {
            let mut command = command(&home);
            command
                .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
                .arg("doctor");
            if proof_first {
                command.arg("--disposable-proof");
            }
            command.arg("--agent-state").arg(&path);
            if !proof_first {
                command.arg("--disposable-proof");
            }
            let output = command.output().unwrap();
            assert_eq!(output.status.code(), Some(2));
            assert!(output.stdout.is_empty());
            assert!(
                String::from_utf8_lossy(&output.stderr)
                    .contains("--disposable-proof cannot use a saved --agent-state")
            );
        }
    }
    assert_eq!(std::fs::read(&path).unwrap(), sentinel);
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 1);
}

#[test]
fn canonical_options_allow_any_order_and_the_four_hosts() {
    let home = Home::new();
    for host in ["codex", "claude-code", "claude-desktop", "generic"] {
        for arguments in [
            vec!["--disposable-proof", "--timeout", "20", "--host", host],
            vec!["--disposable-proof", "--host", host, "--timeout", "20"],
            vec!["--timeout", "20", "--disposable-proof", "--host", host],
            vec!["--timeout", "20", "--host", host, "--disposable-proof"],
            vec!["--host", host, "--disposable-proof", "--timeout", "20"],
            vec!["--host", host, "--timeout", "20", "--disposable-proof"],
        ] {
            refusal(
                command(&home)
                    .arg("doctor")
                    .args(arguments)
                    .output()
                    .unwrap(),
            );
        }
    }
    for timeout in ["0.5", ".5", "1.", "00020.0"] {
        refusal(
            command(&home)
                .args(["doctor", "--timeout", timeout, "--disposable-proof"])
                .output()
                .unwrap(),
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn noncanonical_arguments_defer_without_parser_diagnostics() {
    let home = Home::new();
    for arguments in [
        vec!["--time", "20"],
        vec!["--timeout=20"],
        vec!["--host=generic"],
        vec!["--agent-state=owned-state.json"],
        vec!["--disposable-proof=false"],
        vec!["--dispos"],
        vec!["--h"],
        vec!["--help"],
        vec!["-h"],
        vec!["--"],
        vec!["unexpected"],
        vec!["--host", "invalid"],
        vec!["--host"],
        vec!["--timeout"],
        vec!["--agent-state"],
        vec!["--agent-state", ""],
        vec!["--agent-state", "-owned-state.json"],
        vec!["--disposable-proof"],
        vec!["--timeout", "20", "--timeout", "30"],
        vec!["--host", "generic", "--host", "codex"],
    ] {
        deferred(
            command(&home)
                .args(["doctor", "--disposable-proof"])
                .args(arguments)
                .output()
                .unwrap(),
        );
    }
    let overflow = "9".repeat(400);
    for timeout in [
        "", ".", "0", "-1", "-1\n", "-.5\n", "+20", "2_0", "nan", "inf", "1e2", " 20", "20\n",
        "1.2.3", "２０", &overflow,
    ] {
        deferred(
            command(&home)
                .args(["doctor", "--disposable-proof", "--timeout", timeout])
                .output()
                .unwrap(),
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn nonempty_proof_requires_native_prerequisites_and_help_stays_deferred() {
    let home = Home::new();
    deferred(
        command(&home)
            .args(["doctor", "--disposable-proof", "--help"])
            .output()
            .unwrap(),
    );
    for dsn in [" ", "postgresql://fixture.invalid/disposable"] {
        let output = command(&home)
            .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
            .args(["doctor", "--disposable-proof"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(
            ["NativeDaemonRequired", "ProvisionedModelsRequired"]
                .contains(&report["error"].as_str().unwrap())
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn non_unicode_nonempty_dsn_stays_deferred() {
    #[cfg(unix)]
    let dsn = {
        use std::os::unix::ffi::OsStringExt;
        std::ffi::OsString::from_vec(vec![0xff])
    };
    #[cfg(windows)]
    let dsn = {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[0xd800])
    };
    let home = Home::new();
    deferred(
        command(&home)
            .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
            .args(["doctor", "--disposable-proof"])
            .output()
            .unwrap(),
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

// --- read-only diagnostics -----------------------------------------------------

use common::{Fixture, Reply, Setup};
use serde_json::{Value, json};

const DEFERRED: &str = "pseudolife-stdio: mode 'doctor' is deferred in this candidate\n";

/// Every entry under `root`, with file bytes, so a run can be shown to
/// leave the home as it found it.
fn tree(root: &std::path::Path) -> Vec<(String, Option<Vec<u8>>)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            let rel = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            if path.is_dir() {
                stack.push(path);
                out.push((rel, None));
            } else {
                out.push((rel, Some(std::fs::read(&path).unwrap())));
            }
        }
    }
    out.sort();
    out
}

/// `doctor` against `url`, with a PATH that names an empty directory and
/// the home as working directory.
fn diagnose(home: &Home, url: &str) -> Command {
    let mut command = command(home);
    command
        .env("PSEUDOLIFE_MCP_DAEMON_URL", url)
        .env("PATH", home.0.join("empty-bin"))
        .current_dir(&home.0)
        .arg("doctor");
    command
}

fn report(output: &Output) -> serde_json::Map<String, Value> {
    let text = String::from_utf8(output.stdout.clone()).unwrap();
    assert_eq!(output.stdout, native_text(&text.replace("\r\n", "\n")));
    let text = text.replace("\r\n", "\n");
    // json.dumps(indent=2) and print's newline.
    assert!(
        text.starts_with("{\n  \"ok\": ") && text.ends_with("\n}\n"),
        "{text}"
    );
    let Value::Object(map) = serde_json::from_str(&text).unwrap() else {
        panic!("not an object: {text}");
    };
    map
}

const GIT_BASH_KEYS: [&str; 4] = [
    "git_bash",
    "bash_on_path",
    "bash_on_path_is_wsl_launcher",
    "git_bash_recovery",
];

fn keys(map: &serde_json::Map<String, Value>) -> Vec<&str> {
    map.keys()
        .map(String::as_str)
        .filter(|key| !GIT_BASH_KEYS.contains(key))
        .collect()
}

fn offline_coordination(host: &str) -> Value {
    let (wake, last) = match host {
        "generic" => (
            "unsupported",
            "No host selected and no configured wake path detected; use --host claude-code or --host codex to diagnose that client's wake setup.",
        ),
        "claude-desktop" => (
            "unsupported",
            "Claude Desktop shares its MCP process across conversations; use a supported session host for a mailbox.",
        ),
        _ => (
            "unavailable",
            "This host has no configured idle wake path; check its stop_hook or doorbell reason above and enable that client's plugin or wake setting, or pull mail on the next turn.",
        ),
    };
    json!({
        "daemon": "offline", "health": "not_verified", "authentication": "offline",
        "registration": if host == "claude-desktop" { "unsupported_host" } else { "not_checked" },
        "transport": {"coordination_tools": "not_checked", "mailbox_pull": "not_verified"},
        "host": host, "wake": {"state": wake, "evidence": "configuration_only"},
        "delivery": "unverified",
        "next": ["Start the intended daemon and retry; HTTP health does not prove mail delivery.", last],
    })
}

#[test]
fn an_unreachable_daemon_is_reported_without_touching_the_home() {
    for host in ["generic", "codex", "claude-code", "claude-desktop"] {
        let home = Home::new();
        let output = diagnose(&home, "http://127.0.0.1:1")
            .args(["--host", host, "--timeout", "5"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stderr.is_empty(), "{:?}", output.stderr);
        let map = report(&output);
        assert_eq!(
            keys(&map),
            [
                "ok",
                "interpreter",
                "source",
                "pseudolife-mcp",
                "credential_source",
                "token_sha256_prefix",
                "board",
                "maintainer_passkeys",
                "daemon_status",
                "dreams",
                "error",
                "recovery",
                "wake",
                "coordination",
                "path_resolution",
            ]
        );
        assert_eq!(map["ok"], json!(false));
        assert_eq!(
            map["interpreter"],
            json!(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        );
        assert_eq!(map["pseudolife-mcp"], json!(env!("CARGO_PKG_VERSION")));
        // Maintainer decision 2026-10-09: no MCP SDK line in the native report.
        assert!(!map.contains_key("mcp"));
        assert_eq!(map["credential_source"], json!("none"));
        assert_eq!(
            map["board"],
            json!(
                "off - no bearer token, so the daemon has no principal to admit (open-loopback install)"
            )
        );
        assert_eq!(
            map["maintainer_passkeys"],
            json!({"state": "not_checked", "line": "not checked - the board is off for this token"})
        );
        assert_eq!(map["daemon_status"], json!("unreachable"));
        assert_eq!(map["dreams"], json!("unknown"));
        assert_eq!(map["error"], json!("DaemonUnavailable"));
        assert_eq!(
            map["recovery"],
            json!("Start the intended daemon, then retry; doctor never starts one.")
        );
        assert_eq!(
            map["wake"],
            json!({"claude_code": {"registered": false}, "codex": {"registered": false},
                   "caps": "unknown (daemon unreachable)"})
        );
        assert_eq!(map["coordination"], offline_coordination(host));
        assert_eq!(
            map["path_resolution"],
            json!({"on_path": null, "launcher": null})
        );
        assert_eq!(cfg!(windows), map.contains_key("git_bash"));
        assert!(tree(&home.0).is_empty());
    }
}

#[test]
fn a_saved_instance_is_not_checked_while_the_board_is_off() {
    for host in ["generic", "claude-desktop"] {
        let home = Home::new();
        let output = diagnose(&home, "http://127.0.0.1:1")
            .args(["--host", host, "--agent-state"])
            .arg(home.0.join("absent.json"))
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        let map = report(&output);
        assert_eq!(map["ok"], json!(false));
        assert_eq!(
            map["coordination"]["registration"],
            json!(if host == "generic" {
                "not_checked"
            } else {
                "unsupported_host"
            })
        );
        assert!(tree(&home.0).is_empty());
    }
}

fn annotated(name: &str) -> Value {
    json!({"name": name, "inputSchema": {"type": "object"},
           "annotations": {"readOnlyHint": true}})
}

#[test]
fn a_healthy_daemon_is_checked_through_this_runtimes_own_shim() {
    let home = Home::new();
    let fixture = Fixture::start_with(Setup {
        lists: vec![Reply::Result(json!({"tools": [
            annotated("memory_search"), annotated("memory_agents"), annotated("memory_message"),
            {"name": "bare", "inputSchema": {"type": "object"}}]}))],
        ..Setup::default()
    });
    let output = diagnose(&home, &fixture.url).output().unwrap();
    let map = report(&output);
    assert_eq!(map["daemon_status"], json!("ok"));
    assert_eq!(map["dreams"], json!("no stall reported"));
    assert_eq!(map["daemon_version"], json!(env!("CARGO_PKG_VERSION")));
    assert_eq!(map["codex_hooks"], json!("not-configured"));
    assert_eq!(map["instructions_present"], json!(true));
    assert_eq!(map["tool_count"], json!(4));
    assert_eq!(map["tools_missing_annotations"], json!(["bare"]));
    assert_eq!(map["coordination_tools_present"], json!(true));
    assert_eq!(map["ok"], json!(false));
    assert_eq!(output.status.code(), Some(1));
    if map.get("git_bash") == Some(&Value::Null) {
        // A Windows host without Git for Windows: GitBashMissing names the
        // failure and its recovery replaces the handshake's.
        assert_eq!(map["error"], json!("GitBashMissing"));
        assert_eq!(map["recovery"], map["git_bash_recovery"]);
    } else {
        assert!(!map.contains_key("error"));
        assert_eq!(
            map["recovery"],
            json!(
                "Check shim stderr and daemon MCP access, then compare daemon and shim versions; update the component missing instructions or annotations and reconnect."
            )
        );
    }
    assert_eq!(
        map["wake"]["caps"],
        json!("unknown (the daemon does not report them; update it)")
    );
    assert_eq!(
        map["coordination"]["transport"]["coordination_tools"],
        json!("advertised")
    );
    let records = fixture.records.lock().unwrap().clone();
    // Doctor's own probe comes first and carries no credential.
    assert_eq!(
        (records[0].verb.as_str(), records[0].path.as_str()),
        ("GET", "/health")
    );
    assert!(!records[0].headers.contains_key("authorization"));
    // urllib's request fields exactly: no Accept, identity encoding, close.
    let mut fields: Vec<(&str, &str)> = records[0]
        .headers
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    fields.sort();
    let host = fixture.url.trim_start_matches("http://");
    assert_eq!(
        fields,
        [
            ("accept-encoding", "identity"),
            ("connection", "close"),
            ("host", host),
            ("user-agent", "Python-urllib/3.11"),
        ]
    );
    let methods: Vec<&str> = records
        .iter()
        .filter_map(|record| record.message.get("method").and_then(Value::as_str))
        .collect();
    assert!(methods.contains(&"initialize") && methods.contains(&"tools/list"));
    // The only files are the shim's own handshake cache.
    for (rel, _) in tree(&home.0) {
        let rel = rel.replace('\\', "/");
        assert!(rel.starts_with(".pseudolife-mcp"), "unexpected file {rel}");
    }
}

/// A loopback daemon answering each request path with raw response bytes,
/// then holding the connection open for a while (a body that never comes).
struct RawDaemon {
    url: String,
    paths: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl RawDaemon {
    fn start(answer: fn(&str) -> (Vec<u8>, u64)) -> Self {
        use std::io::{Read, Write};
        use std::sync::atomic::Ordering;
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let paths = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (seen, halt) = (paths.clone(), stop.clone());
        let thread = std::thread::spawn(move || {
            while !halt.load(Ordering::SeqCst) {
                let Ok((mut stream, _)) = listener.accept() else {
                    std::thread::sleep(std::time::Duration::from_millis(5));
                    continue;
                };
                let (seen, halt) = (seen.clone(), halt.clone());
                std::thread::spawn(move || {
                    stream.set_nonblocking(false).unwrap();
                    let mut head = Vec::new();
                    let mut byte = [0u8; 1];
                    while !head.ends_with(b"\r\n\r\n") {
                        if stream.read(&mut byte).unwrap_or(0) == 0 {
                            return;
                        }
                        head.push(byte[0]);
                    }
                    let head = String::from_utf8_lossy(&head).into_owned();
                    let path = head.split(' ').nth(1).unwrap_or("").to_owned();
                    seen.lock().unwrap().push(path.clone());
                    let (reply, hold) = answer(&path);
                    let _ = stream.write_all(&reply);
                    let _ = stream.flush();
                    let until = std::time::Instant::now() + std::time::Duration::from_millis(hold);
                    while std::time::Instant::now() < until && !halt.load(Ordering::SeqCst) {
                        std::thread::sleep(std::time::Duration::from_millis(20));
                    }
                });
            }
        });
        Self {
            url,
            paths,
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for RawDaemon {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

#[test]
fn a_maintainer_refusal_whose_body_never_arrives_is_not_checked() {
    // Python's exc.read(65536) runs inside `except HTTPError`: a timeout
    // there escapes maintainer_probe and doctor ends in a traceback. The
    // native doctor names the probe and reports the rest.
    let daemon = RawDaemon::start(|path| {
        match path {
        "/api/hook/coordination-start" => (
            b"HTTP/1.1 200 OK\r\nX-PL-Board: on\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx"
                .to_vec(),
            0,
        ),
        "/api/maintainer" => (
            b"HTTP/1.1 409 Conflict\r\nContent-Length: 5000\r\nConnection: close\r\n\r\n{\"error\": "
                .to_vec(),
            4000,
        ),
        _ => (
            b"HTTP/1.1 404 No\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
            0,
        ),
    }
    });
    let home = Home::new();
    let before = tree(&home.0);
    let output = diagnose(&home, &daemon.url)
        .env("PSEUDOLIFE_MCP_TOKEN", "fixture-token")
        .args(["--timeout", "1"])
        .output()
        .unwrap();
    let map = report(&output);
    assert_eq!(map["board"], json!("on - token present, principal allowed"));
    assert_eq!(
        map["maintainer_passkeys"],
        json!({"state": "not_checked",
               "line": "not checked - the daemon's maintainer answer was incomplete or in a form this client does not read"})
    );
    // The 404 /health is no health answer: the daemon is unavailable.
    assert_eq!(map["error"], json!("DaemonUnavailable"));
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        *daemon.paths.lock().unwrap(),
        ["/api/hook/coordination-start", "/api/maintainer", "/health"]
    );
    assert_eq!(tree(&home.0), before);
}

#[test]
fn a_health_redirect_to_ftp_is_an_unrecognized_daemon() {
    // urllib would follow it through its FTP handler; no daemon does.
    let daemon = RawDaemon::start(|_| {
        (
            b"HTTP/1.1 302 Found\r\nLocation: ftp://example.com/health\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                .to_vec(),
            0,
        )
    });
    let home = Home::new();
    let output = diagnose(&home, &daemon.url).output().unwrap();
    let map = report(&output);
    assert_eq!(map["daemon_status"], json!("unrecognized"));
    assert_eq!(map["error"], json!("DaemonUnrecognized"));
    assert!(map["wake"].get("codex").is_some());
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(*daemon.paths.lock().unwrap(), ["/health"]);
}

fn assert_deferred(output: &Output) {
    assert_eq!(output.status.code(), Some(1));
    assert!(
        output.stdout.is_empty(),
        "{:?}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(output.stderr, native_text(DEFERRED));
}

type Prepare = fn(&Home, &mut Command);

#[test]
fn shapes_beyond_the_port_defer_before_any_request() {
    // Only argument shapes still defer: an explicit --agent-state path the
    // native resolver does not decide, and timeouts past the cap.
    let cases: [(&str, Prepare); 3] = [
        ("saved instance path through ..", |home, command| {
            std::fs::create_dir_all(home.0.join("x")).unwrap();
            std::fs::write(home.0.join("state.json"), b"{}").unwrap();
            command
                .arg("--agent-state")
                .arg(home.0.join("x").join("..").join("state.json"));
        }),
        ("timeout past the cap", |_, command| {
            command.args(["--timeout", "100000000000000000000"]);
        }),
        ("timeout just past the cap", |_, command| {
            command.args(["--timeout", "1000000.5"]);
        }),
    ];
    for (name, prepare) in cases {
        let home = Home::new();
        let fixture = Fixture::start();
        let mut command = diagnose(&home, &fixture.url);
        prepare(&home, &mut command);
        let before = tree(&home.0);
        let output = command.output().unwrap();
        assert_deferred(&output);
        assert!(
            fixture.records.lock().unwrap().is_empty(),
            "{name}: requested"
        );
        assert_eq!(tree(&home.0), before, "{name}: home changed");
    }
}

#[test]
fn a_health_answer_that_is_no_daemon_report_is_named_before_the_handshake() {
    for health in ["[1, 2]".to_owned(), json!({"status": 7}).to_string()] {
        let home = Home::new();
        let fixture = Fixture::start_with(Setup {
            health: vec![Reply::Http(200, health)],
            ..Setup::default()
        });
        let before = tree(&home.0);
        let output = diagnose(&home, &fixture.url).output().unwrap();
        let map = report(&output);
        assert_eq!(map["daemon_status"], json!("unrecognized"));
        assert_eq!(map["error"], json!("DaemonUnrecognized"));
        assert!(
            map["recovery"]
                .as_str()
                .unwrap()
                .contains("PSEUDOLIFE_MCP_DAEMON_URL")
        );
        assert!(map["wake"].get("claude_code").is_some());
        assert!(map.contains_key("path_resolution"));
        assert_eq!(output.status.code(), Some(1));
        let records = fixture.records.lock().unwrap().clone();
        assert_eq!(records.len(), 1, "only doctor's own health probe");
        assert_eq!(
            (records[0].verb.as_str(), records[0].path.as_str()),
            ("GET", "/health")
        );
        assert_eq!(tree(&home.0), before);
    }
}

#[test]
fn a_codex_hook_copy_in_any_state_is_reported_and_the_handshake_runs() {
    // The plugin is enabled but Codex never installed its hook copy.
    let digest = json!({"status": "ok", "version": env!("CARGO_PKG_VERSION"),
                        "hooks_digest": "0123456789abcdef"});
    let plugin = b"[plugins.\"pseudolife-memory@pseudolife-mcp\"]\nenabled = true\n";
    let home = Home::new();
    std::fs::create_dir_all(home.0.join("codex")).unwrap();
    std::fs::write(home.0.join("codex").join("config.toml"), plugin).unwrap();
    let fixture = Fixture::start_with(Setup {
        health: vec![Reply::Http(200, digest.to_string())],
        lists: vec![Reply::Result(json!({"tools": [
            annotated("memory_search"), annotated("memory_agents"), annotated("memory_message")]}))],
        ..Setup::default()
    });
    let output = diagnose(&home, &fixture.url).output().unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
    let line = map["codex_hooks"].as_str().unwrap();
    assert!(line.starts_with("plugin-managed: "), "{line}");
    assert!(line.contains("plugin manager"), "{line}");
}

#[test]
fn a_dream_stall_is_reported_and_the_handshake_runs() {
    let stalled = json!({"status": "ok", "version": env!("CARGO_PKG_VERSION"),
                         "stall": {"kind": "signed_out"}});
    let home = Home::new();
    let fixture = Fixture::start_with(Setup {
        health: vec![Reply::Http(200, stalled.to_string())],
        lists: vec![Reply::Result(json!({"tools": [
            annotated("memory_search"), annotated("memory_agents"), annotated("memory_message")]}))],
        ..Setup::default()
    });
    let output = diagnose(&home, &fixture.url).output().unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
    let line = map["dreams"].as_str().unwrap();
    assert!(line.starts_with("stalled (signed_out)"), "{line}");
}

#[test]
fn an_invalid_daemon_url_is_named_without_any_request() {
    let home = Home::new();
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env(
            "PSEUDOLIFE_MCP_DAEMON_URL",
            "http://user:url-secret@127.0.0.1:1/path",
        )
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).into_owned();
    assert!(!text.contains("url-secret"), "{text}");
    let map = report(&output);
    assert_eq!(map["error"], json!("DaemonUrlInvalid"));
    assert_eq!(map["daemon_status"], json!("not_checked"));
    assert!(
        map["recovery"]
            .as_str()
            .unwrap()
            .contains("without credentials, a path, query, or fragment")
    );
    assert!(map["wake"].get("claude_code").is_some());
    assert!(map.contains_key("path_resolution"));
    assert_eq!(output.status.code(), Some(1));
    assert!(fixture.records.lock().unwrap().is_empty());
}

#[test]
fn client_files_the_native_client_cannot_read_are_named_and_the_rest_runs() {
    let cases: [(&str, Prepare); 3] = [
        ("invalid config.toml", |home, _| {
            std::fs::create_dir_all(home.0.join("codex")).unwrap();
            std::fs::write(home.0.join("codex").join("config.toml"), b"[mcp_servers\n").unwrap();
        }),
        ("JSON only Python reads", |home, command| {
            command.env_remove("CLAUDE_CONFIG_DIR");
            std::fs::write(home.0.join(".claude.json"), b"{\"n\": NaN}").unwrap();
        }),
        ("no PATH", |_, command| {
            command.env_remove("PATH");
        }),
    ];
    for (name, prepare) in cases {
        if name == "no PATH" && cfg!(windows) {
            continue; // Windows has a defined default search path.
        }
        let home = Home::new();
        let fixture = healthy_fixture();
        let mut command = diagnose(&home, &fixture.url);
        prepare(&home, &mut command);
        let output = command.output().unwrap();
        let map = report(&output);
        assert_ordinary(&map, &output);
        assert_handshake_passed(&map, &output);
        let text = String::from_utf8_lossy(&output.stdout);
        if name == "no PATH" {
            // Searched in the platform's default directories instead.
            assert!(map["path_resolution"].get("on_path").is_some(), "{text}");
        } else {
            assert!(
                text.contains("not read by the native client"),
                "{name}: {text}"
            );
        }
    }
}

// --- conditions that once deferred the whole run ---------------------------------
//
// Each condition below used to hand the whole command to the Python client
// (or, with no Python, leave the user with no diagnosis). Each now runs every
// unrelated check and names its own limitation.

/// A daemon whose MCP surface passes every handshake check.
fn healthy_fixture() -> Fixture {
    Fixture::start_with(Setup {
        lists: vec![Reply::Result(json!({"tools": [
            annotated("memory_search"), annotated("memory_agents"), annotated("memory_message")]}))],
        ..Setup::default()
    })
}

/// The ordinary daemon, auth and client diagnostics are all present.
fn assert_ordinary(map: &serde_json::Map<String, Value>, output: &Output) {
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(!stderr.contains("deferred"), "{stderr}");
    for key in [
        "interpreter",
        "credential_source",
        "token_sha256_prefix",
        "board",
        "maintainer_passkeys",
        "daemon_status",
        "dreams",
        "wake",
        "coordination",
        "path_resolution",
    ] {
        assert!(map.contains_key(key), "{key} missing: {map:?}");
    }
    assert_eq!(map["daemon_status"], json!("ok"));
    assert!(map["wake"].get("claude_code").is_some());
    assert!(map["wake"].get("codex").is_some());
}

/// A healthy daemon: the handshake ran, and `ok` follows Git Bash alone.
fn assert_handshake_passed(map: &serde_json::Map<String, Value>, output: &Output) {
    assert_eq!(map["daemon_version"], json!(env!("CARGO_PKG_VERSION")));
    assert_eq!(map["tool_count"], json!(3), "{map:?}");
    assert_eq!(map["instructions_present"], json!(true));
    let git_bash_missing = map.get("git_bash") == Some(&Value::Null);
    assert_eq!(map["ok"], json!(!git_bash_missing), "{map:?}");
    assert_eq!(
        output.status.code(),
        Some(if git_bash_missing { 1 } else { 0 })
    );
}

#[test]
fn an_empty_tunnel_directory_keeps_every_ordinary_diagnostic() {
    let home = Home::new();
    let fixture = healthy_fixture();
    // Absent first: no tunnel section at all.
    let output = diagnose(&home, &fixture.url).output().unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
    assert!(!map.contains_key("tunnels"));
    // An empty, old tunnel directory: still no tunnel section, nothing lost.
    std::fs::create_dir_all(home.0.join(".pseudolife-mcp").join("tunnel")).unwrap();
    // A fresh daemon: the fixture answers its tool list once.
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url).output().unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
    assert!(!map.contains_key("tunnels"), "{map:?}");
}

#[cfg(unix)]
mod tunnel_profiles {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn root(home: &Home) -> std::path::PathBuf {
        home.0.join(".pseudolife-mcp").join("tunnel")
    }

    fn private(home: &Home, name: &str, data: &[u8]) {
        let root = root(home);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = root.join(name);
        std::fs::write(&path, data).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn profile(home: &Home, name: &str, extra: &str) {
        let token = home.0.join("tunnel-token");
        let body = format!(
            "{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": \"{}\"{extra}}}",
            token.display()
        );
        private(home, &format!("{name}.profile.json"), body.as_bytes());
    }

    const READY: &str = ", \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true";

    /// Names, bytes, modes and modification times under the tunnel root.
    fn snapshot(home: &Home) -> Vec<(String, Option<Vec<u8>>, u32, std::time::SystemTime)> {
        let root = root(home);
        let mut entries = vec![(String::new(), None, 0, std::time::UNIX_EPOCH)];
        let meta = std::fs::metadata(&root).unwrap();
        entries[0].2 = meta.permissions().mode();
        entries[0].3 = meta.modified().unwrap();
        for (rel, bytes) in tree(&root) {
            let meta = std::fs::symlink_metadata(root.join(&rel)).unwrap();
            entries.push((
                rel,
                bytes,
                meta.permissions().mode(),
                meta.modified().unwrap(),
            ));
        }
        entries
    }

    fn tunnels(home: &Home) -> Vec<Value> {
        let fixture = healthy_fixture();
        let before = snapshot(home);
        let output = diagnose(home, &fixture.url).output().unwrap();
        // Doctor only reads tunnel state.
        assert_eq!(snapshot(home), before);
        let map = report(&output);
        assert_ordinary(&map, &output);
        assert_handshake_passed(&map, &output);
        let text = String::from_utf8_lossy(&output.stdout);
        assert!(!text.contains("fixture-tunnel-key"), "{text}");
        map["tunnels"].as_array().expect("a tunnel section").clone()
    }

    #[test]
    fn a_ready_profile_is_reported_beside_every_ordinary_diagnostic() {
        let home = Home::new();
        profile(&home, "dot", READY);
        private(&home, "dot.key", b"PLAIN\0fixture-tunnel-key");
        let entries = tunnels(&home);
        assert_eq!(entries.len(), 1, "{entries:?}");
        let entry = &entries[0];
        assert_eq!(entry["profile"], json!("dot"));
        assert_eq!(entry["state"], json!("ready"));
        assert_eq!(entry["catalog"], json!("current"));
        assert_eq!(entry["private_key_present"], json!(true));
        assert_eq!(
            entry["key_expiry"],
            json!({"known": false, "expired": null})
        );
        assert_eq!(
            entry["update"],
            json!({"state": "none", "needs_attention": false})
        );
        assert!(entry.get("next").is_none(), "{entry:?}");
    }

    #[test]
    fn a_stale_profile_names_its_expired_key_and_the_renewal() {
        let home = Home::new();
        profile(
            &home,
            "dot",
            &format!("{READY}, \"runtime_key_expires_at\": \"2020-01-01T00:00:00+00:00\""),
        );
        let entries = tunnels(&home);
        let entry = &entries[0];
        assert_eq!(entry["state"], json!("ready"));
        assert_eq!(entry["private_key_present"], json!(false));
        assert_eq!(entry["key_expiry"]["expired"], json!(true));
        let next = entry["next"].as_array().unwrap();
        assert!(
            next.iter().any(|line| line
                .as_str()
                .unwrap()
                .contains("pseudolife-mcp tunnel setup --profile dot --read-key")),
            "{entry:?}"
        );
    }

    #[test]
    fn a_malformed_profile_is_one_named_entry_and_the_others_still_report() {
        let home = Home::new();
        private(&home, "bad.profile.json", b"{not json");
        profile(&home, "dot", "");
        let entries = tunnels(&home);
        assert_eq!(entries.len(), 2, "{entries:?}");
        assert_eq!(entries[0]["profile"], json!("bad"));
        assert_eq!(entries[0]["state"], json!("unavailable"));
        assert_eq!(entries[0]["reason"], json!("tunnel profile is invalid"));
        assert!(
            entries[0]["recovery"]
                .as_str()
                .unwrap()
                .contains("bad.profile.json")
        );
        assert_eq!(entries[1]["profile"], json!("dot"));
        assert_eq!(entries[1]["state"], json!("pending"));
    }

    #[test]
    fn a_shared_tunnel_directory_is_named_without_losing_the_report() {
        let home = Home::new();
        profile(&home, "dot", READY);
        std::fs::set_permissions(root(&home), std::fs::Permissions::from_mode(0o755)).unwrap();
        let entries = tunnels(&home);
        assert_eq!(entries.len(), 1, "{entries:?}");
        assert_eq!(entries[0]["state"], json!("unavailable"));
        assert_eq!(
            entries[0]["reason"],
            json!("tunnel profile directory must be owner-only")
        );
        assert!(entries[0].get("recovery").is_some());
    }
}

#[test]
fn a_proxy_variable_is_named_and_every_check_still_runs() {
    let home = Home::new();
    let fixture = healthy_fixture();
    // A proxy URL may carry credentials: only the variable's name is shown.
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let proxy = format!(
        "http://proxyuser:proxy-secret@{}",
        closed.local_addr().unwrap()
    );
    drop(closed);
    let output = diagnose(&home, &fixture.url)
        .env("HTTP_PROXY", &proxy)
        .args(["--timeout", "5"])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).into_owned();
    assert!(
        !text.contains("proxy-secret") && !text.contains("proxyuser"),
        "{text}"
    );
    assert!(!String::from_utf8_lossy(&output.stderr).contains("proxy-secret"));
    let map = report(&output);
    assert_ordinary(&map, &output);
    let note = map["proxy"].as_str().expect("a proxy note");
    assert!(note.starts_with("HTTP_PROXY set:"), "{note}");
    assert!(note.contains("NO_PROXY"), "{note}");
    // The shim honours the proxy, which here refuses: the failure names it.
    assert_eq!(map["ok"], json!(false));
    assert!(
        map["recovery"].as_str().unwrap().contains("NO_PROXY"),
        "{map:?}"
    );
    // NO_PROXY covering the daemon lets the shim through; doctor still names
    // the variables it saw.
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env("HTTP_PROXY", &proxy)
        .env("NO_PROXY", "127.0.0.1")
        .output()
        .unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
    assert!(
        map["proxy"]
            .as_str()
            .unwrap()
            .starts_with("HTTP_PROXY, NO_PROXY set:")
    );
}

#[test]
fn pythons_integer_digit_limit_does_not_apply_to_the_native_client() {
    let home = Home::new();
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env("PYTHONINTMAXSTRDIGITS", "0")
        .output()
        .unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
}

/// A credential the run cannot use: named, never printed, never sent; the
/// unauthenticated checks still run.
fn assert_unusable_credential(
    fixture: &Fixture,
    output: &Output,
    error: &str,
    secret: &str,
) -> serde_json::Map<String, Value> {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !stdout.contains(secret) && !stderr.contains(secret),
        "{stdout}{stderr}"
    );
    let map = report(output);
    assert_ordinary(&map, output);
    assert_eq!(map["ok"], json!(false));
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(map["error"], json!(error), "{map:?}");
    assert!(map["board"].as_str().unwrap().starts_with("not checked - "));
    assert_eq!(map["maintainer_passkeys"]["state"], json!("not_checked"));
    for record in fixture.records.lock().unwrap().iter() {
        assert!(
            !record.headers.contains_key("authorization"),
            "{} {} carried a credential",
            record.verb,
            record.path
        );
    }
    map
}

#[test]
fn a_token_no_client_can_send_is_named_and_the_other_checks_run() {
    for secret in ["fixture-t\u{f6}k", "token-\u{4e00}", "fixture-\u{7f}"] {
        let home = Home::new();
        let fixture = healthy_fixture();
        let output = diagnose(&home, &fixture.url)
            .env("PSEUDOLIFE_MCP_TOKEN", secret)
            .output()
            .unwrap();
        let map = assert_unusable_credential(&fixture, &output, "BearerUnsendable", secret);
        assert_eq!(map["credential_source"], json!("environment"));
        assert!(
            map["recovery"]
                .as_str()
                .unwrap()
                .contains("printable ASCII")
        );
    }
}

#[test]
fn an_unreadable_token_source_is_named_and_the_other_checks_run() {
    let home = Home::new();
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env("PSEUDOLIFE_MCP_TOKEN_FILE", home.0.join("absent-token"))
        .output()
        .unwrap();
    let map = assert_unusable_credential(
        &fixture,
        &output,
        "CredentialUnavailable",
        "absent-token-value",
    );
    assert_eq!(map["token_sha256_prefix"], Value::Null);
    assert!(
        map["recovery"]
            .as_str()
            .unwrap()
            .contains("(configured credential file is missing; source: environment)"),
        "{map:?}"
    );
}
