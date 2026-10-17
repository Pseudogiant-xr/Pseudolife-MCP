//! `memory_search` on the MCP surface (`mcp_server.memory_search`,
//! `_project_search`, `_compact_entry`): the search and cortex-first block
//! the REST route runs (`search_route::search_with_facts`), sized to the
//! caller, then the MCP payload projection. Spec W2-D "memory_search over
//! MCP"; `explain` is declared (`divergences.md`, MCP-SEARCH-EXPLAIN).

use super::search_route::{Request, search_with_facts, unknown_bands};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use serde_json::{Map, Value, json};

/// `CORRECTION_NOTE` (`mcp_server.py:984-990`).
const CORRECTION_NOTE: &str = "Facts flagged correct_with above are aged or contested. If an aged fact contradicts what you observe, run its call NOW with the verified value (re-assert the same value if it checks out) \u{2014} noticing without writing leaves the error for the next session. For a contested fact, read the contenders; resolve only once the human decides.";

/// `_REPLACED_BY_PREVIEW_CHARS`.
const REPLACED_BY_PREVIEW_CHARS: i64 = 120;

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_search", mcp_memory_search)]
}

pub(crate) fn strings(v: Option<&Value>) -> Option<Vec<String>> {
    // `list(x) if x else None`: an empty list is no filter.
    let items = v?.as_array().filter(|a| !a.is_empty())?;
    Some(
        items
            .iter()
            .map(|s| s.as_str().unwrap_or_default().to_string())
            .collect(),
    )
}

fn mcp_memory_search<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let flag = |k: &str| a.get(k).and_then(Value::as_bool).unwrap_or(false);
        let (verbose, explain) = (flag("verbose"), flag("explain"));
        if explain {
            return Err(ToolError::refused(
                "not_implemented",
                Some("explain (the ranking trace) is not yet served by the Rust daemon"),
            ));
        }
        let ready = app
            .service
            .ensure_init()
            .await
            .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?;
        let cfg = &app.service.config.memory;
        let bands = strings(a.get("bands"));
        if let Some(msg) = unknown_bands(cfg, bands.as_deref()) {
            return Err(ToolError::Invalid(msg));
        }
        let episodes = match strings(a.get("episodes")) {
            Some(ids) => Some(ready.write.lock().await.log.episode_subtree(&ids)),
            None => None,
        };
        let raw_q = a.get("query").and_then(Value::as_str).unwrap_or_default();
        let query = crate::http::py_strip(raw_q);
        let compact = !(verbose || explain);
        let text_chars = cfg.mcp.compact_payloads.then_some(cfg.mcp.entry_text_chars);
        if query.is_empty() {
            let blank = json!({"entries": [], "query": "", "count": 0, "low_confidence": true});
            let Value::Object(blank) = blank else {
                unreachable!()
            };
            return project(blank, &[], compact, text_chars);
        }
        // The binder took `ge=1`; a value past i64 asks for every hit.
        let top_k = a
            .get("top_k")
            .and_then(|n| n.to_string().parse::<i64>().ok())
            .unwrap_or(i64::MAX);
        let fact_k = if cfg.mcp.compact_payloads {
            top_k.clamp(1, 5) as usize
        } else {
            5
        };
        let req = Request {
            query: query.to_string(),
            top_k,
            sources: strings(a.get("sources")),
            bands,
            tags: strings(a.get("tags")),
            episodes,
            min_score: a.get("min_score").and_then(crate::mcp::binding::float_arg),
            disable_recency_boost: flag("disable_recency_boost"),
            rerank: a.get("rerank").and_then(Value::as_bool),
            bm25: a.get("bm25").and_then(Value::as_bool),
            count_access: true,
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let (searched, facts) = search_with_facts(&app.service, &ready, req, raw_q, fact_k)
            .await
            .map_err(ToolError::Internal)?;
        project(searched.body, &facts, compact, text_chars)
    })
}

