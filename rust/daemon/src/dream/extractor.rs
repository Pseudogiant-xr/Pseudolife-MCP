//! The dream extractor client (`memory/dream.py`): endpoint resolution, the
//! extractor builders, the endpoint probes, and the OpenAI-compatible
//! `/chat/completions` calls with their reply parsing.
//!
//! The request body is Python's `json.dumps` byte for byte
//! (`crate::pyjson::dumps`): the stub-extractor differential compares the
//! two daemons' outbound bodies exactly. Reply parsing follows Python's
//! coercions: `str()` of any JSON value, `float()` / `int()` with their
//! error classes, `strip()`, and the code-point caps.
//!
//! Python raises an `ExtractorError` from inside each method's `try`, and
//! a few coercions sit outside it (`int(source)` of an infinity, `float()`
//! of an integer too large for a float), which escape as their own
//! exception. [`ExtractFailure::Uncaught`] carries those.
//!
//! The outcome-inference, lesson and digest calls (`infer_outcomes`,
//! `extract_lessons` / `extract_rules`, `summarize_session`) land with the
//! dream run: each is one more `Compat` method over [`Compat::completion`].

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{Map, Value, json};

use super::prompts;
use crate::config::DreamConfig;
use crate::pyjson;

/// `_EXTRACTOR_MODES` (`memory/dream.py:1996`).
pub const EXTRACTOR_MODES: [&str; 3] = ["auto", "primary", "fallback"];
/// `_probe_retry_delay` (`memory/dream.py:2127`).
pub const PROBE_RETRY_DELAY: f64 = 2.0;
/// `_ALIAS_MODEL_NAMES` (`memory/dream.py:2134`).
pub const ALIAS_MODEL_NAMES: [&str; 2] = ["extractor", "bench"];
/// `_SERVED_MODEL_TTL` (`memory/dream.py:2140`).
const SERVED_MODEL_TTL: f64 = 300.0;
/// `_AUTH_BODY_PEEK` (`memory/dream.py:1211`).
const AUTH_BODY_PEEK: usize = 4096;
/// The only two speaker labels a claim may carry (`_SPEAKERS`).
const SPEAKERS: [&str; 2] = ["user", "assistant"];

// ---------------------------------------------------------------------------
// Python value coercions
// ---------------------------------------------------------------------------

/// An exception Python lets escape the method (not an `ExtractorError`).
#[derive(Debug, Clone, PartialEq)]
pub struct Uncaught {
    pub type_name: &'static str,
    pub message: String,
}

fn overflow(message: &str) -> Uncaught {
    Uncaught {
        type_name: "OverflowError",
        message: message.to_string(),
    }
}

/// A JSON number as Python's `json.loads` holds it.
enum PyNum {
    Int(String),
    Float(f64),
}

fn py_num(n: &serde_json::Number) -> PyNum {
    let raw = n.to_string();
    if raw == "NaN" {
        return PyNum::Float(f64::NAN);
    }
    if raw.contains(['.', 'e', 'E']) {
        PyNum::Float(raw.parse().unwrap_or(f64::NAN))
    } else {
        PyNum::Int(pyjson::number_text(n))
    }
}

/// `str(x)` for a float: `inf`, `-inf`, `nan`, else `repr`.
fn float_str(x: f64) -> String {
    if x.is_nan() {
        "nan".into()
    } else if x.is_infinite() {
        if x > 0.0 { "inf".into() } else { "-inf".into() }
    } else {
        pyjson::float_repr(x)
    }
}

fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match py_num(n) {
            PyNum::Int(text) => text,
            PyNum::Float(x) => float_str(x),
        },
        Value::String(s) => crate::storage::py_repr(s),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", crate::storage::py_repr(k), py_repr(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// Python `str(value)` for a value `json.loads` produced.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => match py_num(n) {
            PyNum::Int(text) => text != "0",
            PyNum::Float(x) => x != 0.0,
        },
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

/// The digits of a Python numeric literal with PEP 515 underscores (one
/// between two digits) removed, or None when an underscore is misplaced.
fn drop_underscores(s: &str) -> Option<String> {
    if !s.contains('_') {
        return Some(s.to_string());
    }
    let chars: Vec<char> = s.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_' {
            let before = i.checked_sub(1).and_then(|j| chars.get(j));
            let after = chars.get(i + 1);
            if !before.is_some_and(char::is_ascii_digit) || !after.is_some_and(char::is_ascii_digit)
            {
                return None;
            }
        }
    }
    Some(s.replace('_', ""))
}

/// Python `float(text)` for a string: None where it raises ValueError.
pub fn py_float_str(text: &str) -> Option<f64> {
    let s = drop_underscores(crate::storage::py_strip(text))?;
    let body = s.trim_start_matches(['+', '-']);
    if s.len() - body.len() > 1 {
        return None;
    }
    let lower = body.to_ascii_lowercase();
    if matches!(lower.as_str(), "inf" | "infinity" | "nan") {
        return s.parse().ok();
    }
    // Rust accepts exactly the decimal grammar Python does, minus the
    // special words handled above; refuse anything else Rust might take.
    if body.is_empty()
        || !body
            .chars()
            .all(|c| c.is_ascii_digit() || matches!(c, '.' | 'e' | 'E' | '+' | '-'))
    {
        return None;
    }
    s.parse().ok()
}

/// Python `int(text)` for a string (base 10): None where it raises.
/// Saturates at the `i128` range, far past any batch index.
pub fn py_int_str(text: &str) -> Option<i128> {
    let s = drop_underscores(crate::storage::py_strip(text))?;
    let (neg, digits) = match s.strip_prefix('-') {
        Some(d) => (true, d),
        None => (false, s.strip_prefix('+').unwrap_or(&s)),
    };
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let value = digits.parse::<i128>().unwrap_or(i128::MAX);
    Some(if neg { -value } else { value })
}

/// Python `float(v)`: `Ok(None)` where it raises TypeError/ValueError.
fn py_float(v: &Value) -> Result<Option<f64>, Uncaught> {
    match v {
        Value::Bool(b) => Ok(Some(if *b { 1.0 } else { 0.0 })),
        Value::Number(n) => match py_num(n) {
            PyNum::Float(x) => Ok(Some(x)),
            PyNum::Int(text) => {
                let x: f64 = text.parse().unwrap_or(f64::INFINITY);
                if x.is_infinite() {
                    Err(overflow("int too large to convert to float"))
                } else {
                    Ok(Some(x))
                }
            }
        },
        Value::String(s) => Ok(py_float_str(s)),
        _ => Ok(None),
    }
}

/// Python `int(v)`: `Ok(None)` where it raises TypeError/ValueError.
fn py_int(v: &Value) -> Result<Option<i128>, Uncaught> {
    match v {
        Value::Bool(b) => Ok(Some(i128::from(*b))),
        Value::Number(n) => match py_num(n) {
            PyNum::Int(text) => Ok(Some(text.parse::<i128>().unwrap_or(
                if text.starts_with('-') {
                    i128::MIN
                } else {
                    i128::MAX
                },
            ))),
            PyNum::Float(x) if x.is_nan() => Ok(None),
            PyNum::Float(x) if x.is_infinite() => {
                Err(overflow("cannot convert float infinity to integer"))
            }
            PyNum::Float(x) => Ok(Some(x.trunc() as i128)),
        },
        Value::String(s) => Ok(py_int_str(s)),
        _ => Ok(None),
    }
}

/// `max(0.0, min(1.0, x))` with Python's argument order (NaN lands on 1.0).
fn clamp01(x: f64) -> f64 {
    let low = if x < 1.0 { x } else { 1.0 };
    if low > 0.0 { low } else { 0.0 }
}

