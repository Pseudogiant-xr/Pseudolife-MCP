//! `memory_dream` on `/mcp` (`mcp_server.py:2171-2240`): `status`, `pull`,
//! `commit` and `runs` over the live resident bank (`service_dream.py`
//! `dream_status`, `dream_pull`, `dream_commit`, `dream_runs`).
//!
//! The acknowledgement engine (`cursors.rs`) owns the PostgreSQL halves:
//! initialization and the locked acknowledgement. This module joins them
//! to the daemon's resident bank, so entries stored after startup are
//! pullable, and keeps the tracking state Python keeps on the CMS (the
//! secret and the display cursor). Every call runs under the writer lock
//! (Python's service lock), detached from its request.
//!
//! Initialization is lazy: the first `memory_dream` call runs it, where
//! Python runs it while hydrating at startup. On a bank whose rows are all
//! classified the two are indistinguishable; rows with a NULL state are
//! classified at the first call here (declared).
//!
//! `run` is the consolidation pass (`run.rs`); `deep` and `rollback`
//! answer the `not_implemented` refusal.

use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use super::cursors::{self, Failure, Policy};
use super::token;
use crate::config::{Config, DreamConfig};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::service::Ready;

const TOOL: &str = "memory_dream";
const ACTIONS: [&str; 7] = [
    "status", "pull", "commit", "run", "deep", "runs", "rollback",
];
/// `_INFER_CURSOR_KEY` / `_DIGEST_CURSOR_KEY` (`service_dream.py:356-357`).
const INFER_CURSOR_KEY: &str = "outcome_inference_cursor";
const DIGEST_CURSOR_KEY: &str = "session_digest_cursor";
/// `_pending_inference_candidates`' default `limit`.
const INFER_CANDIDATE_LIMIT: usize = 8;
/// `_REVIEW_QUEUE_JUDGE_FIELDS` (`service_dream.py:3713-3715`).
const JUDGE_FIELDS: [&str; 6] = [
    "judges_enabled",
    "judge_mode",
    "link_judge_mode",
    "junk_judge_mode",
    "curation_judge_mode",
    "candidate_judge_mode",
];

/// The dream state Python keeps on the CMS and the service: the
/// acknowledgement secret, the display cursor, a latched initialization
/// failure and whether it may be retried.
#[derive(Debug, Default)]
pub struct Tracking {
    initialized: bool,
    secret: Option<String>,
    cursor: f64,
    error: Option<String>,
    retryable: bool,
}

impl Tracking {
    /// `_dream_display_cursor`: a JSON-safe display cursor.
    fn display_cursor(&self) -> f64 {
        if self.cursor.is_finite() {
            self.cursor
        } else {
            0.0
        }
    }

    /// `_dream_display_cursor()`, for the dream pass.
    pub(crate) fn display_cursor_value(&self) -> f64 {
        self.display_cursor()
    }

    fn error_code(&self) -> Option<(String, String)> {
        self.error
            .as_ref()
            .map(|e| (e.split(':').next().unwrap_or(e).to_string(), e.clone()))
    }
}

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

fn policy(cfg: &DreamConfig) -> Policy {
    Policy {
        eligible_sources: cfg.eligible_sources.clone().unwrap_or_default(),
        exclude_sources: cfg.exclude_sources.clone(),
    }
}

/// `_initialize_dream_tracking` (`service.py:3610-3736`), PostgreSQL mode.
async fn initialize(ready: &Ready, cfg: &DreamConfig, tracking: &mut Tracking) {
    let client = ready.storage.client();
    match cursors::initialize(client, &policy(cfg)).await {
        Ok(value) => {
            tracking.secret = value["secret"].as_str().map(String::from);
            tracking.cursor = value["dream_cursor"].as_f64().unwrap_or(0.0);
            if let Some(updated) = value["updated_states"].as_object()
                && !updated.is_empty()
            {
                let mut bank = ready.bank.write().expect("bank lock");
                for entry in &mut bank.entries {
                    if let Some(state) = updated.get(&entry.id.to_string()).and_then(Value::as_str)
                    {
                        entry.dream_state = Some(state.to_string());
                    }
                }
            }
            let cursor = tracking.cursor;
            ready.cortex.write(|c| c.dream_cursor = cursor);
            tracking.error = None;
            tracking.retryable = false;
        }
        Err(error) => {
            let text = error.text();
            tracking.retryable = matches!(error, Failure::Operational(_));
            tracking.error = Some(
                if text.starts_with("invalid_legacy_dream_cursor:")
                    || text.starts_with("invalid_dream_ack_state:")
                {
                    text.to_string()
                } else {
                    format!("dream_ack_initialization_failed: {text}")
                },
            );
            eprintln!("dream acknowledgement initialization failed: {text}");
        }
    }
    tracking.initialized = true;
}

