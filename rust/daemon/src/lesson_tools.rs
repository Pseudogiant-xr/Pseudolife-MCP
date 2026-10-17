//! `memory_lesson_search` over MCP (spec LS1-LS10). Python:
//! `mcp_server.py:2062-2093`; `service.py` `_lesson_record_to_dict`
//! (411-430), `lesson_search` (5346-5361), `_cortex_change_index`
//! (5399-5412), `_annotate_lesson_staleness` (5414-5443),
//! `_log_lesson_search` (5979-6003); `storage/postgres.py`
//! `add_lesson_search_event` (2411-2428). The store is `stores/lessons.rs`.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::board::text::py_strip;
use crate::episodes::Caller;
use crate::fact_set::internal;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::service::Ready;
use crate::stores::cortex::{CortexStore, norm_key};
use crate::stores::lessons::LessonRecord;

/// The compact projection's keys (`memory_lesson_search` without `verbose`).
const COMPACT_KEYS: [&str; 10] = [
    "task",
    "aspect",
    "lesson",
    "about",
    "polarity",
    "outcome",
    "confidence",
    "score",
    "re_verify",
    "re_verify_reason",
];

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_lesson_search", mcp_memory_lesson_search)]
}

fn sorted_unique(v: &[String]) -> Vec<String> {
    let mut out: Vec<String> = v.to_vec();
    out.sort();
    out.dedup();
    out
}

/// `_lesson_record_to_dict`.
pub(crate) fn record_to_dict(r: &LessonRecord) -> Map<String, Value> {
    let mut d = Map::new();
    d.insert("task".into(), json!(r.entity));
    d.insert("aspect".into(), json!(r.attribute));
    d.insert("lesson".into(), json!(r.value));
    d.insert("about".into(), json!(r.about));
    d.insert("polarity".into(), json!(r.polarity));
    d.insert("outcome".into(), json!(r.outcome));
    d.insert("status".into(), json!(r.status));
    d.insert(
        "confidence".into(),
        json!(crate::health::round4(r.confidence)),
    );
    d.insert("origin".into(), json!(r.origin));
    d.insert("provenance".into(), json!(sorted_unique(&r.provenance)));
    d.insert("asserted_at".into(), json!(r.asserted_at));
    d.insert("last_confirmed".into(), json!(r.last_confirmed));
    d.insert("supersedes_value".into(), json!(r.supersedes_value));
    d.insert("superseded_by_value".into(), json!(r.superseded_by_value));
    d.insert("superseded_at".into(), json!(r.superseded_at));
    d
}

/// `_cortex_change_index`: the latest assertion or supersession per
/// normalised entity, over every personal-cortex record.
fn cortex_change_index(store: &CortexStore) -> HashMap<String, f64> {
    let mut idx: HashMap<String, f64> = HashMap::new();
    for r in &store.records {
        let ts = r.asserted_at.max(r.superseded_at.unwrap_or(0.0));
        let k = norm_key(&r.entity);
        if ts > *idx.get(&k).unwrap_or(&0.0) {
            idx.insert(k, ts);
        }
    }
    idx
}

/// `_annotate_lesson_staleness`: a row whose `about` (or task) resolves,
/// directly or through the graph entity's canonical, to a cortex entity
/// whose facts changed after the lesson was asserted or confirmed gains
/// `re_verify` and its reason. No change index means no annotation (and
/// no graph lookup).
async fn annotate_staleness(
    db: &tokio_postgres::Client,
    store: &CortexStore,
    rows: &mut [Map<String, Value>],
) -> anyhow::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let idx = cortex_change_index(store);
    if idx.is_empty() {
        return Ok(());
    }
    let text = |row: &Map<String, Value>, k: &str| {
        row.get(k)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    };
    let number =
        |row: &Map<String, Value>, k: &str| row.get(k).and_then(Value::as_f64).unwrap_or(0.0);
    for row in rows.iter_mut() {
        let raw = text(row, "about")
            .or_else(|| text(row, "task"))
            .unwrap_or_default();
        let name = py_strip(&raw).to_string();
        if name.is_empty() {
            continue;
        }
        let mut k = norm_key(&name);
        if !idx.contains_key(&k) && !crate::mutants::active("mcp-lesson-search-no-graph-resolve") {
            let probe = crate::storage::graph::norm_name(&name);
            let node = crate::txn::with_client(db, |c| async move {
                crate::storage::graph::find_entity_body(c, &probe).await
            })
            .await?;
            if !node.is_null() {
                k = norm_key(node["canonical"].as_str().unwrap_or(""));
            }
        }
        let changed = idx.get(&k).copied().unwrap_or(0.0);
        let seen = number(row, "asserted_at").max(number(row, "last_confirmed"));
        if changed != 0.0 && seen != 0.0 && changed > seen {
            row.insert("re_verify".into(), json!(true));
            row.insert(
                "re_verify_reason".into(),
                json!(format!("facts about {name} changed since this lesson")),
            );
        }
    }
    Ok(())
}

