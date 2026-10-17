//! `memory_history` over MCP (spec "memory_history over MCP", HI1-HI7):
//! slot mode. Python: `mcp_server.py:1804-1828`; `service.py`
//! `_parse_as_of` (5746-5756) and `history` (5758-5831); `memory/cortex.py`
//! `members(include_removed=True)` (1182-1191), `slot_kind` (1193-1216) and
//! `records_for` (1526-1528). Chain mode (`attribute` omitted, `service.chain`)
//! needs the lesson store and the graph's entity reads, so it answers the
//! declared `not_implemented` stub (`divergences.md`).

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
            // Chain mode is not served yet: exactly the unserved-tool stub.
            return Err(ToolError::refused(
                "not_implemented",
                Some("memory_history is not yet served by the Rust daemon"),
            ));
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
