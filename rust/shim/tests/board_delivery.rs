use futures::{SinkExt, StreamExt};
use pseudolife_stdio::board::delivery::{Delivery, endpoint};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio_tungstenite::tungstenite::Message;

const THREAD: &str = "11111111-1111-1111-1111-111111111111";

#[allow(clippy::result_large_err)]
async fn fixture(uncertain: bool) -> (String, Arc<Mutex<Vec<Value>>>, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let log = Arc::new(Mutex::new(Vec::new()));
    let recorded = log.clone();
    let task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_hdr_async(
            stream,
            |request: &tokio_tungstenite::tungstenite::handshake::server::Request, response| {
                assert_eq!(
                    request.headers()["authorization"],
                    "Bearer fixture-host-bridge"
                );
                Ok(response)
            },
        )
        .await
        .unwrap();
        while let Some(Ok(Message::Text(text))) = socket.next().await {
            let request: Value = serde_json::from_str(&text).unwrap();
            recorded.lock().unwrap().push(request.clone());
            let Some(method) = request["method"].as_str() else {
                continue;
            };
            if method == "initialized" {
                continue;
            }
            let result = match method {
                "initialize" => json!({}),
                "thread/loaded/list" => json!({"data":[THREAD]}),
                "turn/start" if uncertain => {
                    let _ = socket.close(None).await;
                    break;
                }
                "turn/start" => json!({"turn":{"id":"fixture-turn"}}),
                _ => panic!("unexpected host operation {method}"),
            };
            socket
                .send(Message::Text(
                    json!({"jsonrpc":"2.0","id":request["id"],"result":result})
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
        }
    });
    (url, log, task)
}

#[test]
fn board_bridge_endpoint_refuses_remote_or_credential_bearing_urls() {
    assert!(endpoint("ws://127.0.0.1:8766"));
    assert!(endpoint("ws://[::1]:8766/"));
    for url in [
        "ws://localhost:8766",
        "wss://127.0.0.1:8766",
        "ws://127.0.0.1:8766/path",
        "ws://user@127.0.0.1:8766",
        "ws://127.0.0.1:8766/?token=x",
        "ws://127.0.0.1:0",
    ] {
        assert!(!endpoint(url));
    }
}

#[tokio::test]
async fn board_bridge_delivers_attributed_tool_output_without_user_input_or_ack() {
    let (url, log, server) = fixture(false).await;
    let delivery = Delivery::connect(&url, "fixture-host-bridge", THREAD)
        .await
        .unwrap();
    let peer = "[Pseudolife board message from peer]\nfixture peer content";
    delivery.verify().await.unwrap();
    delivery.deliver(peer).await.unwrap();
    delivery.close().await;
    tokio::time::timeout(std::time::Duration::from_secs(2), server)
        .await
        .unwrap()
        .unwrap();
    let log = log.lock().unwrap();
    let methods: Vec<_> = log
        .iter()
        .map(|request| request["method"].as_str().unwrap())
        .collect();
    assert_eq!(
        methods,
        [
            "initialize",
            "initialized",
            "thread/loaded/list",
            "thread/loaded/list",
            "turn/start"
        ]
    );
    let turn = log.last().unwrap();
    assert_eq!(turn["params"]["input"], json!([]));
    assert_eq!(
        turn["params"]["toolOutput"],
        json!({"name":"pseudolife_message","namespace":"pseudolife","output":peer})
    );
    assert_eq!(turn["params"]["threadId"], THREAD);
}

#[tokio::test]
async fn board_bridge_closed_after_turn_dispatch_reports_uncertain_acceptance() {
    let (url, log, server) = fixture(true).await;
    let delivery = Delivery::connect(&url, "fixture-host-bridge", THREAD)
        .await
        .unwrap();
    assert!(
        delivery
            .deliver("fixture peer content")
            .await
            .unwrap_err()
            .uncertain
    );
    delivery.close().await;
    tokio::time::timeout(std::time::Duration::from_secs(2), server)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        log.lock()
            .unwrap()
            .iter()
            .filter(|request| request["method"] == "turn/start")
            .count(),
        1
    );
}