/// `max(0.0, min(1.0, float(v.get(key, default))))`, the default on a
/// TypeError/ValueError.
fn confidence(obj: &Map<String, Value>, default: f64) -> Result<f64, Uncaught> {
    let x = match obj.get("confidence") {
        None => Some(default),
        Some(v) => py_float(v)?,
    };
    Ok(x.map(clamp01).unwrap_or(default))
}

/// `int(c.get("source")) - 1`, a bool refused, kept only inside `0..n`.
fn source_index(obj: &Map<String, Value>, n: usize) -> Result<Option<usize>, Uncaught> {
    let raw = obj.get("source").unwrap_or(&Value::Null);
    let idx = match py_int(raw)? {
        Some(i) if !raw.is_boolean() => i.saturating_sub(1),
        _ => -1,
    };
    Ok((0..n as i128).contains(&idx).then_some(idx as usize))
}

/// `str(obj.get(key, "")).strip()`.
fn field(obj: &Map<String, Value>, key: &str) -> String {
    match obj.get(key) {
        None => String::new(),
        Some(v) => crate::storage::py_strip(&py_str(v)).to_string(),
    }
}

fn cap(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

// ---------------------------------------------------------------------------
// Parsed records
// ---------------------------------------------------------------------------

/// `Claim` (`memory/dream.py:23-69`), with its optional keys.
#[derive(Debug, Clone, PartialEq)]
pub struct Claim {
    pub entity: String,
    pub attribute: String,
    pub value: String,
    pub confidence: f64,
    pub origin: String,
    pub value_coerced: bool,
    pub op: Option<String>,
    pub stance: Option<String>,
    pub quote: Option<String>,
    pub speaker: Option<String>,
    pub source: Option<usize>,
}

impl Claim {
    /// The dict Python builds, in its key order.
    pub fn to_json(&self) -> Value {
        let mut m = Map::new();
        m.insert("entity".into(), json!(self.entity));
        m.insert("attribute".into(), json!(self.attribute));
        m.insert("value".into(), json!(self.value));
        m.insert("confidence".into(), json!(self.confidence));
        m.insert("origin".into(), json!(self.origin));
        if self.value_coerced {
            m.insert("value_coerced".into(), json!(true));
        }
        for (key, v) in [
            ("op", &self.op),
            ("stance", &self.stance),
            ("quote", &self.quote),
            ("speaker", &self.speaker),
        ] {
            if let Some(v) = v {
                m.insert(key.into(), json!(v));
            }
        }
        if let Some(s) = self.source {
            m.insert("source".into(), json!(s));
        }
        Value::Object(m)
    }
}

/// A chronicle event (`events_from_parsed`, `memory/dream.py:669-716`).
#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub description: String,
    pub actor: String,
    pub date: Option<String>,
    pub date_phrase: Option<String>,
    pub source: Option<usize>,
}

impl Event {
    pub fn to_json(&self) -> Value {
        let mut m = Map::new();
        m.insert("kind".into(), json!("event"));
        m.insert("description".into(), json!(self.description));
        m.insert("actor".into(), json!(self.actor));
        m.insert("date".into(), json!(self.date));
        m.insert("date_phrase".into(), json!(self.date_phrase));
        if let Some(s) = self.source {
            m.insert("source".into(), json!(s));
        }
        Value::Object(m)
    }
}

/// One item `extract` returns: claims first, then the events riding the
/// same call (`claims + events`).
#[derive(Debug, Clone, PartialEq)]
pub enum Extracted {
    Claim(Claim),
    Event(Event),
}

impl Extracted {
    pub fn to_json(&self) -> Value {
        match self {
            Extracted::Claim(c) => c.to_json(),
            Extracted::Event(e) => e.to_json(),
        }
    }
}

/// `RelationClaim` (`memory/dream.py:93-97`).
#[derive(Debug, Clone, PartialEq)]
pub struct RelationClaim {
    pub src: String,
    pub relation: String,
    pub dst: String,
    pub confidence: f64,
}

impl RelationClaim {
    pub fn to_json(&self) -> Value {
        json!({"src": self.src, "relation": self.relation, "dst": self.dst,
               "confidence": self.confidence})
    }
}

/// `datetime.strptime(date, "%Y-%m-%d")` succeeds. `_strptime` matches
/// `\d` as any Unicode digit; a non-ASCII digit refuses here (declared).
fn strptime_ymd(s: &str) -> bool {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| {
        regex::Regex::new(
            r"^([0-9]{4})-(1[0-2]|0[1-9]|[1-9])-(3[01]|[12][0-9]|0[1-9]|[1-9]| [1-9])$",
        )
        .expect("date pattern")
    });
    let Some(c) = re.captures(s) else {
        return false;
    };
    let num = |i: usize| c[i].trim().parse::<u32>().unwrap_or(0);
    let (y, m, d) = (num(1), num(2), num(3));
    if y == 0 {
        return false;
    }
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let days = match m {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    d <= days
}

/// `events_from_parsed` (`memory/dream.py:669-716`).
pub fn events_from_parsed(parsed: &Value, n_texts: usize) -> Result<Vec<Event>, Uncaught> {
    let raw = match parsed {
        Value::Object(m) => m.get("events").cloned().unwrap_or(Value::Array(vec![])),
        _ => Value::Array(vec![]),
    };
    let mut out = Vec::new();
    let Value::Array(items) = raw else {
        return Ok(out);
    };
    for ev in &items {
        let Value::Object(ev) = ev else { continue };
        let description = field(ev, "description");
        if description.is_empty() {
            continue;
        }
        let date = match ev.get("date") {
            None | Some(Value::Null) => None,
            Some(v) => {
                let d = crate::storage::py_strip(&py_str(v)).to_string();
                strptime_ymd(&d).then_some(d)
            }
        };
        let date_phrase = match ev.get("date_phrase") {
            None | Some(Value::Null) => None,
            Some(v) => {
                Some(crate::storage::py_strip(&py_str(v)).to_string()).filter(|s| !s.is_empty())
            }
        };
        let actor = match ev.get("actor") {
            Some(v) if truthy(v) => crate::storage::py_strip(&py_str(v)).to_string(),
            _ => "user".to_string(),
        };
        let actor = if actor.is_empty() {
            "user".into()
        } else {
            actor
        };
        out.push(Event {
            description,
            actor,
            date,
            date_phrase,
            source: source_index(ev, n_texts)?,
        });
    }
    Ok(out)
}

/// The claim loop of `OpenAICompatExtractor.extract` (`memory/dream.py:1347-1396`).
pub fn parse_claims(raw: &Value, n_texts: usize) -> Result<Vec<Claim>, Uncaught> {
    let mut out = Vec::new();
    let Value::Array(items) = raw else {
        return Ok(out);
    };
    for c in items {
        let Value::Object(c) = c else { continue };
        let raw_value = match c.get("value") {
            None | Some(Value::Null) => continue,
            Some(v) => v,
        };
        let entity = field(c, "entity");
        let attribute = field(c, "attribute");
        let value = crate::storage::py_strip(&py_str(raw_value)).to_string();
        if entity.is_empty() || attribute.is_empty() || value.is_empty() {
            continue;
        }
        let confidence = confidence(c, 0.7)?;
        let op = match c.get("op") {
            Some(Value::String(op)) if op == "add" || op == "remove" => Some(op.clone()),
            _ => None,
        };
        let capped = |key: &str, n: usize| match c.get(key) {
            Some(Value::String(s)) if !crate::storage::py_strip(s).is_empty() => {
                Some(cap(crate::storage::py_strip(s), n))
            }
            _ => None,
        };
        let speaker = match c.get("speaker") {
            Some(Value::String(s)) => {
                let who = crate::storage::py_strip(s).to_lowercase();
                SPEAKERS.contains(&who.as_str()).then_some(who)
            }
            _ => None,
        };
        let stance = capped("stance", 48);
        let quote = capped("quote", 200);
        out.push(Claim {
            entity,
            attribute,
            value,
            confidence,
            origin: "agent".into(),
            value_coerced: !raw_value.is_string(),
            op,
            stance,
            quote,
            speaker,
            source: source_index(c, n_texts)?,
        });
    }
    Ok(out)
}

