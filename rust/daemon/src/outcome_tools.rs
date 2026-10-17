//! `memory_outcome` and `memory_reinforce` over MCP (spec "Outcome and
//! reinforcement tools over MCP", OC1-OC12). Python: `mcp_server.py`
//! `_parse_used_ids` (1933-2003), `memory_outcome` (2006-2059),
//! `memory_reinforce` (1542-1554); `service.py` `_caller_episode_id`
//! (4946-4959), `record_outcome` (4976-5044), `_record_signal_used_ids`
//! (5046-5055), `_label_used_entries` (5057-5129), `reinforce` (6140-6155),
//! `_record_retrieval_use` (6043-6070); `storage/postgres.py` `add_signal`
//! (2012-2023), `set_signal_used_ids` (2025-2032), `credit_retrieval_uses`
//! (2229-2319), `bump_reinforcements` (4315-4329).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::board::text::{py_space, py_strip};
use crate::episodes::Caller;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::mcp_reads::{FADED_NOTE, credit_retrieval_use};
use crate::service::Ready;

/// `USED_IDS_CAP`.
const USED_IDS_CAP: usize = 50;
const OUTCOMES: [&str; 3] = ["success", "failure", "correction"];
/// `_OUTCOME_NOTES`, for the reason the MCP surface can reach.
const LESSONS_DISABLED_NOTE: &str =
    "Outcome not kept: lessons are disabled in this bank's config. Do not retry.";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("memory_outcome", mcp_memory_outcome),
        ("memory_reinforce", mcp_memory_reinforce),
    ]
}

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

fn text_arg(a: &Map<String, Value>, key: &str) -> Option<String> {
    a.get(key).and_then(Value::as_str).map(str::to_string)
}

fn caller(call: &ToolCall<'_>) -> Caller {
    Caller {
        header_session: call.ident.header_session.clone(),
        transport_session: call.ident.transport_session.clone(),
    }
}

/// A Python int as its canonical decimal text (any size).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct PyInt(String);

impl PyInt {
    /// An integer JSON number's text (`-?\d+`, which is what Python's
    /// `json` decodes to an `int`), canonicalized.
    fn from_number(n: &serde_json::Number) -> Option<PyInt> {
        let t = n.to_string();
        let digits = t.strip_prefix('-').unwrap_or(&t);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        Some(PyInt::canonical(t.starts_with('-'), digits))
    }

    /// `int(token)` for a string: surrounding whitespace, one optional
    /// sign, ASCII digits with single underscores between them. Python
    /// also takes other Unicode decimal digits (declared divergence).
    fn parse(token: &str) -> Option<PyInt> {
        let t = py_strip(token);
        let (negative, body) = match t.as_bytes().first() {
            Some(b'-') => (true, &t[1..]),
            Some(b'+') => (false, &t[1..]),
            _ => (false, t),
        };
        if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
            return None;
        }
        if !body.bytes().all(|b| b.is_ascii_digit() || b == b'_') {
            return None;
        }
        Some(PyInt::canonical(negative, &body.replace('_', "")))
    }

    fn canonical(negative: bool, digits: &str) -> PyInt {
        let trimmed = digits.trim_start_matches('0');
        if trimmed.is_empty() {
            return PyInt("0".into());
        }
        PyInt(if negative {
            format!("-{trimmed}")
        } else {
            trimmed.to_string()
        })
    }

    fn as_i64(&self) -> Option<i64> {
        self.0.parse().ok()
    }

    /// The JSON number Python's `json` writes for it.
    fn to_json(&self) -> Value {
        serde_json::from_str(&self.0).expect("canonical integer text")
    }
}

