mod board_fixture;
mod common;

use common::{Fixture, Reply, Setup, Shim};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    process::Command,
    thread,
    time::{Duration, Instant},
};

fn initialize(shim: &mut Shim) {
    shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"claims-contract","version":"1"}}}));
    assert!(shim.receive().get("result").is_some());
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
}

fn request(shim: &mut Shim, id: &str, name: &str, arguments: Value) -> Value {
    shim.send(json!({"jsonrpc":"2.0","id":id,"method":"tools/call","params":{"name":name,"arguments":arguments}}));
    response(shim, id)
}

fn response(shim: &Shim, id: &str) -> Value {
    loop {
        let frame = shim.receive();
        if frame.get("id").is_some() {
            assert_eq!(frame["id"], id);
            return frame;
        }
        assert_eq!(frame["method"], "notifications/tools/list_changed");
    }
}

fn arguments(fixture: &Fixture) -> Vec<Value> {
    fixture
        .records
        .lock()
        .unwrap()
        .iter()
        .filter(|record| record.message["method"] == "tools/call")
        .map(|record| record.message["params"]["arguments"].clone())
        .collect()
}

fn checkout(home: &board_fixture::Home) -> PathBuf {
    let root = home.0.join("checkout");
    std::fs::create_dir(&root).unwrap();
    let result = Command::new("git")
        .arg("-C")
        .arg(&root)
        .arg("init")
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .unwrap();
    assert!(result.status.success(), "disposable git init failed");
    root
}

// A standalone Rust child makes local preparation slow without changing the
// proxy or using a shell. Its only effects are two markers in the owned home.
fn delayed_git(home: &board_fixture::Home) -> (PathBuf, PathBuf, String) {
    let source = home.0.join("git.rs");
    let binary = home.0.join(if cfg!(windows) { "git.exe" } else { "git" });
    std::fs::write(&source, r#"
fn main() {
    let marker = std::env::var_os("CLAIMS_PREPARATION_MARKER").unwrap();
    std::fs::write(&marker, std::process::id().to_string()).unwrap();
    std::thread::sleep(std::time::Duration::from_secs(1));
    std::fs::write(std::path::PathBuf::from(marker).with_extension("finished"), b"finished").unwrap();
    let root = std::path::PathBuf::from(std::env::args_os().nth(2).unwrap());
    println!("{}\n{}\n{}", root.display(), root.join(".git").display(), root.join(".git").display());
}
"#).unwrap();
    let compiled = Command::new(std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into()))
        .arg("--edition=2024")
        .arg(&source)
        .arg("-o")
        .arg(&binary)
        .output()
        .unwrap();
    assert!(
        compiled.status.success(),
        "{}",
        String::from_utf8_lossy(&compiled.stderr)
    );
    let mut paths = vec![home.0.clone()];
    paths.extend(std::env::split_paths(&std::env::var_os("PATH").unwrap()));
    let path = std::env::join_paths(paths)
        .unwrap()
        .to_string_lossy()
        .into_owned();
    (
        home.0.join("started"),
        home.0.join("started.finished"),
        path,
    )
}

fn wait_started(marker: &std::path::Path) {
    let deadline = Instant::now() + Duration::from_secs(2);
    while !marker.exists() {
        assert!(Instant::now() < deadline, "local preparation did not start");
        thread::sleep(Duration::from_millis(2));
    }
}