fn mcp_memory_lesson_search<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let query = a
            .get("query")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let m = &app.service.config.memory;
        // The binder admits any integer >= 1; past usize, everything.
        let top_k = match a.get("top_k") {
            Some(v) => v
                .as_u64()
                .map_or(usize::MAX, |n| usize::try_from(n).unwrap_or(usize::MAX)),
            None => usize::try_from(m.lessons_top_k.max(0)).unwrap_or(0),
        };
        let verbose = a.get("verbose").and_then(Value::as_bool).unwrap_or(false);
        let floor = 0.0f64.max(m.lessons_min_confidence);
        let log = m.retrieval_log_enabled;
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let caller = Caller {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let mut out = lesson_search(&ready, &caller, query, top_k, floor, log).await?;
        if !verbose && let Some(Value::Array(entries)) = out.get_mut("entries") {
            for e in entries.iter_mut() {
                if let Value::Object(m) = e {
                    let mut c = Map::new();
                    for k in COMPACT_KEYS {
                        if let Some(v) = m.get(k) {
                            c.insert(k.into(), v.clone());
                        }
                    }
                    *m = c;
                }
            }
        }
        Ok(Value::Object(out))
    })
}

/// `service.lesson_search` under the service lock: the query embedding,
/// the lesson search, the rows with `score`, the staleness annotation and
/// the `lesson_search_events` row.
async fn lesson_search(
    ready: &Arc<Ready>,
    caller: &Caller,
    query: String,
    top_k: usize,
    floor: f64,
    log: bool,
) -> Result<Map<String, Value>, ToolError> {
    let ws = ready.write.lock().await;
    let embedder = ready.embedder.clone();
    let q_text = query.clone();
    let q = tokio::task::spawn_blocking(move || embedder.embed_query(&q_text))
        .await
        .map_err(internal)?
        .map_err(internal)?;
    let lessons = ready.lessons.clone();
    let hits = lessons.search(&q, top_k, floor).map_err(internal)?;
    let mut rows: Vec<Map<String, Value>> = hits
        .iter()
        .map(|&(i, s)| {
            let mut d = record_to_dict(&lessons.records[i]);
            d.insert("score".into(), json!(crate::health::round4(s)));
            d
        })
        .collect();
    let db = ready.storage.client();
    let cortex = ready.cortex.snapshot();
    annotate_staleness(db, &cortex, &mut rows)
        .await
        .map_err(internal)?;
    drop(cortex);
    if log && !crate::mutants::active("mcp-lesson-search-no-log") {
        // `_log_lesson_search`: never raises.
        let served: Vec<Value> = hits
            .iter()
            .enumerate()
            .map(|(rank, &(i, s))| {
                let r = &lessons.records[i];
                json!({"entity_norm": r.key.0, "attribute_norm": r.key.1, "rank": rank,
                       "score": crate::health::round4(s)})
            })
            .collect();
        let logged = match ws.resolve_session(caller) {
            Ok(session) => {
                let episode = session
                    .as_deref()
                    .filter(|s| !s.is_empty())
                    .and_then(|s| ws.log.open_leaf_for(Some(s)))
                    .map(|i| ws.log.episodes[i].id.clone());
                let served = Value::Array(served);
                let (q, session, episode) = (&query, &session, &episode);
                let t = crate::episodes::now();
                crate::txn::run(db, |c| {
                    let served = &served;
                    async move {
                        c.execute(
                            "INSERT INTO lesson_search_events \
                             (query_text, session_id, episode_id, served, created_at) \
                             VALUES ($1, $2, $3, $4, $5)",
                            &[q, session, episode, served, &t],
                        )
                        .await
                        .map(|_| ())
                    }
                })
                .await
                .map_err(|e| e.to_string())
            }
            Err(e) => Err(e),
        };
        if let Err(e) = logged {
            crate::read::stats::record_retrieval_log_error();
            eprintln!("lesson-search log failed: {e}");
        }
    }
    drop(ws);
    let mut out = Map::new();
    out.insert("count".into(), json!(rows.len()));
    out.insert(
        "entries".into(),
        Value::Array(rows.into_iter().map(Value::Object).collect()),
    );
    Ok(out)
}
