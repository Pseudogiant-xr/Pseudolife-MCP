//! `memory_graph_review`'s slot actions (`mcp_server.py:2376-2399`):
//!
//! * `dismiss_slot_pair`: `service.curation_dismiss_duplicate`
//!   (`service.py:7944-7967`) over `curation_safety.mark_human_curation_dismissal`
//!   (`curation_safety.py:531-545`): the namespaced `dismissed_pairs` row and
//!   the rewrite of the automatic-dismissal markers (always, even as `{}`),
//!   one transaction.
//! * `restore_slot`: `service.lesson_restore` / `world_restore`
//!   (`service.py:5476-5604`, `4898-4943`). First
//!   `curation_safety.restore_curated_duplicate` (`1140-1231`), which owns
//!   a target whose retire row carries `_curation_duplicate`: it stages the
//!   restore on a copy of the resident store, validates the locked rows
//!   against the retire row's evidence, rewrites the slots and records the
//!   restore in one transaction, and publishes the copy only after the
//!   commit. When that transaction fails, the store is reloaded from the
//!   database and classified (`_restore_recovery_state`): a commit that
//!   landed publishes the reload and answers `source: "reconciled"`; a
//!   rollback raises `CurationReconciliationError` (`internal_error`).
//!   Otherwise the legacy path: the resident store's `restore`, one
//!   `store_decisions` row per restored record (each its own transaction),
//!   the lesson graph link, then the slot save; the resident store changes
//!   before its writes commit, as Python's does.
//!
//! The audit-snapshot fallback (`_restore_from_snapshot`, `5553-5604`), for
//! retired slots whose row compaction purged, rewrites each snapshot
//! through the store's own writer: `world_write` for world facts and
//! `lesson_write` (`crate::lesson_write`) for lessons.

// The bodies raise `ToolError` as the action modules do (`internal`).
#![allow(clippy::result_large_err)]

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio_postgres::Client;

use super::actions::{dismiss_pair_body, in_txn, py_dict, py_int, reads, set_meta_body};
use super::{get_meta, internal, py_json, py_truthy, py_type};
use crate::episodes::{Caller, WriteState};
#[cfg(test)]
use crate::lesson_write::compound_halves;
use crate::lesson_write::link_lesson_graph;
use crate::mcp::dispatch::ToolError;
use crate::pyjson::py_str;
use crate::service::{Ready, Service};
use crate::stores::cortex::{SlotKey, norm_key, slot_key, unit_f32};
use crate::stores::lessons::{LessonRecord, LessonStore};
use crate::stores::world::{WorldRecord, WorldStore};

/// `service._SLOT_KEY_PIPE`: a literal `|` inside a slot-key component.
const SLOT_KEY_PIPE: &str = "%7C";
/// `curation_safety._AUTO_DISMISSALS_META`.
const AUTO_DISMISSALS_META: &str = "curation_auto_dismissals_v1";
/// `decided_by` for every call from the MCP surface.
const DECIDED_BY: &str = "agent";
/// `retired_slots(..., limit=100)`.
const RETIRED_SLOTS_LIMIT: usize = 100;

const DISMISS_DETAIL: &str = "store='lesson'|'world'; src/dst are 'entity|attribute' keys from \
     the deep response (a literal '|' in a name is spelled %7C)";
const RESTORE_DETAIL: &str = "store='lesson'|'world'; src is the retired 'entity|attribute' key \
     (or a bare entity to restore every retired aspect of it); a literal '|' in a name is \
     spelled %7C";

/// `curation_safety._COMMON_FIELDS`.
const COMMON_FIELDS: [&str; 21] = [
    "entity",
    "attribute",
    "value",
    "polarity",
    "status",
    "confidence",
    "origin",
    "support",
    "provenance",
    "asserted_at",
    "last_confirmed",
    "supersedes_value",
    "superseded_by_value",
    "superseded_at",
    "tx_time",
    "valid_time",
    "hlc_phys",
    "hlc_logical",
    "writer_id",
    "session_id",
    "version",
];
/// `curation_safety._LESSON_FIELDS`.
const LESSON_FIELDS: [&str; 2] = ["about", "outcome"];
/// `curation_safety._WORLD_FIELDS`.
const WORLD_FIELDS: [&str; 6] = [
    "source_url",
    "source_quote",
    "retrieved_at",
    "freshness_class",
    "content_hash",
    "source_doc_id",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Store {
    Lesson,
    World,
}

impl Store {
    fn parse(s: &str) -> Option<Self> {
        match s {
            "lesson" => Some(Store::Lesson),
            "world" => Some(Store::World),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Store::Lesson => "lesson",
            Store::World => "world",
        }
    }
}

#[derive(Debug)]
pub(super) enum SlotPlan {
    Dismiss {
        store: String,
        a: (String, String),
        b: (String, String),
    },
    Restore {
        store: Store,
        entity: String,
        attribute: Option<String>,
    },
}

/// `service._parse_slot_key`: `(entity_norm, attribute_norm)` for a key
/// with exactly one bare `|`, each `%7C` decoded back to a `|`.
pub(super) fn parse_slot_key(key: &str) -> Option<(String, String)> {
    let parts: Vec<&str> = key.split('|').collect();
    match parts.as_slice() {
        [e, a] => Some((e.replace(SLOT_KEY_PIPE, "|"), a.replace(SLOT_KEY_PIPE, "|"))),
        _ => None,
    }
}

/// `service._slot_key`: the components joined by `|`, a literal `|` in one
/// spelled `%7C`.
pub(crate) fn slot_key_text(entity_norm: &str, attribute_norm: &str) -> String {
    format!(
        "{}|{}",
        entity_norm.replace('|', SLOT_KEY_PIPE),
        attribute_norm.replace('|', SLOT_KEY_PIPE)
    )
}

/// The tool's argument checks for the two slot actions, in `mcp_server`'s
/// order. `Err` is the answer Python returns before calling the service.
pub(super) fn plan(action: &str, a: &Map<String, Value>) -> Result<SlotPlan, Value> {
    let text = |k: &str| a.get(k).and_then(Value::as_str).filter(|s| !s.is_empty());
    let (store, src) = (text("store"), text("src"));
    if action == "dismiss_slot_pair" {
        let pa = src.and_then(parse_slot_key);
        let pb = text("dst").and_then(parse_slot_key);
        return match (store, pa, pb) {
            (Some(store), Some(a), Some(b)) => Ok(SlotPlan::Dismiss {
                store: store.to_string(),
                a,
                b,
            }),
            _ => Err(json!({"error": "store_src_dst_required", "detail": DISMISS_DETAIL})),
        };
    }
    let key = src.filter(|s| s.contains('|')).and_then(parse_slot_key);
    match (store.and_then(Store::parse), src) {
        (Some(store), Some(src)) if !(src.contains('|') && key.is_none()) => {
            let (entity, attribute) = match key {
                Some((e, a)) => (e, Some(a)),
                None => (src.replace(SLOT_KEY_PIPE, "|"), None),
            };
            Ok(SlotPlan::Restore {
                store,
                entity,
                // `attr or None`.
                attribute: attribute.filter(|a| !a.is_empty()),
            })
        }
        _ => Err(json!({"error": "store_src_required", "detail": RESTORE_DETAIL})),
    }
}

/// Run a planned slot action under the writer lock (`ws`), answering with
/// `_noted(..., _REVIEW_NOTES)`.
pub(super) async fn run(
    svc: &Service,
    ready: &Ready,
    ws: &WriteState,
    caller: &Caller,
    writer: &str,
    plan: SlotPlan,
) -> Result<Value, ToolError> {
    let out = match plan {
        SlotPlan::Dismiss { store, a, b } => dismiss(ready, &store, &a, &b).await?,
        SlotPlan::Restore {
            store,
            entity,
            attribute,
        } => {
            let w = Writer { ws, caller, writer };
            restore(svc, ready, &w, store, &entity, attribute.as_deref()).await?
        }
    };
    Ok(super::actions::noted(out))
}

/// The caller identity a `world_write` stamps (`_resolve_writer`).
struct Writer<'a> {
    ws: &'a WriteState,
    caller: &'a Caller,
    writer: &'a str,
}

// ── dismiss_slot_pair ────────────────────────────────────────────────────

