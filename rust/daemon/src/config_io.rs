//! The Console's config editor: `GET /api/config` (`web/config_io.py`
//! `read_config`, `_coerce`, `config_path_for`, `_saved_file`).
//!
//! The knob table is Python's `KNOBS`, generated into
//! `harness/goldens/config-knobs.json` (`gen_config_knobs.py`). A knob's
//! `value` is the running one: the value config.yaml set at load, as YAML
//! typed it (Python's dataclasses keep it uncoerced), else this daemon's
//! default for the setting. A restart knob whose saved value differs also
//! carries `saved`.

use std::path::Path;
use std::sync::OnceLock;

use serde_json::{Map, Value, json};

use crate::config::{Config, Node, node_json, safe_load};
use crate::pyjson::{float_repr, number_text, py_str};

const KNOBS_JSON: &str = include_str!("../harness/goldens/config-knobs.json");

/// One knob: its group and the metadata item `read_config` serves.
pub(crate) struct Knob {
    pub group: String,
    pub item: Map<String, Value>,
}

impl Knob {
    pub fn path(&self) -> &str {
        self.item["path"].as_str().expect("knob path")
    }

    fn kind(&self) -> &str {
        self.item["type"].as_str().expect("knob type")
    }

    fn restart(&self) -> bool {
        self.item.get("restart").and_then(Value::as_bool) == Some(true)
    }
}

pub(crate) fn knobs() -> &'static [Knob] {
    static KNOBS: OnceLock<Vec<Knob>> = OnceLock::new();
    KNOBS.get_or_init(|| {
        let table: Value = serde_json::from_str(KNOBS_JSON).expect("knob table");
        table["knobs"]
            .as_array()
            .expect("knob list")
            .iter()
            .map(|k| Knob {
                group: k["group"].as_str().expect("knob group").to_string(),
                item: k["item"].as_object().expect("knob item").clone(),
            })
            .collect()
    })
}

#[cfg(test)]
pub(crate) fn knob(path: &str) -> Option<&'static Knob> {
    knobs().iter().find(|k| k.path() == path)
}

// ── running values ─────────────────────────────────────────────────────────

/// The value a knob runs with: config.yaml's value when the file set it,
/// else this daemon's setting, as `_get_by_path` reads the dataclass.
pub(crate) fn running(cfg: &Config, knob: &Knob) -> Value {
    let path = knob.path();
    if let Some(v) = cfg.user_leaves.get(path)
        && !crate::mutants::active("config-read-typed-values")
    {
        return v.clone();
    }
    let v = setting(cfg, path).unwrap_or_else(|| panic!("no setting for knob {path}"));
    // An int knob some settings hold as a float (`retention_days`).
    match (knob.kind(), int_of(&v), v.as_f64()) {
        ("int", None, Some(f)) => json!(f as i64),
        _ => v,
    }
}

