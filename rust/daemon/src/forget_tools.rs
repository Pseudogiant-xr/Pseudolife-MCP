//! `memory_forget` (spec FG1-FG16): `mcp_server.memory_forget` over the four
//! stores. Scope `memory` is `service.delete` → `CMS.delete_entries` with the
//! `_forget_entries_locked` cascade (`storage/postgres.py`
//! `forget_entry_ids`, `retire_lessons_for_entries`); `fact` is
//! `service.cortex_forget` (a hard delete); `world` and `lesson` are
//! `service.world_forget` / `service.lesson_forget` (a retire with an
//! audit row each).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::config::MemoryConfig;
use crate::episodes::{self, WriteState};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::service::{Ready, Service};
use crate::store::py_strip;
use crate::stores::cortex::norm_key;
use crate::stores::lessons::SOURCE_FORGOTTEN;

const FILTERS: [&str; 5] = ["text", "substring", "source", "episode", "tag"];
const NOTHING_DELETED: &str =
    "Nothing deleted: no entry matched every filter (filters AND-combine).";
const NOTHING_REMOVED: &str = "Nothing removed: no slot matches this entity/attribute.";
/// `_DIGEST_CURSOR_KEY` (`service_dream.py`).
const DIGEST_CURSOR_KEY: &str = "session_digest_cursor";

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![("memory_forget", mcp_memory_forget)]
}

fn opt_str<'a>(a: &'a Map<String, Value>, k: &str) -> Option<&'a str> {
    a.get(k).and_then(Value::as_str)
}

/// `memory_forget` on the MCP surface (`mcp_server.py:2098-2156`): the
/// selector checks, then the scope's service body, detached from the
/// request under the service lock.
fn mcp_memory_forget<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let scope = opt_str(a, "scope").unwrap_or("").to_string();
        if scope == "memory" {
            // `any((text, substring, source, episode, tag))`: empty strings
            // are no filter here, but still filters below.
            if !FILTERS
                .iter()
                .any(|k| opt_str(a, k).is_some_and(|v| !v.is_empty()))
            {
                return Ok(json!({"error": "filter_required", "filters": FILTERS}));
            }
        } else if opt_str(a, "entity").is_none_or(str::is_empty) {
            return Ok(json!({"error": "entity_required", "scope": scope}));
        }
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let svc = app.service.clone();
        let args = a.clone();
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { forget(&svc, &r2, &scope, &args).await })
            .await
            .map_err(internal)?
    })
}

async fn forget(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    scope: &str,
    a: &Map<String, Value>,
) -> Result<Value, ToolError> {
    let cfg = &svc.config.memory;
    let mut out = match scope {
        "memory" => delete(ready, cfg, a).await?,
        _ => {
            let entity = opt_str(a, "entity").unwrap_or("");
            let attribute = opt_str(a, "attribute");
            match scope {
                "fact" => cortex_forget(svc, ready, entity, attribute).await?,
                "world" => world_forget(svc, ready, entity, attribute).await?,
                _ => lesson_forget(svc, ready, entity, attribute).await?,
            }
        }
    };
    let (count_key, note) = if scope == "memory" {
        ("deleted_count", NOTHING_DELETED)
    } else {
        ("removed", NOTHING_REMOVED)
    };
    if let Some(m) = out.as_object_mut()
        && m.get(count_key).and_then(Value::as_i64) == Some(0)
        && !m.contains_key("error")
        && !m.contains_key("note")
    {
        m.insert("note".into(), json!(note));
    }
    Ok(out)
}

// ── scope memory ─────────────────────────────────────────────────────────

/// One resident entry, as the cascade reads it.
struct Doomed {
    id: i64,
    text: String,
    source: String,
    episode_id: Option<String>,
    superseded: bool,
}

