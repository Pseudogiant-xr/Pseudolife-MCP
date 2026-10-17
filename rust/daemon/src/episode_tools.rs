//! The episode tools over MCP (spec "Episode tools over MCP", EP1-EP12):
//! `memory_episode_start`, `memory_episode_end`, `memory_session_title` and
//! the read-only `memory_episode_summary`. Python: `mcp_server.py:2431-2491`;
//! `service.py` `episode_start` (6296-6336), `episode_end` (6338-6394),
//! `set_session_title` (6950-7016), `episode_summary` (7229-7285); the
//! episode primitives are `memory/episodes.py` (`episodes.rs`), the board
//! naming `storage/coordination.py` `title_names` (`board/store.rs`).

use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::board::text::py_strip;
use crate::episodes::{Caller, Episode, WriteState};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::mcp_search::compact_entry;
use crate::search::{annotate_supersession_in, entry_json};
use crate::service::Ready;

/// `episode_summary` serves at most this many recent entries.
const RECENT_CAP: usize = 20;
const HANDLE_WARNING: &str = "unknown or closed episode handle";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("memory_episode_start", mcp_memory_episode_start),
        ("memory_episode_end", mcp_memory_episode_end),
        ("memory_session_title", mcp_memory_session_title),
        ("memory_episode_summary", mcp_memory_episode_summary),
    ]
}

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

fn text_arg(a: &Map<String, Value>, key: &str) -> Option<String> {
    a.get(key).and_then(Value::as_str).map(str::to_string)
}

/// The `episode` handle: `episode or None` ("" from clients means none).
fn handle_arg(a: &Map<String, Value>) -> Option<String> {
    text_arg(a, "episode").filter(|h| !h.is_empty())
}

fn caller(call: &ToolCall<'_>) -> Caller {
    Caller {
        header_session: call.ident.header_session.clone(),
        transport_session: call.ident.transport_session.clone(),
    }
}

/// `_episode_to_dict`.
fn episode_dict(ep: &Episode) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("id".into(), json!(ep.id));
    m.insert("title".into(), json!(ep.title));
    m.insert("started_at".into(), json!(ep.started_at));
    m.insert("ended_at".into(), json!(ep.ended_at));
    m.insert("hint".into(), json!(ep.hint));
    m.insert("closed_by_new_start".into(), json!(ep.closed_by_new_start));
    m.insert("session_key".into(), json!(ep.session_key));
    m.insert("parent_id".into(), json!(ep.parent_id));
    m
}

/// Run a write body to completion in its own task: Python's runs on a
/// worker thread a disconnecting client cannot stop, as `memory_fact_set`'s.
async fn detached<F>(ready: &Arc<Ready>, body: F) -> Result<Value, ToolError>
where
    F: std::future::Future<Output = Result<Value, ToolError>> + Send + 'static,
{
    ready.writes.spawn(body).await.map_err(internal)?
}

fn mcp_memory_episode_start<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let title = text_arg(call.args, "title").unwrap_or_default();
        let hint = text_arg(call.args, "hint");
        let (handle, caller) = (handle_arg(call.args), caller(&call));
        let r2 = ready.clone();
        detached(&ready, async move {
            episode_start(&r2, &caller, title, hint, handle).await
        })
        .await
    })
}

/// `service.episode_start`: a sub-episode nested under the handle root's
/// open subtree leaf (or the root itself), else under the caller's session,
/// whose root opens lazily as a store opens it; then the sweep. An unknown
/// handle degrades to the no-handle path with `episode_warning`.
async fn episode_start(
    ready: &Arc<Ready>,
    caller: &Caller,
    title: String,
    hint: Option<String>,
    handle: Option<String>,
) -> Result<Value, ToolError> {
    let mut ws = ready.write.lock().await;
    let client = ready.storage.client();
    let resolved = ws.resolve_handle(client, handle.as_deref()).await;
    let warning = handle.is_some() && resolved.is_none();
    let (parent, key) = match resolved {
        Some((root, key)) if !crate::mutants::active("mcp-episode-start-no-anchor") => {
            let parent = ws
                .log
                .open_subtree_leaf(&root, key.as_deref())
                .or_else(|| ws.log.index(&root));
            (parent, key)
        }
        _ => {
            let key = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
            ws.ensure_session_episode(client, key.as_deref())
                .await
                .map_err(ToolError::Invalid)?;
            (None, key)
        }
    };
    let i = ws.log.start_nested(title, hint, key, parent);
    ws.persist_episodes(client).await;
    let mut out = episode_dict(&ws.log.episodes[i]);
    if warning {
        out.insert("episode_warning".into(), json!(HANDLE_WARNING));
    }
    Ok(Value::Object(out))
}