/// The startup initialization (run lazily), then `_retry_dream_tracking`
/// where the action calls it (`status`, `pull`).
async fn ensure(ready: &Ready, cfg: &DreamConfig, tracking: &mut Tracking, retry: bool) {
    if !tracking.initialized {
        initialize(ready, cfg, tracking).await;
    }
    if retry && tracking.error.is_some() && tracking.retryable {
        initialize(ready, cfg, tracking).await;
    }
}

/// One pending resident, as the pull reports it.
struct Pending {
    id: i64,
    ts: f64,
    payload: Value,
}

/// `_pending_dream_entries` (`service_dream.py:1045-1078`): eligible
/// pending residents ordered by `(timestamp, db_id)`.
fn pending_entries(ready: &Ready, cfg: &DreamConfig, limit: Option<usize>) -> Vec<Pending> {
    let allowed = cfg.eligible_sources.as_ref().filter(|s| !s.is_empty());
    let bank = ready.bank.read().expect("bank lock");
    let mut rows: Vec<Pending> = bank
        .entries
        .iter()
        .filter(|e| e.dream_state.as_deref() == Some("pending"))
        .filter(|e| match allowed {
            Some(allowed) => allowed.contains(&e.source),
            None => !cfg.exclude_sources.contains(&e.source),
        })
        .map(|e| Pending {
            id: e.id,
            ts: e.ts,
            payload: json!({
                "text": e.text, "timestamp": e.ts, "episode_id": e.episode_id,
                "db_id": e.id, "dream_id": null, "source": e.source,
                "authority": e.authority, "distortion_tolerance": e.distortion_tolerance,
            }),
        })
        .collect();
    drop(bank);
    rows.sort_by(|a, b| {
        let ts = if a.ts == b.ts {
            std::cmp::Ordering::Equal
        } else {
            a.ts.total_cmp(&b.ts)
        };
        ts.then(a.id.cmp(&b.id))
    });
    if crate::mutants::active("mcp-dream-pull-newest-first") {
        rows.reverse();
    }
    if let Some(limit) = limit {
        rows.truncate(limit);
    }
    rows
}

/// `dream_pull` (`service_dream.py:1121-1188`).
pub async fn pull(ready: &Ready, cfg: &DreamConfig, limit: i64) -> Result<Value, ToolError> {
    let mut tracking = ready.dream.lock().await;
    ensure(ready, cfg, &mut tracking, true).await;
    let cursor = tracking.display_cursor();
    if let Some((code, detail)) = tracking.error_code() {
        return Ok(
            json!({"error": code, "detail": detail, "cursor": cursor, "count": 0,
                         "entries": []}),
        );
    }
    let cap = usize::try_from(limit.max(0))
        .unwrap_or(usize::MAX)
        .min(token::MAX_ENTRY_IDS);
    let rows = pending_entries(ready, cfg, Some(cap));
    let Some(secret) = tracking.secret.clone().filter(|s| !s.is_empty()) else {
        return Ok(json!({"error": "dream_ack_initialization_failed",
                         "detail": "dream acknowledgement secret is unavailable",
                         "cursor": cursor, "count": 0, "entries": []}));
    };
    let mut out = Map::new();
    out.insert("cursor".into(), json!(cursor));
    out.insert("count".into(), json!(rows.len()));
    out.insert(
        "entries".into(),
        Value::Array(rows.iter().map(|r| r.payload.clone()).collect()),
    );
    if !rows.is_empty() {
        let newest = rows
            .iter()
            .map(|r| r.ts)
            .reduce(|current, next| if next > current { next } else { current })
            .unwrap_or(cursor);
        let generation = token::generation(&secret).map_err(ToolError::Invalid)?;
        let ids: Vec<i64> = rows.iter().map(|r| r.id).collect();
        let signed =
            token::issue(&secret, &generation, &ids, newest).map_err(ToolError::Invalid)?;
        out.insert("commit_token".into(), json!(signed));
    }
    Ok(Value::Object(out))
}