/// `service.delete` (`service.py:3325-3396`) over `CMS.delete_entries`
/// (`cms.py:2618-2707`).
async fn delete(
    ready: &Arc<Ready>,
    cfg: &MemoryConfig,
    a: &Map<String, Value>,
) -> Result<Value, ToolError> {
    let threshold = cfg.delete_confirm_threshold;
    let confirm = a
        .get("confirm_bulk")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let max_removed = (!confirm && threshold > 0).then_some(threshold);
    let (text, substring, source, episode) = (
        opt_str(a, "text"),
        opt_str(a, "substring"),
        opt_str(a, "source"),
        opt_str(a, "episode"),
    );
    let tag_norm = opt_str(a, "tag").map(|t| py_strip(t).to_lowercase());

    let ws = ready.write.lock().await;
    // Match first, mutate second, bands in configured order.
    let matches: Vec<Doomed> = {
        let bank = ready.bank.read().expect("bank lock");
        let band_pos = |name: &str| cfg.bands.iter().position(|b| b == name).unwrap_or(0);
        let mut hits: Vec<usize> = (0..bank.entries.len())
            .filter(|&i| {
                let e = &bank.entries[i];
                text.is_none_or(|t| e.text == t)
                    && substring.is_none_or(|s| e.text.contains(s))
                    && source.is_none_or(|s| e.source == s)
                    && episode.is_none_or(|ep| e.episode_id.as_deref() == Some(ep))
                    && tag_norm
                        .as_deref()
                        .is_none_or(|t| e.tags.iter().any(|x| x.as_str() == Some(t)))
            })
            .collect();
        hits.sort_by_key(|&i| (band_pos(&bank.entries[i].band), i));
        hits.iter()
            .map(|&i| {
                let e = &bank.entries[i];
                Doomed {
                    id: e.id,
                    text: e.text.clone(),
                    source: e.source.clone(),
                    episode_id: e.episode_id.clone(),
                    superseded: e.superseded_at.is_some(),
                }
            })
            .collect()
    };
    if let Some(limit) = max_removed
        && matches.len() as i64 > limit
    {
        let texts: Vec<&str> = matches.iter().take(20).map(|d| d.text.as_str()).collect();
        return Ok(json!({
            "deleted_count": 0,
            "error": "bulk_confirm_required",
            "would_delete": matches.len(),
            "threshold": limit,
            "sample_texts": texts,
            "hint": "pass confirm_bulk=true to remove them",
        }));
    }
    let retained = if matches.is_empty() {
        HashSet::new()
    } else {
        forget_entries(ready, &ws, &matches).await?
    };
    let removed: Vec<&Doomed> = matches
        .iter()
        .filter(|d| !retained.contains(&d.id))
        .collect();
    {
        let gone: HashSet<i64> = removed.iter().map(|d| d.id).collect();
        let mut bank = ready.bank.write().expect("bank lock");
        for i in (0..bank.entries.len()).rev() {
            if gone.contains(&bank.entries[i].id) {
                bank.remove(i);
            }
        }
    }
    let texts: Vec<&str> = removed.iter().take(20).map(|d| d.text.as_str()).collect();
    Ok(json!({"deleted_count": removed.len(), "deleted_texts": texts}))
}

