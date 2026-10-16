mod common;
use common::*;
use serde_json::json;
fn initialize(shim: &mut Shim) -> serde_json::Value {
    shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"cache-contract","version":"1"}}}));
    let response = shim.receive();
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    response
}
fn list(shim: &mut Shim) -> serde_json::Value {
    shim.send(json!({"jsonrpc":"2.0","id":"list","method":"tools/list","params":{}}));
    shim.receive()
}
#[test]
fn test_shim_initializes_after_instruction_fetch_refused_and_recovers() {
    let fixture = Fixture::start_with(Setup {
        initialize: vec![Reply::Http(401, "{}".into())],
        ..Default::default()
    });
    let mut shim =
        Shim::start_with_env(&fixture.url, &[("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")]);
    let initialized = initialize(&mut shim);
    assert!(initialized.get("result").is_some());
    assert!(initialized["result"].get("instructions").is_none());
    assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "fixture");
    assert!(shim.finish().is_empty());
    assert!(shim.stderr().contains("instructions unavailable"));
    assert!(!shim.stderr().contains("fixture-secret"));
    assert!(!shim.stderr().contains("Traceback"));
    assert_eq!(
        fixture
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|record| record.message["method"] == "initialize")
            .count(),
        2
    );
    fixture.assert_deleted();
}
#[test]
fn test_shim_caches_the_handshake_of_a_healthy_daemon() {
    let fixture = Fixture::start();
    let home =
        std::env::temp_dir().join(format!("pseudolife-stdio-cache-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&home).unwrap();
    {
        let mut shim = Shim::start_with_env(
            &fixture.url,
            &[("PSEUDOLIFE_AGENT_STATE_DIR", home.to_str().unwrap())],
        );
        assert_eq!(
            initialize(&mut shim)["result"]["instructions"],
            "Disposable hanging-call fixture."
        );
        assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "fixture");
        assert!(shim.finish().is_empty());
        fixture.assert_deleted();
    }
    let cache = pseudolife_stdio::cache::HandshakeCache::new(&fixture.url, &home).load();
    assert_eq!(cache["instructions"], "Disposable hanging-call fixture.");
    assert_eq!(cache["tools"][0]["name"], "fixture");
    std::fs::remove_dir_all(home).unwrap();
}
#[test]
fn test_shim_starts_without_its_daemon_and_recovers_when_it_answers() {
    for cached in [false, true] {
        let fixture = Fixture::start_with(Setup {
            calls: vec![Reply::Result(
                json!({"content":[{"type":"text","text":"live answer"}],"isError":false}),
            )],
            ..Default::default()
        });
        let home =
            std::env::temp_dir().join(format!("pseudolife-wire-cache-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&home).unwrap();
        let cache = pseudolife_stdio::cache::HandshakeCache::new(&fixture.url, &home);
        if cached {
            cache.remember_instructions(Some("Cached daemon instructions."));
            cache.remember_tools(vec![
                json!({"name":"cached_tool","inputSchema":{"type":"object"}}),
            ]);
        }
        fixture
            .health_enabled
            .store(false, std::sync::atomic::Ordering::SeqCst);
        fixture
            .mcp_enabled
            .store(false, std::sync::atomic::Ordering::SeqCst);
        let mut shim = Shim::start_with_env(
            &fixture.url,
            &[("PSEUDOLIFE_AGENT_STATE_DIR", home.to_str().unwrap())],
        );
        let started = initialize(&mut shim);
        assert!(
            started["result"]["instructions"]
                .as_str()
                .unwrap()
                .contains("did not answer when this session started")
        );
        if cached {
            assert!(
                started["result"]["instructions"]
                    .as_str()
                    .unwrap()
                    .contains("Cached daemon instructions.")
            );
            assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "cached_tool");
        } else {
            assert_eq!(list(&mut shim)["result"]["tools"], json!([]));
        }
        shim.send(json!({"jsonrpc":"2.0","id":"offline","method":"tools/call","params":{"name":"fixture","arguments":{}}}));
        let failed = shim.receive();
        assert_eq!(
            failed["error"]["data"]["classification"],
            "connection_failure"
        );
        assert_eq!(
            failed["error"]["data"]["operation_outcome"],
            "not_dispatched"
        );
        assert!(
            failed["error"]["message"]
                .as_str()
                .unwrap()
                .contains("unreachable")
        );
        assert!(
            !fixture
                .records
                .lock()
                .unwrap()
                .iter()
                .any(|request| request.message["method"] == "tools/call")
        );
        fixture
            .mcp_enabled
            .store(true, std::sync::atomic::Ordering::SeqCst);
        fixture
            .health_enabled
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let notice = shim.receive();
        assert_eq!(notice["method"], "notifications/tools/list_changed");
        assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "fixture");
        shim.send(json!({"jsonrpc":"2.0","id":"live","method":"tools/call","params":{"name":"fixture","arguments":{}}}));
        let live = shim.receive();
        assert_eq!(live["result"]["content"][0]["text"], "live answer");
        assert_eq!(live["result"]["isError"], false);
        assert!(shim.finish().is_empty());
        assert!(!shim.stderr().contains("Traceback"));
        fixture.assert_deleted();
        assert_eq!(cache.load()["tools"][0]["name"], "fixture");
        assert_eq!(
            fixture
                .records
                .lock()
                .unwrap()
                .iter()
                .filter(|record| record.message["method"] == "tools/call")
                .count(),
            1
        );
        std::fs::remove_dir_all(home).unwrap();
    }
}

#[test]
fn test_a_call_that_gets_through_announces_the_recovery_itself() {
    let fixture = Fixture::start_with(Setup {
        calls: vec![Reply::Result(
            json!({"content":[{"type":"text","text":"live answer"}]}),
        )],
        ..Default::default()
    });
    fixture
        .health_enabled
        .store(false, std::sync::atomic::Ordering::SeqCst);
    let mut shim = Shim::start(&fixture.url);
    assert!(
        initialize(&mut shim)["result"]["instructions"]
            .as_str()
            .unwrap()
            .contains("did not answer")
    );
    assert_eq!(list(&mut shim)["result"]["tools"], json!([]));
    shim.send(json!({"jsonrpc":"2.0","id":"live","method":"tools/call","params":{"name":"fixture","arguments":{}}}));
    let first = shim.receive();
    let second = shim.receive();
    let (notice, result) = if first.get("method").is_some() {
        (first, second)
    } else {
        (second, first)
    };
    assert_eq!(notice["method"], "notifications/tools/list_changed");
    assert_eq!(result["result"]["content"][0]["text"], "live answer");
    assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "fixture");
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}

