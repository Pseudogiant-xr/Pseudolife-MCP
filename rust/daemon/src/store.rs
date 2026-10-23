//! `memory_store` (spec W1): `mcp_server.memory_store` → `service.store` →
//! `ContinuumMemorySystem._store`.

use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use serde_json::{Map, Value, json};

use crate::bank::{Bank, Entry};
use crate::config::MemoryConfig;
use crate::contradiction::{self, Candidate};
use crate::episodes::{self, Caller, WriteState};
use crate::labels::{self, Resolved};
use crate::service::Ready;
use crate::slots::{self, Slot};
use crate::txn;

/// Admitted writes outlive their callers, but must finish before exit.
#[derive(Clone, Default)]
pub struct WriteTasks {
    tasks: tokio_util::task::TaskTracker,
}

impl WriteTasks {
    pub fn spawn<F>(&self, task: F) -> tokio::task::JoinHandle<F::Output>
    where
        F: std::future::Future + Send + 'static,
        F::Output: Send + 'static,
    {
        self.tasks.spawn(task)
    }

    pub async fn drain(&self) {
        self.tasks.close();
        self.tasks.wait().await;
    }
}

/// What a tool body returns when it does not return its dict (W0.1).
#[derive(Debug)]
pub enum ToolError {
    /// A service `ValueError`: `{"error": ..., "message": text}` once framed.
    Invalid(String),
    /// Anything else: the generic internal-error payload once framed.
    Internal(String),
}

/// `_STORE_NOTES` (`mcp_server.py:671-680`).
fn store_note(reason: &str) -> Option<&'static str> {
    Some(match reason {
        "empty" => "Not stored: the text was empty.",
        "filtered_meta" => {
            "Not stored: reads as a statement about the memory system itself. Rephrase as the fact."
        }
        "below_surprise_threshold" => {
            "Not stored: a near-duplicate of a memory already held; no need to retry."
        }
        "rejected" => "Not stored: the admission gate refused it; nothing was written.",
        _ => return None,
    })
}
const EXACT_DUPLICATE_NOTE: &str =
    "Not stored: it repeats a memory already held word for word; no need to retry.";

/// `SURPRISE_HISTORY` cap (`cms.py:304`).
const MAX_HISTORY: usize = 100;

/// Python `str.strip()`.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `round(x, 4)`: correctly rounded on the exact binary value.
pub fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

/// `normalize_tags` (`memory/episodes.py:41-61`).
pub fn normalize_tags(tags: Option<&Value>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let Some(Value::Array(items)) = tags else {
        return out;
    };
    for raw in items {
        let Value::String(raw) = raw else { continue };
        let norm = if crate::mutants::active("store-tags-raw") {
            raw.clone()
        } else {
            py_strip(raw).to_lowercase()
        };
        if !norm.is_empty() && !out.contains(&norm) {
            out.push(norm);
        }
    }
    out
}

fn arg_str<'a>(args: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    args.get(key).and_then(Value::as_str)
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

/// `RetentionPolicy.source_weighted_score` under `balanced`
/// (`protocols.py:84-103`, `retention.py:37-47`).
fn retention_score(e: &Entry, now: f64, boost: f64) -> f64 {
    retention_score_as(e, e.superseded_at.is_some(), now, boost)
}

/// [`retention_score`] with the entry's retirement given: a correction
/// chooses its eviction victim with its targets already retired.
fn retention_score_as(e: &Entry, superseded: bool, now: f64, boost: f64) -> f64 {
    let age = (now - e.ts).max(1.0);
    let balanced = e.access_count.load(Ordering::Relaxed) as f64 / age + e.surprise_value * 0.1;
    let weight = match e.source.as_str() {
        "user_msg" | "user" => 1.5,
        "system" => 10.0,
        "tool_call" => 0.5,
        "llm_thinking" => 0.2,
        _ => 1.0,
    };
    let mut score = (balanced + 1.0) * weight + boost * (e.reinforcements as f64).ln_1p();
    if superseded {
        score *= 0.05;
    }
    score
}

