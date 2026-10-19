//! `memory_dream(action="run")`: the consolidation pass (`mcp_server.py:2229`
//! -> `service_dream.py` `dream_run_auto` 2922-2951, `dream_run` 1491-1508,
//! `_dream_run_locked` 1510-2394, the constraint carrier 2396-2475 and the
//! quarantine route 2477-2568).
//!
//! One pass: pull the pending window, embed the batch text for the vocab
//! hint, make ONE batched `extract` call (unlocked), repair flattened slot
//! keys, attribute each claim to its cited note, then write claim by claim
//! through the cortex service functions (`fact_set::cortex_write`,
//! `set_members::set_add` / `set_remove`, `fact_resolve::cortex_resolve`),
//! each of which takes the writer lock itself, as Python's do. Journal,
//! trace and chronicle statements take the writer lock around their own
//! transaction. Then the separate events pass, the commit, the run stamp,
//! and the post-commit graph work (`relations.rs`).
//!
//! Stages that are not ported (outcome inference, session digests, lesson
//! synthesis, the graph-insight refresh) refuse the whole action with
//! `not_implemented` naming the stage while their switch is on; with it off
//! Python returns a fixed `disabled` answer, reproduced here.

use std::collections::{BTreeSet, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value, json};

use super::extractor::{Claim, Event, ExtractFailure, Extracted, Extractor};
use super::gates;
use crate::config::{Config, DreamConfig};
use crate::episodes::Caller;
use crate::fact_set::{CortexWriteArgs, LabelArg};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::storage::py_strip;
use crate::stores::cortex::{CortexRecord, CortexStore, dot_f32, norm_key, unit_f32};
use crate::stores::cortex_write::{direct_backed, is_compression_echo, norm_value};

/// `max_batch_failures` (`service_dream.py:1561`).
const MAX_BATCH_FAILURES: u32 = 3;
/// `_dream_hints`' `vocab_limit`.
const VOCAB_LIMIT: usize = 120;
/// Actions that stored nothing: no journal row, not a carrier.
const NOTHING_STORED: [&str; 4] = [
    "member_invalid",
    "member_capped",
    "member_not_found",
    "member_remove_refused",
];

pub(super) fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// Refuse a write whose parameters hold a reply's lone surrogate, as
/// psycopg (or the embedder, for the text it embeds) refuses it in Python:
/// the failure takes the write's own error path.
#[allow(clippy::result_large_err)]
pub(super) fn encodable(fields: &[&str]) -> Result<(), ToolError> {
    match super::extractor::unencodable(fields) {
        Some(_) if crate::mutants::active("mcp-dream-run-encode-unchecked") => Ok(()),
        Some(e) => Err(internal(e)),
        None => Ok(()),
    }
}

fn now() -> f64 {
    crate::episodes::now()
}

// ---------------------------------------------------------------------------
// Process-local state
// ---------------------------------------------------------------------------

/// What Python keeps on the service instance between passes.
#[derive(Default)]
pub struct RunState {
    /// `_dream_run_guard`: one pass at a time, non-blocking.
    guard: AtomicBool,
    /// `_dream_batch_failures`, keyed by the batch's entry ids.
    batch_failures: Mutex<HashMap<Vec<i64>, u32>>,
    /// `_alias_name_memo`: entity display name -> normalized embedding.
    pub(super) alias_memo: Mutex<HashMap<String, Arc<[f32]>>>,
    /// `DreamStallTracker`.
    pub stall: Mutex<StallTracker>,
    /// `_last_dream_extractor`: `{which, base_url, at}`.
    pub last_dream_extractor: Mutex<Option<Value>>,
}

/// Releases the single-flight guard however the pass ends.
struct Flight<'a>(&'a AtomicBool);

impl Drop for Flight<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// `DreamStallTracker` (`service_dream.py:47-220`): `record` and `snapshot`.
/// The overdue rule runs only once a sweep runs, which this daemon does not
/// yet; the board notice is not ported (see `notice_due`).
#[derive(Debug, Default)]
pub struct StallTracker {
    failures: u32,
    first_failure_at: Option<f64>,
    reason: Option<String>,
    last_error: Option<String>,
    last_success_at: Option<f64>,
    stall: Option<Map<String, Value>>,
    last_stall: Option<Map<String, Value>>,
    notice_at: Option<f64>,
    clear_owed: bool,
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

fn text_or(v: Option<&Value>, default: &str) -> String {
    match v {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => default.to_string(),
    }
}

impl StallTracker {
    fn open(&mut self, since: f64) {
        if self.stall.is_none() {
            self.clear_owed = false;
        }
        let mut m = Map::new();
        m.insert("since".into(), json!(since));
        m.insert("reason".into(), json!(self.reason));
        m.insert("consecutive_failures".into(), json!(self.failures));
        m.insert("last_error".into(), json!(self.last_error));
        m.insert("last_success_at".into(), json!(self.last_success_at));
        self.stall = Some(m);
    }

    /// `record(result, served_by_fallback=...)`.
    pub fn record(&mut self, result: &Map<String, Value>, served_by_fallback: bool) {
        if truthy(result.get("skipped")) || truthy(result.get("error")) {
            return;
        }
        let pulled = result.get("pulled").and_then(Value::as_i64).unwrap_or(0);
        let now = now();
        let err = result.get("extractor_error").and_then(Value::as_object);
        let field = |k: &str| err.and_then(|e| e.get(k));
        let (reason, error) = if truthy(result.get("extractor_failed")) {
            if result.get("hold_phase").and_then(Value::as_str) == Some("write") {
                return;
            }
            (
                text_or(field("reason"), "extractor_error"),
                text_or(field("error"), "extraction failed"),
            )
        } else if pulled > 0
            && result
                .get("quarantined")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                >= pulled
        {
            (
                text_or(field("reason"), "extractor_error"),
                text_or(field("error"), "every pulled memory failed extraction"),
            )
        } else if served_by_fallback {
            (
                "served_by_fallback".to_string(),
                "primary health probe failed".to_string(),
            )
        } else if pulled > 0 {
            self.succeed(now);
            return;
        } else {
            return;
        };
        self.failures += 1;
        if self.first_failure_at.is_none() {
            self.first_failure_at = Some(now);
        }
        self.reason = Some(reason);
        self.last_error = Some(error);
        if self.stall.is_some() || self.failures >= 2 {
            let since = self
                .stall
                .as_ref()
                .and_then(|s| s.get("since"))
                .and_then(Value::as_f64)
                .or(self.first_failure_at)
                .unwrap_or(now);
            self.open(since);
        }
    }

    fn succeed(&mut self, now: f64) {
        self.last_success_at = Some(now);
        self.failures = 0;
        self.first_failure_at = None;
        self.reason = None;
        self.last_error = None;
        if let Some(mut stall) = self.stall.take() {
            stall.insert("recovered_at".into(), json!(now));
            if self.notice_at.is_some() {
                self.clear_owed = true;
            }
            self.last_stall = Some(stall);
            self.notice_at = None;
        }
    }

    /// `snapshot()`: `(stall, last_stall)`.
    pub fn snapshot(&self) -> (Value, Value) {
        let v = |m: &Option<Map<String, Value>>| m.clone().map_or(Value::Null, Value::Object);
        (v(&self.stall), v(&self.last_stall))
    }

