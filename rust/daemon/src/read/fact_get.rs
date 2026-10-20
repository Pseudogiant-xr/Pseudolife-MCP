//! `memory_fact_get` over MCP (spec "memory_fact_get over MCP", FG1-FG6).
//! Python: `mcp_server.py:1561-1689`; `service.py` `cortex_lookup`
//! (4157-4287), `cortex_contenders` (4419), `cortex_candidates` (4432),
//! `entity_ref` (7556).

use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::cortex_search::{
    CortexKnobs, annotate_evidence_supersession, bump_slot_reads, record_to_dict,
    record_to_dict_age, trace_invalidations_for_slots, traces_for_slot,
};
use crate::read::mcp_search::{CORRECTION_NOTE, correct_with};
use crate::stores::cortex::{CortexRecord, CortexStore, SlotKey, norm_key, slot_key};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use tokio_postgres::Client;

/// `cortex_candidates`'s defaults (`service.py:4432`, `cortex.py:1708`).
const CANDIDATES_TOP_K: usize = 5;
const CANDIDATES_MIN_SCORE: f64 = 0.35;

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_fact_get", mcp_memory_fact_get)]
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn mcp_memory_fact_get<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let text = |k: &str| {
            a.get(k)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        let (entity, attribute) = (text("entity"), text("attribute"));
        let verbose = a.get("verbose").and_then(Value::as_bool).unwrap_or(false);
        let ready = app
            .service
            .ensure_init()
            .await
            .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
        let cfg = &app.service.config;
        let knobs = CortexKnobs::from_config(cfg);
        let relative_age = cfg.time.relative_age;
        // A snapshot: the read lock is never held across an await.
        let snapshot = ready.cortex.snapshot();
        let store: &CortexStore = &snapshot;
        let db = ready.storage.client();
        let internal = |e: anyhow::Error| ToolError::Internal(e);

        // service.cortex_lookup
        let record = crate::txn::with_client(db, |db| {
            lookup(store, db, &entity, &attribute, &knobs, relative_age, now())
        })
        .await
        .map_err(internal)?;
        // service.cortex_contenders: the full record dicts, no alias handling.
        let contenders: Vec<Value> = store
            .contenders_for(&entity, &attribute)
            .into_iter()
            .map(|r| Value::Object(record_to_dict(r, &knobs.stale_policy, now())))
            .collect();
        let is_empty_set = record.as_ref().is_some_and(|r| {
            r.get("kind").and_then(Value::as_str) == Some("set")
                && r.get("members")
                    .and_then(Value::as_array)
                    .is_none_or(Vec::is_empty)
        });
        let mut out = Map::new();
        let mut record = record.map(Value::Object).unwrap_or(Value::Null);
        let contested = !contenders.is_empty();
        out.insert("record".into(), Value::Null);
        out.insert("contenders".into(), Value::Array(contenders));
        if (record.is_null() || is_empty_set)
            && !contested
            && !crate::mutants::active("mcp-fact-get-no-candidates")
        {
            let embedder = ready.embedder.clone();
            let query = format!("{entity} {attribute}");
            let emb = tokio::task::spawn_blocking(move || embedder.embed(&query))
                .await
                .map_err(|e| internal(e.into()))?
                .map_err(internal)?;
            let candidates =
                crate::txn::with_client(db, |db| candidates(store, db, &entity, &attribute, &emb))
                    .await
                    .map_err(internal)?;
            out.insert("candidates".into(), Value::Array(candidates));
        }
        if !record.is_null() && !is_empty_set {
            let mut probe = record.clone();
            probe["contested"] = json!(contested);
            if let Some(cw) = correct_with(&probe) {
                record["correct_with"] = json!(cw);
                out.insert("correction_note".into(), json!(CORRECTION_NOTE));
            }
        }
        // service.entity_ref
        let node = crate::txn::with_client(db, |db| async move {
            crate::storage::graph::find_entity_body(db, &crate::storage::graph::norm_name(&entity))
                .await
        })
        .await
        .map_err(internal)?;
        if !node.is_null() {
            out.insert(
                "entity_ref".into(),
                json!({"entity_id": node["id"], "canonical": node["canonical"],
                       "etype": node["etype"], "aliases": node["aliases"]}),
            );
        }
        if !verbose
            && cfg.memory.mcp.compact_payloads
            && !crate::mutants::active("mcp-fact-get-no-lean")
        {
            record = lean_fact_record(record);
        }
        out.insert("record".into(), record);
        Ok(Value::Object(out))
    })
}