/// The CMS half of the scan: per-band surprise, the coreference-aware slots
/// and whether any band holds a possible conflict (W1.5-W1.7).
struct Scan {
    per_band: Vec<f64>,
    slots: Vec<Slot>,
    contradiction: bool,
}

fn scan(bank: &Bank, bands: &[String], text: &str, q: &[f32], anchor: Option<&str>) -> Scan {
    let mut best: Vec<Option<f32>> = vec![None; bands.len()];
    let mut sims = Vec::with_capacity(bank.entries.len());
    for (i, e) in bank.entries.iter().enumerate() {
        let s = dot(bank.row(i), q);
        sims.push(s);
        if let Some(b) = bands.iter().position(|n| *n == e.band) {
            best[b] = Some(best[b].map_or(s, |m| m.max(s)));
        }
    }
    let per_band = best
        .iter()
        .map(|m| match m {
            None => 1.0,
            Some(m) => (1.0 - f64::from(*m)).clamp(0.0, 1.0),
        })
        .collect();
    let slots = slots::extract_slots(text, anchor);
    let contradiction = bands.iter().any(|band| {
        let candidates = bank
            .entries
            .iter()
            .zip(&sims)
            .filter(|(e, _)| e.band == *band)
            .map(|(e, &sim)| Candidate {
                text: &e.text,
                slots: &e.slots,
                superseded: e.superseded_at.is_some(),
                sim,
                cues: &e.cues,
            });
        contradiction::any_contradiction(text, &slots, candidates)
    });
    Scan {
        per_band,
        slots,
        contradiction,
    }
}

/// `delete_evicted_entry` (`storage/postgres.py:1380-1422`).
async fn delete_evicted(
    client: &tokio_postgres::Client,
    e: &Entry,
) -> Result<(), tokio_postgres::Error> {
    let superseded = e.superseded_at.is_some();
    txn::run(client, |c| async move {
        delete_evicted_rows(c, e.id, &e.source, superseded).await
    })
    .await
}

/// The statements of [`delete_evicted`], on a transaction the caller holds.
/// `superseded` is the victim's retirement as the caller staged it.
pub(crate) async fn delete_evicted_rows(
    c: &tokio_postgres::Client,
    id: i64,
    source: &str,
    superseded: bool,
) -> Result<(), tokio_postgres::Error> {
    let record = json!({
        "count": 1,
        "last_at": episodes::now(),
        "last_entry_id": id,
        "last_source": source,
        "last_superseded": superseded,
    });
    c.execute("DELETE FROM entries WHERE id = $1", &[&id])
        .await?;
    c.execute(
        "INSERT INTO meta (key, value) VALUES ('capacity_true_drops', $1) \
         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value \
         || jsonb_build_object('count', CASE \
         WHEN jsonb_typeof(meta.value->'count') IS DISTINCT FROM 'number' THEN 0 \
         WHEN (meta.value->>'count')::numeric >= 0 \
         AND (meta.value->>'count')::numeric < 9223372036854775807 \
         THEN floor((meta.value->>'count')::numeric)::bigint \
         ELSE 0 END + 1)",
        &[&record],
    )
    .await
    .map(|_| ())
}

pub(crate) struct NewEntry<'a> {
    pub text: &'a str,
    pub row: Vec<f32>,
    pub source: &'a str,
    pub tags: Vec<String>,
    pub session_key: Option<&'a str>,
    pub attribution: Option<&'a str>,
    pub authority: Option<String>,
    pub distortion: Option<String>,
}

/// What `ContinuumMemorySystem._store` decides before its write-through.
pub(crate) enum Admit {
    /// The meta filter refused it (`(False, 0.0)`, no surprise recorded).
    Filtered,
    /// The surprise gate refused it; the per-band surprises still count.
    BelowGate {
        overall: f64,
        per_band: Vec<f64>,
    },
    Admitted(Box<Admission>),
}