/// `dream_commit` (`service_dream.py:1190-1302`), PostgreSQL mode.
pub async fn commit(
    ready: &Ready,
    cfg: &DreamConfig,
    persist_errors: &std::sync::atomic::AtomicU64,
    commit_token: &str,
) -> Result<Value, ToolError> {
    let mut tracking = ready.dream.lock().await;
    ensure(ready, cfg, &mut tracking, false).await;
    if let Some((code, detail)) = tracking.error_code() {
        return Ok(json!({"error": code, "detail": detail}));
    }
    let secret = tracking.secret.clone().unwrap_or_default();
    let verified = token::generation(&secret)
        .and_then(|generation| token::verify(commit_token, &secret, &generation));
    let payload = match verified {
        Ok(p) => p,
        Err(_) => {
            return Ok(json!({"error": "invalid_dream_commit_token",
                             "detail": "invalid_dream_commit_token"}));
        }
    };
    let client = ready.storage.client();
    match cursors::acknowledge(client, &payload.entry_ids, payload.display_timestamp).await {
        Ok(value) => {
            let ids = |key: &str| -> Vec<i64> {
                value[key]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_i64)
                    .collect()
            };
            let acknowledged = ids("acknowledged_ids");
            let missing = ids("missing_ids");
            // The resident copy of a vanished row is retired too.
            if !crate::mutants::active("mcp-dream-commit-resident-stale") {
                let settled: std::collections::HashSet<i64> =
                    acknowledged.iter().chain(&missing).copied().collect();
                let mut bank = ready.bank.write().expect("bank lock");
                for entry in &mut bank.entries {
                    if settled.contains(&entry.id) {
                        entry.dream_state = Some("acknowledged".into());
                    }
                }
            }
            let cursor = value["dream_cursor"].as_f64().unwrap_or(tracking.cursor);
            tracking.cursor = cursor;
            ready.cortex.write(|c| c.dream_cursor = cursor);
            if !missing.is_empty() {
                eprintln!(
                    "dream commit: {} pulled entr{} no longer exist(s); acknowledging the remaining {}",
                    missing.len(),
                    if missing.len() == 1 { "y" } else { "ies" },
                    acknowledged.len()
                );
            }
            let mut out = Map::new();
            out.insert("dream_cursor".into(), json!(cursor));
            out.insert("acknowledged".into(), json!(acknowledged.len()));
            out.insert(
                "newly_acknowledged".into(),
                value["newly_acknowledged"].clone(),
            );
            if !missing.is_empty() {
                out.insert("missing".into(), json!(missing.len()));
            }
            Ok(Value::Object(out))
        }
        Err(e) => {
            let code = match &e {
                Failure::Operational(_) => {
                    persist_errors.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    eprintln!("Dream acknowledgement was not confirmed: {}", e.text());
                    "dream_ack_persist_failed"
                }
                Failure::Refusal(text) => match text.split(':').next().unwrap_or(text) {
                    code @ ("dream_ack_invalid_states" | "dream_ack_missing_entries") => code,
                    _ => "dream_ack_rejected",
                },
            };
            Ok(json!({"error": code, "detail": e.text()}))
        }
    }
}

/// `_DREAM_RUN_COLS` (`storage/postgres.py:3198-3200`).
const RUN_COLS: [&str; 12] = [
    "id",
    "started_at",
    "finished_at",
    "cursor_before",
    "cursor_after",
    "pulled",
    "claims",
    "tallies",
    "status",
    "extractor",
    "writer_id",
    "rolled_back_at",
];

/// `dream_runs` (`service_dream.py:1033-1043`): newest first, None-valued
/// fields dropped per row.
pub async fn runs(ready: &Ready, limit: i64) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let rows = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT id, started_at, finished_at, cursor_before, cursor_after, pulled, \
             claims, tallies, status, extractor, writer_id, rolled_back_at \
             FROM dream_runs ORDER BY id DESC LIMIT $1",
            &[&limit],
        )
        .await
    })
    .await
    .map_err(internal)?;
    let mut out = Vec::new();
    for r in rows {
        let values = [
            json!(r.get::<_, i64>(0)),
            json!(r.get::<_, f64>(1)),
            json!(r.get::<_, Option<f64>>(2)),
            json!(r.get::<_, f64>(3)),
            json!(r.get::<_, Option<f64>>(4)),
            json!(r.get::<_, i32>(5)),
            json!(r.get::<_, i32>(6)),
            r.get::<_, Value>(7),
            json!(r.get::<_, String>(8)),
            json!(r.get::<_, Option<String>>(9)),
            json!(r.get::<_, Option<String>>(10)),
            json!(r.get::<_, Option<f64>>(11)),
        ];
        let mut row = Map::new();
        for (col, v) in RUN_COLS.iter().zip(values) {
            if !v.is_null() {
                row.insert((*col).into(), v);
            }
        }
        out.push(Value::Object(row));
    }
    Ok(json!({"count": out.len(), "runs": out}))
}

