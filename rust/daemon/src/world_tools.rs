//! `memory_world_set` and `memory_world_search` over MCP (spec "World cortex
//! tools over MCP", WD1-WD12). Python: `mcp_server.py` `memory_world_set`
//! (1834-1868), `memory_world_search` (1871-1903), `_compact_world`
//! (1906-1921), `_world_correct_with` (1038-1049), `_WORLD_NOTES` (747-750);
//! `service.py` `_STALE_*` and `_apply_stale_policy` / `_demote_stale`
//! (268-294), `_URL_SCHEME` / `_is_safe_source_url` (362-382),
//! `_world_record_to_dict` (385-408), `world_write` (4816-4842),
//! `world_search` (4851-4864); the store is `stores/world.rs`.

use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::board::text::py_strip;
use crate::episodes::Caller;
use crate::fact_set::{embed, internal};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::cortex_search::{apply_stale_policy, ttl};
use crate::read::mcp_search::CORRECTION_NOTE;
use crate::service::{Ready, Service};
use crate::stores::world::{ORIGIN, WorldRecord, WorldWrite};

/// `_WORLD_NOTES["unsafe_source_url"]`.
const UNSAFE_URL_NOTE: &str =
    "Not stored: source_url must be an http(s) URL or empty; other schemes are refused.";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("memory_world_set", mcp_memory_world_set),
        ("memory_world_search", mcp_memory_world_search),
    ]
}

fn text_arg(a: &Map<String, Value>, key: &str) -> Option<String> {
    a.get(key).and_then(Value::as_str).map(str::to_string)
}

/// `_is_safe_source_url`: empty, an http(s) URL, or a scheme-less string,
/// after removing every character U+0000..U+0020 and lowercasing.
fn is_safe_source_url(url: &str) -> bool {
    if url.is_empty() || crate::mutants::active("mcp-world-set-unsafe-allowed") {
        return true;
    }
    let cleaned: String = url
        .chars()
        .filter(|&c| c > '\u{20}')
        .collect::<String>()
        .to_lowercase();
    if cleaned.starts_with("http://") || cleaned.starts_with("https://") {
        return true;
    }
    // `_URL_SCHEME.match`: `[a-z][a-z0-9+.\-]*:` at the start.
    let mut chars = cleaned.chars();
    if !chars.next().is_some_and(|c| c.is_ascii_lowercase()) {
        return true;
    }
    for c in chars {
        if c == ':' {
            return false;
        }
        if !(c.is_ascii_lowercase() || c.is_ascii_digit() || "+.-".contains(c)) {
            return true;
        }
    }
    true
}

/// `_world_record_to_dict(rec, stale_policy=...)`.
pub(crate) fn record_to_dict(r: &WorldRecord, stale_policy: &str, now: f64) -> Map<String, Value> {
    let mut d = Map::new();
    d.insert("entity".into(), json!(r.entity));
    d.insert("attribute".into(), json!(r.attribute));
    d.insert("value".into(), json!(r.value));
    d.insert("polarity".into(), json!(r.polarity));
    d.insert("status".into(), json!(r.status));
    d.insert(
        "confidence".into(),
        json!(crate::health::round4(r.confidence)),
    );
    d.insert(
        "effective_confidence".into(),
        json!(crate::health::round4(r.effective_confidence(now))),
    );
    d.insert("stale".into(), json!(r.is_stale(now)));
    d.insert("origin".into(), json!(ORIGIN));
    d.insert("freshness_class".into(), json!(r.freshness_class));
    d.insert("source_url".into(), json!(r.source_url));
    d.insert("source_quote".into(), json!(r.source_quote));
    d.insert("retrieved_at".into(), json!(r.retrieved_at));
    d.insert("asserted_at".into(), json!(r.asserted_at));
    d.insert("last_confirmed".into(), json!(r.last_confirmed));
    d.insert("supersedes_value".into(), json!(r.supersedes_value));
    d.insert("superseded_by_value".into(), json!(r.superseded_by_value));
    d.insert("superseded_at".into(), json!(r.superseded_at));
    apply_stale_policy(&mut d, stale_policy);
    d
}