/// An admitted entry, fully stamped, not yet written or resident.
pub(crate) struct Admission {
    pub entry: Entry,
    /// The band-normalized row the resident matrix holds.
    pub unit: Vec<f32>,
    /// The encoder's row, as the insert writes it.
    pub row: Vec<f32>,
    pub overall: f64,
    pub per_band: Vec<f64>,
    /// The first band's capacity victim, a resident index.
    pub victim: Option<usize>,
    /// The coreference anchor this entry sets (`_last_entity_seen`).
    pub anchor: Option<String>,
}

/// `ContinuumMemorySystem._store` up to its write-through (W1.4-W1.8):
/// the meta filter, the per-band surprise, the contradiction scan, the gate
/// (unless `bypass`, `bypass_surprise_gate`), the capacity victim and the
/// stamped entry. Nothing is written and nothing resident changes. The
/// victim is chosen as if every row in `retiring` were already retired.
pub(crate) async fn admit(
    ready: &Ready,
    cfg: &MemoryConfig,
    ws: &WriteState,
    new: NewEntry<'_>,
    bypass: bool,
    retiring: &[i64],
) -> Result<Admit, ToolError> {
    if cfg.meta_filter_enabled && crate::meta_filter::is_meta_statement(new.text, new.source) {
        return Ok(Admit::Filtered);
    }
    // The band normalizes both the stored rows and the query again
    // (band.py:104-106, 207-214), on top of the encoder's own normalization.
    let mut unit = new.row.clone();
    crate::bank::normalize_query(&mut unit);
    let bank = ready.bank.clone();
    let bands = cfg.bands.clone();
    let text = new.text.to_string();
    let row = unit.clone();
    let anchor = ws.last_entity_seen.clone();
    let found = tokio::task::spawn_blocking(move || {
        let bank = bank.read().expect("bank lock");
        scan(&bank, &bands, &text, &row, anchor.as_deref())
    })
    .await
    .map_err(|e| ToolError::Internal(e.to_string()))?;
    let overall = found.per_band.iter().copied().fold(f64::INFINITY, f64::min);
    if !bypass && !found.contradiction && overall < cfg.surprise_threshold {
        return Ok(Admit::BelowGate {
            overall,
            per_band: found.per_band,
        });
    }

    // Capacity first (band.py:130-132): the victim is chosen before the new
    // entry exists.
    let band0 = cfg.bands[0].clone();
    let victim = first_band_victim(ready, cfg, retiring);

    let mut entry = Entry {
        band: band0.clone(),
        stored_band: band0,
        text: new.text.to_string(),
        surprise: overall as f32,
        surprise_value: overall,
        embedding: new.row.clone(),
        ts: episodes::now(),
        access_count: AtomicI64::new(0),
        source: new.source.to_string(),
        seq: episodes::next_seq(),
        tags: new.tags.into_iter().map(Value::String).collect(),
        slots: found.slots.iter().map(Slot::to_json).collect(),
        authority: new.authority,
        distortion_tolerance: new.distortion,
        // `MemoryEntry.dream_state` defaults to "pending", as the insert writes.
        dream_state: (!crate::mutants::active("mcp-dream-store-not-pending"))
            .then(|| "pending".into()),
        ..Default::default()
    };
    if let Some(i) = ws.log.stamp_target(new.session_key) {
        entry.episode_id = Some(ws.log.episodes[i].id.clone());
        entry.episode_title = Some(ws.log.episodes[i].title.clone());
    }
    if let Some(a) = new.attribution {
        entry.episode_id = Some(a.to_string());
        if let Some(ep) = ws.log.get(a) {
            entry.episode_title = Some(ep.title.clone());
        }
    }
    let anchor = found
        .slots
        .iter()
        .find(|s| s.attribute == "name" || s.attribute == "type")
        .map(|s| s.entity.clone());
    Ok(Admit::Admitted(Box::new(Admission {
        entry,
        unit,
        row: new.row,
        overall,
        per_band: found.per_band,
        victim,
        anchor,
    })))
}

