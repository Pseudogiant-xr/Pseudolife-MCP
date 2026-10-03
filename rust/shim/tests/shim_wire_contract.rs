mod common;
use common::*;
use serde_json::{Value, json};
fn open(shim: &mut Shim, modern: bool) {
    shim.send(if modern {json!({"jsonrpc":"2.0","id":"open","method":"server/discover","params":{"_meta":meta()}})} else {json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"wire","version":"1"}}})});
    assert!(shim.receive().get("result").is_some());
    if !modern {
        shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    }
}
fn call(shim: &mut Shim, modern: bool) {
    let mut params = json!({"name":"memory_toolset","arguments":{"tags":"[\"decision\"]"}});
    if modern {
        params["_meta"] = meta();
    }
    shim.send(json!({"jsonrpc":"2.0","id":"call","method":"tools/call","params":params}));
}
fn changed() -> Reply {
    Reply::Result(
        json!({"content":[{"type":"text","text":"changed"}],"structuredContent":{"result":{"changed":true}},"isError":false}),
    )
}
#[test]
fn subscription_ack_precedes_filtered_toolset_change_and_cancellation_releases_listener() {
    let fixture = Fixture::start_with(Setup {
        calls: vec![changed()],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    open(&mut shim, true);
    shim.send(json!({"jsonrpc":"2.0","id":"listener","method":"subscriptions/listen","params":{"_meta":meta(),"notifications":{"toolsListChanged":true,"promptsListChanged":true,"resourcesListChanged":false,"resourceSubscriptions":["fixture://one"]}}}));
    let ack = shim.receive();
    assert_eq!(ack["method"], "notifications/subscriptions/acknowledged");
    assert_eq!(
        ack["params"]["notifications"],
        json!({"toolsListChanged":true,"promptsListChanged":true,"resourceSubscriptions":["fixture://one"]})
    );
    call(&mut shim, true);
    let first = shim.receive();
    let second = shim.receive();
    let (notice, result) = if first.get("method").is_some() {
        (first, second)
    } else {
        (second, first)
    };
    assert_eq!(
        notice,
        json!({"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{"_meta":{"io.modelcontextprotocol/subscriptionId":"listener"}}})
    );
    assert_eq!(result["id"], "call");
    assert_eq!(result["result"]["resultType"], "complete");
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"listener"}}));
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}
#[test]
fn legacy_toolset_change_notifies_the_downstream_and_preserves_stringified_arguments() {
    let fixture = Fixture::start_with(Setup {
        calls: vec![changed()],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    open(&mut shim, false);
    call(&mut shim, false);
    let first = shim.receive();
    let second = shim.receive();
    let (notice, result) = if first.get("method").is_some() {
        (first, second)
    } else {
        (second, first)
    };
    assert_eq!(notice["method"], "notifications/tools/list_changed");
    assert_eq!(result["id"], "call");
    assert!(result["result"].get("resultType").is_none());
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    assert_eq!(
        fixture
            .records
            .lock()
            .unwrap()
            .iter()
            .find(|r| r.message["method"] == "tools/call")
            .unwrap()
            .message["params"]["arguments"]["tags"],
        "[\"decision\"]"
    );
}
#[test]
fn exact_integer_structured_output_and_annotation_precision_survive_typed_sdk() {
    let result:Value=serde_json::from_str(r#"{"content":[{"type":"text","text":"fixture","annotations":{"priority":0.12345678901234568}}],"structuredContent":{"n":184467440737095516160,"negative":-184467440737095516160,"z":1,"a":2},"isError":true}"#).unwrap();
    let fixture = Fixture::start_with(Setup {
        calls: vec![Reply::Result(result.clone())],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    open(&mut shim, false);
    call(&mut shim, false);
    let response = shim.receive();
    assert_eq!(response["result"], result);
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    let frames = shim.raw();
    let raw = String::from_utf8(frames.last().unwrap().clone()).unwrap();
    assert!(raw.contains("184467440737095516160"));
    assert!(raw.contains("0.12345678901234568"));
    assert!(raw.contains(r#""z":1,"a":2"#));
}
#[test]
fn modern_list_adds_defaults_and_preserves_pagination_without_creating_a_listener() {
    let fixture = Fixture::start_with(Setup {
        lists: vec![Reply::Result(
            json!({"tools":[],"nextCursor":"next","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"upstream","version":"42"}}}),
        )],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    open(&mut shim, true);
    shim.send(json!({"jsonrpc":"2.0","id":"list","method":"tools/list","params":{"_meta":meta(),"cursor":"opaque"}}));
    let response = shim.receive();
    assert_eq!(response["result"]["nextCursor"], "next");
    assert_eq!(response["result"]["ttlMs"], 0);
    assert_eq!(response["result"]["cacheScope"], "private");
    assert_eq!(
        response["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
        "upstream"
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
}

#[test]
fn list_schema_member_order_survives_the_typed_sdk_and_cursorless_cache() {
    let tools:Value=serde_json::from_str(r#"[{"name":"fixture","inputSchema":{"type":"object","properties":{"z":{},"a":{}},"required":["z"],"x-extension":{"z":1,"a":2}},"outputSchema":{"type":"object","properties":{"b":{},"a":{}}}}]"#).unwrap();
    let fixture = Fixture::start_with(Setup {
        lists: vec![Reply::Result(json!({"tools":tools}))],
        ..Default::default()
    });
    let mut shim = Shim::start(&fixture.url);
    open(&mut shim, true);
    shim.send(json!({"jsonrpc":"2.0","id":"list","method":"tools/list","params":{"_meta":meta()}}));
    let response = shim.receive();
    assert!(response.get("result").is_some());
    assert_eq!(
        response["result"]["tools"][0]["inputSchema"]["x-extension"],
        json!({"z":1,"a":2})
    );
    assert_eq!(response["result"]["tools"][0].as_object().unwrap().len(), 3);
    let cached =
        pseudolife_stdio::cache::HandshakeCache::new(&fixture.url, shim.state_dir()).load();
    assert_eq!(
        cached["tools"][0]["inputSchema"].to_string(),
        tools[0]["inputSchema"].to_string()
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    let frames = shim.raw();
    let raw = String::from_utf8(frames.last().unwrap().clone()).unwrap();
    assert!(raw.contains(
        r#""inputSchema":{"type":"object","properties":{"z":{},"a":{}},"required":["z"],"x-extension":{"z":1,"a":2}}"#
    ));
    assert!(raw.contains(r#""outputSchema":{"type":"object","properties":{"b":{},"a":{}}}"#));
}

#[test]
fn arbitrary_integer_rpc_ids_preserve_response_ack_cancellation_and_eof_identity() {
    for modern in [false, true] {
        let fixture = Fixture::start();
        let mut shim = Shim::start(&fixture.url);
        let ids:Vec<Value>=serde_json::from_str("[9223372036854775808,-9223372036854775809,184467440737095516160,-340282366920938463463374607431768211456]").unwrap();
        shim.send(if modern {json!({"jsonrpc":"2.0","id":ids[0],"method":"server/discover","params":{"_meta":meta()}})}else{json!({"jsonrpc":"2.0","id":ids[0],"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"large-id","version":"1"}}})});
        assert_eq!(shim.receive()["id"], ids[0]);
        if !modern {
            shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
        }
        for id in &ids[1..] {
            shim.send(json!({"jsonrpc":"2.0","id":id,"method":"tools/list","params":if modern {json!({"_meta":meta()})}else{json!({})}}));
            assert_eq!(shim.receive()["id"], *id);
        }
        for id in [
            ids[2].clone(),
            json!(ids[2].to_string()),
            json!(0),
            json!("pseudolife-rpc-00000000-0000-0000-0000-000000000000-0"),
            ids[2].clone(),
        ] {
            shim.send(json!({"jsonrpc":"2.0","id":id,"method":"tools/list","params":if modern {json!({"_meta":meta()})}else{json!({})}}));
            assert_eq!(shim.receive()["id"], id);
        }
        if modern {
            shim.send(json!({"jsonrpc":"2.0","id":ids[2],"method":"subscriptions/listen","params":{"_meta":meta(),"notifications":{"toolsListChanged":true}}}));
            assert_eq!(
                shim.receive()["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"],
                ids[2]
            );
            shim.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":ids[2]}}));
            shim.send(json!({"jsonrpc":"2.0","id":"barrier","method":"tools/list","params":{"_meta":meta()}}));
            assert_eq!(shim.receive()["id"], "barrier");
            shim.send(json!({"jsonrpc":"2.0","id":ids[2],"method":"subscriptions/listen","params":{"_meta":meta(),"notifications":{"toolsListChanged":true}}}));
            assert_eq!(
                shim.receive()["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"],
                ids[2]
            );
            shim.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":ids[2]}}));
            shim.send(json!({"jsonrpc":"2.0","id":ids[3],"method":"subscriptions/listen","params":{"_meta":meta(),"notifications":{}}}));
            assert_eq!(
                shim.receive()["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"],
                ids[3]
            );
            let tail = shim.finish();
            assert_eq!(
                tail,
                vec![
                    json!({"jsonrpc":"2.0","id":ids[3],"error":{"code":-32000,"message":"Connection closed"}})
                ]
            );
        } else {
            assert!(shim.finish().is_empty());
        }
        fixture.assert_deleted();
    }
}

#[test]
fn shared_host_claim_is_refused_before_any_upstream_call() {
    let fixture = Fixture::start();
    let mut shim = Shim::start_with_env(
        &fixture.url,
        &[
            ("PSEUDOLIFE_MCP_SHARED_HOST", "1"),
            ("PSEUDOLIFE_AGENT_COORDINATION", "1"),
        ],
    );
    open(&mut shim, false);
    shim.send(json!({"jsonrpc":"2.0","id":"claim","method":"tools/call","params":{"name":"memory_agents","arguments":{"action":"claim","worktree":"does-not-exist","files":["private-file"]}}}));
    let response = shim.receive();
    assert_eq!(
        response["error"]["data"],
        json!({"classification":"coordination_unavailable","phase":"call","operation_outcome":"not_dispatched"})
    );
    assert!(
        response["error"]["message"]
            .as_str()
            .unwrap()
            .contains("shared")
    );
    assert!(shim.finish().is_empty());
    fixture.assert_deleted();
    assert!(
        !fixture
            .records
            .lock()
            .unwrap()
            .iter()
            .any(|record| record.message["method"] == "tools/call")
    );
}

#[test]
fn strict_non_integer_rpc_ids_are_ignored_before_a_valid_handshake() {
    for modern in [false, true] {
        for id in [
            json!(true),
            json!(false),
            json!(1.0),
            json!(1.5),
            Value::Null,
        ] {
            let fixture = Fixture::start();
            let mut shim = Shim::start(&fixture.url);
            shim.send(if modern {json!({"jsonrpc":"2.0","id":id,"method":"server/discover","params":{"_meta":meta()}})} else {json!({"jsonrpc":"2.0","id":id,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"types","version":"1"}}})});
            open(&mut shim, modern);
            assert!(shim.finish().is_empty());
            assert_eq!(shim.raw().len(), 1, "invalid identifier emitted a frame");
            fixture.assert_deleted();
        }
    }
}
