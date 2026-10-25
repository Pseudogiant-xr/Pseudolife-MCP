//! The sweep's dream work after its reapers (`memory/dream.py`
//! `run_sweep_once` 2398-2547): the analyzer filing tick, the deep tick,
//! the merge judge, `dream_status`, the stall tick and the automatic dream;
//! and the session-end dream (`service_dream.py` `_fire_and_forget_dream`
//! 3120-3131), which a SessionEnd hook fires without waiting for it.
//!
//! Each step is the Python step's port; a step that Python wraps in its own
//! `try` never fails the tick here either (it logs and answers its `error`
//! shape), and one Python lets propagate (the analyzer tick, `dream_status`)
//! ends the tick, as an exception ends Python's.
//!
//! The review-queue judges between the merge judge and `dream_status`
//! (links, junk, curation, candidates) are not run yet (task P3b,
//! `divergences.md`).

use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::episodes::Caller;
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};

/// The deep-dream result keys the tick keeps (`deep_dream_tick`'s slim).
const DEEP_TICK_KEYS: [&str; 12] = [
    "applied",
    "error",
    "rescored",
    "superseded",
    "merged",
    "merge_proposed",
    "junk_proposed",
    "junk_deleted",
    "scoped",
    "analyzer_filed",
    "orphans_deleted",
    "snapshot",
];

fn log_value(prefix: &str, v: &Value) {
    eprintln!("{prefix}: {}", crate::pyjson::dumps(v));
}

/// `deep_dream_tick` (`service_dream.py:3824-3845`): when the need signal
/// recommends it, run the deep dream's apply without snippets (rescore,
/// junk, scopes, proposals, snapshot first; never the judgment step).
/// Never fails the sweep: a failure answers `fired: false` with its text.
pub async fn deep_dream_tick(svc: &Service, ready: &Ready) -> Value {
    let cfg = &svc.config.memory.deep_dream;
    if !cfg.auto_tick || crate::mutants::active("mcp-sweep-no-deep-tick") {
        return json!({"fired": false, "reason": "disabled"});
    }
    let need = {
        let _ws = ready.write.lock().await;
        super::service::deep_dream_need(ready, &svc.config, crate::episodes::now()).await
    };
    let need = match need {
        Ok(n) => n,
        Err(e) => return json!({"fired": false, "reason": format!("error: {}", error_text(&e))}),
    };
    if need.get("recommended").and_then(Value::as_bool) != Some(true) {
        return json!({"fired": false, "reason": "below_threshold", "need": need});
    }
    let result = match super::deep::deep_dream(svc, ready, true, false).await {
        Ok(r) => r,
        Err(e) => return json!({"fired": false, "reason": format!("error: {}", error_text(&e))}),
    };
    let mut out = Map::new();
    out.insert("fired".into(), json!(true));
    out.insert(
        "reason".into(),
        need.get("reason").cloned().unwrap_or(Value::Null),
    );
    for k in DEEP_TICK_KEYS {
        if let Some(v) = result.get(k) {
            out.insert(k.into(), v.clone());
        }
    }
    Value::Object(out)
}

/// `str(exc)` for a step's failure: `code: detail`, or the text.
fn error_text(e: &ToolError) -> String {
    match e {
        ToolError::Refused { code, detail, .. } => match detail {
            Some(d) => format!("{code}: {d}"),
            None => code.clone(),
        },
        ToolError::Invalid(t) | ToolError::FileNotFound(t) => t.clone(),
        ToolError::Internal(e) => e.to_string(),
    }
}

/// The deep tick's answer is worth a log line only when it fired
/// (`if deep and deep.get("fired")`).
fn fired(v: &Value) -> bool {
    v.get("fired").and_then(Value::as_bool) == Some(true)
}