/// `MIRASBand.store`'s capacity check on the first band (`band.py:130-132`,
/// `_evict_one`): at capacity, the resident index of the entry with the
/// lowest source-weighted retention score, as if every row in `retiring`
/// were already retired.
pub(crate) fn first_band_victim(
    ready: &Ready,
    cfg: &MemoryConfig,
    retiring: &[i64],
) -> Option<usize> {
    let band0 = &cfg.bands[0];
    let cap = cfg
        .band_specs
        .first()
        .map(|b| b.max_entries as i64)
        .unwrap_or(i64::MAX);
    let bank = ready.bank.read().expect("bank lock");
    let held: Vec<usize> = (0..bank.entries.len())
        .filter(|&i| bank.entries[i].band == *band0)
        .collect();
    let score = |i: usize, t: f64| {
        let e = &bank.entries[i];
        let superseded = e.superseded_at.is_some() || retiring.contains(&e.id);
        retention_score_as(e, superseded, t, cfg.retention_boost)
    };
    (held.len() as i64 >= cap && !held.is_empty()).then(|| {
        let t = episodes::now();
        let mut worst = held[0];
        let mut worst_score = score(worst, t);
        for &i in &held[1..] {
            let s = score(i, t);
            if s < worst_score {
                worst = i;
                worst_score = s;
            }
        }
        worst
    })
}

/// `_on_band_evict` for the first band's victim: a true drop (removed from
/// the resident bank, its row deleted and counted; a failed delete is
/// logged, never the store's failure).
pub(crate) async fn evict_resident(ready: &Ready, idx: usize) {
    let gone = ready.bank.write().expect("bank lock").remove(idx);
    crate::read::stats::record_true_drop();
    // Only the last band's overflow is a true drop; a deeper band would
    // take the entry (not ported: see divergences).
    if let Err(e) = delete_evicted(ready.storage.client(), &gone).await {
        eprintln!("evict write-through failed: {e}");
    }
    eprintln!(
        "capacity eviction true drop: entry_id={} source={:?}",
        gone.id, gone.source
    );
}

/// Append one store's per-band surprises to `_surprise_history`.
pub(crate) fn record_surprise(ws: &mut WriteState, cfg: &MemoryConfig, per_band: &[f64]) {
    for (band, s) in cfg.bands.iter().zip(per_band) {
        let h = ws.surprise_history.entry(band.clone()).or_default();
        h.push(*s);
        if h.len() > MAX_HISTORY {
            h.drain(..h.len() - MAX_HISTORY);
        }
    }
}

/// The write-through insert (`storage.insert_entry(entry_to_row(entry))`),
/// on a transaction the caller holds: the new row's id.
pub(crate) async fn insert_entry_row(
    c: &tokio_postgres::Client,
    e: &Entry,
    row: &Vec<f32>,
) -> Result<i64, tokio_postgres::Error> {
    let tags = Value::from(e.tags.clone());
    let slot_rows = Value::from(e.slots.clone());
    let r = c
        .query_one(
            "INSERT INTO entries (band, text, embedding, surprise, ts, access_count, \
             source, superseded_at, superseded_by_text, last_logical_turn, episode_id, \
             episode_title, tags, slots, authority, distortion_tolerance, dream_state) \
             VALUES ($1, $2, $3::real[]::vector, $4, $5, 0, $6, NULL, NULL, NULL, $7, \
             $8, $9, $10, $11, $12, 'pending') RETURNING id",
            &[
                &e.band,
                &e.text,
                row,
                &e.surprise,
                &e.ts,
                &e.source,
                &e.episode_id,
                &e.episode_title,
                &tags,
                &slot_rows,
                &e.authority,
                &e.distortion_tolerance,
            ],
        )
        .await?;
    Ok(r.get(0))
}