/// The relation loop of `extract_relations` (`memory/dream.py:1644-1659`).
pub fn parse_relations(raw: &Value) -> Result<Vec<RelationClaim>, Uncaught> {
    let mut out = Vec::new();
    let Value::Array(items) = raw else {
        return Ok(out);
    };
    for r in items {
        let Value::Object(r) = r else { continue };
        let (src, relation, dst) = (field(r, "src"), field(r, "relation"), field(r, "dst"));
        if src.is_empty() || relation.is_empty() || dst.is_empty() {
            continue;
        }
        let confidence = confidence(r, 0.6)?;
        out.push(RelationClaim {
            src,
            relation,
            dst,
            confidence,
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

/// `_vocab_hint` (`memory/dream.py:619-631`).
pub fn vocab_hint(vocab: &[String]) -> String {
    if vocab.is_empty() {
        return String::new();
    }
    let keys: Vec<&str> = vocab.iter().take(60).map(String::as_str).collect();
    format!("{}{}", prompts::VOCAB_HINT_HEAD, keys.join(", "))
}

/// `_facts_hint` (`memory/dream.py:890-894`).
pub fn facts_hint(known: Option<&[(String, String, String)]>) -> String {
    match known {
        Some(facts) if !facts.is_empty() => {
            let lines: Vec<String> = facts
                .iter()
                .map(|(e, a, v)| format!("- {e} \u{2014} {a}: {v}"))
                .collect();
            format!("{}{}", prompts::FACTS_HINT_HEAD, lines.join("\n"))
        }
        _ => String::new(),
    }
}

/// `_relations_prompt` (`memory/dream.py:1067-1069`).
pub fn relations_prompt(relations: &[(String, String)]) -> String {
    let body: Vec<String> = relations
        .iter()
        .map(|(n, d)| format!("- {n}: {d}"))
        .collect();
    format!(
        "{}{}{}",
        prompts::RELATIONS_PROMPT_HEAD,
        body.join("\n"),
        prompts::RELATIONS_PROMPT_TAIL
    )
}

/// The numbered user message: `"\n\n".join(f"[{i + 1}] {t}" ...)`.
fn numbered(texts: &[&str]) -> String {
    texts
        .iter()
        .enumerate()
        .map(|(i, t)| format!("[{}] {t}", i + 1))
        .collect::<Vec<_>>()
        .join("\n\n")
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// What an `ExtractorError` was raised from: the parts of its exception
/// chain `classify_extractor_error` reads.
#[derive(Debug, Clone, PartialEq)]
pub enum Cause {
    /// `urllib.error.HTTPError` (a non-2xx answer or a refused redirect).
    Http(u16),
    /// `socket.timeout` / `TimeoutError`.
    Timeout,
    /// `ConnectionRefusedError`.
    Refused,
    /// Any other `URLError` / `OSError`, by the inner exception's type name.
    Unreachable(String),
    /// Anything else, by the innermost exception's type name.
    Other(String),
}

/// `ExtractorError` (`memory/dream.py:1189-1199`).
#[derive(Debug, Clone, PartialEq)]
pub struct ExtractorError {
    /// `str(exc)`: `"<label> failed: <cause>"`. Best effort on the cause
    /// text; it never decides anything.
    pub message: String,
    pub auth_failure: bool,
    pub cause: Cause,
}

impl std::fmt::Display for ExtractorError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

fn auth_words() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(
            r"(?i)\b(?:oauth|session expired|not logged in|log ?in|unauthori[sz]ed|401)\b",
        )
        .expect("auth words")
    })
}

impl ExtractorError {
    /// `classify_extractor_error` (`memory/dream.py:2278-2308`):
    /// `(reason, error)` for the dream-stall record.
    pub fn classify(&self) -> (&'static str, String) {
        let auth = self.auth_failure || auth_words().is_match(&self.message);
        let failed = if auth {
            "login_expired"
        } else {
            "extractor_error"
        };
        match &self.cause {
            Cause::Http(code) => (failed, format!("HTTP {code}")),
            Cause::Timeout => ("extractor_unreachable", "timed out".into()),
            Cause::Refused => ("extractor_unreachable", "connection refused".into()),
            Cause::Unreachable(name) => ("extractor_unreachable", format!("unreachable ({name})")),
            Cause::Other(name) => (failed, name.clone()),
        }
    }
}

/// `classify_extractor_error` for any failure a method returns: an
/// `ExtractorError` by its chain, an escaped exception by its type name.
pub fn classify_failure(e: &ExtractFailure) -> (&'static str, String) {
    match e {
        ExtractFailure::Extractor(x) => x.classify(),
        ExtractFailure::Uncaught(u) => {
            let auth = auth_words().is_match(&u.message);
            (
                if auth {
                    "login_expired"
                } else {
                    "extractor_error"
                },
                u.type_name.to_string(),
            )
        }
    }
}

/// A method's failure: Python's `ExtractorError`, or an exception raised
/// outside the method's `try`.
#[derive(Debug, Clone, PartialEq)]
pub enum ExtractFailure {
    Extractor(ExtractorError),
    Uncaught(Uncaught),
}

impl From<ExtractorError> for ExtractFailure {
    fn from(e: ExtractorError) -> Self {
        ExtractFailure::Extractor(e)
    }
}

impl From<Uncaught> for ExtractFailure {
    fn from(e: Uncaught) -> Self {
        ExtractFailure::Uncaught(e)
    }
}

/// A failure inside the request's `try`, before it is labelled.
struct Raised {
    text: String,
    auth: bool,
    cause: Cause,
}

impl Raised {
    fn other(type_name: &str, text: impl Into<String>) -> Self {
        Raised {
            text: text.into(),
            auth: false,
            cause: Cause::Other(type_name.into()),
        }
    }

    fn label(self, label: &str, keep_auth: bool) -> ExtractorError {
        ExtractorError {
            message: format!("{label} failed: {}", self.text),
            // Only `extract` copies the HTTP auth hint onto its error.
            auth_failure: keep_auth && self.auth,
            cause: self.cause,
        }
    }
}

/// `json.loads(text)` with the exception class Python's chain ends in:
/// the C scanner's "Expecting value" leaves a `StopIteration` context.
fn loads(text: &str) -> Result<Value, Raised> {
    if let Some(v) = crate::mcp::py_loads::loads(text) {
        return Ok(v);
    }
    Err(if json_error_expects_value(text) {
        Raised::other("StopIteration", "Expecting value")
    } else {
        Raised::other("JSONDecodeError", "invalid JSON")
    })
}

/// Whether CPython's C scanner fails `text` with "Expecting value" (a
/// value position, top-level or nested, that starts no value: its
/// `StopIteration` reaches `raw_decode`, which leaves it as the context)
/// rather than another `JSONDecodeError` (a delimiter, a property name, a
/// string, extra data or a BOM). Only called once the parse has failed.
fn json_error_expects_value(text: &str) -> bool {
    #[derive(PartialEq)]
    enum Fail {
        Value,
        Other,
    }
    fn ws(s: &[char], mut i: usize) -> usize {
        while i < s.len() && matches!(s[i], ' ' | '\t' | '\n' | '\r') {
            i += 1;
        }
        i
    }
    fn lit(s: &[char], i: usize, word: &str) -> bool {
        let w: Vec<char> = word.chars().collect();
        s.len() >= i + w.len() && s[i..i + w.len()] == w[..]
    }
    fn string(s: &[char], mut i: usize) -> Result<usize, Fail> {
        i += 1;
        while i < s.len() {
            match s[i] {
                '"' => return Ok(i + 1),
                '\\' => {
                    let Some(&e) = s.get(i + 1) else {
                        return Err(Fail::Other);
                    };
                    if e == 'u' {
                        if s.len() < i + 6 || !s[i + 2..i + 6].iter().all(char::is_ascii_hexdigit) {
                            return Err(Fail::Other);
                        }
                        i += 6;
                    } else if matches!(e, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't') {
                        i += 2;
                    } else {
                        return Err(Fail::Other);
                    }
                }
                c if (c as u32) < 0x20 => return Err(Fail::Other),
                _ => i += 1,
            }
        }
        Err(Fail::Other)
    }
    fn number(s: &[char], i: usize) -> Option<usize> {
        let digit = |k: usize| s.get(k).is_some_and(char::is_ascii_digit);
        let mut k = i;
        if s.get(k) == Some(&'-') {
            k += 1;
        }
        if s.get(k) == Some(&'0') {
            k += 1;
        } else if digit(k) {
            while digit(k) {
                k += 1;
            }
        } else {
            return None;
        }
        if s.get(k) == Some(&'.') && digit(k + 1) {
            k += 1;
            while digit(k) {
                k += 1;
            }
        }
        if matches!(s.get(k), Some('e' | 'E')) {
            let mut e = k + 1;
            if matches!(s.get(e), Some('+' | '-')) {
                e += 1;
            }
            if digit(e) {
                while digit(e) {
                    e += 1;
                }
                k = e;
            }
        }
        Some(k)
    }
    fn value(s: &[char], i: usize) -> Result<usize, Fail> {
        let Some(&c) = s.get(i) else {
            return Err(Fail::Value);
        };
        match c {
            '"' => string(s, i),
            '{' => {
                let mut i = ws(s, i + 1);
                if s.get(i) == Some(&'}') {
                    return Ok(i + 1);
                }
                loop {
                    if s.get(i) != Some(&'"') {
                        return Err(Fail::Other);
                    }
                    i = ws(s, string(s, i)?);
                    if s.get(i) != Some(&':') {
                        return Err(Fail::Other);
                    }
                    i = ws(s, value(s, ws(s, i + 1))?);
                    match s.get(i) {
                        Some('}') => return Ok(i + 1),
                        Some(',') => i = ws(s, i + 1),
                        _ => return Err(Fail::Other),
                    }
                }
            }
            '[' => {
                let mut i = ws(s, i + 1);
                if s.get(i) == Some(&']') {
                    return Ok(i + 1);
                }
                loop {
                    i = ws(s, value(s, i)?);
                    match s.get(i) {
                        Some(']') => return Ok(i + 1),
                        Some(',') => i = ws(s, i + 1),
                        _ => return Err(Fail::Other),
                    }
                }
            }
            _ => {
                for word in ["null", "true", "false", "NaN", "Infinity", "-Infinity"] {
                    if lit(s, i, word) {
                        return Ok(i + word.chars().count());
                    }
                }
                number(s, i).ok_or(Fail::Value)
            }
        }
    }
    let s: Vec<char> = text.chars().collect();
    if s.first() == Some(&'\u{feff}') {
        return false;
    }
    match value(&s, ws(&s, 0)) {
        Err(f) => f == Fail::Value,
        Ok(_) => false,
    }
}

fn reqwest_failure(e: &reqwest::Error) -> Raised {
    let text = e.to_string();
    if e.is_timeout() {
        return Raised {
            text: "timed out".into(),
            auth: false,
            cause: Cause::Timeout,
        };
    }
    if e.is_connect() {
        let mut source: Option<&(dyn std::error::Error + 'static)> = std::error::Error::source(e);
        while let Some(s) = source {
            if let Some(io) = s.downcast_ref::<std::io::Error>() {
                if io.kind() == std::io::ErrorKind::ConnectionRefused {
                    return Raised {
                        text: format!("<urlopen error {io}>"),
                        auth: false,
                        cause: Cause::Refused,
                    };
                }
                if io.kind() == std::io::ErrorKind::TimedOut {
                    return Raised {
                        text: "<urlopen error timed out>".into(),
                        auth: false,
                        cause: Cause::Timeout,
                    };
                }
            }
            source = s.source();
        }
        // A name that does not resolve is `socket.gaierror` in Python.
        let name = if text.contains("dns") || text.contains("resolve") {
            "gaierror"
        } else {
            "OSError"
        };
        return Raised {
            text: format!("<urlopen error {text}>"),
            auth: false,
            cause: Cause::Unreachable(name.into()),
        };
    }
    Raised {
        text,
        auth: false,
        cause: Cause::Unreachable("OSError".into()),
    }
}

// ---------------------------------------------------------------------------
// The OpenAI-compatible extractor
// ---------------------------------------------------------------------------

/// `OpenAICompatExtractor` (`memory/dream.py:1231-1284`) as the daemon
/// builds it (no prompt overrides, no judge-thinking knob).
#[derive(Debug, Clone, PartialEq)]
pub struct Compat {
    pub base_url: String,
    pub model: String,
    pub api_key: Option<String>,
    pub max_tokens: i64,
    pub timeout: f64,
    pub extra_body: Map<String, Value>,
    pub system_prompt: String,
    pub events_prompt: String,
}

impl Compat {
    pub fn new(
        base_url: &str,
        model: &str,
        api_key: Option<String>,
        max_tokens: i64,
        timeout: f64,
        extra_body: Option<Map<String, Value>>,
    ) -> Self {
        Compat {
            base_url: base_url.trim_end_matches('/').to_string(),
            model: model.to_string(),
            api_key: api_key.filter(|k| !k.is_empty()),
            max_tokens,
            timeout,
            extra_body: extra_body.unwrap_or_default(),
            system_prompt: prompts::SYSTEM_PROMPT.to_string(),
            events_prompt: prompts::EVENTS_SYSTEM_PROMPT.to_string(),
        }
    }

    /// The request body every call sends: `{**extra_body, "model", "messages",
    /// "response_format", "max_tokens", "temperature", "chat_template_kwargs"}`
    /// as `json.dumps(...).encode()`.
    pub fn body(&self, system: &str, user: &str) -> String {
        let mut m = self.extra_body.clone();
        m.insert("model".into(), json!(self.model));
        m.insert(
            "messages".into(),
            json!([{"role": "system", "content": system}, {"role": "user", "content": user}]),
        );
        m.insert("response_format".into(), json!({"type": "json_object"}));
        m.insert("max_tokens".into(), json!(self.max_tokens));
        m.insert("temperature".into(), json!(0));
        m.insert(
            "chat_template_kwargs".into(),
            json!({"enable_thinking": false}),
        );
        pyjson::dumps(&Value::Object(m))
    }

    /// The claims request body, or None when no non-empty text remains.
    pub fn extract_body(
        &self,
        texts: &[String],
        vocab: &[String],
        known_facts: Option<&[(String, String, String)]>,
    ) -> Option<String> {
        let texts: Vec<&str> = texts
            .iter()
            .map(String::as_str)
            .filter(|t| !t.is_empty())
            .collect();
        if texts.is_empty() {
            return None;
        }
        let system = format!(
            "{}{}{}",
            self.system_prompt,
            vocab_hint(vocab),
            facts_hint(known_facts)
        );
        Some(self.body(&system, &numbered(&texts)))
    }

    pub fn events_body(&self, texts: &[String]) -> Option<String> {
        let texts: Vec<&str> = texts
            .iter()
            .map(String::as_str)
            .filter(|t| !t.is_empty())
            .collect();
        if texts.is_empty() {
            return None;
        }
        Some(self.body(&self.events_prompt, &numbered(&texts)))
    }

    pub fn relations_body(
        &self,
        texts: &[String],
        relations: &[(String, String)],
    ) -> Option<String> {
        let texts: Vec<&str> = texts
            .iter()
            .map(String::as_str)
            .filter(|t| !t.is_empty())
            .collect();
        if texts.is_empty() {
            return None;
        }
        Some(self.body(&relations_prompt(relations), &texts.join("\n\n")))
    }

    /// POST the body, then `data["choices"][0]["message"]["content"] or ""`,
    /// cut from the first `{` to the last `}`, then `json.loads`. Every step
    /// sits inside the method's `try`.
    async fn completion(&self, body: String) -> Result<Value, Raised> {
        let content = self.content(body).await?;
        let content = match (content.find('{'), content.rfind('}')) {
            (Some(s), Some(e)) if e > s => &content[s..=e],
            _ => content.as_str(),
        };
        loads(content)
    }

    /// The reply's message content, as `completion` reads it.
    async fn content(&self, body: String) -> Result<String, Raised> {
        let timeout = Duration::try_from_secs_f64(self.timeout)
            .map_err(|_| Raised::other("ValueError", "Timeout value must be positive"))?;
        // `no_redirect.urlopen`: a redirect is an HTTPError with its own code.
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(timeout)
            .build()
            .map_err(|e| Raised::other("OSError", e.to_string()))?;
        let mut req = client
            .post(format!("{}/chat/completions", self.base_url))
            .header("content-type", "application/json")
            .body(body);
        if let Some(key) = &self.api_key {
            req = req.header("authorization", format!("Bearer {key}"));
        }
        let resp = req.send().await.map_err(|e| reqwest_failure(&e))?;
        let status = resp.status();
        let bytes = resp.bytes().await.map_err(|e| reqwest_failure(&e))?;
        if !status.is_success() {
            let code = status.as_u16();
            let peek = String::from_utf8_lossy(&bytes[..bytes.len().min(AUTH_BODY_PEEK)]);
            return Err(Raised {
                text: format!(
                    "HTTP Error {code}: {}",
                    status.canonical_reason().unwrap_or("")
                ),
                auth: code == 401 || code == 403 || auth_words().is_match(&peek),
                cause: Cause::Http(code),
            });
        }
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| Raised::other("UnicodeDecodeError", "'utf-8' codec can't decode"))?;
        let data = loads(text)?;
        let content = envelope_content(&data)?;
        match content {
            c if !truthy(c) => Ok(String::new()),
            Value::String(s) => Ok(s.clone()),
            other => Err(Raised::other(
                "AttributeError",
                format!("{} object has no attribute 'find'", type_name(other)),
            )),
        }
    }

    /// `extract` (`memory/dream.py:1286-1397`): claims, then the events the
    /// same reply carries.
    pub async fn extract(
        &self,
        texts: &[String],
        vocab: &[String],
        known_facts: Option<&[(String, String, String)]>,
    ) -> Result<Vec<Extracted>, ExtractFailure> {
        let Some(body) = self.extract_body(texts, vocab, known_facts) else {
            return Ok(Vec::new());
        };
        let n = texts.iter().filter(|t| !t.is_empty()).count();
        let parsed = self
            .completion(body)
            .await
            .map_err(|e| e.label("extract", true))?;
        let raw = match &parsed {
            Value::Object(m) => m.get("claims").cloned().unwrap_or(Value::Array(vec![])),
            _ => Value::Array(vec![]),
        };
        // `events_from_parsed` runs inside the try: its escape is an
        // ExtractorError here.
        let events = events_from_parsed(&parsed, n)
            .map_err(|u| Raised::other(u.type_name, u.message).label("extract", true))?;
        let mut out: Vec<Extracted> = parse_claims(&raw, n)?
            .into_iter()
            .map(Extracted::Claim)
            .collect();
        out.extend(events.into_iter().map(Extracted::Event));
        Ok(out)
    }

    /// `extract_events` (`memory/dream.py:1399-1443`).
    pub async fn extract_events(&self, texts: &[String]) -> Result<Vec<Event>, ExtractFailure> {
        let Some(body) = self.events_body(texts) else {
            return Ok(Vec::new());
        };
        let n = texts.iter().filter(|t| !t.is_empty()).count();
        let parsed = self
            .completion(body)
            .await
            .map_err(|e| e.label("events pass", false))?;
        Ok(events_from_parsed(&parsed, n)?)
    }

    /// `extract_relations` (`memory/dream.py:1603-1659`).
    pub async fn extract_relations(
        &self,
        texts: &[String],
        relations: &[(String, String)],
    ) -> Result<Vec<RelationClaim>, ExtractFailure> {
        let Some(body) = self.relations_body(texts, relations) else {
            return Ok(Vec::new());
        };
        let parsed = self
            .completion(body)
            .await
            .map_err(|e| e.label("extract_relations", false))?;
        let raw = match &parsed {
            Value::Object(m) => m.get("relations").cloned().unwrap_or(Value::Array(vec![])),
            _ => Value::Array(vec![]),
        };
        Ok(parse_relations(&raw)?)
    }
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "'NoneType'",
        Value::Bool(_) => "'bool'",
        Value::Number(n) => match py_num(n) {
            PyNum::Int(_) => "'int'",
            PyNum::Float(_) => "'float'",
        },
        Value::String(_) => "'str'",
        Value::Array(_) => "'list'",
        Value::Object(_) => "'dict'",
    }
}

/// `data["choices"][0]["message"]["content"]` with Python's failures.
fn envelope_content(data: &Value) -> Result<&Value, Raised> {
    let key = |v: &'_ Value, k: &str| -> Raised {
        match v {
            Value::Object(_) => Raised::other("KeyError", format!("'{k}'")),
            Value::String(_) => {
                Raised::other("TypeError", "string indices must be integers, not 'str'")
            }
            Value::Array(_) => Raised::other(
                "TypeError",
                "list indices must be integers or slices, not str",
            ),
            other => Raised::other(
                "TypeError",
                format!("{} object is not subscriptable", type_name(other)),
            ),
        }
    };
    let choices = match data {
        Value::Object(m) if m.contains_key("choices") => &m["choices"],
        other => return Err(key(other, "choices")),
    };
    let first = match choices {
        Value::Array(items) => items
            .first()
            .ok_or_else(|| Raised::other("IndexError", "list index out of range"))?,
        Value::Object(_) => return Err(Raised::other("KeyError", "0")),
        Value::String(s) => {
            // `"..."[0]` is a one-character string; indexing it next fails.
            if s.is_empty() {
                return Err(Raised::other("IndexError", "string index out of range"));
            }
            return Err(Raised::other(
                "TypeError",
                "string indices must be integers, not 'str'",
            ));
        }
        other => {
            return Err(Raised::other(
                "TypeError",
                format!("{} object is not subscriptable", type_name(other)),
            ));
        }
    };
    let message = match first {
        Value::Object(m) if m.contains_key("message") => &m["message"],
        other => return Err(key(other, "message")),
    };
    match message {
        Value::Object(m) if m.contains_key("content") => Ok(&m["content"]),
        other => Err(key(other, "content")),
    }
}