/// `_load_digest_cursor` (`service_dream.py:3197-3210`) and the cascade's
/// `regenerate` union: the cursor the forget writes back.
fn digest_cursor(raw: Option<Value>, regenerate: &BTreeSet<String>) -> Result<Value, String> {
    let mut cur = Map::new();
    let mut existing: Vec<Value> = Vec::new();
    match raw {
        Some(Value::Object(raw)) => {
            let ts = match raw.get("ts") {
                None => 0.0,
                Some(Value::Number(n)) => {
                    n.to_string().parse::<f64>().map_err(|e| e.to_string())?
                }
                Some(Value::Bool(b)) => f64::from(u8::from(*b)),
                Some(Value::String(s)) => py_strip(s)
                    .parse::<f64>()
                    .map_err(|_| format!("could not convert string to float: {s:?}"))?,
                Some(other) => {
                    return Err(format!("float() argument must be a number, not {other}"));
                }
            };
            cur.insert("ts".into(), json!(ts));
            let retry = match raw.get("retry") {
                None => Value::Object(Map::new()),
                Some(Value::Object(m)) => Value::Object(m.clone()),
                Some(other) => return Err(format!("cannot convert retry to dict: {other}")),
            };
            cur.insert("retry".into(), retry);
            for key in ["regenerate", "context_deferred"] {
                if let Some(v) = raw.get(key) {
                    let items = match v {
                        Value::Array(a) => a.clone(),
                        Value::Object(m) => m.keys().map(|k| json!(k)).collect(),
                        Value::String(s) => s.chars().map(|c| json!(c.to_string())).collect(),
                        other => return Err(format!("{other} is not iterable")),
                    };
                    if key == "regenerate" {
                        existing = items.clone();
                    }
                    cur.insert(key.into(), Value::Array(items));
                }
            }
        }
        _ => {
            cur.insert("ts".into(), json!(0.0));
            cur.insert("retry".into(), Value::Object(Map::new()));
        }
    }
    // sorted(set(cur.get("regenerate", [])) | (roots & surviving_roots))
    let mut all: BTreeSet<String> = regenerate.clone();
    for v in existing {
        match v {
            Value::String(s) => {
                all.insert(s);
            }
            other => return Err(format!("'<' not supported for {other}")),
        }
    }
    cur.insert(
        "regenerate".into(),
        Value::Array(all.into_iter().map(Value::String).collect()),
    );
    Ok(Value::Object(cur))
}

/// What the forget transaction changed.
struct Forgotten {
    /// The slots it retired, `(entity_norm, attribute_norm)`.
    slots: Vec<(String, String)>,
    /// Whether any lesson retired (`_lesson_source_refresh`).
    lessons_retired: bool,
}