/// `ContinuumMemorySystem._store` (W1.4-W1.10): `(stored, surprise)`.
async fn cms_store(
    ready: &Ready,
    cfg: &MemoryConfig,
    ws: &mut WriteState,
    new: NewEntry<'_>,
) -> Result<(bool, f64), ToolError> {
    let adm = match admit(ready, cfg, ws, new, false, &[]).await? {
        Admit::Filtered => return Ok((false, 0.0)),
        Admit::BelowGate { overall, per_band } => {
            record_surprise(ws, cfg, &per_band);
            return Ok((false, overall));
        }
        Admit::Admitted(adm) => adm,
    };
    record_surprise(ws, cfg, &adm.per_band);
    let client = ready.storage.client();
    if let Some(idx) = adm.victim {
        evict_resident(ready, idx).await;
    }
    let Admission {
        mut entry,
        unit,
        row,
        overall,
        anchor,
        ..
    } = *adm;
    if let Some(a) = anchor {
        ws.last_entity_seen = Some(a);
    }
    let id: i64 = {
        let (e, row) = (&entry, &row);
        txn::run(client, |c| async move { insert_entry_row(c, e, row).await })
            .await
            .map_err(|e| ToolError::Internal(format!("entry insert failed: {e}")))?
    };
    entry.id = id;
    debug_assert_eq!(unit.len(), ready.bank.read().expect("bank lock").dim);
    ready.bank.write().expect("bank lock").push(entry, &unit);
    ws.interaction_count += 1;
    Ok((true, overall))
}

fn resolved_label(r: Resolved) -> Option<String> {
    match r {
        Resolved::Inherit => None,
        Resolved::Set(v) => v,
    }
}

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![("memory_store", mcp_memory_store)]
}

/// `memory_store` on the MCP surface (spec W0.4): bound arguments and the
/// request's caller identity into the body below. A service `ValueError`
/// is `invalid_argument` prose; anything else, initialization included,
/// is `internal_error` (`mcp_server._error_payload`).
fn mcp_memory_store<'a>(
    app: &'a crate::http::App,
    call: crate::mcp::dispatch::ToolCall<'a>,
) -> crate::mcp::dispatch::ToolFuture<'a> {
    use crate::mcp::dispatch::ToolError as McpError;
    Box::pin(async move {
        let ready = app
            .service
            .ensure_init()
            .await
            .map_err(|e| McpError::Internal(anyhow::anyhow!(e)))?;
        let caller = Caller {
            header_session: call
                .ident
                .header_session
                .clone()
                .filter(|_| !crate::mutants::active("mcp-store-no-session")),
            transport_session: call.ident.transport_session.clone(),
        };
        memory_store(&ready, &app.service.config.memory, &caller, call.args)
            .await
            .map_err(|e| match e {
                ToolError::Invalid(text) => McpError::Invalid(text),
                ToolError::Internal(text) => McpError::Internal(anyhow::anyhow!(text)),
            })
    })
}

/// The `memory_store` tool body (W1): the dict `_store_noted` returns.
///
/// Runs to completion in its own task: Python's body runs on a worker
/// thread that a disconnecting client cannot stop (`_async_offload`), so a
/// store whose caller hung up still lands.
pub async fn memory_store(
    ready: &Arc<Ready>,
    cfg: &MemoryConfig,
    caller: &Caller,
    args: &Map<String, Value>,
) -> Result<Value, ToolError> {
    let writes = ready.writes.clone();
    let (ready, cfg, caller, args) = (ready.clone(), cfg.clone(), caller.clone(), args.clone());
    let task = async move { store_body(&ready, &cfg, &caller, &args).await };
    if crate::mutants::active("store-cancellable") {
        return task.await;
    }
    writes
        .spawn(task)
        .await
        .map_err(|e| ToolError::Internal(e.to_string()))?
}

