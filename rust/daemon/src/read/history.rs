//! `memory_history` over MCP (spec "memory_history over MCP", HI1-HI7 slot
//! mode, CH1-CH6 chain mode). Python: `mcp_server.py:1804-1828`;
//! `service.py` `_parse_as_of` (5746-5756), `history` (5758-5831) and
//! `chain` (5833-5927); `memory/cortex.py` `members(include_removed=True)`
//! (1182-1191), `slot_kind` (1193-1216) and `records_for` (1526-1528);
//! `storage/postgres.py` `entries_for_entity` (4156-4171) and `load_graph`
//! (4370-4393).

use serde_json::{Map, Value, json};

use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::cortex_search::record_to_dict_age;
use crate::stores::cortex::{CortexRecord, CortexStore, slot_key};

/// `_cortex_record_to_dict`'s default stale policy: the version chain is
/// the audit surface, so it is never policy-rendered.
const ANNOTATE: &str = "annotate";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_history", mcp_memory_history)]
}

fn mcp_memory_history<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let entity = a
            .get("entity")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let Some(attribute) = a
            .get("attribute")
            .and_then(Value::as_str)
            .map(str::to_string)
        else {
            // Chain mode (`as_of` is ignored there, once bound).
            let ready = app
                .service
                .ensure_init()
                .await
                .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
            return chain(&ready, &entity, CHAIN_LIMIT)
                .await
                .map_err(ToolError::Internal);
        };
        // `_parse_as_of` runs before the lock; the binder has refused what
        // it cannot parse (`_check_as_of`).
        let cutoff = a.get("as_of").and_then(crate::mcp::as_of::epoch);
        let ready = app
            .service
            .ensure_init()
            .await
            .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
        let snap = ready.cortex.snapshot();
        let relative_age = app.service.config.time.relative_age;
        Ok(history(
            &snap,
            &entity,
            &attribute,
            cutoff,
            relative_age,
            crate::episodes::now(),
        ))
    })
}

/// `service.chain`'s default `limit`.
const CHAIN_LIMIT: usize = 20;
/// `(en.get("text") or "")[:160]`: an entry event's summary length.
const ENTRY_SUMMARY_CHARS: usize = 160;