enum Fail {
    Db(tokio_postgres::Error),
    /// A `RuntimeError` (an incomplete write); rolled back.
    Runtime(&'static str),
}

impl From<tokio_postgres::Error> for Fail {
    fn from(e: tokio_postgres::Error) -> Self {
        Fail::Db(e)
    }
}

/// `_forget_entries_locked` (`service.py:3264-3323`): the synchronous
/// cascade before the resident delete. Returns the matched digests kept as
/// retired history.
async fn forget_entries(
    ready: &Arc<Ready>,
    ws: &WriteState,
    matches: &[Doomed],
) -> Result<HashSet<i64>, ToolError> {
    let root_of = |episode_id: Option<&str>| -> Option<String> {
        let mut ep = episode_id.and_then(|id| ws.log.get(id))?;
        let mut seen = HashSet::new();
        while let Some(parent) = ep.parent_id.as_deref() {
            if !seen.insert(ep.id.clone()) {
                break;
            }
            match ws.log.get(parent) {
                Some(p) => ep = p,
                None => return None,
            }
        }
        Some(ep.id.clone())
    };
    let roots: BTreeSet<String> = matches
        .iter()
        .filter_map(|d| root_of(d.episode_id.as_deref()))
        .collect();
    // A source/bulk forget retires matching digests as history too.
    let has_source = matches.iter().any(|d| d.source != "digest");
    let retained: HashSet<i64> = matches
        .iter()
        .filter(|d| {
            has_source
                && d.source == "digest"
                && (d.superseded || d.episode_id.as_ref().is_some_and(|e| roots.contains(e)))
                && !crate::mutants::active("mcp-forget-delete-digests")
        })
        .map(|d| d.id)
        .collect();
    let ids: Vec<i64> = matches
        .iter()
        .filter(|d| !retained.contains(&d.id))
        .map(|d| d.id)
        .collect();
    if ids.is_empty() {
        return Ok(HashSet::new());
    }
    let client = ready.storage.client();
    // `slots_for_entries(ids)`: the last row per slot names it (the largest
    // source id), as the resident retirement reads it.
    let trace_rows = client
        .query(
            "SELECT entity_norm, attribute_norm, entry_id FROM memory_traces \
             WHERE entry_id = ANY($1) ORDER BY entity_norm, attribute_norm, entry_id",
            &[&ids],
        )
        .await
        .map_err(internal)?;
    let mut source_for_slot: HashMap<(String, String), i64> = HashMap::new();
    for r in &trace_rows {
        source_for_slot.insert((r.get(0), r.get(1)), r.get(2));
    }
    let matched: HashSet<i64> = matches.iter().map(|d| d.id).collect();
    let (surviving_roots, digests): (BTreeSet<String>, Vec<i64>) = {
        let bank = ready.bank.read().expect("bank lock");
        let surviving = bank
            .entries
            .iter()
            .filter(|e| !matched.contains(&e.id) && e.source != "digest")
            .filter_map(|e| root_of(e.episode_id.as_deref()))
            .collect();
        let digests = bank
            .entries
            .iter()
            .filter(|e| {
                e.source == "digest"
                    && e.episode_id.as_ref().is_some_and(|ep| roots.contains(ep))
                    && e.superseded_at.is_none()
                    && (!matched.contains(&e.id) || retained.contains(&e.id))
            })
            .map(|e| e.id)
            .collect();
        (surviving, digests)
    };
    let raw_cursor: Option<Value> = client
        .query_opt(
            "SELECT value FROM meta WHERE key = $1",
            &[&DIGEST_CURSOR_KEY],
        )
        .await
        .map_err(internal)?
        .map(|r| r.get(0));
    let regenerate: BTreeSet<String> = roots.intersection(&surviving_roots).cloned().collect();
    let cursor = digest_cursor(raw_cursor, &regenerate).map_err(ToolError::Invalid)?;
    let now = episodes::now();
    let changed = forget_entry_ids(client, &ids, &digests, &cursor, now)
        .await
        .map_err(|e| match e {
            Fail::Db(e) => internal(e),
            Fail::Runtime(m) => internal(m),
        })?;

    // Publication of what committed: the resident cortex, the digests, and
    // the lessons the cascade retired.
    if !crate::mutants::active("mcp-forget-cortex-stale") {
        ready.cortex.write(|c| {
            for key in &changed.slots {
                let source = source_for_slot.get(key).copied().unwrap_or_default();
                c.retire_unsupported(&key.0, &key.1, source, now);
            }
        });
    }
    {
        let by_text = format!("forgotten source entry {}", ids[0]);
        let wanted: HashSet<i64> = digests.iter().copied().collect();
        let mut bank = ready.bank.write().expect("bank lock");
        for e in bank.entries.iter_mut().filter(|e| wanted.contains(&e.id)) {
            e.superseded_at = Some(now);
            e.superseded_by_text = Some(by_text.clone());
        }
    }
    if changed.lessons_retired {
        refresh_source_retired_lessons(ready).await?;
    }
    Ok(retained)
}

/// `PostgresStorage.forget_entry_ids` (`storage/postgres.py:1225-1314`) with
/// `retire_lessons_for_entries` (`:1332-1389`), one transaction.
async fn forget_entry_ids(
    client: &tokio_postgres::Client,
    ids: &[i64],
    digest_ids: &[i64],
    cursor: &Value,
    now: f64,
) -> Result<Forgotten, Fail> {
    let ids: Vec<i64> = ids
        .iter()
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let digest_ids: Vec<i64> = digest_ids
        .iter()
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    crate::txn::run(client, |c| {
        let (ids, digest_ids) = (&ids, &digest_ids);
        async move {
            let lock: Vec<i64> = ids
                .iter()
                .chain(digest_ids.iter())
                .copied()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect();
            let locked = c
                .query(
                    "SELECT id, source, superseded_at FROM entries WHERE id = ANY($1) \
                     ORDER BY id FOR UPDATE",
                    &[&lock],
                )
                .await?;
            let retiring_digests: BTreeSet<i64> = locked
                .iter()
                .filter(|r| {
                    digest_ids.contains(&r.get::<_, i64>(0))
                        && r.get::<_, String>(1) == "digest"
                        && r.get::<_, Option<f64>>(2).is_none()
                })
                .map(|r| r.get(0))
                .collect();
            let lessons_retired = if crate::mutants::active("mcp-forget-no-lesson-cascade") {
                false
            } else {
                retire_lessons_for_entries(c, ids, now).await?
            };
            let slots: Vec<(String, String)> = c
                .query(
                    "SELECT DISTINCT t.entity_norm, t.attribute_norm \
                     FROM memory_traces t WHERE t.entry_id = ANY($1) \
                     AND NOT EXISTS ( \
                      SELECT 1 FROM memory_traces s \
                      JOIN entries e ON e.id = s.entry_id \
                      WHERE s.entity_norm = t.entity_norm \
                      AND s.attribute_norm = t.attribute_norm \
                      AND NOT (s.entry_id = ANY($1)) \
                      AND e.superseded_at IS NULL) \
                     ORDER BY t.entity_norm, t.attribute_norm",
                    &[ids],
                )
                .await?
                .iter()
                .map(|r| (r.get(0), r.get(1)))
                .collect();
            let edge_ids: Vec<i64> = c
                .query(
                    "SELECT DISTINCT ev.edge_id FROM edge_evidence ev \
                     JOIN edges g ON g.id = ev.edge_id \
                     WHERE ev.entry_id = ANY($1) AND g.superseded_at IS NULL \
                     AND g.origin = 'agent' AND NOT EXISTS ( \
                      SELECT 1 FROM edge_evidence s \
                      JOIN entries e ON e.id = s.entry_id \
                      WHERE s.edge_id = ev.edge_id \
                      AND NOT (s.entry_id = ANY($1)) \
                      AND e.superseded_at IS NULL)",
                    &[ids],
                )
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect();
            for (entity, attribute) in &slots {
                let source: i64 = c
                    .query_one(
                        "SELECT entry_id FROM memory_traces WHERE entity_norm = $1 \
                         AND attribute_norm = $2 AND entry_id = ANY($3) \
                         ORDER BY entry_id LIMIT 1",
                        &[entity, attribute, ids],
                    )
                    .await?
                    .get(0);
                if !crate::mutants::active("mcp-forget-facts-kept") {
                    c.execute(
                        "UPDATE facts SET status = 'retired', superseded_at = $1, \
                         superseded_by_value = $2 WHERE entity_norm = $3 \
                         AND attribute_norm = $4 AND status = 'current'",
                        &[
                            &now,
                            &format!("forgotten source entry {source}"),
                            entity,
                            attribute,
                        ],
                    )
                    .await?;
                }
            }
            if !edge_ids.is_empty() && !crate::mutants::active("mcp-forget-edges-kept") {
                c.execute(
                    "UPDATE edges SET superseded_at = $1 WHERE id = ANY($2)",
                    &[&now, &edge_ids],
                )
                .await?;
            }
            if !digest_ids.is_empty() {
                let retired: BTreeSet<i64> = c
                    .query(
                        "UPDATE entries SET superseded_at = $1, superseded_by_text = $2 \
                         WHERE id = ANY($3) AND source = 'digest' \
                         AND superseded_at IS NULL RETURNING id",
                        &[
                            &now,
                            &format!("forgotten source entry {}", ids[0]),
                            digest_ids,
                        ],
                    )
                    .await?
                    .iter()
                    .map(|r| r.get(0))
                    .collect();
                if retired != retiring_digests {
                    return Err(Fail::Runtime("forget digest retirement was incomplete"));
                }
            }
            c.execute(
                "INSERT INTO meta (key, value) VALUES ($1, $2) \
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                &[&DIGEST_CURSOR_KEY, cursor],
            )
            .await?;
            let deleted = c
                .execute("DELETE FROM entries WHERE id = ANY($1)", &[ids])
                .await?;
            if deleted != ids.len() as u64 {
                return Err(Fail::Runtime("forget entry delete was incomplete"));
            }
            Ok(Forgotten {
                slots,
                lessons_retired,
            })
        }
    })
    .await
}

/// `retire_lessons_for_entries`: lessons whose last trusted lineage source
/// this forget removes retire, each with a `source_cascade` audit row.
/// Returns whether any retired.
async fn retire_lessons_for_entries(
    c: &tokio_postgres::Client,
    ids: &[i64],
    now: f64,
) -> Result<bool, tokio_postgres::Error> {
    let forgotten: BTreeSet<i64> = ids.iter().copied().collect();
    let tokens: Vec<String> = forgotten.iter().map(|i| format!("entry:{i}")).collect();
    if tokens.is_empty() {
        return Ok(false);
    }
    let rows = c
        .query(
            "SELECT id, entity, attribute, entity_norm, attribute_norm, value, about, \
             polarity, outcome, confidence::text, origin, provenance, support, asserted_at, \
             last_confirmed, supersedes_value FROM lessons WHERE status = 'current' \
             AND provenance ?| $1 ORDER BY id FOR UPDATE",
            &[&tokens],
        )
        .await?;
    let mut retiring: Vec<i64> = Vec::new();
    for row in &rows {
        let (entity_norm, attribute_norm): (String, String) = (row.get(3), row.get(4));
        let value: String = row.get(5);
        let asserted_at: f64 = row.get(13);
        let mut sources: BTreeSet<i64> = BTreeSet::new();
        for audit in c
            .query(
                "SELECT record FROM store_decisions WHERE store = 'lesson' \
                 AND entity_norm = $1 AND attribute_norm = $2 \
                 AND action = 'lineage' AND decided_by = 'lesson_synthesis' \
                 AND reason = 'verified_dependencies'",
                &[&entity_norm, &attribute_norm],
            )
            .await?
        {
            let Some(Value::Object(a)) = audit.get::<_, Option<Value>>(0) else {
                continue;
            };
            let same_lesson = a.get("lesson").and_then(Value::as_str) == Some(value.as_str());
            let same_time = a
                .get("asserted_at")
                .filter(|v| !v.is_boolean())
                .and_then(crate::mcp::binding::float_arg)
                == Some(asserted_at);
            if let (true, true, Some(Value::Array(list))) =
                (same_lesson, same_time, a.get("source_entry_ids"))
            {
                // `type(i) is int`: JSON integers only, never booleans or floats.
                sources.extend(list.iter().filter_map(|i| {
                    let n = i.as_number()?;
                    let t = n.to_string();
                    (!t.contains(['.', 'e', 'E'])).then(|| i.as_i64()).flatten()
                }));
            }
        }
        if sources.is_disjoint(&forgotten) {
            continue;
        }
        let remaining: Vec<i64> = sources.difference(&forgotten).copied().collect();
        if !remaining.is_empty()
            && c.query_opt(
                "SELECT 1 FROM entries WHERE id = ANY($1) LIMIT 1",
                &[&remaining],
            )
            .await?
            .is_some()
        {
            continue; // another source still supports the lesson
        }
        let id: i64 = row.get(0);
        retiring.push(id);
        let confidence: f64 = row.get::<_, String>(9).parse().unwrap_or(0.0);
        let snapshot = json!({
            "about": row.get::<_, Option<String>>(6),
            "polarity": row.get::<_, String>(7),
            "outcome": row.get::<_, String>(8),
            "confidence": confidence,
            "origin": row.get::<_, Option<String>>(10),
            "provenance": row.get::<_, Option<Value>>(11),
            "support": row.get::<_, Option<Value>>(12),
            "asserted_at": asserted_at,
            "last_confirmed": row.get::<_, f64>(14),
            "supersedes_value": row.get::<_, Option<String>>(15),
            "task": row.get::<_, String>(1),
            "aspect": row.get::<_, String>(2),
            "lesson": value,
            "status": "retired",
            "superseded_at": now,
            "superseded_by_value": SOURCE_FORGOTTEN,
        });
        record_store_decision(
            c,
            "lesson",
            (&entity_norm, &attribute_norm),
            "retire",
            Some("source_cascade"),
            Some(SOURCE_FORGOTTEN),
            &snapshot,
            now,
        )
        .await?;
    }
    if !retiring.is_empty() {
        c.execute(
            "UPDATE lessons SET status = 'retired', superseded_at = $1, \
             superseded_by_value = $2 WHERE id = ANY($3)",
            &[&now, &SOURCE_FORGOTTEN, &retiring],
        )
        .await?;
    }
    Ok(!retiring.is_empty())
}

/// `record_store_decision` (`storage/postgres.py:3527-3543`): the FK-free
/// audit row, on the transaction the caller holds.
#[allow(clippy::too_many_arguments)]
async fn record_store_decision(
    c: &tokio_postgres::Client,
    store: &str,
    key: (&str, &str),
    action: &str,
    decided_by: Option<&str>,
    reason: Option<&str>,
    record: &Value,
    now: f64,
) -> Result<(), tokio_postgres::Error> {
    c.execute(
        "INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, \
         decided_by, reason, record, decided_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        &[
            &store,
            &key.0,
            &key.1,
            &action,
            &decided_by,
            &reason,
            record,
            &now,
        ],
    )
    .await
    .map(|_| ())
}