    /// Whether `take_notice` would owe a notice now (an open stall, or a
    /// clear for an announced incident). The board notice itself is not
    /// ported.
    fn notice_due(&self) -> bool {
        self.stall.is_some() || self.clear_owed
    }
}

// ---------------------------------------------------------------------------
// Stages
// ---------------------------------------------------------------------------

/// The `not_implemented` refusal while a stage this port does not run is
/// switched on, or None.
pub fn refusal(config: &Config) -> Option<ToolError> {
    let m = &config.memory;
    let stage = if m.lessons_enabled
        && m.lessons_infer_outcomes
        && m.lessons_infer_outcomes_max_signals > 0
    {
        "infer_outcomes_stage (memory.lessons.infer_outcomes)"
    } else if m.lessons_enabled && m.lessons_synthesize_in_dream {
        "synthesize_lessons (memory.lessons.synthesize_in_dream)"
    } else if config.dream.digest_enabled {
        "generate_digests_stage (memory.dream.digest_enabled)"
    } else if m.graph_insight_enabled {
        "_safe_refresh_graph_insight (memory.graph_insight.enabled)"
    } else {
        return None;
    };
    Some(ToolError::refused(
        "not_implemented",
        Some(&format!(
            "memory_dream run: the {stage} stage is not yet served by the Rust daemon; \
             switch it off to run the consolidation pass"
        )),
    ))
}

/// `infer_outcomes_stage` with its switch off.
fn outcome_inference_disabled() -> Value {
    json!({"scanned": 0, "written": 0, "skipped": "disabled"})
}

/// `generate_digests_stage` with its switch off.
fn digests_disabled() -> Value {
    json!({"scanned": 0, "written": 0, "skipped": "disabled"})
}

/// `synthesize_lessons` with its switch off.
fn lessons_disabled() -> Value {
    json!({"signals": 0, "lessons": 0, "skipped": "disabled"})
}

/// `_safe_refresh_graph_insight` with `memory.graph_insight.enabled` off.
fn graph_insight_disabled() -> Value {
    json!({"refreshed": false, "reason": "disabled"})
}

// ---------------------------------------------------------------------------
// Hints
// ---------------------------------------------------------------------------

fn key_text(r: &CortexRecord) -> String {
    format!("{}.{}", r.key().0, r.key().1)
}

/// `CortexStore.vocab(limit)`: current slot keys, sorted.
pub(super) fn vocab_alpha(s: &CortexStore, limit: usize) -> Vec<String> {
    let keys: BTreeSet<String> = s.current_records().map(key_text).collect();
    keys.into_iter().take(limit).collect()
}

/// Indices of `with` ordered by descending cosine of their slot embedding
/// against `q` (a stable sort, as Python's `sorted(..., key=-sim)`).
fn ranked(with: &[&CortexRecord], q: &[f32]) -> Vec<usize> {
    let qn = unit_f32(q);
    let sims: Vec<f32> = with
        .iter()
        .map(|r| dot_f32(&unit_f32(r.slot_embedding.as_deref().unwrap_or(&[])), &qn))
        .collect();
    let mut order: Vec<usize> = (0..with.len()).collect();
    order.sort_by(|&a, &b| {
        sims[b]
            .partial_cmp(&sims[a])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    order
}

/// `CortexStore.vocab_ranked` (`cortex.py:1544-1573`).
pub(super) fn vocab_ranked(s: &CortexStore, q: &[f32], limit: usize) -> Vec<String> {
    let cur: Vec<&CortexRecord> = s.current_records().collect();
    let with: Vec<&CortexRecord> = cur
        .iter()
        .copied()
        .filter(|r| r.slot_embedding.is_some())
        .collect();
    if with.is_empty() {
        return vocab_alpha(s, limit);
    }
    let mut keys = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for i in ranked(&with, q) {
        let k = key_text(with[i]);
        if seen.insert(k.clone()) {
            keys.push(k);
        }
    }
    let tail: BTreeSet<String> = cur
        .iter()
        .filter(|r| r.slot_embedding.is_none())
        .map(|r| key_text(r))
        .collect();
    keys.extend(tail.into_iter().filter(|k| !seen.contains(k)));
    keys.truncate(limit);
    keys
}

type Fact = (String, String, String);

/// `CortexStore.facts_ranked` (`cortex.py:1575-1608`).
pub(super) fn facts_ranked(s: &CortexStore, q: &[f32], limit: usize) -> Vec<Fact> {
    if limit == 0 {
        return Vec::new();
    }
    let triple = |r: &CortexRecord| -> Fact {
        let v = if r.value.chars().count() <= 120 {
            r.value.clone()
        } else {
            format!("{}\u{2026}", r.value.chars().take(119).collect::<String>())
        };
        (r.entity.clone(), r.attribute.clone(), v)
    };
    let cur: Vec<&CortexRecord> = s.current_records().collect();
    let with: Vec<&CortexRecord> = cur
        .iter()
        .copied()
        .filter(|r| r.slot_embedding.is_some())
        .collect();
    let by_key = |v: &mut Vec<&CortexRecord>| v.sort_by_key(|r| key_text(r));
    if with.is_empty() {
        let mut all = cur.clone();
        by_key(&mut all);
        return all.into_iter().take(limit).map(triple).collect();
    }
    let mut out: Vec<Fact> = ranked(&with, q)
        .into_iter()
        .map(|i| triple(with[i]))
        .collect();
    let mut tail: Vec<&CortexRecord> = cur
        .iter()
        .copied()
        .filter(|r| r.slot_embedding.is_none())
        .collect();
    by_key(&mut tail);
    out.extend(tail.into_iter().map(triple));
    out.truncate(limit);
    out
}

/// `_dream_hints` (`service_dream.py:1328-1347`): the relevance-ranked
/// vocab and the known-facts window from one document-side embedding of
/// the batch text; the alphabetical vocab if the embedding fails.
async fn hints(
    ready: &Arc<Ready>,
    texts: &[String],
    facts_limit: usize,
) -> (Vec<String>, Vec<Fact>) {
    let joined: String = texts.join(" ").chars().take(4000).collect();
    let _ws = ready.write.lock().await;
    match crate::fact_set::embed(ready, joined).await {
        Ok(emb) => {
            let snap = ready.cortex.snapshot();
            let vocab = vocab_ranked(&snap, &emb, VOCAB_LIMIT);
            let facts = if facts_limit > 0 {
                facts_ranked(&snap, &emb, facts_limit)
            } else {
                Vec::new()
            };
            (vocab, facts)
        }
        Err(e) => {
            eprintln!("dream hint build failed ({e:?}); using alphabetical vocab, no facts window");
            (
                vocab_alpha(&ready.cortex.snapshot(), VOCAB_LIMIT),
                Vec::new(),
            )
        }
    }
}

/// `CortexStore.resolve_slot` (`cortex.py:1739-1762`).
fn resolve_slot(s: &CortexStore, q: &[f32], threshold: f64) -> Option<(String, String)> {
    if threshold <= 0.0 {
        return None;
    }
    let cands: Vec<&CortexRecord> = s
        .current_records()
        .filter(|r| r.slot_embedding.is_some())
        .collect();
    if cands.is_empty() {
        return None;
    }
    let qn = unit_f32(q);
    let mut best = 0;
    let mut best_sim = f32::NEG_INFINITY;
    for (i, r) in cands.iter().enumerate() {
        let sim = dot_f32(&unit_f32(r.slot_embedding.as_deref().unwrap_or(&[])), &qn);
        if sim > best_sim {
            best = i;
            best_sim = sim;
        }
    }
    (f64::from(best_sim) >= threshold)
        .then(|| (cands[best].entity.clone(), cands[best].attribute.clone()))
}

/// `_resolve_dream_slot` (`service_dream.py:1304-1326`): a no-op at the
/// default threshold (0.0).
async fn resolve_dream_slot(
    ready: &Arc<Ready>,
    threshold: f64,
    entity: &str,
    attribute: &str,
) -> (String, String) {
    let original = (entity.to_string(), attribute.to_string());
    if threshold <= 0.0 {
        return original;
    }
    let _ws = ready.write.lock().await;
    if ready.cortex.snapshot().lookup(entity, attribute).is_some() {
        return original;
    }
    let text = py_strip(&format!("{entity} {attribute}")).to_string();
    // The embedder's tokenizer refuses a lone surrogate (a TypeError in
    // Python), which this resolver swallows.
    if let Some(e) = super::extractor::unencodable(&[&text]) {
        eprintln!("dream slot resolve failed ({e}); using literal slot");
        return original;
    }
    match crate::fact_set::embed(ready, text).await {
        Ok(emb) => resolve_slot(&ready.cortex.snapshot(), &emb, threshold).unwrap_or(original),
        Err(e) => {
            eprintln!("dream slot resolve failed ({e:?}); using literal slot");
            original
        }
    }
}

// ---------------------------------------------------------------------------
// Storage statements
// ---------------------------------------------------------------------------

/// `start_dream_run` (`storage/postgres.py:3208-3218`).
async fn start_run(
    ready: &Ready,
    cursor_before: f64,
    pulled: i32,
    extractor: &str,
) -> Result<i64, ToolError> {
    let _ws = ready.write.lock().await;
    let started = now();
    crate::txn::run(ready.storage.client(), |c| async move {
        c.query_one(
            "INSERT INTO dream_runs (started_at, cursor_before, pulled, status, extractor, \
             writer_id) VALUES ($1, $2, $3, 'running', $4, NULL) RETURNING id",
            &[&started, &cursor_before, &pulled, &extractor],
        )
        .await
        .map(|r| r.get::<_, i64>(0))
    })
    .await
    .map_err(internal)
}

/// `finish_dream_run` (`storage/postgres.py:3220-3228`).
async fn finish_run_row(
    ready: &Ready,
    id: i64,
    status: &str,
    cursor_after: Option<f64>,
    claims: i32,
    tallies: Value,
) -> Result<(), ToolError> {
    let _ws = ready.write.lock().await;
    let finished = now();
    let tallies = &tallies;
    crate::txn::run(ready.storage.client(), |c| async move {
        c.execute(
            "UPDATE dream_runs SET status=$1, finished_at=$2, cursor_after=$3, claims=$4, \
             tallies=$5 WHERE id=$6",
            &[&status, &finished, &cursor_after, &claims, tallies, &id],
        )
        .await
    })
    .await
    .map(drop)
    .map_err(internal)
}

/// `update_dream_run_tallies` (`storage/postgres.py:3230-3239`).
async fn update_run_tallies(ready: &Ready, id: i64, extra: Value) -> Result<(), ToolError> {
    let _ws = ready.write.lock().await;
    let extra = &extra;
    crate::txn::run(ready.storage.client(), |c| async move {
        c.execute(
            "UPDATE dream_runs SET tallies = COALESCE(tallies, '{}'::jsonb) || $1 WHERE id=$2",
            &[extra, &id],
        )
        .await
    })
    .await
    .map(drop)
    .map_err(internal)
}

/// One `dream_run_slots` row (`add_dream_run_slot`, 3241-3262).
struct SlotRow {
    seq: i32,
    entity: String,
    attribute: String,
    kind: &'static str,
    op: Option<String>,
    prev_kind: Option<&'static str>,
    prev: Option<(String, String, f64, Option<String>)>,
    new_value: String,
    action: String,
    src_entry_id: Option<i64>,
    chronicle_event_id: Option<i64>,
    /// Event rows journal `attribute_norm` "event", not `norm_key`.
    attribute_norm: Option<String>,
}

async fn add_run_slot(ready: &Ready, run_id: i64, row: SlotRow) -> Result<(), ToolError> {
    encodable(&[
        &row.entity,
        &row.attribute,
        &row.new_value,
        row.prev.as_ref().map_or("", |p| p.0.as_str()),
    ])?;
    let _ws = ready.write.lock().await;
    let entity_norm = norm_key(&row.entity);
    let attribute_norm = row
        .attribute_norm
        .clone()
        .unwrap_or_else(|| norm_key(&row.attribute));
    let (pv, ps, pc, pp) = match &row.prev {
        Some((v, s, c, p)) => (Some(v.clone()), Some(s.clone()), Some(*c as f32), p.clone()),
        None => (None, None, None, None),
    };
    let at = now();
    let row = &row;
    let (entity_norm, attribute_norm) = (&entity_norm, &attribute_norm);
    let (pv, ps, pp) = (&pv, &ps, &pp);
    crate::txn::run(ready.storage.client(), |c| async move {
        c.execute(
            "INSERT INTO dream_run_slots (run_id, seq, entity, attribute, entity_norm, \
             attribute_norm, kind, op, prev_kind, prev_value, prev_status, prev_confidence, \
             prev_support, new_value, action, src_entry_id, at, chronicle_event_id) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, \
             $17, $18)",
            &[
                &run_id,
                &row.seq,
                &row.entity,
                &row.attribute,
                entity_norm,
                attribute_norm,
                &row.kind,
                &row.op,
                &row.prev_kind,
                pv,
                ps,
                &pc,
                pp,
                &row.new_value,
                &row.action,
                &row.src_entry_id,
                &at,
                &row.chronicle_event_id,
            ],
        )
        .await
    })
    .await
    .map(drop)
    .map_err(internal)
}

/// `add_chronicle_event` (`storage/postgres.py:3315-3344`): `(id, inserted)`.
async fn add_chronicle_event(
    ready: &Ready,
    occurred_at: Option<&str>,
    phrase: Option<&str>,
    actor: &str,
    description: &str,
    episode: Option<&str>,
    src_entry_id: Option<i64>,
) -> Result<(i64, bool), ToolError> {
    encodable(&[
        actor,
        description,
        phrase.unwrap_or(""),
        occurred_at.unwrap_or(""),
    ])?;
    let _ws = ready.write.lock().await;
    let actor_norm = norm_key(actor);
    let description_norm = norm_value(description);
    let recorded = now();
    let (actor_norm, description_norm) = (&actor_norm, &description_norm);
    crate::txn::run(ready.storage.client(), |c| async move {
        if let Some(r) = c
            .query_opt(
                "SELECT id FROM chronicle_events WHERE actor_norm = $1 AND description_norm = $2 \
                 AND occurred_at IS NOT DISTINCT FROM $3::text::timestamptz \
                 AND invalidated_at IS NULL ORDER BY id LIMIT 1",
                &[actor_norm, description_norm, &occurred_at],
            )
            .await?
        {
            return Ok((r.get::<_, i64>(0), false));
        }
        let r = c
            .query_one(
                "INSERT INTO chronicle_events (occurred_at, occurred_phrase, recorded_at, actor, \
                 actor_norm, description, description_norm, episode, src_entry_id, hlc_phys, \
                 hlc_logical, writer_id) VALUES ($1::text::timestamptz, $2, $3, $4, $5, $6, $7, \
                 $8, $9, NULL, NULL, NULL) RETURNING id",
                &[
                    &occurred_at,
                    &phrase,
                    &recorded,
                    &actor,
                    actor_norm,
                    &description,
                    description_norm,
                    &episode,
                    &src_entry_id,
                ],
            )
            .await?;
        Ok::<_, tokio_postgres::Error>((r.get::<_, i64>(0), true))
    })
    .await
    .map_err(internal)
}

/// `has_trace` (`storage/postgres.py:3150-3160`).
async fn has_trace(ready: &Ready, en: &str, an: &str, entry: i64) -> Result<bool, ToolError> {
    encodable(&[en, an])?;
    let _ws = ready.write.lock().await;
    crate::txn::with_client(ready.storage.client(), |c| async move {
        c.query_opt(
            "SELECT 1 FROM memory_traces WHERE entity_norm=$1 AND attribute_norm=$2 \
             AND entry_id=$3",
            &[&en, &an, &entry],
        )
        .await
        .map(|r| r.is_some())
    })
    .await
    .map_err(internal)
}

/// `add_trace` (3162-3196), then `bump_reinforcements(entry, 1)` and the
/// resident `bump_entry_reinforcements` when the trace is new.
async fn add_trace(ready: &Ready, en: &str, an: &str, entry: i64) -> Result<bool, ToolError> {
    encodable(&[en, an])?;
    let _ws = ready.write.lock().await;
    let db = ready.storage.client();
    let t = now();
    let inserted = crate::txn::run(db, |c| async move {
        let source = c
            .query_opt(
                "SELECT superseded_at FROM entries WHERE id = $1 FOR UPDATE",
                &[&entry],
            )
            .await?;
        let row = c
            .query_opt(
                "INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) \
                 VALUES ($1, $2, $3, $4) \
                 ON CONFLICT (entity_norm, attribute_norm, entry_id) DO NOTHING \
                 RETURNING entry_id",
                &[&en, &an, &entry, &t],
            )
            .await?;
        if let Some(s) = source
            && let Some(at) = s.get::<_, Option<f64>>(0)
        {
            c.execute(
                "INSERT INTO memory_trace_invalidations (entity_norm, attribute_norm, \
                 source_entry_id, invalidated_at, cause) \
                 VALUES ($1, $2, $3, $4, 'source_superseded') \
                 ON CONFLICT (entity_norm, attribute_norm, source_entry_id) DO NOTHING",
                &[&en, &an, &entry, &at],
            )
            .await?;
        }
        Ok::<_, tokio_postgres::Error>(row.is_some())
    })
    .await
    .map_err(internal)?;
    if inserted {
        crate::txn::run(db, |c| async move {
            c.execute(
                "UPDATE entries SET reinforcements = reinforcements + $1 WHERE id = $2",
                &[&1i32, &entry],
            )
            .await
        })
        .await
        .map_err(internal)?;
        let mut bank = ready.bank.write().expect("bank lock");
        if let Some(e) = bank.entries.iter_mut().find(|e| e.id == entry) {
            e.reinforcements += 1;
        }
    }
    Ok(inserted)
}

/// `_dream_reflush_stale` (`service_dream.py:3011-3033`): the pulled ids
/// whose rows are gone. Re-inserting them (`cms.reflush_entries`) is not
/// ported: the hold is the same, only the healing is not (declared).
async fn reflush_stale(ready: &Ready, ids: &[i64]) -> usize {
    if ids.is_empty() {
        return 0;
    }
    let _ws = ready.write.lock().await;
    let found = crate::txn::with_client(ready.storage.client(), |c| async move {
        c.query("SELECT id FROM entries WHERE id = ANY($1)", &[&ids])
            .await
            .map(|rows| {
                rows.iter()
                    .map(|r| r.get::<_, i64>(0))
                    .collect::<std::collections::HashSet<i64>>()
            })
    })
    .await;
    match found {
        Ok(found) => {
            let missing = ids.iter().filter(|i| !found.contains(i)).count();
            if missing > 0 {
                eprintln!(
                    "dream: {missing} pulled entry id(s) are gone; re-flushing them is not ported"
                );
            }
            0
        }
        Err(e) => {
            eprintln!("dream stale-id heal failed ({e})");
            0
        }
    }
}

// ---------------------------------------------------------------------------
// The pass
// ---------------------------------------------------------------------------

/// One pulled entry, as `dream_pull` reports it.
pub(super) struct PEntry {
    pub text: String,
    pub db_id: Option<i64>,
    pub source: Option<String>,
    pub episode_id: Option<String>,
    pub authority: Option<String>,
    pub distortion: Option<String>,
}

fn opt_str(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).map(str::to_string)
}

fn parse_entries(pulled: &Value) -> Vec<PEntry> {
    pulled["entries"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| PEntry {
            text: opt_str(e, "text").unwrap_or_default(),
            db_id: e.get("db_id").and_then(Value::as_i64),
            source: opt_str(e, "source"),
            episode_id: opt_str(e, "episode_id"),
            authority: opt_str(e, "authority"),
            distortion: opt_str(e, "distortion_tolerance"),
        })
        .collect()
}

/// One claim (or event) with its attribution: the source entry's id and
/// its index in the batch.
struct Pair {
    item: Extracted,
    src_id: Option<i64>,
    src: Option<usize>,
}

/// The pass's counters, outside the action tally as in Python.
#[derive(Default)]
struct Counts {
    tally: Map<String, Value>,
    traces: i64,
    quarantined: usize,
    literal_flagged: i64,
    literal_dropped: i64,
    span_flagged: i64,
    span_parked: i64,
    qt_parked: i64,
    qt_held: i64,
    qt_promoted: i64,
    events_inserted: i64,
    events_duplicate: i64,
    events_pass_failed: bool,
    carried: BTreeSet<usize>,
    run_seq: i32,
}

impl Counts {
    fn new() -> Self {
        let mut c = Counts::default();
        for k in ["inserted", "confirmed", "contested", "superseded"] {
            c.tally.insert(k.into(), json!(0));
        }
        c
    }

    fn bump(&mut self, action: &str) {
        let n = self.tally.get(action).and_then(Value::as_i64).unwrap_or(0) + 1;
        self.tally.insert(action.into(), json!(n));
    }

    fn claims(&self) -> i64 {
        self.tally.values().filter_map(Value::as_i64).sum()
    }
}

/// `classify_extractor_error` for a failure.
fn classify(e: &ExtractFailure) -> (String, String) {
    let (why, error) = super::extractor::classify_failure(e);
    (why.to_string(), error)
}

struct Pass<'a> {
    svc: &'a Arc<Service>,
    ready: &'a Arc<Ready>,
    cfg: &'a DreamConfig,
    caller: &'a Caller,
    writer: &'a str,
    entries: Vec<PEntry>,
    batch_text: String,
    batch_has_date: bool,
    run_id: Option<i64>,
    c: Counts,
    constraint_idx: Vec<usize>,
}

impl Pass<'_> {
    /// `_held(reason, exc, phase=...)`.
    fn held(&self, phase: &str, error: Option<(String, String)>) -> Map<String, Value> {
        let c = &self.c;
        let mut m = Map::new();
        m.insert("pulled".into(), json!(self.entries.len()));
        for k in [
            "claims",
            "inserted",
            "confirmed",
            "contested",
            "superseded",
            "relations",
        ] {
            m.insert(k.into(), json!(0));
        }
        m.insert(
            "cursor".into(),
            json!(self.ready.cortex.snapshot().dream_cursor),
        );
        m.insert("extractor_failed".into(), json!(true));
        m.insert("hold_phase".into(), json!(phase));
        if phase == "extract"
            && let Some((why, error)) = error
        {
            m.insert(
                "extractor_error".into(),
                json!({"reason": why, "error": error}),
            );
        }
        m.insert("literal_flagged".into(), json!(c.literal_flagged));
        m.insert("literal_dropped".into(), json!(c.literal_dropped));
        m.insert("span_flagged".into(), json!(c.span_flagged));
        m.insert("span_parked".into(), json!(c.span_parked));
        m.insert("quarantine_parked".into(), json!(c.qt_parked));
        m.insert("quarantine_held".into(), json!(c.qt_held));
        m.insert("quarantine_promoted".into(), json!(c.qt_promoted));
        m.insert("events_inserted".into(), json!(c.events_inserted));
        m.insert("events_duplicate".into(), json!(c.events_duplicate));
        m.insert("events_pass_failed".into(), json!(false));
        m.insert("constraint_verbatim".into(), json!(c.carried.len()));
        m.insert("constraint_misses".into(), json!([]));
        m.insert("lessons".into(), json!({"signals": 0, "lessons": 0}));
        m
    }

