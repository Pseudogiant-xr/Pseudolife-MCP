//! Episode curation: `POST /api/episodes/prune`, `/rename` and `/merge`
//! (`web/routes.py:166-172`; `service.py` `episode_prune_empty`,
//! `episode_rename`, `episode_merge`). The body arrives as Python's route
//! lambdas read it, with no validation: a missing `id` is a `KeyError`
//! (answered as the 405 `web/api.py` gives a known path), and a field of the
//! wrong type raises where Python's would, as a 500 with the exception text.

use super::console::Request;
use crate::episodes::{Episode, WriteState};
use crate::http::{json_response, py_strip};
use crate::service::Ready;
use axum::response::Response;
use serde_json::{Map, Value, json};
use std::sync::Arc;

/// The Console registrations this family serves in full.
#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("POST", "/api/episodes/prune"),
    ("POST", "/api/episodes/rename"),
    ("POST", "/api/episodes/merge"),
];

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    let op = match (req.method, req.path) {
        ("POST", "/api/episodes/prune") => Op::Prune,
        ("POST", "/api/episodes/rename") => Op::Rename,
        ("POST", "/api/episodes/merge") => Op::Merge,
        _ => return None,
    };
    Some(answer(req.app, req.path, op, std::mem::take(&mut req.body)).await)
}

#[derive(Clone, Copy)]
enum Op {
    Prune,
    Rename,
    Merge,
}

/// How a handler ends other than with its dict.
enum Failure {
    /// A `KeyError` out of the route lambda.
    KeyError,
    /// Any other exception: its `str()`.
    Raised(String),
}

async fn answer(app: &crate::http::App, path: &str, op: Op, body: Map<String, Value>) -> Response {
    let result = match op {
        Op::Rename => match rename_args(&body) {
            Err(f) => Err(f),
            // `episode_rename` answers an empty title before it initializes.
            Ok(None) => Ok(json!({"ok": false, "reason": "empty title"})),
            Ok(Some((id, title))) => {
                run(
                    app,
                    move |ready| async move { rename(&ready, id, title).await },
                )
                .await
            }
        },
        Op::Prune => {
            let include_open = body
                .get("include_open")
                .is_some_and(crate::board::store::py_truthy);
            run(app, move |ready| async move {
                prune(&ready, include_open).await
            })
            .await
        }
        Op::Merge => run(app, move |ready| async move { merge(&ready, body).await }).await,
    };
    match result {
        Ok(v) => json_response(200, &v),
        Err(Failure::KeyError) => {
            json_response(405, &json!({"error": "method_not_allowed", "path": path}))
        }
        Err(Failure::Raised(e)) => json_response(500, &json!({"error": e})),
    }
}

/// Initialize, then run the body to completion in its own task: Python's
/// handler runs on a worker thread a disconnecting client cannot stop.
async fn run<F, Fut>(app: &crate::http::App, body: F) -> Result<Value, Failure>
where
    F: FnOnce(Arc<Ready>) -> Fut + Send + 'static,
    Fut: std::future::Future<Output = Result<Value, Failure>> + Send + 'static,
{
    let ready = app.service.ensure_init().await.map_err(Failure::Raised)?;
    let task = ready.writes.spawn(body(ready.clone()));
    task.await
        .map_err(|e| Failure::Raised(format!("episode curation task failed: {e}")))?
}

/// `type(v).__name__` for a value loaded from JSON.
fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if !n.to_string().contains(['.', 'e', 'E']) => "int",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `(v or "").strip()`.
fn strip_or_empty(v: Option<&Value>) -> Result<String, Failure> {
    match v {
        None => Ok(String::new()),
        Some(v) if !crate::board::store::py_truthy(v) => Ok(String::new()),
        Some(Value::String(s)) => Ok(py_strip(s).to_string()),
        Some(other) => Err(Failure::Raised(format!(
            "'{}' object has no attribute 'strip'",
            py_type(other)
        ))),
    }
}

/// Hashing a value (a dict key or lookup): lists and dicts raise.
fn hashable(v: &Value) -> Result<(), Failure> {
    match v {
        Value::Array(_) | Value::Object(_) => Err(Failure::Raised(format!(
            "unhashable type: '{}'",
            py_type(v)
        ))),
        _ => Ok(()),
    }
}

/// A bool or number as Python holds it: an exact integer's digits, or a
/// float.
enum PyNumber {
    Int(String),
    Float(f64),
}

fn py_number(v: &Value) -> Option<PyNumber> {
    match v {
        Value::Bool(b) => Some(PyNumber::Int(u8::from(*b).to_string())),
        Value::Number(n) => {
            let text = crate::pyjson::number_text(n);
            Some(if n.to_string().contains(['.', 'e', 'E']) {
                PyNumber::Float(n.to_string().parse().unwrap_or(f64::NAN))
            } else {
                PyNumber::Int(text)
            })
        }
        _ => None,
    }
}

