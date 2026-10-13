mod common;
use common::*;
use serde_json::{Value, json};

fn initialize(shim: &mut Shim) {
    shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"wire-contract","version":"1"}}}));
    assert!(shim.receive().get("result").is_some());
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
}
fn request(shim: &mut Shim, id: &str, method: &str, params: Value) -> Value {
    shim.send(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}));
    let response = loop {
        let response = shim.receive();
        if response.get("id").is_some() {
            break response;
        }
        assert_eq!(response["method"], "notifications/tools/list_changed");
    };
    assert_eq!(response["id"], id);
    response
}
fn call(shim: &mut Shim, id: &str) -> Value {
    request(
        shim,
        id,
        "tools/call",
        json!({"name":"write","arguments":{"request_id":"fixture-write"}}),
    )
}
fn success() -> Reply {
    Reply::Result(json!({"content":[{"type":"text","text":"complete"}],"isError":false}))
}
fn failure_then_recovery(reply: Reply, classification: &str, outcome: &str, code: i32) -> Value {
    let fixture = Fixture::start_with(Setup {
        calls: vec![reply, success()],
        ..Default::default()
    });
    let mut shim =
        Shim::start_with_env(&fixture.url, &[("PSEUDOLIFE_MCP_TOKEN", "secret.invalid")]);
    initialize(&mut shim);
    let failed = call(&mut shim, "write");
    let error = failed.get("error").expect("call must fail");
    assert_eq!(error["code"], code);
    assert_eq!(error["data"]["classification"], classification);
    assert_eq!(error["data"]["operation_outcome"], outcome);
    assert_eq!(error["data"]["phase"], "call");
    assert!(!error.to_string().contains("secret.invalid"));
    let recovered = call(&mut shim, "next");
    assert_eq!(recovered["result"]["content"][0]["text"], "complete");
    assert!(shim.finish().is_empty());
    assert!(!shim.stderr().contains("secret.invalid"));
    fixture.assert_deleted();
    let records = fixture.records.lock().unwrap();
    assert_eq!(
        records
            .iter()
            .filter(|r| r.message["method"] == "tools/call")
            .count(),
        2,
        "failed write was never replayed"
    );
    let sessions: Vec<_> = records
        .iter()
        .filter(|r| r.message["method"] == "tools/call")
        .map(|r| r.headers["mcp-session-id"].clone())
        .collect();
    assert_ne!(sessions[0], sessions[1], "fresh upstream per operation");
    failed
}
#[test]
fn test_http_failure_is_sanitized_and_next_call_recovers() {
    for (status, classification) in [
        (401, "authentication_required"),
        (503, "service_unavailable"),
    ] {
        let fixture = Fixture::start_with(Setup {
            calls: vec![Reply::Http(
                status,
                "secret.invalid http://private.invalid BODY_MARKER".into(),
            )],
            ..Default::default()
        });
        let mut shim =
            Shim::start_with_env(&fixture.url, &[("PSEUDOLIFE_MCP_TOKEN", "secret.invalid")]);
        initialize(&mut shim);
        let response = request(
            &mut shim,
            "fail",
            "tools/call",
            json!({"name":"read","arguments":{}}),
        );
        assert_eq!(response["error"]["code"], -32603);
        assert_eq!(
            response["error"]["data"],
            json!({"classification":classification,"phase":"call","operation_outcome":"unknown"})
        );
        assert!(
            response["error"]["message"]
                .as_str()
                .unwrap()
                .contains("Check its result before retrying")
        );
        let recovered = request(
            &mut shim,
            "next",
            "tools/call",
            json!({"name":"read","arguments":{}}),
        );
        assert_eq!(
            serde_json::from_str::<Value>(
                recovered["result"]["content"][0]["text"].as_str().unwrap()
            )
            .unwrap()["writes"],
            0
        );
        assert!(shim.finish().is_empty());
        for marker in ["secret.invalid", "private.invalid", "BODY_MARKER"] {
            assert!(!response.to_string().contains(marker));
            assert!(!shim.stderr().contains(marker));
        }
        fixture.assert_deleted();
        assert_eq!(fixture.writes.load(std::sync::atomic::Ordering::SeqCst), 0);
    }
}
#[test]
fn test_principals_unavailable_refusal_is_definitely_not_executed() {
    for (reply, initial, phase) in [
        (
            Reply::Http(503, r#"{"error":"principals_unavailable"}"#.into()),
            false,
            "call",
        ),
        (
            Reply::RpcError(
                -32003,
                "principals_unavailable".into(),
                Some(json!({"status":503,"error":"principals_unavailable"})),
            ),
            false,
            "call",
        ),
        (
            Reply::Http(503, r#"{"error":"principals_unavailable"}"#.into()),
            true,
            "initialize",
        ),
    ] {
        refusal_then_recovery(
            reply,
            initial,
            -32003,
            "principals_unavailable",
            phase,
            503,
            "principals_unavailable",
        );
    }
}
#[test]
fn test_revoked_token_refusal_is_definitely_not_executed() {
    refusal_then_recovery(
        Reply::RpcError(
            -32004,
            "unauthorized".into(),
            Some(json!({"status":401,"error":"unauthorized"})),
        ),
        false,
        -32004,
        "authentication_required",
        "call",
        401,
        "unauthorized",
    );
}
fn refusal_then_recovery(
    reply: Reply,
    initial: bool,
    code: i32,
    classification: &str,
    phase: &str,
    status: u16,
    name: &str,
) {
    let completed =
        Reply::Result(json!({"content":[{"type":"text","text":"{\"writes\":1}"}],"isError":false}));
    let setup = if initial {
        Setup {
            initialize: vec![
                Reply::Result(
                    json!({"protocolVersion":"2025-11-25","capabilities":{},"serverInfo":{"name":"fixture","version":"1"}}),
                ),
                reply,
            ],
            calls: vec![completed],
            ..Default::default()
        }
    } else {
        Setup {
            calls: vec![reply, completed],
            ..Default::default()
        }
    };
    let fixture = Fixture::start_with(setup);
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    let failed = call(&mut shim, "refused");
    assert_eq!(failed["error"]["code"], code);
    let mut data = json!({"classification":classification,"phase":phase,"operation_outcome":"not_dispatched","status":status,"error":name});
    if code == -32003 {
        data["hint"] = json!(
            "the daemon cannot check invited machines' tokens right now (its database is not answering); try again shortly"
        );
    }
    assert_eq!(failed["error"]["data"], data);
    assert!(
        failed["error"]["message"]
            .as_str()
            .unwrap()
            .contains("No tool ran")
    );
    assert!(
        !failed["error"]["message"]
            .as_str()
            .unwrap()
            .contains("may have completed")
    );
    if code == -32004 {
        assert!(
            failed["error"]["message"]
                .as_str()
                .unwrap()
                .contains("no longer accepts this token")
        );
    }
    assert_eq!(fixture.writes.load(std::sync::atomic::Ordering::SeqCst), 0);
    let recovered = call(&mut shim, "next");
    assert_eq!(
        serde_json::from_str::<Value>(recovered["result"]["content"][0]["text"].as_str().unwrap())
            .unwrap()["writes"],
        1
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    assert_eq!(fixture.writes.load(std::sync::atomic::Ordering::SeqCst), 1);
}
#[test]
fn test_inexact_principals_refusal_stays_unknown() {
    let cases = vec![
        (
            Reply::RpcError(
                -32003,
                "secret.invalid".into(),
                Some(json!({"status":503,"error":"principals_unavailable"})),
            ),
            -32003,
            "protocol",
        ),
        (
            Reply::RpcError(
                -32003,
                "principals_unavailable".into(),
                Some(json!({"status":"503","error":"principals_unavailable"})),
            ),
            -32003,
            "protocol",
        ),
        (
            Reply::RpcError(
                -32003,
                "principals_unavailable".into(),
                Some(
                    json!({"status":503,"error":"principals_unavailable","credential":"secret.invalid"}),
                ),
            ),
            -32003,
            "protocol",
        ),
        (
            Reply::RpcError(-32003, "principals_unavailable".into(), None),
            -32003,
            "protocol",
        ),
        (
            Reply::Http(
                503,
                r#"{"error":"principals_unavailable","credential":"secret.invalid"}"#.into(),
            ),
            -32603,
            "service_unavailable",
        ),
        (
            Reply::Http(
                503,
                r#"{"error":"principals_unavailable","error":"principals_unavailable"}"#.into(),
            ),
            -32603,
            "service_unavailable",
        ),
        (
            Reply::Http(503, r#"{"error":"principals_unavailable""#.into()),
            -32603,
            "service_unavailable",
        ),
        (
            Reply::Http(
                503,
                format!(
                    "{}{}",
                    r#"{"error":"principals_unavailable"}"#,
                    " ".repeat(257)
                ),
            ),
            -32603,
            "service_unavailable",
        ),
        (
            Reply::HttpType(
                503,
                "text/plain",
                r#"{"error":"principals_unavailable"}"#.into(),
            ),
            -32603,
            "service_unavailable",
        ),
        (
            Reply::RpcError(
                -32004,
                "unauthorized".into(),
                Some(json!({"status":"401","error":"unauthorized"})),
            ),
            -32004,
            "protocol",
        ),
        (
            Reply::RpcError(
                -32004,
                "secret.invalid".into(),
                Some(json!({"status":401,"error":"unauthorized"})),
            ),
            -32004,
            "protocol",
        ),
        (
            Reply::RpcError(
                -32004,
                "unauthorized".into(),
                Some(json!({"status":401,"error":"unauthorized","credential":"secret.invalid"})),
            ),
            -32004,
            "protocol",
        ),
    ];
    for (reply, code, classification) in cases {
        let failed = failure_then_recovery(reply, classification, "unknown", code);
        assert_eq!(
            failed["error"]["data"],
            json!({"classification":classification,"phase":"call","operation_outcome":"unknown"})
        );
        assert!(
            failed["error"]["message"]
                .as_str()
                .unwrap()
                .contains("Check its result before retrying")
        );
    }
}
#[test]
fn test_write_committed_before_503_is_unknown_and_never_replayed() {
    committed_failure_then_read(
        Reply::Http(503, "{}".into()),
        "service_unavailable",
        "unknown",
        -32603,
    );
}
#[test]
fn test_write_committed_before_401_has_safe_unknown_outcome_guidance() {
    committed_failure_then_read(
        Reply::Http(401, "secret.invalid".into()),
        "authentication_required",
        "unknown",
        -32603,
    );
}
#[test]
fn test_stream_cut_after_dispatch_is_reported_as_a_lost_response() {
    let failed = failure_then_recovery(Reply::Cut, "response_lost", "unknown", -32603);
    assert_eq!(
        failed["error"]["data"],
        json!({"classification":"response_lost","phase":"call","operation_outcome":"unknown"})
    );
    assert!(
        failed["error"]["message"]
            .as_str()
            .unwrap()
            .starts_with("The memory daemon's response stream closed before a result arrived")
    );
}
#[test]
fn test_lost_write_response_is_unknown_and_never_replayed() {
    committed_failure_then_read(Reply::Cut, "response_lost", "unknown", -32603);
}
fn committed_failure_then_read(reply: Reply, classification: &str, outcome: &str, code: i32) {
    let fixture = Fixture::start_with(Setup {
        calls: vec![Reply::AfterCommit(Box::new(reply))],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    let failed = call(&mut shim, "write");
    assert_eq!(failed["error"]["code"], code);
    assert_eq!(
        failed["error"]["data"],
        json!({"classification":classification,"phase":"call","operation_outcome":outcome})
    );
    assert!(
        failed["error"]["message"]
            .as_str()
            .unwrap()
            .contains("Check its result before retrying")
    );
    assert!(
        failed["error"]["message"]
            .as_str()
            .unwrap()
            .contains("same request_id")
    );
    assert!(!failed.to_string().contains("secret.invalid"));
    assert_eq!(fixture.writes.load(std::sync::atomic::Ordering::SeqCst), 1);
    let recovered = request(
        &mut shim,
        "read",
        "tools/call",
        json!({"name":"read","arguments":{}}),
    );
    let observed: Value =
        serde_json::from_str(recovered["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(observed["writes"], 1);
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    assert_eq!(
        fixture.writes.load(std::sync::atomic::Ordering::SeqCst),
        1,
        "committed write replayed"
    );
    assert_eq!(
        fixture
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.message["method"] == "tools/call")
            .count(),
        2
    );
}
#[test]
fn test_closed_listener_recovers_on_next_operation() {
    let fixture = Fixture::start();
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    fixture
        .mcp_enabled
        .store(false, std::sync::atomic::Ordering::SeqCst);
    let failed = request(
        &mut shim,
        "offline",
        "tools/call",
        json!({"name":"read","arguments":{}}),
    );
    assert_eq!(
        failed["error"]["data"],
        json!({"classification":"connection_failure","phase":"initialize","operation_outcome":"not_dispatched"})
    );
    fixture
        .mcp_enabled
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let recovered = request(
        &mut shim,
        "read",
        "tools/call",
        json!({"name":"read","arguments":{}}),
    );
    assert_eq!(
        serde_json::from_str::<Value>(recovered["result"]["content"][0]["text"].as_str().unwrap())
            .unwrap()["writes"],
        0
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}
#[test]
fn test_protocol_code_is_preserved_only_from_the_sdk_error_type() {
    failure_then_recovery(
        Reply::RpcError(
            -32602,
            "secret.invalid".into(),
            Some(json!({"secret":"secret.invalid"})),
        ),
        "protocol",
        "unknown",
        -32602,
    );
}
#[test]
fn test_daemon_refusal_on_tools_list_keeps_its_code() {
    for (code, name, status, classification) in [
        (
            -32003,
            "principals_unavailable",
            503,
            "principals_unavailable",
        ),
        (-32004, "unauthorized", 401, "authentication_required"),
    ] {
        let fixture = Fixture::start_with(Setup {
            lists: vec![Reply::RpcError(
                code,
                name.into(),
                Some(json!({"status":status,"error":name})),
            )],
            ..Default::default()
        });
        let mut shim = Shim::start(&fixture.url);
        initialize(&mut shim);
        let response = request(&mut shim, "list", "tools/list", json!({}));
        assert_eq!(response["error"]["code"], code, "response: {response}");
        assert_eq!(response["error"]["data"]["classification"], classification);
        assert_eq!(response["error"]["data"]["phase"], "list");
        assert_eq!(
            response["error"]["data"]["operation_outcome"],
            "not_dispatched"
        );
        assert!(shim.finish().is_empty());
        fixture.assert_deleted();
    }
}
#[test]
fn test_large_tool_results_survive_the_client_sse_event_cap() {
    let text = "x".repeat(1_400_000);
    let fixture = Fixture::start_with(Setup {
        calls: vec![Reply::Sse(json!({"content":[{"type":"text","text":text}]}))],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    let response = call(&mut shim, "large");
    assert_eq!(response["result"]["isError"], false);
    assert_eq!(
        response["result"]["content"][0]["text"]
            .as_str()
            .unwrap()
            .len(),
        1_400_000
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}
#[test]
fn future_cache_hints_do_not_break_legacy_upstream_list() {
    let fixture = Fixture::start_with(Setup {
        lists: vec![Reply::Result(
            json!({"ttlMs":-50,"cacheScope":"unrecognized","resultType":"complete","tools":[{"name":"fixture","inputSchema":{"type":"object"}}]}),
        )],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    let response = request(&mut shim, "list", "tools/list", json!({}));
    assert_eq!(
        response["result"]["tools"][0]["name"], "fixture",
        "response: {response}"
    );
    assert!(response["result"].get("ttlMs").is_none());
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}
#[test]
fn pagination_and_list_meta_are_forwarded_and_call_meta_is_consumed() {
    let fixture = Fixture::start_with(Setup {
        calls: vec![success()],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    request(
        &mut shim,
        "list",
        "tools/list",
        json!({"cursor":"opaque","_meta":{"fixture":18446744073709551615u64}}),
    );
    request(
        &mut shim,
        "call",
        "tools/call",
        json!({"name":"write","arguments":{},"_meta":{"fixture":"private"}}),
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    let records = fixture.records.lock().unwrap();
    let list = records
        .iter()
        .find(|r| r.message["method"] == "tools/list")
        .unwrap();
    assert_eq!(list.message["params"]["cursor"], "opaque");
    assert_eq!(
        list.message["params"]["_meta"]["fixture"],
        json!(18446744073709551615u64)
    );
    let call = records
        .iter()
        .find(|r| r.message["method"] == "tools/call")
        .unwrap();
    assert_eq!(call.message["params"]["_meta"], json!({}));
}
#[test]
fn test_mcp_transport_never_follows_cross_origin_redirect() {
    let target = Fixture::start();
    let source = Fixture::start_with(Setup {
        calls: vec![Reply::Redirect(format!("{}/mcp", target.url)), success()],
        ..Default::default()
    });
    let mut shim = Shim::start(&source.url);
    initialize(&mut shim);
    assert!(call(&mut shim, "redirect").get("error").is_some());
    assert_eq!(
        call(&mut shim, "next")["result"]["content"][0]["text"],
        "complete"
    );
    assert!(shim.finish().is_empty());
    assert!(
        target.records.lock().unwrap().is_empty(),
        "redirect target received credentials or any request"
    );
    source.assert_deleted();
}
#[test]
fn test_cancellation_remains_cancellation() {
    let fixture = Fixture::start_with(Setup {
        calls: vec![Reply::Hang, success()],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    initialize(&mut shim);
    shim.send(json!({"jsonrpc":"2.0","id":"cancel","method":"tools/call","params":{"name":"write","arguments":{}}}));
    assert!(fixture.wait_calls(1));
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"cancel","reason":"contract"}}));
    assert_eq!(
        call(&mut shim, "next")["result"]["content"][0]["text"],
        "complete"
    );
    assert!(
        shim.finish().is_empty(),
        "cancelled handler must not emit a final response"
    );
    fixture.assert_deleted();
}
#[test]
fn test_call_timeout_is_unknown_and_next_operation_recovers() {
    let fixture = Fixture::start_with(Setup {
        calls: vec![Reply::Hang],
        ..Default::default()
    });
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "0.15")],
    );
    initialize(&mut shim);
    let response = request(
        &mut shim,
        "timeout",
        "tools/call",
        json!({"name":"read","arguments":{}}),
    );
    assert_eq!(
        response["error"]["data"],
        json!({"classification":"timeout","phase":"call","operation_outcome":"unknown"})
    );
    let recovered = request(
        &mut shim,
        "next",
        "tools/call",
        json!({"name":"read","arguments":{}}),
    );
    assert_eq!(
        serde_json::from_str::<Value>(recovered["result"]["content"][0]["text"].as_str().unwrap())
            .unwrap()["writes"],
        0
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}