    /// `_finish_run(status, cursor_after)`: bookkeeping never breaks a pass.
    async fn finish_run(&self, status: &str, cursor_after: Option<f64>) {
        let Some(id) = self.run_id else { return };
        let c = &self.c;
        let mut tallies = c.tally.clone();
        for (k, v) in [
            ("literal_flagged", json!(c.literal_flagged)),
            ("literal_dropped", json!(c.literal_dropped)),
            ("span_flagged", json!(c.span_flagged)),
            ("span_parked", json!(c.span_parked)),
            ("events_inserted", json!(c.events_inserted)),
            ("events_duplicate", json!(c.events_duplicate)),
            ("events_pass_failed", json!(c.events_pass_failed)),
            ("quarantine_parked", json!(c.qt_parked)),
            ("quarantine_held", json!(c.qt_held)),
            ("quarantine_promoted", json!(c.qt_promoted)),
            ("constraint_verbatim", json!(c.carried.len())),
            (
                "constraint_missed",
                json!(self.constraint_idx.len() - c.carried.len()),
            ),
            ("quarantined", json!(c.quarantined)),
        ] {
            tallies.insert(k.into(), v);
        }
        let claims = i32::try_from(c.claims()).unwrap_or(i32::MAX);
        if let Err(e) = finish_run_row(
            self.ready,
            id,
            status,
            cursor_after,
            claims,
            Value::Object(tallies),
        )
        .await
        {
            eprintln!("dream: run-row finish failed ({e:?})");
        }
    }