/// What `build_extractor` returns: the OpenAI-compatible client, or the
/// `NoOpExtractor` whose `extract` returns `[]`.
#[derive(Debug, Clone, PartialEq)]
pub enum Extractor {
    NoOp,
    Compat(Box<Compat>),
}

impl Extractor {
    pub fn as_compat(&self) -> Option<&Compat> {
        match self {
            Extractor::Compat(c) => Some(c),
            Extractor::NoOp => None,
        }
    }

    /// `extract` on either kind (`NoOpExtractor.extract` returns `[]`).
    pub async fn extract(
        &self,
        texts: &[String],
        vocab: &[String],
        known_facts: Option<&[(String, String, String)]>,
    ) -> Result<Vec<Extracted>, ExtractFailure> {
        match self {
            Extractor::NoOp => Ok(Vec::new()),
            Extractor::Compat(c) => c.extract(texts, vocab, known_facts).await,
        }
    }
}

// ---------------------------------------------------------------------------
// Endpoint resolution and the builders
// ---------------------------------------------------------------------------

/// `resolve_endpoints`' dict (`memory/dream.py:1999-2053`).
#[derive(Debug, Clone, PartialEq)]
pub struct Endpoints {
    pub mode: String,
    pub primary_url: Option<String>,
    pub primary_model: Option<String>,
    pub fallback_url: Option<String>,
    pub fallback_model: Option<String>,
    pub max_tokens: i64,
    pub timeout: f64,
}

