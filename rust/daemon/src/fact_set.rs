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

/// The tool-boundary refusal for a set-valued slot (`service.py:4014`).
pub const SET_SLOT_REFUSAL: &str = "slot holds a set; use memory_set_add / memory_set_remove";
/// `episode_warning` for an unknown, closed or ambiguous handle.
const EPISODE_WARNING: &str = "unknown or closed episode handle";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_fact_set", mcp_memory_fact_set)]
}

pub(crate) fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// A label argument of `service.cortex_write` (`authority`,
/// `distortion_tolerance`): Python's `INHERIT` sentinel, or a requested
/// value for `resolve_authority` / `resolve_distortion` (`"auto"` infers
/// from the value, a string is validated, None clears).
pub(crate) enum LabelArg {
    /// Passed by the dream's claim writes for an unlabelled source.
    #[allow(dead_code)]
    Inherit,
    Requested(Option<String>),
}

impl LabelArg {
    fn auto() -> LabelArg {
        LabelArg::Requested(Some("auto".into()))
    }

    /// `resolve_authority(requested, value)` / `resolve_distortion(..)`.
    fn resolve(
        &self,
        value: &str,
        f: fn(Option<&str>, &str) -> Result<crate::labels::Resolved, String>,
    ) -> Result<crate::labels::Resolved, String> {
        match self {
            LabelArg::Inherit => Ok(crate::labels::Resolved::Inherit),
            LabelArg::Requested(r) => f(r.as_deref(), value),
        }
    }
}

/// The arguments of `service.cortex_write` (`service.py:3916-3933`) but
/// `now`, which no caller passes: `memory_fact_set` binds its MCP
/// arguments (`Self::bind`); the dream's claim writes pass `support`,
/// `stance`, both labels, `dream=True` and, to park, `provenance` and
/// `force_contend` (`service_dream.py:2106-2208`).
pub(crate) struct CortexWriteArgs {
    pub entity: String,
    pub attribute: String,
    pub value: String,
    pub confidence: f64,
    pub provenance: Vec<String>,
    /// As passed; `_norm_support` runs in the store.
    pub support: Option<String>,
    pub episode: Option<String>,
    pub freshness_class: String,
    pub force_contend: bool,
    pub stance: Option<String>,
    pub authority: LabelArg,
    pub distortion_tolerance: LabelArg,
    pub dream: bool,
}

impl CortexWriteArgs {
    /// The Python defaults: confidence 0.7, no provenance or support,
    /// `freshness_class="auto"`, both labels `"auto"`, no stance, neither
    /// `force_contend` nor `dream`.
    pub(crate) fn new(entity: &str, attribute: &str, value: &str) -> CortexWriteArgs {
        CortexWriteArgs {
            entity: entity.into(),
            attribute: attribute.into(),
            value: value.into(),
            confidence: 0.7,
            provenance: Vec::new(),
            support: None,
            episode: None,
            freshness_class: "auto".into(),
            force_contend: false,
            stance: None,
            authority: LabelArg::auto(),
            distortion_tolerance: LabelArg::auto(),
            dream: false,
        }
    }