fn mcp_memory_episode_end<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let (handle, caller) = (handle_arg(call.args), caller(&call));
        let r2 = ready.clone();
        detached(
            &ready,
            async move { episode_end(&r2, &caller, handle).await },
        )
        .await
    })
}

/// `service.episode_end`: close the caller's open sub-episode and pop to
/// its parent. A session root is never closed here. With a handle only a
/// leaf strictly below its root closes, and a closed or swept handle is
/// refused without being reopened; without one, an open leaf owned by
/// another session (or by one when the caller has none) is refused.
async fn episode_end(
    ready: &Arc<Ready>,
    caller: &Caller,
    handle: Option<String>,
) -> Result<Value, ToolError> {
    let mut ws = ready.write.lock().await;
    let client = ready.storage.client();
    if let Some(h) = handle {
        let Some((root, key)) = ws.resolve_handle_with(client, Some(&h), false).await else {
            return Ok(json!({"closed": null, "reason": HANDLE_WARNING}));
        };
        let Some(leaf) = ws.log.open_subtree_leaf(&root, key.as_deref()) else {
            return Ok(json!({}));
        };
        let i = ws.log.end_episode(leaf);
        ws.persist_episodes(client).await;
        return Ok(Value::Object(episode_dict(&ws.log.episodes[i])));
    }
    let key = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let open = match key.as_deref() {
        Some(k) => ws.log.open_leaf_for(Some(k)),
        None => ws.log.open_episode(),
    };
    let Some(i) = open else {
        return Ok(json!({}));
    };
    let ep = &ws.log.episodes[i];
    if ep.session_key != key {
        return Ok(json!({"closed": null, "reason": "no owned open session"}));
    }
    if ep.parent_id.is_none()
        && ep.session_key.is_some()
        && !crate::mutants::active("mcp-episode-end-pops-root")
    {
        return Ok(json!({}));
    }
    let closed = ws.log.end_leaf(key.as_deref());
    ws.persist_episodes(client).await;
    Ok(closed.map_or(json!({}), |i| {
        Value::Object(episode_dict(&ws.log.episodes[i]))
    }))
}

fn mcp_memory_session_title<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let title = text_arg(call.args, "title").unwrap_or_default();
        let (handle, caller) = (handle_arg(call.args), caller(&call));
        // `_request_principal`: the bearer's principal, None when it names
        // none (the board then names no row).
        let headers = crate::board::http::header_dict(&call.ident.headers);
        let principal = crate::board::http::authenticated_principal(app, &headers).ok();
        let (r2, board) = (ready.clone(), app.board.clone());
        detached(&ready, async move {
            let (out, named) = retitle_session(&r2, &caller, &title, handle).await?;
            if let Some((keys, title)) = named
                && !crate::mutants::active("mcp-session-title-no-board")
                && let Err(e) = board.title_names(&keys, &title, principal.as_deref()).await
            {
                // A board name never fails a retitle.
                eprintln!("board name from session title not set ({e:?})");
            }
            Ok(out)
        })
        .await
    })
}

/// `service._retitle_session`: the answer, and on success the session's
/// keys (its key, then its root id) with the stripped title for the board,
/// which is named after the write lock is released.
async fn retitle_session(
    ready: &Arc<Ready>,
    caller: &Caller,
    title: &str,
    handle: Option<String>,
) -> Result<(Value, Option<(Vec<String>, String)>), ToolError> {
    let title = py_strip(title).to_string();
    if title.is_empty() {
        return Ok((json!({"ok": false, "reason": "empty title"}), None));
    }
    let mut ws = ready.write.lock().await;
    let client = ready.storage.client();
    let resolved = ws.resolve_handle(client, handle.as_deref()).await;
    let root = if let Some((id, _)) = resolved {
        let i = ws
            .log
            .index(&id)
            .ok_or_else(|| internal("resolved root vanished"))?;
        retitle(ready, &mut ws, i, &title).await;
        i
    } else {
        if handle.is_some() {
            return Ok((json!({"ok": false, "reason": HANDLE_WARNING}), None));
        }
        let key = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
        let Some(key) = key.filter(|k| !k.is_empty()) else {
            return Ok((
                json!({"ok": false, "reason": "no session id on this request"}),
                None,
            ));
        };
        let mut root = ws.log.session_root(Some(&key));
        if root.is_none() {
            // A root the idle reaper closed reopens, as a store reopens it.
            root = ws
                .resume_closed_session(client, &key)
                .await
                .map_err(ToolError::Invalid)?;
        }
        match root {
            // `start_session` keeps an open leaf for the key untitled.
            None => ws.log.start_session(title.clone(), Some(key)),
            Some(i) => {
                retitle(ready, &mut ws, i, &title).await;
                i
            }
        }
    };
    ws.persist_episodes(client).await;
    let ep = &ws.log.episodes[root];
    let keys = [ep.session_key.clone(), Some(ep.id.clone())]
        .into_iter()
        .flatten()
        .filter(|k| !k.is_empty())
        .collect();
    let out = json!({"ok": true, "id": ep.id, "title": title});
    Ok((out, Some((keys, title))))
}