fn set(v: &Option<String>) -> bool {
    v.as_deref().is_some_and(|s| !s.is_empty())
}

impl Endpoints {
    pub fn has_primary(&self) -> bool {
        set(&self.primary_url) && set(&self.primary_model)
    }

    pub fn has_fallback(&self) -> bool {
        set(&self.fallback_url) && set(&self.fallback_model)
    }
}

/// The process environment, as `os.environ.get` reads it.
pub fn process_env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

/// `resolve_endpoints` over a given environment.
pub fn resolve_endpoints_with(
    cfg: &DreamConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Endpoints {
    // `os.environ.get(name) or default`: an empty value falls through.
    let pick = |name: &str, default: &Option<String>| {
        env(name)
            .filter(|v| !v.is_empty())
            .or_else(|| default.clone())
    };
    let mut out = if cfg.extractor_source == "config" {
        Endpoints {
            mode: cfg.extractor_mode.clone(),
            primary_url: cfg.extractor_base_url.clone(),
            primary_model: cfg.extractor_model.clone(),
            fallback_url: cfg.fallback_base_url.clone(),
            fallback_model: cfg.fallback_model.clone(),
            max_tokens: cfg.extractor_max_tokens,
            timeout: cfg.extractor_timeout_seconds,
        }
    } else {
        let max_tokens = env("PSEUDOLIFE_DREAM_MAX_TOKENS")
            .filter(|v| !v.is_empty())
            .map_or(cfg.extractor_max_tokens, |raw| {
                py_int_str(&raw)
                    .and_then(|v| i64::try_from(v).ok())
                    .unwrap_or(cfg.extractor_max_tokens)
            });
        let timeout = env("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS")
            .filter(|v| !v.is_empty())
            .map_or(cfg.extractor_timeout_seconds, |raw| {
                py_float_str(&raw).unwrap_or(cfg.extractor_timeout_seconds)
            });
        Endpoints {
            mode: env("PSEUDOLIFE_DREAM_EXTRACTOR_MODE")
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| cfg.extractor_mode.clone()),
            primary_url: pick("PSEUDOLIFE_DREAM_BASE_URL", &cfg.extractor_base_url),
            primary_model: pick("PSEUDOLIFE_DREAM_MODEL", &cfg.extractor_model),
            fallback_url: pick("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL", &cfg.fallback_base_url),
            fallback_model: pick("PSEUDOLIFE_DREAM_FALLBACK_MODEL", &cfg.fallback_model),
            max_tokens,
            timeout,
        }
    };
    if !EXTRACTOR_MODES.contains(&out.mode.as_str()) {
        out.mode = "auto".into();
    }
    // The model-only override wins over both owners, primary only.
    if set(&cfg.extractor_model_override) {
        out.primary_model = cfg.extractor_model_override.clone();
    }
    out
}