/// `service.chain`: the entity's dated events from four streams (the
/// personal cortex's assertions and supersessions under the queried name,
/// its graph node's canonical and aliases; the node's source entries; its
/// live edges; current lessons whose task or `about` names it), sorted
/// stably by time and cut to the last `limit`.
async fn chain(ready: &crate::service::Ready, entity: &str, limit: usize) -> anyhow::Result<Value> {
    let not_found = || json!({"found": false, "entity": entity, "count": 0, "events": []});
    let name = crate::board::text::py_strip(entity);
    if name.is_empty() {
        return Ok(not_found());
    }
    let db = ready.storage.client();
    let mut keys: std::collections::HashSet<String> =
        std::collections::HashSet::from([crate::stores::cortex::norm_key(name)]);
    let mut display = name.to_string();
    let probe = crate::storage::graph::norm_name(name);
    let node = crate::txn::with_client(db, |c| async move {
        crate::storage::graph::find_entity_body(c, &probe).await
    })
    .await?;
    if !node.is_null() {
        display = py_str(&node["display"]);
        let names = crate::txn::with_client(db, |c| {
            let node = &node;
            async move { crate::read::fact_get::alias_retry_names(c, name, node).await }
        })
        .await?;
        keys.extend(names.iter().map(|n| crate::stores::cortex::norm_key(n)));
    }
    let mut events: Vec<(f64, Value)> = Vec::new();
    // 1. canonical fact history.
    let cortex = ready.cortex.snapshot();
    for r in &cortex.records {
        if !keys.contains(&r.key.0) {
            continue;
        }
        events.push((
            r.asserted_at,
            json!({"t": r.asserted_at, "kind": "fact_set",
                   "summary": format!("{} = {}", r.attribute, r.value),
                   "refs": {"attribute": r.attribute}}),
        ));
        if let Some(at) = r.superseded_at.filter(|t| *t != 0.0) {
            let by = r
                .superseded_by_value
                .as_deref()
                .filter(|v| !v.is_empty())
                .map(|v| format!(" by {v}"))
                .unwrap_or_default();
            events.push((
                at,
                json!({"t": at, "kind": "superseded",
                       "summary": format!("{}: {} superseded{by}", r.attribute, r.value),
                       "refs": {"attribute": r.attribute}}),
            ));
        }
    }
    drop(cortex);
    // 2 + 3. source entries and edges need the graph node.
    if let Some(id) = node.get("id").and_then(Value::as_i64) {
        let lim = limit as i64;
        let rows = crate::txn::with_client(db, |c| async move {
            c.query(
                "SELECT DISTINCT en.id, en.band, en.source, en.ts, en.text, en.episode_title \
                 FROM facts f JOIN memory_traces t ON t.entity_norm = f.entity_norm \
                 JOIN entries en ON en.id = t.entry_id \
                 WHERE f.entity_id = $1 AND f.status = 'current' \
                 ORDER BY en.ts DESC LIMIT $2",
                &[&id, &lim],
            )
            .await
        })
        .await?;
        for r in rows {
            let entry_id: i64 = r.get(0);
            let ts: f64 = r.get(3);
            let text: Option<String> = r.get(4);
            let title: Option<String> = r.get(5);
            let mut refs = Map::new();
            refs.insert("entry_id".into(), json!(entry_id));
            if let Some(t) = title.filter(|t| !t.is_empty()) {
                refs.insert("episode_title".into(), json!(t));
            }
            let summary: String = text
                .unwrap_or_default()
                .chars()
                .take(ENTRY_SUMMARY_CHARS)
                .collect();
            events.push((
                ts,
                json!({"t": ts, "kind": "entry", "summary": summary, "refs": refs}),
            ));
        }
        let g = crate::storage::graph::load_graph(db).await?;
        let disp: std::collections::HashMap<i64, String> = g["entities"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|e| Some((e["id"].as_i64()?, py_str(&e["display"]))))
            .collect();
        let shown = |i: Option<i64>| {
            i.and_then(|i| disp.get(&i).cloned())
                .unwrap_or_else(|| "None".into())
        };
        for e in g["edges"].as_array().into_iter().flatten() {
            let (src, dst) = (e["src_id"].as_i64(), e["dst_id"].as_i64());
            if src != Some(id) && dst != Some(id) {
                continue;
            }
            let Some(at) = e["asserted_at"].as_f64().filter(|t| *t != 0.0) else {
                continue;
            };
            let relation = e["relation"].as_str().unwrap_or_default();
            events.push((
                at,
                json!({"t": at, "kind": "edge",
                       "summary": format!("{} \u{2014}{relation}\u{2192} {}", shown(src), shown(dst)),
                       "refs": {"relation": relation}}),
            ));
        }
    }
    // 4. lessons whose task or about names the entity.
    if !crate::mutants::active("mcp-history-chain-no-lessons") {
        crate::forget_tools::ensure_lessons_refreshed(ready)
            .await
            .map_err(anyhow::Error::msg)?;
        let lessons = ready.lessons.snapshot();
        for r in lessons.current_records() {
            let about = r
                .about
                .as_deref()
                .filter(|a| !a.is_empty())
                .map(crate::stores::cortex::norm_key);
            if keys.contains(&r.key.0) || about.is_some_and(|a| keys.contains(&a)) {
                events.push((
                    r.asserted_at,
                    json!({"t": r.asserted_at, "kind": "lesson", "summary": r.value,
                           "refs": {"task": r.entity, "aspect": r.attribute}}),
                ));
            }
        }
    }
    if events.is_empty() {
        return Ok(not_found());
    }
    events.sort_by(|x, y| x.0.total_cmp(&y.0));
    let keep = limit.max(1);
    let events: Vec<Value> = events
        .split_off(events.len().saturating_sub(keep))
        .into_iter()
        .map(|e| e.1)
        .collect();
    Ok(json!({"found": true, "entity": display, "count": events.len(), "events": events}))
}

/// `str(value)` for a display name (Python prints a missing one as None).
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

/// `(r.tx_time or r.asserted_at)`: a zero transaction time falls back too.
fn tx_or_asserted(r: &CortexRecord) -> f64 {
    r.tx_time.filter(|t| *t != 0.0).unwrap_or(r.asserted_at)
}