/// `service.curation_dismiss_duplicate` with the listing keys decoded.
async fn dismiss(
    ready: &Ready,
    store: &str,
    a: &(String, String),
    b: &(String, String),
) -> Result<Value, ToolError> {
    if Store::parse(store).is_none() {
        return Ok(json!({"dismissed": false, "reason": "bad_store", "store": store}));
    }
    let (an, aa) = (norm_key(&a.0), norm_key(&a.1));
    let (bn, ba) = (norm_key(&b.0), norm_key(&b.1));
    if an.is_empty() || aa.is_empty() || bn.is_empty() || ba.is_empty() || (an == bn && aa == ba) {
        return Ok(json!({"dismissed": false, "reason": "bad_pair", "store": store}));
    }
    let (a_key, b_key) = (slot_key_text(&an, &aa), slot_key_text(&bn, &ba));
    let new = {
        let (a_key, b_key) = (&a_key, &b_key);
        in_txn(ready.storage.client(), |c| async move {
            mark_human_dismissal(c, store, a_key, b_key).await
        })
        .await?
    };
    Ok(json!({"dismissed": true, "new": new, "store": store, "a_key": a_key, "b_key": b_key}))
}

/// `mark_human_curation_dismissal` without its transaction: the dismissal,
/// then every automatic marker on the same pair dropped and the markers
/// written back, even when none matched.
async fn mark_human_dismissal(
    c: &Client,
    store: &str,
    a_key: &str,
    b_key: &str,
) -> Result<bool, ToolError> {
    let mut target = [a_key, b_key];
    target.sort();
    let new =
        dismiss_pair_body(c, &format!("{store}:{a_key}"), &format!("{store}:{b_key}")).await?;
    let stored = get_meta(c, AUTO_DISMISSALS_META)
        .await
        .map_err(internal)?
        .map(py_json);
    let markers = py_dict(stored.as_ref()).map_err(ToolError::Invalid)?;
    let before = markers.len();
    let mut kept = Map::new();
    for (id, marker) in markers {
        let Value::Object(m) = &marker else {
            return Err(internal(format!(
                "AttributeError: '{}' object has no attribute 'get'",
                py_type(&marker)
            )));
        };
        if m.get("store").and_then(Value::as_str) == Some(store) && marker_pair_is(m, &target)? {
            continue;
        }
        kept.insert(id, marker);
    }
    if kept.len() == before && crate::mutants::active("mcp-review-slot-meta-noop") {
        return Ok(new);
    }
    set_meta_body(c, AUTO_DISMISSALS_META, &Value::Object(kept)).await?;
    Ok(new)
}

/// `sorted(((marker.get("a") or {}).get("key"), (marker.get("b") or
/// {}).get("key"))) == target`. Two strings compare; two numbers (or
/// booleans) sort but never equal two strings; any other mix raises
/// Python's `TypeError` (`'<' not supported`), as does a non-dict side.
/// (Two lists would sort elementwise in Python; a marker never holds one,
/// and here it raises.)
fn marker_pair_is(m: &Map<String, Value>, target: &[&str; 2]) -> Result<bool, ToolError> {
    let side = |k: &str| -> Result<Value, ToolError> {
        match m.get(k) {
            Some(v) if py_truthy(v) => match v {
                Value::Object(o) => Ok(o.get("key").cloned().unwrap_or(Value::Null)),
                other => Err(internal(format!(
                    "AttributeError: '{}' object has no attribute 'get'",
                    py_type(other)
                ))),
            },
            _ => Ok(Value::Null),
        }
    };
    let (ka, kb) = (side("a")?, side("b")?);
    let numeric = |v: &Value| matches!(v, Value::Number(_) | Value::Bool(_));
    match (&ka, &kb) {
        (Value::String(x), Value::String(y)) => {
            let mut pair = [x.as_str(), y.as_str()];
            pair.sort();
            Ok(&pair == target)
        }
        (x, y) if numeric(x) && numeric(y) => Ok(false),
        // A TypeError is `invalid_argument` with its text (`_error_payload`);
        // the sort compares the second key with the first.
        (x, y) => Err(ToolError::Invalid(format!(
            "'<' not supported between instances of '{}' and '{}'",
            py_type(y),
            py_type(x)
        ))),
    }
}

// ── restore_slot ─────────────────────────────────────────────────────────

/// One `store_decisions` row as `retired_slots` returns it.
#[derive(Debug, Clone)]
struct Decision {
    id: i64,
    entity_norm: String,
    attribute_norm: String,
    action: String,
    record: Option<Value>,
    decided_at: f64,
}

/// The latest decision per slot of the entity (`DISTINCT ON` the slot,
/// newest `decided_at`, then `id`), any action.
async fn latest_slot_decisions(
    client: &Client,
    store: Store,
    entity_norm: &str,
) -> Result<Vec<Decision>, ToolError> {
    let rows = reads(client, |c| async move {
        c.query(
            "SELECT DISTINCT ON (entity_norm, attribute_norm) id, entity_norm, attribute_norm, \
             action, record, decided_at FROM store_decisions \
             WHERE store = $1 AND entity_norm = $2 \
             ORDER BY entity_norm, attribute_norm, decided_at DESC, id DESC",
            &[&store.name(), &entity_norm],
        )
        .await
        .map_err(internal)
    })
    .await?;
    rows.iter()
        .map(|r| {
            Ok(Decision {
                id: r.get(0),
                entity_norm: r.get(1),
                attribute_norm: r.get(2),
                action: r.get(3),
                record: r.get::<_, Option<Value>>(4).map(py_json),
                decided_at: r
                    .get::<_, Option<f64>>(5)
                    .ok_or_else(|| internal("float() argument must be a number, not 'NoneType'"))?,
            })
        })
        .collect()
}

/// `retired_slots` (`storage/postgres.py:3563-3579`) over the latest
/// decisions: the retires, newest first, the first hundred.
fn retired_only(latest: Vec<Decision>) -> Vec<Decision> {
    let mut out: Vec<Decision> = latest
        .into_iter()
        .filter(|d| d.action == "retire")
        .collect();
    out.sort_by(|x, y| {
        y.decided_at
            .partial_cmp(&x.decided_at)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(y.id.cmp(&x.id))
    });
    out.truncate(RETIRED_SLOTS_LIMIT);
    out
}

async fn retired_slots(
    client: &Client,
    store: Store,
    entity_norm: &str,
) -> Result<Vec<Decision>, ToolError> {
    Ok(retired_only(
        latest_slot_decisions(client, store, entity_norm).await?,
    ))
}

/// The snapshot rows `lesson_restore` / `world_restore` hand the fallback:
/// retired slots of the entity (or the one aspect) with a record, whose
/// slot is not live.
fn fallback_snaps(
    retired: Vec<Decision>,
    attribute_norm: Option<&str>,
    live: &HashSet<SlotKey>,
) -> Vec<Decision> {
    retired
        .into_iter()
        .filter(|d| {
            attribute_norm.is_none_or(|a| d.attribute_norm == a)
                && d.record.as_ref().is_some_and(py_truthy)
                && !live.contains(&(d.entity_norm.clone(), d.attribute_norm.clone()))
        })
        .collect()
}