    fn src_text(&self, src_id: i64) -> &str {
        self.entries
            .iter()
            .find(|e| e.db_id == Some(src_id))
            .map_or("", |e| e.text.as_str())
    }

    fn gate_corpus(&self, src_id: i64) -> &str {
        if self.cfg.literal_gate_scope == "batch" {
            &self.batch_text
        } else {
            self.src_text(src_id)
        }
    }

    fn note_index(&self, source: Option<usize>) -> Option<usize> {
        source.filter(|&i| i < self.entries.len())
    }

    /// `_write_event` (`service_dream.py:1830-1888`).
    async fn write_event(&mut self, ev: &Event, src_id: Option<i64>) -> Result<(), ToolError> {
        let desc = py_strip(&ev.description).to_string();
        if desc.is_empty() {
            return Ok(());
        }
        if self.cfg.literal_gate != "off"
            && let Some(sid) = src_id
        {
            let bad = gates::literal_violations(&desc, self.gate_corpus(sid));
            if !bad.is_empty() {
                self.c.literal_flagged += 1;
                if self.cfg.literal_gate == "enforce" {
                    self.c.literal_dropped += 1;
                    return Ok(());
                }
            }
        }
        let actor = if ev.actor.is_empty() {
            "user".to_string()
        } else {
            ev.actor.clone()
        };
        let episode = self
            .note_index(ev.source)
            .and_then(|i| self.entries[i].episode_id.clone());
        let occurred = if self.batch_has_date {
            ev.date.clone()
        } else {
            None
        };
        let (ev_id, inserted) = add_chronicle_event(
            self.ready,
            occurred.as_deref(),
            ev.date_phrase.as_deref(),
            &actor,
            &desc,
            episode.as_deref(),
            src_id,
        )
        .await?;
        if !inserted {
            self.c.events_duplicate += 1;
            return Ok(());
        }
        self.c.events_inserted += 1;
        if let Some(run_id) = self.run_id {
            let row = SlotRow {
                seq: self.c.run_seq,
                entity: actor.clone(),
                attribute: "event".into(),
                kind: "event",
                op: None,
                prev_kind: None,
                prev: None,
                new_value: desc,
                action: "event_inserted".into(),
                src_entry_id: src_id,
                chronicle_event_id: Some(ev_id),
                attribute_norm: Some("event".into()),
            };
            add_run_slot(self.ready, run_id, row).await?;
            self.c.run_seq += 1;
        }
        Ok(())
    }

