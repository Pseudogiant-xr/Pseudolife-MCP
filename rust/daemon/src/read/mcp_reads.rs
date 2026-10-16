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
