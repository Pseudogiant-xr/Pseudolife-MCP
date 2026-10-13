mod common;
use common::*;
use serde_json::json;

fn eof_case(modern: bool, calls: usize, listener: bool) {
    let fixture = Fixture::start();
    let mut shim = Shim::start(&fixture.url);
    let open = if modern {
        json!({"jsonrpc":"2.0","id":"open","method":"server/discover","params":{"_meta":meta()}})
    } else {
        json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"eof-contract","version":"1"}}})
    };
    shim.send(open);
    let initialized = shim.receive();
    assert_eq!(initialized["id"], "open");
    assert!(
        initialized.get("result").is_some(),
        "initialize/discover must succeed: {initialized}"
    );
    if !modern {
        shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    }
    if listener {
        shim.send(json!({"jsonrpc":"2.0","id":"listener","method":"subscriptions/listen","params":{"_meta":meta(),"notifications":{"toolsListChanged":true}}}));
    }
    for number in 0..calls {
        let mut params = json!({"name":"hang", "arguments":{}});
        if modern {
            params["_meta"] = meta();
        }
        shim.send(json!({"jsonrpc":"2.0","id":format!("hang-{number}"),"method":"tools/call","params":params}));
        assert!(
            fixture.wait_calls(number + 1),
            "candidate did not dispatch call; responses: {:?}",
            shim.output.try_iter().collect::<Vec<_>>()
        );
    }
    let frames = shim.finish();
    let expected = |id: &str| json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":"Connection closed"}});
    if listener {
        assert_eq!(frames.len(), 2, "one ACK and one terminal listener error");
        assert_eq!(
            frames[0],
            json!({"jsonrpc":"2.0","method":"notifications/subscriptions/acknowledged","params":{"_meta":{"io.modelcontextprotocol/subscriptionId":"listener"},"notifications":{"toolsListChanged":true}}})
        );
        assert_eq!(frames[1], expected("listener"));
    } else {
        let one = vec![expected("hang-0")];
        let forward = vec![expected("hang-0"), expected("hang-1")];
        let reverse = vec![expected("hang-1"), expected("hang-0")];
        match calls {
            0 => assert!(frames.is_empty()),
            1 => assert_eq!(frames, one),
            2 => assert!(
                frames == forward || frames == reverse,
                "only observed order-set accepted: {frames:?}"
            ),
            _ => unreachable!(),
        }
    }
    fixture.assert_deleted();
}
#[test]
fn legacy_eof_with_zero_pending() {
    eof_case(false, 0, false);
}
#[test]
fn legacy_eof_with_one_pending() {
    eof_case(false, 1, false);
}
#[test]
fn legacy_eof_with_two_pending() {
    eof_case(false, 2, false);
}
#[test]
fn modern_eof_with_zero_pending() {
    eof_case(true, 0, false);
}
#[test]
fn modern_eof_with_one_pending() {
    eof_case(true, 1, false);
}
#[test]
fn modern_eof_with_two_pending() {
    eof_case(true, 2, false);
}
#[test]
fn modern_eof_acknowledges_pending_listener_once() {
    eof_case(true, 0, true);
}