    /// `_source_class`: the source's distortion class, except that
    /// `constraint` rides only on the claim carrying the text verbatim.
    fn source_class(&self, value: &str, src: Option<usize>) -> LabelArg {
        let Some(e) = src.map(|i| &self.entries[i]) else {
            return LabelArg::Inherit;
        };
        match e.distortion.as_deref() {
            None | Some("") => LabelArg::Inherit,
            Some("constraint") if !gates::contains_verbatim(value, &e.text) => LabelArg::Inherit,
            Some(label) => LabelArg::Requested(Some(label.to_string())),
        }
    }

    /// `_mark_carried`.
    fn mark_carried(&mut self, claim: &Claim, src: Option<usize>) {
        let Some(i) = src else { return };
        if matches!(claim.op.as_deref(), Some("add" | "remove")) {
            return;
        }
        if self.constraint_idx.contains(&i)
            && gates::contains_verbatim(&claim.value, &self.entries[i].text)
        {
            self.c.carried.insert(i);
        }
    }

    fn trust(&self, src: Option<usize>) -> Option<gates::TrustEntry<'_>> {
        src.map(|i| {
            let e = &self.entries[i];
            gates::TrustEntry {
                source: e.source.as_deref(),
                episode_id: e.episode_id.as_deref(),
                authority: e.authority.as_deref(),
            }
        })
    }

    /// `_quarantine_route` (`service_dream.py:2477-2568`).
    fn quarantine_route(
        &self,
        ent: &str,
        attr: &str,
        value: &str,
        tier_entry: Option<usize>,
        claim_tier: &str,
    ) -> (Option<&'static str>, String, bool) {
        let trust = self.trust(tier_entry);
        let low = gates::quarantine_low_trust(trust.as_ref(), &self.cfg.trusted_sources);
        let witness = gates::quarantine_witness(trust.as_ref());
        let snap = self.ready.cortex.snapshot();
        let cur = snap.lookup(ent, attr);
        let cur_value = cur.map(|r| r.value.clone());
        let cur_origin = cur.map(|r| r.origin());
        let cur_direct = snap.protect_provenance && direct_backed(cur);
        let want = norm_value(value);
        let found = snap
            .contenders_for(ent, attr)
            .into_iter()
            .find(|r| norm_value(&r.value) == want);
        let marked =
            found.is_some_and(|r| r.provenance.iter().any(|p| p == "quarantine:low_trust"));
        let witnesses: Vec<String> = found.map(|r| r.provenance.clone()).unwrap_or_default();
        let matched = found.is_some();
        drop(snap);
        if let Some(cv) = &cur_value
            && (norm_value(cv) == want || is_compression_echo(value, cv))
        {
            return (None, witness, false);
        }
        if marked {
            let independent = !low || !witnesses.contains(&witness);
            let tier_ok = cur_origin
                .is_none_or(|o| gates::tier_rank(claim_tier) >= gates::tier_rank(o))
                && !cur_direct;
            let route = if independent && tier_ok {
                "promote"
            } else {
                "hold"
            };
            return (Some(route), witness, low);
        }
        if matched {
            return (low.then_some("hold_ordinary"), witness, low);
        }
        if low {
            return (Some("park"), witness, low);
        }
        (None, witness, low)
    }

    /// The claim-derived arguments every scalar write shares
    /// (`claim_kwargs`, `service_dream.py:2097-2117`).
    fn claim_args(
        &self,
        ent: &str,
        attr: &str,
        claim: &Claim,
        src: Option<usize>,
        origin: &str,
    ) -> CortexWriteArgs {
        let mut a = CortexWriteArgs::new(ent, attr, &claim.value);
        a.confidence = claim.confidence;
        a.support = Some(origin.to_string());
        a.stance = claim.stance.clone();
        a.authority = match src
            .and_then(|i| self.entries[i].authority.clone())
            .filter(|s| !s.is_empty())
        {
            Some(label) => LabelArg::Requested(Some(label)),
            None => LabelArg::Inherit,
        };
        a.distortion_tolerance = self.source_class(&claim.value, src);
        a.dream = true;
        a
    }

    async fn write(&self, a: CortexWriteArgs) -> Result<Value, ToolError> {
        // `cortex_write` embeds the claim and slot texts first, so a lone
        // surrogate there fails before anything is written. A stance is
        // never embedded: Python writes the resident record, mints the
        // subject entity and fails at the save (declared); this fails first.
        encodable(&[
            &a.entity,
            &a.attribute,
            &a.value,
            a.stance.as_deref().unwrap_or(""),
        ])?;
        crate::fact_set::cortex_write(
            self.svc,
            self.ready,
            self.caller,
            self.writer.to_string(),
            a,
        )
        .await
    }

    /// The constraint carrier's eligibility (`_carrier_slot_eligible`).
    async fn carrier_slot_eligible(&self, entity: &str, attribute: &str) -> bool {
        let (ent, attr) = resolve_dream_slot(
            self.ready,
            self.svc.config.memory.cortex.dream_slot_match_threshold,
            entity,
            attribute,
        )
        .await;
        let snap = self.ready.cortex.snapshot();
        match snap.lookup(&ent, &attr) {
            None => snap.members(&ent, &attr).is_empty(),
            Some(cur) => cur.distortion_tolerance.as_deref() == Some("constraint"),
        }
    }

    /// `_apply_constraint_carrier` (`service_dream.py:2411-2462`).
    async fn apply_constraint_carrier(&self, pairs: &mut [Pair]) {
        for i in 0..self.entries.len() {
            if self.entries[i].distortion.as_deref() != Some("constraint") {
                continue;
            }
            let text = self.entries[i].text.clone();
            let scalars: Vec<usize> = pairs
                .iter()
                .enumerate()
                .filter(|(_, p)| p.src == Some(i))
                .filter_map(|(k, p)| match &p.item {
                    Extracted::Claim(c) if !matches!(c.op.as_deref(), Some("add" | "remove")) => {
                        Some(k)
                    }
                    _ => None,
                })
                .collect();
            let claim = |k: usize| match &pairs[k].item {
                Extracted::Claim(c) => c.clone(),
                Extracted::Event(_) => unreachable!("scalars are claims"),
            };
            if scalars
                .iter()
                .any(|&k| gates::contains_verbatim(&claim(k).value, &text))
            {
                continue;
            }
            let rule = gates::content_tokens(&text);
            let mut best: Option<usize> = None;
            let mut best_overlap = 0;
            for &k in &scalars {
                let c = claim(k);
                let overlap = rule
                    .intersection(&gates::content_tokens(&format!(
                        "{} {}",
                        c.attribute, c.value
                    )))
                    .count();
                if overlap <= best_overlap {
                    continue;
                }
                if !self.carrier_slot_eligible(&c.entity, &c.attribute).await {
                    continue;
                }
                best = Some(k);
                best_overlap = overlap;
            }
            match best {
                None => eprintln!(
                    "dream: constraint entry {} has no eligible carrier claim; left for the guard",
                    self.entries[i]
                        .db_id
                        .map_or(i.to_string(), |id| id.to_string())
                ),
                Some(k) => {
                    if let Extracted::Claim(c) = &mut pairs[k].item {
                        c.value = text;
                    }
                }
            }
        }
    }

    /// One claim of the write loop (`service_dream.py:1892-2270`).
    async fn claim(
        &mut self,
        claim: &Claim,
        src_id: Option<i64>,
        src: Option<usize>,
        known: &std::collections::HashSet<String>,
        new_entities: &mut Vec<(String, String)>,
    ) -> Result<(), ToolError> {
        let cfg = self.cfg;
        // The write tier: the backing note's source tier, when the cited
        // note states the value and names the slot.
        let mut tier_entry = src;
        if let Some(i) = src {
            let e = &self.entries[i];
            let tier = gates::origin_from_source(e.source.as_deref());
            let checked = !crate::mutants::active("mcp-dream-run-citation-unchecked");
            if tier.is_some_and(|t| t != "agent")
                && checked
                && (claim.value_coerced
                    || !gates::citation_backs(
                        &claim.value,
                        &e.text,
                        &claim.entity,
                        &claim.attribute,
                        claim.op.as_deref() != Some("remove"),
                    ))
            {
                tier_entry = None;
            }
        }
        let mut origin = tier_entry
            .and_then(|i| gates::origin_from_source(self.entries[i].source.as_deref()))
            .unwrap_or("agent")
            .to_string();
        let mut policy = cfg.assistant_claims.as_str();
        if !matches!(policy, "contender" | "supersede" | "drop") {
            eprintln!("dream: unknown memory.dream.assistant_claims {policy:?}; using 'contender'");
            policy = "contender";
        }
        if claim.speaker.as_deref() == Some("assistant") {
            if policy == "drop" {
                return Ok(());
            }
            if policy == "contender" {
                origin = "assistant".into();
            }
        }
        let (ent, attr) = resolve_dream_slot(
            self.ready,
            self.svc.config.memory.cortex.dream_slot_match_threshold,
            &claim.entity,
            &claim.attribute,
        )
        .await;
        let op = claim.op.clone().filter(|o| o == "add" || o == "remove");
        let ent_norm = norm_key(&ent);
        if !known.contains(&ent_norm) && !new_entities.iter().any(|(k, _)| *k == ent_norm) {
            new_entities.push((ent_norm.clone(), ent.clone()));
        }
        // The literal gate: uncited claims abstain.
        if cfg.literal_gate != "off"
            && let Some(sid) = src_id
            && !crate::mutants::active("mcp-dream-run-literal-gate-off")
        {
            let bad = gates::literal_violations(&claim.value, self.gate_corpus(sid));
            if !bad.is_empty() {
                self.c.literal_flagged += 1;
                if cfg.literal_gate == "enforce" {
                    self.c.literal_dropped += 1;
                    return Ok(());
                }
            }
        }
        let mut span_reason = None;
        if cfg.span_gate != "off" {
            let corpus = src.map_or("", |i| self.entries[i].text.as_str());
            span_reason = gates::span_unbacked(claim.quote.as_deref(), corpus);
            if span_reason.is_some() {
                self.c.span_flagged += 1;
            }
        }
        let attr_norm = norm_key(&attr);
        if op.is_none()
            && self.svc.config.memory.traces_enabled
            && let Some(sid) = src_id
            && has_trace(self.ready, &ent_norm, &attr_norm, sid).await?
        {
            self.mark_carried(claim, src);
            return Ok(());
        }
        // Pre-image capture (fields, not the record).
        let mut prev_kind = None;
        let mut prev = None;
        if self.run_id.is_some() {
            let snap = self.ready.cortex.snapshot();
            let cur = snap.lookup(&ent, &attr);
            let members = snap.members(&ent, &attr);
            if cur.is_some() {
                prev_kind = Some("scalar");
            } else if !members.is_empty() {
                prev_kind = Some("set");
            }
            let prev_rec = if op.is_none() {
                cur
            } else {
                let want = norm_value(&claim.value);
                members
                    .iter()
                    .copied()
                    .find(|m| norm_value(&m.value) == want)
                    .or(if op.as_deref() == Some("add") {
                        cur
                    } else {
                        None
                    })
            };
            prev = prev_rec.map(|r| {
                let mut best: Option<&String> = None;
                for s in &r.support {
                    if best.is_none_or(|b| gates::tier_rank(s) > gates::tier_rank(b)) {
                        best = Some(s);
                    }
                }
                (
                    r.value.clone(),
                    r.status.clone(),
                    r.confidence,
                    best.cloned(),
                )
            });
        }
        let res: Value = match op.as_deref() {
            Some("add") => {
                // `set_add` embeds "{entity} {attribute} {member}" first.
                encodable(&[&ent, &attr, &claim.value])?;
                let mut a = crate::set_members::SetAddArgs::new(&ent, &attr, &claim.value);
                a.confidence = claim.confidence;
                a.origin = Some(origin.clone());
                a.dream = true;
                crate::set_members::set_add(
                    self.svc,
                    self.ready,
                    self.caller,
                    self.writer.to_string(),
                    a,
                )
                .await?
            }
            Some(_) => {
                let mut a = crate::set_members::SetRemoveArgs::new(&ent, &attr, &claim.value);
                a.origin = Some(origin.clone());
                a.dream = true;
                crate::set_members::set_remove(self.svc, self.ready, a).await?
            }
            None => {
                let (route, witness, low) = if cfg.quarantine_low_trust {
                    self.quarantine_route(&ent, &attr, &claim.value, tier_entry, &origin)
                } else {
                    (None, String::new(), false)
                };
                let args = || self.claim_args(&ent, &attr, claim, src, &origin);
                let marked = |low: bool, witness: &str| {
                    if low {
                        vec!["quarantine:low_trust".to_string(), witness.to_string()]
                    } else {
                        vec![witness.to_string()]
                    }
                };
                let written = match route {
                    Some("promote") => {
                        let mut r = crate::fact_resolve::ResolveArgs::new(&ent, &attr, true);
                        r.support = origin.clone();
                        r.dream = true;
                        r.expect_value = Some(claim.value.clone());
                        let rr =
                            crate::fact_resolve::cortex_resolve(self.svc, self.ready, r).await?;
                        if rr.get("resolved").and_then(Value::as_bool) == Some(true) {
                            self.c.qt_promoted += 1;
                            Ok(json!({"action": "quarantine_promoted"}))
                        } else {
                            let mut a = args();
                            a.provenance = marked(low, &witness);
                            a.force_contend = true;
                            let out = self.write(a).await;
                            if let Ok(v) = &out
                                && v["action"] == "contested"
                            {
                                self.c.qt_parked += 1;
                            }
                            out
                        }
                    }
                    Some(r @ ("park" | "hold" | "hold_ordinary")) => {
                        let mut a = args();
                        a.provenance = if r == "hold_ordinary" {
                            vec![witness.clone()]
                        } else {
                            vec!["quarantine:low_trust".to_string(), witness.clone()]
                        };
                        a.force_contend = true;
                        let out = self.write(a).await;
                        if let Ok(v) = &out
                            && v["action"] == "contested"
                        {
                            if r == "park" {
                                self.c.qt_parked += 1;
                            } else {
                                self.c.qt_held += 1;
                            }
                        }
                        out
                    }
                    _ if cfg.span_gate == "contend" && span_reason.is_some() => {
                        let mut a = args();
                        a.provenance = vec!["span:unbacked".into()];
                        a.force_contend = true;
                        let out = self.write(a).await;
                        if let Ok(v) = &out
                            && v["action"] == "contested"
                        {
                            self.c.span_parked += 1;
                        }
                        out
                    }
                    _ => self.write(args()).await,
                };
                match written {
                    Ok(v) => v,
                    Err(ToolError::Invalid(m)) if m.contains("holds a set") => {
                        self.c.bump("dropped_set_slot");
                        return Ok(());
                    }
                    Err(e) => return Err(e),
                }
            }
        };
        let action = res["action"].as_str().unwrap_or("").to_string();
        self.c.bump(&action);
        if !NOTHING_STORED.contains(&action.as_str()) {
            self.mark_carried(claim, src);
        }
        if let Some(run_id) = self.run_id
            && !NOTHING_STORED.contains(&action.as_str())
            && !crate::mutants::active("mcp-dream-run-no-journal")
        {
            let row = SlotRow {
                seq: self.c.run_seq,
                entity: ent.clone(),
                attribute: attr.clone(),
                kind: if op.is_some() { "member" } else { "scalar" },
                op: op.clone(),
                prev_kind,
                prev,
                new_value: claim.value.clone(),
                action: action.clone(),
                src_entry_id: src_id,
                chronicle_event_id: None,
                attribute_norm: None,
            };
            add_run_slot(self.ready, run_id, row).await?;
            self.c.run_seq += 1;
        }
        let skip = if crate::mutants::active("mcp-dream-run-trace-contested") {
            matches!(action.as_str(), "member_invalid" | "member_capped")
        } else {
            matches!(
                action.as_str(),
                "member_invalid" | "member_capped" | "contested"
            )
        };
        if skip {
            return Ok(());
        }
        if self.svc.config.memory.traces_enabled
            && let Some(sid) = src_id
            && add_trace(self.ready, &ent_norm, &attr_norm, sid).await?
        {
            self.c.traces += 1;
        }
        Ok(())
    }
}