/// `a == b` for hashable values loaded from JSON: `1 == 1.0 == True`, with
/// an int compared to an int or a float exactly, as Python compares them.
fn py_eq(a: &Value, b: &Value) -> bool {
    use PyNumber::{Float, Int};
    let exact =
        |int: &str, x: f64| x.is_finite() && x.fract() == 0.0 && format!("{:.0}", x + 0.0) == int;
    match (py_number(a), py_number(b)) {
        (Some(Int(x)), Some(Int(y))) => x == y,
        (Some(Float(x)), Some(Float(y))) => x == y,
        (Some(Int(i)), Some(Float(x))) | (Some(Float(x)), Some(Int(i))) => exact(&i, x),
        (None, None) => a == b,
        _ => false,
    }
}

/// `episodes.get(key)`: only a string can name an episode.
fn lookup<'a>(ws: &'a WriteState, key: &Value) -> Option<&'a Episode> {
    key.as_str().and_then(|id| ws.log.get(id))
}

/// `EpisodeManager.remove` plus `_delete_episode_row` (best effort).
async fn remove(ready: &Ready, ws: &mut WriteState, id: &str) {
    if let Some(i) = ws.log.index(id) {
        ws.log.episodes.remove(i);
    }
    if ws.log.current_id.as_deref() == Some(id) {
        ws.log.current_id = None;
    }
    let res = crate::txn::run(ready.storage.client(), |c| async move {
        c.execute("DELETE FROM episodes WHERE id = $1", &[&id])
            .await
            .map(|_| ())
    })
    .await;
    if let Err(e) = res {
        eprintln!("episode delete failed: {e}");
    }
}

/// The route lambda's `b["id"]` and `b.get("title") or ""`, then
/// `episode_rename`'s strip: None for an empty title.
fn rename_args(body: &Map<String, Value>) -> Result<Option<(Value, String)>, Failure> {
    let id = body.get("id").cloned().ok_or(Failure::KeyError)?;
    let title = strip_or_empty(body.get("title"))?;
    Ok((!title.is_empty()).then_some((id, title)))
}

/// `episode_rename` after its empty-title check.
async fn rename(ready: &Arc<Ready>, id: Value, title: String) -> Result<Value, Failure> {
    let mut ws = ready.write.lock().await;
    hashable(&id)?;
    let Some(i) = id.as_str().and_then(|id| ws.log.index(id)) else {
        return Ok(json!({
            "ok": false,
            "reason": format!("unknown episode {}", crate::pyjson::py_str(&id)),
        }));
    };
    crate::episode_tools::retitle(ready, &mut ws, i, &title).await;
    ws.persist_episodes(ready.storage.client()).await;
    let ep = &ws.log.episodes[i];
    Ok(json!({"ok": true, "id": ep.id, "title": ep.title}))
}

/// `_episode_entry_counts`: resident entries per episode, digests excluded.
fn entry_counts(ready: &Ready) -> std::collections::HashMap<String, usize> {
    let bank = ready.bank.read().expect("bank lock");
    let mut counts = std::collections::HashMap::new();
    for e in &bank.entries {
        if let Some(id) = e.episode_id.as_deref().filter(|id| !id.is_empty())
            && (e.source != "digest" || crate::mutants::active("episodes-prune-counts-digests"))
        {
            *counts.entry(id.to_string()).or_insert(0) += 1;
        }
    }
    counts
}

/// `episode_prune_empty`: delete every episode with no attached entry, by
/// default only closed ones. The episode log is not re-persisted.
async fn prune(ready: &Arc<Ready>, include_open: bool) -> Result<Value, Failure> {
    let mut ws = ready.write.lock().await;
    let counts = entry_counts(ready);
    let victims: Vec<String> = ws
        .log
        .episodes
        .iter()
        .filter(|e| counts.get(&e.id).copied().unwrap_or(0) == 0)
        .filter(|e| include_open || e.ended_at.is_some())
        .map(|e| e.id.clone())
        .collect();
    for id in &victims {
        remove(ready, &mut ws, id).await;
    }
    Ok(json!({"deleted": victims.len(), "ids": victims}))
}