// ---------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------

pub(super) async fn get_meta(client: &Client, key: &str) -> Result<Option<Value>, ToolError> {
    let key = key.to_string();
    crate::txn::with_client(client, |c| async move {
        c.query_opt("SELECT value FROM meta WHERE key = $1", &[&key])
            .await
            .map(|r| r.map(|r| r.get::<_, Value>(0)))
    })
    .await
    .map_err(internal)
}

/// Python `float(x)` where a failure raises out of the status call.
pub(super) fn meta_float(v: Option<&Value>, default: f64) -> Result<f64, String> {
    match v {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(f64::from(u8::from(*b))),
        Some(Value::Number(n)) => n
            .to_string()
            .parse()
            .map_err(|_| "could not convert to float".to_string()),
        Some(Value::String(s)) => super::extractor::py_float_str(s)
            .ok_or_else(|| format!("could not convert string to float: {s:?}")),
        Some(other) => Err(format!(
            "float() argument must be a string or a real number, not {other}"
        )),
    }
}

/// `dict(raw.get("retry", {}))`: its length.
fn retry_len(raw: &Map<String, Value>) -> Result<usize, String> {
    match raw.get("retry") {
        None => Ok(0),
        Some(Value::Object(m)) => Ok(m.len()),
        Some(Value::Array(pairs))
            if pairs
                .iter()
                .all(|p| p.as_array().is_some_and(|a| a.len() == 2)) =>
        {
            let keys: std::collections::HashSet<String> =
                pairs.iter().map(|p| p[0].to_string()).collect();
            Ok(keys.len())
        }
        Some(other) => Err(format!("cannot convert {other} to dict")),
    }
}

/// `_load_infer_cursor` / `_load_digest_cursor`: `(ts, retry count, raw)`.
async fn load_cursor(
    client: &Client,
    key: &str,
) -> Result<(f64, usize, Map<String, Value>), ToolError> {
    match get_meta(client, key).await? {
        Some(Value::Object(raw)) => {
            let ts = meta_float(raw.get("ts"), 0.0).map_err(internal)?;
            let retry = retry_len(&raw).map_err(internal)?;
            Ok((ts, retry, raw))
        }
        _ => Ok((0.0, 0, Map::new())),
    }
}

/// `_episode_entry_counts` (`service.py:6782-6792`).
pub(super) fn episode_entry_counts(ready: &Ready) -> std::collections::HashMap<String, usize> {
    let mut counts = std::collections::HashMap::new();
    let bank = ready.bank.read().expect("bank lock");
    for e in &bank.entries {
        if let Some(ep) = e.episode_id.as_deref().filter(|ep| !ep.is_empty())
            && e.source != "digest"
        {
            *counts.entry(ep.to_string()).or_insert(0) += 1;
        }
    }
    counts
}

/// The subtree of a root: its id and every episode descending from it.
pub(super) fn subtree(log: &crate::episodes::EpisodeLog, root: &str) -> Vec<String> {
    let mut ids = vec![root.to_string()];
    for (i, e) in log.episodes.iter().enumerate() {
        if e.id != root && log.descends_from(i, root) {
            ids.push(e.id.clone());
        }
    }
    ids
}

/// Closed session roots, sorted by `key` (a stable sort, as Python's).
pub(super) fn closed_roots(
    log: &crate::episodes::EpisodeLog,
    keep: impl Fn(&crate::episodes::Episode) -> bool,
) -> Vec<&crate::episodes::Episode> {
    log.episodes
        .iter()
        .filter(|e| {
            e.parent_id.is_none()
                && e.session_key.as_deref().is_some_and(|k| !k.is_empty())
                && e.ended_at.is_some()
                && keep(e)
        })
        .collect()
}

pub(super) fn by_float(a: f64, b: f64) -> std::cmp::Ordering {
    a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal)
}