struct SetArgs {
    entity: String,
    attribute: String,
    value: String,
    source_url: String,
    source_quote: String,
    freshness_class: String,
    confidence: f64,
    retrieved_at: Option<f64>,
    content_hash: Option<String>,
}

fn mcp_memory_world_set<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let float = |k: &str| a.get(k).and_then(crate::mcp::binding::float_arg);
        let args = SetArgs {
            entity: text_arg(a, "entity").unwrap_or_default(),
            attribute: text_arg(a, "attribute").unwrap_or_default(),
            value: text_arg(a, "value").unwrap_or_default(),
            source_url: text_arg(a, "source_url").unwrap_or_default(),
            source_quote: text_arg(a, "source_quote").unwrap_or_default(),
            freshness_class: text_arg(a, "freshness_class").unwrap_or_else(|| "volatile".into()),
            confidence: float("confidence").unwrap_or(0.85),
            retrieved_at: float("retrieved_at"),
            content_hash: text_arg(a, "content_hash"),
        };
        // Refused at the write boundary, before the service initializes.
        if !is_safe_source_url(&args.source_url) {
            return Ok(json!({"action": "rejected", "reason": "unsafe_source_url",
                             "source_url": args.source_url, "note": UNSAFE_URL_NOTE}));
        }
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let caller = Caller {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let writer = call.ident.writer.clone();
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { world_write(&svc, &r2, &caller, writer, args).await })
            .await
            .map_err(internal)?
    })
}

/// `service.world_write` under the write lock: the embedding of
/// `f"{entity} {attribute} {value}".strip()`, the writer and session, one
/// HLC tick, the store write, then the slot save.
async fn world_write(
    svc: &Service,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: String,
    a: SetArgs,
) -> Result<Value, ToolError> {
    let ws = ready.write.lock().await;
    let text = format!("{} {} {}", a.entity, a.attribute, a.value);
    let embedding = embed(ready, py_strip(&text).to_string()).await?;
    let session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let hlc = ready.tick_hlc();
    let now = crate::episodes::now();
    let w = WorldWrite {
        confidence: a.confidence,
        source_url: &a.source_url,
        source_quote: &a.source_quote,
        freshness_class: &a.freshness_class,
        retrieved_at: a.retrieved_at,
        content_hash: a.content_hash.as_deref(),
        now,
        hlc,
        writer_id: Some(writer.as_str()),
        session_id: session.as_deref(),
    };
    let (action, idx) = ready
        .world
        .write(|s| s.write_fact(&a.entity, &a.attribute, &a.value, Some(embedding), &w));
    if !crate::mutants::active("mcp-world-set-no-save") {
        save_world(svc, ready).await?;
    }
    let snap = ready.world.snapshot();
    let policy = &svc.config.memory.search.stale_policy;
    let mut out = Map::new();
    out.insert("action".into(), json!(action));
    out.extend(record_to_dict(
        &snap.records[idx],
        policy,
        crate::episodes::now(),
    ));
    drop(ws);
    Ok(Value::Object(out))
}

/// `_save_world`: the dirty slots' rows; a failure counts a persistence
/// error and raises (`PersistenceError`), as `save_cortex` does.
async fn save_world(svc: &Service, ready: &Ready) -> Result<(), ToolError> {
    let snap = ready.world.snapshot();
    let synced = crate::stores::world::sync_slot_rows(ready.storage.client(), &snap).await;
    drop(snap);
    let slots = synced.map_err(|e| {
        svc.persist_errors
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        eprintln!("World cortex save failed (NOT durably persisted): {e:#}");
        internal(format!("world cortex save failed: {e:#}"))
    })?;
    ready.world.write(|s| {
        for key in &slots {
            s.dirty_slots.remove(key);
        }
    });
    Ok(())
}