/// `_parse_used_ids`: `(ids, ignored, truncated)`, ids deduplicated in
/// first-seen order and capped at fifty.
fn parse_used_ids(value: Option<&Value>) -> (Vec<PyInt>, usize, usize) {
    let items: Vec<Value> = match value {
        None | Some(Value::Null) | Some(Value::Bool(_)) => return (vec![], 0, 0),
        Some(Value::Array(items)) => items.clone(),
        Some(Value::Number(n)) => match PyInt::from_number(n) {
            Some(_) => vec![Value::Number(n.clone())],
            None => return (vec![], 1, 0),
        },
        Some(Value::String(s)) => {
            let text = py_strip(s);
            if text.is_empty() {
                return (vec![], 0, 0);
            }
            match serde_json::from_str::<Value>(text) {
                Ok(Value::Array(items)) => items,
                Ok(Value::Number(n)) if PyInt::from_number(&n).is_some() => {
                    vec![Value::Number(n)]
                }
                // Anything else (a float, a string, an object, invalid
                // JSON) is split as text.
                _ => text
                    .trim_matches(|c| "[]() ".contains(c))
                    .replace(',', " ")
                    .split(py_space)
                    .filter(|t| !t.is_empty())
                    .map(|t| Value::String(t.to_string()))
                    .collect(),
            }
        }
        Some(Value::Object(_)) => return (vec![], 1, 0),
    };
    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    let mut ignored = 0;
    for item in &items {
        let n = match item {
            Value::Number(n) => match PyInt::from_number(n) {
                Some(n) => n,
                None => {
                    ignored += 1;
                    continue;
                }
            },
            Value::String(s) => {
                let token = py_strip(s).trim_matches(|c| c == '"' || c == '\'');
                if token.is_empty() {
                    continue;
                }
                match PyInt::parse(token) {
                    Some(n) => n,
                    None => {
                        ignored += 1;
                        continue;
                    }
                }
            }
            _ => {
                ignored += 1;
                continue;
            }
        };
        if seen.insert(n.clone()) {
            ids.push(n);
        }
    }
    let truncated = ids.len().saturating_sub(USED_IDS_CAP);
    ids.truncate(USED_IDS_CAP);
    (ids, ignored, truncated)
}

/// The memory config the outcome path reads.
#[derive(Clone)]
struct LabelConfig {
    lessons: bool,
    retrieval_log: bool,
    window: f64,
}

struct OutcomeArgs {
    task: String,
    outcome: String,
    about: Option<String>,
    detail: Option<String>,
    polarity: Option<String>,
    handle: Option<String>,
    ids: Vec<PyInt>,
}

fn mcp_memory_outcome<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let raw_used = a.get("used_ids").filter(|v| !v.is_null());
        let (ids, ignored, truncated) = parse_used_ids(raw_used);
        let args = OutcomeArgs {
            task: text_arg(a, "task").unwrap_or_default(),
            outcome: text_arg(a, "outcome").unwrap_or_default(),
            about: text_arg(a, "about"),
            detail: text_arg(a, "detail"),
            polarity: text_arg(a, "polarity"),
            handle: text_arg(a, "episode").filter(|h| !h.is_empty()),
            ids: ids.clone(),
        };
        let m = &app.service.config.memory;
        let cfg = LabelConfig {
            lessons: m.lessons_enabled,
            retrieval_log: m.retrieval_log_enabled,
            window: m.retrieval_log_use_window_seconds,
        };
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let (r2, caller) = (ready.clone(), caller(&call));
        let mut out = ready
            .writes
            .spawn(async move { record_outcome(&r2, &caller, &cfg, args).await })
            .await
            .map_err(internal)??;
        // `_noted(..., _OUTCOME_NOTES)`.
        if out.get("reason").and_then(Value::as_str) == Some("lessons disabled") {
            out.entry("note")
                .or_insert_with(|| json!(LESSONS_DISABLED_NOTE));
        }
        if ignored > 0 {
            out.insert("used_ids_ignored".into(), json!(ignored));
        }
        if truncated > 0 {
            out.insert("used_ids_truncated".into(), json!(truncated));
        }
        if raw_used.is_some() && ids.is_empty() {
            // Refuse the label, never the signal.
            let reason = if ignored > 0 {
                "no usable entry ids"
            } else {
                "empty: no label written (used_ids is positive-only)"
            };
            out.insert("used_ids_reason".into(), json!(reason));
        }
        Ok(Value::Object(out))
    })
}