/// This daemon's setting under a knob's dotted path.
fn setting(cfg: &Config, path: &str) -> Option<Value> {
    let m = &cfg.memory;
    let d = &cfg.dream;
    let opt = |v: &Option<String>| v.as_deref().map_or(Value::Null, |s| json!(s));
    if let Some(field) = path.strip_prefix("memory.deep_dream.") {
        return m.deep_dream.as_dict.get(field).cloned();
    }
    Some(match path {
        "memory.surprise_threshold" => json!(m.surprise_threshold),
        "memory.search_confidence_floor" => json!(m.search_confidence_floor),
        "memory.top_k" => json!(m.top_k),
        "memory.hide_superseded" => json!(m.hide_superseded),
        "memory.reranker.enabled" => json!(m.reranker_enabled),
        "memory.reranker.fusion_weight" => json!(m.reranker.fusion_weight),
        "memory.reranker.top_n" => json!(m.reranker.top_n),
        "memory.reranker.skip_margin" => json!(m.reranker.skip_margin),
        "memory.bm25.enabled" => json!(m.bm25.enabled),
        "memory.bm25.weight" => json!(m.bm25.weight),
        "memory.bm25.cortex_enabled" => json!(m.bm25.cortex_enabled),
        "memory.retrieval_log.enabled" => json!(m.retrieval_log_enabled),
        "memory.retrieval_log.retention_days" => json!(m.retrieval_retention_days),
        "memory.retrieval_log.use_window_seconds" => json!(m.retrieval_log_use_window_seconds),
        "memory.mcp.compact_payloads" => json!(m.mcp.compact_payloads),
        "memory.mcp.entry_text_chars" => json!(m.mcp.entry_text_chars),
        "memory.cortex.search_first" => json!(m.cortex.search_first),
        "memory.cortex.guard_min_score" => json!(m.cortex.guard_min_score),
        "memory.cortex.protect_provenance" => json!(m.cortex.protect_provenance),
        "memory.cortex.supersede_confidence_margin" => {
            json!(m.cortex.supersede_confidence_margin)
        }
        "memory.cortex.auto_promote" => json!(m.cortex_auto_promote),
        "memory.cortex.dream_slot_match_threshold" => json!(m.cortex.dream_slot_match_threshold),
        "memory.cortex.pin_constraints" => json!(m.cortex.pin_constraints),
        "memory.dream.enabled" => json!(d.enabled),
        "memory.dream.min_batch" => json!(d.min_batch),
        "memory.dream.idle_seconds" => json!(d.idle_seconds),
        "memory.dream.max_batch" => json!(d.max_batch),
        "memory.dream.sweep_interval_seconds" => json!(d.sweep_interval_seconds),
        "memory.dream.extract_relations" => json!(d.extract_relations),
        "memory.dream.write_dedup_min_jaccard" => json!(d.write_dedup_min_jaccard),
        "memory.dream.alias_candidate_min_cosine" => json!(d.alias_candidate_min_cosine),
        "memory.dream.min_relation_confidence" => json!(d.min_relation_confidence),
        "memory.dream.relation_quarantine_below" => json!(d.relation_quarantine_below),
        "memory.dream.retype_quarantined_max" => json!(d.retype_quarantined_max),
        "memory.dream.literal_gate" => json!(d.literal_gate),
        "memory.dream.literal_gate_scope" => json!(d.literal_gate_scope),
        "memory.dream.span_gate" => json!(d.span_gate),
        "memory.dream.chronicle" => json!(d.chronicle),
        "memory.dream.digest_enabled" => json!(d.digest_enabled),
        "memory.dream.digest_target_chars" => json!(d.digest_target_chars),
        "memory.dream.digest_context_chars" => json!(d.digest_context_chars),
        "memory.dream.extractor_model_override" => opt(&d.extractor_model_override),
        "memory.dream.extractor_reasoning_effort" => opt(&d.extractor_reasoning_effort),
        "memory.dream.extractor_source" => json!(d.extractor_source),
        "memory.dream.extractor_base_url" => opt(&d.extractor_base_url),
        "memory.dream.extractor_model" => opt(&d.extractor_model),
        "memory.dream.extractor_timeout_seconds" => json!(d.extractor_timeout_seconds),
        "memory.dream.extractor_max_tokens" => json!(d.extractor_max_tokens),
        "memory.dream.extractor_mode" => json!(d.extractor_mode),
        "memory.dream.fallback_base_url" => opt(&d.fallback_base_url),
        "memory.dream.fallback_model" => opt(&d.fallback_model),
        "memory.dream.runs_keep" => json!(d.runs_keep),
        "memory.lessons.enabled" => json!(m.lessons_enabled),
        // No reader in this daemon (the binder never reads it over MCP):
        // `LessonsConfig.top_k`'s default.
        "memory.lessons.top_k" => json!(5),
        "memory.lessons.signal_retention_days" => json!(m.lessons_signal_retention_days),
        "memory.lessons.synthesize_in_dream" => json!(m.lessons_synthesize_in_dream),
        "memory.lessons.infer_outcomes" => json!(m.lessons_infer_outcomes),
        "memory.lessons.infer_outcomes_max_signals" => {
            json!(m.lessons_infer_outcomes_max_signals)
        }
        "memory.recall.driver" => json!(m.recall.driver),
        "memory.recall.default_hops" => json!(m.recall.default_hops),
        "memory.recall.default_top_k" => json!(m.recall.default_top_k),
        "memory.recall.max_searches_per_hop" => json!(m.recall.max_searches_per_hop),
        "memory.recall.max_total_searches" => json!(m.recall.max_total_searches),
        "memory.recall.time_budget_seconds" => json!(m.recall.time_budget_seconds),
        "memory.recall.skip_part_of_expansion" => json!(m.recall.skip_part_of_expansion),
        "memory.compaction.enabled" => json!(m.compaction_enabled),
        "memory.compaction.keep_per_slot" => json!(m.compaction_keep_per_slot),
        "memory.compaction.min_age_days" => json!(m.compaction_min_age_days),
        "time.relative_age" => json!(cfg.time.relative_age),
        _ => return None,
    })
}