/// `_retitle_locked`: the episode's title, and the `episode_title` stamp of
/// each resident entry attributed to it that differs, in memory and in its
/// row (one transaction each; a failure is logged and skips that row).
async fn retitle(ready: &Arc<Ready>, ws: &mut WriteState, i: usize, title: &str) {
    ws.log.episodes[i].title = title.to_string();
    if crate::mutants::active("mcp-session-title-no-entry-stamp") {
        return;
    }
    let id = ws.log.episodes[i].id.clone();
    let rows: Vec<i64> = {
        let mut bank = ready.bank.write().expect("bank lock");
        bank.entries
            .iter_mut()
            .filter(|e| e.episode_id.as_deref() == Some(id.as_str()))
            .filter(|e| e.episode_title.as_deref() != Some(title))
            .map(|e| {
                e.episode_title = Some(title.to_string());
                e.id
            })
            .collect()
    };
    let client = ready.storage.client();
    for row in rows {
        let res = crate::txn::run(client, |c| async move {
            c.execute(
                "UPDATE entries SET episode_title = $1 WHERE id = $2",
                &[&title, &row],
            )
            .await
            .map(|_| ())
        })
        .await;
        if let Err(e) = res {
            eprintln!("entry retitle failed: {e}");
        }
    }
}

fn mcp_memory_episode_summary<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let id = text_arg(call.args, "id").unwrap_or_default();
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let band_order: Vec<String> = app
            .service
            .config
            .memory
            .band_specs
            .iter()
            .map(|b| b.name.clone())
            .collect();
        let ws = ready.write.lock().await;
        let Some(ep) = ws.log.get(&id).cloned() else {
            return Ok(json!({"found": false, "id": id}));
        };
        let bank = ready.bank.read().expect("bank lock");
        let out = episode_summary(&bank, &ep, &band_order).map_err(ToolError::Invalid)?;
        drop(bank);
        drop(ws);
        Ok(out)
    })
}

/// A count row's key, compared as Python compares two tags of one type.
fn tag_order(a: &Value, b: &Value) -> std::cmp::Ordering {
    match (a.as_str(), b.as_str()) {
        (Some(x), Some(y)) => x.cmp(y),
        // Non-string tags (no tool writes them) order by their JSON text;
        // Python's order for them is not reproduced (divergences.md).
        _ => a.to_string().cmp(&b.to_string()),
    }
}

/// `(-count, key)` rows in first-seen order before the sort.
fn distribution(counts: Vec<(Value, i64)>, field: &str) -> Value {
    let mut rows = counts;
    if crate::mutants::active("mcp-episode-summary-count-order") {
        rows.sort_by(|a, b| tag_order(&a.0, &b.0));
    } else {
        rows.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| tag_order(&a.0, &b.0)));
    }
    Value::Array(
        rows.into_iter()
            .map(|(k, c)| {
                let mut m = Map::new();
                m.insert(field.into(), k);
                m.insert("count".into(), json!(c));
                Value::Object(m)
            })
            .collect(),
    )
}

fn count(counts: &mut Vec<(Value, i64)>, key: &Value) {
    match counts.iter_mut().find(|(k, _)| k == key) {
        Some((_, c)) => *c += 1,
        None => counts.push((key.clone(), 1)),
    }
}

