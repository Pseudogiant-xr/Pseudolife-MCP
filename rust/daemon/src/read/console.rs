//! The Console's read projections (`web/routes.py`): each GET wraps one
//! `MemoryService` read, with the route's own query coercions (`_s`, `_i`,
//! `_f`, `_list`) and `ConsoleRoutes._limited`'s row cap.
//!
//! Served here: `/api/facts`, `/api/facts/history`, `/api/world`,
//! `/api/lessons`, `/api/sources`, `/api/recent`, `/api/chain`,
//! `/api/entry`, `/api/episodes`, `/api/episodes/summary`,
//! `/api/consolidation`, the dashboard summary `/api/overview` and, through
//! the board family, `/api/agents`' default view. Each body is the service
//! function the matching MCP tool also calls, without the MCP layer's
//! reshaping (compaction, the faded note, `replaced_by`). A handler's
//! exception is the 500 `{"error": str(exc)}` that `web/api.py` answers for
//! any non-`ValueError`.

use std::collections::HashMap;
use std::sync::Arc;

use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};

use crate::http::{App, json_response};
use crate::read::cortex_search::{
    CortexKnobs, annotate_evidence_supersession, contender_fields, record_to_dict_age,
    traces_for_slot,
};
use crate::read::search_route::{list, py_float, py_int};
use crate::service::{Ready, Service};
use crate::stores::cortex::{CortexRecord, SlotKey, norm_key};

/// The GET registrations this module answers.
pub(crate) const PATHS: &[&str] = &[
    "/api/facts",
    "/api/facts/history",
    "/api/world",
    "/api/lessons",
    "/api/sources",
    "/api/recent",
    "/api/chain",
    "/api/entry",
    "/api/episodes",
    "/api/episodes/summary",
    "/api/consolidation",
    "/api/overview",
];

/// `_s(params, key)`: the value unless absent or empty.
fn s_param(q: &HashMap<String, String>, key: &str) -> Option<String> {
    q.get(key).filter(|v| !v.is_empty()).cloned()
}

/// `_i(params, key, default)`: `int(v)`, else the default. Python's
/// integers are unbounded; one past i64 saturates, which every bound here
/// reads the same way (all rows, or none).
pub(crate) fn i_param(q: &HashMap<String, String>, key: &str, default: i64) -> i64 {
    q.get(key)
        .filter(|v| !v.is_empty())
        .and_then(|v| py_int(v).or_else(|| saturated_int(v)))
        .unwrap_or(default)
}

/// An ASCII integer literal `py_int` cannot hold: `i64::MAX` or `i64::MIN`
/// by its sign. Underscores follow Python's rule (single, between digits).
fn saturated_int(v: &str) -> Option<i64> {
    let t = crate::read::search_route::py_strip(v);
    let (negative, digits) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let well_formed = !digits.is_empty()
        && digits
            .split('_')
            .all(|g| !g.is_empty() && g.bytes().all(|b| b.is_ascii_digit()));
    well_formed.then_some(if negative { i64::MIN } else { i64::MAX })
}

/// `_f(params, key, default)`: `float(v)`, else the default.
fn f_param(q: &HashMap<String, String>, key: &str, default: f64) -> f64 {
    q.get(key)
        .filter(|v| !v.is_empty())
        .and_then(|v| py_float(v))
        .unwrap_or(default)
}

/// `max(0, int(n))` as a slice bound.
fn non_negative(n: i64) -> usize {
    usize::try_from(n.max(0)).unwrap_or(usize::MAX)
}

/// `ConsoleRoutes._limited`: the first `limit` rows, and how many exist.
fn limited(rows: Vec<Value>, limit: i64) -> Value {
    let total = rows.len();
    let entries: Vec<Value> = rows.into_iter().take(non_negative(limit)).collect();
    json!({"count": entries.len(), "total": total,
           "truncated": entries.len() < total, "entries": entries})
}

/// `_demote_stale`: under "demote", stale rows last, the order otherwise kept.
fn demote_stale(rows: &mut [Map<String, Value>], policy: &str) {
    if policy == "demote" {
        rows.sort_by_key(|d| d.get("stale").is_some_and(crate::board::store::py_truthy));
    }
}

/// `str.lower()` of a row's text field, the dumps' sort key.
fn lower(d: &Map<String, Value>, key: &str) -> String {
    d.get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase()
}

/// The `X-PL-Session` header and, under the legacy transport, the MCP
/// session id: what `_resolve_writer` reads for a REST call.
pub(crate) fn caller_session(h: &HeaderMap) -> (Option<String>, Option<String>) {
    let last = |name: &str| {
        h.get_all(name)
            .iter()
            .next_back()
            .map(|v| v.as_bytes().iter().map(|&b| b as char).collect::<String>())
    };
    let header = last("x-pl-session");
    let transport = crate::mcp::identity::legacy_transport_session()
        .then(|| last("mcp-session-id"))
        .flatten();
    (header, transport)
}

