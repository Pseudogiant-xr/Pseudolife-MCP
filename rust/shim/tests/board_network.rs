mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{Board, Preparation, policy};
use serde_json::{Value, json};
use std::time::Duration;

#[tokio::test(flavor = "current_thread")]
async fn board_opted_in_shim_injects_private_instance_identity_and_detaches() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "1")).await;
    assert!(board.board_checkin().await);
    let Preparation::Forward(call) = board
        .prepare_call(
            "memory_agents",
            Some(json!({"action":"update"}).as_object().unwrap().clone()),
            &json!({}),
        )
        .await
        .unwrap()
    else {
        panic!("expected forwarding")
    };
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    assert_eq!(call.operation.headers["x-pl-bank"], BANK);
    assert!(call.operation.headers["x-pl-agent-key"].is_sensitive());
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    assert!(
        board
            .finish_call(&call, &mut result)
            .await
            .unwrap()
            .contains("1 addressed message pending")
    );
    board.close().await;
    assert_eq!(fixture.count("/detach"), 1);
    assert!(
        std::fs::read_dir(home.0.join("digests"))
            .unwrap()
            .next()
            .is_none()
    );
}

#[tokio::test(flavor = "current_thread")]
async fn board_context_mismatch_omits_identity_and_blocks_coordination_tool() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "yes")).await;
    *fixture.bank.lock().unwrap() = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb".into();
    let optional = board.operation_context().await.unwrap();
    assert!(!optional.headers.contains_key("x-pl-agent-key"));
    let error = match board
        .prepare_call(
            "memory_message",
            Some(json!({"action":"receive"}).as_object().unwrap().clone()),
            &json!({}),
        )
        .await
    {
        Err(error) => error,
        _ => panic!("required identity must fail"),
    };
    assert_eq!(
        error.data.as_ref().unwrap()["classification"],
        "coordination_unavailable"
    );
    let context_calls = fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter(|request| request.path.ends_with("/context"))
        .all(|request| !request.headers.contains("x-pl-agent-key"));
    assert!(context_calls);
    board.close().await;
    assert_eq!(fixture.count("/detach"), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn board_registration_retries_after_transient_startup_failure_and_announces_once() {
    let fixture = Fixture::new(1);
    let (answer, mut attached) =
        Answer::json(200, json!({"generation":1,"pending_count":1})).gated();
    fixture.answer("attach", answer);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "1")).await;
    let error = match board.prepare_call("memory_message", None, &json!({})).await {
        Err(error) => error,
        _ => panic!("retry must fail closed"),
    };
    assert_eq!(error.message, policy::PENDING);
    let timing = pseudolife_stdio::board::Timing::default();
    let observation = timing.retry_delays[0] + timing.retry_attempt + timing.startup;
    tokio::time::timeout(observation, async {
        loop {
            if fixture.count("/attach") > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let Preparation::Forward(call) = board
        .prepare_call("memory_recall", None, &json!({}))
        .await
        .unwrap()
    else {
        panic!("forward")
    };
    attached.wait().await;
    assert!(!call.operation.headers.contains_key("x-pl-agent"));
    attached.release();
    // Hosted Windows run 37988825676 (2026-10-09) reached /attach before the
    // adapter was published. Receipt is not completion: wait for validated
    // forwarding identity before asserting the one-shot recovery hint.
    let call = tokio::time::timeout(observation, async {
        loop {
            if let Ok(Preparation::Forward(call)) =
                board.prepare_call("memory_recall", None, &json!({})).await
                && call.operation.headers.contains_key("x-pl-agent")
            {
                break call;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("recovered attachment did not publish validated identity");
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    let hint = board.finish_call(&call, &mut result).await.unwrap();
    assert!(hint.starts_with(policy::REGISTERED));
    let mut result = rmcp::model::CallToolResult::success(vec![]);
    assert!(board.finish_call(&call, &mut result).await.is_none());
    board.close().await;
    assert_eq!(fixture.count("/register"), 2);
    assert_eq!(fixture.count("/detach"), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn board_codex_metadata_overrides_session_and_attaches_lazily_even_when_opted_out() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "off")).await;
    let Preparation::Forward(call) = board
        .prepare_call("memory_recall", None, &json!({"threadId":BANK}))
        .await
        .unwrap()
    else {
        panic!("forward")
    };
    assert_eq!(call.operation.headers["x-pl-session"], BANK);
    assert!(!call.operation.headers.contains_key("x-pl-agent"));
    assert_eq!(fixture.count("/register"), 0);
    board.close().await;
}

#[tokio::test(flavor = "current_thread")]
async fn board_shared_host_refusal_precedes_git_and_state_access() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let mut options = options(&runtime, &home, "1");
    options.shared = true;
    let board = Board::attach_options(runtime, options).await;
    let error = match board
        .prepare_call(
            "memory_agents",
            Some(
                json!({"action":"claim","worktree":"invalid","path":"../bad"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
            &Value::Null,
        )
        .await
    {
        Err(error) => error,
        _ => panic!("shared refusal"),
    };
    assert_eq!(error.message, policy::SHARED_REFUSAL);
    assert_eq!(fixture.requests.lock().unwrap().len(), 0);
    assert!(!home.0.join("agents").exists());
    board.close().await;
}

#[tokio::test]
async fn board_codex_pull_registry_attaches_metadata_thread_lazily_and_closes_identity() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "1")).await;
    assert_eq!(fixture.count("/register"), 0);
    let Preparation::Forward(call) = board
        .prepare_call(
            "memory_message",
            Some(json!({"action":"receive"}).as_object().unwrap().clone()),
            &json!({"threadId":BANK}),
        )
        .await
        .unwrap()
    else {
        panic!("forward")
    };
    assert_eq!(call.operation.headers["x-pl-session"], BANK);
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    {
        let requests = fixture.requests.lock().unwrap();
        let register = requests
            .iter()
            .find(|request| request.path.ends_with("/register"))
            .unwrap();
        assert_eq!(register.body["capabilities"]["codex"], false);
        assert_eq!(register.body["wake_enabled"], false);
    }
    board.close().await;
    assert_eq!(fixture.count("/detach"), 1);
}

#[tokio::test]
async fn board_default_reads_checkin_route_and_registers_only_after_ready_answer() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "")).await;
    assert!(board.board_checkin().await);
    {
        let requests = fixture.requests.lock().unwrap();
        assert!(requests[0].path.ends_with("/api/hook/coordination-start"));
        assert!(!requests[0].headers.contains("x-pl-agent-key"));
    }
    board.close().await;
}

#[tokio::test]
async fn board_explicit_opt_out_is_quiet_and_opt_in_does_not_probe() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, false);
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "off")).await;
    assert!(!board.board_checkin().await);
    assert!(fixture.requests.lock().unwrap().is_empty());
    board.close().await;
    let board = Board::attach_options(runtime.clone(), options(&runtime, &home, "1")).await;
    assert!(board.board_checkin().await);
    assert_eq!(fixture.count("coordination-start"), 0);
    assert_eq!(fixture.count("/register"), 1);
    board.close().await;
}

#[tokio::test]
async fn board_codex_fixed_state_is_refused_without_registration() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = runtime(&fixture, true);
    let mut configured = options(&runtime, &home, "1");
    configured.state = Some(home.0.join("fixed.json"));
    let board = Board::attach_options(runtime, configured).await;
    assert!(!board.board_checkin().await);
    assert!(fixture.requests.lock().unwrap().is_empty());
    assert!(!home.0.join("fixed.json").exists());
    board.close().await;
}