fn extractor_label(x: &Extractor) -> String {
    match x {
        Extractor::NoOp => "NoOpExtractor".into(),
        Extractor::Compat(c) if c.model.is_empty() => "OpenAICompatExtractor".into(),
        Extractor::Compat(c) => c.model.clone(),
    }
}

/// `dream_run_auto` (`service_dream.py:2922-2951`).
pub async fn run_auto(
    svc: &Arc<Service>,
    ready: Arc<Ready>,
    caller: Caller,
    writer: String,
    limit: Option<i64>,
) -> Result<Value, ToolError> {
    let cfg = &svc.config.dream;
    let (extractor, which) = match super::extractor::build_extractor_with_fallback(cfg).await {
        Ok(x) => x,
        Err(e) => return Ok(json!({"error": e, "pulled": 0, "claims": 0})),
    };
    let base_url = extractor.as_compat().map(|c| c.base_url.clone());
    *ready
        .dream_run
        .last_dream_extractor
        .lock()
        .expect("extractor record") =
        Some(json!({"which": which, "base_url": base_url, "at": now()}));
    let mut result = dream_run(svc, &ready, &caller, &writer, &extractor, limit).await?;
    result.insert("extractor".into(), json!(which));
    let forced = super::extractor::resolve_endpoints(cfg).mode == "fallback";
    let due = {
        let mut stall = ready.dream_run.stall.lock().expect("stall tracker");
        stall.record(&result, which == "fallback" && !forced);
        stall.notice_due()
    };
    if due && cfg.stall_notice && svc.config.coordination.enabled {
        eprintln!("dream stall notice owed; the board notice is not ported");
    }
    Ok(Value::Object(result))
}