/// `resolve_endpoints(cfg)` with the process environment.
pub fn resolve_endpoints(cfg: &DreamConfig) -> Endpoints {
    resolve_endpoints_with(cfg, &process_env)
}

/// `_cache_extra_body` (`memory/dream.py:2181-2189`).
pub fn cache_extra_body(cfg: &DreamConfig) -> Option<Map<String, Value>> {
    cfg.extractor_cache_prompt.map(|v| {
        let mut m = Map::new();
        m.insert("cache_prompt".into(), json!(v));
        m
    })
}

/// `_primary_extra_body` (`memory/dream.py:2192-2204`).
pub fn primary_extra_body(cfg: &DreamConfig) -> Option<Map<String, Value>> {
    let mut body = cache_extra_body(cfg).unwrap_or_default();
    if let Some(effort) = cfg
        .extractor_reasoning_effort
        .as_deref()
        .filter(|e| !e.is_empty())
    {
        body.insert("reasoning_effort".into(), json!(effort));
    }
    (!body.is_empty()).then_some(body)
}

/// `build_extractor` (`memory/dream.py:2366-2395`) over a given environment.
pub fn build_extractor_with(cfg: &DreamConfig, env: &dyn Fn(&str) -> Option<String>) -> Extractor {
    let r = resolve_endpoints_with(cfg, env);
    let api_key = env("PSEUDOLIFE_DREAM_API_KEY")
        .filter(|v| !v.is_empty())
        .or_else(|| cfg.extractor_api_key.clone());
    if r.has_primary() {
        return Extractor::Compat(Box::new(Compat::new(
            r.primary_url.as_deref().unwrap_or(""),
            r.primary_model.as_deref().unwrap_or(""),
            api_key,
            r.max_tokens,
            r.timeout,
            primary_extra_body(cfg),
        )));
    }
    Extractor::NoOp
}

pub fn build_extractor(cfg: &DreamConfig) -> Extractor {
    build_extractor_with(cfg, &process_env)
}

fn fallback_extractor(cfg: &DreamConfig, r: &Endpoints, key: Option<String>) -> Extractor {
    Extractor::Compat(Box::new(Compat::new(
        r.fallback_url.as_deref().unwrap_or(""),
        r.fallback_model.as_deref().unwrap_or(""),
        key,
        r.max_tokens,
        r.timeout,
        cache_extra_body(cfg),
    )))
}

/// `build_extractor_with_fallback` (`memory/dream.py:2207-2247`): the
/// extractor and which endpoint it is (`primary` / `fallback`). `Err` is
/// the ValueError for mode `fallback` with no fallback endpoint.
pub async fn build_extractor_with_fallback_using(
    cfg: &DreamConfig,
    env: &(dyn Fn(&str) -> Option<String> + Sync),
    retry_delay: f64,
) -> Result<(Extractor, &'static str), String> {
    let r = resolve_endpoints_with(cfg, env);
    let fallback_key = env("PSEUDOLIFE_DREAM_FALLBACK_API_KEY")
        .filter(|v| !v.is_empty())
        .or_else(|| cfg.fallback_api_key.clone());
    if r.mode == "fallback" {
        if !r.has_fallback() {
            return Err(
                "extractor_mode=fallback but no fallback endpoint is configured \
                        (fallback_base_url/fallback_model)"
                    .into(),
            );
        }
        return Ok((fallback_extractor(cfg, &r, fallback_key), "fallback"));
    }
    if !r.has_fallback() || r.mode == "primary" {
        return Ok((build_extractor_with(cfg, env), "primary"));
    }
    if let Some(url) = r.primary_url.as_deref().filter(|u| !u.is_empty())
        && probe_primary(url, retry_delay).await
    {
        return Ok((build_extractor_with(cfg, env), "primary"));
    }
    eprintln!(
        "dream primary extractor {} unreachable — using fallback {}",
        r.primary_url.as_deref().unwrap_or("None"),
        r.fallback_url.as_deref().unwrap_or("None")
    );
    Ok((fallback_extractor(cfg, &r, fallback_key), "fallback"))
}

pub async fn build_extractor_with_fallback(
    cfg: &DreamConfig,
) -> Result<(Extractor, &'static str), String> {
    build_extractor_with_fallback_using(cfg, &process_env, PROBE_RETRY_DELAY).await
}

// ---------------------------------------------------------------------------
// Probes
// ---------------------------------------------------------------------------

