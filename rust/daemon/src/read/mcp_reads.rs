//! Read tools over MCP: `memory_recent` (spec "memory_recent over MCP").

use crate::bank::Bank;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::mcp_search::{compact_entry, strings};
use crate::search::{annotate_supersession_in, entry_json};
use serde_json::{Value, json};

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_recent", mcp_memory_recent)]
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
        let result = tokio::task::spawn_blocking(move || {
            let bank = bank.read().expect("bank lock");
            recent(&bank, n, &filters)
        })
        .await
        .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?
        .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
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
    /// `set(entry.tags)` does (unhashable), so the call is an internal error.
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
        if e.tags.iter().any(|t| t.is_array() || t.is_object()) {
            return Err(format!("entry {} has an unhashable tag", e.id));
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
fn recent(bank: &Bank, n: usize, filters: &Filters) -> Result<Value, String> {
    let mut order = Vec::new();
    for (i, e) in bank.entries.iter().enumerate() {
        if filters.admit(e)? {
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

/// `_compact_entries` with no text budget: `count` stays as served.
fn compact(mut result: Value) -> Value {
    if let Some(Value::Array(entries)) = result.get_mut("entries") {
        for e in entries.iter_mut() {
            *e = compact_entry(e, None);
        }
    }
    result
}
