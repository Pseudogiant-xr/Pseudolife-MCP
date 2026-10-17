//! `memory_fact_set` over MCP (spec "memory_fact_set over MCP", FS1-FS12).
//! Python: `mcp_server.py:1692-1747`; `service.py` `cortex_write`
//! (3916-4040), `_entity_kind_map` (3903), `_ensure_subject_entity` (1859),
//! `_save_cortex` (3746), `_emit_correction_signal` (4961); the engine is
//! `stores/cortex_write.rs`, the persistence `stores/cortex_sync.rs`.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::episodes::{self, Caller};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::cortex_search::record_to_dict;
use crate::service::{Ready, Service};
use crate::store::py_strip;
use crate::stores::cortex::norm_key;
use crate::stores::cortex_write::{FactWrite, norm_support};

/// The tool-boundary refusal for a set-valued slot (`service.py:4013`).
pub const SET_SLOT_REFUSAL: &str = "slot holds a set; use memory_set_add / memory_set_remove";
/// `episode_warning` for an unknown, closed or ambiguous handle.
const EPISODE_WARNING: &str = "unknown or closed episode handle";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_fact_set", mcp_memory_fact_set)]
}

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// The bound arguments `service.cortex_write` receives from
/// `mcp_server.memory_fact_set`.
struct Args {
    entity: String,
    attribute: String,
    value: String,
    /// `origin or "agent"`.
    support: String,
    confidence: f64,
    episode: Option<String>,
    freshness_class: String,
    authority: String,
    distortion_tolerance: String,
}

impl Args {
    fn bind(a: &Map<String, Value>) -> Args {
        let text = |k: &str, d: &str| a.get(k).and_then(Value::as_str).unwrap_or(d).to_string();
        Args {
            entity: text("entity", ""),
            attribute: text("attribute", ""),
            value: text("value", ""),
            support: text("origin", "agent"),
            confidence: a
                .get("confidence")
                .and_then(crate::mcp::binding::float_arg)
                .unwrap_or(0.8),
            episode: a.get("episode").and_then(Value::as_str).map(str::to_string),
            freshness_class: text("freshness_class", "auto"),
            authority: text("authority", "auto"),
            distortion_tolerance: text("distortion_tolerance", "auto"),
        }
    }
}

fn mcp_memory_fact_set<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let caller = Caller {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let writer = call.ident.writer.clone();
        let args = Args::bind(call.args);
        // Python's body runs on a worker thread a disconnecting client cannot
        // stop; an admitted write runs to completion in its own task.
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { cortex_write(&svc, &r2, &caller, writer, args).await })
            .await
            .map_err(internal)?
    })
}

/// `_entity_kind_map().get(_norm_key(entity))`: `entity_kinds` is read once
/// and cached for the process (`service.py:3903-3914`).
async fn entity_kind(
    ws: &mut episodes::WriteState,
    db: &Client,
    entity: &str,
) -> Result<Option<String>, ToolError> {
    if ws.entity_kinds.is_none() {
        let rows = crate::txn::with_client(db, |db| {
            db.query("SELECT entity_norm, kind FROM entity_kinds", &[])
        })
        .await
        .map_err(internal)?;
        let map: HashMap<String, String> = rows.iter().map(|r| (r.get(0), r.get(1))).collect();
        ws.entity_kinds = Some(map);
    }
    Ok(ws
        .entity_kinds
        .as_ref()
        .and_then(|m| m.get(&norm_key(entity)).cloned()))
}

async fn embed(ready: &Ready, text: String) -> Result<Arc<[f32]>, ToolError> {
    let embedder = ready.embedder.clone();
    let row = tokio::task::spawn_blocking(move || embedder.embed(&text))
        .await
        .map_err(internal)?
        .map_err(internal)?;
    Ok(Arc::from(row))
}