fn label(store: Store, entity: &str, attribute: Option<&str>) -> [(&'static str, Value); 2] {
    match store {
        Store::Lesson => [("task", json!(entity)), ("aspect", json!(attribute))],
        Store::World => [("entity", json!(entity)), ("attribute", json!(attribute))],
    }
}

/// `service._restore_result`.
fn restore_result(
    store: Store,
    entity: &str,
    attribute: Option<&str>,
    recs: usize,
    mut entries: Vec<Value>,
    fallback: Vec<Value>,
    live: bool,
) -> Value {
    let total = recs + fallback.len();
    let mut out = Map::new();
    out.insert("restored".into(), json!(total));
    if total == 0 {
        out.extend(label(store, entity, attribute).map(|(k, v)| (k.to_string(), v)));
        let reason = if live { "slot_live" } else { "nothing_retired" };
        out.insert("reason".into(), json!(reason));
        return Value::Object(out);
    }
    let source = if fallback.is_empty() {
        "retired_record"
    } else if recs == 0 {
        "audit_snapshot"
    } else {
        "mixed"
    };
    out.insert("source".into(), json!(source));
    out.extend(label(store, entity, attribute).map(|(k, v)| (k.to_string(), v)));
    entries.extend(fallback);
    out.insert("entries".into(), Value::Array(entries));
    Value::Object(out)
}

/// A restore audit row (`_record_store_decision` with `action="restore"`),
/// its own transaction.
async fn write_restore_decision(
    ready: &Ready,
    store: Store,
    key: &SlotKey,
    reason: Option<&str>,
    record: &Value,
    now: f64,
) -> Result<(), ToolError> {
    crate::txn::run(ready.storage.client(), |c| async move {
        crate::forget_tools::record_store_decision(
            c,
            store.name(),
            (&key.0, &key.1),
            "restore",
            Some(DECIDED_BY),
            reason,
            record,
            now,
        )
        .await
    })
    .await
    .map_err(internal)
}

/// `service.lesson_restore` / `world_restore` with `decided_by="agent"`.
async fn restore(
    svc: &Service,
    ready: &Ready,
    w: &Writer<'_>,
    store: Store,
    entity: &str,
    attribute: Option<&str>,
) -> Result<Value, ToolError> {
    // `_ensure_init`'s read barrier for committed cascade retirements.
    crate::forget_tools::ensure_lessons_refreshed(ready)
        .await
        .map_err(internal)?;
    if let Some(out) = restore_curated(svc, ready, store, entity, attribute).await? {
        return Ok(out);
    }
    match store {
        Store::Lesson => restore_lessons(svc, ready, w, entity, attribute).await,
        Store::World => restore_world(svc, ready, w, entity, attribute).await,
    }
}

/// A lesson the legacy restore brought back: its audit snapshot, what the
/// graph link reads, and its answer entry.
struct Restored {
    key: SlotKey,
    audit: Value,
    task: String,
    about: Option<String>,
    polarity: String,
    entry: Value,
}

/// The lesson legacy path, then the audit-snapshot fallback for the retired
/// slots it could not cover (`service.lesson_restore`).
async fn restore_lessons(
    svc: &Service,
    ready: &Ready,
    w: &Writer<'_>,
    task: &str,
    aspect: Option<&str>,
) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let ne = norm_key(task);
    let na = aspect.map(norm_key);
    let now = crate::episodes::now();
    let recs: Vec<Restored> = ready.lessons.write(|l| {
        l.restore(task, aspect)
            .into_iter()
            .map(|i| {
                let r = &l.records[i];
                Restored {
                    key: r.key.clone(),
                    audit: r.audit_snapshot(),
                    task: r.entity.clone(),
                    about: r.about.clone(),
                    polarity: r.polarity.clone(),
                    entry: Value::Object(crate::lesson_tools::record_to_dict(r)),
                }
            })
            .collect()
    });
    if !recs.is_empty() {
        for r in &recs {
            if !crate::mutants::active("mcp-review-slot-no-decision") {
                write_restore_decision(ready, Store::Lesson, &r.key, None, &r.audit, now).await?;
            }
            link_lesson_graph(client, &r.task, r.about.as_deref(), &r.polarity).await?;
        }
        crate::forget_tools::save_lessons(svc, ready).await?;
    }
    let live: HashSet<SlotKey> = ready
        .lessons
        .snapshot()
        .current_records()
        .filter(|r| r.key.0 == ne && na.as_ref().is_none_or(|a| r.key.1 == *a))
        .map(|r| r.key.clone())
        .collect();
    let snaps = fallback_snaps(
        retired_slots(client, Store::Lesson, &ne).await?,
        na.as_deref(),
        &live,
    );
    let entries: Vec<Value> = recs.into_iter().map(|r| r.entry).collect();
    let n = entries.len();
    let fallback = if snaps.is_empty() {
        Vec::new()
    } else {
        lessons_from_snapshots(svc, ready, w, &snaps).await?
    };
    Ok(restore_result(
        Store::Lesson,
        task,
        aspect,
        n,
        entries,
        fallback,
        !live.is_empty(),
    ))
}

/// The world legacy path, then the audit-snapshot fallback for the retired
/// slots it could not cover.
async fn restore_world(
    svc: &Service,
    ready: &Ready,
    w: &Writer<'_>,
    entity: &str,
    attribute: Option<&str>,
) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let ne = norm_key(entity);
    let na = attribute.map(norm_key);
    let now = crate::episodes::now();
    let recs: Vec<(usize, SlotKey, Value)> = ready.world.write(|s| {
        s.restore(entity, attribute)
            .into_iter()
            .map(|i| (i, s.records[i].key.clone(), s.records[i].audit_snapshot()))
            .collect()
    });
    if !recs.is_empty() {
        for (_, key, snapshot) in &recs {
            if !crate::mutants::active("mcp-review-slot-no-decision") {
                write_restore_decision(ready, Store::World, key, None, snapshot, now).await?;
            }
        }
        crate::world_tools::save_world(svc, ready).await?;
    }
    let live: HashSet<SlotKey> = ready
        .world
        .snapshot()
        .records
        .iter()
        .filter(|r| {
            r.status == "current" && r.key.0 == ne && na.as_ref().is_none_or(|a| r.key.1 == *a)
        })
        .map(|r| r.key.clone())
        .collect();
    let snaps = fallback_snaps(
        retired_slots(client, Store::World, &ne).await?,
        na.as_deref(),
        &live,
    );
    let entries: Vec<Value> = {
        let snap = ready.world.snapshot();
        let policy = &svc.config.memory.search.stale_policy;
        recs.iter()
            .map(|(i, _, _)| {
                Value::Object(crate::world_tools::record_to_dict(
                    &snap.records[*i],
                    policy,
                    crate::episodes::now(),
                ))
            })
            .collect()
    };
    let fallback = if snaps.is_empty() {
        Vec::new()
    } else {
        world_from_snapshots(svc, ready, w, &snaps).await?
    };
    Ok(restore_result(
        Store::World,
        entity,
        attribute,
        recs.len(),
        entries,
        fallback,
        !live.is_empty(),
    ))
}

/// Python's `float(x)` for a decoded JSON value.
fn py_float(v: &Value) -> Result<f64, ToolError> {
    let x = match v {
        Value::Bool(b) => f64::from(u8::from(*b)),
        Value::Number(n) => n.to_string().parse::<f64>().map_err(internal)?,
        Value::String(s) => crate::store::py_strip(s).parse::<f64>().map_err(|_| {
            ToolError::Invalid(format!(
                "could not convert string to float: {}",
                crate::storage::py_repr(s)
            ))
        })?,
        other => {
            return Err(ToolError::Invalid(format!(
                "float() argument must be a string or a real number, not '{}'",
                py_type(other)
            )));
        }
    };
    Ok(x)
}

/// `float(x or default)`.
fn float_or(v: Option<&Value>, default: f64) -> Result<f64, ToolError> {
    match v {
        Some(v) if py_truthy(v) => py_float(v),
        _ => Ok(default),
    }
}

/// A string field as Python passes it on (`x or default` when `default`).
fn str_field(
    rec: &Map<String, Value>,
    k: &str,
    default: Option<&str>,
) -> Result<Option<String>, ToolError> {
    match rec.get(k) {
        Some(Value::String(s)) if !s.is_empty() || default.is_none() => Ok(Some(s.clone())),
        Some(v) if py_truthy(v) => Err(internal(format!(
            "unsupported {k} of type '{}' in the audit snapshot",
            py_type(v)
        ))),
        _ => Ok(default.map(str::to_string)),
    }
}

/// `rec[k]` for a required string.
fn str_key(rec: &Map<String, Value>, k: &str) -> Result<String, ToolError> {
    match rec.get(k) {
        Some(Value::String(s)) => Ok(s.clone()),
        Some(v) => Err(internal(format!(
            "unsupported {k} of type '{}' in the audit snapshot",
            py_type(v)
        ))),
        None => Err(internal(format!("KeyError: '{k}'"))),
    }
}

/// `int(x)` for an optional integer field (`None` stays `None`).
fn opt_int(v: Option<&Value>) -> Result<Option<i64>, ToolError> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(v) => py_int(v)
            .map_err(ToolError::Invalid)?
            .as_i64()
            .map(Some)
            .ok_or_else(|| internal("integer out of range")),
    }
}

/// A snapshot's record as a dict, or the error Python's `rec["..."]`
/// raises on what else a truthy record can be.
fn snapshot_dict(d: &Decision) -> Result<&Map<String, Value>, ToolError> {
    match &d.record {
        Some(Value::Object(rec)) => Ok(rec),
        Some(Value::Array(_)) => Err(ToolError::Invalid(
            "list indices must be integers or slices, not str".into(),
        )),
        Some(Value::String(_)) => Err(ToolError::Invalid(
            "string indices must be integers, not 'str'".into(),
        )),
        other => Err(ToolError::Invalid(format!(
            "'{}' object is not subscriptable",
            other.as_ref().map_or("NoneType", py_type)
        ))),
    }
}

/// `rec.get(k) or None` for a provenance or support list: its strings.
fn str_list(rec: &Map<String, Value>, k: &str) -> Result<Vec<String>, ToolError> {
    match rec.get(k) {
        Some(Value::Array(items)) => items
            .iter()
            .map(|v| match v {
                Value::String(s) => Ok(s.clone()),
                other => Err(internal(format!(
                    "unsupported {k} item of type '{}' in the audit snapshot",
                    py_type(other)
                ))),
            })
            .collect(),
        Some(v) if py_truthy(v) => Err(internal(format!(
            "unsupported {k} of type '{}' in the audit snapshot",
            py_type(v)
        ))),
        _ => Ok(Vec::new()),
    }
}

