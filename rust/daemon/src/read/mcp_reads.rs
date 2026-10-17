//! Read tools over MCP: `memory_recent` (spec "memory_recent over MCP") and
//! `memory_get` (spec "memory_get over MCP").

use crate::bank::Bank;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::mcp_search::{compact_entry, replaced_by, strings};
use crate::search::{annotate_supersession_in, entry_json};
use serde_json::{Value, json};

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("memory_recent", mcp_memory_recent),
        ("memory_get", mcp_memory_get),
    ]
}

/// `memory_recent` (mcp_server.py:1320-1343): `service.recent`, compact
/// entries unless `verbose`.
fn mcp_memory_recent<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let verbose = a.get("verbose").and_then(Value::as_bool).unwrap_or(false);
        // The binder admits any integer >= 1; Python slices with it as is,
        // so one past u64 (or usize) takes everything.
        let n = match a.get("n") {
            Some(v) => v
                .as_u64()
                .map_or(usize::MAX, |n| usize::try_from(n).unwrap_or(usize::MAX)),
            None => 10,
        };
        let filters = Filters {
            sources: strings(a.get("sources")),
            episodes: strings(a.get("episodes")),
            // `set(_norm(tags)) if tags else None`: tags that normalise to
            // nothing leave an empty (falsy) filter, which filters nothing.
            tags: strings(a.get("tags"))
                .map(|_| crate::store::normalize_tags(a.get("tags")))
                .filter(|t| !t.is_empty()),
        };
        let ready = app
            .service
            .ensure_init()
            .await
            .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
        let bank = ready.bank.clone();
        let band_order: Vec<String> = app
            .service
            .config
            .memory
            .band_specs
            .iter()
            .map(|b| b.name.clone())
            .collect();
        let result = tokio::task::spawn_blocking(move || {
            let bank = bank.read().expect("bank lock");
            recent(&bank, n, &filters, &band_order)
        })
        .await
        .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?
        // Python's TypeError in prose: `invalid_argument` with its text.
        .map_err(ToolError::Invalid)?;
        Ok(if verbose { result } else { compact(result) })
    })
}

struct Filters {
    sources: Option<Vec<String>>,
    episodes: Option<Vec<String>>,
    tags: Option<Vec<String>>,
}

impl Filters {
    /// OR within each filter, AND across them; episodes match exactly
    /// (no subtree, unlike search). With a tag filter, an entry that passed
    /// the others and stores a list or object tag fails as Python's
    /// `set(entry.tags)` does, with its TypeError text for the first
    /// unhashable tag.
    fn admit(&self, e: &crate::bank::Entry) -> Result<bool, String> {
        if !self.sources.as_ref().is_none_or(|s| s.contains(&e.source))
            || !self
                .episodes
                .as_ref()
                .is_none_or(|ids| e.episode_id.as_ref().is_some_and(|id| ids.contains(id)))
        {
            return Ok(false);
        }
        let Some(want) = &self.tags else {
            return Ok(true);
        };
        if let Some(t) = e.tags.iter().find(|t| t.is_array() || t.is_object()) {
            let kind = if t.is_array() { "list" } else { "dict" };
            return Err(format!("unhashable type: '{kind}'"));
        }
        Ok(e.tags
            .iter()
            .filter_map(Value::as_str)
            .any(|t| want.iter().any(|w| w == t)))
    }
}