// The body-slice error type (`dispatch::ToolError`) is the large one.
#[allow(clippy::result_large_err)]
/// `_project_search`: the cortex whitelist, the correction note, restated
/// entries dropped, the low-confidence override, then compact entries.
fn project(
    mut result: Map<String, Value>,
    facts: &[Value],
    compact: bool,
    text_chars: Option<i64>,
) -> Result<Value, ToolError> {
    if !result.contains_key("cortex") {
        result.insert("cortex".into(), json!([]));
    }
    if !facts.is_empty() {
        let served: Vec<Value> = facts.iter().map(project_fact).collect();
        let note = served.iter().any(|c| c.get("correct_with").is_some());
        result.insert("cortex".into(), Value::Array(served));
        if note {
            result.insert("correction_note".into(), json!(CORRECTION_NOTE));
        }
        // Against the underlying value, not a quarantine wrapper.
        let mut values = Vec::with_capacity(facts.len());
        for f in facts {
            let v = f
                .get("last_known_value")
                .or_else(|| f.get("value"))
                .cloned()
                .unwrap_or(json!(""));
            values.push(match v {
                Value::String(s) => s,
                Value::Null => String::new(),
                other => {
                    return Err(ToolError::Internal(anyhow::anyhow!(
                        "cortex value is not text: {other}"
                    )));
                }
            });
        }
        let entries = result
            .get("entries")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let kept: Vec<Value> = entries
            .into_iter()
            .filter(|e| {
                let text = e.get("text").and_then(Value::as_str).unwrap_or_default();
                crate::mutants::active("mcp-search-no-dedup")
                    || !values.iter().any(|v| restates_fact(text, v))
            })
            .collect();
        result.insert("count".into(), json!(kept.len()));
        result.insert("entries".into(), Value::Array(kept));
    }
    let low = result
        .get("low_confidence")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let any_cortex = result
        .get("cortex")
        .and_then(Value::as_array)
        .is_some_and(|c| !c.is_empty());
    result.insert("low_confidence".into(), json!(low && !any_cortex));
    if compact {
        let entries = result
            .get("entries")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let compacted = entries
            .iter()
            .map(|e| compact_entry(e, text_chars))
            .collect();
        result.insert("entries".into(), Value::Array(compacted));
    }
    Ok(Value::Object(result))
}

fn truthy(v: Option<&Value>) -> bool {
    v.is_some_and(crate::board::store::py_truthy)
}

fn get(f: &Value, k: &str) -> Value {
    f.get(k).cloned().unwrap_or(Value::Null)
}

/// One served cortex fact (`_project_search`'s whitelist, in its key order).
fn project_fact(f: &Value) -> Value {
    let mut out = Map::new();
    for k in ["entity", "attribute", "value"] {
        out.insert(k.into(), get(f, k));
    }
    out.insert(
        "origin".into(),
        f.get("origin").cloned().unwrap_or(json!("")),
    );
    out.insert("confidence".into(), get(f, "confidence"));
    out.insert("score".into(), get(f, "score"));
    out.insert(
        "contested".into(),
        f.get("contested").cloned().unwrap_or(json!(false)),
    );
    out.insert("asserted_at".into(), opt(iso_seconds(f.get("asserted_at"))));
    out.insert(
        "last_confirmed".into(),
        opt(iso_seconds(f.get("last_confirmed"))),
    );
    out.insert("age".into(), get(f, "age"));
    let class = f
        .get("freshness_class")
        .filter(|v| truthy(Some(v)))
        .and_then(Value::as_str)
        .unwrap_or("evergreen");
    if class != "evergreen" {
        out.insert("freshness_class".into(), get(f, "freshness_class"));
        out.insert(
            "effective_confidence".into(),
            get(f, "effective_confidence"),
        );
        out.insert(
            "stale".into(),
            f.get("stale").cloned().unwrap_or(json!(false)),
        );
    }
    if truthy(f.get("contested")) {
        out.insert("contender_value".into(), get(f, "contender_value"));
        out.insert(
            "contender_origin".into(),
            f.get("contender_origin").cloned().unwrap_or(json!("")),
        );
    }
    if truthy(f.get("warning")) {
        out.insert("warning".into(), get(f, "warning"));
    }
    if let Some(v) = f.get("last_known_value") {
        out.insert("last_known_value".into(), v.clone());
    }
    if truthy(f.get("re_verify")) {
        out.insert("re_verify".into(), json!(true));
        out.insert("re_verify_reason".into(), get(f, "re_verify_reason"));
    }
    for k in ["authority", "distortion_tolerance"] {
        if truthy(f.get(k)) {
            out.insert(k.into(), get(f, k));
        }
    }
    if truthy(f.get("pinned")) {
        out.insert("pinned".into(), json!(true));
    }
    if let Some(cw) = correct_with(f) {
        out.insert("correct_with".into(), json!(cw));
    }
    Value::Object(out)
}

