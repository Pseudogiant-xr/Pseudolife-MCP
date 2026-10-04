mod common;
use common::*;
use serde_json::json;
use std::sync::Arc;

fn concurrent_case(modern: bool, release_order: &str) {
    let first = Arc::new(ResponseGate::default());
    let second = if release_order == "together" {
        first.clone()
    } else {
        Arc::new(ResponseGate::default())
    };
    let answer = |name: &str| {
        Reply::Result(
            json!({"content":[{"type":"text","text":format!("answer-{name}")}],"isError":false}),
        )
    };
    let fixture = Fixture::start_with(Setup {
        calls: vec![
            Reply::Gated(first.clone(), Box::new(answer("A"))),
            Reply::Gated(second.clone(), Box::new(answer("B"))),
        ],
        ..Setup::default()
    });
    let mut shim = Shim::start(&fixture.url);
    shim.send(if modern {
        json!({"jsonrpc":"2.0","id":"open","method":"server/discover","params":{"_meta":meta()}})
    } else {
        json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"concurrent-contract","version":"1"}}})
    });
    let opened = shim.receive();
    assert_eq!(opened["id"], "open");
    assert!(opened.get("result").is_some(), "opening failed: {opened}");
    if !modern {
        shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    }
    for (index, name) in ["A", "B"].into_iter().enumerate() {
        let mut params = json!({"name":name,"arguments":{}});
        if modern {
            params["_meta"] = meta();
        }
        shim.send(json!({"jsonrpc":"2.0","id":name,"method":"tools/call","params":params}));
        assert!(
            fixture.wait_calls(index + 1),
            "both calls must arrive before either held response is released"
        );
    }
    assert!(
        shim.output.try_recv().is_err(),
        "a held response escaped before release"
    );
    let mut observed = Vec::new();
    if release_order == "together" {
        first.release();
        for _ in 0..2 {
            observed.push(shim.receive());
        }
        let ids = observed
            .iter()
            .map(|frame| frame["id"].as_str().unwrap())
            .collect::<Vec<_>>();
        assert!(
            ids == ["A", "B"] || ids == ["B", "A"],
            "unobserved order: {ids:?}"
        );
    } else {
        for name in release_order.chars() {
            if name == 'A' {
                first.release();
            } else {
                second.release();
            }
            let frame = shim.receive();
            assert_eq!(frame["id"], name.to_string());
            observed.push(frame);
        }
    }
    for frame in &observed {
        let name = frame["id"].as_str().unwrap();
        assert_eq!(frame["jsonrpc"], "2.0");
        assert!(frame.get("error").is_none());
        assert_eq!(frame["result"]["isError"], false);
        assert_eq!(
            frame["result"]["content"],
            json!([{"type":"text","text":format!("answer-{name}")}])
        );
    }
    // Both receive calls above occur while stdin remains open. EOF is only
    // sent after complete results; it cannot authorize the observed order.
    assert!(shim.finish().is_empty(), "extra response at EOF");
    assert_eq!(
        shim.raw().len(),
        3,
        "one opening and exactly two call frames"
    );
    assert_eq!(shim.stderr(), "");
    fixture.assert_deleted();
}

#[test]
fn legacy_concurrent_calls_finish_in_individual_release_order() {
    for order in ["AB", "BA"] {
        concurrent_case(false, order);
    }
}

#[test]
fn modern_concurrent_calls_finish_in_individual_release_order() {
    for order in ["AB", "BA"] {
        concurrent_case(true, order);
    }
}

#[test]
fn legacy_common_release_accepts_only_the_observed_call_pair_orders() {
    for _ in 0..5 {
        concurrent_case(false, "together");
    }
}

#[test]
fn modern_common_release_accepts_only_the_observed_call_pair_orders() {
    for _ in 0..5 {
        concurrent_case(true, "together");
    }
}