/// A plain `urllib.request.urlopen` GET: redirects followed.
async fn get(url: &str, timeout: f64) -> Result<(u16, Vec<u8>), ()> {
    let timeout = Duration::try_from_secs_f64(timeout).map_err(|_| ())?;
    let client = reqwest::Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|_| ())?;
    let resp = client.get(url).send().await.map_err(|_| ())?;
    let status = resp.status().as_u16();
    let body = resp.bytes().await.map_err(|_| ())?;
    Ok((status, body.to_vec()))
}

/// `probe_endpoint` (`memory/dream.py:2056-2076`): `GET {root}/health`
/// (any trailing `/v1` stripped); a 404 there retries `GET {base}/models`.
/// Only HTTP 200 counts.
pub async fn probe_endpoint(base_url: &str, timeout: f64) -> bool {
    let base = base_url.trim_end_matches('/');
    let root = base.strip_suffix("/v1").unwrap_or(base);
    for (i, url) in [format!("{root}/health"), format!("{base}/models")]
        .iter()
        .enumerate()
    {
        match get(url, timeout).await {
            Ok((status, _)) if (200..300).contains(&status) => return status == 200,
            Ok((404, _)) if i == 0 => continue,
            _ => return false,
        }
    }
    false
}

/// `_probe_primary` (`memory/dream.py:2169-2178`): one retry after the delay.
pub async fn probe_primary(url: &str, delay: f64) -> bool {
    if probe_endpoint(url, 3.0).await {
        return true;
    }
    if let Ok(d) = Duration::try_from_secs_f64(delay) {
        tokio::time::sleep(d).await;
    }
    probe_endpoint(url, 3.0).await
}

type ServedCache = Mutex<HashMap<String, (Option<String>, Instant)>>;

fn served_cache() -> &'static ServedCache {
    static CACHE: std::sync::OnceLock<ServedCache> = std::sync::OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

/// `fetch_served_model` (`memory/dream.py:2143-2166`): the first model id
/// of `GET {base_url}/models`, cached per URL for five minutes, failures
/// included.
pub async fn fetch_served_model(base_url: &str) -> Option<String> {
    if let Some((served, at)) = served_cache().lock().expect("served cache").get(base_url)
        && at.elapsed().as_secs_f64() < SERVED_MODEL_TTL
    {
        return served.clone();
    }
    let started = Instant::now();
    let url = format!("{}/models", base_url.trim_end_matches('/'));
    let served = match get(&url, 1.5).await {
        Ok((status, body)) if (200..300).contains(&status) => served_id(&body),
        _ => None,
    };
    served_cache()
        .lock()
        .expect("served cache")
        .insert(base_url.to_string(), (served.clone(), started));
    served
}

/// `data.get("data") or []`, then the first entry's truthy `id` as `str()`.
fn served_id(body: &[u8]) -> Option<String> {
    let data = crate::mcp::py_loads::loads(&String::from_utf8_lossy(body))?;
    let entries = data.as_object()?.get("data").filter(|v| truthy(v))?;
    let first = match entries {
        Value::Array(items) => items.first()?,
        _ => return None,
    };
    let id = first.as_object()?.get("id").filter(|v| truthy(v))?;
    Some(py_str(id))
}