/// `dream_run` (`service_dream.py:1491-1508`): single-flight.
async fn dream_run(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: &str,
    extractor: &Extractor,
    limit: Option<i64>,
) -> Result<Map<String, Value>, ToolError> {
    if ready
        .dream_run
        .guard
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        let mut m = Map::new();
        m.insert("skipped".into(), json!("dream_in_progress"));
        m.insert("pulled".into(), json!(0));
        m.insert("claims".into(), json!(0));
        return Ok(m);
    }
    let _flight = Flight(&ready.dream_run.guard);
    run_locked(svc, ready, caller, writer, extractor, limit).await
}

/// `_dream_run_locked` (`service_dream.py:1510-2394`).
async fn run_locked(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: &str,
    extractor: &Extractor,
    limit: Option<i64>,
) -> Result<Map<String, Value>, ToolError> {
    let config = &svc.config;
    let cfg = &config.dream;
    let cap = limit.unwrap_or(cfg.max_batch);
    let pulled = {
        let _ws = ready.write.lock().await;
        super::service::pull(ready, cfg, cap).await?
    };
    if let Some(error) = pulled.get("error") {
        let mut m = Map::new();
        m.insert("pulled".into(), json!(0));
        m.insert("claims".into(), json!(0));
        m.insert("error".into(), error.clone());
        m.insert(
            "detail".into(),
            pulled.get("detail").cloned().unwrap_or(json!("")),
        );
        return Ok(m);
    }
    let entries = parse_entries(&pulled);
    if entries.is_empty() {
        let retyped = super::relations::retype_quarantined_links(
            svc,
            ready,
            extractor,
            cfg.retype_quarantined_max,
        )
        .await?;
        let mut m = Map::new();
        for k in [
            "pulled",
            "claims",
            "inserted",
            "confirmed",
            "contested",
            "superseded",
            "relations",
            "literal_flagged",
            "literal_dropped",
            "quarantine_parked",
            "quarantine_held",
            "quarantine_promoted",
            "events_inserted",
            "events_duplicate",
        ] {
            m.insert(k.into(), json!(0));
        }
        m.insert("events_pass_failed".into(), json!(false));
        m.insert("constraint_verbatim".into(), json!(0));
        m.insert("constraint_misses".into(), json!([]));
        m.insert("cursor".into(), pulled["cursor"].clone());
        m.insert("lessons".into(), lessons_disabled());
        m.insert("outcome_inference".into(), outcome_inference_disabled());
        m.insert("digests".into(), digests_disabled());
        m.insert("graph_insight".into(), graph_insight_disabled());
        m.insert("retyped".into(), retyped);
        return Ok(m);
    }
    let kf_n = usize::try_from(cfg.known_facts_window.max(0)).unwrap_or(0);
    let texts: Vec<String> = entries.iter().map(|e| e.text.clone()).collect();
    let batch_text = texts.join("\n");
    let batch_has_date = gates::date_like_re().is_match(&batch_text);
    let batch_key: Vec<i64> = entries.iter().map(|e| e.db_id.unwrap_or(-1)).collect();
    let mut pass = Pass {
        svc,
        ready,
        cfg,
        caller,
        writer,
        entries,
        batch_text,
        batch_has_date,
        run_id: None,
        c: Counts::new(),
        constraint_idx: Vec::new(),
    };
    let (vocab, known_facts) = hints(ready, &texts, kf_n).await;
    let mut isolation_error: Option<(String, String)> = None;
    let mut pairs: Vec<Pair> = Vec::new();
    let kf = |k: &Vec<Fact>| (!k.is_empty()).then(|| k.clone());
    match extractor
        .extract(&texts, &vocab, kf(&known_facts).as_deref())
        .await
    {
        Ok(mut extracted) => {
            for item in &mut extracted {
                if let Extracted::Claim(c) = item
                    && let Some((e, a)) = gates::unflatten_slot_key(&c.entity, &c.attribute, &vocab)
                {
                    c.entity = e;
                    c.attribute = a;
                }
            }
            let n = pass.entries.len();
            for item in extracted {
                let source = match &item {
                    Extracted::Claim(c) => c.source,
                    Extracted::Event(e) => e.source,
                };
                let src = match source.filter(|&i| i < n) {
                    Some(i) => Some(i),
                    None if n == 1 => Some(0),
                    None => None,
                };
                let src_id = src.and_then(|i| pass.entries[i].db_id);
                pairs.push(Pair { item, src_id, src });
            }
            ready
                .dream_run
                .batch_failures
                .lock()
                .expect("batch failures")
                .remove(&batch_key);
        }
        Err(failure) => {
            let fails = {
                let mut map = ready
                    .dream_run
                    .batch_failures
                    .lock()
                    .expect("batch failures");
                let n = map.entry(batch_key.clone()).or_insert(0);
                *n += 1;
                *n
            };
            if fails < MAX_BATCH_FAILURES {
                eprintln!(
                    "dream extractor failed ({fails}/{MAX_BATCH_FAILURES} for this batch); cursor NOT advanced"
                );
                return Ok(pass.held("extract", Some(classify(&failure))));
            }
            let mut succeeded = 0;
            let mut failed = 0;
            let mut last = failure.clone();
            for (i, key) in batch_key.iter().enumerate() {
                let one = vec![pass.entries[i].text.clone()];
                let (e_vocab, e_kf) = hints(ready, &one, kf_n).await;
                match extractor
                    .extract(&one, &e_vocab, kf(&e_kf).as_deref())
                    .await
                {
                    Ok(claims) => {
                        succeeded += 1;
                        let src_id = pass.entries[i].db_id;
                        pairs.extend(claims.into_iter().map(|item| Pair {
                            item,
                            src_id,
                            src: Some(i),
                        }));
                    }
                    Err(e2) => {
                        eprintln!("dream: entry {key} failed isolated extraction");
                        failed += 1;
                        last = e2;
                    }
                }
            }
            if succeeded == 0 && pass.entries.len() > 1 {
                return Ok(pass.held("extract", Some(classify(&failure))));
            }
            if succeeded == 0 {
                isolation_error = Some(classify(&last));
            }
            pass.c.quarantined = failed;
            ready
                .dream_run
                .batch_failures
                .lock()
                .expect("batch failures")
                .remove(&batch_key);
        }
    }

    pass.constraint_idx = (0..pass.entries.len())
        .filter(|&i| pass.entries[i].distortion.as_deref() == Some("constraint"))
        .collect();
    if !pass.constraint_idx.is_empty() {
        pass.apply_constraint_carrier(&mut pairs).await;
    }
    let known: std::collections::HashSet<String> = ready
        .cortex
        .snapshot()
        .current_records()
        .map(|r| r.key().0.clone())
        .collect();
    let mut new_entities: Vec<(String, String)> = Vec::new();
    let chronicle_on = cfg.chronicle;
    let actionable = pairs
        .iter()
        .any(|p| !matches!(p.item, Extracted::Event(_)) || chronicle_on);
    if !pairs.is_empty() && actionable {
        let cursor_before = ready.cortex.snapshot().dream_cursor;
        let pulled_n = i32::try_from(pass.entries.len()).unwrap_or(i32::MAX);
        pass.run_id =
            Some(start_run(ready, cursor_before, pulled_n, &extractor_label(extractor)).await?);
    }

    let body: Result<Vec<Value>, ToolError> = async {
        for p in &pairs {
            match &p.item {
                Extracted::Event(ev) => {
                    if chronicle_on {
                        pass.write_event(ev, p.src_id).await?;
                    }
                }
                Extracted::Claim(c) => {
                    pass.claim(c, p.src_id, p.src, &known, &mut new_entities)
                        .await?;
                }
            }
        }
        if chronicle_on && let Some(compat) = extractor.as_compat() {
            let items = match compat.extract_events(&texts).await {
                Ok(items) => items,
                Err(e) => {
                    eprintln!("dream: events pass failed ({e:?}); events skipped this cycle");
                    pass.c.events_pass_failed = true;
                    Vec::new()
                }
            };
            let n = pass.entries.len();
            for ev in items {
                let ev_src = match ev.source.filter(|&i| i < n) {
                    Some(i) => pass.entries[i].db_id,
                    None if n == 1 => pass.entries[0].db_id,
                    None => None,
                };
                pass.write_event(&ev, ev_src).await?;
            }
        }
        let misses: Vec<Value> = pass
            .constraint_idx
            .iter()
            .filter(|i| !pass.c.carried.contains(i))
            .map(|&i| {
                let e = &pass.entries[i];
                json!({"entry_id": e.db_id, "text": e.text})
            })
            .collect();
        Ok(misses)
    }
    .await;
    let constraint_misses = match body {
        Ok(m) => m,
        Err(e) => {
            eprintln!("dream claim write failed ({e:?}); cursor NOT advanced");
            pass.finish_run("failed", None).await;
            let ids: Vec<i64> = pass.entries.iter().filter_map(|e| e.db_id).collect();
            reflush_stale(ready, &ids).await;
            return Ok(pass.held("write", None));
        }
    };

    let token = pulled["commit_token"].as_str().unwrap_or("").to_string();
    if crate::mutants::active("mcp-dream-run-stamp-before-commit") {
        let before = ready.cortex.snapshot().dream_cursor;
        pass.finish_run("committed", Some(before)).await;
    }
    let ack = {
        let _ws = ready.write.lock().await;
        super::service::commit(ready, cfg, &svc.persist_errors, &token).await?
    };
    let c = &pass.c;
    if let Some(error) = ack.get("error") {
        pass.finish_run("failed", None).await;
        let c = &pass.c;
        let display = {
            let _ws = ready.write.lock().await;
            ready.dream.lock().await.display_cursor_value()
        };
        let mut m = Map::new();
        m.insert("pulled".into(), json!(pass.entries.len()));
        m.insert("claims".into(), json!(c.claims()));
        for (k, v) in &c.tally {
            m.insert(k.clone(), v.clone());
        }
        m.insert("cursor".into(), json!(display));
        m.insert("acknowledgement_failed".into(), json!(true));
        m.insert("error".into(), error.clone());
        m.insert(
            "detail".into(),
            ack.get("detail").cloned().unwrap_or(json!("")),
        );
        m.insert("relations".into(), json!(0));
        m.insert("traces".into(), json!(c.traces));
        counters_tail(&mut m, c, false);
        return Ok(m);
    }
    let _ = c;
    let newest = ack.get("dream_cursor").cloned().unwrap_or(Value::Null);
    if !crate::mutants::active("mcp-dream-run-stamp-before-commit") {
        pass.finish_run("committed", newest.as_f64()).await;
    }
    let batch_sources: Vec<String> = pass
        .entries
        .iter()
        .filter_map(|e| e.source.clone().filter(|s| !s.is_empty()))
        .collect();
    let relations_n = super::relations::extract_relations(
        svc,
        ready,
        extractor,
        &texts,
        &batch_sources,
        &pass.entries,
    )
    .await;
    let alias_candidates =
        super::relations::propose_alias_candidates(svc, ready, &new_entities, &known).await;
    let lessons = lessons_disabled();
    if let Some(id) = pass.run_id
        && let Err(e) = update_run_tallies(
            ready,
            id,
            json!({"lesson_signals": 0, "lessons_written": 0, "lessons_deduped": 0}),
        )
        .await
    {
        eprintln!("dream: lesson-tally update failed ({e:?})");
    }
    let sources_attributed = super::relations::graph_backfill_sources(svc, ready).await?;
    let retyped = super::relations::retype_quarantined_links(
        svc,
        ready,
        extractor,
        cfg.retype_quarantined_max,
    )
    .await?;
    let c = &pass.c;
    let mut m = Map::new();
    m.insert("pulled".into(), json!(pass.entries.len()));
    m.insert("claims".into(), json!(c.claims()));
    m.insert("cursor".into(), newest);
    m.insert("relations".into(), json!(relations_n));
    for (k, v) in &c.tally {
        m.insert(k.clone(), v.clone());
    }
    m.insert("alias_candidates".into(), json!(alias_candidates));
    m.insert(
        "digests".into(),
        json!({"scanned": 0, "written": 0, "skipped": "claims-branch"}),
    );
    m.insert("lessons".into(), lessons);
    m.insert("outcome_inference".into(), outcome_inference_disabled());
    m.insert("graph_insight".into(), graph_insight_disabled());
    counters_tail(&mut m, c, true);
    m.insert("constraint_verbatim".into(), json!(c.carried.len()));
    m.insert("constraint_misses".into(), Value::Array(constraint_misses));
    m.insert("traces".into(), json!(c.traces));
    m.insert("sources_attributed".into(), json!(sources_attributed));
    m.insert("quarantined".into(), json!(c.quarantined));
    m.insert("retyped".into(), retyped);
    if let Some((why, error)) = isolation_error {
        m.insert(
            "extractor_error".into(),
            json!({"reason": why, "error": error}),
        );
    }
    Ok(m)
}