/// `_alias_retry_names` (`service.py:4157`): after `entity`, the node's
/// canonical then its aliases (in `find_entity`'s order), minus any alias
/// that is another entity's canonical, deduplicated on the cortex key.
async fn alias_retry_names(db: &Client, entity: &str, node: &Value) -> anyhow::Result<Vec<String>> {
    let aliases: Vec<String> = node["aliases"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let shadowed = canonical_names_among(db, &aliases).await?;
    let mut seen: BTreeSet<String> = BTreeSet::from([norm_key(entity)]);
    let mut out = Vec::new();
    let canonical = node["canonical"].as_str().map(str::to_string);
    for c in canonical.into_iter().chain(aliases) {
        if c.is_empty() || shadowed.contains(&c) || !seen.insert(norm_key(&c)) {
            continue;
        }
        out.push(c);
    }
    Ok(out)
}

/// `canonical_names_among` (`storage/postgres.py:2858`): the names that
/// are some entity's canonical; no query for an empty list.
async fn canonical_names_among(db: &Client, names: &[String]) -> anyhow::Result<BTreeSet<String>> {
    if names.is_empty() {
        return Ok(BTreeSet::new());
    }
    Ok(db
        .query(
            "SELECT canonical FROM entities WHERE canonical = ANY($1)",
            &[&names],
        )
        .await?
        .iter()
        .map(|r| r.get(0))
        .collect())
}

/// `_track_slot_reads`: serving telemetry; a failure is logged and swallowed.
async fn track(db: &Client, slots: &[SlotKey], knobs: &CortexKnobs, now: f64) {
    if knobs.read_tracking
        && let Err(e) = bump_slot_reads(db, slots, now).await
    {
        crate::read::stats::record_slot_read_error();
        eprintln!("slot-read telemetry failed: {e:#}");
    }
}

/// `cortex_lookup(entity, attribute)` with `track=True`.
async fn lookup(
    store: &CortexStore,
    db: &Client,
    entity: &str,
    attribute: &str,
    knobs: &CortexKnobs,
    relative_age: bool,
    now: f64,
) -> anyhow::Result<Option<Map<String, Value>>> {
    let mut rec: Option<&CortexRecord> = store.lookup(entity, attribute);
    let mut names = vec![entity.to_string()];
    if rec.is_none() && !crate::mutants::active("mcp-fact-get-no-alias-retry") {
        let node =
            crate::storage::graph::find_entity_body(db, &crate::storage::graph::norm_name(entity))
                .await?;
        if !node.is_null() {
            names.extend(alias_retry_names(db, entity, &node).await?);
            rec = names[1..].iter().find_map(|n| store.lookup(n, attribute));
        }
    }
    let Some(r) = rec else {
        for name in names.iter().filter(|n| !n.is_empty()) {
            if store.slot_kind(name, attribute) != Some("set") {
                continue;
            }
            let members = store.members(name, attribute);
            let removed: Vec<&CortexRecord> = store
                .member_rows(name, attribute)
                .into_iter()
                .filter(|r| r.status == "removed")
                .collect();
            let key = slot_key(name, attribute);
            if !members.is_empty() {
                track(db, std::slice::from_ref(&key), knobs, now).await;
            }
            let dict = |r: &CortexRecord| {
                Value::Object(record_to_dict_age(
                    r,
                    &knobs.stale_policy,
                    now,
                    relative_age,
                ))
            };
            let mut out = Map::new();
            out.insert("kind".into(), json!("set"));
            out.insert("entity".into(), json!(name));
            out.insert("attribute".into(), json!(attribute));
            out.insert("members".into(), members.iter().map(|r| dict(r)).collect());
            out.insert("removed".into(), removed.iter().map(|r| dict(r)).collect());
            if !members.is_empty() {
                annotate_set(db, &mut out, &key, &members, knobs).await?;
            }
            return Ok(Some(out));
        }
        return Ok(None);
    };
    let mut d = record_to_dict_age(r, &knobs.stale_policy, now, relative_age);
    let key = slot_key(&r.entity, &r.attribute);
    d.insert(
        "source_entries".into(),
        json!(traces_for_slot(db, &key).await?),
    );
    annotate_evidence_supersession(db, std::slice::from_mut(&mut d), knobs).await?;
    track(db, std::slice::from_ref(&key), knobs, now).await;
    Ok(Some(d))
}

/// `_annotate_set_slot_evidence` (`service.py:4346`): the slot-wide warning,
/// seen at the latest member's `last_confirmed or asserted_at`.
async fn annotate_set(
    db: &Client,
    out: &mut Map<String, Value>,
    key: &SlotKey,
    members: &[&CortexRecord],
    knobs: &CortexKnobs,
) -> anyhow::Result<()> {
    if !knobs.traces_enabled {
        return Ok(());
    }
    let seen = members
        .iter()
        .map(|m| {
            if m.last_confirmed != 0.0 {
                m.last_confirmed
            } else {
                m.asserted_at
            }
        })
        .filter(|&s| s != 0.0)
        .reduce(f64::max);
    let inv = trace_invalidations_for_slots(db, std::slice::from_ref(key)).await?;
    let Some(seen) = seen else { return Ok(()) };
    let ids: BTreeSet<i64> = inv
        .get(key)
        .map(|v| {
            v.iter()
                .filter(|ev| ev.cause == "source_superseded" && ev.invalidated_at > seen)
                .map(|ev| ev.source_entry_id)
                .collect()
        })
        .unwrap_or_default();
    if !ids.is_empty() {
        let n = ids.len();
        out.insert("re_verify".into(), json!(true));
        out.insert(
            "re_verify_reason".into(),
            json!(format!(
                "derived from {n} source {} corrected since this fact was last confirmed",
                if n == 1 { "memory" } else { "memories" }
            )),
        );
    }
    Ok(())
}

/// `cortex_candidates` (`service.py:4432`): candidates under the queried
/// name and, when its graph node's canonical differs, under that too;
/// deduplicated on the lower-cased slot, same-entity first, then by score
/// (a null or zero score sorting as 1.0), at most five.
async fn candidates(
    store: &CortexStore,
    db: &Client,
    entity: &str,
    attribute: &str,
    emb: &[f32],
) -> anyhow::Result<Vec<Value>> {
    use crate::storage::graph::{find_entity_body, norm_name};
    let mut names = vec![entity.to_string()];
    let node = find_entity_body(db, &norm_name(entity)).await?;
    if let Some(canon) = node["canonical"].as_str()
        && !canon.is_empty()
        && norm_name(canon) != norm_name(entity)
    {
        names.push(canon.to_string());
    }
    let mut seen: BTreeSet<(String, String)> = BTreeSet::new();
    let mut out: Vec<Value> = Vec::new();
    for name in &names {
        for c in store.candidates_for(
            name,
            attribute,
            Some(emb),
            CANDIDATES_TOP_K,
            CANDIDATES_MIN_SCORE,
        ) {
            let lower = |k: &str| c[k].as_str().unwrap_or_default().to_lowercase();
            if seen.insert((lower("entity"), lower("attribute"))) {
                out.push(c);
            }
        }
    }
    let rank = |c: &Value| {
        let score = c["score"].as_f64().filter(|&s| s != 0.0).unwrap_or(1.0);
        (c["why"] != "same_entity", -score)
    };
    out.sort_by(|x, y| {
        let ((ax, as_), (bx, bs)) = (rank(x), rank(y));
        ax.cmp(&bx).then(as_.total_cmp(&bs))
    });
    out.truncate(CANDIDATES_TOP_K);
    Ok(out)
}

/// `_LEAN_FACT_KEYS` (`mcp_server.py:1642`), in order.
const LEAN_FACT_KEYS: &[&str] = &[
    "entity",
    "attribute",
    "value",
    "kind",
    "confidence",
    "origin",
    "asserted_at",
    "last_confirmed",
    "age",
    "freshness_class",
    "stale",
    "members",
    "removed",
    "re_verify",
    "re_verify_reason",
    "correct_with",
    "warning",
    "last_known_value",
    "source_entries",
    "stance",
    "authority",
    "distortion_tolerance",
];

/// `_lean_fact_record` (`mcp_server.py:1671`): the allow-listed keys, then
/// `effective_confidence` when the class decays; set members projected the
/// same way. Anything but an object passes through.
fn lean_fact_record(rec: Value) -> Value {
    let Value::Object(rec) = rec else { return rec };
    let mut out = Map::new();
    for &k in LEAN_FACT_KEYS {
        if let Some(v) = rec.get(k) {
            out.insert(k.into(), v.clone());
        }
    }
    // `(rec.get("freshness_class") or "evergreen") != "evergreen"`.
    let decays = match rec.get("freshness_class") {
        Some(v) if crate::board::store::py_truthy(v) => v.as_str() != Some("evergreen"),
        _ => false,
    };
    if decays && let Some(v) = rec.get("effective_confidence") {
        out.insert("effective_confidence".into(), v.clone());
    }
    for k in ["members", "removed"] {
        if let Some(Value::Array(items)) = out.get_mut(k) {
            *items = std::mem::take(items)
                .into_iter()
                .map(lean_fact_record)
                .collect();
        }
    }
    Value::Object(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lean_keeps_the_allow_list_in_order_and_decaying_confidence() {
        let rec = json!({"entity": "box", "attribute": "os", "value": "debian", "polarity": "+",
                         "status": "current", "kind": "scalar", "confidence": 0.7,
                         "origin": "user", "support": ["user"], "freshness_class": "volatile",
                         "effective_confidence": 0.5, "stale": false, "age": "just now",
                         "source_entries": [3], "writer_id": null});
        let lean = lean_fact_record(rec);
        let keys: Vec<&str> = lean
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            [
                "entity",
                "attribute",
                "value",
                "kind",
                "confidence",
                "origin",
                "age",
                "freshness_class",
                "stale",
                "source_entries",
                "effective_confidence"
            ]
        );
        let evergreen = lean_fact_record(json!({"value": 1, "freshness_class": "evergreen",
                                                "effective_confidence": 0.7}));
        assert!(evergreen.get("effective_confidence").is_none());
        assert_eq!(lean_fact_record(Value::Null), Value::Null);
    }

    #[test]
    fn lean_projects_set_members_on_the_same_terms() {
        let set = json!({"kind": "set", "entity": "box", "attribute": "users",
                         "members": [{"value": "alice", "status": "current", "tx_time": 1.0}],
                         "removed": []});
        let lean = lean_fact_record(set);
        assert_eq!(lean["members"], json!([{"value": "alice"}]));
    }
}