// ── Python semantics ───────────────────────────────────────────────────────

/// Python `==` over decoded JSON: numbers and booleans by value
/// (`True == 1`, `1 == 1.0`), containers element by element.
pub(crate) fn py_eq(a: &Value, b: &Value) -> bool {
    fn num(v: &Value) -> Option<f64> {
        match v {
            Value::Bool(b) => Some(f64::from(u8::from(*b))),
            Value::Number(n) => n.as_f64(),
            _ => None,
        }
    }
    match (a, b) {
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, p)| y.get(k).is_some_and(|q| py_eq(p, q)))
        }
        _ => match (num(a), num(b)) {
            (Some(x), Some(y)) => match (int_of(a), int_of(b)) {
                (Some(i), Some(j)) => i == j,
                _ => x == y,
            },
            _ => a == b,
        },
    }
}

/// An integer-valued JSON int or boolean, exactly.
fn int_of(v: &Value) -> Option<i128> {
    match v {
        Value::Bool(b) => Some(i128::from(*b)),
        Value::Number(n) => {
            let text = number_text(n);
            if text.contains(['.', 'e', 'n', 'N', 'I']) {
                None
            } else {
                text.parse().ok()
            }
        }
        _ => None,
    }
}

/// Python's type name for a value `int()` / `float()` refuse.
fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
        Value::String(_) => "str",
        Value::Bool(_) => "bool",
        Value::Number(_) => "float",
    }
}

/// Why `_coerce` raised, with the exception's text. The POST route
/// answers a `ValueError` 400 and the others 500; `read_config` catches
/// `TypeError` and `ValueError` but not `OverflowError`.
#[derive(Debug, PartialEq)]
pub(crate) enum Refused {
    Value(String),
    Type(String),
    Overflow(String),
}

/// A number as `_coerce` compares it with a knob bound.
#[derive(Clone, Copy)]
enum Num {
    Int(i128),
    Float(f64),
}

impl Num {
    fn lt(self, other: Num) -> bool {
        match (self, other) {
            (Num::Int(a), Num::Int(b)) => a < b,
            (a, b) => a.f() < b.f(),
        }
    }

    fn f(self) -> f64 {
        match self {
            Num::Int(i) => i as f64,
            Num::Float(f) => f,
        }
    }

    /// `str(v)`.
    fn text(self) -> String {
        match self {
            Num::Int(i) => i.to_string(),
            Num::Float(f) => float_repr(f),
        }
    }

    fn json(self) -> Value {
        match self {
            Num::Int(i) => serde_json::from_str(&i.to_string()).expect("integer"),
            Num::Float(f) => json!(f),
        }
    }
}

fn bound(v: &Value) -> Num {
    match int_of(v) {
        Some(i) => Num::Int(i),
        None => Num::Float(v.as_f64().expect("numeric knob bound")),
    }
}

/// A JSON number written as an integer, which `json.loads` makes an `int`
/// however long it is.
fn is_int_literal(n: &serde_json::Number) -> bool {
    !n.to_string().contains(['.', 'e', 'E'])
}

/// A JSON number as Python's `json.loads` holds it.
fn json_num(n: &serde_json::Number) -> Num {
    let v = Value::Number(n.clone());
    match int_of(&v) {
        Some(i) => Num::Int(i),
        None => Num::Float(
            n.to_string()
                .parse::<f64>()
                .expect("a JSON number parses as f64"),
        ),
    }
}