async fn store_body(
    ready: &Arc<Ready>,
    cfg: &MemoryConfig,
    caller: &Caller,
    args: &Map<String, Value>,
) -> Result<Value, ToolError> {
    let raw_text = arg_str(args, "text").unwrap_or("");
    let source = arg_str(args, "source").unwrap_or("agent");
    let episode = arg_str(args, "episode");
    let authority =
        labels::resolve_authority(Some(arg_str(args, "authority").unwrap_or("auto")), raw_text)
            .map_err(ToolError::Invalid)?;
    let distortion = labels::resolve_distortion(
        Some(arg_str(args, "distortion_tolerance").unwrap_or("auto")),
        raw_text,
    )
    .map_err(ToolError::Invalid)?;
    let (auth, dt) = (resolved_label(authority), resolved_label(distortion));

    let mut ws = ready.write.lock().await;
    let text = py_strip(raw_text);
    let mut out = Map::new();
    if text.is_empty() {
        out.insert("stored".into(), json!(false));
        out.insert("surprise".into(), json!(0.0));
        out.insert("reason".into(), json!("empty"));
        out.insert("cortex_promoted".into(), json!(0));
        out.insert("note".into(), json!(store_note("empty")));
        return Ok(Value::Object(out));
    }
    let embedder = ready.embedder.clone();
    let owned = text.to_string();
    let row = tokio::task::spawn_blocking(move || embedder.embed(&owned))
        .await
        .map_err(|e| ToolError::Internal(e.to_string()))?
        .map_err(|e| ToolError::Internal(e.to_string()))?;
    let client = ready.storage.client();
    let mut session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let resolved = ws.resolve_handle(client, episode).await;
    let warning = episode.is_some_and(|e| !e.is_empty()) && resolved.is_none();
    match &resolved {
        Some((_, key)) => {
            if caller.header_session.as_deref().is_none_or(str::is_empty) {
                session = key.clone();
            }
        }
        None if crate::mutants::active("store-skip-session-episode") => {}
        None => ws
            .ensure_session_episode(client, session.as_deref())
            .await
            .map_err(ToolError::Invalid)?,
    }
    let new = NewEntry {
        text,
        row,
        source,
        tags: normalize_tags(args.get("tags")),
        session_key: session.as_deref(),
        attribution: resolved.as_ref().map(|r| r.0.as_str()),
        authority: auth.clone(),
        distortion: dt.clone(),
    };
    let (stored, surprise) = cms_store(ready, cfg, &mut ws, new).await?;
    let reason = (!stored).then_some(if surprise == 0.0 {
        "filtered_meta"
    } else if surprise < cfg.surprise_threshold {
        "below_surprise_threshold"
    } else {
        "rejected"
    });
    out.insert("stored".into(), json!(stored));
    out.insert(
        "surprise".into(),
        json!(if crate::mutants::active("store-surprise-raw") {
            surprise + 0.01
        } else {
            round4(surprise)
        }),
    );
    out.insert("reason".into(), json!(reason));
    out.insert("cortex_promoted".into(), json!(0));
    if let Some(a) = auth {
        out.insert("authority".into(), json!(a));
    }
    if let Some(d) = dt {
        out.insert("distortion_tolerance".into(), json!(d));
    }
    let root = match &resolved {
        Some((id, _)) => ws.log.index(id),
        None => ws.log.session_root(session.as_deref()),
    };
    if let Some(i) = root
        && episodes::is_generic_title(&ws.log.episodes[i].title)
        && !crate::mutants::active("store-no-hint")
    {
        let handle_arg = if resolved.is_some() {
            let id = &ws.log.episodes[i].id;
            format!(", episode='{}'", id.chars().take(12).collect::<String>())
        } else {
            String::new()
        };
        out.insert(
            "episode_hint".into(),
            json!(format!(
                "session episode is untitled \u{2014} call memory_session_title('<project> - <topic>'{handle_arg})"
            )),
        );
    }
    if warning {
        out.insert(
            "episode_warning".into(),
            json!("unknown or closed episode handle"),
        );
    }
    if let Some(r) = reason {
        // `_store_noted`: an exact duplicate reads as filtered_meta.
        if r == "filtered_meta"
            && cfg.surprise_threshold > 0.0
            && !(cfg.meta_filter_enabled && crate::meta_filter::is_meta_statement(text, source))
        {
            out.insert("note".into(), json!(EXACT_DUPLICATE_NOTE));
        } else if let Some(n) = store_note(r) {
            out.insert("note".into(), json!(n));
        }
    }
    Ok(Value::Object(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn shutdown_waits_for_a_store_after_its_caller_disconnects() {
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::time::Duration;
        let writes = WriteTasks::default();
        let persisted = Arc::new(AtomicBool::new(false));
        let recorded = persisted.clone();
        let (release, finish) = tokio::sync::oneshot::channel::<()>();
        let caller = writes.spawn(async move {
            finish.await.unwrap();
            recorded.store(true, Ordering::SeqCst);
        });
        drop(caller);
        let mut shutdown = Box::pin(writes.drain());
        assert!(
            tokio::time::timeout(Duration::from_millis(20), &mut shutdown)
                .await
                .is_err()
        );
        release.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(1), shutdown)
            .await
            .unwrap();
        assert!(persisted.load(Ordering::SeqCst));
    }

    /// The harness-only route (spec W0.2) never ships: it is not in
    /// Python's route table, and every line that reaches it is gated.
    #[test]
    fn harness_route_is_feature_gated() {
        assert!(!crate::routes::RouteTable::get().has("/api/_harness/tool/memory_store"));
        let http = include_str!("http.rs");
        let main = include_str!("main.rs");
        for (src, needle) in [(http, "harness_route::call"), (main, "mod harness_route;")] {
            let lines: Vec<&str> = src.lines().collect();
            let hits: Vec<usize> = (0..lines.len())
                .filter(|&i| lines[i].contains(needle))
                .collect();
            assert_eq!(hits.len(), 1, "{needle}");
            let gated = (hits[0].saturating_sub(2)..hits[0])
                .any(|i| lines[i].trim() == "#[cfg(feature = \"harness\")]");
            assert!(
                gated,
                "{needle} must sit under #[cfg(feature = \"harness\")]"
            );
        }
    }

    #[test]
    fn tags_normalize_like_python() {
        let v = json!([
            " Decision ",
            "decision",
            "",
            "  ",
            3,
            "BLOCKER",
            "blocker\u{1c}"
        ]);
        assert_eq!(normalize_tags(Some(&v)), ["decision", "blocker"]);
        assert!(normalize_tags(None).is_empty());
        assert!(normalize_tags(Some(&Value::Null)).is_empty());
    }

    #[test]
    fn round4_is_python_round() {
        // Values from CPython 3.11: the decision follows the exact binary
        // value (0.00015 and 2.67455 sit just below their halves).
        for (x, want) in [
            (0.12345, 0.1235),
            (1.00005, 1.0001),
            (0.00015, 0.0001),
            (2.67455, 2.6745),
            (1.0, 1.0),
        ] {
            assert_eq!(round4(x), want, "round({x}, 4)");
        }
    }

    #[test]
    fn retention_matches_python_formula() {
        let e = Entry {
            ts: 100.0,
            surprise: 0.5,
            surprise_value: 0.5,
            source: "user".into(),
            reinforcements: 3,
            access_count: AtomicI64::new(4),
            ..Default::default()
        };
        // ((4/50 + 0.05) + 1) * 1.5 + 1.0 * log1p(3)
        let want = (4.0 / 50.0 + 0.05 + 1.0) * 1.5 + 3f64.ln_1p();
        assert!((retention_score(&e, 150.0, 1.0) - want).abs() < 1e-12);
    }
}