/// `service.history`: a set slot's member events, or the slot's versions,
/// oldest first, filtered to `cutoff` when given (echoed as `as_of`).
fn history(
    store: &CortexStore,
    entity: &str,
    attribute: &str,
    cutoff: Option<f64>,
    relative_age: bool,
    now: f64,
) -> Value {
    let mut out = Map::new();
    if store.slot_kind(entity, attribute) == Some("set") {
        // Each member adds an "added" event and, once removed, a "removed"
        // one; ties break adds before removes, then by member row order.
        let mut versions: Vec<(f64, (u8, usize), Value)> = Vec::new();
        for (idx, r) in store.member_rows(entity, attribute).into_iter().enumerate() {
            versions.push((
                r.asserted_at,
                (0, idx),
                json!({"value": r.value, "event": "added", "at": r.asserted_at}),
            ));
            if r.status == "removed"
                && let Some(at) = r.superseded_at
                && !crate::mutants::active("mcp-history-set-no-removed")
            {
                versions.push((
                    at,
                    (1, idx),
                    json!({"value": r.value, "event": "removed", "at": at}),
                ));
            }
        }
        if let Some(c) = cutoff {
            versions.retain(|v| v.0 <= c);
        }
        if !crate::mutants::active("mcp-history-set-unsorted") {
            versions.sort_by(|x, y| x.0.total_cmp(&y.0).then(x.1.cmp(&y.1)));
        }
        out.insert("kind".into(), json!("set"));
        out.insert("entity".into(), json!(entity));
        out.insert("attribute".into(), json!(attribute));
        out.insert("count".into(), json!(versions.len()));
        out.insert(
            "versions".into(),
            Value::Array(versions.into_iter().map(|v| v.2).collect()),
        );
    } else {
        let key = slot_key(entity, attribute);
        let mut recs: Vec<&CortexRecord> = store.records.iter().filter(|r| r.key == key).collect();
        let order: fn(&CortexRecord) -> f64 =
            if crate::mutants::active("mcp-history-asserted-order") {
                |r| r.asserted_at
            } else {
                tx_or_asserted
            };
        if let Some(c) = cutoff
            && !crate::mutants::active("mcp-history-ignore-as-of")
        {
            recs.retain(|r| tx_or_asserted(r) <= c);
        }
        recs.sort_by(|x, y| order(x).total_cmp(&order(y)));
        out.insert("entity".into(), json!(entity));
        out.insert("attribute".into(), json!(attribute));
        out.insert("count".into(), json!(recs.len()));
        out.insert(
            "versions".into(),
            Value::Array(
                recs.iter()
                    .map(|r| Value::Object(record_to_dict_age(r, ANNOTATE, now, relative_age)))
                    .collect(),
            ),
        );
    }
    if let Some(c) = cutoff {
        // A NaN or infinite cutoff is null here, as in Python's structured
        // content; Python's text block spells it `NaN` / `Infinity`
        // (declared in divergences.md).
        out.insert("as_of".into(), json!(c));
    }
    Value::Object(out)
}

#[cfg(test)]
mod tests {
    use crate::mcp::as_of::epoch;
    use serde_json::json;

    #[test]
    fn as_of_epochs_match_python_parse_as_of() {
        // Python's `_parse_as_of` answers, recorded with CPython 3.11.
        for (raw, want) in [
            ("2026-10-01T12:00:00+00:00", 1790856000.0),
            ("2026-10-01T12:30:45.5-05:30", 1790877645.5),
            ("20261001T1230Z", 1790857800.0),
            ("2026-W40-4T00:00+01:00", 1790809200.0),
            (" 1.5e9 ", 1500000000.0),
            ("1_000", 1000.0),
            ("2026-10-01T12:30:45,123456789+00:00", 1790857845.123456),
            // An offset with zero whole seconds is UTC (its fraction dropped).
            ("2026-10-01T12:30:45+00:00:00.5", 1790857845.0),
            ("2026-10-01T12:30:45-00:00:00.5", 1790857845.0),
            ("2026-10-01T12:30:45+00:00:01.5", 1790857843.5),
            ("2026-10-01T12:30:45-00:00:01.5", 1790857846.5),
            ("2026-10-01T12:30:45+05:30:00.25", 1790838044.75),
        ] {
            assert_eq!(epoch(&json!(raw)), Some(want), "{raw}");
        }
        // Far from today the exact division and a float one part ways
        // (CPython 3.11: 147264870891.66766).
        assert_eq!(
            epoch(&json!("6636-08-21T12:34:51.667663+00:00")),
            Some(147264870891.66766)
        );
        assert_eq!(
            epoch(&json!("0001-01-02T00:00:00.5+00:00")),
            Some(-62135510399.5)
        );
        assert_eq!(epoch(&json!(12.5)), Some(12.5));
        assert_eq!(epoch(&serde_json::Value::Null), None);
    }
}
