//! The lesson write path: `service.lesson_write` (`service.py:5131-5153`)
//! and `_write_lesson_locked` (5155-5171) over `LessonStore.write_fact`
//! (`stores::lessons`), and `_link_lesson_graph` (5231-5267), which keeps a
//! lesson traversable from the graph: the task-type entity, the `about`
//! object and the `prefers` / `avoids` edge between them.
//!
//! Two callers: the audit-snapshot restore (`graph_review_slots.rs`), which
//! writes through the live store and saves the slot like any write, and
//! lesson synthesis (`dream/lessons.rs`), which writes a staged copy inside
//! its one transaction ([`write_staged`], the graph link on the borrowed
//! client).

use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::episodes::{Caller, WriteState};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::stores::lessons::{LessonStore, LessonWrite};

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

// ── the lesson graph link (service._link_lesson_graph) ───────────────────

/// `graph_consolidation._compound_halves`: split at the first spaced `/`
/// or any `+`; both halves non-empty with ASCII alphanumeric content, and
/// neither ending in a dot-extension.
pub(crate) fn compound_halves(name: &str) -> Option<(String, String)> {
    let space = crate::stores::cortex::py_isspace;
    let chars: Vec<(usize, char)> = name.char_indices().collect();
    let mut found = None;
    for (k, &(i, c)) in chars.iter().enumerate() {
        if space(c)
            && chars.get(k + 1).map(|x| x.1) == Some('/')
            && chars.get(k + 2).is_some_and(|x| space(x.1))
        {
            found = Some((i, chars.get(k + 3).map_or(name.len(), |x| x.0)));
            break;
        }
        if c == '+' {
            found = Some((i, i + 1));
            break;
        }
    }
    let (start, end) = found?;
    let strip = |s: &str| s.trim_matches(space).to_string();
    let (a, b) = (strip(&name[..start]), strip(&name[end..]));
    if a.is_empty() || b.is_empty() {
        return None;
    }
    let alnum = |s: &str| s.chars().any(|c| c.is_ascii_alphanumeric());
    if !alnum(&a) || !alnum(&b) {
        return None;
    }
    if file_extension(&a) || file_extension(&b) {
        return None;
    }
    Some((a, b))
}

/// `re.search(r"\.[A-Za-z0-9]{1,4}$", s)`: `$` also matches before a final
/// newline.
fn file_extension(s: &str) -> bool {
    let at_end = |t: &str| {
        t.rfind('.').is_some_and(|p| {
            let tail = &t[p + 1..];
            (1..=4).contains(&tail.len()) && tail.chars().all(|c| c.is_ascii_alphanumeric())
        })
    };
    at_end(s) || s.strip_suffix('\n').is_some_and(at_end)
}

/// `_link_lesson_graph(task, about, polarity)`: the task-type entity, the
/// `about` object unless it is blank, the task itself, junk or a compound,
/// and the `prefers` / `avoids` edge between them. Each statement is its
/// own transaction, as Python's are outside a batch.
pub(crate) async fn link_lesson_graph(
    client: &Client,
    task: &str,
    about: Option<&str>,
    polarity: &str,
) -> Result<(), ToolError> {
    link(client, task, about, polarity, false).await
}

/// [`link_lesson_graph`] inside a transaction the caller holds.
pub(crate) async fn link_lesson_graph_in(
    c: &Client,
    task: &str,
    about: Option<&str>,
    polarity: &str,
) -> Result<(), ToolError> {
    link(c, task, about, polarity, true).await
}

async fn link(
    client: &Client,
    task: &str,
    about: Option<&str>,
    polarity: &str,
    in_txn: bool,
) -> Result<(), ToolError> {
    use crate::graph_write_tools::{resolve_or_create, resolve_or_create_in};
    use crate::storage::graph::{self, norm_name};
    let tn = norm_name(task);
    if tn.is_empty() {
        return Ok(());
    }
    let id_of = |node: &Value| {
        node.get("id")
            .and_then(Value::as_i64)
            .ok_or_else(|| internal("entity without an id"))
    };
    let node = if in_txn {
        resolve_or_create_in(client, task, Some("task-type")).await?
    } else {
        resolve_or_create(client, task, Some("task-type")).await?
    };
    let tid = id_of(&node)?;
    let Some(about) = about.filter(|a| !a.is_empty()) else {
        return Ok(());
    };
    let an = norm_name(about);
    if an.is_empty() || an == tn {
        return Ok(());
    }
    if crate::junk_names::junk_name_reason(about).is_some() || compound_halves(about).is_some() {
        return Ok(());
    }
    let node = if in_txn {
        resolve_or_create_in(client, about, None).await?
    } else {
        resolve_or_create(client, about, None).await?
    };
    let oid = id_of(&node)?;
    let relation = if polarity == "-" { "avoids" } else { "prefers" };
    let edge = graph::Edge {
        src_id: tid,
        relation,
        dst_id: oid,
        confidence: 0.7,
        origin: Some("action"),
        revive: true,
        source_entry_ids: &[],
    };
    if in_txn {
        graph::upsert_edge_body(client, edge).await
    } else {
        graph::upsert_edge(client, edge).await
    }
    .map_err(internal)?;
    Ok(())
}