/// `service.record_outcome` (`origin="action"`), under the write lock to
/// the answer: the signal row attributed to the handle's root or else the
/// caller's open leaf, then the `used_ids` label and its record.
async fn record_outcome(
    ready: &Arc<Ready>,
    caller: &Caller,
    cfg: &LabelConfig,
    a: OutcomeArgs,
) -> Result<Map<String, Value>, ToolError> {
    let mut out = Map::new();
    if !OUTCOMES.contains(&a.outcome.as_str()) {
        out.insert("recorded".into(), json!(false));
        out.insert("reason".into(), json!("unknown_outcome"));
        out.insert("outcomes".into(), json!(OUTCOMES));
        return Ok(out);
    }
    let mut ws = ready.write.lock().await;
    if !cfg.lessons {
        out.insert("recorded".into(), json!(false));
        out.insert("reason".into(), json!("lessons disabled"));
        return Ok(out);
    }
    let client = ready.storage.client();
    let resolved = ws.resolve_handle(client, a.handle.as_deref()).await;
    let warning = a.handle.is_some() && resolved.is_none();
    let session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let episode_id = match resolved {
        Some((root, _)) if !crate::mutants::active("mcp-outcome-ignore-handle") => Some(root),
        // `_caller_episode_id`: the session's open leaf; none without one.
        _ => session
            .as_deref()
            .filter(|s| !s.is_empty())
            .and_then(|s| ws.log.open_leaf_for(Some(s)))
            .map(|i| ws.log.episodes[i].id.clone()),
    };
    let now = crate::episodes::now();
    let signal_id: i64 = {
        let (task, outcome, about, detail, polarity, episode_id) = (
            &a.task,
            &a.outcome,
            &a.about,
            &a.detail,
            &a.polarity,
            &episode_id,
        );
        crate::txn::run(client, |c| async move {
            let r = c
                .query_one(
                    "INSERT INTO outcome_signals (task, outcome, about, detail, polarity, \
                     origin, episode_id, created_at) \
                     VALUES ($1, $2, $3, $4, $5, 'action', $6, $7) RETURNING id",
                    &[task, outcome, about, detail, polarity, episode_id, &now],
                )
                .await?;
            Ok::<_, tokio_postgres::Error>(r.get(0))
        })
        .await
        .map_err(|e| internal(format!("signal insert failed: {e}")))?
    };
    out.insert("recorded".into(), json!(true));
    out.insert("signal_id".into(), json!(signal_id));
    out.insert("task".into(), json!(a.task));
    out.insert("outcome".into(), json!(a.outcome));
    if warning {
        out.insert(
            "episode_warning".into(),
            json!("unknown or closed episode handle"),
        );
    }
    if !a.ids.is_empty() {
        let (report, record) = label_used_entries(client, cfg, &a.ids, session.as_deref()).await;
        out.extend(report);
        if let Some(record) = record
            && !crate::mutants::active("mcp-outcome-no-used-ids-record")
        {
            record_signal_used_ids(client, signal_id, &record).await;
        }
    }
    drop(ws);
    Ok(out)
}

/// `_record_signal_used_ids`: a failure is counted and logged, never undoes
/// the recorded outcome.
async fn record_signal_used_ids(client: &tokio_postgres::Client, signal_id: i64, record: &Value) {
    let res = crate::txn::run(client, |c| async move {
        c.execute(
            "UPDATE outcome_signals SET used_ids = $1 WHERE id = $2",
            &[record, &signal_id],
        )
        .await
        .map(|_| ())
    })
    .await;
    if let Err(e) = res {
        crate::read::stats::record_retrieval_log_error();
        eprintln!("used_ids record failed: {e}");
    }
}

fn int_list(ids: &[&PyInt]) -> Value {
    Value::Array(ids.iter().map(|n| n.to_json()).collect())
}

/// `_label_used_entries`: the report keys and the record the signal row
/// keeps (`None` when the log is off).
async fn label_used_entries(
    client: &tokio_postgres::Client,
    cfg: &LabelConfig,
    ids: &[PyInt],
    session: Option<&str>,
) -> (Map<String, Value>, Option<Value>) {
    let mut report = Map::new();
    if !cfg.retrieval_log {
        report.insert("used_ids_recorded".into(), json!(0));
        report.insert("used_ids_reason".into(), json!("retrieval log disabled"));
        return (report, None);
    }
    let hits = match credit_retrieval_uses(client, ids, session, cfg.window).await {
        Ok(hits) => hits,
        Err(e) => {
            crate::read::stats::record_retrieval_log_error();
            eprintln!("retrieval-use labels failed: {e:#}");
            report.insert("used_ids_recorded".into(), json!(0));
            report.insert("used_ids_errors".into(), json!(ids.len()));
            let all: Vec<&PyInt> = ids.iter().collect();
            let record = json!({"unchecked": int_list(&all), "reason": "label write failed"});
            return (report, Some(record));
        }
    };
    let (mut credited, mut unmatched, mut elsewhere) = (vec![], vec![], vec![]);
    for id in ids {
        match id.as_i64().and_then(|n| hits.get(&n)) {
            Some((events, _)) if *events => credited.push(id),
            Some((_, true)) => elsewhere.push(id),
            _ => unmatched.push(id),
        }
    }
    report.insert("used_ids_recorded".into(), json!(credited.len()));
    if !unmatched.is_empty() {
        report.insert("used_ids_unmatched".into(), int_list(&unmatched));
    }
    if !elsewhere.is_empty() {
        report.insert("used_ids_served_elsewhere".into(), int_list(&elsewhere));
    }
    let record = json!({"credited": int_list(&credited), "unmatched": int_list(&unmatched),
                        "served_elsewhere": int_list(&elsewhere)});
    (report, Some(record))
}