/// `source_ids or []` iterated by `dict.fromkeys`: the distinct elements in
/// first-seen order.
fn source_ids(v: Option<&Value>) -> Result<Vec<Value>, Failure> {
    let items: Vec<Value> = match v {
        None => vec![],
        Some(v) if !crate::board::store::py_truthy(v) => vec![],
        Some(Value::Array(a)) => a.clone(),
        Some(Value::String(s)) => s.chars().map(|c| Value::String(c.into())).collect(),
        Some(Value::Object(o)) => o.keys().map(|k| Value::String(k.clone())).collect(),
        Some(other) => {
            return Err(Failure::Raised(format!(
                "'{}' object is not iterable",
                py_type(other)
            )));
        }
    };
    let mut out: Vec<Value> = Vec::new();
    for item in items {
        hashable(&item)?;
        if !out.iter().any(|seen| py_eq(seen, &item)) {
            out.push(item);
        }
    }
    Ok(out)
}

/// A new rollup's hint. Declared divergence: Python keeps any value in
/// memory, and its episode write-through then stores PostgreSQL's text form
/// of a scalar or fails on a list or dict, leaving the retargeted entries
/// pointing at no episode row; Rust refuses anything but a string or null.
fn new_target_hint(hint: &Value) -> Result<Option<String>, Failure> {
    match hint {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        other => Err(Failure::Raised(format!(
            "hint must be a string, not '{}'",
            py_type(other)
        ))),
    }
}

