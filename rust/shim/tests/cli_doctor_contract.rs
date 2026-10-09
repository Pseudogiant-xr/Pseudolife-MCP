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
fn canonical_saved_state_requests_stay_deferred_without_mutation() {
    let home = Home::new();
    let path = home.0.join("owned-state.json");
    let sentinel = b"owned state sentinel\n";
    std::fs::write(&path, sentinel).unwrap();
    for dsn in ["", "postgresql://fixture.invalid/disposable"] {
        for proof_first in [None, Some(false), Some(true)] {
            let mut command = command(&home);
            command
                .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
                .arg("doctor");
            if proof_first == Some(true) {
                command.arg("--disposable-proof");
            }
            command.arg("--agent-state").arg(&path);
            if proof_first == Some(false) {
                command.arg("--disposable-proof");
            }
            deferred(command.output().unwrap());
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
fn nonempty_proof_and_unsupported_help_stay_deferred() {
    let home = Home::new();
    deferred(
        command(&home)
            .args(["doctor", "--disposable-proof", "--help"])
            .output()
            .unwrap(),
    );
    for dsn in [" ", "postgresql://fixture.invalid/disposable"] {
        deferred(
            command(&home)
                .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
                .args(["doctor", "--disposable-proof"])
                .output()
                .unwrap(),
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
            "This host has no verified idle wake path; pull mail on the next turn.",
        ),
        "claude-desktop" => (
            "unsupported",
            "Claude Desktop shares its MCP process across conversations; use a supported session host for a mailbox.",
        ),
        _ => (
            "unavailable",
            "Configured wake is not delivery evidence; verify enqueue, hint/ring, receive and explicit ack in a disposable bank.",
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
                "mcp",
                "credential_source",
                "board",
                "maintainer_passkeys",
                "daemon_status",
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
        assert_eq!(map["mcp"], json!("not installed"));
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
    assert_eq!(map["daemon_version"], json!(env!("CARGO_PKG_VERSION")));
    assert_eq!(map["codex_hooks"], json!("not-configured"));
    assert_eq!(map["instructions_present"], json!(true));
    assert_eq!(map["tool_count"], json!(4));
    assert_eq!(map["tools_missing_annotations"], json!(["bare"]));
    assert_eq!(map["coordination_tools_present"], json!(true));
    assert_eq!(map["ok"], json!(false));
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        map["recovery"],
        json!(
            "Check shim stderr and daemon MCP access, then compare daemon and shim versions; update the component missing instructions or annotations and reconnect."
        )
    );
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
    let cases: [(&str, Prepare); 9] = [
        ("saved instance", |home, command| {
            std::fs::write(home.0.join("state.json"), b"{}").unwrap();
            command.arg("--agent-state").arg(home.0.join("state.json"));
        }),
        ("saved tunnels", |home, _| {
            std::fs::create_dir_all(home.0.join(".pseudolife-mcp").join("tunnel")).unwrap();
        }),
        ("proxy", |_, command| {
            command.env("HTTP_PROXY", "http://127.0.0.1:3128");
        }),
        ("invalid daemon url", |_, command| {
            command.env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1/path");
        }),
        ("unusable token file", |home, command| {
            command.env("PSEUDOLIFE_MCP_TOKEN_FILE", home.0.join("absent-token"));
        }),
        ("token outside Latin-1", |_, command| {
            command.env("PSEUDOLIFE_MCP_TOKEN", "token-\u{4e00}");
        }),
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
fn daemon_answers_beyond_the_port_defer_before_the_handshake() {
    let digest = json!({"status": "ok", "version": env!("CARGO_PKG_VERSION"),
                        "hooks_digest": "0123456789abcdef"});
    let plugin = b"[plugins.\"pseudolife-memory@pseudolife-mcp\"]\nenabled = true\n";
    let cases: [(Reply, bool); 3] = [
        (Reply::Http(200, "[1, 2]".into()), false),
        (Reply::Http(200, json!({"status": 7}).to_string()), false),
        (Reply::Http(200, digest.to_string()), true),
    ];
    for (health, codex_plugin) in cases {
        let home = Home::new();
        if codex_plugin {
            std::fs::create_dir_all(home.0.join("codex")).unwrap();
            std::fs::write(home.0.join("codex").join("config.toml"), plugin).unwrap();
        }
        let fixture = Fixture::start_with(Setup {
            health: vec![health],
            ..Setup::default()
        });
        let before = tree(&home.0);
        let output = diagnose(&home, &fixture.url).output().unwrap();
        assert_deferred(&output);
        let records = fixture.records.lock().unwrap().clone();
        assert_eq!(records.len(), 1, "only doctor's own health probe");
        assert_eq!(
            (records[0].verb.as_str(), records[0].path.as_str()),
            ("GET", "/health")
        );
        assert_eq!(tree(&home.0), before);
    }
}