/// `service.episode_summary` with the MCP compaction: the episode, how many
/// resident entries attribute to it, their tag and source distributions
/// (`(-count, key)` order) and the newest twenty (by timestamp, a stable
/// sort over Python's band-by-band walk) as `_compact_entry` with
/// supersession annotated over every resident entry. A list or dict tag
/// fails as Python's dict key does.
fn episode_summary(
    bank: &crate::bank::Bank,
    ep: &Episode,
    band_order: &[String],
) -> Result<Value, String> {
    let rank = |band: &str| {
        band_order
            .iter()
            .position(|b| b == band)
            .unwrap_or(usize::MAX)
    };
    let mut entries: Vec<usize> = (0..bank.entries.len())
        .filter(|&i| bank.entries[i].episode_id.as_deref() == Some(ep.id.as_str()))
        .collect();
    entries.sort_by_key(|&i| rank(&bank.entries[i].band));
    entries.sort_by(|&x, &y| bank.entries[y].ts.total_cmp(&bank.entries[x].ts));
    let mut tags = Vec::new();
    let mut sources = Vec::new();
    for &i in &entries {
        for t in &bank.entries[i].tags {
            if t.is_array() || t.is_object() {
                let kind = if t.is_array() { "list" } else { "dict" };
                return Err(format!("unhashable type: '{kind}'"));
            }
            count(&mut tags, t);
        }
    }
    for &i in &entries {
        count(&mut sources, &json!(bank.entries[i].source));
    }
    let mut served: Vec<(usize, Value)> = entries
        .iter()
        .take(RECENT_CAP)
        .map(|&i| {
            let mut d = entry_json(&bank.entries[i], 0.0);
            if let Value::Object(m) = &mut d {
                m.remove("score");
            }
            (i, d)
        })
        .collect();
    annotate_supersession_in(bank, &mut served);
    let mut out = Map::new();
    out.insert("found".into(), json!(true));
    out.extend(episode_dict(ep));
    out.insert("entry_count".into(), json!(entries.len()));
    out.insert("tag_distribution".into(), distribution(tags, "tag"));
    out.insert(
        "source_distribution".into(),
        distribution(sources, "source"),
    );
    out.insert(
        "recent_entries".into(),
        Value::Array(served.iter().map(|(_, d)| compact_entry(d, None)).collect()),
    );
    Ok(Value::Object(out))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bank::{Bank, Entry};

    fn entry(id: i64, band: &str, ts: f64, source: &str, tags: &[&str], ep: &str) -> Entry {
        Entry {
            id,
            band: band.into(),
            ts,
            source: source.into(),
            tags: tags.iter().map(|t| json!(t)).collect(),
            episode_id: Some(ep.into()),
            ..Default::default()
        }
    }

    fn episode(id: &str) -> Episode {
        Episode {
            id: id.into(),
            title: "t".into(),
            started_at: 1.0,
            ended_at: None,
            hint: None,
            closed_by_new_start: false,
            session_key: Some("k".into()),
            parent_id: None,
        }
    }

    #[test]
    fn summary_orders_counts_then_keys_and_newest_first_stably() {
        let bank = Bank {
            dim: 0,
            entries: vec![
                entry(1, "working", 5.0, "agent", &["b", "a"], "e"),
                entry(2, "working", 7.0, "status", &["a"], "e"),
                entry(3, "working", 5.0, "agent", &["c"], "e"),
                entry(4, "working", 9.0, "agent", &["z"], "other"),
            ],
            matrix: vec![],
        };
        let out = episode_summary(&bank, &episode("e"), &["working".into()]).unwrap();
        let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
        assert_eq!(
            keys,
            [
                "found",
                "id",
                "title",
                "started_at",
                "ended_at",
                "hint",
                "closed_by_new_start",
                "session_key",
                "parent_id",
                "entry_count",
                "tag_distribution",
                "source_distribution",
                "recent_entries"
            ]
        );
        assert_eq!(out["entry_count"], 3);
        assert_eq!(
            out["tag_distribution"],
            json!([{"tag": "a", "count": 2}, {"tag": "b", "count": 1},
                   {"tag": "c", "count": 1}])
        );
        assert_eq!(
            out["source_distribution"],
            json!([{"source": "agent", "count": 2}, {"source": "status", "count": 1}])
        );
        let ids: Vec<i64> = out["recent_entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["id"].as_i64().unwrap())
            .collect();
        // Equal timestamps keep the walk's order, as Python's reverse sort.
        assert_eq!(ids, [2, 1, 3]);
    }

    #[test]
    fn summary_refuses_an_unhashable_tag_as_python_does() {
        let mut e = entry(1, "working", 1.0, "agent", &[], "e");
        e.tags = vec![json!(["x"])];
        let bank = Bank {
            dim: 0,
            entries: vec![e],
            matrix: vec![],
        };
        assert_eq!(
            episode_summary(&bank, &episode("e"), &["working".into()]).unwrap_err(),
            "unhashable type: 'list'"
        );
    }

    #[test]
    fn summary_caps_recent_entries_but_counts_every_entry() {
        let entries = (0..25)
            .map(|i| entry(i, "working", i as f64, "agent", &[], "e"))
            .collect();
        let bank = Bank {
            dim: 0,
            entries,
            matrix: vec![],
        };
        let out = episode_summary(&bank, &episode("e"), &["working".into()]).unwrap();
        assert_eq!(out["entry_count"], 25);
        assert_eq!(out["recent_entries"].as_array().unwrap().len(), RECENT_CAP);
        assert_eq!(out["recent_entries"][0]["id"], 24);
    }
}
