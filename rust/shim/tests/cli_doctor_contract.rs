#![forbid(unsafe_code)]
mod common;
use common::{LeaseHome as Home, cli::native_text};
use std::{
    ffi::OsStr,
    process::{Command, Output},
};

const REFUSAL: &str = "{\"ok\": false, \"error\": \"ExplicitDisposableDatabaseRequired\", \"recovery\": \"Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used.\"}\n";

// Each cell runs even on the baseline so its failure is observed separately.
#[test]
fn native_argument_cells_never_defer_or_echo_values() {
    let home = Home::new();
    let mut failures = Vec::new();
    for (args, code, needle) in [
        (vec!["--help"], 0, "Usage:"),
        (vec!["-h"], 0, "Usage:"),
        (
            vec!["--timeout=20", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--host=codex", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--timeout", "1e2", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--timeout", "+20", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--timeout", "1000000.5", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--timeout", "20", "--timeout", "30", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--host", "generic", "--host", "codex", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (
            vec!["--disposable-proof", "--disposable-proof"],
            2,
            "ExplicitDisposableDatabaseRequired",
        ),
        (vec!["--host", "w44-secret-value"], 2, "--host"),
        (vec!["--timeout", "1e300"], 2, "--timeout"),
        (vec!["--timeout", "NaN"], 2, "--timeout"),
        (vec!["--timeout", "0"], 2, "--timeout"),
        (vec!["--timeout", "-1"], 2, "--timeout"),
        (vec!["--timeout"], 2, "--timeout"),
        (vec!["--host"], 2, "--host"),
        (vec!["--agent-state"], 2, "--agent-state"),
        (vec!["--agent-state", ""], 2, "--agent-state"),
        (vec!["--time", "20"], 2, "--help"),
        (vec!["--dispos"], 2, "--help"),
        (vec!["--disposable-proof=false"], 2, "--disposable-proof"),
        (vec!["w44-secret-value"], 2, "--help"),
    ] {
        let output = command(&home).arg("doctor").args(&args).output().unwrap();
        let text = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let ok = output.status.code() == Some(code)
            && text.contains(needle)
            && !text.contains("deferred")
            && !text.contains("w44-secret-value");
        eprintln!("native argument cell {args:?}: {ok}");
        if !ok {
            failures.push(args);
        }
    }
    assert!(failures.is_empty(), "failed cells: {failures:?}");
}

#[test]
fn native_agent_paths_report_findings_even_offline() {
    let home = Home::new();
    std::fs::create_dir(home.0.join("child")).unwrap();
    let mut failures = Vec::new();
    for name in [
        "child/../missing.json",
        "child/../bad.json",
        "bad.json/",
        "bad.json",
    ] {
        std::fs::write(
            home.0.join("bad.json"),
            br#"{"credential":"w44-secret-value","n":NaN}"#,
        )
        .unwrap();
        let output = diagnose(&home, "http://127.0.0.1:1")
            .arg("--agent-state")
            .arg(home.0.join(name))
            .output()
            .unwrap();
        let text = String::from_utf8_lossy(&output.stdout);
        let ok = text.contains("agent_state")
            && text.contains("--agent-state")
            && text.contains("daemon_status")
            && !text.contains("w44-secret-value")
            && output.stderr.is_empty();
        eprintln!("native agent path {name}: {ok}");
        if !ok {
            failures.push(name);
        }
    }
    assert!(failures.is_empty(), "failed paths: {failures:?}");
}

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

fn usage_error(output: Output) {
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.starts_with("pseudolife-mcp doctor:"), "{stderr}");
    assert!(!stderr.contains("deferred"));
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
fn malformed_arguments_have_usage_diagnostics() {
    let home = Home::new();
    for arguments in [
        vec!["--time", "20"],
        vec!["--disposable-proof=false"],
        vec!["--dispos"],
        vec!["--h"],
        vec!["unexpected"],
        vec!["--host", "invalid"],
        vec!["--host"],
        vec!["--timeout"],
        vec!["--agent-state"],
        vec!["--agent-state", ""],
    ] {
        usage_error(
            command(&home)
                .args(["doctor", "--disposable-proof"])
                .args(arguments)
                .output()
                .unwrap(),
        );
    }
    let overflow = "9".repeat(400);
    for timeout in [
        "", ".", "0", "-1", "-1\n", "-.5\n", "2_0", "nan", "inf", " 20", "20\n", "1.2.3", "２０",
        &overflow,
    ] {
        usage_error(
            command(&home)
                .args(["doctor", "--disposable-proof", "--timeout", timeout])
                .output()
                .unwrap(),
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn nonempty_proof_requires_native_prerequisites_and_help_is_local() {
    let home = Home::new();
    let output = command(&home)
        .args(["doctor", "--disposable-proof", "--help"])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("Usage:"));
    assert!(output.stderr.is_empty());
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
fn non_unicode_nonempty_dsn_has_named_refusal() {
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
    let output = command(&home)
        .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
        .args(["doctor", "--disposable-proof"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let parsed: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(parsed["error"], "DisposableDatabaseInvalid");
    assert!(
        parsed["recovery"]
            .as_str()
            .unwrap()
            .contains("PSEUDOLIFE_TEST_DATABASE_URL")
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

// --- read-only diagnostics -----------------------------------------------------

use common::{Fixture, Reply, Setup};
use serde_json::{Value, json};

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
               "line": "not checked - could not read maintainer answer: GET /api/maintainer refusal body was incomplete; restore the daemon connection and retry"})
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

type Prepare = fn(&Home, &mut Command);

#[test]
fn formerly_deferred_paths_and_timeouts_have_native_results() {
    let home = Home::new();
    std::fs::create_dir_all(home.0.join("x")).unwrap();
    std::fs::write(home.0.join("state.json"), b"{}").unwrap();
    let before = tree(&home.0);
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(home.0.join("x/../state.json"))
        .output()
        .unwrap();
    assert!(report(&output)["agent_state"].is_string());
    let output = diagnose(&home, "http://127.0.0.1:1")
        .args(["--timeout", "1000000.5"])
        .output()
        .unwrap();
    assert_eq!(report(&output)["daemon_status"], "unreachable");
    usage_error(
        diagnose(&home, "http://127.0.0.1:1")
            .args(["--timeout", "100000000000000000000"])
            .output()
            .unwrap(),
    );
    assert_eq!(tree(&home.0), before);
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
    // No daemon was contacted, so none is called offline.
    assert_eq!(map["coordination"]["daemon"], json!("not_checked"));
    assert_eq!(map["coordination"]["authentication"], json!("not_checked"));
    let next = map["coordination"]["next"][0].as_str().unwrap();
    assert!(
        next.starts_with("Correct PSEUDOLIFE_MCP_DAEMON_URL first"),
        "{next}"
    );
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
            // Searched in the platform's default directories instead: no
            // `unknown (not read ...)` fallback anywhere in the report
            // (whether those directories hold pseudolife-mcp is the host's).
            assert!(!text.contains("could not read"), "{text}");
            assert!(map["path_resolution"].get("on_path").is_some(), "{text}");
        } else {
            assert!(text.contains("could not read"), "{name}: {text}");
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
    let root = home.0.join(".pseudolife-mcp").join("tunnel");
    assert!(!root.exists(), "doctor created the tunnel directory");
    // An empty, old tunnel directory: still no tunnel section, nothing lost.
    std::fs::create_dir_all(&root).unwrap();
    // A fresh daemon: the fixture answers its tool list once.
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url).output().unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_handshake_passed(&map, &output);
    assert!(!map.contains_key("tunnels"), "{map:?}");
    assert_eq!(std::fs::read_dir(&root).unwrap().count(), 0);
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
        let section = map["tunnels"].as_array().expect("a tunnel section").clone();
        // Every step doctor names is one this client runs.
        assert!(
            !Value::Array(section.clone()).to_string().contains("Python"),
            "{section:?}"
        );
        section
    }

    /// Native `tunnel status --profile dot --json` on the store doctor
    /// read: the command doctor's advice names must answer it, with the
    /// refresh outcome and the key's state, not defer.
    fn assert_tunnel_status_answers(home: &Home) {
        let output = tunnel_status(home);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert_eq!(output.status.code(), Some(0), "{stderr}");
        assert!(!stderr.contains("deferred"), "{stderr}");
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(report["update"]["state"].is_string(), "{report}");
        assert!(report.get("private_key_present").is_some(), "{report}");
    }

    fn tunnel_status(home: &Home) -> Output {
        command(home)
            .args(["tunnel", "status", "--profile", "dot", "--json"])
            .output()
            .unwrap()
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
        assert_eq!(
            entry["runtime"],
            json!({"checked": false, "process_record": false})
        );
    }

    #[test]
    fn runtime_refresh_and_verification_records_are_read_and_left_alone() {
        let home = Home::new();
        profile(&home, "dot", READY);
        private(&home, "dot.key", b"PLAIN\0fixture-tunnel-key");
        private(&home, "dot.process.json", b"{\"pid\": 1}");
        private(
            &home,
            "dot.reload.result.json",
            b"{\"id\": \"0123456789abcdef0123456789abcdef\", \"state\": \"failed\"}",
        );
        let verification = root(&home).join("dot-verification");
        std::fs::create_dir_all(&verification).unwrap();
        std::fs::set_permissions(&verification, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(verification.join("challenge.json"), b"{}").unwrap();
        std::fs::set_permissions(
            verification.join("challenge.json"),
            std::fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        // `tunnels` asserts the whole tree is byte- and mtime-identical.
        let entries = tunnels(&home);
        let entry = &entries[0];
        assert_eq!(
            entry["update"],
            json!({"state": "failed", "needs_attention": true, "rolled_back": false})
        );
        assert_eq!(
            entry["runtime"],
            json!({"checked": false, "process_record": true})
        );
        let next = entry["next"].as_array().unwrap();
        assert!(
            next.iter().any(|line| {
                let line = line.as_str().unwrap();
                line == "Last tunnel refresh: failed; needs attention: pseudolife-mcp tunnel status --profile dot --json shows the outcome and the key's state."
            }),
            "{entry:?}"
        );
        assert_tunnel_status_answers(&home);
        // Without the process record, native `tunnel status` is the step.
        std::fs::remove_file(root(&home).join("dot.process.json")).unwrap();
        let entries = tunnels(&home);
        let line = entries[0]["next"][0].as_str().unwrap().to_owned();
        assert!(
            line.ends_with(
                "pseudolife-mcp tunnel status --profile dot --json shows the outcome and the key's state."
            ),
            "{line}"
        );
    }

    #[test]
    fn a_key_file_the_native_client_cannot_read_is_named_with_its_fix() {
        // Native `tunnel status` reads the key first and does not answer
        // while it cannot (a FIFO here), so doctor names that file instead.
        let home = Home::new();
        profile(&home, "dot", READY);
        private(
            &home,
            "dot.reload.result.json",
            b"{\"id\": \"0123456789abcdef0123456789abcdef\", \"state\": \"failed\"}",
        );
        let status = std::process::Command::new("mkfifo")
            .arg(root(&home).join("dot.key"))
            .status()
            .unwrap();
        assert!(status.success());
        // Not `tunnels()`: its snapshot would read the FIFO and block.
        let fixture = healthy_fixture();
        let output = diagnose(&home, &fixture.url).output().unwrap();
        let map = report(&output);
        assert_ordinary(&map, &output);
        let entry = &map["tunnels"][0];
        assert_eq!(entry["private_key_present"], json!("not_verified"));
        let line = entry["next"][0].as_str().unwrap();
        assert_eq!(
            line,
            format!(
                "Last tunnel refresh: failed; needs attention: pseudolife-mcp tunnel status --profile dot cannot read the key file {}; check that it is a plain owner-only file, not a pipe or a link.",
                root(&home).join("dot.key").display()
            )
        );
        assert!(!entry.to_string().contains("Python"), "{entry}");
        // Doctor names the file because `tunnel status` really does not
        // answer while it is a pipe.
        let output = tunnel_status(&home);
        assert_eq!(output.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&output.stderr).contains("deferred"));
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
        assert_eq!(
            entry["next"],
            json!([
                "The tunnel key expired on 2020-01-01T00:00:00+00:00; create a new restricted key at https://platform.openai.com/settings/organization/api-keys; this client cannot save it yet."
            ])
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
        let recovery = entries[0]["recovery"].as_str().unwrap();
        assert!(recovery.contains("bad.profile.json"), "{recovery}");
        assert!(
            recovery.ends_with("move it aside; this client cannot set a tunnel up yet."),
            "{recovery}"
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

/// A loopback proxy that answers every request 502 at once: deterministic,
/// with no reused port or slow closed-port connect.
fn refusing_proxy() -> RawDaemon {
    RawDaemon::start(|_| {
        (
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
            0,
        )
    })
}

/// A daemon whose handshake fails on its own (a tool without annotations).
fn failing_handshake_fixture() -> Fixture {
    Fixture::start_with(Setup {
        lists: vec![Reply::Result(json!({"tools": [
            {"name": "bare", "inputSchema": {"type": "object"}}]}))],
        ..Setup::default()
    })
}

/// A proxy URL with credentials in it, which must never be shown.
fn secret_proxy(proxy: &RawDaemon) -> String {
    format!(
        "http://w11proxyuser:w11proxysecret@{}",
        proxy.url.trim_start_matches("http://")
    )
}

fn assert_no_proxy_secret(output: &Output) {
    for stream in [&output.stdout, &output.stderr] {
        let text = String::from_utf8_lossy(stream);
        assert!(
            !text.contains("w11proxysecret") && !text.contains("w11proxyuser"),
            "{text}"
        );
    }
}

#[test]
fn a_proxy_the_shim_uses_is_named_and_every_check_still_runs() {
    let home = Home::new();
    let fixture = healthy_fixture();
    let proxy = refusing_proxy();
    let output = diagnose(&home, &fixture.url)
        .env("HTTP_PROXY", secret_proxy(&proxy))
        .output()
        .unwrap();
    assert_no_proxy_secret(&output);
    let map = report(&output);
    assert_ordinary(&map, &output);
    let note = map["proxy"].as_str().expect("a proxy note");
    assert!(note.starts_with("HTTP_PROXY set:"), "{note}");
    assert!(note.contains("through the proxy"), "{note}");
    // The shim really went through the proxy, which refused: the failure
    // names the proxy.
    assert!(!proxy.paths.lock().unwrap().is_empty());
    assert_eq!(map["ok"], json!(false));
    assert!(
        map["recovery"].as_str().unwrap().contains("NO_PROXY"),
        "{map:?}"
    );
}

#[test]
fn a_proxy_the_shim_does_not_use_never_takes_the_blame() {
    let proxy = refusing_proxy();
    let proxy_url = secret_proxy(&proxy);
    // An https proxy only (an http daemon), and an http proxy whose
    // NO_PROXY names the daemon: the shim connects directly either way.
    let cases: [&[(&str, &str)]; 2] = [
        &[("HTTPS_PROXY", proxy_url.as_str())],
        &[
            ("HTTP_PROXY", proxy_url.as_str()),
            ("NO_PROXY", "127.0.0.1"),
        ],
    ];
    for vars in cases {
        // A passing handshake: nothing goes through the proxy.
        let home = Home::new();
        let fixture = healthy_fixture();
        let mut command = diagnose(&home, &fixture.url);
        command.envs(vars.iter().copied());
        let output = command.output().unwrap();
        assert_no_proxy_secret(&output);
        let map = report(&output);
        assert_ordinary(&map, &output);
        assert_handshake_passed(&map, &output);
        let note = map["proxy"].as_str().unwrap();
        assert!(note.contains("not used for this daemon"), "{note}");
        // A handshake that fails on its own: the recovery does not blame
        // the proxy.
        let fixture = failing_handshake_fixture();
        let mut command = diagnose(&home, &fixture.url);
        command.envs(vars.iter().copied());
        let output = command.output().unwrap();
        let map = report(&output);
        assert_eq!(map["ok"], json!(false));
        let recovery = map["recovery"].as_str().unwrap();
        assert!(!recovery.contains("NO_PROXY"), "{vars:?}: {recovery}");
    }
    assert!(
        proxy.paths.lock().unwrap().is_empty(),
        "the shim used a proxy it should not"
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

/// The ASCII marker every secret in these tests starts with: stdout is
/// ASCII-escaped JSON, so a printed non-ASCII secret would show only this.
const SECRET_MARK: &str = "w11sec";

/// Every string in a parsed report, keys included.
fn strings(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::String(text) => out.push(text.clone()),
        Value::Array(items) => items.iter().for_each(|item| strings(item, out)),
        Value::Object(map) => {
            for (key, item) in map {
                out.push(key.clone());
                strings(item, out);
            }
        }
        _ => {}
    }
}

/// A credential the run cannot use: named, never printed (raw, escaped or
/// decoded), never sent; the unauthenticated checks still run. `secret` is
/// the credential's text where the test wrote one.
fn assert_unusable_credential(
    fixture: &Fixture,
    output: &Output,
    error: &str,
    secret: Option<&str>,
) -> serde_json::Map<String, Value> {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if let Some(secret) = secret {
        assert!(secret.starts_with(SECRET_MARK));
        assert!(
            !stdout.contains(SECRET_MARK) && !stderr.contains(SECRET_MARK),
            "{stdout}{stderr}"
        );
    }
    let map = report(output);
    if let Some(secret) = secret {
        let mut all = Vec::new();
        strings(&Value::Object(map.clone()), &mut all);
        assert!(
            all.iter().all(|text| !text.contains(secret.trim())),
            "{all:?}"
        );
    }
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
fn a_token_the_shim_cannot_authenticate_with_is_named_and_the_other_checks_run() {
    for secret in ["w11sec-t\u{f6}k", "w11sec-\u{4e00}", "w11sec-\u{7f}"] {
        let home = Home::new();
        let fixture = healthy_fixture();
        let output = diagnose(&home, &fixture.url)
            .env("PSEUDOLIFE_MCP_TOKEN", secret)
            .output()
            .unwrap();
        let map = assert_unusable_credential(&fixture, &output, "BearerUnsendable", Some(secret));
        assert_eq!(map["credential_source"], json!("environment"));
        let recovery = map["recovery"].as_str().unwrap();
        assert!(recovery.contains("printable ASCII"), "{recovery}");
        assert!(recovery.contains("as UTF-8"), "{recovery}");
        assert!(recovery.contains("as Latin-1"), "{recovery}");
        assert!(!recovery.contains("no Pseudolife client"), "{recovery}");
    }
}

#[test]
fn an_inline_token_with_whitespace_is_named_as_the_inline_token() {
    for secret in ["w11sec-token ", "w11sec-\ttoken", "w11sec-\u{a0}token"] {
        let home = Home::new();
        let fixture = healthy_fixture();
        let output = diagnose(&home, &fixture.url)
            .env("PSEUDOLIFE_MCP_TOKEN", secret)
            .output()
            .unwrap();
        let map =
            assert_unusable_credential(&fixture, &output, "CredentialUnavailable", Some(secret));
        let recovery = map["recovery"].as_str().unwrap();
        assert!(
            recovery.contains("the PSEUDOLIFE_MCP_TOKEN value is empty, longer than 4096 bytes, or contains whitespace"),
            "{recovery}"
        );
        assert!(
            recovery.contains("Set PSEUDOLIFE_MCP_TOKEN again in this shell without spaces"),
            "{recovery}"
        );
        assert!(!recovery.contains("credential file"), "{recovery}");
        assert!(
            !recovery.contains("PSEUDOLIFE_MCP_TOKEN_FILE"),
            "{recovery}"
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
    // No credential exists to print: the file is absent.
    let map = assert_unusable_credential(&fixture, &output, "CredentialUnavailable", None);
    assert_eq!(map["token_sha256_prefix"], Value::Null);
    assert!(
        map["recovery"]
            .as_str()
            .unwrap()
            .contains("(configured credential file is missing; source: environment)"),
        "{map:?}"
    );
    // The file wins while it is set, so the inline alternative names unsetting it.
    let recovery = map["recovery"].as_str().unwrap();
    assert!(
        recovery.contains("unset PSEUDOLIFE_MCP_TOKEN_FILE and set PSEUDOLIFE_MCP_TOKEN"),
        "{recovery}"
    );
    // Following that advice clears the error.
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env("PSEUDOLIFE_MCP_TOKEN", "fixture-token")
        .output()
        .unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_ne!(
        map.get("error"),
        Some(&json!("CredentialUnavailable")),
        "{map:?}"
    );
    assert!(map["token_sha256_prefix"].is_string());
}

#[test]
fn a_registration_credential_problem_is_fixed_in_the_registration() {
    let home = Home::new();
    let missing = home.0.join("registered-token");
    let registration = json!({"mcpServers": {"pseudolife-memory": {"env": {
        "PSEUDOLIFE_MCP_TOKEN_FILE": missing.to_str().unwrap()}}}});
    std::fs::write(home.0.join(".claude.json"), registration.to_string()).unwrap();
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env_remove("CLAUDE_CONFIG_DIR")
        .output()
        .unwrap();
    let map = assert_unusable_credential(&fixture, &output, "CredentialUnavailable", None);
    assert!(
        map["credential_source"]
            .as_str()
            .unwrap()
            .starts_with("Claude Code registration (")
    );
    let recovery = map["recovery"].as_str().unwrap();
    assert!(
        recovery.contains("that registration's env block"),
        "{recovery}"
    );
    assert!(
        recovery.contains("setting shell variables would only make doctor pass"),
        "{recovery}"
    );
    assert!(!recovery.contains("in this shell"), "{recovery}");
}

#[cfg(unix)]
#[test]
fn a_token_file_others_can_read_is_named_and_its_contents_never_shown() {
    use std::os::unix::fs::PermissionsExt;
    let home = Home::new();
    let path = home.0.join("shared-token");
    std::fs::write(&path, b"w11sec-shared-file-token\n").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env("PSEUDOLIFE_MCP_TOKEN_FILE", &path)
        .output()
        .unwrap();
    let map = assert_unusable_credential(
        &fixture,
        &output,
        "CredentialUnavailable",
        Some("w11sec-shared-file-token"),
    );
    assert!(
        map["recovery"]
            .as_str()
            .unwrap()
            .contains("(credential file must be owner-only; source: environment)"),
        "{map:?}"
    );
}

#[test]
fn a_missing_bearer_beside_an_unread_registration_names_both() {
    let home = Home::new();
    std::fs::write(home.0.join(".claude.json"), b"{\"n\": NaN}").unwrap();
    let auth = json!({"status": "ok", "auth": true, "version": env!("CARGO_PKG_VERSION")});
    let fixture = Fixture::start_with(Setup {
        health: vec![Reply::Http(200, auth.to_string())],
        ..Setup::default()
    });
    let output = diagnose(&home, &fixture.url)
        .env_remove("CLAUDE_CONFIG_DIR")
        .output()
        .unwrap();
    let map = report(&output);
    assert_ordinary(&map, &output);
    assert_eq!(map["error"], json!("BearerMissing"));
    assert!(
        map["registrations"]
            .as_str()
            .unwrap()
            .starts_with("could not read")
    );
    let recovery = map["recovery"].as_str().unwrap();
    assert!(recovery.contains("PSEUDOLIFE_MCP_TOKEN_FILE"), "{recovery}");
    assert!(recovery.contains("Could not read"), "{recovery}");
    assert_eq!(output.status.code(), Some(1));
}

fn private_state(path: &std::path::Path, bytes: &[u8]) {
    use std::io::Write;
    let mut file = pseudolife_stdio::board::state::create(path).unwrap();
    file.file.write_all(bytes).unwrap();
}

#[test]
fn native_agent_json_cells_and_private_path_near_misses() {
    let home = Home::new();
    std::fs::create_dir(home.0.join("child")).unwrap();
    let mut failures = Vec::new();
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(home.0.join("child"))
        .output()
        .unwrap();
    if !report(&output)["agent_state"]
        .as_str()
        .is_some_and(|line| line.contains("not a regular file"))
    {
        failures.push(8);
    }
    for (index, bytes, cause) in [
        (0, br#"{"n":NaN}"#.to_vec(), "JSON"),
        (1, br#"{"n":Infinity}"#.to_vec(), "JSON"),
        (2, br#"{"n":"\ud800"}"#.to_vec(), "JSON"),
        (3, b"\xff".to_vec(), "UTF-8"),
        (4, b"[]".to_vec(), "object"),
        (5, b"{}".to_vec(), "bank_url"),
        (6, vec![b' '; 16385], "16384"),
        (
            7,
            format!("{}0{}", "[".repeat(257), "]".repeat(257)).into_bytes(),
            "256",
        ),
    ] {
        let path = home.0.join(format!("state-{index}.json"));
        private_state(&path, &bytes);
        let output = diagnose(&home, "http://127.0.0.1:1")
            .arg("--agent-state")
            .arg(&path)
            .output()
            .unwrap();
        let parsed: Value = serde_json::from_slice(&output.stdout).unwrap_or(Value::Null);
        let ok = parsed["agent_state"]
            .as_str()
            .is_some_and(|line| line.contains(cause) && line.contains(';'));
        eprintln!("agent JSON cell {index}: {ok}");
        if !ok {
            failures.push(index);
        }
        assert_eq!(std::fs::read(path).unwrap(), bytes);
    }
    let good = home.0.join("valid.json");
    private_state(&good, br#"{"bank_url":"http://127.0.0.1:1","agent_id":"fixture","credential":"w44-secret-value","extra":"NaN Infinity \ud83d\ude00"}"#);
    for path in [
        good.clone(),
        home.0.join("child/../valid.json"),
        home.0.join("valid.json/"),
    ] {
        let output = diagnose(&home, "http://127.0.0.1:1")
            .arg("--agent-state")
            .arg(path)
            .output()
            .unwrap();
        let map = report(&output);
        assert!(!map.contains_key("agent_state"), "{map:?}");
        assert!(!String::from_utf8_lossy(&output.stdout).contains("w44-secret-value"));
    }
    assert!(failures.is_empty(), "{failures:?}");
}

#[test]
fn native_non_unicode_arguments_and_environment_have_named_results() {
    #[cfg(unix)]
    let value = {
        use std::os::unix::ffi::OsStringExt;
        std::ffi::OsString::from_vec(vec![0xff])
    };
    #[cfg(windows)]
    let value = {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[0xd800])
    };
    let home = Home::new();
    let mut failures = Vec::new();
    for key in [
        "HOME",
        "USERPROFILE",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "PATH",
        "PSEUDOLIFE_SHIM_LAUNCHER",
    ] {
        if (key == "HOME" && cfg!(windows)) || (key == "USERPROFILE" && cfg!(unix)) {
            continue;
        }
        let output = diagnose(&home, "http://127.0.0.1:1")
            .env_remove("CLAUDE_CONFIG_DIR")
            .env_remove("CODEX_HOME")
            .env("PSEUDOLIFE_SHIM_RUNTIMES", "fixture")
            .env(key, &value)
            .output()
            .unwrap();
        let text = String::from_utf8_lossy(&output.stdout);
        let ok = text.contains(key) && text.contains("Unicode") && text.contains("daemon_status");
        eprintln!("non-Unicode environment {key}: {ok}");
        if !ok {
            failures.push(key);
        }
    }
    let output = command(&home)
        .args(["doctor", "--disposable-proof"])
        .env("PSEUDOLIFE_TEST_DATABASE_URL", &value)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout);
    if output.status.code() != Some(2)
        || !text.contains("PSEUDOLIFE_TEST_DATABASE_URL")
        || !text.contains("Unicode")
    {
        failures.push("proof DSN");
    }
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(&value)
        .output()
        .unwrap();
    if !String::from_utf8_lossy(&output.stdout).contains("agent_state") {
        failures.push("agent path");
    }
    let good = home.0.join(&value);
    private_state(&good, br#"{"bank_url":"http://127.0.0.1:1","agent_id":"fixture","credential":"w44-secret-value"}"#);
    for inline in [false, true] {
        let mut command = diagnose(&home, "http://127.0.0.1:1");
        if inline {
            let mut argument = std::ffi::OsString::from("--agent-state=");
            argument.push(&good);
            command.arg(argument);
        } else {
            command.arg("--agent-state").arg(&good);
        }
        let output = command.output().unwrap();
        let parsed = serde_json::from_slice::<Value>(&output.stdout).ok();
        let ok = parsed.as_ref().is_some_and(|value| {
            value.get("daemon_status").is_some() && value.get("agent_state").is_none()
        });
        eprintln!("existing non-Unicode agent path (inline={inline}): {ok}");
        if !ok {
            failures.push(if inline {
                "inline non-Unicode agent path"
            } else {
                "existing non-Unicode agent path"
            });
        }
    }
    let output = command(&home).arg("doctor").arg(&value).output().unwrap();
    if output.status.code() != Some(2)
        || !String::from_utf8_lossy(&output.stderr).contains("Unicode")
    {
        failures.push("option");
    }
    assert!(failures.is_empty(), "{failures:?}");
}

#[test]
fn native_saved_connection_failure_does_not_prevent_other_checks() {
    let home = Home::new();
    std::fs::create_dir_all(home.0.join("codex/pseudolife")).unwrap();
    private_state(
        &home.0.join("codex/pseudolife/connection.json"),
        b"{broken w44-secret-value",
    );
    let output = command(&home)
        .arg("doctor")
        .env_remove("PSEUDOLIFE_MCP_DAEMON_URL")
        .env("CODEX_THREAD_ID", "fixture")
        .env("PATH", home.0.join("absent"))
        .output()
        .unwrap();
    let map = report(&output);
    assert!(map["host_connection"].as_str().unwrap().contains("Codex"));
    assert_eq!(map["daemon_status"], "not_checked");
    assert!(map.contains_key("path_resolution"));
    assert!(!String::from_utf8_lossy(&output.stdout).contains("w44-secret-value"));
    // An explicit intended daemon bypasses the broken saved connection.
    let output = diagnose(&home, "http://127.0.0.1:1")
        .env("CODEX_THREAD_ID", "fixture")
        .output()
        .unwrap();
    assert!(!report(&output).contains_key("host_connection"));
}

#[test]
fn native_agent_link_and_plain_file_are_distinguished() {
    let home = Home::new();
    let path = home.0.join("state.json");
    private_state(&path, br#"{"bank_url":"http://127.0.0.1:1","agent_id":"fixture","credential":"w44-secret-value"}"#);
    let link = home.0.join("link.json");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&path, &link).unwrap();
    #[cfg(windows)]
    std::os::windows::fs::symlink_file(&path, &link).unwrap();
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(&link)
        .output()
        .unwrap();
    std::fs::remove_file(link).unwrap();
    let map = report(&output);
    assert!(map["agent_state"].as_str().unwrap().contains("link"));
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(&path)
        .output()
        .unwrap();
    assert!(
        !report(&output).contains_key("agent_state"),
        "{:?}",
        report(&output)
    );
}

#[test]
fn native_trusted_directory_redirect_is_read_normally() {
    let home = Home::new();
    // TEMP can grant other users Modify. Protect only this disposable holder.
    pseudolife_stdio::board::state::private_dir(&home.0).unwrap();
    let directory = home.0.join("original");
    pseudolife_stdio::board::state::private_dir(&directory).unwrap();
    private_state(&directory.join("state.json"), br#"{"bank_url":"http://127.0.0.1:1","agent_id":"fixture","credential":"w44-secret-value"}"#);
    let link = home.0.join("redirect");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&directory, &link).unwrap();
    #[cfg(windows)]
    {
        let output = Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(&link)
            .arg(&directory)
            .output()
            .unwrap();
        assert!(output.status.success());
    }
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(link.join("state.json"))
        .output()
        .unwrap();
    #[cfg(windows)]
    std::fs::remove_dir(link).unwrap();
    #[cfg(unix)]
    std::fs::remove_file(link).unwrap();
    assert!(
        !report(&output).contains_key("agent_state"),
        "{:?}",
        report(&output)
    );
}

#[test]
fn native_probe_header_failure_names_cause_and_continues() {
    let daemon = RawDaemon::start(|path| {
        if path == "/api/hook/coordination-start" {
            (b"HTTP/1.1 200 OK\r\nX-PL-Board: on\r\nX-PL-Board: on\r\nContent-Length: 2\r\n\r\n{}".to_vec(), 0)
        } else {
            (b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 21\r\n\r\n{\"status\":\"degraded\"}".to_vec(), 0)
        }
    });
    let home = Home::new();
    let output = diagnose(&home, &daemon.url)
        .env("PSEUDOLIFE_MCP_TOKEN", "w44-secret-value")
        .output()
        .unwrap();
    let map = report(&output);
    assert!(map["board"].as_str().unwrap().contains("X-PL-Board"));
    assert!(
        daemon
            .paths
            .lock()
            .unwrap()
            .iter()
            .any(|path| path == "/health")
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("w44-secret-value"));
}

#[cfg(unix)]
#[test]
fn native_untrusted_directory_redirect_is_a_private_finding() {
    use std::os::unix::fs::PermissionsExt;
    let home = Home::new();
    let directory = home.0.join("original");
    std::fs::create_dir(&directory).unwrap();
    private_state(&directory.join("state.json"), br#"{"bank_url":"http://127.0.0.1:1","agent_id":"fixture","credential":"w44-secret-value"}"#);
    let link = home.0.join("w44-secret-value");
    std::os::unix::fs::symlink(&directory, &link).unwrap();
    std::fs::set_permissions(&home.0, std::fs::Permissions::from_mode(0o777)).unwrap();
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(link.join("state.json"))
        .output()
        .unwrap();
    std::fs::set_permissions(&home.0, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert!(
        report(&output)["agent_state"]
            .as_str()
            .unwrap()
            .contains("replace")
    );
    assert!(output.stderr.is_empty());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("w44-secret-value"));
    let output = diagnose(&home, "http://127.0.0.1:1")
        .arg("--agent-state")
        .arg(link.join("state.json"))
        .output()
        .unwrap();
    std::fs::remove_file(link).unwrap();
    assert!(!report(&output).contains_key("agent_state"));
}

#[test]
fn native_reports_do_not_echo_bearers_or_credential_urls_from_answers() {
    let home = Home::new();
    let mut failures = Vec::new();
    for health in [
        json!({"status":"ok", "version":"w44-secret-value https://user:w44-url-password@example.com https://example.com/?token=w44-query-secret"}),
        json!({"status":"w44-secret-value"}),
        json!({"status":"postgresql://fixture:w44-url-password@example.invalid/bank"}),
    ] {
        let fixture = Fixture::start_with(Setup {
            health: vec![Reply::Http(200, health.to_string())],
            ..Setup::default()
        });
        let output = diagnose(&home, &fixture.url)
            .env("PSEUDOLIFE_MCP_TOKEN", "w44-secret-value")
            .output()
            .unwrap();
        let text = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let safe = !text.contains("w44-secret-value")
            && !text.contains("w44-url-password")
            && !text.contains("w44-query-secret")
            && text.contains("redacted");
        eprintln!("response redaction cell: {safe}");
        if !safe {
            failures.push("reflected response credential");
        }
    }
    // Redaction must not change version comparison or health classification.
    let fixture = healthy_fixture();
    let output = diagnose(&home, &fixture.url)
        .env("PSEUDOLIFE_MCP_TOKEN", env!("CARGO_PKG_VERSION"))
        .output()
        .unwrap();
    assert!(!report(&output).contains_key("error"));
    assert_eq!(report(&output)["ok"], true);
    assert_eq!(report(&output)["daemon_status"], "ok");
    let fixture = Fixture::start_with(Setup {
        health: vec![Reply::Http(
            200,
            json!({"status":"https://example.invalid/status"}).to_string(),
        )],
        ..Setup::default()
    });
    let output = diagnose(&home, &fixture.url).output().unwrap();
    assert_eq!(
        report(&output)["daemon_status"],
        "https://example.invalid/status"
    );
    assert!(failures.is_empty(), "{failures:?}");
}