/// `_coerce(knob, value)`.
pub(crate) fn coerce(knob: &Knob, value: &Value) -> Result<Value, Refused> {
    let path = knob.path();
    let v = match knob.kind() {
        "bool" => {
            return Ok(Value::Bool(match value {
                Value::Bool(b) => *b,
                Value::String(s) => {
                    matches!(py_lower(py_strip(s)).as_str(), "1" | "true" | "yes" | "on")
                }
                other => truthy(other),
            }));
        }
        "int" => Num::Int(py_int(value)?),
        "float" => {
            let f = py_float(value)?;
            if !f.is_finite() {
                return Err(Refused::Value(format!("{path}: must be finite")));
            }
            Num::Float(f)
        }
        "enum" => {
            let v = py_str(value);
            let options: Vec<&str> = knob.item["options"]
                .as_array()
                .expect("enum options")
                .iter()
                .map(|o| o.as_str().expect("enum option"))
                .collect();
            if !options.contains(&v.as_str()) {
                let listed: Vec<String> =
                    options.iter().map(|o| crate::storage::py_repr(o)).collect();
                return Err(Refused::Value(format!(
                    "{path}: {} not in [{}]",
                    crate::storage::py_repr(&v),
                    listed.join(", ")
                )));
            }
            return Ok(json!(v));
        }
        _ => {
            if value.is_null() {
                return Ok(Value::Null);
            }
            let text = py_str(value);
            let v = py_strip(&text);
            if v.is_empty() {
                return Ok(Value::Null);
            }
            if knob.item.get("format").and_then(Value::as_str) == Some("url") {
                let lower = py_lower(v);
                if !(lower.starts_with("http://") || lower.starts_with("https://")) {
                    return Err(Refused::Value(format!("{path}: must be an http(s) URL")));
                }
            }
            return Ok(json!(v));
        }
    };
    if let Some(lo) = knob.item.get("min").map(bound)
        && v.lt(lo)
    {
        return Err(Refused::Value(format!(
            "{path}: {} < min {}",
            v.text(),
            lo.text()
        )));
    }
    if let Some(hi) = knob.item.get("max").map(bound)
        && hi.lt(v)
    {
        return Err(Refused::Value(format!(
            "{path}: {} > max {}",
            v.text(),
            hi.text()
        )));
    }
    Ok(v.json())
}

/// Python `int(value)` on a decoded JSON value.
fn py_int(value: &Value) -> Result<i128, Refused> {
    match value {
        Value::Bool(b) => Ok(i128::from(*b)),
        Value::Number(n) => match json_num(n) {
            Num::Int(i) => Ok(i),
            Num::Float(f) if f.is_nan() => {
                Err(Refused::Value("cannot convert float NaN to integer".into()))
            }
            Num::Float(f) if f.is_infinite() => Err(Refused::Overflow(
                "cannot convert float infinity to integer".into(),
            )),
            Num::Float(f) => Ok(f.trunc() as i128),
        },
        Value::String(s) => crate::dream::extractor::py_int_str(s).ok_or_else(|| {
            Refused::Value(format!(
                "invalid literal for int() with base 10: {}",
                crate::storage::py_repr(s)
            ))
        }),
        other => Err(Refused::Type(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            type_name(other)
        ))),
    }
}