    /// The arguments `mcp_server.memory_fact_set` passes: `origin or
    /// "agent"`, confidence 0.8 by default, the episode, the class and both
    /// labels as strings; the rest stay neutral.
    fn bind(a: &Map<String, Value>) -> CortexWriteArgs {
        let text = |k: &str, d: &str| a.get(k).and_then(Value::as_str).unwrap_or(d).to_string();
        let mut w = CortexWriteArgs::new(
            &text("entity", ""),
            &text("attribute", ""),
            &text("value", ""),
        );
        w.support = Some(text("origin", "agent"));
        w.confidence = a
            .get("confidence")
            .and_then(crate::mcp::binding::float_arg)
            .unwrap_or(0.8);
        w.episode = a.get("episode").and_then(Value::as_str).map(str::to_string);
        w.freshness_class = text("freshness_class", "auto");
        w.authority = LabelArg::Requested(Some(text("authority", "auto")));
        w.distortion_tolerance = LabelArg::Requested(Some(text("distortion_tolerance", "auto")));
        w
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
        let args = CortexWriteArgs::bind(call.args);
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

pub(crate) async fn embed(ready: &Ready, text: String) -> Result<Arc<[f32]>, ToolError> {
    let embedder = ready.embedder.clone();
    let row = tokio::task::spawn_blocking(move || embedder.embed(&text))
        .await
        .map_err(internal)?
        .map_err(internal)?;
    Ok(Arc::from(row))
}

/// `service.cortex_write` (`service.py:3916-4040`), under the write lock
/// (the Python service lock) from the freshness lookup to the answer: the
/// label resolution before it, then the class, both embeddings, the writer
/// and episode stamps, an HLC tick, the store write, the entity mint, the
/// per-slot save and, for a direct user supersede (never a dream write),
/// the correction signal. A set-valued slot answers `ToolError::Invalid`
/// with `SET_SLOT_REFUSAL`. `memory_fact_set` calls it with the neutral
/// inputs; the dream's claim writes call it with theirs.
pub(crate) async fn cortex_write(
    svc: &Service,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: String,
    a: CortexWriteArgs,
) -> Result<Value, ToolError> {
    let authority = a
        .authority
        .resolve(&a.value, crate::labels::resolve_authority)
        .map_err(ToolError::Invalid)?;
    let distortion = a
        .distortion_tolerance
        .resolve(&a.value, crate::labels::resolve_distortion)
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
        support: norm_support(a.support.as_deref()),
        now: episodes::now(),
        hlc,
        writer_id: Some(writer),
        session_id: session.clone(),
        freshness_class,
        authority,
        distortion_tolerance: distortion,
        provenance: a.provenance.clone(),
        stance: a.stance.clone(),
        force_contend: a.force_contend,
        dream: a.dream,
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
        && !a.dream
        && py_strip(a.support.as_deref().unwrap_or("")).to_lowercase() == "user"
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
pub(crate) async fn ensure_subject_entity(db: &Client, entity: &str) -> anyhow::Result<()> {
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
pub(crate) async fn save_cortex(svc: &Service, ready: &Ready) -> Result<(), ToolError> {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_mcp_binding_passes_the_neutral_dream_inputs() {
        let a = CortexWriteArgs::bind(
            json!({"entity": "db", "attribute": "host", "value": "a", "confidence": 0.6})
                .as_object()
                .unwrap(),
        );
        assert_eq!(a.support.as_deref(), Some("agent"));
        assert_eq!(a.confidence, 0.6);
        assert!(a.provenance.is_empty() && a.stance.is_none());
        assert!(!a.force_contend && !a.dream);
        assert_eq!(a.freshness_class, "auto");
        for label in [&a.authority, &a.distortion_tolerance] {
            assert!(matches!(label, LabelArg::Requested(Some(v)) if v == "auto"));
        }
        let d = CortexWriteArgs::new("db", "host", "a");
        assert_eq!((d.confidence, d.support), (0.7, None));
    }

    #[test]
    fn label_args_resolve_like_python() {
        use crate::labels::{Resolved, resolve_authority};
        let r = |l: LabelArg, v: &str| l.resolve(v, resolve_authority);
        assert_eq!(r(LabelArg::Inherit, "you must").unwrap(), Resolved::Inherit);
        assert_eq!(
            r(LabelArg::Requested(None), "x").unwrap(),
            Resolved::Set(None)
        );
        assert_eq!(
            r(LabelArg::Requested(Some(" Quoted ".into())), "x").unwrap(),
            Resolved::Set(Some("quoted".into()))
        );
        assert!(r(LabelArg::Requested(Some("loud".into())), "x").is_err());
    }
}