/// `_refresh_source_retired_lessons_locked`: mirror the cascade's committed
/// lesson retirements (`superseded_by_value = 'source_forgotten'`).
async fn refresh_source_retired_lessons(ready: &Ready) -> Result<(), ToolError> {
    let rows = ready
        .storage
        .client()
        .query(
            "SELECT entity_norm, attribute_norm, value, asserted_at, superseded_at \
             FROM lessons WHERE status = 'retired' AND superseded_by_value = $1 ORDER BY id",
            &[&SOURCE_FORGOTTEN],
        )
        .await
        .map_err(internal)?;
    let retired: Vec<(String, String, String, f64, Option<f64>)> = rows
        .iter()
        .map(|r| (r.get(0), r.get(1), r.get(2), r.get(3), r.get(4)))
        .collect();
    ready.lessons.write(|s| s.mirror_source_retired(&retired));
    Ok(())
}

// ── scopes fact, world, lesson ───────────────────────────────────────────

/// `service.cortex_forget` (`service.py:6161-6183`): a hard delete, the
/// per-slot save, then the slot-read counters (a failure there is counted,
/// never the call's).
async fn cortex_forget(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    entity: &str,
    attribute: Option<&str>,
) -> Result<Value, ToolError> {
    let _ws = ready.write.lock().await;
    let removed = ready.cortex.write(|c| c.forget(entity, attribute));
    if removed > 0 {
        crate::fact_set::save_cortex(svc, ready).await?;
        if !crate::mutants::active("mcp-forget-slot-reads-kept") {
            let ne = norm_key(entity);
            let res =
                match attribute {
                    None => {
                        ready
                            .storage
                            .client()
                            .execute("DELETE FROM slot_reads WHERE entity_norm = $1", &[&ne])
                            .await
                    }
                    Some(a) => ready
                        .storage
                        .client()
                        .execute(
                            "DELETE FROM slot_reads WHERE entity_norm = $1 AND attribute_norm = $2",
                            &[&ne, &norm_key(a)],
                        )
                        .await,
                };
            if let Err(e) = res {
                crate::read::stats::record_slot_read_error();
                eprintln!("slot-read cleanup failed: {e}");
            }
        }
    }
    Ok(json!({"removed": removed, "entity": entity, "attribute": attribute}))
}