fn band_order(svc: &Service) -> Vec<String> {
    svc.config
        .memory
        .band_specs
        .iter()
        .map(|b| b.name.clone())
        .collect()
}

pub(crate) async fn route(
    app: &App,
    path: &str,
    q: &HashMap<String, String>,
    h: &HeaderMap,
) -> Response {
    let ready = match app.service.ensure_init().await {
        Ok(r) => r,
        // `_overview` reaches the bank only inside each `_safe` layer, so a
        // failed init serves every layer's default, not a 500.
        Err(_) if path == "/api/overview" => {
            return json_response(200, &overview_defaults(&app.service));
        }
        Err(e) => return json_response(500, &json!({"error": e})),
    };
    // The body runs to completion in its own task: Python's handler runs on
    // a worker thread a disconnecting client cannot stop, and `/api/entry`
    // writes.
    let (svc, path, q, session) = (
        app.service.clone(),
        path.to_string(),
        q.clone(),
        caller_session(h),
    );
    let task = tokio::spawn(async move { answer(&svc, &ready, &path, &q, session).await });
    match task.await {
        Ok(Ok(v)) => json_response(200, &v),
        Ok(Err(e)) => json_response(500, &json!({"error": e})),
        Err(e) => json_response(500, &json!({"error": e.to_string()})),
    }
}

async fn answer(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    path: &str,
    q: &HashMap<String, String>,
    session: (Option<String>, Option<String>),
) -> Result<Value, String> {
    let err = |e: anyhow::Error| format!("{e:#}");
    match path {
        "/api/facts" => Ok(limited(
            facts_dump(svc, ready).await.map_err(err)?,
            i_param(q, "limit", 500),
        )),
        "/api/facts/history" => Ok(history(
            svc,
            ready,
            s_param(q, "entity"),
            s_param(q, "attribute"),
        )),
        "/api/world" => Ok(limited(world_dump(svc, ready), i_param(q, "limit", 500))),
        "/api/lessons" => Ok(limited(
            lessons_dump(ready, LESSONS_DUMP_LIMIT).await?,
            i_param(q, "limit", 500),
        )),
        "/api/sources" => Ok(list_sources(ready)),
        "/api/recent" => recent(svc, ready, q).await,
        "/api/chain" => {
            let entity = s_param(q, "entity").unwrap_or_default();
            let mut limit = i_param(q, "limit", 20);
            if crate::mutants::active("console-chain-limit-clamped") {
                limit = limit.max(0);
            }
            crate::read::history::chain(ready, &entity, limit)
                .await
                .map_err(db_error)
        }
        "/api/entry" => crate::read::mcp_reads::get_entry(svc, ready, i_param(q, "id", 0), session)
            .await
            .map_err(db_error),
        "/api/episodes" => Ok(episode_list(ready, i_param(q, "limit", 100)).await),
        "/api/episodes/summary" => episode_summary(svc, ready, s_param(q, "id")).await,
        "/api/consolidation" => consolidation(svc, ready, q).await,
        "/api/overview" => Ok(overview(svc, ready).await),
        _ => Err(format!("unrouted console read {path}")),
    }
}

/// `str(exc)` for a service exception a tool body reports as a `ToolError`.
pub(crate) fn tool_error_text(e: crate::mcp::dispatch::ToolError) -> String {
    use crate::mcp::dispatch::ToolError;
    match e {
        ToolError::Refused { code, detail, .. } => match detail {
            Some(d) => format!("{code}: {d}"),
            None => code,
        },
        ToolError::Invalid(s) | ToolError::FileNotFound(s) => s,
        ToolError::Internal(e) => db_error(e),
    }
}

/// `str(exc)` for an exception out of the bank: psycopg's text is the
/// server's primary message.
fn db_error(e: anyhow::Error) -> String {
    for cause in e.chain() {
        if let Some(db) = cause
            .downcast_ref::<tokio_postgres::Error>()
            .and_then(tokio_postgres::Error::as_db_error)
        {
            return db.message().to_string();
        }
    }
    format!("{e:#}")
}

/// `ConsoleRoutes` asks `lessons_dump` for this many rows before `_limited`,
/// and `_overview` as many lessons and episodes.
const LESSONS_DUMP_LIMIT: usize = 100_000;

/// `GET /api/agents` outside `view=coordination`:
/// `coordination_awareness(limit=_i(limit, 5))` for the caller's bearer and
/// `X-PL-Session`. A limit below 1 is the service's `ValueError`, a 400.
pub(crate) async fn agents(app: &App, q: &HashMap<String, String>, h: &HeaderMap) -> Response {
    let limit = i_param(q, "limit", 5);
    let (session, _) = caller_session(h);
    match crate::board::mcp::awareness(app, h, session.as_deref(), Some(limit)).await {
        Ok(v) => json_response(200, &v),
        Err(e) => json_response(400, &json!({"error": e})),
    }
}