fn opt(s: Option<String>) -> Value {
    s.map_or(Value::Null, Value::String)
}

fn number(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        n @ Value::Number(_) => crate::mcp::binding::float_arg(n),
        _ => None,
    }
}

/// `_cortex_correct_with`: the resolve path for a contested fact, else the
/// re-assert call for a stale or aged one.
fn correct_with(f: &Value) -> Option<String> {
    let repr = |k: &str| crate::labels::py_repr(f.get(k).and_then(Value::as_str).unwrap_or(""));
    if truthy(f.get("contested")) {
        let slot = format!("entity={}, attribute={}", repr("entity"), repr("attribute"));
        return Some(format!(
            "memory_fact_get({slot}) to read the contenders; once the human decides, \
             memory_fact_resolve({slot}, accept=<the human's decision>) (core tier)"
        ));
    }
    let class = f
        .get("freshness_class")
        .filter(|v| truthy(Some(v)))
        .and_then(Value::as_str)
        .unwrap_or("evergreen");
    // `last_confirmed or asserted_at`: the second even when falsy.
    let anchor = number(if truthy(f.get("last_confirmed")) {
        f.get("last_confirmed")
    } else {
        f.get("asserted_at")
    });
    let aged = match (super::cortex_search::ttl(class), anchor) {
        (Some(ttl), Some(at)) => now() - at > ttl / 3.0,
        _ => false,
    };
    if !(truthy(f.get("stale")) || aged) || crate::mutants::active("mcp-search-no-correct-with") {
        return None;
    }
    Some(format!(
        "memory_fact_set(entity={}, attribute={}, value=<the verified current value>)",
        repr("entity"),
        repr("attribute")
    ))
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `_iso_seconds`: epoch seconds to local ISO-8601 to the second, None for a
/// missing or zero stamp or one `datetime.fromtimestamp` refuses.
fn iso_seconds(v: Option<&Value>) -> Option<String> {
    use chrono::{Datelike, TimeZone};
    let ts = number(v.filter(|v| truthy(Some(v))))?;
    if !ts.is_finite() {
        return None;
    }
    // fromtimestamp rounds to microseconds, half to even, before isoformat
    // drops them.
    let mut secs = ts.floor();
    if ((ts - secs) * 1e6).round_ties_even() >= 1e6 {
        secs += 1.0;
    }
    if !(-1e13..1e13).contains(&secs) {
        return None;
    }
    let local = chrono::Local.timestamp_opt(secs as i64, 0).earliest()?;
    (1..=9999)
        .contains(&local.year())
        .then(|| local.naive_local().format("%Y-%m-%dT%H:%M:%S").to_string())
}

/// `_truncate`: `t[:cap] + "…"` in code points, and whether it cut.
fn truncate(t: &str, cap: i64) -> (String, bool) {
    if cap <= 0 || t.chars().count() as i64 <= cap {
        return (t.to_string(), false);
    }
    let mut out: String = t.chars().take(cap as usize).collect();
    out.push('\u{2026}');
    (out, true)
}

/// `_replaced_by`: the pointer a superseded hit carries.
fn replaced_by(e: &Value) -> Value {
    let at = iso_seconds(e.get("superseded_at")).map(|s| s[..10].to_string());
    let text = e
        .get("superseded_by_text")
        .and_then(Value::as_str)
        .unwrap_or_default();
    json!({
        "id": get(e, "superseded_by_id"),
        "at": at,
        "preview": truncate(text, REPLACED_BY_PREVIEW_CHARS).0,
        "verified": truthy(e.get("supersession_verified")),
        "current": truthy(e.get("superseded_by_current")),
    })
}

/// `_compact_entry`.
pub(crate) fn compact_entry(e: &Value, text_chars: Option<i64>) -> Value {
    let mut out = Map::new();
    for k in ["id", "text", "source", "tags", "score"] {
        if let Some(v) = e.get(k) {
            out.insert(k.into(), v.clone());
        }
    }
    if let Some(written) = iso_seconds(e.get("timestamp")) {
        out.insert("date".into(), json!(written[..10]));
    }
    if truthy(e.get("superseded")) {
        out.insert("superseded".into(), json!(true));
    }
    if truthy(e.get("superseded_by_text")) {
        out.insert("replaced_by".into(), replaced_by(e));
    }
    for k in ["authority", "distortion_tolerance"] {
        if truthy(e.get(k)) {
            out.insert(k.into(), get(e, k));
        }
    }
    if let (Some(cap), Some(Value::String(text))) = (text_chars, out.get("text")) {
        let (text, cut) = truncate(text, cap);
        out.insert("text".into(), json!(text));
        if cut {
            out.insert("truncated".into(), json!(true));
        }
    }
    Value::Object(out)
}

/// Python `str.isalnum` for one character: a letter (L*) or a number
/// (N*). Rust's `is_alphanumeric` also counts Other_Alphabetic marks such as
/// Devanagari vowel signs, which Python does not.
fn py_isalnum(c: char) -> bool {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"^[\p{L}\p{N}]$").expect("static pattern"))
        .is_match(c.encode_utf8(&mut [0; 4]))
}