/// `episode_merge`: fold closed source episodes into `into` or into a new
/// closed rollup titled `title`.
async fn merge(ready: &Arc<Ready>, body: Map<String, Value>) -> Result<Value, Failure> {
    let null = Value::Null;
    let into = body.get("into").unwrap_or(&null);
    let title = body.get("title");
    let hint = body.get("hint").unwrap_or(&null);
    let mut ws = ready.write.lock().await;
    let (mut sources, mut skipped_open, mut missing) = (Vec::new(), Vec::new(), Vec::new());
    for sid in source_ids(body.get("sources"))? {
        if !into.is_null() && py_eq(&sid, into) {
            continue;
        }
        match lookup(&ws, &sid) {
            None => missing.push(sid),
            Some(ep) if ep.ended_at.is_none() => skipped_open.push(sid),
            Some(ep) => sources.push(ep.clone()),
        }
    }
    let refusal = |reason: String| json!({"ok": false, "skipped_open": skipped_open, "missing": missing, "reason": reason});
    let target = if !into.is_null() {
        hashable(into)?;
        let Some(t) = lookup(&ws, into) else {
            return Ok(refusal(format!(
                "unknown target episode {}",
                crate::pyjson::py_str(into)
            )));
        };
        Some(t.id.clone())
    } else if strip_or_empty(title)?.is_empty() {
        return Ok(refusal("a new target needs a title".into()));
    } else {
        None
    };
    if sources.is_empty() {
        return Ok(refusal("no closed source episodes to merge".into()));
    }
    let target_id = match target {
        Some(id) => id,
        None => {
            let hint = new_target_hint(hint)?;
            let title = py_strip(title.and_then(Value::as_str).unwrap_or("")).to_string();
            let id = uuid::Uuid::new_v4().simple().to_string();
            ws.log.episodes.push(Episode {
                id: id.clone(),
                title,
                started_at: sources
                    .iter()
                    .map(|s| s.started_at)
                    .fold(f64::INFINITY, f64::min),
                ended_at: sources.iter().filter_map(|s| s.ended_at).reduce(f64::max),
                hint,
                closed_by_new_start: false,
                session_key: None,
                parent_id: None,
            });
            id
        }
    };
    let t = ws.log.index(&target_id).expect("target is in the log");
    let target_title = ws.log.episodes[t].title.clone();
    let mut src_ids: Vec<String> = sources.iter().map(|s| s.id.clone()).collect();
    src_ids.sort();
    src_ids.dedup();
    let client = ready.storage.client();
    let rows: Vec<i64> = {
        let mut bank = ready.bank.write().expect("bank lock");
        bank.entries
            .iter_mut()
            .filter(|e| e.episode_id.as_ref().is_some_and(|id| src_ids.contains(id)))
            .map(|e| {
                if !crate::mutants::active("episodes-merge-rows-only") {
                    e.episode_id = Some(target_id.clone());
                    e.episode_title = Some(target_title.clone());
                }
                e.id
            })
            .collect()
    };
    for row in &rows {
        let (id, title) = (&target_id, &target_title);
        let res = crate::txn::run(client, |c| async move {
            c.execute(
                "UPDATE entries SET episode_id = $1, episode_title = $2 WHERE id = $3",
                &[id, title, row],
            )
            .await
            .map(|_| ())
        })
        .await;
        if let Err(e) = res {
            eprintln!("entry retarget failed: {e}");
        }
    }
    // `retarget_episode_refs`: rows the bank no longer holds, and the
    // outcome-signal log, in one transaction.
    let (ids, id, title) = (&src_ids, &target_id, &target_title);
    let res = crate::txn::run(client, |c| async move {
        c.execute(
            "UPDATE entries SET episode_id = $1, episode_title = $2 WHERE episode_id = ANY($3)",
            &[id, title, ids],
        )
        .await?;
        if crate::mutants::active("episodes-merge-keeps-signals") {
            return Ok(());
        }
        c.execute(
            "UPDATE outcome_signals SET episode_id = $1 WHERE episode_id = ANY($2)",
            &[id, ids],
        )
        .await
        .map(|_| ())
    })
    .await;
    if let Err(e) = res {
        eprintln!("episode retarget failed: {e}");
    }
    for ep in &mut ws.log.episodes {
        if ep.parent_id.as_ref().is_some_and(|p| src_ids.contains(p)) {
            ep.parent_id = Some(target_id.clone());
        }
    }
    let target = &mut ws.log.episodes[t];
    for s in &sources {
        target.started_at = target.started_at.min(s.started_at);
    }
    if let Some(ended) = target.ended_at
        && !crate::mutants::active("episodes-merge-keeps-span")
    {
        // `max([ended_at] + [s.ended_at for s in sources if s.ended_at])`.
        target.ended_at = Some(
            sources
                .iter()
                .filter_map(|s| s.ended_at)
                .filter(|&e| e != 0.0)
                .fold(ended, f64::max),
        );
    }
    for s in &sources {
        remove(ready, &mut ws, &s.id).await;
    }
    ws.persist_episodes(client).await;
    Ok(json!({
        "ok": true,
        "id": target_id,
        "title": target_title,
        "merged": src_ids,
        "entries_moved": rows.len(),
        "skipped_open": skipped_open,
        "missing": missing,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_ids_iterate_and_dedupe_as_dict_fromkeys() {
        let ids = |v: Value| {
            source_ids(Some(&v)).map_err(|f| match f {
                Failure::Raised(e) => e,
                Failure::KeyError => "KeyError".into(),
            })
        };
        assert_eq!(ids(json!(null)).unwrap(), Vec::<Value>::new());
        assert_eq!(ids(json!(0)).unwrap(), Vec::<Value>::new());
        assert_eq!(ids(json!("aba")).unwrap(), vec![json!("a"), json!("b")]);
        assert_eq!(
            ids(json!({"x": 1, "y": 2})).unwrap(),
            vec![json!("x"), json!("y")]
        );
        assert_eq!(
            ids(json!([1, 1.0, true, "1", 0, false])).unwrap(),
            vec![json!(1), json!("1"), json!(0)]
        );
        // Ints compare exactly, beyond f64's 2**53, also against a float.
        let big: Value = serde_json::from_str(
            "[9007199254740993, 9007199254740992, 9007199254740992.0, -0, -0.0]",
        )
        .unwrap();
        assert_eq!(ids(big).unwrap().len(), 3);
        assert_eq!(ids(json!(5)).unwrap_err(), "'int' object is not iterable");
        assert_eq!(
            ids(json!(true)).unwrap_err(),
            "'bool' object is not iterable"
        );
        assert_eq!(
            ids(json!(["a", ["b"]])).unwrap_err(),
            "unhashable type: 'list'"
        );
    }

    #[test]
    fn a_new_rollup_takes_only_a_string_hint() {
        assert_eq!(new_target_hint(&json!(null)).ok(), Some(None));
        assert_eq!(new_target_hint(&json!("h")).ok(), Some(Some("h".into())));
        for (hint, name) in [
            (json!(5), "int"),
            (json!(false), "bool"),
            (json!([]), "list"),
        ] {
            match new_target_hint(&hint) {
                Err(Failure::Raised(e)) => {
                    assert_eq!(e, format!("hint must be a string, not '{name}'"))
                }
                _ => panic!("{hint} accepted"),
            }
        }
    }

    #[test]
    fn rename_args_follow_the_route_lambda() {
        let args = |v: Value| match rename_args(v.as_object().unwrap()) {
            Ok(x) => Ok(x),
            Err(Failure::KeyError) => Err("KeyError".to_string()),
            Err(Failure::Raised(e)) => Err(e),
        };
        assert_eq!(args(json!({"title": "x"})).unwrap_err(), "KeyError");
        assert_eq!(args(json!({"id": "a", "title": "  "})).unwrap(), None);
        assert_eq!(args(json!({"id": "a", "title": 0})).unwrap(), None);
        assert_eq!(
            args(json!({"id": "a", "title": 5})).unwrap_err(),
            "'int' object has no attribute 'strip'"
        );
        assert_eq!(
            args(json!({"id": 7, "title": " t "})).unwrap(),
            Some((json!(7), "t".into()))
        );
    }
}
