mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{Board, Options, Preparation, policy};
use serde_json::json;
use std::time::Duration;

fn fast(runtime: &pseudolife_stdio::lifecycle::Runtime, home: &Home, setting: &str) -> Options {
    let mut options = options(runtime, home, setting);
    // Accelerate only the backoff. A gated 350 ms registration on 2026-10-11
    // made the old 300 ms retry budget register twice; client and private-file
    // setup must retain their production budgets even on a loaded runner.
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
    // Hosted Windows on 2026-10-09 reached recovery but exceeded the old 2 s
    // observer (runs 37987593201/1 and 37992417769). Allow one production retry
    // attempt and its validation; the identity and one-shot hint stay required.
    let timing = pseudolife_stdio::board::Timing::default();
    tokio::time::timeout(timing.retry_attempt + timing.startup, async {
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

async fn attach(
    runtime: std::sync::Arc<pseudolife_stdio::lifecycle::Runtime>,
    options: Options,
    gate: Option<&mut ResponseGate>,
) -> std::sync::Arc<Board> {
    let Some(gate) = gate else {
        return Board::attach_options(runtime, options).await;
    };
    // Expire only the deliberately held startup request. Keep virtual time
    // still during real client setup; the blocker bounds a missing gate too.
    let (hold, release) = std::sync::mpsc::channel::<()>();
    let clock = tokio::task::spawn_blocking(move || {
        let _ = release.recv_timeout(Duration::from_secs(15));
    });
    tokio::time::pause();
    let startup = options.timing.startup;
    let pending = tokio::spawn(Board::attach_options(runtime, options));
    gate.wait().await;
    tokio::time::advance(startup + Duration::from_millis(1)).await;
    // Keep time frozen while the expired startup timer runs. A later request
    // timeout must not substitute for the startup deadline under test.
    let finish_by = std::time::Instant::now() + Duration::from_secs(1);
    while !pending.is_finished() && std::time::Instant::now() < finish_by {
        tokio::task::yield_now().await;
    }
    let completed = pending.is_finished();
    if !completed {
        pending.abort();
    }
    tokio::time::resume();
    drop(hold);
    clock.await.unwrap();
    gate.release();
    assert!(
        completed,
        "attach did not finish at the frozen startup deadline"
    );
    pending.await.unwrap()
}

#[tokio::test]
async fn board_retry_slow_successful_attempt_registers_once() {
    let fixture = Fixture::new(0);
    fixture.answer("context", outage("overloaded"));
    // Healthy setup may exceed the gate's former 2 s observer while staying
    // inside the production request budget. It is not the held registration.
    fixture.answer(
        "context",
        Answer::json(200, json!({"bank_id":BANK,"principal":"fixture"}))
            .delayed(Duration::from_millis(2100)),
    );
    let (answer, mut gate) = Answer::json(
        200,
        json!({"agent_id":"fixture-agent","credential":"fixture-agent-key"}),
    )
    .gated();
    fixture.answer("register", answer);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    // Freeze before retry setup as well as while registration is held. Real
    // socket and private-file setup consume none of the virtual phase budget.
    let (hold, release) = std::sync::mpsc::channel::<()>();
    let clock = tokio::task::spawn_blocking(move || {
        let _ = release.recv_timeout(Duration::from_secs(15));
    });
    tokio::time::pause();
    let options = fast(&runtime, &home, "1");
    let backoff = options.timing.retry_delays[0];
    let board = Board::attach_options(runtime.clone(), options).await;
    tokio::task::yield_now().await;
    // Cross the timer wheel's millisecond tick as well as the backoff deadline.
    tokio::time::advance(backoff + Duration::from_millis(1)).await;
    gate.wait().await;
    // Pin the fixture budget: a successful 350 ms registration must still fit
    // the production retry budget when only the backoff is accelerated.
    tokio::time::advance(Duration::from_millis(350)).await;
    tokio::task::yield_now().await;
    gate.release();
    tokio::time::resume();
    drop(hold);
    clock.await.unwrap();
    let call = registered(&board).await;
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    assert_eq!(fixture.count("/register"), 1);
    board.close().await;
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
        let mut gate = if mode == "hung" {
            let (answer, gate) = outage(mode).gated();
            fixture.answer("context", answer);
            Some(gate)
        } else {
            fixture.answer("context", outage(mode));
            None
        };
        let home = Home::new();
        let runtime = runtime(&fixture, false);
        let board = attach(runtime.clone(), fast(&runtime, &home, "1"), gate.as_mut()).await;
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
async fn board_retry_fixture_serves_context_while_an_accepted_connection_is_empty() {
    use std::{io::Read, net::TcpStream};

    let mut fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), fast(&runtime, &home, "1")).await;
    registered(&board).await;
    let before = fixture.count("/context");
    let (call, mut empty) = tokio::time::timeout(Duration::from_secs(2), async {
        let empty = TcpStream::connect(fixture.url.trim_start_matches("http://")).unwrap();
        fixture.wait_connection(&empty).await;
        (forward(&board, true).await, empty)
    })
    .await
    .expect("accepted empty connection blocked context validation");
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    assert!(fixture.count("/context") > before);
    empty.set_nonblocking(true).unwrap();
    assert!(
        matches!(empty.peek(&mut [0]), Err(error) if error.kind() == std::io::ErrorKind::WouldBlock),
        "fixture closed the held socket before context validation completed"
    );
    empty.set_nonblocking(false).unwrap();
    board.close().await;
    let accepted = fixture.accepted_connections();
    assert_eq!(
        fixture.close(),
        accepted,
        "fixture did not join every handler"
    );
    empty
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    assert_eq!(
        empty.read(&mut [0]).unwrap(),
        0,
        "fixture kept its socket open"
    );
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
    let mut timing = fast(&runtime, &home, "1");
    // The permanent startup refusal uses the production client setup budget.
    timing.timing.startup = pseudolife_stdio::board::Timing::default().startup;
    let board = Board::attach_options(runtime.clone(), timing).await;
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
        let mut gate = if mode == "hung" {
            let (answer, gate) = outage(mode).gated();
            fixture.answer("context", answer);
            Some(gate)
        } else {
            fixture.answer("context", outage(mode));
            None
        };
        for _ in 0..19 {
            fixture.answer("context", outage(mode));
        }
        let home = Home::new();
        let runtime = runtime(&fixture, false);
        let board = attach(runtime.clone(), fast(&runtime, &home, "1"), gate.as_mut()).await;
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
    let (answer, mut gate) = Answer::json(200, json!({"generation":1,"pending_count":0})).gated();
    fixture.answer("attach", answer);
    fixture.answer(
        "attach",
        Answer::json(409, json!({"error":"attachment_busy"})),
    );
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = attach(runtime.clone(), fast(&runtime, &home, "1"), Some(&mut gate)).await;
    let call = registered(&board).await;
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    assert_eq!(fixture.count("/register"), 1);
    assert_eq!(fixture.count("/attach"), 3);
    board.close().await;
    assert_eq!(fixture.count("/detach"), 1);
}
async fn late_probe(codex: bool, ready: bool) {
    let fixture = Fixture::new(0);
    if ready {
        fixture.answer(
            "register",
            Answer::json(
                200,
                json!({"agent_id":"fixture-agent","credential":"fixture-agent-key"}),
            )
            .delayed(Duration::from_millis(350)),
        );
    }
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
    // This case forces probe recovery, not a registration timeout. The 350 ms
    // reply proves the old 300 ms accelerated retry needlessly re-registered;
    // use the production setup budgets in both host modes.
    let production = pseudolife_stdio::board::Timing::default();
    timing.timing.startup = production.startup;
    timing.timing.retry_attempt = production.retry_attempt;
    let board = Board::attach_options(runtime.clone(), timing).await;
    assert!(!board.board_checkin().await);
    gate.wait().await;
    assert_eq!(fixture.count("coordination-start"), 2);
    gate.release();
    if ready {
        let call = registered(&board).await;
        assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
        assert_eq!(fixture.count("/register"), 1);
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