/// `MemoryService.recent` (service.py:2171-2216): the admitted resident
/// entries, newest `(timestamp, seq)` first, at most `n`, as
/// `_entry_to_dict` (no score) with supersession annotated. No side effects.
/// Entries are examined band by band, as Python walks `cms.bands`, so a
/// failing tag is the one Python meets first (new stores sit at the end of
/// the vector but in the first band).
fn recent(
    bank: &Bank,
    n: usize,
    filters: &Filters,
    band_order: &[String],
) -> Result<Value, String> {
    let rank = |band: &str| {
        band_order
            .iter()
            .position(|b| b == band)
            .unwrap_or(usize::MAX)
    };
    let mut scan: Vec<usize> = (0..bank.entries.len()).collect();
    scan.sort_by_key(|&i| rank(&bank.entries[i].band));
    let mut order = Vec::new();
    for i in scan {
        if filters.admit(&bank.entries[i])? {
            order.push(i);
        }
    }
    order.sort_by(|&x, &y| {
        let (a, b) = (&bank.entries[x], &bank.entries[y]);
        b.ts.total_cmp(&a.ts).then(b.seq.cmp(&a.seq))
    });
    if crate::mutants::active("mcp-recent-oldest-first") {
        order.reverse();
    }
    order.truncate(n);
    let mut served: Vec<(usize, Value)> = order
        .into_iter()
        .map(|i| {
            let mut d = entry_json(&bank.entries[i], 0.0);
            if let Value::Object(m) = &mut d {
                m.remove("score");
            }
            (i, d)
        })
        .collect();
    annotate_supersession_in(bank, &mut served);
    let entries: Vec<Value> = served.into_iter().map(|(_, d)| d).collect();
    Ok(json!({"count": entries.len(), "entries": entries}))
}

/// `_FADED_NOTE` (`mcp_server.py:718`).
const FADED_NOTE: &str = "No entry with this id: a wrong id, a forgotten or evicted entry, or a bank without Postgres (file mode cannot read ids). Search again for a current id.";
/// `_SUPERSESSION_SERVICE_KEYS` (`mcp_server.py:910`).
const SUPERSESSION_SERVICE_KEYS: [&str; 5] = [
    "superseded_at",
    "superseded_by_text",
    "superseded_by_id",
    "supersession_verified",
    "superseded_by_current",
];