/// `service.world_forget` (`service.py:4876-4897`, `decided_by="agent"`):
/// the current records retire, one audit row each (its own transaction),
/// then the per-slot save.
async fn world_forget(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    entity: &str,
    attribute: Option<&str>,
) -> Result<Value, ToolError> {
    let _ws = ready.write.lock().await;
    let now = episodes::now();
    let snaps: Vec<(String, String, Value)> = ready.world.write(|w| {
        let retired = w.retire(entity, attribute, now);
        retired
            .iter()
            .map(|&i| {
                let r = &w.records[i];
                (r.key.0.clone(), r.key.1.clone(), r.audit_snapshot())
            })
            .collect()
    });
    if !snaps.is_empty() {
        for (en, an, snap) in &snaps {
            if crate::mutants::active("mcp-forget-no-world-audit") {
                break;
            }
            write_store_decision(ready, "world", (en, an), snap, now).await?;
        }
        crate::world_tools::save_world(svc, ready).await?;
    }
    Ok(json!({
        "removed": snaps.len(), "retired": snaps.len(),
        "entity": entity, "attribute": attribute,
    }))
}

/// `service.lesson_forget` (`service.py:5449-5474`, `decided_by="agent"`).
async fn lesson_forget(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    task: &str,
    aspect: Option<&str>,
) -> Result<Value, ToolError> {
    let _ws = ready.write.lock().await;
    let now = episodes::now();
    let snaps: Vec<(String, String, Value)> = ready.lessons.write(|l| {
        let retired = l.retire(task, aspect, now);
        retired
            .iter()
            .map(|&i| {
                let r = &l.records[i];
                (r.key.0.clone(), r.key.1.clone(), r.audit_snapshot())
            })
            .collect()
    });
    if !snaps.is_empty() {
        for (en, an, snap) in &snaps {
            write_store_decision(ready, "lesson", (en, an), snap, now).await?;
        }
        save_lessons(svc, ready).await?;
    }
    Ok(json!({
        "removed": snaps.len(), "retired": snaps.len(),
        "task": task, "aspect": aspect,
    }))
}