/// `ConsoleRoutes._overview` (`web/routes.py:401-461`): one round trip for
/// the dashboard, each layer read as `_safe` reads it (a failing layer
/// serves its empty default, never a 500).
async fn overview(svc: &Arc<Service>, ready: &Ready) -> Value {
    let stats = crate::read::stats::stats(svc)
        .await
        .map(Value::Object)
        .unwrap_or_else(|_| json!({}));
    let facts = facts_dump(svc, ready).await.unwrap_or_default();
    let world = world_dump(svc, ready);
    let lessons = lessons_dump(ready, LESSONS_DUMP_LIMIT)
        .await
        .unwrap_or_default();
    let episodes = episode_list(ready, LESSONS_DUMP_LIMIT as i64).await;
    let sources = list_sources(ready);
    let tags = list_tags(ready).unwrap_or_else(|| json!({"tags": [], "total": 0}));
    let dream = crate::dream::service::status(ready, &svc.config)
        .await
        .unwrap_or_else(|_| json!({}));
    let loop_health = loop_health(svc, ready, LOOP_WINDOW_DAYS)
        .await
        .unwrap_or_else(|_| json!({"available": false}));

    let truthy = |v: Option<&Value>| v.is_some_and(crate::board::store::py_truthy);
    // `stats.get("total_memories", 0) or 0`, else the band sizes' sum.
    let mut total_entries = stats
        .get("total_memories")
        .filter(|v| truthy(Some(v)))
        .cloned()
        .unwrap_or(json!(0));
    if !truthy(Some(&total_entries)) {
        let sum: i64 = stats
            .get("bands")
            .and_then(Value::as_array)
            .map(|bands| {
                bands
                    .iter()
                    .filter_map(|b| b.get("size").and_then(Value::as_i64))
                    .sum()
            })
            .unwrap_or(0);
        total_entries = json!(sum);
    }
    // Contested slots, not rows: a set slot's member rows share its flag.
    let contested: std::collections::HashSet<(String, String)> = facts
        .iter()
        .filter(|f| truthy(f.get("contested")))
        .map(|f| {
            let text = |k: &str| f.get(k).and_then(Value::as_str).unwrap_or("");
            (norm_key(text("entity")), norm_key(text("attribute")))
        })
        .collect();
    let mut by_origin = Map::new();
    for f in &facts {
        // `f.get("origin") or "agent"`, counted in first-seen order.
        let origin = f
            .get("origin")
            .and_then(Value::as_str)
            .filter(|o| !o.is_empty())
            .unwrap_or("agent");
        let n = by_origin.get(origin).and_then(Value::as_i64).unwrap_or(0);
        by_origin.insert(origin.to_string(), json!(n + 1));
    }
    let world_stale = world.iter().filter(|w| truthy(w.get("stale"))).count();
    let count_of = |v: &Value, key: &str| v.get(key).and_then(Value::as_array).map_or(0, Vec::len);
    let health = overview_health(svc);
    json!({
        "health": health,
        "counts": {
            "entries": total_entries,
            "facts": facts.len(),
            "facts_contested": if crate::mutants::active("console-overview-contested-rows") {
                facts.iter().filter(|f| truthy(f.get("contested"))).count()
            } else {
                contested.len()
            },
            "facts_by_origin": by_origin,
            "world": world.len(),
            "world_stale": world_stale,
            "lessons": lessons.len(),
            "episodes": count_of(&episodes, "episodes"),
            "sources": count_of(&sources, "sources"),
            "tags": count_of(&tags, "tags"),
        },
        "stats": stats,
        "dream": dream,
        "loop": loop_health,
    })
}

/// `ConsoleRoutes._health`: the overview's health block (no bank read).
fn overview_health(svc: &Service) -> Value {
    json!({
        "status": "ok",
        "schema": crate::health::SCHEMA,
        "storage": "postgres",
        "writer_id": crate::mcp::identity::env_writer().unwrap_or_else(|| "unknown".into()),
        "persist_errors": svc.persist_errors.load(std::sync::atomic::Ordering::Relaxed),
    })
}

/// `_overview` when every layer's `_safe` read raised (the bank never
/// initialized): the health block, zero counts and each layer's default.
fn overview_defaults(svc: &Service) -> Value {
    json!({
        "health": overview_health(svc),
        "counts": {
            "entries": 0, "facts": 0, "facts_contested": 0, "facts_by_origin": {},
            "world": 0, "world_stale": 0, "lessons": 0, "episodes": 0,
            "sources": 0, "tags": 0,
        },
        "stats": {},
        "dream": {},
        "loop": {"available": false},
    })
}