fn mcp_memory_world_search<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let query = text_arg(a, "query").unwrap_or_default();
        // The binder admits any integer >= 1; past usize, everything.
        let top_k = a.get("top_k").map_or(5, |v| {
            v.as_u64()
                .map_or(usize::MAX, |n| usize::try_from(n).unwrap_or(usize::MAX))
        });
        let verbose = a.get("verbose").and_then(Value::as_bool).unwrap_or(false);
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let embedder = ready.embedder.clone();
        let q = tokio::task::spawn_blocking(move || embedder.embed_query(&query))
            .await
            .map_err(internal)?
            .map_err(internal)?;
        let snap = ready.world.snapshot();
        let policy = app.service.config.memory.search.stale_policy.clone();
        let now = crate::episodes::now();
        let mut entries: Vec<Map<String, Value>> = snap
            .search(&q, top_k, 0.0)
            .map_err(internal)?
            .into_iter()
            .map(|(i, s)| {
                let mut d = record_to_dict(&snap.records[i], &policy, now);
                d.insert("score".into(), json!(crate::health::round4(s)));
                d
            })
            .collect();
        // `_demote_stale`: stale last, otherwise in score order.
        if policy == "demote" {
            entries.sort_by_key(|d| d.get("stale") == Some(&Value::Bool(true)));
        }
        let mut flagged = false;
        for e in &mut entries {
            if let Some(cw) = correct_with(e)
                && !crate::mutants::active("mcp-world-search-no-correct-with")
            {
                e.insert("correct_with".into(), json!(cw));
                flagged = true;
            }
        }
        let mut out = Map::new();
        out.insert("count".into(), json!(entries.len()));
        let entries: Vec<Value> = entries
            .into_iter()
            .map(|e| Value::Object(if verbose { e } else { compact(&e) }))
            .collect();
        out.insert("entries".into(), Value::Array(entries));
        if flagged {
            out.insert("correction_note".into(), json!(CORRECTION_NOTE));
        }
        Ok(Value::Object(out))
    })
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

/// `_world_correct_with`: the correction call for a stale entry, or one
/// past a third of its class TTL since `last_confirmed or retrieved_at or
/// asserted_at` (the last even when falsy).
fn correct_with(e: &Map<String, Value>) -> Option<String> {
    let class = e
        .get("freshness_class")
        .filter(|v| truthy(Some(v)))
        .and_then(Value::as_str)
        .unwrap_or("volatile");
    let anchor = ["last_confirmed", "retrieved_at"]
        .iter()
        .map(|k| e.get(*k))
        .find(|v| truthy(*v))
        .flatten()
        .or_else(|| e.get("asserted_at"))
        .and_then(Value::as_f64);
    let aged = match (ttl(class), anchor) {
        (Some(t), Some(at)) => crate::episodes::now() - at > t / 3.0,
        _ => false,
    };
    if !(truthy(e.get("stale")) || aged) {
        return None;
    }
    let repr = |k: &str| crate::labels::py_repr(e.get(k).and_then(Value::as_str).unwrap_or(""));
    Some(format!(
        "memory_world_set(entity={}, attribute={}, value=<the verified current value>, \
         source_url=<citation>)",
        repr("entity"),
        repr("attribute")
    ))
}

/// `_compact_world`.
fn compact(e: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for k in [
        "entity",
        "attribute",
        "value",
        "effective_confidence",
        "stale",
        "score",
        "correct_with",
        "warning",
    ] {
        if let Some(v) = e.get(k) {
            out.insert(k.into(), v.clone());
        }
    }
    if let Some(v) = e.get("last_known_value") {
        out.insert("last_known_value".into(), v.clone());
    }
    for k in ["source_url", "source_quote"] {
        if truthy(e.get(k)) {
            out.insert(k.into(), e[k].clone());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_urls_follow_python_is_safe_source_url() {
        // Python's answers, recorded with CPython 3.11.
        for (url, safe) in [
            ("", true),
            ("https://example.com/a", true),
            ("HTTP://EXAMPLE.COM", true),
            ("javascript:alert(1)", false),
            ("\tJaVaScRiPt:alert(1)", false),
            ("java\u{0}script:x", false),
            ("data:text/html,x", false),
            ("vbscript:x", false),
            ("example.com/page", true),
            ("/relative/path", true),
            ("1http:x", true),
            ("mailto:a@b.c", false),
            ("ftp://host/file", false),
            ("h ttps://x", true),
            ("\u{212a}ey:x", false),
        ] {
            assert_eq!(is_safe_source_url(url), safe, "{url:?}");
        }
    }
}