/// `len(_pending_inference_candidates())` (`service_dream.py:3167-3195`).
async fn infer_pending(
    ready: &Ready,
    log: &crate::episodes::EpisodeLog,
    cursor_ts: f64,
) -> Result<usize, ToolError> {
    let counts = episode_entry_counts(ready);
    let mut roots = closed_roots(log, |e| e.ended_at.is_some_and(|t| t > cursor_ts));
    roots.sort_by(|a, b| by_float(a.ended_at.unwrap_or(0.0), b.ended_at.unwrap_or(0.0)));
    let client = ready.storage.client();
    let mut out = 0;
    for root in roots {
        let ids = subtree(log, &root.id);
        if ids
            .iter()
            .map(|i| counts.get(i).copied().unwrap_or(0))
            .sum::<usize>()
            == 0
        {
            continue;
        }
        let ids2 = ids.clone();
        let signals: i64 = crate::txn::with_client(client, |c| async move {
            c.query_one(
                "SELECT COUNT(*) FROM outcome_signals WHERE episode_id = ANY($1)",
                &[&ids2],
            )
            .await
            .map(|r| r.get(0))
        })
        .await
        .map_err(internal)?;
        if signals > 0 {
            continue;
        }
        out += 1;
        if out >= INFER_CANDIDATE_LIMIT {
            break;
        }
    }
    Ok(out)
}

/// `len(_pending_digest_candidates())` (`service_dream.py:3217-3262`).
fn digest_pending(
    ready: &Ready,
    log: &crate::episodes::EpisodeLog,
    cfg: &DreamConfig,
    cursor_ts: f64,
    raw: &Map<String, Value>,
) -> usize {
    let cap = usize::try_from(cfg.digest_max_per_cycle.max(0)).unwrap_or(usize::MAX);
    let counts = episode_entry_counts(ready);
    let digested: std::collections::HashSet<String> = {
        let bank = ready.bank.read().expect("bank lock");
        bank.entries
            .iter()
            .filter(|e| e.source == "digest" && e.superseded_at.is_none())
            .filter_map(|e| e.episode_id.clone().filter(|ep| !ep.is_empty()))
            .collect()
    };
    let list = |key: &str| -> Vec<String> {
        raw.get(key)
            .and_then(Value::as_array)
            .map(|a| a.iter().map(super::extractor::py_str).collect())
            .unwrap_or_default()
    };
    let regenerate: std::collections::HashSet<String> = list("regenerate").into_iter().collect();
    let mut deferred = std::collections::HashMap::new();
    for (i, rid) in list("context_deferred").into_iter().enumerate() {
        deferred.insert(rid, i as i64);
    }
    let mut roots = closed_roots(log, |e| {
        e.ended_at.is_some_and(|t| t > cursor_ts) || regenerate.contains(&e.id)
    });
    roots.sort_by(|a, b| {
        let da = deferred.get(&a.id).copied().unwrap_or(-1);
        let db = deferred.get(&b.id).copied().unwrap_or(-1);
        da.cmp(&db)
            .then_with(|| by_float(a.ended_at.unwrap_or(0.0), b.ended_at.unwrap_or(0.0)))
    });
    let mut out = 0;
    for root in roots {
        if digested.contains(&root.id) {
            continue;
        }
        let n: usize = subtree(log, &root.id)
            .iter()
            .map(|i| counts.get(i).copied().unwrap_or(0))
            .sum();
        if n == 0 {
            continue;
        }
        out += 1;
        if out >= cap {
            break;
        }
    }
    out
}

/// Python `round(x, n)`.
fn py_round(x: f64, n: usize) -> f64 {
    format!("{x:.n$}").parse().unwrap_or(x)
}