/// `service.list_tags` (`service.py:7639-7660`): every resident tag with
/// its occurrence count, by count descending then tag; `total` sums the
/// occurrences. `None` (the overview's `_safe` default) for a non-string
/// tag, which no tool writes (`normalize_tags` keeps strings).
fn list_tags(ready: &Ready) -> Option<Value> {
    let bank = ready.bank.read().expect("bank lock");
    let mut counts: HashMap<&str, usize> = HashMap::new();
    let mut total = 0usize;
    for e in &bank.entries {
        for t in &e.tags {
            *counts.entry(t.as_str()?).or_default() += 1;
            total += 1;
        }
    }
    let mut rows: Vec<(&str, usize)> = counts.into_iter().collect();
    rows.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    let rows: Vec<Value> = rows
        .into_iter()
        .map(|(t, c)| json!({"tag": t, "count": c}))
        .collect();
    Some(json!({"tags": rows, "total": total}))
}

/// `_overview` reads `loop_health()` with its default seven-day window.
const LOOP_WINDOW_DAYS: i64 = 7;
/// `SESSION_PAIR_WINDOW_S` (`storage/postgres.py`): a hook root and its
/// shim root open within this many seconds of each other.
const SESSION_PAIR_WINDOW_S: f64 = 5.0;

/// `_HOOK_SESSION_KEY`: a Claude Code session UUID, lowercase hex.
fn hook_session_key(key: &str) -> bool {
    let groups: Vec<&str> = key.split('-').collect();
    groups.len() == 5
        && groups
            .iter()
            .zip([8, 4, 4, 4, 12])
            .all(|(g, n)| g.len() == n && g.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
}

/// `count_client_sessions` (`storage/postgres.py:362-389`) over keyed
/// roots `(id, session_key, started_at)`: hook roots always count, other
/// roots only when active, and a hook root and an active other root that
/// are each other's only candidate within the pair window count once.
fn count_client_sessions(
    roots: &[(String, String, f64)],
    active: &std::collections::HashSet<String>,
) -> usize {
    let hooks: Vec<&(String, String, f64)> =
        roots.iter().filter(|r| hook_session_key(&r.1)).collect();
    let others: Vec<&(String, String, f64)> = roots
        .iter()
        .filter(|r| !hook_session_key(&r.1) && active.contains(&r.0))
        .collect();
    let near: Vec<Vec<&str>> = hooks
        .iter()
        .map(|h| {
            others
                .iter()
                .filter(|o| (o.2 - h.2).abs() <= SESSION_PAIR_WINDOW_S)
                .map(|o| o.0.as_str())
                .collect()
        })
        .collect();
    let mut claims: HashMap<&str, usize> = HashMap::new();
    for cands in &near {
        for o in cands {
            *claims.entry(o).or_default() += 1;
        }
    }
    let merged = near
        .iter()
        .filter(|c| c.len() == 1 && claims.get(c[0]) == Some(&1))
        .count();
    hooks.len() + others.len() - merged
}

/// `service.loop_health(window_days)` (`service.py:5495-5519`) over
/// `PostgresStorage.loop_health` (`storage/postgres.py:2715-2777`):
/// windowed stores and outcome signals against the window before, client
/// sessions, pending signals split at the synthesis retry window, and
/// lesson recency, with per-session rates.
async fn loop_health(svc: &Service, ready: &Ready, window_days: i64) -> anyhow::Result<Value> {
    let retry_days = svc.config.memory.lessons_signal_retry_days;
    let t = crate::episodes::now();
    let since = (retry_days > 0).then_some(t - retry_days as f64 * 86400.0);
    let window_s = window_days as f64 * 86400.0;
    let (cutoff, prev_cutoff) = (t - window_s, t - 2.0 * window_s);
    let db = ready.storage.client();
    let h = crate::txn::with_client(db, |c| async move {
        let window = |table: &'static str, col: &'static str| {
            format!(
                "SELECT COUNT(*) FILTER (WHERE {col} >= $1),                  COUNT(*) FILTER (WHERE {col} >= $2 AND {col} < $1) FROM {table}"
            )
        };
        let row = c
            .query_one(&window("entries", "ts"), &[&cutoff, &prev_cutoff])
            .await?;
        let stores = json!({"current": row.get::<_, i64>(0), "previous": row.get::<_, i64>(1)});
        let row = c
            .query_one(
                &window("outcome_signals", "created_at"),
                &[&cutoff, &prev_cutoff],
            )
            .await?;
        let mut by_outcome = Map::new();
        for r in c
            .query(
                "SELECT outcome, COUNT(*) FROM outcome_signals                  WHERE created_at >= $1 GROUP BY outcome",
                &[&cutoff],
            )
            .await?
        {
            by_outcome.insert(r.get::<_, String>(0), json!(r.get::<_, i64>(1)));
        }
        let outcomes = json!({"current": row.get::<_, i64>(0), "previous": row.get::<_, i64>(1),
                              "by_outcome": by_outcome});
        let roots: Vec<(String, Option<String>, f64)> = c
            .query(
                "SELECT id, session_key, started_at FROM episodes                  WHERE started_at >= $1 AND parent_id IS NULL ORDER BY started_at, id",
                &[&cutoff],
            )
            .await?
            .iter()
            .map(|r| (r.get(0), r.get(1), r.get(2)))
            .collect();
        let keyed: Vec<(String, String, f64)> = roots
            .iter()
            .filter_map(|(id, key, at)| key.clone().map(|k| (id.clone(), k, *at)))
            .collect();
        let active = active_roots(c, &keyed, cutoff).await?;
        let sessions = count_client_sessions(&keyed, &active);
        let (pending, expired): (i64, i64) = match since {
            None => {
                let r = c
                    .query_one(
                        "SELECT COUNT(*) FROM outcome_signals WHERE consumed_at IS NULL",
                        &[],
                    )
                    .await?;
                (r.get(0), 0)
            }
            Some(since) => {
                let r = c
                    .query_one(
                        "SELECT COUNT(*) FILTER (WHERE created_at >= $1),                          COUNT(*) FILTER (WHERE created_at < $1)                          FROM outcome_signals WHERE consumed_at IS NULL",
                        &[&since],
                    )
                    .await?;
                (r.get(0), r.get(1))
            }
        };
        let r = c
            .query_one(
                "SELECT MAX(asserted_at), COUNT(*) FILTER (WHERE status = 'current') FROM lessons",
                &[],
            )
            .await?;
        let (last_lesson, lessons_current): (Option<f64>, i64) = (r.get(0), r.get(1));
        Ok::<_, anyhow::Error>((
            stores,
            outcomes,
            sessions,
            roots.len(),
            pending,
            expired,
            last_lesson,
            lessons_current,
        ))
    })
    .await?;
    let (stores, outcomes, sessions, root_episodes, pending, expired, last_lesson, lessons_current) =
        h;
    let rate = |n: Option<i64>| -> Value {
        match (n, sessions) {
            (Some(n), s) if s > 0 => json!(crate::read::stats::py_round(n as f64 / s as f64, 2)),
            _ => Value::Null,
        }
    };
    let stores_per_session = rate(stores["current"].as_i64());
    let outcomes_per_session = rate(outcomes["current"].as_i64());
    Ok(json!({
        "available": true,
        "window_days": window_days,
        "stores": stores,
        "outcomes": outcomes,
        "sessions": sessions,
        "root_episodes": root_episodes,
        "pending_signals": pending,
        "pending_signals_expired": expired,
        "last_lesson_at": last_lesson,
        "lessons_current": lessons_current,
        "stores_per_session": stores_per_session,
        "outcomes_per_session": outcomes_per_session,
    }))
}

/// `PostgresStorage._active_roots`: the keyed roots with memory activity
/// since `since` (an entry or outcome signal in the root's subtree, or a
/// search under the root's session key; a search with no session counts
/// by its episode). A later root wins a reused key.
async fn active_roots(
    c: &tokio_postgres::Client,
    roots: &[(String, String, f64)],
    since: f64,
) -> Result<std::collections::HashSet<String>, tokio_postgres::Error> {
    let mut active = std::collections::HashSet::new();
    if roots.is_empty() {
        return Ok(active);
    }
    let ids: Vec<&str> = roots.iter().map(|r| r.0.as_str()).collect();
    let mut root_of: HashMap<String, String> = HashMap::new();
    for r in c
        .query(
            "WITH RECURSIVE tree AS (               SELECT id, id AS root_id FROM episodes WHERE id = ANY($1)                UNION ALL                SELECT e.id, t.root_id FROM episodes e JOIN tree t ON e.parent_id = t.id             ) SELECT id, root_id FROM tree",
            &[&ids],
        )
        .await?
    {
        root_of.insert(r.get(0), r.get(1));
    }
    let root_of_key: HashMap<&str, &str> = roots
        .iter()
        .map(|(id, key, _)| (key.as_str(), id.as_str()))
        .collect();
    for r in c
        .query(
            "SELECT DISTINCT episode_id FROM entries              WHERE ts >= $1 AND episode_id IS NOT NULL              UNION SELECT DISTINCT episode_id FROM outcome_signals              WHERE created_at >= $1 AND episode_id IS NOT NULL",
            &[&since],
        )
        .await?
    {
        if let Some(root) = root_of.get(&r.get::<_, String>(0)) {
            active.insert(root.clone());
        }
    }
    for r in c
        .query(
            "SELECT DISTINCT episode_id, session_id FROM retrieval_events WHERE created_at >= $1",
            &[&since],
        )
        .await?
    {
        let (ep, sid): (Option<String>, Option<String>) = (r.get(0), r.get(1));
        let root = match sid.as_deref().filter(|s| !s.is_empty()) {
            Some(sid) => root_of_key.get(sid).map(|s| s.to_string()),
            None => ep.and_then(|ep| root_of.get(&ep).cloned()),
        };
        if let Some(root) = root {
            active.insert(root);
        }
    }
    Ok(active)
}

/// `service.cortex_dump` (`service.py:5791-5830`): every current fact,
/// with its slot's contender, sorted by `(entity.lower(), attribute.lower())`,
/// stale-demoted, then each row's graph `entity_id`, its slot's
/// `source_entries` and the evidence-supersession flag.
async fn facts_dump(svc: &Service, ready: &Ready) -> anyhow::Result<Vec<Value>> {
    // Python holds the service lock for the whole dump, so no write lands
    // between the snapshot and the per-row trace reads.
    let _ws = ready.write.lock().await;
    let knobs = CortexKnobs::from_config(&svc.config);
    let relative_age = svc.config.time.relative_age;
    let now = crate::episodes::now();
    let mut rows: Vec<(Map<String, Value>, SlotKey)> = {
        let snap = ready.cortex.snapshot();
        let mut parked: HashMap<&SlotKey, Vec<&CortexRecord>> = HashMap::new();
        for c in snap.records.iter().filter(|c| c.status == "contested") {
            parked.entry(&c.key).or_default().push(c);
        }
        snap.current_records()
            .map(|r| {
                let mut d = record_to_dict_age(r, &knobs.stale_policy, now, relative_age);
                let conts = parked.get(&r.key).map_or(&[][..], Vec::as_slice);
                if !crate::mutants::active("console-facts-no-contender") {
                    contender_fields(&mut d, conts);
                } else {
                    contender_fields(&mut d, &[]);
                }
                (d, r.key.clone())
            })
            .collect()
    };
    rows.sort_by_cached_key(|(d, _)| (lower(d, "entity"), lower(d, "attribute")));
    if knobs.stale_policy == "demote" {
        rows.sort_by_key(|(d, _)| d.get("stale") == Some(&Value::Bool(true)));
    }
    let db = ready.storage.client();
    let emap = crate::storage::graph::entity_id_map(db).await?;
    for (d, _) in &mut rows {
        let text = |k: &str| d.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let (entity, attribute) = (text("entity"), text("attribute"));
        let id = emap
            .get(crate::storage::graph::norm_name(&entity))
            .cloned()
            .unwrap_or(Value::Null);
        d.insert("entity_id".into(), id);
        // Keyed by the row's own spelling, as `traces_for_slot(_norm_key(
        // entity), _norm_key(attribute))` is.
        let key = (norm_key(&entity), norm_key(&attribute));
        let traces =
            crate::txn::with_client(db, |c| async move { traces_for_slot(c, &key).await }).await?;
        d.insert("source_entries".into(), json!(traces));
    }
    let mut maps: Vec<Map<String, Value>> = rows.into_iter().map(|(d, _)| d).collect();
    annotate_evidence_supersession(db, &mut maps, &knobs).await?;
    Ok(maps.into_iter().map(Value::Object).collect())
}

/// `service.history(entity, attribute)` with no `as_of`. The route passes
/// `None` for an absent parameter: it keys the empty slot and is echoed
/// as null.
fn history(
    svc: &Service,
    ready: &Ready,
    entity: Option<String>,
    attribute: Option<String>,
) -> Value {
    let snap = ready.cortex.snapshot();
    let mut out = crate::read::history::history(
        &snap,
        entity.as_deref().unwrap_or(""),
        attribute.as_deref().unwrap_or(""),
        None,
        svc.config.time.relative_age,
        crate::episodes::now(),
    );
    if let Value::Object(m) = &mut out {
        m.insert("entity".into(), json!(entity));
        m.insert("attribute".into(), json!(attribute));
    }
    out
}

/// `service.world_dump` (`service.py:4984-4992`): every current world
/// fact, sorted by `(entity.lower(), attribute.lower())`, stale-demoted.
fn world_dump(svc: &Service, ready: &Ready) -> Vec<Value> {
    let policy = &svc.config.memory.search.stale_policy;
    let now = crate::episodes::now();
    let snap = ready.world.snapshot();
    let mut rows: Vec<Map<String, Value>> = snap
        .records
        .iter()
        .filter(|r| r.status == "current")
        .map(|r| crate::world_tools::record_to_dict(r, policy, now))
        .collect();
    rows.sort_by_cached_key(|d| (lower(d, "entity"), lower(d, "attribute")));
    demote_stale(&mut rows, policy);
    rows.into_iter().map(Value::Object).collect()
}

/// `service.lessons_dump(limit)` (`service.py:5485-5493`): current lessons
/// sorted by `(task.lower(), aspect.lower())`, the first `limit`, each
/// annotated for re-verification. `_ensure_init` first mirrors lessons a
/// forget's cascade retired.
async fn lessons_dump(ready: &Ready, limit: usize) -> Result<Vec<Value>, String> {
    let _ws = ready.write.lock().await;
    crate::forget_tools::ensure_lessons_refreshed(ready).await?;
    let snap = ready.lessons.snapshot();
    let mut rows: Vec<Map<String, Value>> = snap
        .current_records()
        .map(crate::lesson_tools::record_to_dict)
        .collect();
    rows.sort_by_cached_key(|d| (lower(d, "task"), lower(d, "aspect")));
    rows.truncate(limit);
    let cortex = ready.cortex.snapshot();
    crate::lesson_tools::annotate_staleness(ready.storage.client(), &cortex, &mut rows)
        .await
        .map_err(|e| format!("{e:#}"))?;
    Ok(rows.into_iter().map(Value::Object).collect())
}

/// `service.list_sources` (`service.py:2324-2345`): resident entries per
/// source, by count descending then source.
fn list_sources(ready: &Ready) -> Value {
    let bank = ready.bank.read().expect("bank lock");
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for e in &bank.entries {
        *counts.entry(e.source.as_str()).or_default() += 1;
    }
    let mut rows: Vec<(&str, usize)> = counts.into_iter().collect();
    if crate::mutants::active("console-sources-by-name") {
        rows.sort_by(|a, b| a.0.cmp(b.0));
    } else {
        rows.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    }
    let rows: Vec<Value> = rows
        .into_iter()
        .map(|(s, c)| json!({"source": s, "count": c}))
        .collect();
    json!({"sources": rows, "total": bank.entries.len()})
}

/// `service.recent(n=_i(n, 50), sources=_list(source), episodes=_list(episode),
/// tags=_list(tag))`.
async fn recent(
    svc: &Service,
    ready: &Ready,
    q: &HashMap<String, String>,
) -> Result<Value, String> {
    let n = non_negative(i_param(q, "n", 50));
    let tags = list(q, "tag").map(|t| {
        crate::store::normalize_tags(Some(&Value::Array(
            t.into_iter().map(Value::String).collect(),
        )))
    });
    let filters = crate::read::mcp_reads::Filters {
        sources: list(q, "source"),
        episodes: list(q, "episode"),
        // `set(_norm(tags)) if tags else None`: an empty set filters nothing.
        tags: tags.filter(|t| !t.is_empty()),
    };
    let bank = ready.bank.clone();
    let order = band_order(svc);
    tokio::task::spawn_blocking(move || {
        let bank = bank.read().expect("bank lock");
        crate::read::mcp_reads::recent(&bank, n, &filters, &order)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// `service.episode_list(limit, include_open=True)` (`service.py:7307-7328`):
/// episodes newest `started_at` first (stable over the log's order), capped
/// unless `limit` is negative, each with its resident entry count.
async fn episode_list(ready: &Ready, limit: i64) -> Value {
    let ws = ready.write.lock().await;
    let counts = crate::routes::episodes::entry_counts(ready);
    let mut eps: Vec<&crate::episodes::Episode> = ws.log.episodes.iter().collect();
    if !crate::mutants::active("console-episodes-unsorted") {
        eps.sort_by(|a, b| b.started_at.total_cmp(&a.started_at));
    }
    if limit >= 0 {
        eps.truncate(usize::try_from(limit).unwrap_or(usize::MAX));
    }
    let rows: Vec<Value> = eps
        .into_iter()
        .map(|ep| {
            let mut row = crate::episode_tools::episode_dict(ep);
            row.insert(
                "entry_count".into(),
                json!(counts.get(&ep.id).copied().unwrap_or(0)),
            );
            Value::Object(row)
        })
        .collect();
    json!({"count": rows.len(), "episodes": rows})
}

/// `service.episode_summary(id)` (`service.py:7351-7406`), uncompacted;
/// `{"found": false, "id": id}` for an unknown or absent id.
async fn episode_summary(
    svc: &Service,
    ready: &Ready,
    id: Option<String>,
) -> Result<Value, String> {
    let order = band_order(svc);
    let ws = ready.write.lock().await;
    let Some(ep) = id.as_deref().and_then(|id| ws.log.get(id)).cloned() else {
        return Ok(json!({"found": false, "id": id}));
    };
    let bank = ready.bank.read().expect("bank lock");
    crate::episode_tools::episode_summary(&bank, &ep, &order, false)
}

/// `service.consolidation_candidates(query=_s(q), episode=_s(episode),
/// sources=_list(source), tags=_list(tag), top_k=_i(top_k, 20),
/// min_cohesion=_f(min_cohesion, 0.6))`.
async fn consolidation(
    svc: &Service,
    ready: &Arc<Ready>,
    q: &HashMap<String, String>,
) -> Result<Value, String> {
    let args = crate::consolidation_tools::CandidateArgs {
        query: s_param(q, "q"),
        episode: s_param(q, "episode"),
        sources: list(q, "source"),
        tags: list(q, "tag"),
        top_k: i_param(q, "top_k", 20),
        min_cohesion: f_param(q, "min_cohesion", 0.6),
        min_cluster_size: 2,
        max_clusters: 10,
    };
    crate::consolidation_tools::candidates(svc, ready, args)
        .await
        .map_err(tool_error_text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn client_sessions_pair_a_hook_root_with_its_only_shim_root() {
        let hook = "0123abcd-0000-4000-8000-00000000abcd";
        let root = |id: &str, key: &str, at: f64| (id.to_string(), key.to_string(), at);
        let active: std::collections::HashSet<String> =
            ["shim-a", "shim-b", "shim-far"].map(String::from).into();
        // A hook root and one active shim root within the window: one session.
        let roots = vec![root("h1", hook, 100.0), root("shim-a", "s-a", 102.0)];
        assert_eq!(count_client_sessions(&roots, &active), 1);
        // Two candidates for one hook: unmerged (an upper bound).
        let roots = vec![
            root("h1", hook, 100.0),
            root("shim-a", "s-a", 101.0),
            root("shim-b", "s-b", 103.0),
        ];
        assert_eq!(count_client_sessions(&roots, &active), 3);
        // An idle shim root does not count; a far one counts on its own.
        let roots = vec![
            root("h1", hook, 100.0),
            root("idle", "s-idle", 101.0),
            root("shim-far", "s-far", 200.0),
        ];
        assert_eq!(count_client_sessions(&roots, &active), 2);
        assert!(hook_session_key(hook));
        assert!(!hook_session_key("0123ABCD-0000-4000-8000-00000000abcd"));
        assert!(!hook_session_key("not-a-uuid"));
    }

    #[tokio::test]
    async fn overview_defaults_follow_safe() {
        let app = crate::http::tests::mcp_refusal_app();
        let v = overview_defaults(&app.service);
        assert_eq!(v["counts"]["facts_contested"], json!(0));
        assert_eq!(v["counts"]["facts_by_origin"], json!({}));
        assert_eq!(v["loop"], json!({"available": false}));
        assert_eq!(v["stats"], json!({}));
        assert_eq!(v["dream"], json!({}));
        assert_eq!(v["health"]["status"], json!("ok"));
        let keys: Vec<&String> = v.as_object().unwrap().keys().collect();
        assert_eq!(keys, ["health", "counts", "stats", "dream", "loop"]);
    }

    #[test]
    fn rates_round_like_python() {
        assert_eq!(crate::read::stats::py_round(2.0 / 3.0, 2), 0.67);
        assert_eq!(crate::read::stats::py_round(0.125, 2), 0.12);
        assert_eq!(crate::read::stats::py_round(1.0, 2), 1.0);
    }

    #[test]
    fn limited_caps_and_reports_the_total() {
        let rows = vec![json!(1), json!(2), json!(3)];
        assert_eq!(
            limited(rows.clone(), 2),
            json!({"count": 2, "total": 3, "truncated": true, "entries": [1, 2]})
        );
        assert_eq!(
            limited(rows.clone(), -4),
            json!({"count": 0, "total": 3, "truncated": true, "entries": []})
        );
        assert_eq!(
            limited(rows, 9),
            json!({"count": 3, "total": 3, "truncated": false, "entries": [1, 2, 3]})
        );
    }

    #[test]
    fn route_coercions_follow_python() {
        let q: HashMap<String, String> = [
            ("a", "12"),
            ("b", ""),
            ("c", "x"),
            ("d", " 7 "),
            ("e", "1.5"),
            ("f", "-3"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        assert_eq!(i_param(&q, "a", 5), 12);
        assert_eq!(i_param(&q, "b", 5), 5);
        assert_eq!(i_param(&q, "c", 5), 5);
        assert_eq!(i_param(&q, "d", 5), 7);
        // `int("1.5")` raises: the default.
        assert_eq!(i_param(&q, "e", 5), 5);
        assert_eq!(i_param(&q, "f", 5), -3);
        assert_eq!(i_param(&q, "missing", 5), 5);
        let big: HashMap<String, String> = [
            ("p", "100000000000000000000"),
            ("n", "-1_000_000_000_000_000_000_000"),
            ("bad", "1__0"),
            ("frac", "1e30"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        assert_eq!(i_param(&big, "p", 5), i64::MAX);
        assert_eq!(i_param(&big, "n", 5), i64::MIN);
        assert_eq!(i_param(&big, "bad", 5), 5);
        assert_eq!(i_param(&big, "frac", 5), 5);
        assert_eq!(f_param(&q, "e", 0.6), 1.5);
        assert_eq!(f_param(&q, "c", 0.6), 0.6);
        assert_eq!(s_param(&q, "b"), None);
        assert_eq!(s_param(&q, "c").as_deref(), Some("x"));
    }
}