fn faded() -> Value {
    json!({"found": false, "faded": true, "note": FADED_NOTE})
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// One `entries` row as `PostgresStorage.get_entry` reads it.
struct EntryRow {
    id: i64,
    text: String,
    source: String,
    reinforcements: i32,
    explicit_reinforcements: i32,
    access_count: i32,
    superseded_at: Option<f64>,
    superseded_by_text: Option<String>,
}

/// `memory_get` (mcp_server.py:1518-1539) over `service.get_entry`
/// (service.py:6088-6138).
fn mcp_memory_get<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let internal = |e: anyhow::Error| ToolError::Internal(e);
        let ready = app
            .service
            .ensure_init()
            .await
            .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
        // An id past BIGINT reaches Python's query as numeric and matches
        // no row: faded, as any unknown id.
        let Some(id) = call.args.get("entry_id").and_then(Value::as_i64) else {
            return Ok(faded());
        };
        let db = ready.storage.client();
        // The read and its bump in one transaction, the row locked: Python
        // holds the service lock across both, so a concurrent get answers
        // the count after this one's bump, never the same count.
        let bump = !crate::mutants::active("mcp-get-no-db-bump");
        let row = crate::txn::run(db, |db| async move {
            let r = db
                .query_opt(
                    "SELECT id, text, source, reinforcements, explicit_reinforcements, \
                     access_count, superseded_at, superseded_by_text FROM entries WHERE id = $1 \
                     FOR UPDATE",
                    &[&id],
                )
                .await?;
            if r.is_some() && bump {
                db.execute(
                    "UPDATE entries SET access_count = access_count + 1 WHERE id = $1",
                    &[&id],
                )
                .await?;
            }
            Ok::<_, anyhow::Error>(r.map(|r| EntryRow {
                id: r.get(0),
                text: r.get(1),
                source: r.get(2),
                reinforcements: r.get(3),
                explicit_reinforcements: r.get(4),
                access_count: r.get(5),
                superseded_at: r.get(6),
                superseded_by_text: r.get(7),
            }))
        })
        .await
        .map_err(internal)?;
        let Some(row) = row else {
            return Ok(faded());
        };
        // The resident copy follows the database bump.
        {
            let bank = ready.bank.read().expect("bank lock");
            if let Some(e) = bank.entries.iter().find(|e| e.id == id) {
                e.access_count
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
        }
        let facts = crate::txn::with_client(db, |db| async move {
            Ok::<_, anyhow::Error>(
                db.query(
                    "SELECT f.entity, f.attribute, f.value FROM facts f \
                     JOIN memory_traces t ON f.entity_norm = t.entity_norm \
                     AND f.attribute_norm = t.attribute_norm \
                     WHERE t.entry_id = $1 AND f.status = 'current' \
                     ORDER BY f.entity_norm, f.attribute_norm",
                    &[&id],
                )
                .await?
                .iter()
                .map(|r| {
                    json!({"entity": r.get::<_, String>(0), "attribute": r.get::<_, String>(1),
                           "value": r.get::<_, String>(2)})
                })
                .collect::<Vec<Value>>(),
            )
        })
        .await
        .map_err(internal)?;
        let cfg = &app.service.config.memory;
        if cfg.retrieval_log_enabled && !crate::mutants::active("mcp-get-no-use-credit") {
            let window = cfg.retrieval_log_use_window_seconds;
            let (header, transport) = (
                call.ident.header_session.clone(),
                call.ident.transport_session.clone(),
            );
            // `_record_retrieval_use`: never raises.
            if let Err(e) = crate::txn::run(db, |db| async move {
                credit_retrieval_use(db, id, header, transport, window).await
            })
            .await
            {
                eprintln!("retrieval-use label failed: {e:#}");
            }
        }
        let mut out = serde_json::Map::new();
        out.insert("found".into(), json!(true));
        out.insert("entry_id".into(), json!(row.id));
        out.insert("text".into(), json!(row.text));
        out.insert("source".into(), json!(row.source));
        out.insert("reinforcements".into(), json!(row.reinforcements));
        out.insert(
            "explicit_reinforcements".into(),
            json!(row.explicit_reinforcements),
        );
        // +1 for the bump just applied.
        out.insert(
            "access_count".into(),
            json!(i64::from(row.access_count) + 1),
        );
        out.insert("consolidated_into".into(), Value::Array(facts));
        if let Some(at) = row.superseded_at {
            out.insert("superseded".into(), json!(true));
            out.insert("superseded_at".into(), json!(at));
            out.insert("superseded_by_text".into(), json!(row.superseded_by_text));
            let bank = ready.bank.read().expect("bank lock");
            annotate_row_supersession(&bank, id, row.superseded_by_text.as_deref(), &mut out);
        }
        // The MCP layer: `replaced_by` when there is a successor text, and the
        // raw supersession keys never served.
        let superseded = out.get("superseded") == Some(&Value::Bool(true));
        if superseded {
            let has_text = out
                .get("superseded_by_text")
                .and_then(Value::as_str)
                .is_some_and(|t| !t.is_empty());
            if has_text {
                let pointer = replaced_by(&Value::Object(out.clone()));
                out.insert("replaced_by".into(), pointer);
            }
            for k in SUPERSESSION_SERVICE_KEYS {
                out.shift_remove(k);
            }
        }
        Ok(Value::Object(out))
    })
}

/// `credit_retrieval_use` (storage/postgres.py:2189) for `used_via="get"`:
/// the most recent event in the window served `entry_id` under the caller's
/// session (`_resolve_writer`) gains one `retrieval_uses` row.
async fn credit_retrieval_use(
    db: &tokio_postgres::Client,
    entry_id: i64,
    header: Option<String>,
    transport: Option<String>,
    window: f64,
) -> anyhow::Result<()> {
    let t = now();
    let session =
        crate::read::identity::session_id(db, header.as_deref(), transport.as_deref(), t).await;
    let served = json!([{"entry_id": entry_id}]);
    let event: Option<i64> = db
        .query_opt(
            "SELECT id FROM retrieval_events WHERE session_id IS NOT DISTINCT FROM $1 \
             AND created_at >= $2 AND served @> $3 ORDER BY created_at DESC, id DESC LIMIT 1",
            &[&session, &(t - window), &served],
        )
        .await?
        .map(|r| r.get(0));
    if let Some(event) = event {
        db.execute(
            "INSERT INTO retrieval_uses (event_id, entry_id, used_via, created_at) \
             VALUES ($1, $2, 'get', $3) ON CONFLICT DO NOTHING",
            &[&event, &entry_id, &t],
        )
        .await?;
    }
    Ok(())
}