/// Python `float(value)` on a decoded JSON value.
fn py_float(value: &Value) -> Result<f64, Refused> {
    match value {
        Value::Bool(b) => Ok(f64::from(u8::from(*b))),
        // `float(int)` past the float range.
        Value::Number(n) if is_int_literal(n) && json_num(n).f().is_infinite() => Err(
            Refused::Overflow("int too large to convert to float".into()),
        ),
        Value::Number(n) => Ok(json_num(n).f()),
        Value::String(s) => crate::dream::extractor::py_float_str(s).ok_or_else(|| {
            Refused::Value(format!(
                "could not convert string to float: {}",
                crate::storage::py_repr(s)
            ))
        }),
        other => Err(Refused::Type(format!(
            "float() argument must be a string or a real number, not '{}'",
            type_name(other)
        ))),
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => json_num(n).f() != 0.0,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str.isspace` set: Rust's `White_Space` plus U+001C..U+001F.
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

fn py_lower(s: &str) -> String {
    s.to_lowercase()
}

// ── read_config ────────────────────────────────────────────────────────────

/// The process environment, as the routes read it.
pub(crate) type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

pub(crate) fn process_env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

/// `config_path_for`: `PSEUDOLIFE_MCP_CONFIG` when set and non-empty, else
/// `<data_dir>/config.yaml`, as `str(Path(...))` prints it.
pub(crate) fn config_path_for(env: Env, data_dir: &Path) -> String {
    match env("PSEUDOLIFE_MCP_CONFIG") {
        Some(path) if !path.is_empty() => crate::documents::py_path(&path).shown,
        _ => {
            let dir = crate::documents::py_path(&data_dir.to_string_lossy()).shown;
            let joined = if dir == "." {
                "config.yaml".to_string()
            } else {
                format!("{dir}{}config.yaml", std::path::MAIN_SEPARATOR)
            };
            crate::documents::py_path(&joined).shown
        }
    }
}

/// `_saved_file`: config.yaml as last written, or an empty mapping when it
/// is absent, unreadable or not a mapping.
fn saved_file(path: &str) -> Vec<(Node, Node)> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    match safe_load(&text) {
        Ok(Some(Node::Map(entries))) => entries,
        _ => Vec::new(),
    }
}

/// `_nested_get`.
fn nested_get<'a>(entries: &'a [(Node, Node)], path: &str) -> Option<&'a Node> {
    let mut cur: Option<&Node> = None;
    let mut level = entries;
    for part in path.split('.') {
        let found = level.iter().find_map(|(k, v)| match k {
            Node::Str(s) if s == part => Some(v),
            _ => None,
        })?;
        cur = Some(found);
        level = match found {
            Node::Map(inner) => inner,
            _ => &[],
        };
    }
    cur
}

/// A loaded YAML value as `_coerce` receives it; infinities stay numbers.
fn node_value(node: &Node) -> Value {
    match node {
        Node::Float(f) if f.is_infinite() => {
            serde_json::from_str(if *f > 0.0 { "1e400" } else { "-1e400" }).expect("number")
        }
        other => node_json(other),
    }
}

/// `read_config`: every knob with its running value, grouped in first-seen
/// order. `Err` carries the text of an exception `read_config` lets escape
/// (an infinite saved value for an int knob), which the route answers 500.
pub(crate) fn read_config(cfg: &Config, env: Env, data_dir: &Path) -> Result<Value, String> {
    let config_path = config_path_for(env, data_dir);
    let saved = saved_file(&config_path);
    let mut groups: Vec<(String, Vec<Value>)> = Vec::new();
    for knob in knobs() {
        let current = running(cfg, knob);
        let mut item = knob.item.clone();
        item.insert("value".into(), current.clone());
        if knob.restart() && !crate::mutants::active("config-read-no-saved") {
            let shown = match nested_get(&saved, knob.path()) {
                None => None,
                Some(node) => match coerce(knob, &node_value(node)) {
                    Ok(v) => Some(v),
                    Err(Refused::Value(_) | Refused::Type(_)) => None,
                    Err(Refused::Overflow(text)) => return Err(text),
                },
            };
            if let Some(v) = shown
                && !py_eq(&v, &current)
            {
                item.insert("saved".into(), v);
            }
        }
        match groups.iter_mut().find(|(name, _)| *name == knob.group) {
            Some((_, list)) => list.push(Value::Object(item)),
            None => groups.push((knob.group.clone(), vec![Value::Object(item)])),
        }
    }
    Ok(json!({
        "config_path": config_path,
        "groups": groups
            .into_iter()
            .map(|(name, knobs)| json!({"name": name, "knobs": knobs}))
            .collect::<Vec<_>>(),
    }))
}

#[cfg(test)]
#[path = "config_io_tests.rs"]
mod tests;