/// `credit_retrieval_uses`: one statement for the whole list. Every event
/// in the window that served an id under the session gains a use row
/// (`used_via="outcome"`); without a session only the most recent
/// NULL-session event per id does. Per served id: whether a same-session
/// event served it, and whether another session's did. An id outside
/// BIGINT fails the statement, as psycopg's cast does.
async fn credit_retrieval_uses(
    client: &tokio_postgres::Client,
    ids: &[PyInt],
    session: Option<&str>,
    window: f64,
) -> anyhow::Result<HashMap<i64, (bool, bool)>> {
    let mut sorted: Vec<i64> = ids
        .iter()
        .map(|n| {
            n.as_i64()
                .ok_or_else(|| anyhow::anyhow!("bigint out of range: {}", n.0))
        })
        .collect::<anyhow::Result<_>>()?;
    sorted.sort_unstable();
    sorted.dedup();
    let t = crate::episodes::now();
    let all_events = session.is_some() && !crate::mutants::active("mcp-outcome-credit-latest-only");
    let session = session.map(str::to_string);
    let since = t - window;
    let rows = crate::txn::run(client, |c| {
        let (sorted, session) = (&sorted, &session);
        async move {
            c.query(
                "WITH ids AS (SELECT unnest($1::bigint[]) AS entry_id), \
                 hit AS ( \
                   SELECT i.entry_id, e.id AS event_id, \
                          (e.session_id IS NOT DISTINCT FROM $2) AS same_session, \
                          row_number() OVER ( \
                            PARTITION BY i.entry_id, (e.session_id IS NOT DISTINCT FROM $2) \
                            ORDER BY e.created_at DESC, e.id DESC) AS rn \
                   FROM ids i \
                   JOIN retrieval_events e \
                     ON e.created_at >= $3 \
                    AND e.served @> jsonb_build_array(jsonb_build_object('entry_id', i.entry_id)) \
                 ), \
                 ins AS ( \
                   INSERT INTO retrieval_uses (event_id, entry_id, used_via, created_at) \
                   SELECT event_id, entry_id, 'outcome', $4 FROM hit \
                   WHERE same_session AND ($5 OR rn = 1) \
                   ON CONFLICT DO NOTHING \
                   RETURNING event_id, entry_id \
                 ) \
                 SELECT h.entry_id, \
                        COALESCE(array_agg(h.event_id ORDER BY h.event_id) \
                                 FILTER (WHERE h.same_session AND ($5 OR h.rn = 1)), '{}'), \
                        bool_or(NOT h.same_session) \
                 FROM hit h GROUP BY h.entry_id",
                &[sorted, session, &since, &t, &all_events],
            )
            .await
        }
    })
    .await?;
    Ok(rows
        .iter()
        .map(|r| {
            let events: Vec<i64> = r.get(1);
            let elsewhere: Option<bool> = r.get(2);
            (
                r.get::<_, i64>(0),
                (!events.is_empty(), elsewhere.unwrap_or(false)),
            )
        })
        .collect())
}

fn faded() -> Value {
    json!({"reinforced": false, "faded": true, "note": FADED_NOTE})
}

fn mcp_memory_reinforce<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let ready = app.service.ensure_init().await.map_err(internal)?;
        // An id past BIGINT reaches Python's query as numeric and matches
        // no row: faded, as any unknown id.
        let Some(id) = call.args.get("entry_id").and_then(Value::as_i64) else {
            return Ok(faded());
        };
        let m = &app.service.config.memory;
        let (log, window) = (m.retrieval_log_enabled, m.retrieval_log_use_window_seconds);
        let (header, transport) = (
            call.ident.header_session.clone(),
            call.ident.transport_session.clone(),
        );
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { reinforce(&r2, id, log, window, header, transport).await })
            .await
            .map_err(internal)?
    })
}