/// `_status_extractor_fields` (`memory/dream.py:2250-2275`), in its key order.
pub async fn status_extractor_fields(
    cfg: &DreamConfig,
    last_dream_extractor: Option<&Value>,
) -> Map<String, Value> {
    let r = resolve_endpoints(cfg);
    let has_fallback = r.has_fallback();
    let served = match (&r.primary_url, &r.primary_model) {
        (Some(url), Some(model))
            if !url.is_empty() && ALIAS_MODEL_NAMES.contains(&model.as_str()) =>
        {
            fetch_served_model(url).await
        }
        _ => None,
    };
    let mut m = Map::new();
    m.insert("extractor_mode".into(), json!(r.mode));
    m.insert("primary_url".into(), json!(r.primary_url));
    m.insert("primary_model".into(), json!(r.primary_model));
    m.insert("primary_model_served".into(), json!(served));
    m.insert(
        "fallback_url".into(),
        json!(if has_fallback {
            r.fallback_url.clone()
        } else {
            None
        }),
    );
    m.insert(
        "fallback_model".into(),
        json!(if has_fallback {
            r.fallback_model.clone()
        } else {
            None
        }),
    );
    m.insert("extractor_source".into(), json!(cfg.extractor_source));
    m.insert("model_override".into(), json!(cfg.extractor_model_override));
    m.insert(
        "reasoning_effort".into(),
        json!(
            cfg.extractor_reasoning_effort
                .as_deref()
                .filter(|e| !e.is_empty())
        ),
    );
    let healthy = match r.primary_url.as_deref() {
        Some(_) if has_fallback && crate::mutants::active("mcp-dream-status-no-probe") => {
            Some(true)
        }
        Some(url) if has_fallback && !url.is_empty() => Some(probe_endpoint(url, 2.0).await),
        _ => None,
    };
    m.insert("primary_healthy".into(), json!(healthy));
    m.insert("last_dream_extractor".into(), json!(last_dream_extractor));
    m
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn json_errors_classify_like_the_c_scanner() {
        // CPython 3.11: `json.loads(s)` and whether the error's context is
        // a StopIteration ("Expecting value").
        let value = [
            "not json at all",
            "nope",
            "[nope]",
            "[1,]",
            "{\"a\": }",
            "",
            "  ",
            "-",
            "-x",
            "tru",
            "-Inf",
            "[",
            "{\"claims\": [}",
            "x",
            "]",
            ".5",
        ];
        let other = [
            "{\"a\" 1}",
            "{a:1}",
            "null x",
            "\"abc",
            "[1 2]",
            "1e",
            "01",
            "{",
            "\u{feff}{}",
            "1.",
        ];
        for s in value {
            assert!(json_error_expects_value(s), "{s:?}");
        }
        for s in other {
            assert!(!json_error_expects_value(s), "{s:?}");
        }
    }

    const GOLDEN: &str = include_str!("../../harness/goldens/dream-extractor.json");

    fn golden() -> Value {
        serde_json::from_str(GOLDEN).expect("golden file")
    }

    fn ctor(name: &str, base: &str) -> Compat {
        match name {
            "primary" => {
                let mut extra = Map::new();
                extra.insert("cache_prompt".into(), json!(false));
                extra.insert("reasoning_effort".into(), json!("high"));
                Compat::new(base, "qwen-27b", Some("k".into()), 2048, 30.0, Some(extra))
            }
            _ => Compat::new(base, "m", None, 7, 30.0, None),
        }
    }

    #[test]
    fn request_bodies_match_python_byte_for_byte() {
        let g = golden();
        let texts: Vec<String> = [
            "we moved the deploy target to prod-eu",
            "",
            "caf\u{e9} \u{2014} \u{fc}n\u{ef}code \u{1F600} \"q\" \\ tab\t",
            "line\nbreak\u{7f}",
        ]
        .map(String::from)
        .to_vec();
        let vocab: Vec<String> = (0..65).map(|i| format!("entity-{i}.attr-{i}")).collect();
        let known = vec![
            (
                "deploy target".to_string(),
                "environment".to_string(),
                "prod-eu".to_string(),
            ),
            ("u".to_string(), "a".to_string(), "v\u{e9}".to_string()),
        ];
        let relations = vec![
            ("depends-on".to_string(), "src needs dst".to_string()),
            ("uses".to_string(), "src makes use of dst".to_string()),
        ];
        let cases = g["bodies"].as_array().unwrap();
        assert_eq!(cases.len(), 8);
        for case in cases {
            let ex = ctor(case["ctor"].as_str().unwrap(), "http://127.0.0.1:1/v1");
            let body = match case["method"].as_str().unwrap() {
                "extract" => ex.extract_body(&texts, &vocab, Some(&known)),
                "extract_no_hints" => ex.extract_body(&texts, &[], None),
                "events" => ex.events_body(&texts),
                _ => ex.relations_body(&texts, &relations),
            };
            assert_eq!(
                body.as_deref(),
                case["body"].as_str(),
                "{} {}",
                case["ctor"],
                case["method"]
            );
        }
        let ex = ctor("plain", "http://h");
        assert_eq!(ex.extract_body(&[String::new()], &[], None), None);
    }

    /// One fixed reply per connection, as the oracle's endpoint answered.
    fn serve(status: u16, body: Vec<u8>, headers: Vec<(String, String)>) -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { return };
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let n = s.read(&mut chunk).unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..end]).to_ascii_lowercase();
                        let len = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .map_or(0, |v| v.trim().parse().unwrap_or(0));
                        if buf.len() >= end + 4 + len {
                            break;
                        }
                    }
                }
                let mut head = format!(
                    "HTTP/1.1 {status} X\r\nContent-Length: {}\r\nConnection: close\r\n",
                    body.len()
                );
                for (k, v) in &headers {
                    head.push_str(&format!("{k}: {v}\r\n"));
                }
                head.push_str("\r\n");
                let _ = s.write_all(head.as_bytes());
                let _ = s.write_all(&body);
            }
        });
        format!("http://127.0.0.1:{port}/v1")
    }

    #[tokio::test]
    async fn replies_parse_and_fail_as_python() {
        let g = golden();
        let notes: Vec<String> = ["n1", "n2", "n3"].map(String::from).to_vec();
        for case in g["replies"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let body: Vec<u8> = case["body"]
                .as_str()
                .unwrap()
                .chars()
                .map(|c| c as u32 as u8)
                .collect();
            let headers = case["headers"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                .collect();
            let base = serve(case["status"].as_u64().unwrap() as u16, body, headers);
            let ex = ctor("primary", &base);
            let got: Result<Vec<Value>, ExtractFailure> = match case["method"].as_str().unwrap() {
                "extract" => ex
                    .extract(&notes, &[], None)
                    .await
                    .map(|v| v.iter().map(Extracted::to_json).collect()),
                "events" => ex
                    .extract_events(&notes)
                    .await
                    .map(|v| v.iter().map(Event::to_json).collect()),
                _ => ex
                    .extract_relations(&notes[..2], &[("uses".into(), "u".into())])
                    .await
                    .map(|v| v.iter().map(RelationClaim::to_json).collect()),
            };
            match (&case["ok"], got) {
                (Value::String(want), Ok(items)) => {
                    assert_eq!(&pyjson::dumps(&Value::Array(items)), want, "{name}");
                }
                (Value::Null, Err(ExtractFailure::Extractor(e))) => {
                    let (reason, error) = e.classify();
                    assert_eq!(
                        json!([reason, error]),
                        case["error"],
                        "{name}: {}",
                        e.message
                    );
                }
                (_, other) => panic!("{name}: {other:?}"),
            }
        }
    }

    fn config_from(v: &Value) -> DreamConfig {
        let mut c = DreamConfig::default();
        let s = |k: &str| v.get(k).and_then(Value::as_str).map(String::from);
        if let Some(x) = s("extractor_source") {
            c.extractor_source = x;
        }
        if let Some(x) = s("extractor_mode") {
            c.extractor_mode = x;
        }
        c.extractor_base_url = s("extractor_base_url");
        c.extractor_model = s("extractor_model");
        c.fallback_base_url = s("fallback_base_url");
        c.fallback_model = s("fallback_model");
        c.extractor_model_override = s("extractor_model_override");
        c.extractor_api_key = s("extractor_api_key");
        c.fallback_api_key = s("fallback_api_key");
        c.extractor_reasoning_effort = s("extractor_reasoning_effort");
        if let Some(x) = v.get("extractor_max_tokens").and_then(Value::as_i64) {
            c.extractor_max_tokens = x;
        }
        if let Some(x) = v.get("extractor_timeout_seconds").and_then(Value::as_f64) {
            c.extractor_timeout_seconds = x;
        }
        if let Some(x) = v.get("extractor_cache_prompt") {
            c.extractor_cache_prompt = x.as_bool();
        }
        c
    }

    #[tokio::test]
    async fn endpoints_and_builders_match_python() {
        let g = golden();
        for case in g["endpoints"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let cfg = config_from(&case["config"]);
            let env_map = case["env"].as_object().unwrap().clone();
            let env = move |k: &str| env_map.get(k).and_then(Value::as_str).map(String::from);
            let r = resolve_endpoints_with(&cfg, &env);
            let want = &case["resolved"];
            assert_eq!(json!(r.mode), want["mode"], "{name}");
            assert_eq!(json!(r.primary_url), want["primary_url"], "{name}");
            assert_eq!(json!(r.primary_model), want["primary_model"], "{name}");
            assert_eq!(json!(r.fallback_url), want["fallback_url"], "{name}");
            assert_eq!(json!(r.fallback_model), want["fallback_model"], "{name}");
            assert_eq!(json!(r.max_tokens), want["max_tokens"], "{name}");
            assert_eq!(r.timeout, want["timeout"].as_f64().unwrap(), "{name}");
            let built = build_extractor_with_fallback_using(&cfg, &env, 0.0).await;
            let want = &case["built"];
            match built {
                Err(e) => assert_eq!(json!(e), want["error"], "{name}"),
                Ok((Extractor::NoOp, which)) => {
                    assert_eq!(want["kind"], "noop", "{name}");
                    assert_eq!(json!(which), want["which"], "{name}");
                }
                Ok((Extractor::Compat(c), which)) => {
                    assert_eq!(want["kind"], "compat", "{name}");
                    assert_eq!(json!(which), want["which"], "{name}");
                    assert_eq!(json!(c.base_url), want["base_url"], "{name}");
                    assert_eq!(json!(c.model), want["model"], "{name}");
                    assert_eq!(json!(c.api_key), want["api_key"], "{name}");
                    assert_eq!(json!(c.max_tokens), want["max_tokens"], "{name}");
                    assert_eq!(c.timeout, want["timeout"].as_f64().unwrap(), "{name}");
                    assert_eq!(
                        Value::Object(c.extra_body.clone()),
                        want["extra_body"],
                        "{name}"
                    );
                }
            }
        }
    }

    #[test]
    fn python_coercions() {
        assert_eq!(py_float_str(" 1_0 "), Some(10.0));
        assert_eq!(py_float_str("1__0"), None);
        assert_eq!(py_float_str("-inf"), Some(f64::NEG_INFINITY));
        assert!(py_float_str("NaN").unwrap().is_nan());
        assert_eq!(py_float_str("1e"), None);
        assert_eq!(py_float_str("--1"), None);
        assert_eq!(py_int_str(" +7 "), Some(7));
        assert_eq!(py_int_str("1.0"), None);
        assert_eq!(py_int_str("_1"), None);
        assert_eq!(clamp01(f64::NAN), 1.0);
        assert_eq!(clamp01(-0.0).to_bits(), 0.0f64.to_bits());
        assert!(strptime_ymd("2024-02-29"));
        assert!(!strptime_ymd("2023-02-29"));
        assert!(strptime_ymd("2023-1- 9"));
        assert!(!strptime_ymd("2023-13-01"));
        let inf = crate::mcp::py_loads::loads("[Infinity, -Infinity, NaN, 1e400]").unwrap();
        assert_eq!(py_str(&inf), "[inf, -inf, nan, inf]");
    }

    #[test]
    fn auth_words_are_whole_words() {
        let e = |m: &str| ExtractorError {
            message: m.into(),
            auth_failure: false,
            cause: Cause::Other("KeyError".into()),
        };
        assert_eq!(
            e("extract failed: not logged in").classify().0,
            "login_expired"
        );
        assert_eq!(
            e("extract failed: author catalog index").classify().0,
            "extractor_error"
        );
    }
}