/// `_restore_from_snapshot` for lessons: each snapshot re-written through
/// `lesson_write` (one embedding, HLC tick, graph link and slot save each;
/// `lesson_write` never rejects), then one restore audit row per slot
/// (`reason="from audit snapshot"`, the verbatim record). Returns the
/// snapshots' records.
async fn lessons_from_snapshots(
    svc: &Service,
    ready: &Ready,
    w: &Writer<'_>,
    snaps: &[Decision],
) -> Result<Vec<Value>, ToolError> {
    for d in snaps {
        let rec = snapshot_dict(d)?;
        let task = str_key(rec, "task")?;
        let aspect = str_key(rec, "aspect")?;
        let lesson = str_key(rec, "lesson")?;
        let about = match rec.get("about") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(v) => {
                return Err(internal(format!(
                    "unsupported about of type '{}' in the audit snapshot",
                    py_type(v)
                )));
            }
        };
        let outcome = str_field(rec, "outcome", Some("success"))?.unwrap_or_default();
        let polarity = str_field(rec, "polarity", Some("+"))?.unwrap_or_default();
        let confidence = float_or(rec.get("confidence"), 0.6)?;
        let origin = str_field(rec, "origin", Some("agent"))?.unwrap_or_default();
        let provenance = str_list(rec, "provenance")?;
        let support = str_list(rec, "support")?;
        let args = crate::lesson_write::LessonWriteArgs {
            task: &task,
            aspect: &aspect,
            lesson: &lesson,
            about: about.as_deref(),
            outcome: &outcome,
            polarity: &polarity,
            confidence,
            origin: &origin,
            provenance: &provenance,
            support: &support,
        };
        if !crate::mutants::active("mcp-dream-stage-snapshot-restore-skipped") {
            crate::lesson_write::lesson_write(svc, ready, w.ws, w.caller, w.writer, &args).await?;
        }
    }
    let now = crate::episodes::now();
    for d in snaps {
        let key = (d.entity_norm.clone(), d.attribute_norm.clone());
        let record = d.record.clone().unwrap_or(Value::Null);
        write_restore_decision(
            ready,
            Store::Lesson,
            &key,
            Some("from audit snapshot"),
            &record,
            now,
        )
        .await?;
    }
    Ok(snaps
        .iter()
        .map(|d| d.record.clone().unwrap_or(Value::Null))
        .collect())
}

/// `_restore_from_snapshot` for world facts: each snapshot re-written
/// through `world_write` (one embedding, HLC tick and slot save each; an
/// unsafe `source_url` is rejected and skipped), then one restore audit row
/// per written slot (`reason="from audit snapshot"`, the verbatim record).
/// Returns the written snapshots' records.
async fn world_from_snapshots(
    svc: &Service,
    ready: &Ready,
    w: &Writer<'_>,
    snaps: &[Decision],
) -> Result<Vec<Value>, ToolError> {
    let mut restored: Vec<&Decision> = Vec::new();
    for d in snaps {
        let rec = match &d.record {
            Some(Value::Object(rec)) => rec,
            // `rec["entity"]` on what a truthy record can be besides a dict.
            Some(Value::Array(_)) => {
                return Err(ToolError::Invalid(
                    "list indices must be integers or slices, not str".into(),
                ));
            }
            Some(Value::String(_)) => {
                return Err(ToolError::Invalid(
                    "string indices must be integers, not 'str'".into(),
                ));
            }
            other => {
                return Err(ToolError::Invalid(format!(
                    "'{}' object is not subscriptable",
                    other.as_ref().map_or("NoneType", py_type)
                )));
            }
        };
        let entity = str_key(rec, "entity")?;
        let attribute = str_key(rec, "attribute")?;
        let value = str_key(rec, "value")?;
        let confidence = float_or(rec.get("confidence"), 0.7)?;
        let source_url = str_field(rec, "source_url", Some(""))?.unwrap_or_default();
        let source_quote = str_field(rec, "source_quote", Some(""))?.unwrap_or_default();
        let freshness = str_field(rec, "freshness_class", Some("volatile"))?.unwrap_or_default();
        let retrieved_at = match rec.get("retrieved_at") {
            None | Some(Value::Null) => None,
            Some(v) => Some(py_float(v)?),
        };
        let content_hash = match rec.get("content_hash") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(v) => {
                return Err(internal(format!(
                    "unsupported content_hash of type '{}' in the audit snapshot",
                    py_type(v)
                )));
            }
        };
        let source_doc_id = opt_int(rec.get("source_doc_id"))?;
        // `world_write` refuses an unsafe citation before it locks.
        if !crate::world_tools::safe_source_url(&source_url) {
            continue;
        }
        let args = crate::world_tools::WorldWriteArgs {
            entity: &entity,
            attribute: &attribute,
            value: &value,
            confidence,
            source_url: &source_url,
            source_quote: &source_quote,
            freshness_class: &freshness,
            retrieved_at,
            content_hash: content_hash.as_deref(),
            source_doc_id,
        };
        crate::world_tools::write_locked(svc, ready, w.ws, w.caller, w.writer, &args).await?;
        restored.push(d);
    }
    let now = crate::episodes::now();
    for d in &restored {
        let key = (d.entity_norm.clone(), d.attribute_norm.clone());
        let record = d.record.clone().unwrap_or(Value::Null);
        write_restore_decision(
            ready,
            Store::World,
            &key,
            Some("from audit snapshot"),
            &record,
            now,
        )
        .await?;
    }
    Ok(restored
        .iter()
        .map(|d| d.record.clone().unwrap_or(Value::Null))
        .collect())
}

// ── curation evidence (curation_safety.py) ───────────────────────────────

/// `_jsonable` then `json.dumps(sort_keys=True, separators=(",", ":"),
/// ensure_ascii=False, allow_nan=False)`.
fn canonical_json(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&crate::pyjson::number_text(n)),
        Value::String(s) => canonical_str(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical_json(out, item);
            }
            out.push(']');
        }
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical_str(out, k);
                out.push(':');
                canonical_json(out, &m[k]);
            }
            out.push('}');
        }
    }
}