/// `_restates_fact`: an entry that only restates a served cortex value.
fn restates_fact(entry_text: &str, value: &str) -> bool {
    let norm = |s: &str| {
        s.to_lowercase()
            .split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
            .filter(|w| !w.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    };
    let (t, v) = (norm(entry_text), norm(value));
    let (tc, vc): (Vec<char>, Vec<char>) = (t.chars().collect(), v.chars().collect());
    if vc.len() < 5 || tc.is_empty() {
        return false;
    }
    let Some(byte) = t.find(&v) else {
        return false;
    };
    let i = t[..byte].chars().count();
    if (i > 0 && py_isalnum(tc[i - 1])) || (i + vc.len() < tc.len() && py_isalnum(tc[i + vc.len()]))
    {
        return false;
    }
    vc.len() as f64 >= 0.5 * tc.len() as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncation_counts_code_points() {
        assert_eq!(
            truncate("\u{e9}\u{e9}\u{e9}", 2),
            ("\u{e9}\u{e9}\u{2026}".into(), true)
        );
        assert_eq!(truncate("abc", 3), ("abc".into(), false));
        assert_eq!(truncate("abc", 0), ("abc".into(), false));
    }

    #[test]
    fn restated_values_need_a_bounded_dominant_match() {
        assert!(restates_fact("PostgreSQL 18", "postgresql 18"));
        assert!(!restates_fact("postgresql is the store", "postgres"));
        assert!(!restates_fact(
            "claude code is the client we use daily",
            "claude"
        ));
        assert!(!restates_fact(
            "the daemon runs on port 8765 behind caddy",
            "8765"
        ));
        assert!(restates_fact("  Port   8765 ", "port 8765"));
    }

    #[test]
    fn combining_marks_bound_a_restated_value_as_python_does() {
        // U+0940 is Mc, not alphanumeric to Python: the match is bounded.
        assert!(restates_fact(
            "\u{92d}\u{93e}\u{930}\u{924} \u{926}\u{947}\u{936}\u{940} \u{932}\u{94b}\u{917}",
            "\u{92d}\u{93e}\u{930}\u{924} \u{926}\u{947}\u{936}"
        ));
        assert!(py_isalnum('\u{2460}')); // circled digit one: No
        assert!(!py_isalnum('\u{940}'));
    }

    #[test]
    fn blank_query_projects_to_the_documented_shape() {
        let blank = json!({"entries": [], "query": "", "count": 0, "low_confidence": true});
        let Value::Object(m) = blank else {
            unreachable!()
        };
        let out = project(m, &[], true, Some(600)).unwrap();
        assert_eq!(
            out.to_string(),
            r#"{"entries":[],"query":"","count":0,"low_confidence":true,"cortex":[]}"#
        );
    }

    #[test]
    fn contested_facts_get_the_resolve_path_and_the_note() {
        let fact = json!({"entity": "pl", "attribute": "port", "value": "8765",
                          "contested": true, "contender_value": "8766"});
        let m = json!({"query": "q", "count": 1, "low_confidence": true,
                       "entries": [{"id": 1, "text": "8765", "score": 0.9}]});
        let Value::Object(m) = m else { unreachable!() };
        let out = project(m, &[fact], true, Some(600)).unwrap();
        let cw = out["cortex"][0]["correct_with"].as_str().unwrap();
        assert!(
            cw.starts_with("memory_fact_get(entity='pl', attribute='port')"),
            "{cw}"
        );
        assert_eq!(out["correction_note"], json!(CORRECTION_NOTE));
        assert_eq!(out["low_confidence"], json!(false));
        assert_eq!(out["cortex"][0]["contender_origin"], json!(""));
    }
}