/// `deep_dream_need` (`service_dream.py:3677-3710`).
async fn deep_dream_need(ready: &Ready, config: &Config, now: f64) -> Result<Value, ToolError> {
    let cfg = &config.memory.deep_dream;
    let client = ready.storage.client();
    let mark = get_meta(client, "deep_last_apply").await?;
    let max_id: i64 = crate::txn::with_client(client, |c| async move {
        c.query_one("SELECT coalesce(max(id), 0)::bigint FROM entities", &[])
            .await
            .map(|r| r.get(0))
    })
    .await
    .map_err(internal)?;
    let Some(mark) = mark else {
        if max_id == 0 {
            return Ok(json!({"recommended": false, "reason": "empty graph",
                             "last_apply_at": null}));
        }
        return Ok(json!({"recommended": true,
                         "reason": format!("never deep-dreamed ({max_id} entities)"),
                         "last_apply_at": null, "new_entities": max_id}));
    };
    let mark_id = match mark.get("max_entity_id") {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64))
            .ok_or_else(|| internal("invalid deep_last_apply max_entity_id"))?,
        Some(Value::String(s)) => super::extractor::py_int_str(s)
            .and_then(|v| i64::try_from(v).ok())
            .ok_or_else(|| internal("invalid deep_last_apply max_entity_id"))?,
        Some(Value::Bool(b)) => i64::from(*b),
        _ => return Err(internal("KeyError: 'max_entity_id'")),
    };
    let new: i64 = crate::txn::with_client(client, |c| async move {
        c.query_one("SELECT count(*) FROM entities WHERE id > $1", &[&mark_id])
            .await
            .map(|r| r.get(0))
    })
    .await
    .map_err(internal)?;
    let ts = meta_float(
        Some(mark.get("ts").ok_or_else(|| internal("KeyError: 'ts'"))?),
        0.0,
    )
    .map_err(internal)?;
    let days = (now - ts) / 86400.0;
    let mut out = Map::new();
    out.insert("recommended".into(), json!(false));
    out.insert("reason".into(), json!("below thresholds"));
    out.insert("last_apply_at".into(), mark["ts"].clone());
    out.insert("new_entities".into(), json!(new));
    out.insert("days_since".into(), json!(py_round(days, 2)));
    if cfg.auto_min_new_entities != 0 && new >= cfg.auto_min_new_entities {
        out.insert("recommended".into(), json!(true));
        out.insert(
            "reason".into(),
            json!(format!("{new} new entities since the last deep apply")),
        );
    } else if cfg.auto_interval_days != 0.0 && days >= cfg.auto_interval_days {
        out.insert("recommended".into(), json!(true));
        out.insert(
            "reason".into(),
            json!(format!("{days:.1} days since the last deep apply")),
        );
    }
    Ok(Value::Object(out))
}

/// `review_queue_health(include_agreement=True)` (`service_dream.py:3717-3787`).
async fn review_queue_health(ready: &Ready, config: &Config, now: f64) -> Value {
    let quiet = json!({"needed": false, "reasons": []});
    match review_queue_inner(ready, config, now).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("review-queue health failed: {e}");
            json!({"error": e, "attention": quiet})
        }
    }
}

async fn review_queue_inner(ready: &Ready, config: &Config, now: f64) -> Result<Value, String> {
    let cfg = &config.memory.deep_dream;
    let client = ready.storage.client();
    let (merge, junk, oldest, unjudged, link) = crate::txn::with_client(client, |c| async move {
        let r = c
            .query_one(
                "SELECT count(*) FILTER (WHERE kind = 'merge'), \
                        count(*) FILTER (WHERE kind = 'junk'), \
                        min(created_at) FILTER (WHERE kind = 'merge'), \
                        min(created_at) FILTER (WHERE kind = 'merge' \
                                                AND judge_verdict IS NULL) \
                 FROM entity_proposals WHERE status = 'pending'",
                &[],
            )
            .await?;
        let link: i64 = c
            .query_one(
                "SELECT count(*) FROM edge_proposals WHERE status = 'pending'",
                &[],
            )
            .await?
            .get(0);
        Ok::<_, tokio_postgres::Error>((
            r.get::<_, i64>(0),
            r.get::<_, i64>(1),
            r.get::<_, Option<f64>>(2),
            r.get::<_, Option<f64>>(3),
            link,
        ))
    })
    .await
    .map_err(|e| format!("{}: {e}", "OperationalError"))?;
    let age = |ts: Option<f64>| ts.map(|t| py_round((now - t).max(0.0) / 86400.0, 2));
    let (age_v, unjudged_age) = (age(oldest), age(unjudged));
    let mut reasons: Vec<String> = Vec::new();
    if cfg.review_queue_alert_pending != 0 && merge >= cfg.review_queue_alert_pending {
        reasons.push(format!(
            "{merge} merge proposals pending (alert at {})",
            cfg.review_queue_alert_pending
        ));
    }
    let draining = cfg.judges_enabled && matches!(cfg.judge_mode.as_str(), "auto-reject" | "auto");
    let judge = if cfg.judges_enabled {
        cfg.judge_mode.clone()
    } else {
        "disabled (judges_enabled false)".into()
    };
    let waited = if draining { unjudged_age } else { age_v };
    if cfg.review_queue_alert_age_days != 0.0
        && let Some(waited) = waited
        && waited >= cfg.review_queue_alert_age_days
        && merge >= cfg.review_queue_alert_min_pending
    {
        let what = if draining {
            "unjudged merge proposal"
        } else {
            "pending merge proposal"
        };
        reasons.push(format!(
            "oldest {what} {waited:.0} days old ({merge} pending) while the merge judge is {judge}"
        ));
    }
    let mut judges = Map::new();
    for key in JUDGE_FIELDS {
        let v = match key {
            "judges_enabled" => json!(cfg.judges_enabled),
            "judge_mode" => json!(cfg.judge_mode),
            "link_judge_mode" => json!(cfg.link_judge_mode),
            "junk_judge_mode" => json!(cfg.junk_judge_mode),
            "curation_judge_mode" => json!(cfg.curation_judge_mode),
            _ => json!(cfg.candidate_judge_mode),
        };
        judges.insert(key.into(), v);
    }
    let agreement = super::judge_agreement::judge_agreement(
        client,
        now,
        cfg.judge_accept_min_confidence,
        cfg.judge_reject_min_confidence_2,
        cfg.link_two_vote_min_confidence,
    )
    .await;
    let mut out = Map::new();
    out.insert(
        "pending".into(),
        json!({"merge": merge, "junk": junk, "link": link}),
    );
    out.insert("oldest_merge_age_days".into(), json!(age_v));
    out.insert("oldest_unjudged_merge_age_days".into(), json!(unjudged_age));
    out.insert("judges".into(), Value::Object(judges));
    out.insert("judge_agreement".into(), agreement);
    out.insert(
        "attention".into(),
        json!({"needed": !reasons.is_empty(), "reasons": reasons}),
    );
    Ok(Value::Object(out))
}