/// `json`'s `ensure_ascii=False` string: only `"`, `\` and the C0 controls
/// are escaped.
fn canonical_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `evidence_fingerprint`: the SHA-256 of the canonical JSON.
pub(super) fn fingerprint(record: &Map<String, Value>) -> String {
    let mut text = String::new();
    canonical_json(&mut text, &Value::Object(record.clone()));
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// `_embedding_snapshot` of a float32 vector.
fn embedding_snapshot(embedding: Option<&[f32]>) -> Value {
    let Some(e) = embedding else {
        return Value::Null;
    };
    let mut bytes = Vec::with_capacity(e.len() * 4);
    for x in e {
        bytes.extend_from_slice(&x.to_le_bytes());
    }
    json!({"dtype": "float32", "shape": [e.len()], "sha256": hex::encode(Sha256::digest(&bytes))})
}

/// A finite float as a JSON number (`allow_nan=False` refuses the rest).
fn finite(x: f64) -> Result<Value, ToolError> {
    if x.is_finite() {
        Ok(json!(x))
    } else {
        Err(ToolError::Invalid(
            "Out of range float values are not JSON compliant".into(),
        ))
    }
}

/// `snapshot_record(store, record)` over the record as `_get` sees it: `d`
/// holds every attribute (or column) the record has, an absent one taking
/// the default; the embedding is passed apart.
pub(super) fn snapshot_record(
    store: Store,
    d: &Map<String, Value>,
    embedding: Option<&[f32]>,
) -> Result<Map<String, Value>, ToolError> {
    let text = |k: &str| d.get(k).map_or_else(String::new, py_str);
    let (en, an) = (norm_key(&text("entity")), norm_key(&text("attribute")));
    let mut out = Map::new();
    out.insert("store".into(), json!(store.name()));
    out.insert("key".into(), json!(format!("{en}|{an}")));
    out.insert("entity_norm".into(), json!(en));
    out.insert("attribute_norm".into(), json!(an));
    for name in COMMON_FIELDS {
        let value = match d.get(name) {
            Some(v) => v.clone(),
            None => match name {
                "polarity" => json!("+"),
                "status" => json!("current"),
                "confidence" => json!(0.0),
                "support" if store == Store::World => json!(["source"]),
                "support" | "provenance" => json!([]),
                "version" => json!(1),
                _ => Value::Null,
            },
        };
        let value = match name {
            "support" | "provenance" => {
                let mut items: Vec<String> = match &value {
                    v if !py_truthy(v) => Vec::new(),
                    Value::Array(a) => a.iter().map(py_str).collect(),
                    Value::String(s) => s.chars().map(String::from).collect(),
                    Value::Object(m) => m.keys().cloned().collect(),
                    other => {
                        return Err(ToolError::Invalid(format!(
                            "'{}' object is not iterable",
                            py_type(other)
                        )));
                    }
                };
                items.sort();
                json!(items)
            }
            "confidence" => finite(round6(float_or(Some(&value), 0.0)?))?,
            "hlc_phys" | "hlc_logical" | "version" => match value {
                Value::Null => Value::Null,
                v => py_int(&v).map_err(ToolError::Invalid)?,
            },
            "asserted_at" | "last_confirmed" | "superseded_at" | "tx_time" | "valid_time" => {
                match value {
                    Value::Null => Value::Null,
                    v => finite(py_float(&v)?)?,
                }
            }
            _ => value,
        };
        out.insert(name.into(), value);
    }
    let fields: &[&str] = match store {
        Store::Lesson => &LESSON_FIELDS,
        Store::World => &WORLD_FIELDS,
    };
    for &name in fields {
        let value = d.get(name).cloned().unwrap_or(Value::Null);
        let value = match name {
            "retrieved_at" => finite(float_or(Some(&value), 0.0)?)?,
            "source_doc_id" => match value {
                Value::Null => Value::Null,
                v => py_int(&v).map_err(ToolError::Invalid)?,
            },
            _ => value,
        };
        out.insert(name.into(), value);
    }
    out.insert("embedding".into(), embedding_snapshot(embedding));
    Ok(out)
}

/// `round(x, 6)`: the correctly rounded decimal, ties to even.
fn round6(x: f64) -> f64 {
    format!("{x:.6}").parse().unwrap_or(x)
}

/// A resident lesson as `_get` reads the dataclass (hydration's
/// `version or 1` applied, as Python's resident record holds it).
fn lesson_dict(r: &LessonRecord) -> Map<String, Value> {
    let mut d = Map::new();
    d.insert("entity".into(), json!(r.entity));
    d.insert("attribute".into(), json!(r.attribute));
    d.insert("value".into(), json!(r.value));
    d.insert("polarity".into(), json!(r.polarity));
    d.insert("status".into(), json!(r.status));
    d.insert("confidence".into(), json!(r.confidence));
    d.insert("origin".into(), json!(r.origin));
    d.insert("support".into(), json!(r.support));
    d.insert("provenance".into(), json!(r.provenance));
    d.insert("asserted_at".into(), json!(r.asserted_at));
    d.insert("last_confirmed".into(), json!(r.last_confirmed));
    d.insert("supersedes_value".into(), json!(r.supersedes_value));
    d.insert("superseded_by_value".into(), json!(r.superseded_by_value));
    d.insert("superseded_at".into(), json!(r.superseded_at));
    d.insert("tx_time".into(), json!(r.tx_time));
    d.insert("valid_time".into(), json!(r.valid_time));
    d.insert("hlc_phys".into(), json!(r.hlc_phys));
    d.insert("hlc_logical".into(), json!(r.hlc_logical));
    d.insert("writer_id".into(), json!(r.writer_id));
    d.insert("session_id".into(), json!(r.session_id));
    d.insert(
        "version".into(),
        json!(if r.version == 0 { 1 } else { r.version }),
    );
    d.insert("about".into(), json!(r.about));
    d.insert("outcome".into(), json!(r.outcome));
    d
}

/// A resident world fact as `_get` reads the dataclass: no `support` or
/// `provenance` attribute (the defaults apply), `origin` the constant.
fn world_dict(r: &WorldRecord) -> Map<String, Value> {
    let mut d = Map::new();
    d.insert("entity".into(), json!(r.entity));
    d.insert("attribute".into(), json!(r.attribute));
    d.insert("value".into(), json!(r.value));
    d.insert("polarity".into(), json!(r.polarity));
    d.insert("status".into(), json!(r.status));
    d.insert("confidence".into(), json!(r.confidence));
    d.insert("origin".into(), json!(crate::stores::world::ORIGIN));
    d.insert("asserted_at".into(), json!(r.asserted_at));
    d.insert("last_confirmed".into(), json!(r.last_confirmed));
    d.insert("supersedes_value".into(), json!(r.supersedes_value));
    d.insert("superseded_by_value".into(), json!(r.superseded_by_value));
    d.insert("superseded_at".into(), json!(r.superseded_at));
    d.insert("tx_time".into(), json!(r.tx_time));
    d.insert("valid_time".into(), json!(r.valid_time));
    d.insert("hlc_phys".into(), json!(r.hlc_phys));
    d.insert("hlc_logical".into(), json!(r.hlc_logical));
    d.insert("writer_id".into(), json!(r.writer_id));
    d.insert("session_id".into(), json!(r.session_id));
    d.insert("version".into(), json!(r.version));
    d.insert("source_url".into(), json!(r.source_url));
    d.insert("source_quote".into(), json!(r.source_quote));
    d.insert("retrieved_at".into(), json!(r.retrieved_at));
    d.insert("freshness_class".into(), json!(r.freshness_class));
    d.insert("content_hash".into(), json!(r.content_hash));
    d.insert("source_doc_id".into(), json!(r.source_doc_id));
    d
}

/// `_pair_from_retire_audit`: the evidence a curated retire row carries.
struct Curated {
    a: Map<String, Value>,
    b: Map<String, Value>,
    keep_a: bool,
    curation: Map<String, Value>,
}

impl Curated {
    /// `(survivor, loser)` evidence snapshots.
    fn sides(&self) -> (&Map<String, Value>, &Map<String, Value>) {
        if self.keep_a {
            (&self.a, &self.b)
        } else {
            (&self.b, &self.a)
        }
    }
}

/// `dict(curation[k])`, or None where Python's `dict()` raises.
fn evidence_side(v: Option<&Value>) -> Option<Map<String, Value>> {
    match v? {
        Value::Null | Value::Bool(_) | Value::Number(_) => None,
        other => py_dict(Some(other)).ok(),
    }
}

fn pair_from_retire_audit(record: Option<&Value>) -> Result<Option<Curated>, ToolError> {
    // `decision.get("record") or {}`.
    let record = match record {
        Some(v) if py_truthy(v) => v,
        _ => return Ok(None),
    };
    let Value::Object(rec) = record else {
        return Err(internal(format!(
            "AttributeError: '{}' object has no attribute 'get'",
            py_type(record)
        )));
    };
    let Some(Value::Object(cur)) = rec.get("_curation_duplicate") else {
        return Ok(None);
    };
    let (Some(a), Some(b)) = (evidence_side(cur.get("a")), evidence_side(cur.get("b"))) else {
        return Ok(None);
    };
    if !(cur.contains_key("a_fingerprint") && cur.contains_key("b_fingerprint")) {
        return Ok(None);
    }
    let Some(keep) = cur.get("keep").map(py_str) else {
        return Ok(None);
    };
    if keep != "a" && keep != "b" {
        return Ok(None);
    }
    Ok(Some(Curated {
        a,
        b,
        keep_a: keep == "a",
        curation: cur.clone(),
    }))
}

/// `_slot(snapshot)`: `(str(entity_norm), str(attribute_norm))`.
fn snap_slot(snap: &Map<String, Value>) -> Result<SlotKey, ToolError> {
    let get = |k: &str| {
        snap.get(k)
            .map(py_str)
            .ok_or_else(|| internal(format!("KeyError: '{k}'")))
    };
    Ok((get("entity_norm")?, get("attribute_norm")?))
}

/// A staged copy of the resident store the curation restore works on.
#[derive(Clone)]
enum Staged {
    Lesson(LessonStore),
    World(WorldStore),
}

impl Staged {
    fn of(ready: &Ready, store: Store) -> Self {
        match store {
            Store::Lesson => Staged::Lesson((*ready.lessons.snapshot()).clone()),
            Store::World => Staged::World((*ready.world.snapshot()).clone()),
        }
    }

    fn store(&self) -> Store {
        match self {
            Staged::Lesson(_) => Store::Lesson,
            Staged::World(_) => Store::World,
        }
    }

    /// `_resident_records`: slot to current record index, the last wins.
    fn current_index(&self) -> HashMap<SlotKey, usize> {
        let mut out = HashMap::new();
        match self {
            Staged::Lesson(s) => {
                for (i, r) in s.records.iter().enumerate() {
                    if r.status == "current" {
                        out.insert(r.key.clone(), i);
                    }
                }
            }
            Staged::World(s) => {
                for (i, r) in s.records.iter().enumerate() {
                    if r.status == "current" {
                        out.insert(r.key.clone(), i);
                    }
                }
            }
        }
        out
    }

    /// `snapshot_record(store, record)` of record `i`.
    fn snapshot(&self, i: usize) -> Result<Map<String, Value>, ToolError> {
        match self {
            Staged::Lesson(s) => {
                let r = &s.records[i];
                snapshot_record(Store::Lesson, &lesson_dict(r), r.embedding.as_deref())
            }
            Staged::World(s) => {
                let r = &s.records[i];
                snapshot_record(Store::World, &world_dict(r), r.embedding.as_deref())
            }
        }
    }

    fn has_retired_at(&self, key: &SlotKey) -> bool {
        match self {
            Staged::Lesson(s) => s
                .records
                .iter()
                .any(|r| &r.key == key && r.status == "retired"),
            Staged::World(s) => s
                .records
                .iter()
                .any(|r| &r.key == key && r.status == "retired"),
        }
    }

    fn restore(&mut self, entity: &str, attribute: Option<&str>) -> Vec<usize> {
        match self {
            Staged::Lesson(s) => s.restore(entity, attribute),
            Staged::World(s) => s.restore(entity, attribute),
        }
    }

    /// `_record_from_snapshot` appended as current, its slot dirty.
    fn push_from_snapshot(
        &mut self,
        snap: &Map<String, Value>,
        embedding: Option<Vec<f32>>,
    ) -> Result<usize, ToolError> {
        let text_or = |k: &str, default: &str| -> Result<String, ToolError> {
            Ok(str_field(snap, k, Some(default))?.unwrap_or_default())
        };
        let opt_text = |k: &str| str_field(snap, k, None);
        let opt_float = |k: &str| -> Result<Option<f64>, ToolError> {
            match snap.get(k) {
                None | Some(Value::Null) => Ok(None),
                Some(v) => py_float(v).map(Some),
            }
        };
        let entity = str_key(snap, "entity")?;
        let attribute = str_key(snap, "attribute")?;
        let value = str_key(snap, "value")?;
        let polarity = text_or("polarity", "+")?;
        let confidence = float_or(snap.get("confidence"), 0.0)?;
        let asserted_at = float_or(snap.get("asserted_at"), 0.0)?;
        let last_confirmed = float_or(snap.get("last_confirmed"), 0.0)?;
        let hlc_logical = opt_int(snap.get("hlc_logical"))?
            .map(i32::try_from)
            .transpose()
            .map_err(internal)?;
        // `int(snap.get("version") or 1)`.
        let version = match snap.get("version") {
            Some(v) if py_truthy(v) => opt_int(Some(v))?.unwrap_or(1),
            _ => 1,
        };
        let version = i32::try_from(version).map_err(internal)?;
        let embedding: Option<std::sync::Arc<[f32]>> = embedding.map(std::sync::Arc::from);
        match self {
            Staged::Lesson(s) => {
                let strings = |k: &str| -> Result<Vec<String>, ToolError> {
                    let mut out: Vec<String> = match snap.get(k) {
                        Some(Value::Array(a)) if !a.is_empty() => a
                            .iter()
                            .map(|x| match x {
                                Value::String(s) => Ok(s.clone()),
                                other => Err(internal(format!(
                                    "unsupported {k} member of type '{}'",
                                    py_type(other)
                                ))),
                            })
                            .collect::<Result<_, _>>()?,
                        Some(v) if py_truthy(v) => {
                            return Err(internal(format!("unsupported {k} in the audit snapshot")));
                        }
                        _ => Vec::new(),
                    };
                    out.sort();
                    out.dedup();
                    Ok(out)
                };
                let rec = LessonRecord {
                    key: slot_key(&entity, &attribute),
                    unit: embedding
                        .as_deref()
                        .map(|e| std::sync::Arc::from(unit_f32(e))),
                    entity,
                    attribute,
                    value,
                    about: opt_text("about")?,
                    polarity,
                    outcome: text_or("outcome", "success")?,
                    confidence,
                    status: "current".into(),
                    origin: opt_text("origin")?,
                    support: strings("support")?,
                    provenance: strings("provenance")?,
                    asserted_at,
                    last_confirmed,
                    supersedes_value: opt_text("supersedes_value")?,
                    superseded_by_value: opt_text("superseded_by_value")?,
                    superseded_at: opt_float("superseded_at")?,
                    embedding,
                    tx_time: opt_float("tx_time")?,
                    valid_time: opt_float("valid_time")?,
                    hlc_phys: opt_int(snap.get("hlc_phys"))?,
                    hlc_logical,
                    writer_id: opt_text("writer_id")?,
                    session_id: opt_text("session_id")?,
                    version,
                };
                s.dirty_slots.insert(rec.key.clone());
                s.records.push(rec);
                Ok(s.records.len() - 1)
            }
            Staged::World(s) => {
                let rec = WorldRecord {
                    entity,
                    attribute,
                    value,
                    polarity,
                    confidence,
                    status: "current".into(),
                    source_url: text_or("source_url", "")?,
                    source_quote: text_or("source_quote", "")?,
                    freshness_class: text_or("freshness_class", "volatile")?,
                    retrieved_at: float_or(snap.get("retrieved_at"), 0.0)?,
                    content_hash: opt_text("content_hash")?,
                    source_doc_id: opt_int(snap.get("source_doc_id"))?,
                    asserted_at,
                    last_confirmed,
                    supersedes_value: opt_text("supersedes_value")?,
                    superseded_by_value: opt_text("superseded_by_value")?,
                    superseded_at: opt_float("superseded_at")?,
                    embedding,
                    tx_time: opt_float("tx_time")?,
                    valid_time: opt_float("valid_time")?,
                    hlc_phys: opt_int(snap.get("hlc_phys"))?,
                    hlc_logical,
                    writer_id: opt_text("writer_id")?,
                    session_id: opt_text("session_id")?,
                    version,
                    key: (String::new(), String::new()),
                    unit: None,
                };
                Ok(s.push_current(rec))
            }
        }
    }

    /// The answer's entry for record `i`.
    fn entry(&self, i: usize, stale_policy: &str) -> Value {
        match self {
            Staged::Lesson(s) => Value::Object(crate::lesson_tools::record_to_dict(&s.records[i])),
            Staged::World(s) => Value::Object(crate::world_tools::record_to_dict(
                &s.records[i],
                stale_policy,
                crate::episodes::now(),
            )),
        }
    }

    /// `sync_lesson_slots` / `sync_world_slots` inside the caller's
    /// transaction.
    async fn sync_in(&self, c: &Client) -> anyhow::Result<()> {
        match self {
            Staged::Lesson(s) => crate::stores::lessons::sync_slot_rows_in(c, s)
                .await
                .map(|_| ()),
            Staged::World(s) => crate::stores::world::sync_slot_rows_in(c, s)
                .await
                .map(|_| ()),
        }
    }

    /// `staged.dirty_slots.clear()`, then `_publish_store`.
    fn publish(self, ready: &Ready) {
        match self {
            Staged::Lesson(mut s) => {
                s.dirty_slots.clear();
                ready.lessons.write(|l| *l = s);
            }
            Staged::World(mut s) => {
                s.dirty_slots.clear();
                ready.world.write(|w| *w = s);
            }
        }
    }
}

/// `_restore_decision_snapshot`: the restored record's evidence snapshot,
/// tagged with the retire row it undoes, without the embedding digest; a
/// lesson's slot fields under their procedural names (appended last).
fn restore_decision_snapshot(
    store: Store,
    mut snap: Map<String, Value>,
    retire_decision_id: i64,
) -> Value {
    snap.insert("_curation_undo_of".into(), json!(retire_decision_id));
    snap.shift_remove("embedding");
    if store == Store::Lesson {
        let task = snap.shift_remove("entity").unwrap_or(Value::Null);
        let aspect = snap.shift_remove("attribute").unwrap_or(Value::Null);
        let lesson = snap.shift_remove("value").unwrap_or(Value::Null);
        snap.insert("task".into(), task);
        snap.insert("aspect".into(), aspect);
        snap.insert("lesson".into(), lesson);
    }
    Value::Object(snap)
}

/// The two slots' rows of any status, locked (`_targeted_all_rows`), each
/// as its evidence snapshot.
async fn targeted_snapshots(
    c: &Client,
    store: Store,
    a: &SlotKey,
    b: &SlotKey,
) -> Result<Vec<Map<String, Value>>, ToolError> {
    let sql = match store {
        Store::Lesson => {
            "SELECT entity, attribute, value, polarity, status, confidence::text, origin, \
             support, provenance, asserted_at, last_confirmed, supersedes_value, \
             superseded_by_value, superseded_at, tx_time, valid_time, hlc_phys, hlc_logical, \
             writer_id, session_id, version, about, outcome, embedding::real[] \
             FROM lessons WHERE ((entity_norm = $1 AND attribute_norm = $2) OR \
             (entity_norm = $3 AND attribute_norm = $4)) FOR UPDATE"
        }
        Store::World => {
            "SELECT entity, attribute, value, polarity, status, confidence::text, origin, \
             support, provenance, asserted_at, last_confirmed, supersedes_value, \
             superseded_by_value, superseded_at, tx_time, valid_time, hlc_phys, hlc_logical, \
             writer_id, session_id, version, source_url, source_quote, retrieved_at, \
             freshness_class, content_hash, source_doc_id, embedding::real[] \
             FROM world_facts WHERE ((entity_norm = $1 AND attribute_norm = $2) OR \
             (entity_norm = $3 AND attribute_norm = $4)) FOR UPDATE"
        }
    };
    let rows = c
        .query(sql, &[&a.0, &a.1, &b.0, &b.1])
        .await
        .map_err(internal)?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let mut d = Map::new();
        let text = |i: usize| json!(r.get::<_, Option<String>>(i));
        let float = |i: usize| json!(r.get::<_, Option<f64>>(i));
        for (i, k) in ["entity", "attribute", "value", "polarity", "status"]
            .into_iter()
            .enumerate()
        {
            d.insert(k.into(), text(i));
        }
        let confidence = r
            .get::<_, Option<String>>(5)
            .map(|t| t.parse::<f64>().map_err(internal))
            .transpose()?;
        d.insert("confidence".into(), json!(confidence));
        d.insert("origin".into(), text(6));
        d.insert(
            "support".into(),
            r.get::<_, Option<Value>>(7).map_or(Value::Null, py_json),
        );
        d.insert(
            "provenance".into(),
            r.get::<_, Option<Value>>(8).map_or(Value::Null, py_json),
        );
        for (i, k) in [
            (9, "asserted_at"),
            (10, "last_confirmed"),
            (13, "superseded_at"),
            (14, "tx_time"),
            (15, "valid_time"),
        ] {
            d.insert(k.into(), float(i));
        }
        d.insert("supersedes_value".into(), text(11));
        d.insert("superseded_by_value".into(), text(12));
        d.insert("hlc_phys".into(), json!(r.get::<_, Option<i64>>(16)));
        d.insert("hlc_logical".into(), json!(r.get::<_, Option<i32>>(17)));
        d.insert("writer_id".into(), text(18));
        d.insert("session_id".into(), text(19));
        d.insert("version".into(), json!(r.get::<_, Option<i32>>(20)));
        let embedding: Option<Vec<f32>> = match store {
            Store::Lesson => {
                d.insert("about".into(), text(21));
                d.insert("outcome".into(), text(22));
                r.get(23)
            }
            Store::World => {
                d.insert("source_url".into(), text(21));
                d.insert("source_quote".into(), text(22));
                d.insert("retrieved_at".into(), float(23));
                d.insert("freshness_class".into(), text(24));
                d.insert("content_hash".into(), text(25));
                d.insert("source_doc_id".into(), json!(r.get::<_, Option<i64>>(26)));
                r.get(27)
            }
        };
        out.push(snapshot_record(store, &d, embedding.as_deref())?);
    }
    Ok(out)
}