/// `run_sweep_once` after the reapers and the `dream.enabled` gate.
pub async fn sweep(
    svc: &Service,
    ready: &Arc<Ready>,
    stop: &tokio::sync::watch::Receiver<bool>,
) -> Result<(), String> {
    // The analyzer's bounded filing pass, before the judges so what it
    // files is eligible for this tick's review. Python lets it raise.
    let analyzer = super::deep::analyzer_duplicate_tick(svc, ready)
        .await
        .map_err(|e| error_text(&e))?;
    if analyzer.get("filed").and_then(Value::as_i64).unwrap_or(0) != 0
        || analyzer.get("error").is_some()
    {
        log_value("analyzer duplicate tick", &analyzer);
    }
    let deep = deep_dream_tick(svc, ready).await;
    if fired(&deep) {
        log_value("deep-dream tick fired", &deep);
    }
    // The merge judge checks its gates first and initializes inside its
    // own `try` (`service_dream.py:4576-4596`).
    let judged = super::judge::deep_dream_judge(svc, stop).await;
    if super::judge::worth_logging(&judged) {
        log_value("deep-dream judge", &Value::Object(judged));
    }
    if *stop.borrow() {
        return Ok(());
    }
    let status = super::service::status(ready, &svc.config)
        .await
        .map_err(|e| error_text(&e))?;
    stall_tick(svc, ready, &status).await;
    if status.get("would_fire").and_then(Value::as_bool) != Some(true)
        || crate::mutants::active("mcp-sweep-no-auto-dream")
    {
        return Ok(());
    }
    let Some(me) = svc.handle() else {
        eprintln!(
            "dream sweep: the service handle is not installed; the automatic dream is skipped"
        );
        return Ok(());
    };
    match super::run::run_auto(
        &me,
        ready.clone(),
        background_caller(),
        background_writer(),
        None,
    )
    .await
    {
        Ok(result) => log_value("dream sweep fired", &result),
        Err(e) => return Err(error_text(&e)),
    }
    Ok(())
}

/// A background dream's caller: no request, so no session.
fn background_caller() -> Caller {
    Caller {
        header_session: None,
        transport_session: None,
    }
}

/// A background dream's writer: the process default (`PSEUDOLIFE_WRITER_ID`,
/// else `unknown`), as Python resolves it outside a request.
fn background_writer() -> String {
    crate::mcp::identity::env_writer().unwrap_or_else(|| "unknown".to_string())
}

/// `dream_stall_tick(status)` (`service_dream.py:2958-2971`): arm and apply
/// the overdue rule with the sweep's interval, then post a notice that is
/// due.
pub async fn stall_tick(svc: &Service, ready: &Ready, status: &Value) {
    let cfg = &svc.config.dream;
    {
        let mut tracker = ready.dream_run.stall.lock().expect("stall tracker");
        tracker.set_sweep_interval(cfg.sweep_interval_seconds);
        tracker.check_overdue(
            status.get("backlog").and_then(Value::as_i64).unwrap_or(0),
            cfg.min_batch,
            status.get("would_fire").and_then(Value::as_bool) == Some(true),
            cfg.sweep_interval_seconds,
        );
    }
    stall_notify(svc, ready).await;
}

/// `dream_stall_notify` (`service_dream.py:2973-3011`): post the board
/// notice a stall owes, one notifier at a time; a notice nobody received
/// stays owed. Never fails its caller.
pub async fn stall_notify(svc: &Service, ready: &Ready) -> Option<i64> {
    if !svc.config.dream.stall_notice {
        return None;
    }
    let _one = ready.dream_run.notify_lock.try_lock().ok()?;
    let repeat = svc.config.dream.stall_repeat_hours * 3600.0;
    let (kind, record) = ready
        .dream_run
        .stall
        .lock()
        .expect("stall tracker")
        .take_notice(repeat)?;
    let text = super::run::stall_notice_text(kind, &record);
    let sent = match svc.board() {
        Some(board) => board.daemon_notice(&text).await,
        None => None,
    };
    if sent.is_some_and(|n| n > 0) {
        ready
            .dream_run
            .stall
            .lock()
            .expect("stall tracker")
            .mark_noticed(kind);
    }
    sent
}

/// `_fire_and_forget_dream`: one automatic dream in the background, so the
/// caller (the SessionEnd hook) never waits on the extractor; a failure is
/// logged, never returned.
pub fn fire_and_forget(svc: &Arc<Service>) {
    let svc = svc.clone();
    tokio::spawn(async move {
        let ready = match svc.ensure_init().await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("session-end dream failed: {e}");
                return;
            }
        };
        let r2 = ready.clone();
        let me = svc.clone();
        let done = ready
            .writes
            .spawn(async move {
                super::run::run_auto(&me, r2, background_caller(), background_writer(), None).await
            })
            .await;
        match done {
            Ok(Ok(_)) => {}
            Ok(Err(e)) => eprintln!("session-end dream failed: {}", error_text(&e)),
            Err(e) => eprintln!("session-end dream failed: {e}"),
        }
    });
}
