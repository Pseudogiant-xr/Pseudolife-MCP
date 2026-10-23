//! Port of Python #637 (tests/test_codex_coordination_recovery.py): an
//! existing Codex thread's authority check recovers one transient failure
//! with the same adapter, and a check that stays unresolved never forwards
//! the call and says why.
mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{Board, Options, Preparation};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};

const OTHER: &str = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const TRANSPORT: &str = "Coordination: authority validation failed (transport_unavailable); saved state was preserved. Retry when the bank responds.";
const UNAVAILABLE: &str = "Coordination: authority validation failed (context_unavailable); saved state was preserved. Retry when the bank responds.";
const UNREADABLE: &str = "Coordination: authority validation failed (context_unreadable); saved state was preserved. Retry when the bank responds.";
const TIMEOUT: &str = "Coordination: authority validation timed out (timeout); saved state was preserved. Retry when the bank responds.";
const ACCESS: &str = "Coordination: authority validation was refused by authentication or access policy; saved state was preserved. Check the configured credential and access.";
const BANK_HINT: &str = "Coordination: bank or principal differs from the saved mailbox; state was preserved. Check the configured bank and credential.";
const CREDENTIAL: &str = "Coordination: credential source changed or is unavailable; saved identity was preserved. Retry after credential setup completes.";

