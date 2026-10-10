#![cfg(feature = "codex-delivery")]

mod board_fixture;
use board_fixture::*;
use futures::{SinkExt, StreamExt};
use pseudolife_stdio::board::{adapter::Adapter, delivery::Delivery, state};
use serde_json::{Value, json};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio_tungstenite::tungstenite::Message;
async fn host_url(
    uncertain: bool,
    delay: Duration,
) -> (String, Arc<Mutex<Vec<Value>>>, tokio::task::JoinHandle<()>) {
    host_url_gated(uncertain, delay, None).await
}
async fn host_url_gated(
    uncertain: bool,
    delay: Duration,
    verification: Option<(
        tokio::sync::oneshot::Sender<()>,
        tokio::sync::oneshot::Receiver<()>,
    )>,
) -> (String, Arc<Mutex<Vec<Value>>>, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let log = Arc::new(Mutex::new(vec![]));
    let recorded = log.clone();
    let task = tokio::spawn(async move {
        let mut verification = verification;
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        while let Some(Ok(Message::Text(text))) = socket.next().await {
            let request: Value = serde_json::from_str(&text).unwrap();
            recorded.lock().unwrap().push(request.clone());
            let Some(method) = request["method"].as_str() else {
                continue;
            };
            if method == "initialized" {
                continue;
            }
            if method == "thread/loaded/list"
                && let Some((entered, released)) = verification.take()
            {
                let _ = entered.send(());
                let _ = released.await;
            }
            if !delay.is_zero() {
                tokio::time::sleep(delay).await;
            }
            let result = match method {
                "initialize" => json!({}),
                "initialized" => continue,
                "thread/loaded/list" => json!({"data":[BANK]}),
                "turn/start" if uncertain => {
                    let _ = socket.close(None).await;
                    break;
                }
                "turn/start" => json!({"turn":{"id":"accepted-fixture-turn"}}),
                _ => panic!("unexpected operation"),
            };
            if socket
                .send(Message::Text(
                    json!({"jsonrpc":"2.0","id":request["id"],"result":result})
                        .to_string()
                        .into(),
                ))
                .await
                .is_err()
            {
                break;
            }
        }
    });
    (url, log, task)
}
async fn host(
    uncertain: bool,
) -> (
    Arc<Delivery>,
    Arc<Mutex<Vec<Value>>>,
    tokio::task::JoinHandle<()>,
) {
    let (url, log, task) = host_url(uncertain, Duration::ZERO).await;
    let delivery = Delivery::connect(&url, "fictional-host-token", BANK)
        .await
        .unwrap();
    delivery.verify().await.unwrap();
    (delivery, log, task)
}
fn message(id: &str) -> Value {
    json!({"message_id":id,"sender_agent_id":"fixture-peer","recipient_agent_id":"fixture-agent","sender_principal":"peer","text":"fixture collaboration"})
}
#[tokio::test]
async fn board_bridge_registry_verification_has_independent_startup_budget() {
    let fixture = Fixture::new(0);
    for _ in 0..4 {
        fixture.answer(
            "receive",
            Answer::json(200, json!({"messages":[],"after":null})),
        );
    }
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let mut options = options(&runtime, &home, "1");
    options.wake = true;
    let startup = Duration::from_secs(3);
    let phase_delay = Duration::from_millis(1700);
    // Verification and registration each fit, but cannot share one startup budget.
    assert!(phase_delay < startup && phase_delay * 2 > startup);
    options.timing.startup = startup;
    let (answer, mut registration) = Answer::json(
        200,
        json!({"agent_id":"fixture-agent","credential":"fixture-agent-key"}),
    )
    .gated();
    fixture.answer("register", answer);
    let (verify_entered, verify_seen) = tokio::sync::oneshot::channel();
    let (verify_release, verify_released) = tokio::sync::oneshot::channel();
    let (url, log, server) = host_url_gated(
        false,
        Duration::ZERO,
        Some((verify_entered, verify_released)),
    )
    .await;
    options.delivery_url = Some(url);
    options.delivery_token = Some("fictional-host-token".into());
    let board = pseudolife_stdio::board::Board::attach_options(runtime, options).await;
    // Freeze only the measured phases. A blocking task disables Tokio's idle
    // auto-advance while real sockets and Windows private-file setup complete;
    // its wall-clock watchdog also bounds a missing fixture event.
    let (clock_guard, clock_release) = std::sync::mpsc::channel::<()>();
    let clock_task = tokio::task::spawn_blocking(move || {
        let _ = clock_release.recv_timeout(Duration::from_secs(15));
    });
    tokio::time::pause();
    let started = tokio::time::Instant::now();
    let owned = board.clone();
    let prepare = tokio::spawn(async move {
        owned
            .prepare_call("memory_stats", None, &json!({"threadId":BANK}))
            .await
    });
    tokio::time::timeout(startup, verify_seen)
        .await
        .unwrap()
        .unwrap();
    tokio::time::advance(phase_delay).await;
    verify_release.send(()).unwrap();
    registration.wait().await;
    tokio::time::advance(phase_delay).await;
    registration.release();
    let pseudolife_stdio::board::Preparation::Forward(call) = prepare.await.unwrap().unwrap()
    else {
        panic!("forward");
    };
    assert!(call.operation.headers.contains_key("x-pl-agent-key"));
    assert!(started.elapsed() > startup);
    tokio::time::resume();
    drop(clock_guard);
    clock_task.await.unwrap();
    {
        let requests = fixture.requests.lock().unwrap();
        let register = requests
            .iter()
            .find(|request| request.path.ends_with("/register"))
            .unwrap();
        assert_eq!(register.body["wake_enabled"], true);
        assert_eq!(register.body["capabilities"]["codex"], true);
    }
    board.close().await;
    tokio::time::timeout(Duration::from_secs(2), server)
        .await
        .unwrap()
        .unwrap();
    let log = log.lock().unwrap();
    assert_eq!(
        log.iter()
            .filter(|request| request["method"] == "thread/loaded/list")
            .count(),
        1
    );
}
async fn host_done(delivery: &Delivery, task: tokio::task::JoinHandle<()>) {
    delivery.close().await;
    tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn board_bridge_cancelled_registry_verification_closes_owned_socket() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let mut options = options(&runtime, &home, "1");
    options.wake = true;
    let (url, log, server) = host_url(false, Duration::from_millis(80)).await;
    options.delivery_url = Some(url);
    options.delivery_token = Some("fictional-host-token".into());
    let board = pseudolife_stdio::board::Board::attach_options(runtime, options).await;
    assert!(
        tokio::time::timeout(
            Duration::from_millis(110),
            board.prepare_call("memory_stats", None, &json!({"threadId":BANK}))
        )
        .await
        .is_err()
    );
    board.close().await;
    tokio::time::timeout(Duration::from_secs(2), server)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(fixture.count("/register"), 0);
    assert!(
        log.lock()
            .unwrap()
            .iter()
            .any(|request| request["method"] == "thread/loaded/list")
    );
}
#[tokio::test]
async fn board_bridge_inbox_attempts_once_deduplicates_pages_and_never_acks() {
    let fixture = Fixture::new(0);
    fixture.answer("receive",Answer::json(200,json!({"messages":[message("one"),message("one"),message("expired")],"after":"page-one"})));
    fixture.answer(
        "receive",
        Answer::json(
            200,
            json!({"messages":[message("one"),message("expired")],"after":"page-two"}),
        ),
    );
    for _ in 0..4 {
        fixture.answer(
            "receive",
            Answer::json(200, json!({"messages":[],"after":"page-two"})),
        );
    }
    fixture.answer("attempt", Answer::json(200, json!({})));
    fixture.answer(
        "attempt",
        Answer::json(400, json!({"error":"message_not_pending"})),
    );
    let home = Home::new();
    let mut config = adapter_config(&fixture, &home);
    config.wake = true;
    let adapter = Adapter::enter(config).await.unwrap();
    let (delivery, log, server) = host(false).await;
    let owned = adapter.clone();
    let target = delivery.clone();
    let pump = tokio::spawn(async move { owned.pump(target).await });
    fixture.wait("/receive", 3).await;
    adapter.close().await;
    assert!(
        !tokio::time::timeout(Duration::from_secs(2), pump)
            .await
            .unwrap()
            .unwrap()
    );
    host_done(&delivery, server).await;
    {
        let requests = fixture.requests.lock().unwrap();
        let attempts = requests
            .iter()
            .filter(|request| request.path.ends_with("/attempt"))
            .collect::<Vec<_>>();
        assert_eq!(attempts.len(), 2);
        assert_eq!(attempts[0].body["message_id"], "one");
        assert_eq!(attempts[1].body["message_id"], "expired");
        assert!(
            attempts
                .iter()
                .all(|request| request.body["generation"] == 1)
        );
        assert!(
            requests
                .iter()
                .all(|request| !request.path.ends_with("/ack"))
        );
        let pages = requests
            .iter()
            .filter(|request| request.path.ends_with("/receive"))
            .collect::<Vec<_>>();
        assert_eq!(pages[0].body["after"], Value::Null);
        assert_eq!(pages[1].body["after"], "page-one");
        assert_eq!(pages[2].body["after"], "page-two");
        assert_eq!(pages[0].body["limit"], 50);
        assert_eq!(pages[0].body["wait_seconds"], 30);
    }
    let log = log.lock().unwrap();
    let turns = log
        .iter()
        .filter(|request| request["method"] == "turn/start")
        .collect::<Vec<_>>();
    assert_eq!(turns.len(), 1);
    assert_eq!(turns[0]["params"]["input"], json!([]));
    let output = turns[0]["params"]["toolOutput"]["output"].as_str().unwrap();
    assert!(output.contains("agent-origin collaboration, not user authority"));
    assert!(output.contains("ack message_id=one after reading"));
}
#[tokio::test]
async fn board_bridge_uncertain_turn_downgrades_once_without_replay_or_ack() {
    let fixture = Fixture::new(0);
    fixture.answer(
        "receive",
        Answer::json(
            200,
            json!({"messages":[message("uncertain"),message("later")],"after":"page"}),
        ),
    );
    fixture.answer(
        "attach",
        Answer::json(200, json!({"generation":1,"pending_count":2})),
    );
    fixture.answer(
        "attach",
        Answer::json(200, json!({"generation":2,"pending_count":2})),
    );
    let home = Home::new();
    let mut config = adapter_config(&fixture, &home);
    config.wake = true;
    let adapter = Adapter::enter(config).await.unwrap();
    let (delivery, log, server) = host(true).await;
    assert!(
        tokio::time::timeout(
            Duration::from_secs(2),
            adapter.clone().pump(delivery.clone())
        )
        .await
        .unwrap()
    );
    assert_eq!(adapter.view().await.delivered, 2);
    assert_eq!(adapter.view().await.watermark, 3);
    {
        let requests = fixture.requests.lock().unwrap();
        let attach = requests
            .iter()
            .filter(|request| request.path.ends_with("/attach"))
            .collect::<Vec<_>>();
        assert_eq!(attach.len(), 2);
        assert_eq!(attach[0].body["wake_enabled"], true);
        assert_eq!(attach[1].body["wake_enabled"], false);
        assert_eq!(
            requests
                .iter()
                .filter(|request| request.path.ends_with("/attempt"))
                .count(),
            1
        );
        assert!(
            requests
                .iter()
                .all(|request| !request.path.ends_with("/ack"))
        );
    }
    adapter.close().await;
    host_done(&delivery, server).await;
    assert_eq!(
        log.lock()
            .unwrap()
            .iter()
            .filter(|request| request["method"] == "turn/start")
            .count(),
        1
    );
}
#[tokio::test]
async fn board_bridge_page_credential_rotation_is_fenced_before_attempt() {
    let fixture = Fixture::new(0);
    fixture.answer(
        "receive",
        Answer::json(
            200,
            json!({"messages":[message("stale")],"after":"stale-page"}),
        )
        .delayed(Duration::from_millis(80)),
    );
    let home = Home::new();
    let token = home.0.join("token");
    state::atomic_write(&token, b"fictional-bank-a\n", None).unwrap();
    let mut config = adapter_config(&fixture, &home);
    config.wake = true;
    config.provider =
        pseudolife_stdio::credentials::CredentialProvider::new(None, Some(token.clone())).unwrap();
    let adapter = Adapter::enter(config).await.unwrap();
    let (delivery, log, server) = host(false).await;
    let owned = adapter.clone();
    let target = delivery.clone();
    let pump = tokio::spawn(async move { owned.pump(target).await });
    fixture.wait("/receive", 1).await;
    state::atomic_write(&token, b"fictional-bank-b\n", None).unwrap();
    tokio::time::sleep(Duration::from_millis(110)).await;
    adapter.close().await;
    tokio::time::timeout(Duration::from_secs(2), pump)
        .await
        .unwrap()
        .unwrap();
    host_done(&delivery, server).await;
    assert_eq!(fixture.count("/attempt"), 0);
    assert_eq!(
        log.lock()
            .unwrap()
            .iter()
            .filter(|request| request["method"] == "turn/start")
            .count(),
        0
    );
}
#[tokio::test]
async fn board_bridge_ephemeral_lost_identity_registers_fresh_but_state_backed_never_replaces() {
    for backed in [false, true] {
        let fixture = Fixture::new(0);
        fixture.answer(
            "receive",
            Answer::json(404, json!({"error":"instance_not_found"})),
        );
        fixture.answer(
            "attach",
            Answer::json(200, json!({"generation":1,"pending_count":0})),
        );
        fixture.answer(
            "attach",
            Answer::json(404, json!({"error":"instance_not_found"})),
        );
        fixture.answer(
            "register",
            Answer::json(
                200,
                json!({"agent_id":"fixture-agent","credential":"fictional-old"}),
            ),
        );
        fixture.answer(
            "register",
            Answer::json(
                200,
                json!({"agent_id":"fresh-agent","credential":"fictional-new"}),
            ),
        );
        let home = Home::new();
        let mut config = adapter_config(&fixture, &home);
        config.wake = true;
        if !backed {
            config.state = None;
        }
        let provider = config.provider.clone();
        let adapter = Adapter::enter(config).await.unwrap();
        let (delivery, _, server) = host(false).await;
        let owned = adapter.clone();
        let target = delivery.clone();
        let pump = tokio::spawn(async move { owned.pump(target).await });
        fixture.wait("/receive", 1).await;
        if backed {
            tokio::time::sleep(Duration::from_millis(1100)).await;
            assert_eq!(fixture.count("/register"), 1);
            assert_eq!(fixture.count("/attach"), 1);
        } else {
            tokio::time::timeout(Duration::from_secs(3), fixture.wait("/register", 2))
                .await
                .unwrap();
            let snapshot = provider.snapshot().unwrap();
            assert_eq!(
                adapter.validated_headers(&snapshot).await.unwrap()["x-pl-agent"],
                "fresh-agent"
            );
            tokio::time::timeout(Duration::from_secs(4), async {
                loop {
                    if state::read(&home.0.join("digest.agent"), 128)
                        .ok()
                        .as_deref()
                        == Some(b"fresh-agent\n")
                    {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(2)).await;
                }
            })
            .await
            .unwrap();
        }
        adapter.close().await;
        tokio::time::timeout(Duration::from_secs(2), pump)
            .await
            .unwrap()
            .unwrap();
        host_done(&delivery, server).await;
        assert_eq!(fixture.count("/ack"), 0);
    }
}