/// `service.cortex_write` with the MCP arguments, under the write lock (the
/// Python service lock) from the freshness lookup to the answer.
async fn cortex_write(
    svc: &Service,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: String,
    a: Args,
) -> Result<Value, ToolError> {
    let authority = crate::labels::resolve_authority(Some(&a.authority), &a.value)
        .map_err(ToolError::Invalid)?;
    let distortion = crate::labels::resolve_distortion(Some(&a.distortion_tolerance), &a.value)
        .map_err(ToolError::Invalid)?;
    let db = ready.storage.client();
    let mut ws = ready.write.lock().await;
    let freshness_class = if a.freshness_class == "auto" {
        let kind = entity_kind(&mut ws, db, &a.entity).await?;
        crate::freshness::resolve_class(kind.as_deref(), &norm_key(&a.attribute)).to_string()
    } else {
        a.freshness_class.clone()
    };
    let claim = py_strip(&format!("{} {} {}", a.entity, a.attribute, a.value)).to_string();
    let embedding = embed(ready, claim).await?;
    let slot_text = py_strip(&format!("{} {}", a.entity, a.attribute)).to_string();
    let slot_embedding = embed(ready, slot_text).await?;
    let mut session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let resolved = ws.resolve_handle(db, a.episode.as_deref()).await;
    let warning = a.episode.as_deref().is_some_and(|e| !e.is_empty()) && resolved.is_none();
    if let Some((_, key)) = &resolved
        && caller.header_session.as_deref().is_none_or(str::is_empty)
    {
        session = key.clone();
    }
    // Ticked before the write, so a refused write still advances the clock.
    let hlc = ready.tick_hlc();
    let w = FactWrite {
        entity: &a.entity,
        attribute: &a.attribute,
        value: &a.value,
        embedding,
        slot_embedding: Some(slot_embedding),
        confidence: a.confidence,
        support: norm_support(Some(&a.support)),
        now: episodes::now(),
        hlc,
        writer_id: Some(writer),
        session_id: session.clone(),
        freshness_class,
        authority,
        distortion_tolerance: distortion,
    };
    let outcome = ready
        .cortex
        .write(|s| s.write_fact(&w))
        .map_err(|_| ToolError::Invalid(SET_SLOT_REFUSAL.into()))?;
    if !crate::mutants::active("mcp-fact-set-no-entity-mint") {
        ensure_subject_entity(db, &a.entity)
            .await
            .map_err(internal)?;
    }
    save_cortex(svc, ready).await?;
    let snap = ready.cortex.snapshot();
    let record = &snap.records[outcome.record];
    if outcome.action == "superseded"
        && py_strip(&a.support).to_lowercase() == "user"
        && !crate::mutants::active("mcp-fact-set-no-correction-signal")
    {
        let episode_id = match &resolved {
            Some((id, _)) => Some(id.clone()),
            None => ws
                .log
                .open_leaf_for(session.as_deref().filter(|s| !s.is_empty()))
                .map(|i| ws.log.episodes[i].id.clone()),
        };
        let old = record.supersedes_value.as_deref().unwrap_or("None");
        emit_correction_signal(svc, db, &a.entity, &a.attribute, old, &a.value, episode_id).await;
    }
    let policy = &svc.config.memory.search.stale_policy;
    let now = episodes::now();
    let mut out = Map::new();
    out.insert("action".into(), json!(outcome.action));
    out.extend(record_to_dict(record, policy, now));
    if outcome.action == "contested" {
        let current = snap
            .lookup(&a.entity, &a.attribute)
            .map_or(Value::Null, |c| {
                Value::Object(record_to_dict(c, policy, now))
            });
        out.insert("current".into(), current);
    }
    if warning {
        out.insert("episode_warning".into(), json!(EPISODE_WARNING));
    }
    Ok(Value::Object(out))
}

/// `_ensure_subject_entity` (`service.py:1859`): mint the subject's graph
/// node unless its name is junk-shaped or it already resolves.
async fn ensure_subject_entity(db: &Client, entity: &str) -> anyhow::Result<()> {
    if crate::junk_names::junk_name_reason(entity).is_some() {
        return Ok(());
    }
    let n = crate::storage::graph::norm_name(entity);
    if n.is_empty() {
        return Ok(());
    }
    if crate::storage::graph::find_entity(db, &n).await?.is_null() {
        crate::storage::graph::ensure_entity(db, &n, Some(py_strip(entity)), None).await?;
    }
    Ok(())
}

/// `_save_cortex` (`service.py:3746`): the per-slot sync. A failure counts
/// in `persist_errors` and surfaces as an internal error (Python's
/// `PersistenceError`); marks persisted before it are cleared, the rest kept.
async fn save_cortex(svc: &Service, ready: &Ready) -> Result<(), ToolError> {
    let db = ready.storage.client();
    let fail = |e: anyhow::Error| {
        svc.persist_errors
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        eprintln!("Cortex save failed (NOT durably persisted): {e:#}");
        internal(format!("cortex save failed: {e:#}"))
    };
    let snap = ready.cortex.snapshot();
    let rows = crate::stores::cortex_sync::sync_slot_rows(db, &snap).await;
    drop(snap);
    let synced = rows.map_err(fail)?;
    ready.cortex.write(|s| {
        for key in &synced.slots {
            s.dirty_slots.remove(key);
        }
        for &(i, id) in &synced.ids {
            s.records[i].id = id;
        }
    });
    let snap = ready.cortex.snapshot();
    let meta = crate::stores::cortex_sync::sync_meta(db, &snap).await;
    drop(snap);
    if meta.map_err(fail)? {
        ready.cortex.write(|s| s.meta_dirty = false);
    }
    Ok(())
}

/// `_emit_correction_signal` (`service.py:4961`): with lessons enabled, one
/// `outcome_signals` row in its own transaction; a failure is logged and
/// swallowed.
async fn emit_correction_signal(
    svc: &Service,
    db: &Client,
    entity: &str,
    attribute: &str,
    old: &str,
    new: &str,
    episode_id: Option<String>,
) {
    if !svc.config.memory.lessons_enabled {
        return;
    }
    let detail = format!("{attribute}: {old} \u{2192} {new}");
    let (detail, episode_id) = (&detail, &episode_id);
    let res = crate::txn::run(db, |c| async move {
        c.execute(
            "INSERT INTO outcome_signals (task, outcome, about, detail, polarity, origin, \
             episode_id, created_at) VALUES ($1, 'correction', $1, $2, NULL, 'action', $3, $4)",
            &[&entity, detail, episode_id, &episodes::now()],
        )
        .await
    })
    .await;
    if let Err(e) = res {
        eprintln!("correction signal emit failed: {e}");
    }
}