#[test]
fn test_file_claim_is_prepared_locally_before_remote_call() {
    let home = board_fixture::Home::new();
    let root = checkout(&home);
    let fixture = Fixture::start_with(Setup {
        calls: (0..3)
            .map(|_| {
                Reply::Result(
                    json!({"content":[{"type":"text","text":"complete"}],"isError":false}),
                )
            })
            .collect(),
        ..Default::default()
    });
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[
            ("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "5"),
            ("PSEUDOLIFE_MCP_SHARED_HOST", "0"),
        ],
    );
    initialize(&mut shim);
    let claimed = request(
        &mut shim,
        "claim",
        "memory_agents",
        json!({"action":"claim","worktree":root,"path":"src\\file.py"}),
    );
    assert_eq!(claimed["result"]["isError"], false, "{claimed}");
    let prepared = arguments(&fixture).pop().unwrap();
    assert!(prepared.get("worktree").is_none());
    assert_eq!(prepared["repository_id"].as_str().unwrap().len(), 64);
    assert_eq!(prepared["path"], "src/file.py");
    let released = request(
        &mut shim,
        "release",
        "memory_agents",
        json!({"action":"release","worktree":root,"path":"src/file.py"}),
    );
    assert_eq!(released["result"]["isError"], false);
    assert_eq!(
        arguments(&fixture).last().unwrap()["repository_id"],
        prepared["repository_id"]
    );
    let bad = request(
        &mut shim,
        "bad",
        "memory_agents",
        json!({"action":"claim","worktree":root,"path":"../outside.py"}),
    );
    assert_eq!(bad["result"]["isError"], true);
    assert_eq!(bad["result"]["content"][0]["text"], "invalid_claim_path");
    let generic = request(
        &mut shim,
        "generic",
        "memory_agents",
        json!({"action":"claim","lease":"gpu"}),
    );
    assert_eq!(generic["result"]["isError"], false);
    let calls = arguments(&fixture);
    assert_eq!(calls.len(), 3);
    assert!(
        !serde_json::to_string(&calls)
            .unwrap()
            .contains(&root.to_string_lossy().replace('\\', "\\\\"))
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}

#[test]
fn test_shared_process_refuses_file_claim_before_inspecting_local_paths() {
    let home = board_fixture::Home::new();
    let plain = home.0.join("not-a-checkout");
    std::fs::create_dir(&plain).unwrap();
    let (marker, finished, path) = delayed_git(&home);
    let fixture = Fixture::start();
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[
            ("PSEUDOLIFE_MCP_SHARED_HOST", "1"),
            ("PATH", &path),
            ("CLAIMS_PREPARATION_MARKER", marker.to_str().unwrap()),
        ],
    );
    initialize(&mut shim);
    for (id, path) in [("ordinary", "file.py"), ("traversal", "../outside.py")] {
        let refused = request(
            &mut shim,
            id,
            "memory_agents",
            json!({"action":"claim","worktree":plain,"path":path}),
        );
        assert_eq!(refused["error"]["code"], -32603);
        assert_eq!(
            refused["error"]["message"],
            pseudolife_stdio::board::policy::SHARED_REFUSAL
        );
        assert_eq!(
            refused["error"]["data"],
            json!({"classification":"coordination_unavailable","phase":"initialize","operation_outcome":"not_dispatched"})
        );
    }
    assert!(arguments(&fixture).is_empty());
    assert!(
        !marker.exists(),
        "shared refusal must precede git execution"
    );
    assert!(!finished.exists());
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}

#[test]
fn test_file_claim_preparation_uses_total_deadline_and_next_call_recovers() {
    let home = board_fixture::Home::new();
    let root = checkout(&home);
    let (marker, finished, path) = delayed_git(&home);
    let fixture = Fixture::start();
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[
            ("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "0.15"),
            ("PSEUDOLIFE_MCP_SHARED_HOST", "0"),
            ("PATH", &path),
            ("CLAIMS_PREPARATION_MARKER", marker.to_str().unwrap()),
        ],
    );
    initialize(&mut shim);
    let started = Instant::now();
    let failed = request(
        &mut shim,
        "claim",
        "memory_agents",
        json!({"action":"claim","worktree":root,"path":"file.py"}),
    );
    assert!(started.elapsed() < Duration::from_millis(700));
    assert!(
        marker.exists(),
        "the timeout must occur during local preparation"
    );
    assert_eq!(failed["error"]["code"], -32603);
    assert_eq!(
        failed["error"]["data"],
        json!({"classification":"timeout","phase":"initialize","operation_outcome":"not_dispatched"})
    );
    assert_eq!(
        request(&mut shim, "recovered", "read", json!({}))["result"]["isError"],
        false
    );
    thread::sleep(Duration::from_millis(1100));
    assert!(!finished.exists(), "timed-out git child must be reaped");
    assert_eq!(arguments(&fixture).len(), 1);
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}

#[test]
fn test_cancelled_file_claim_preparation_cannot_dispatch_later() {
    let home = board_fixture::Home::new();
    let root = checkout(&home);
    let (marker, finished, path) = delayed_git(&home);
    let fixture = Fixture::start();
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[
            ("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "5"),
            ("PSEUDOLIFE_MCP_SHARED_HOST", "0"),
            ("PATH", &path),
            ("CLAIMS_PREPARATION_MARKER", marker.to_str().unwrap()),
        ],
    );
    initialize(&mut shim);
    shim.send(json!({"jsonrpc":"2.0","id":"claim","method":"tools/call","params":{"name":"memory_agents","arguments":{"action":"claim","worktree":root,"path":"file.py"}}}));
    wait_started(&marker);
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"claim","reason":"fixture cancellation"}}));
    thread::sleep(Duration::from_millis(1100));
    assert!(!finished.exists(), "cancelled git child must be reaped");
    assert!(arguments(&fixture).is_empty());
    assert_eq!(
        request(&mut shim, "recovered", "read", json!({}))["result"]["isError"],
        false
    );
    assert_eq!(arguments(&fixture).len(), 1);
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}