// ── lesson_write ─────────────────────────────────────────────────────────

/// The arguments of `service.lesson_write` (its defaults: outcome
/// `success`, polarity `+`, confidence 0.6, origin `agent`).
#[derive(Debug, Clone)]
pub(crate) struct LessonWriteArgs<'a> {
    pub task: &'a str,
    pub aspect: &'a str,
    pub lesson: &'a str,
    pub about: Option<&'a str>,
    pub outcome: &'a str,
    pub polarity: &'a str,
    pub confidence: f64,
    pub origin: &'a str,
    pub provenance: &'a [String],
    pub support: &'a [String],
}

/// The text a lesson's embedding encodes: `f"{task} {aspect} {lesson}".strip()`.
pub(crate) fn embed_text(task: &str, aspect: &str, lesson: &str) -> String {
    crate::storage::py_strip(&format!("{task} {aspect} {lesson}")).to_string()
}

/// `service.lesson_write`, under the write lock the caller holds (`ws`):
/// the claim embedding, the writer and session stamps, an HLC tick, the
/// store write, the graph link, then the slot save. Returns
/// `{"action": ..., **_lesson_record_to_dict(rec)}`.
pub(crate) async fn lesson_write(
    svc: &Service,
    ready: &Ready,
    ws: &WriteState,
    caller: &Caller,
    writer: &str,
    a: &LessonWriteArgs<'_>,
) -> Result<Value, ToolError> {
    // `_ensure_init`'s read barrier for committed cascade retirements.
    crate::forget_tools::ensure_lessons_refreshed(ready)
        .await
        .map_err(internal)?;
    let embedding = crate::fact_set::embed(ready, embed_text(a.task, a.aspect, a.lesson)).await?;
    let session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let hlc = ready.tick_hlc();
    let w = LessonWrite {
        about: a.about,
        outcome: a.outcome,
        polarity: a.polarity,
        confidence: a.confidence,
        origin: Some(a.origin),
        provenance: a.provenance,
        support: a.support,
        now: crate::episodes::now(),
        hlc: Some(hlc),
        valid_time: None,
        writer_id: Some(writer),
        session_id: session.as_deref(),
    };
    let (action, about, polarity, dict) = ready.lessons.write(|s| {
        let (action, i) = s.write_fact(a.task, a.aspect, a.lesson, Some(embedding), &w);
        let r = &s.records[i];
        (
            action,
            r.about.clone(),
            r.polarity.clone(),
            crate::lesson_tools::record_to_dict(r),
        )
    });
    link_lesson_graph(ready.storage.client(), a.task, about.as_deref(), &polarity).await?;
    crate::forget_tools::save_lessons(svc, ready).await?;
    let mut out = Map::new();
    out.insert("action".into(), json!(action));
    out.extend(dict);
    Ok(Value::Object(out))
}

/// `_write_lesson_locked` on a staged store inside lesson synthesis'
/// transaction: the store write, then the graph link on the borrowed
/// client. The caller owns the embedding, the stamps and persistence.
/// Returns the action and the written record's index.
pub(crate) async fn write_staged(
    c: &Client,
    staged: &mut LessonStore,
    task: &str,
    aspect: &str,
    lesson: &str,
    embedding: Arc<[f32]>,
    w: &LessonWrite<'_>,
) -> Result<(&'static str, usize), ToolError> {
    let (action, i) = staged.write_fact(task, aspect, lesson, Some(embedding), w);
    let (about, polarity) = {
        let r = &staged.records[i];
        (r.about.clone(), r.polarity.clone())
    };
    link_lesson_graph_in(c, task, about.as_deref(), &polarity).await?;
    Ok((action, i))
}