/// `_validate_restore_locked`: inside the transaction, the survivor still
/// current and unchanged, the loser not current, any retired loser row the
/// one the retire wrote, and the retire row itself still a curated retire.
async fn validate_restore_locked(
    c: &Client,
    store: Store,
    cur: &Curated,
    decision: &Decision,
) -> Result<bool, ToolError> {
    let (survivor_snap, loser_snap) = cur.sides();
    let (survivor_slot, loser_slot) = (snap_slot(survivor_snap)?, snap_slot(loser_snap)?);
    let snapshots = targeted_snapshots(c, store, &snap_slot(&cur.a)?, &snap_slot(&cur.b)?).await?;
    let mut survivor = None;
    let mut loser_current = false;
    let mut retired: Vec<String> = Vec::new();
    for s in &snapshots {
        let slot = snap_slot(s)?;
        let status = s.get("status").and_then(Value::as_str);
        if slot == survivor_slot && status == Some("current") && survivor.is_none() {
            survivor = Some(s);
        }
        if slot == loser_slot && status == Some("current") {
            loser_current = true;
        }
        if slot == loser_slot && status == Some("retired") {
            retired.push(fingerprint(s));
        }
    }
    let Some(survivor) = survivor else {
        return Ok(false);
    };
    if loser_current || fingerprint(survivor) != fingerprint(survivor_snap) {
        return Ok(false);
    }
    let expected = match &decision.record {
        Some(Value::Object(m)) => m.get("_curation_retired_fingerprint").cloned(),
        _ => None,
    };
    if !retired.is_empty()
        && !expected
            .as_ref()
            .and_then(Value::as_str)
            .is_some_and(|e| retired.iter().any(|f| f == e))
    {
        return Ok(false);
    }
    let locked = c
        .query_opt(
            "SELECT action, record FROM store_decisions WHERE id = $1 FOR UPDATE",
            &[&decision.id],
        )
        .await
        .map_err(internal)?;
    let Some(locked) = locked else {
        return Ok(false);
    };
    let action: String = locked.get(0);
    if action != "retire" {
        return Ok(false);
    }
    let record = locked.get::<_, Option<Value>>(1).map(py_json);
    let curated = match record.as_ref() {
        Some(v) if py_truthy(v) => match v {
            Value::Object(m) => m.get("_curation_duplicate").is_some_and(py_truthy),
            other => {
                return Err(internal(format!(
                    "AttributeError: '{}' object has no attribute 'get'",
                    py_type(other)
                )));
            }
        },
        _ => false,
    };
    Ok(curated)
}