/// `service.reinforce`: faded when no row has the id; else both counters
/// in one statement, the resident copy, then the dereference credit
/// (`used_via="reinforce"`, which never fails the call).
async fn reinforce(
    ready: &Arc<Ready>,
    id: i64,
    log: bool,
    window: f64,
    header: Option<String>,
    transport: Option<String>,
) -> Result<Value, ToolError> {
    let _ws = ready.write.lock().await;
    let db = ready.storage.client();
    let explicit = !crate::mutants::active("mcp-reinforce-no-explicit");
    let found = crate::txn::run(db, |c| async move {
        let row = c
            .query_opt("SELECT id FROM entries WHERE id = $1 FOR UPDATE", &[&id])
            .await?;
        if row.is_none() {
            return Ok::<_, tokio_postgres::Error>(false);
        }
        let sql = if explicit {
            "UPDATE entries SET reinforcements = reinforcements + 1, \
             explicit_reinforcements = explicit_reinforcements + 1 WHERE id = $1"
        } else {
            "UPDATE entries SET reinforcements = reinforcements + 1 WHERE id = $1"
        };
        c.execute(sql, &[&id]).await?;
        Ok(true)
    })
    .await
    .map_err(internal)?;
    if !found {
        return Ok(faded());
    }
    {
        let mut bank = ready.bank.write().expect("bank lock");
        if let Some(e) = bank.entries.iter_mut().find(|e| e.id == id) {
            e.reinforcements += 1;
        }
    }
    if log && !crate::mutants::active("mcp-reinforce-no-use-credit") {
        let res = crate::txn::run(db, |c| async move {
            credit_retrieval_use(c, id, header, transport, window, "reinforce").await
        })
        .await;
        if let Err(e) = res {
            crate::read::stats::record_retrieval_log_error();
            eprintln!("retrieval-use label failed: {e:#}");
        }
    }
    Ok(json!({"reinforced": true, "entry_id": id}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(v: Value) -> (Vec<String>, usize, usize) {
        let (ids, ignored, truncated) = parse_used_ids(Some(&v));
        (ids.into_iter().map(|n| n.0).collect(), ignored, truncated)
    }

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn used_ids_parse_every_shape_python_accepts() {
        assert_eq!(ids(json!([1421, 903, 1421])), (s(&["1421", "903"]), 0, 0));
        assert_eq!(ids(json!("[1421, 903]")), (s(&["1421", "903"]), 0, 0));
        assert_eq!(
            ids(json!("1421, 903 77")),
            (s(&["1421", "903", "77"]), 0, 0)
        );
        assert_eq!(ids(json!(" 12 ")), (s(&["12"]), 0, 0));
        assert_eq!(
            ids(json!("[\"5\", \"x\", 1.5, true, null]")),
            (s(&["5"]), 4, 0)
        );
        // Not JSON: split as text, quotes and brackets stripped per token.
        assert_eq!(ids(json!("(7, '8', abc,)")), (s(&["7", "8"]), 1, 0));
        // int(): sign, leading zeros, single underscores.
        assert_eq!(
            ids(json!("+007 -0 1_000 1__0 _1")),
            (s(&["7", "0", "1000"]), 2, 0)
        );
        // A float or other JSON scalar is split as text.
        assert_eq!(ids(json!("1e3")), (s(&[]), 1, 0));
        assert_eq!(ids(json!("")), (s(&[]), 0, 0));
        assert_eq!(ids(json!([])), (s(&[]), 0, 0));
        assert_eq!(ids(json!(true)), (s(&[]), 0, 0));
        // Big integers stay exact.
        assert_eq!(
            ids(json!("99999999999999999999")),
            (s(&["99999999999999999999"]), 0, 0)
        );
    }

    #[test]
    fn used_ids_cap_at_fifty_and_count_the_rest() {
        let many: Vec<i64> = (1..=53).collect();
        let (got, ignored, truncated) = ids(json!(many));
        assert_eq!(got.len(), USED_IDS_CAP);
        assert_eq!((ignored, truncated), (0, 3));
        assert_eq!(got[49], "50");
    }

    #[test]
    fn a_big_id_writes_back_as_the_same_json_number() {
        let n = PyInt::parse("123456789012345678901234567890").unwrap();
        assert_eq!(n.as_i64(), None);
        assert_eq!(n.to_json().to_string(), "123456789012345678901234567890");
    }
}