#[test]
fn test_a_daemon_whose_health_answers_but_mcp_drops_does_not_flap_the_tool_list() {
    let fixture = Fixture::start();
    fixture
        .mcp_enabled
        .store(false, std::sync::atomic::Ordering::SeqCst);
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    assert_eq!(list(&mut shim)["result"]["tools"], json!([]));
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(12);
    let mut notices = 0;
    while let Some(remaining) = deadline.checked_duration_since(std::time::Instant::now()) {
        match shim.output.recv_timeout(remaining) {
            Ok(notice) => {
                assert_eq!(notice["method"], "notifications/tools/list_changed");
                notices += 1;
                assert_eq!(list(&mut shim)["result"]["tools"], json!([]));
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => break,
            Err(error) => panic!("recovery shim stopped: {error}"),
        }
    }
    assert!(
        (1..=4).contains(&notices),
        "false-recovery notices: {notices}"
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}

#[test]
fn cursor_pages_do_not_replace_the_cursorless_recovery_cache() {
    let fixture = Fixture::start_with(Setup {
        lists: vec![
            Reply::Result(json!({"tools":[{"name":"full","inputSchema":{"type":"object"}}]})),
            Reply::Result(json!({"tools":[{"name":"page","inputSchema":{"type":"object"}}]})),
        ],
        ..Default::default()
    });
    let home =
        std::env::temp_dir().join(format!("pseudolife-stdio-cache-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&home).unwrap();
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[("PSEUDOLIFE_AGENT_STATE_DIR", home.to_str().unwrap())],
    );
    initialize(&mut shim);
    assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "full");
    shim.send(
        json!({"jsonrpc":"2.0","id":"page","method":"tools/list","params":{"cursor":"page-token"}}),
    );
    assert_eq!(shim.receive()["result"]["tools"][0]["name"], "page");
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    let cache = pseudolife_stdio::cache::HandshakeCache::new(&fixture.url, &home).load();
    assert_eq!(cache["tools"][0]["name"], "full");
    std::fs::remove_dir_all(home).unwrap();
}

#[test]
fn optional_instruction_fetch_stall_is_bounded_and_next_list_recovers() {
    let fixture = Fixture::start_with(Setup {
        initialize: vec![Reply::Hang],
        ..Default::default()
    });
    let mut shim =
        Shim::start_with_env(&fixture.url, &[("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")]);
    let start = std::time::Instant::now();
    let initialized = initialize(&mut shim);
    assert!(initialized.get("result").is_some());
    assert!(initialized["result"].get("instructions").is_none());
    assert!(start.elapsed() < std::time::Duration::from_secs(9));
    assert_eq!(list(&mut shim)["result"]["tools"][0]["name"], "fixture");
    assert!(shim.finish().is_empty());
    assert!(shim.stderr().contains("instructions unavailable"));
    assert!(!shim.stderr().contains("fixture-secret"));
    assert!(!shim.stderr().contains("Traceback"));
    assert_eq!(
        fixture
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|record| record.message["method"] == "initialize")
            .count(),
        2
    );
    fixture.assert_deleted();
}