/// The gate and event counters, in the result's order (insert keeps a key
/// the tally already placed where it is, as a Python dict literal does).
fn counters_tail(m: &mut Map<String, Value>, c: &Counts, _committed: bool) {
    m.insert("literal_flagged".into(), json!(c.literal_flagged));
    m.insert("literal_dropped".into(), json!(c.literal_dropped));
    m.insert("span_flagged".into(), json!(c.span_flagged));
    m.insert("span_parked".into(), json!(c.span_parked));
    m.insert("quarantine_parked".into(), json!(c.qt_parked));
    m.insert("quarantine_held".into(), json!(c.qt_held));
    m.insert("quarantine_promoted".into(), json!(c.qt_promoted));
    m.insert("events_inserted".into(), json!(c.events_inserted));
    m.insert("events_duplicate".into(), json!(c.events_duplicate));
    m.insert("events_pass_failed".into(), json!(c.events_pass_failed));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stall_opens_on_the_second_failure_and_closes_on_success() {
        let mut t = StallTracker::default();
        let held = json!({"pulled": 2, "extractor_failed": true, "hold_phase": "extract",
                          "extractor_error": {"reason": "extractor_error", "error": "HTTP 500"}});
        t.record(held.as_object().unwrap(), false);
        assert_eq!(t.snapshot(), (Value::Null, Value::Null));
        t.record(held.as_object().unwrap(), false);
        let (stall, _) = t.snapshot();
        let keys: Vec<&String> = stall.as_object().unwrap().keys().collect();
        assert_eq!(
            keys,
            [
                "since",
                "reason",
                "consecutive_failures",
                "last_error",
                "last_success_at"
            ]
        );
        assert_eq!(stall["consecutive_failures"], json!(2));
        assert_eq!(stall["last_error"], json!("HTTP 500"));
        let write = json!({"pulled": 2, "extractor_failed": true, "hold_phase": "write"});
        t.record(write.as_object().unwrap(), false);
        assert_eq!(t.snapshot().0["consecutive_failures"], json!(2));
        let ok = json!({"pulled": 2, "quarantined": 1});
        t.record(ok.as_object().unwrap(), false);
        let (stall, last) = t.snapshot();
        assert!(stall.is_null());
        assert!(last["recovered_at"].is_number());
        let skipped = json!({"skipped": "dream_in_progress", "pulled": 0});
        t.record(skipped.as_object().unwrap(), false);
        let all_quarantined = json!({"pulled": 1, "quarantined": 1});
        t.record(all_quarantined.as_object().unwrap(), false);
        assert_eq!(t.failures, 1);
        assert_eq!(
            t.last_error.as_deref(),
            Some("every pulled memory failed extraction")
        );
    }

    #[test]
    fn refusal_names_the_first_enabled_stage() {
        let mut c = Config::default();
        let text = |c: &Config| match refusal(c) {
            Some(ToolError::Refused { detail, .. }) => detail.unwrap_or_default(),
            _ => String::new(),
        };
        assert!(text(&c).contains("infer_outcomes_stage"));
        c.memory.lessons_infer_outcomes = false;
        assert!(text(&c).contains("synthesize_lessons"));
        c.memory.lessons_enabled = false;
        assert!(text(&c).contains("_safe_refresh_graph_insight"));
        c.dream.digest_enabled = true;
        assert!(text(&c).contains("generate_digests_stage"));
        c.dream.digest_enabled = false;
        c.memory.graph_insight_enabled = false;
        assert!(refusal(&c).is_none());
    }
}