/// `dream_status` (`service_dream.py:3035-3118`).
pub async fn status(ready: &Ready, config: &Config) -> Result<Value, ToolError> {
    let cfg = &config.dream;
    let ws = ready.write.lock().await;
    let mut tracking = ready.dream.lock().await;
    ensure(ready, cfg, &mut tracking, true).await;
    let backlog = if tracking.error.is_some() {
        0
    } else {
        pending_entries(ready, cfg, None).len()
    };
    let latest = {
        let bank = ready.bank.read().expect("bank lock");
        bank.entries
            .iter()
            .map(|e| e.ts)
            // `max()`: an item replaces the best only when greater.
            .fold(None, |best: Option<f64>, ts| match best {
                Some(b) if ts > b => Some(ts),
                Some(b) => Some(b),
                None => Some(ts),
            })
            .unwrap_or(0.0)
    };
    let cursor = tracking.display_cursor();
    let endpoints = super::extractor::resolve_endpoints(cfg);
    let has_extractor = endpoints.has_primary() || endpoints.has_fallback();
    let client = ready.storage.client();
    let m = &config.memory;
    let (infer, retry) = if m.lessons_enabled
        && m.lessons_infer_outcomes
        && m.lessons_infer_outcomes_max_signals > 0
        && has_extractor
    {
        let (ts, retry, _) = load_cursor(client, INFER_CURSOR_KEY).await?;
        (infer_pending(ready, &ws.log, ts).await?, retry)
    } else {
        (0, 0)
    };
    let (digest, digest_retry) = if cfg.digest_enabled && has_extractor {
        let (ts, retry, raw) = load_cursor(client, DIGEST_CURSOR_KEY).await?;
        (digest_pending(ready, &ws.log, cfg, ts, &raw), retry)
    } else {
        (0, 0)
    };
    let error = tracking.error_code();
    drop(tracking);
    let last_dream_extractor = ready
        .dream_run
        .last_dream_extractor
        .lock()
        .expect("extractor record")
        .clone();
    let now = crate::episodes::now();
    let idle = if latest != 0.0 { now - latest } else { 0.0 };
    let would_fire = error.is_none()
        && cfg.enabled
        && ((backlog as i64) >= cfg.min_batch
            || (backlog >= 1 && idle >= cfg.idle_seconds)
            || infer >= 1
            || (digest >= 1 && backlog == 0));
    let deep = deep_dream_need(ready, config, now).await?;
    let review = review_queue_health(ready, config, now).await;
    drop(ws);
    let mut out = Map::new();
    out.insert("backlog".into(), json!(backlog));
    out.insert("idle_seconds".into(), json!(idle));
    out.insert("dream_cursor".into(), json!(cursor));
    out.insert("would_fire".into(), json!(would_fire));
    out.insert(
        "infer_outcomes".into(),
        json!({"pending": infer, "retry_pending": retry}),
    );
    out.insert(
        "digests".into(),
        json!({"pending": digest, "retry_pending": digest_retry}),
    );
    out.insert("deep_dream".into(), deep);
    out.insert("review_queue".into(), review);
    // The stall tracker is process-local; its overdue rule is armed by the
    // sweep tick, which this daemon does not run yet, so only `run` passes
    // move it.
    let (stall, last_stall) = ready
        .dream_run
        .stall
        .lock()
        .expect("stall tracker")
        .snapshot();
    out.insert("stall".into(), stall);
    out.insert("last_stall".into(), last_stall);
    out.extend(super::extractor::status_extractor_fields(cfg, last_dream_extractor.as_ref()).await);
    if let Some((code, detail)) = error {
        out.insert("error".into(), json!(code));
        out.insert("detail".into(), json!(detail));
    }
    Ok(Value::Object(out))
}