/// What a failed curated restore's transaction left durable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Recovery {
    Committed,
    RolledBack,
}

/// A failure's text, as `str(exc)` would carry it into a message.
fn error_text(e: &ToolError) -> String {
    match e {
        ToolError::Internal(e) => format!("{e:#}"),
        ToolError::Invalid(t) | ToolError::FileNotFound(t) => t.clone(),
        ToolError::Refused { code, detail, .. } => match detail {
            Some(d) => format!("{code}: {d}"),
            None => code.clone(),
        },
    }
}

/// `_restore_recovery_state`: the reloaded store (`resident`) and the loser
/// slot's latest decision (`latest`) classified. The survivor must still be
/// current and unchanged; then a loser that is not current under its own
/// retire row is the state before, and a loser current as its evidence
/// under a restore row undoing that retire is the state after. Anything
/// else raises `CurationReconciliationError`.
fn restore_recovery_state(
    resident: &Staged,
    cur: &Curated,
    retire_decision_id: i64,
    latest: Option<&Decision>,
) -> Result<Recovery, ToolError> {
    let current = resident.current_index();
    let (survivor_snap, loser_snap) = cur.sides();
    let survivor_matches = match current.get(&snap_slot(survivor_snap)?) {
        Some(&i) => fingerprint(&resident.snapshot(i)?) == fingerprint(survivor_snap),
        None => false,
    };
    if !survivor_matches {
        return Err(internal("durable curation restore changed the survivor"));
    }
    match current.get(&snap_slot(loser_snap)?) {
        None => {
            if latest.is_some_and(|d| d.id == retire_decision_id && d.action == "retire") {
                return Ok(Recovery::RolledBack);
            }
        }
        Some(&i) => {
            if fingerprint(&resident.snapshot(i)?) == fingerprint(loser_snap) {
                // `(latest or {}).get("record") or {}`, then `.get`.
                let undo_of = match latest.and_then(|d| d.record.as_ref()) {
                    Some(Value::Object(m)) => m.get("_curation_undo_of"),
                    Some(v) if py_truthy(v) => {
                        return Err(internal(format!(
                            "AttributeError: '{}' object has no attribute 'get'",
                            py_type(v)
                        )));
                    }
                    _ => None,
                };
                if latest.is_some_and(|d| d.action == "restore")
                    && undo_of.is_some_and(|v| py_eq_int(v, retire_decision_id))
                {
                    return Ok(Recovery::Committed);
                }
            }
        }
    }
    Err(internal(
        "durable curation restore is neither before nor after the decision",
    ))
}