fn codex_runtime(
    fixture: &Fixture,
    token: Option<std::path::PathBuf>,
) -> Arc<pseudolife_stdio::lifecycle::Runtime> {
    let base = runtime(fixture, true);
    let Some(token) = token else {
        return base;
    };
    Arc::new(pseudolife_stdio::lifecycle::Runtime {
        url: base.url.clone(),
        provider: pseudolife_stdio::credentials::CredentialProvider::new(None, Some(token))
            .unwrap(),
        session: base.session.clone(),
        health: base.health.clone(),
        instructions_note: String::new(),
        cache: None,
    })
}
fn codex_options(runtime: &pseudolife_stdio::lifecycle::Runtime, home: &Home) -> Options {
    let mut options = options(runtime, home, "1");
    // Wide enough for a loopback answer on a loaded Windows runner; the
    // deadline cases hold their answer well past it.
    options.timing.validation = Duration::from_secs(1);
    options
}
async fn ack(board: &Board) -> Result<Preparation, rmcp::ErrorData> {
    board
        .prepare_call(
            "memory_message",
            Some(
                json!({"action":"ack","message_id":"33333333333333333333333333333333"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
            &json!({"threadId":BANK}),
        )
        .await
}
async fn attached(fixture: &Fixture, board: &Board) {
    let Ok(Preparation::Forward(call)) = ack(board).await else {
        panic!("the thread did not attach");
    };
    assert!(call.operation.headers.contains_key("x-pl-agent"));
    assert_eq!(fixture.count("/register"), 1);
}
fn contexts_since(fixture: &Fixture, start: usize) -> Vec<Value> {
    fixture.requests.lock().unwrap()[start..]
        .iter()
        .filter(|request| request.path.ends_with("/context"))
        .map(|request| request.body.clone())
        .collect()
}
fn mark(fixture: &Fixture) -> usize {
    fixture.requests.lock().unwrap().len()
}

#[tokio::test]
async fn codex_existing_thread_makes_one_read_only_check_per_call() {
    // Python reads the thread's identity headers from the adapter after its
    // one check; a second network check per call was a Rust-only extra.
    let fixture = Fixture::new(0);
    let home = Home::new();
    let runtime = codex_runtime(&fixture, None);
    let board = Board::attach_options(runtime.clone(), codex_options(&runtime, &home)).await;
    attached(&fixture, &board).await;
    let start = mark(&fixture);
    let Ok(Preparation::Forward(call)) = ack(&board).await else {
        panic!("an existing thread's call was refused");
    };
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    assert_eq!(
        contexts_since(&fixture, start),
        vec![json!({"read_only":true})]
    );
    board.close().await;
}

#[tokio::test]
async fn codex_existing_thread_recovers_one_transient_check() {
    let ordinary = || Answer::json(200, json!({"bank_id":BANK,"principal":"fixture"}));
    for (name, blip) in [
        ("dropped", Answer::text(0, b"")),
        ("body cut", ordinary().truncated()),
        ("503", Answer::json(503, json!({"error":"private fixture"}))),
        ("deadline", ordinary().delayed(Duration::from_millis(3500))),
        // An answer that cannot be read as a bank identity names no bank:
        // an outage, not a verdict (2026-10-23, Python `fetch_context`).
        ("malformed", Answer::json(200, json!({"private":"fixture"}))),
        ("not json", Answer::text(200, b"private fixture")),
    ] {
        let fixture = Fixture::new(0);
        let home = Home::new();
        let runtime = codex_runtime(&fixture, None);
        let board = Board::attach_options(runtime.clone(), codex_options(&runtime, &home)).await;
        attached(&fixture, &board).await;
        let attaches = fixture.count("/attach");
        let start = mark(&fixture);
        fixture.answer("context", blip);
        let Ok(Preparation::Forward(call)) = ack(&board).await else {
            panic!("{name}: one transient check refused the call");
        };
        assert_eq!(
            call.operation.headers["x-pl-agent"], "fixture-agent",
            "{name}"
        );
        assert_eq!(
            contexts_since(&fixture, start),
            vec![json!({"read_only":true}); 2],
            "{name}"
        );
        assert_eq!(fixture.count("/register"), 1, "{name}");
        assert_eq!(fixture.count("/attach"), attaches, "{name}");
        board.close().await;
    }
}

#[tokio::test]
async fn codex_unresolved_check_never_forwards_and_names_its_cause() {
    let slow = || {
        Answer::json(200, json!({"bank_id":BANK,"principal":"fixture"}))
            .delayed(Duration::from_millis(3500))
    };
    let cases: Vec<(&str, Vec<Answer>, usize, &str)> = vec![
        (
            "dropped",
            vec![Answer::text(0, b""), Answer::text(0, b"")],
            2,
            TRANSPORT,
        ),
        (
            "503",
            vec![Answer::json(503, json!({})), Answer::json(503, json!({}))],
            2,
            UNAVAILABLE,
        ),
        ("deadline", vec![slow(), slow()], 2, TIMEOUT),
        // Not in Python's transient set: a 501 is answered at once.
        ("501", vec![Answer::json(501, json!({}))], 1, UNAVAILABLE),
        ("401", vec![Answer::json(401, json!({}))], 1, ACCESS),
        ("403", vec![Answer::json(403, json!({}))], 1, ACCESS),
        (
            "malformed",
            vec![
                Answer::json(200, json!({"private":"fixture"})),
                Answer::json(200, json!({"private":"fixture"})),
            ],
            2,
            UNREADABLE,
        ),
        (
            "not json",
            vec![
                Answer::text(200, b"private fixture"),
                Answer::text(200, b"private fixture"),
            ],
            2,
            UNREADABLE,
        ),
        // The near miss: a readable answer naming another bank is a verdict.
        (
            "other bank",
            vec![Answer::json(
                200,
                json!({"bank_id":OTHER,"principal":"fixture"}),
            )],
            1,
            BANK_HINT,
        ),
        ("mismatch", vec![], 1, BANK_HINT),
    ];
    for (name, answers, checks, hint) in cases {
        let fixture = Fixture::new(0);
        let home = Home::new();
        let runtime = codex_runtime(&fixture, None);
        let board = Board::attach_options(runtime.clone(), codex_options(&runtime, &home)).await;
        attached(&fixture, &board).await;
        let start = mark(&fixture);
        for answer in answers {
            fixture.answer("context", answer);
        }
        if name == "mismatch" {
            *fixture.bank.lock().unwrap() = OTHER.into();
        }
        let error = ack(&board).await.err().unwrap();
        let data = error.data.unwrap();
        assert_eq!(data["classification"], "coordination_unavailable", "{name}");
        assert_eq!(data["operation_outcome"], "not_dispatched", "{name}");
        assert_eq!(data["hint"], hint, "{name}");
        assert!(!data.to_string().contains("private fixture"), "{name}");
        assert_eq!(contexts_since(&fixture, start).len(), checks, "{name}");
        assert_eq!(fixture.count("/register"), 1, "{name}");
        *fixture.bank.lock().unwrap() = BANK.into();
        board.close().await;
    }
}

#[tokio::test]
async fn codex_check_spanning_a_credential_rotation_is_never_retried() {
    // A failed read may span a credential rotation: that authority is not
    // retried, even when its failure looked transient.
    for retried in [false, true] {
        let fixture = Fixture::new(0);
        let home = Home::new();
        let token = home.0.join("token");
        pseudolife_stdio::board::state::atomic_write(&token, b"fictional-bank-a\n", None).unwrap();
        let runtime = codex_runtime(&fixture, Some(token.clone()));
        let mut options = codex_options(&runtime, &home);
        options.timing.validation = Duration::from_secs(5);
        let board = Board::attach_options(runtime.clone(), options).await;
        attached(&fixture, &board).await;
        let start = mark(&fixture);
        if retried {
            fixture.answer("context", Answer::text(0, b""));
        }
        let held = if retried {
            Answer::json(200, json!({"bank_id":BANK,"principal":"fixture"}))
        } else {
            Answer::text(0, b"")
        };
        let (held, mut gate) = held.gated();
        fixture.answer("context", held);
        let pending = {
            let board = board.clone();
            tokio::spawn(async move { ack(&board).await })
        };
        gate.wait().await;
        pseudolife_stdio::board::state::atomic_write(&token, b"fictional-bank-b\n", None).unwrap();
        gate.release();
        let error = pending.await.unwrap().err().unwrap();
        let data = error.data.unwrap();
        assert_eq!(data["hint"], CREDENTIAL, "retried={retried}");
        assert_eq!(data["operation_outcome"], "not_dispatched");
        assert_eq!(
            contexts_since(&fixture, start).len(),
            1 + usize::from(retried),
            "retried={retried}"
        );
        board.close().await;
    }
}

#[tokio::test]
async fn codex_startup_failures_name_the_attachment() {
    // Startup is "identity attachment", not "authority validation".
    let slow = Answer::json(200, json!({"bank_id":BANK,"principal":"fixture"}))
        .delayed(Duration::from_millis(3500));
    for (name, action, answer, hint) in [
        (
            "503",
            "context",
            Answer::json(503, json!({})),
            "Coordination: identity attachment failed (context_unavailable); saved state was preserved. Retry when the bank responds.",
        ),
        (
            "timeout",
            "context",
            slow,
            "Coordination: identity attachment timed out (timeout); saved state was preserved. Retry when the bank responds.",
        ),
        (
            "busy",
            "attach",
            Answer::json(409, json!({"error":"attachment_busy"})),
            "Coordination: identity attachment is busy (attachment_busy); saved state was preserved. Wait for the prior attachment lease to expire.",
        ),
    ] {
        let fixture = Fixture::new(0);
        let home = Home::new();
        let runtime = codex_runtime(&fixture, None);
        let mut options = codex_options(&runtime, &home);
        // Only the timeout case needs a short budget. The others must
        // finish attaching (context, register, private state, attach),
        // which took over 1 s on the Windows dev VM's slow disk.
        options.timing.startup = if name == "timeout" {
            Duration::from_secs(1)
        } else {
            Duration::from_secs(10)
        };
        fixture.answer(action, answer);
        let board = Board::attach_options(runtime.clone(), options).await;
        let error = ack(&board).await.err().unwrap();
        assert_eq!(error.data.unwrap()["hint"], hint, "{name}");
        board.close().await;
    }
}