/// `_record_store_decision` (`service.py:5539-5551`): `decided_by="agent"`,
/// no reason, its own transaction; a failure is an internal error.
async fn write_store_decision(
    ready: &Ready,
    store: &str,
    key: (&str, &str),
    snapshot: &Value,
    now: f64,
) -> Result<(), ToolError> {
    crate::txn::run(ready.storage.client(), |c| async move {
        record_store_decision(c, store, key, "retire", Some("agent"), None, snapshot, now).await
    })
    .await
    .map_err(internal)
}

/// `_save_lessons` (`service.py:3774-3785`): the per-slot sync; a failure is
/// counted and raised.
async fn save_lessons(svc: &Service, ready: &Ready) -> Result<(), ToolError> {
    let snap = ready.lessons.snapshot();
    let synced = crate::stores::lessons::sync_slot_rows(ready.storage.client(), &snap).await;
    drop(snap);
    let slots = synced.map_err(|e| {
        svc.persist_errors
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        eprintln!("Lesson store save failed (NOT durably persisted): {e:#}");
        internal(format!("lesson store save failed: {e:#}"))
    })?;
    ready.lessons.write(|s| {
        for key in &slots {
            s.dirty_slots.remove(key);
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_cursor_matches_load_and_union() {
        let regen: BTreeSet<String> = ["b".to_string()].into();
        let fresh = digest_cursor(None, &regen).unwrap();
        assert_eq!(fresh, json!({"ts": 0.0, "retry": {}, "regenerate": ["b"]}));
        let raw = json!({"ts": 5, "retry": {"x": 2}, "regenerate": ["c", "a"], "other": 1});
        let cur = digest_cursor(Some(raw), &regen).unwrap();
        assert_eq!(
            cur,
            json!({"ts": 5.0, "retry": {"x": 2}, "regenerate": ["a", "b", "c"]})
        );
        let raw = json!({"context_deferred": ["z"]});
        let cur = digest_cursor(Some(raw), &BTreeSet::new()).unwrap();
        assert_eq!(cur["context_deferred"], json!(["z"]));
        assert_eq!(cur["regenerate"], json!([]));
    }
}