/// Python's `v == n` for a decoded JSON value and an int.
fn py_eq_int(v: &Value, n: i64) -> bool {
    match v {
        Value::Bool(b) => i64::from(*b) == n,
        Value::Number(x) => x.as_i64() == Some(n) || x.as_f64().is_some_and(|f| f == n as f64),
        _ => false,
    }
}

/// `store_decisions(store, entity_norm, attribute_norm, limit=1)`: the
/// slot's newest decision.
async fn latest_decision(
    client: &Client,
    store: Store,
    slot: &SlotKey,
) -> Result<Option<Decision>, ToolError> {
    let row = reads(client, |c| async move {
        c.query_opt(
            "SELECT id, entity_norm, attribute_norm, action, record, decided_at \
             FROM store_decisions WHERE store = $1 AND entity_norm = $2 \
             AND attribute_norm = $3 ORDER BY decided_at DESC, id DESC LIMIT 1",
            &[&store.name(), &slot.0, &slot.1],
        )
        .await
        .map_err(internal)
    })
    .await?;
    Ok(row.map(|r| Decision {
        id: r.get(0),
        entity_norm: r.get(1),
        attribute_norm: r.get(2),
        action: r.get(3),
        record: r.get::<_, Option<Value>>(4).map(py_json),
        decided_at: r.get(5),
    }))
}

/// `_recover_slot_curation_locked` for a curated restore whose transaction
/// failed: the store reloaded from the database (`_hydrate_store`) and
/// classified; a commit that landed publishes the reload. A rollback leaves
/// the resident store as it was (Python publishes its pre-stage copy, the
/// same store: the staged one was a copy). A failure to reload or classify
/// is a `CurationReconciliationError`.
async fn reconcile_restore(
    ready: &Ready,
    store: Store,
    cur: &Curated,
    retire_decision_id: i64,
) -> Result<Recovery, ToolError> {
    let client = ready.storage.client();
    let required =
        |e: String| internal(format!("slot curation commit reconciliation required: {e}"));
    let resident = match store {
        Store::Lesson => crate::stores::lessons::hydrate(client)
            .await
            .map(Staged::Lesson),
        Store::World => crate::stores::world::hydrate(client)
            .await
            .map(Staged::World),
    }
    .map_err(|e| required(format!("{e:#}")))?;
    let loser_slot = snap_slot(cur.sides().1)?;
    let latest = latest_decision(client, store, &loser_slot)
        .await
        .map_err(|e| required(error_text(&e)))?;
    let state = restore_recovery_state(&resident, cur, retire_decision_id, latest.as_ref())
        .map_err(|e| match e {
            ToolError::Internal(_) => e,
            other => required(error_text(&other)),
        })?;
    if state == Recovery::Committed {
        resident.publish(ready);
    }
    Ok(state)
}

/// `curation_safety.restore_curated_duplicate`: None unless the target's
/// retire row is a curated duplicate retirement.
async fn restore_curated(
    svc: &Service,
    ready: &Ready,
    store: Store,
    entity: &str,
    attribute: Option<&str>,
) -> Result<Option<Value>, ToolError> {
    let client = ready.storage.client();
    let ne = norm_key(entity);
    let decisions = retired_slots(client, store, &ne).await?;
    let (decision, cur, na) = match attribute {
        None => {
            let mut curated = Vec::new();
            for d in &decisions {
                if let Some(p) = pair_from_retire_audit(d.record.as_ref())? {
                    curated.push((d, p));
                }
            }
            if curated.is_empty() {
                return Ok(None);
            }
            // A mixed or multi-slot whole-entity request fails closed.
            if decisions.len() != 1 || curated.len() != 1 {
                return Ok(Some(
                    json!({"restored": 0, "reason": "curation_restore_requires_attribute"}),
                ));
            }
            let (d, p) = curated.remove(0);
            (d, p, d.attribute_norm.clone())
        }
        Some(a) => {
            let na = norm_key(a);
            let Some(d) = decisions.iter().find(|d| d.attribute_norm == na) else {
                return Ok(None);
            };
            let Some(p) = pair_from_retire_audit(d.record.as_ref())? else {
                return Ok(None);
            };
            (d, p, na)
        }
    };
    let (survivor_snap, loser_snap) = cur.sides();
    let loser_slot = snap_slot(loser_snap)?;
    if loser_slot != (ne.clone(), na.clone()) {
        return Ok(Some(
            json!({"restored": 0, "reason": "curation_target_mismatch"}),
        ));
    }
    let changed = || {
        Ok(Some(
            json!({"restored": 0, "reason": "curation_evidence_changed"}),
        ))
    };
    let mut staged = Staged::of(ready, store);
    let current = staged.current_index();
    let survivor_slot = snap_slot(survivor_snap)?;
    match current.get(&survivor_slot) {
        None => return changed(),
        Some(&i) => {
            if fingerprint(&staged.snapshot(i)?) != fingerprint(survivor_snap)
                || current.contains_key(&loser_slot)
            {
                return changed();
            }
        }
    }
    // `_stage_restore`.
    let loser_retired = staged.has_retired_at(&loser_slot);
    let loser_entity = str_key(loser_snap, "entity")?;
    let loser_attribute = match loser_snap.get("attribute") {
        None => return Err(internal("KeyError: 'attribute'")),
        Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(v) => {
            return Err(internal(format!(
                "AttributeError: '{}' object has no attribute 'strip'",
                py_type(v)
            )));
        }
    };
    let restored = staged.restore(&loser_entity, loser_attribute.as_deref());
    let (idx, source) = match restored.first() {
        Some(&first) => {
            if fingerprint(&staged.snapshot(first)?) != fingerprint(loser_snap) {
                return Err(internal(
                    "retired curation record no longer matches its pre-image",
                ));
            }
            (first, "retired_record")
        }
        None => {
            if loser_retired {
                return Err(internal("curation loser could not be restored"));
            }
            let embedding = match cur.curation.get("loser_embedding") {
                None | Some(Value::Null) => None,
                Some(Value::Array(xs)) => Some(
                    xs.iter()
                        .map(|x| py_float(x).map(|f| f as f32))
                        .collect::<Result<Vec<f32>, _>>()?,
                ),
                Some(v) => {
                    return Err(internal(format!(
                        "could not build a float32 tensor from '{}'",
                        py_type(v)
                    )));
                }
            };
            (
                staged.push_from_snapshot(loser_snap, embedding)?,
                "audit_snapshot",
            )
        }
    };
    let stamp = crate::episodes::now();
    let undo = restore_decision_snapshot(store, staged.snapshot(idx)?, decision.id);
    let outcome = {
        let (staged, cur, undo, ne, na) = (&staged, &cur, &undo, &ne, &na);
        in_txn(client, |c| async move {
            if !validate_restore_locked(c, store, cur, decision).await? {
                return Ok(false);
            }
            staged.sync_in(c).await.map_err(internal)?;
            crate::forget_tools::record_store_decision(
                c,
                store.name(),
                (ne, na),
                "restore",
                Some(DECIDED_BY),
                None,
                undo,
                stamp,
            )
            .await
            .map_err(internal)?;
            Ok(true)
        })
        .await
    };
    let reconciled = match outcome {
        Ok(true) => {
            let entry = staged.entry(idx, &svc.config.memory.search.stale_policy);
            debug_assert_eq!(staged.store(), store);
            staged.publish(ready);
            return Ok(Some(curated_answer(
                store, entity, attribute, source, entry,
            )));
        }
        Ok(false) => return changed(),
        // The commit may have landed with its reply lost: the database
        // decides (`_recover_slot_curation_locked`).
        Err(exc) => match reconcile_restore(ready, store, &cur, decision.id).await? {
            Recovery::Committed => "reconciled",
            Recovery::RolledBack => {
                return Err(internal(format!(
                    "curation restore rolled back: {}",
                    error_text(&exc)
                )));
            }
        },
    };
    let entry = staged.entry(idx, &svc.config.memory.search.stale_policy);
    Ok(Some(curated_answer(
        store, entity, attribute, reconciled, entry,
    )))
}

/// `{"restored": 1, "source", **label, "entries": [entry]}`.
fn curated_answer(
    store: Store,
    entity: &str,
    attribute: Option<&str>,
    source: &str,
    entry: Value,
) -> Value {
    let mut out = Map::new();
    out.insert("restored".into(), json!(1));
    out.insert("source".into(), json!(source));
    out.extend(label(store, entity, attribute).map(|(k, v)| (k.to_string(), v)));
    out.insert("entries".into(), json!([entry]));
    Value::Object(out)
}

#[cfg(test)]
#[path = "graph_review_slots_tests.rs"]
mod tests;