// ---------------------------------------------------------------------------
// MCP
// ---------------------------------------------------------------------------

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![(TOOL, mcp_memory_dream)]
}

/// The refusal an unregistered tool answers, for the actions not served yet.
fn unserved() -> ToolError {
    let stub = crate::mcp::dispatch::not_implemented(TOOL);
    let message = stub["message"].as_str().unwrap_or_default();
    ToolError::refused("not_implemented", message.strip_prefix("not_implemented: "))
}

fn positive(args: &Map<String, Value>, key: &str, default: i64) -> i64 {
    // `limit or default`: the binding refuses anything under 1.
    args.get(key)
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .unwrap_or(default)
}

fn mcp_memory_dream<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    let action = call
        .args
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if action == "run" {
        let service = app.service.clone();
        let limit = call.args.get("limit").and_then(Value::as_i64);
        let caller = crate::episodes::Caller {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let writer = call.ident.writer.clone();
        return Box::pin(async move {
            if let Some(refused) = super::run::refusal(&service.config) {
                return Err(refused);
            }
            let ready = service.ensure_init().await.map_err(internal)?;
            let r2 = ready.clone();
            let svc = service.clone();
            ready
                .writes
                .spawn(async move { super::run::run_auto(&svc, r2, caller, writer, limit).await })
                .await
                .map_err(internal)?
        });
    }
    if !matches!(action.as_str(), "status" | "pull" | "commit" | "runs") {
        return Box::pin(async move {
            if matches!(action.as_str(), "deep" | "rollback") {
                Err(unserved())
            } else {
                Ok(json!({"error": "unknown_action", "actions": ACTIONS}))
            }
        });
    }
    if action == "commit" && call.args.get("commit_token").is_none_or(Value::is_null) {
        let legacy = call.args.get("cursor").is_some_and(|c| !c.is_null());
        return Box::pin(async move {
            Ok(if legacy {
                json!({"error": "legacy_cursor_unsupported",
                       "detail": "Timestamp commits are unsafe. Pull again and pass the returned commit_token."})
            } else {
                json!({"error": "commit_token_required"})
            })
        });
    }
    let service = app.service.clone();
    let args = call.args.clone();
    Box::pin(async move {
        let ready = service.ensure_init().await.map_err(internal)?;
        let r2 = ready.clone();
        let svc = service.clone();
        ready
            .writes
            .spawn(async move { run_action(&svc, r2, &action, &args).await })
            .await
            .map_err(internal)?
    })
}

async fn run_action(
    service: &Arc<crate::service::Service>,
    ready: Arc<Ready>,
    action: &str,
    args: &Map<String, Value>,
) -> Result<Value, ToolError> {
    let config = &service.config;
    match action {
        "status" => status(&ready, config).await,
        "runs" => {
            let _ws = ready.write.lock().await;
            runs(&ready, positive(args, "limit", 10)).await
        }
        "pull" => {
            let _ws = ready.write.lock().await;
            pull(&ready, &config.dream, positive(args, "limit", 40)).await
        }
        _ => {
            let _ws = ready.write.lock().await;
            let token = args
                .get("commit_token")
                .and_then(Value::as_str)
                .unwrap_or_default();
            commit(&ready, &config.dream, &service.persist_errors, token).await
        }
    }
}
