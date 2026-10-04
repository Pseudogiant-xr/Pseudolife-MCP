mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{Board, Options, Preparation, policy};
use serde_json::json;
use std::time::Duration;

fn fast(runtime: &pseudolife_stdio::lifecycle::Runtime, home: &Home, setting: &str) -> Options {
    let mut options = options(runtime, home, setting);
    options.timing.startup = Duration::from_millis(60);
    // Keep the production probe deadline: client setup is inside that budget.
    options.timing.retry_attempt = Duration::from_millis(300);
    options.timing.retry_delays = [Duration::from_millis(40); 6];
    options
}
fn capture(name: &str, expected: &[&str], absent: &[&str]) -> bool {
    if std::env::var("PSEUDOLIFE_BOARD_TEST_CHILD").ok().as_deref() == Some(name) {
        return false;
    }
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", name, "--nocapture"])
        .env("PSEUDOLIFE_BOARD_TEST_CHILD", name)
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        stderr
    );
    for text in expected {
        assert!(stderr.contains(text), "missing {text}: {stderr}");
    }
    for text in absent {
        assert!(!stderr.contains(text), "unexpected {text}: {stderr}");
    }
    true
}
async fn forward(board: &Board, required: bool) -> pseudolife_stdio::board::PreparedCall {
    let Preparation::Forward(call) = board
        .prepare_call(
            if required {
                "memory_agents"
            } else {
                "memory_search"
            },
            Some(json!({"action":"update"}).as_object().unwrap().clone()),
            &json!({"threadId":BANK}),
        )
        .await
        .unwrap()
    else {
        panic!("forward");
    };
    call
}
async fn registered(board: &Board) -> pseudolife_stdio::board::PreparedCall {
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Ok(Preparation::Forward(call)) = board
                .prepare_call(
                    "memory_agents",
                    Some(json!({"action":"update"}).as_object().unwrap().clone()),
                    &json!({"threadId":BANK}),
                )
                .await
                && call.operation.headers.contains_key("x-pl-agent")
            {
                return call;
            }
            tokio::time::sleep(Duration::from_millis(3)).await;
        }
    })
    .await
    .unwrap()
}
async fn unregistered(board: &Board) -> pseudolife_stdio::board::PreparedCall {
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Ok(Preparation::Forward(call)) = board
                .prepare_call(
                    "memory_agents",
                    Some(json!({"action":"update"}).as_object().unwrap().clone()),
                    &json!({"threadId":BANK}),
                )
                .await
            {
                assert!(!call.operation.headers.contains_key("x-pl-agent"));
                return call;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("refused probe did not leave pending coordination")
}
fn outage(mode: &str) -> Answer {
    match mode {
        "refused" => Answer::text(0, b""),
        "overloaded" => Answer::json(503, json!({"error":"context_unavailable"})),
        _ => Answer::json(200, json!({"bank_id":BANK,"principal":"fixture"}))
            .delayed(Duration::from_millis(90)),
    }
}

#[tokio::test]
async fn board_retry_transient_startup_matrix() {
    if capture(
        "board_retry_transient_startup_matrix",
        &[
            "registration is retried in the background",
            "coordination registered after a startup delay",
        ],
        &[],
    ) {
        return;
    }
    for mode in ["refused", "overloaded", "hung"] {
        let fixture = Fixture::new(0);
        fixture.answer("context", outage(mode));
        let home = Home::new();
        let runtime = runtime(&fixture, false);
        let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
        let error = board
            .prepare_call(
                "memory_agents",
                Some(json!({"action":"update"}).as_object().unwrap().clone()),
                &json!({}),
            )
            .await
            .err()
            .unwrap();
        assert_eq!(error.message, policy::PENDING);
        assert_eq!(error.data.unwrap()["operation_outcome"], "not_dispatched");
        assert_eq!(fixture.count("/register"), 0);
        let call = registered(&board).await;
        assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
        assert_eq!(
            call.operation.headers["x-pl-agent-key"],
            "fixture-agent-key"
        );
        board.close().await;
        assert!(
            fixture
                .requests
                .lock()
                .unwrap()
                .last()
                .unwrap()
                .path
                .ends_with("/detach")
        );
    }
}
#[tokio::test]
async fn board_retry_registered_note_once_after_validated_call() {
    if capture(
        "board_retry_registered_note_once_after_validated_call",
        &["coordination registered after"],
        &[],
    ) {
        return;
    }
    let fixture = Fixture::new(0);
    fixture.answer("context", outage("overloaded"));
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
    fixture.wait("/attach", 1).await;
    *fixture.bank.lock().unwrap() = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb".into();
    let call = forward(&board, false).await;
    assert!(!call.operation.headers.contains_key("x-pl-agent"));
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    assert!(
        !board
            .finish_call(&call, &mut result)
            .await
            .unwrap_or_default()
            .contains(policy::REGISTERED)
    );
    *fixture.bank.lock().unwrap() = BANK.into();
    let call = registered(&board).await;
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    assert!(
        board
            .finish_call(&call, &mut result)
            .await
            .unwrap()
            .starts_with(policy::REGISTERED)
    );
    for _ in 0..2 {
        let mut result = rmcp::model::CallToolResult::success(vec![]);
        assert!(board.finish_call(&call, &mut result).await.is_none());
    }
    board.close().await;
}
#[tokio::test]
async fn board_retry_permanent_startup_forwarding_is_not_retried() {
    if capture(
        "board_retry_permanent_startup_forwarding_is_not_retried",
        &["coordination unavailable; memory proxy remains active"],
        &["retried"],
    ) {
        return;
    }
    let fixture = Fixture::new(0);
    fixture.answer("context", Answer::json(401, json!({})));
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
    tokio::time::sleep(Duration::from_millis(140)).await;
    let call = forward(&board, true).await;
    assert!(!call.operation.headers.contains_key("x-pl-agent"));
    assert_eq!(fixture.count("/context"), 1);
    board.close().await;
}
#[tokio::test]
async fn board_retry_permanent_later_refusal_stops_and_hints_once() {
    if capture(
        "board_retry_permanent_later_refusal_stops_and_hints_once",
        &["coordination unavailable; memory proxy remains active"],
        &[],
    ) {
        return;
    }
    let fixture = Fixture::new(0);
    fixture.answer("context", outage("refused"));
    fixture.answer("context", Answer::json(401, json!({})));
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
    fixture.wait("/context", 2).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(fixture.count("/context"), 2);
    let call = forward(&board, true).await;
    assert!(!call.operation.headers.contains_key("x-pl-agent"));
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    assert!(
        board
            .finish_call(&call, &mut result)
            .await
            .unwrap()
            .contains("stopped retrying")
    );
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    assert!(board.finish_call(&call, &mut result).await.is_none());
    board.close().await;
}
#[tokio::test]
async fn board_retry_close_cancels_hung_and_continuously_refused_attempts() {
    for mode in ["refused", "hung"] {
        let fixture = Fixture::new(0);
        for _ in 0..20 {
            fixture.answer("context", outage(mode));
        }
        let home = Home::new();
        let runtime = runtime(&fixture, false);
        let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
        fixture.wait("/context", 2).await;
        tokio::time::timeout(Duration::from_millis(100), board.close())
            .await
            .unwrap();
        let count = fixture.count("/context");
        tokio::time::sleep(Duration::from_millis(120)).await;
        assert_eq!(fixture.count("/context"), count);
        assert_eq!(fixture.count("/register"), 0);
    }
}
#[tokio::test]
async fn board_retry_cancelled_attach_waits_out_attachment_busy() {
    let fixture = Fixture::new(0);
    fixture.answer(
        "attach",
        Answer::json(200, json!({"generation":1,"pending_count":0}))
            .delayed(Duration::from_millis(350)),
    );
    fixture.answer(
        "attach",
        Answer::json(409, json!({"error":"attachment_busy"})),
    );
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
    let call = registered(&board).await;
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    assert_eq!(fixture.count("/register"), 1);
    assert_eq!(fixture.count("/attach"), 3);
    board.close().await;
    assert_eq!(fixture.count("/detach"), 1);
}
async fn late_probe(codex: bool, ready: bool) {
    let fixture = Fixture::new(0);
    fixture.answer("coordination-start", outage("refused"));
    let (answer, mut gate) = Answer::text(
        if ready { 200 } else { 401 },
        if ready { b"ready" } else { b"" },
    )
    .gated();
    fixture.answer("coordination-start", answer);
    let home = Home::new();
    let runtime = runtime(&fixture, codex);
    let mut timing = fast(&runtime, &home, "");
    if codex && ready {
        // This probe-recovery case uses the production per-thread setup budget.
        timing.timing.startup = pseudolife_stdio::board::Timing::default().startup;
    }
    let board = Board::attach_options(runtime.clone(), timing).await;
    assert!(!board.board_checkin().await);
    gate.wait().await;
    assert_eq!(fixture.count("coordination-start"), 2);
    gate.release();
    if ready {
        let call = registered(&board).await;
        assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
        let mut result = rmcp::model::CallToolResult::success(vec![]);
        assert!(
            board
                .finish_call(&call, &mut result)
                .await
                .unwrap()
                .starts_with(policy::REGISTERED)
        );
    } else {
        let call = unregistered(&board).await;
        assert_eq!(fixture.count("/register"), 0);
        assert!(!call.operation.headers.contains_key("x-pl-agent"));
    }
    board.close().await;
    assert_eq!(fixture.count("coordination-start"), 2);
}
#[tokio::test]
async fn board_retry_default_probe_later_serves_and_registers() {
    if capture(
        "board_retry_default_probe_later_serves_and_registers",
        &[
            "check and registration are retried",
            "coordination registered after",
        ],
        &[],
    ) {
        return;
    }
    late_probe(false, true).await;
}
#[tokio::test]
async fn board_retry_default_probe_later_refusal_stops_quietly() {
    if capture(
        "board_retry_default_probe_later_refusal_stops_quietly",
        &["check and registration are retried"],
        &["coordination unavailable"],
    ) {
        return;
    }
    late_probe(false, false).await;
}
#[tokio::test]
async fn board_retry_codex_probe_later_serves_registry() {
    if capture(
        "board_retry_codex_probe_later_serves_registry",
        &[
            "check is retried in the background",
            "Codex threads register on their next call",
        ],
        &[],
    ) {
        return;
    }
    late_probe(true, true).await;
}
#[tokio::test]
async fn board_retry_codex_probe_later_refusal_stops_quietly() {
    if capture(
        "board_retry_codex_probe_later_refusal_stops_quietly",
        &["check is retried in the background"],
        &["coordination unavailable"],
    ) {
        return;
    }
    late_probe(true, false).await;
}
#[tokio::test]
async fn board_retry_close_while_probe_answers_registers_nothing() {
    for codex in [false, true] {
        let fixture = Fixture::new(0);
        fixture.answer("coordination-start", outage("refused"));
        let (answer, mut gate) = Answer::text(200, b"ready").gated();
        fixture.answer("coordination-start", answer);
        let home = Home::new();
        let runtime = runtime(&fixture, codex);
        let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "")).await;
        gate.wait().await;
        board.close().await;
        gate.release();
        assert_eq!(fixture.count("/register"), 0);
        assert_eq!(fixture.count("coordination-start"), 2);
    }
}
#[tokio::test]
async fn board_retry_codex_close_cancels_unanswered_probe() {
    let fixture = Fixture::new(0);
    for _ in 0..20 {
        fixture.answer("coordination-start", outage("refused"));
    }
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "")).await;
    fixture.wait("coordination-start", 2).await;
    board.close().await;
    let count = fixture.count("coordination-start");
    tokio::time::sleep(Duration::from_millis(80)).await;
    assert_eq!(fixture.count("coordination-start"), count);
    assert_eq!(fixture.count("/register"), 0);
}
#[tokio::test]
async fn board_retry_codex_late_refusal_explains_explicit_doorbell() {
    if capture(
        "board_retry_codex_late_refusal_explains_explicit_doorbell",
        &[
            "PSEUDOLIFE_CODEX_DOORBELL needs agent coordination, which the daemon does not serve this bearer; doorbell off.",
        ],
        &[],
    ) {
        return;
    }
    let fixture = Fixture::new(0);
    fixture.answer("coordination-start", outage("refused"));
    fixture.answer("coordination-start", Answer::text(401, b""));
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let mut options = fast(&runtime, &home, "");
    options.doorbell_setting = Some("1".into());
    let board = Board::attach_options(runtime, options).await;
    fixture.wait("coordination-start", 2).await;
    tokio::time::sleep(Duration::from_millis(10)).await;
    board.close().await;
    assert_eq!(fixture.count("/register"), 0);
}

#[tokio::test]
async fn board_retry_close_wins_late_transport_failure_without_another_attempt() {
    let fixture = Fixture::new(0);
    fixture.answer("context", outage("refused"));
    fixture.answer(
        "context",
        Answer::json(503, serde_json::json!({})).delayed(Duration::from_millis(120)),
    );
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
    fixture.wait("context", 2).await;
    tokio::time::timeout(Duration::from_millis(100), board.close())
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(fixture.count("context"), 2);
    assert_eq!(fixture.count("/register"), 0);
}