/// `_annotate_supersession` for a row the CMS may not hold: the successor is
/// the resident entry with the replacement text (the row itself excluded by
/// id); with several, the one still current; else none.
fn annotate_row_supersession(
    bank: &Bank,
    id: i64,
    successor_text: Option<&str>,
    out: &mut serde_json::Map<String, Value>,
) {
    let mut found: Vec<&crate::bank::Entry> = match successor_text {
        Some(t) if !t.is_empty() => bank
            .entries
            .iter()
            .filter(|e| e.text == t && e.id != id)
            .collect(),
        _ => Vec::new(),
    };
    if found.len() > 1 {
        found.retain(|e| e.superseded_at.is_none());
    }
    let one = (found.len() == 1).then(|| found[0]);
    out.insert("superseded_by_id".into(), json!(one.map(|e| e.id)));
    out.insert(
        "supersession_verified".into(),
        json!(one.is_some_and(|e| e.source == "correction" || e.source == "consolidation")),
    );
    out.insert(
        "superseded_by_current".into(),
        json!(one.is_some_and(|e| e.superseded_at.is_none())),
    );
}

/// `_compact_entries` with no text budget: `count` stays as served.
fn compact(mut result: Value) -> Value {
    if let Some(Value::Array(entries)) = result.get_mut("entries") {
        for e in entries.iter_mut() {
            *e = compact_entry(e, None);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bank::Entry;

    fn entry(id: i64, band: &str, source: &str, tags: Vec<Value>) -> Entry {
        Entry {
            id,
            band: band.into(),
            source: source.into(),
            tags,
            ..Default::default()
        }
    }

    fn filters(sources: Option<&[&str]>, tags: Option<&[&str]>) -> Filters {
        let owned = |v: &[&str]| v.iter().map(|s| s.to_string()).collect();
        Filters {
            sources: sources.map(owned),
            episodes: None,
            tags: tags.map(owned),
        }
    }

    #[test]
    fn an_unhashable_tag_fails_only_where_python_builds_the_set() {
        let e = entry(
            1,
            "a",
            "notes",
            vec![json!("deploy"), json!(["x"]), json!({"k": 1})],
        );
        let err = filters(None, Some(&["deploy"])).admit(&e).unwrap_err();
        assert_eq!(err, "unhashable type: 'list'");
        let dict_first = entry(2, "a", "notes", vec![json!({"k": 1}), json!(["x"])]);
        let err = filters(None, Some(&["deploy"]))
            .admit(&dict_first)
            .unwrap_err();
        assert_eq!(err, "unhashable type: 'dict'");
        // No tag filter, or another filter rejects the entry first: no set().
        assert_eq!(filters(None, None).admit(&e), Ok(true));
        assert_eq!(
            filters(Some(&["other"]), Some(&["deploy"])).admit(&e),
            Ok(false)
        );
    }

    #[test]
    fn the_first_failing_entry_is_met_band_by_band() {
        let bank = Bank {
            dim: 0,
            matrix: Vec::new(),
            entries: vec![
                entry(1, "deep", "notes", vec![json!(["x"])]),
                entry(2, "hot", "notes", vec![json!({"k": 1})]),
            ],
        };
        let order = ["hot".to_string(), "deep".to_string()];
        let err = recent(&bank, 10, &filters(None, Some(&["t"])), &order).unwrap_err();
        assert_eq!(err, "unhashable type: 'dict'");
    }
}
