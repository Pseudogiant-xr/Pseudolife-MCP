//! The extractor calls behind the dream's post-consolidation stages
//! (`memory/dream.py`): `infer_outcomes` (1715-1748, parsed by
//! `_parse_outcome_claims` 1092-1126), the lesson calls `extract_lessons`
//! and `extract_rules` (1522-1597) over `_lessons_completion` (1445-1478)
//! and `_lesson_claims` (1480-1520), and `summarize_session` (1955-1987,
//! parsed by `_parse_digest` 1129-1153), with the helpers they read:
//! `_format_signals`, `is_rule_signal`, `_strip_rule_prefix`,
//! `_must_include`, `_missing_values` and `split_session_context`.
//!
//! Each body is the shared request ([`Compat::body`]); the replies parse
//! with Python's coercions. Where Python lets an exception escape the
//! method (a `RecursionError` from a too-deep reply in the two parsers
//! that catch only `ValueError`, an `OverflowError` from `float()` of a
//! huge integer confidence), the call returns it as
//! [`ExtractFailure::Uncaught`].

use serde_json::{Map, Value, json};

use super::extractor::{
    Cause, Compat, ExtractFailure, ExtractorError, Uncaught, confidence, field, loads, truthy,
};
use super::prompts;
use crate::storage::{py_repr, py_strip};

/// `OUTCOMES` (`memory/lessons.py:32`).
pub const OUTCOMES: [&str; 3] = ["success", "failure", "correction"];
/// `_RULE_PREFIX`.
const RULE_PREFIX: &str = "rule:";

// ---------------------------------------------------------------------------
// Signals
// ---------------------------------------------------------------------------

/// One `outcome_signals` row as `pending_signals` reads it: `id`, the
/// `_SIGNAL_COLS` and `used_ids`.
#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    pub id: i64,
    pub task: String,
    pub outcome: String,
    pub about: Option<String>,
    pub detail: Option<String>,
    pub polarity: Option<String>,
    pub origin: Option<String>,
    pub episode_id: Option<String>,
    pub created_at: f64,
    pub used_ids: Option<Value>,
}

fn nonempty(v: &Option<String>) -> Option<&str> {
    v.as_deref().filter(|s| !s.is_empty())
}

/// `_format_signals`: one line per signal, the free-text fields as Python
/// `repr()` (`!r`), an inferred signal marked.
pub fn format_signals(signals: &[Signal]) -> String {
    let mut lines = Vec::with_capacity(signals.len());
    for s in signals {
        let mut parts = vec![
            format!("[{}]", s.outcome),
            format!("task={}", py_repr(&s.task)),
        ];
        if let Some(about) = nonempty(&s.about) {
            parts.push(format!("about={}", py_repr(about)));
        }
        if let Some(detail) = nonempty(&s.detail) {
            parts.push(format!("detail={}", py_repr(detail)));
        }
        if let Some(polarity) = nonempty(&s.polarity) {
            parts.push(format!("polarity={polarity}"));
        }
        let line = parts.join(" ");
        if s.origin.as_deref() == Some("inferred") {
            lines.push(format!("[machine-inferred] {line}"));
        } else {
            lines.push(line);
        }
    }
    lines.join("\n")
}

/// Python `str.lower()` (Unicode 14, final sigma in context).
fn py_lower(s: &str) -> String {
    crate::storage::graph::py_lower(s)
}

/// `is_rule_signal`: every signal in rule mode, else one whose `about`
/// starts with `rule:` (any case, surrounding space stripped).
pub fn is_rule_signal(signal: &Signal, rule_mode: bool) -> bool {
    if rule_mode {
        return true;
    }
    let about = signal.about.as_deref().unwrap_or("");
    py_lower(py_strip(about)).starts_with(RULE_PREFIX)
}

/// `_strip_rule_prefix`: the `rule:` routing prefix and the space around
/// it dropped; None for what is left blank.
pub fn strip_rule_prefix(about: Option<&str>) -> Option<String> {
    let mut text = py_strip(about.unwrap_or("")).to_string();
    if py_lower(&text).starts_with(RULE_PREFIX) {
        let rest: String = text.chars().skip(RULE_PREFIX.chars().count()).collect();
        text = py_strip(&rest).to_string();
    }
    (!text.is_empty()).then_some(text)
}

/// `str.splitlines()`: the line boundaries Python splits on, a `\r\n`
/// counting once, no trailing empty line.
fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let boundary = matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !boundary {
            continue;
        }
        out.push(&s[start..i]);
        let mut end = i + c.len_utf8();
        if c == '\r'
            && let Some(&(j, '\n')) = it.peek()
        {
            it.next();
            end = j + 1;
        }
        start = end;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `_must_include`: the values of the first `MUST INCLUDE: a; b` line of a
/// signal's detail (the label matched after `strip().upper()`).
pub fn must_include(detail: Option<&str>) -> Vec<String> {
    for line in py_splitlines(detail.unwrap_or("")) {
        if py_strip(line).to_uppercase().starts_with("MUST INCLUDE:") {
            let body = line.split_once(':').map_or("", |(_, b)| b);
            return body
                .split(';')
                .map(py_strip)
                .filter(|v| !v.is_empty())
                .map(str::to_string)
                .collect();
        }
    }
    Vec::new()
}

/// `_missing_values`: the required values a lesson does not contain,
/// compared lowercased.
pub fn missing_values(lesson: &str, must: &[String]) -> Vec<String> {
    let low = py_lower(lesson);
    must.iter()
        .filter(|v| !low.contains(&py_lower(v)))
        .cloned()
        .collect()
}

// ---------------------------------------------------------------------------
// Parsed records
// ---------------------------------------------------------------------------

/// One inferred outcome (`_parse_outcome_claims`' dict).
#[derive(Debug, Clone, PartialEq)]
pub struct OutcomeClaim {
    pub task: String,
    pub outcome: String,
    pub about: Option<String>,
    pub detail: Option<String>,
}

impl OutcomeClaim {
    pub fn to_json(&self) -> Value {
        json!({"task": self.task, "outcome": self.outcome, "about": self.about,
               "detail": self.detail})
    }
}

/// `LessonClaim` (`memory/dream.py:83-90`).
#[derive(Debug, Clone, PartialEq)]
pub struct LessonClaim {
    pub task: String,
    pub aspect: String,
    pub lesson: String,
    pub about: Option<String>,
    pub polarity: String,
    pub outcome: String,
    pub confidence: f64,
}

impl LessonClaim {
    pub fn to_json(&self) -> Value {
        json!({"task": self.task, "aspect": self.aspect, "lesson": self.lesson,
               "about": self.about, "polarity": self.polarity, "outcome": self.outcome,
               "confidence": self.confidence})
    }
}

/// What `extract_rules` returns, with the per-call lists it leaves on the
/// extractor (`last_rule_failed_ids`, `last_rule_empty_ids`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Rules {
    pub claims: Vec<LessonClaim>,
    pub failed_ids: Vec<i64>,
    pub empty_ids: Vec<i64>,
}

/// An `ExtractorError` raised by the extractor's own checks (no transport
/// chain): its message is the whole `str(exc)`.
fn raised_here(message: &str) -> ExtractorError {
    ExtractorError {
        message: message.to_string(),
        auth_failure: false,
        redacted_auth_words: false,
        cause: Cause::Other("ExtractorError".into()),
    }
}

/// `str(c.get(key, "") or "").strip() or None`.
fn opt_field(obj: &Map<String, Value>, key: &str) -> Option<String> {
    match obj.get(key) {
        Some(v) if truthy(v) => {
            let s = py_strip(&super::extractor::py_str(v)).to_string();
            (!s.is_empty()).then_some(s)
        }
        _ => None,
    }
}

/// `str(c.get(key, default)).strip()`.
fn field_or(obj: &Map<String, Value>, key: &str, default: &str) -> String {
    match obj.get(key) {
        None => default.to_string(),
        Some(_) => field(obj, key),
    }
}

/// `content[s:e + 1]` from the first `{` to the last `}`, or None when the
/// reply has no such span.
fn object_span(content: &str) -> Option<&str> {
    match (content.find('{'), content.rfind('}')) {
        (Some(s), Some(e)) if e > s => Some(&content[s..=e]),
        _ => None,
    }
}

/// `json.loads` under `except ValueError`: None for a decode failure, the
/// escaping `RecursionError` as an uncaught exception.
fn loads_value_error(text: &str) -> Result<Option<Value>, Uncaught> {
    match loads(text) {
        Ok(v) => Ok(Some(v)),
        Err(r) if r.cause == Cause::Other("RecursionError".into()) => Err(Uncaught {
            type_name: "RecursionError",
            message: r.text,
        }),
        Err(_) => Ok(None),
    }
}

/// `_parse_outcome_claims(content, cap)`: None = malformed (retryable),
/// an empty list = nothing found, else at most `cap` claims; enum
/// violations dropped.
pub fn parse_outcome_claims(
    content: &str,
    cap: i64,
) -> Result<Option<Vec<OutcomeClaim>>, Uncaught> {
    if cap <= 0 {
        return Ok(Some(Vec::new()));
    }
    let Some(span) = object_span(content) else {
        return Ok(None);
    };
    let Some(parsed) = loads_value_error(span)? else {
        return Ok(None);
    };
    let Some(raw) = parsed.as_object().and_then(|m| m.get("outcomes")) else {
        return Ok(None);
    };
    let Some(items) = raw.as_array() else {
        return Ok(None);
    };
    let mut out = Vec::new();
    for c in items {
        let Some(c) = c.as_object() else { continue };
        let task = field(c, "task");
        let outcome = field(c, "outcome");
        if task.is_empty() || !OUTCOMES.contains(&outcome.as_str()) {
            continue;
        }
        out.push(OutcomeClaim {
            task,
            outcome,
            about: opt_field(c, "about"),
            detail: opt_field(c, "detail"),
        });
        if out.len() as i64 >= cap {
            break;
        }
    }
    Ok(Some(out))
}

/// The phrases of the digest prompt itself (`_DIGEST_PROMPT_ECHO`).
fn digest_echo() -> &'static [regex::Regex] {
    static RE: std::sync::OnceLock<Vec<regex::Regex>> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        [
            r"\bmissing detail\b.{0,100}\bcheap\b.{0,100}\binvented one\b.{0,100}\bpoison",
            concat!(
                r"\bone compact narrative paragraph\b.{0,100}\bno headings\b.{0,40}\bbullet lists\b",
                r"|\bno headings\b.{0,40}\bbullet lists\b.{0,100}\bone compact narrative paragraph\b"
            ),
            r"\bpast tense anchored to the session\b",
            r"\bhistory,? never as a claim about the present\b",
            r"\bwhat the session set out to do\b.{0,100}\bphases or steps\b",
        ]
        .iter()
        .map(|p| regex::Regex::new(&format!("(?is){p}")).expect("digest echo pattern"))
        .collect()
    })
}

/// `" ".join(s.split())`: Python's whitespace runs collapsed.
fn collapse_ws(s: &str) -> String {
    s.split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// `_parse_digest(content, context_text=...)`: None = malformed or blank
/// (retryable), or a digest echoing the prompt's own phrases that the
/// session record does not carry.
pub fn parse_digest(content: &str, context: Option<&str>) -> Result<Option<String>, Uncaught> {
    let Some(span) = object_span(content) else {
        return Ok(None);
    };
    let Some(parsed) = loads_value_error(span)? else {
        return Ok(None);
    };
    let Some(Value::String(digest)) = parsed.as_object().and_then(|m| m.get("digest")) else {
        return Ok(None);
    };
    if py_strip(digest).is_empty() {
        return Ok(None);
    }
    if let Some(context) = context {
        let words = collapse_ws(digest);
        let context_words = collapse_ws(context);
        let echo = !crate::mutants::active("mcp-dream-stage-digest-echo-unchecked")
            && digest_echo()
                .iter()
                .any(|p| p.is_match(&words) && !p.is_match(&context_words));
        if echo {
            return Ok(None);
        }
    }
    Ok(Some(py_strip(digest).to_string()))
}

/// Why `_lesson_claims` raised: the rule contract's own `ExtractorError`,
/// or an exception escaping it.
#[derive(Debug, Clone, PartialEq)]
pub enum ClaimsError {
    Rule(ExtractorError),
    Uncaught(Uncaught),
}

/// `_lesson_claims(raw, force_aspect=...)`: clustering repairs enum
/// violations; a rule refuses them, and only the first rule is kept.
pub fn lesson_claims(raw: &[Value], rule: bool) -> Result<Vec<LessonClaim>, ClaimsError> {
    let mut out = Vec::new();
    for c in raw {
        let Some(c) = c.as_object() else { continue };
        let task = field(c, "task");
        let lesson = field(c, "lesson");
        if task.is_empty() || lesson.is_empty() {
            continue;
        }
        let aspect = if rule {
            "rule".to_string()
        } else {
            // `str(c.get("aspect", "") or "lesson").strip() or "lesson"`.
            let raw = match c.get("aspect") {
                Some(v) if truthy(v) => super::extractor::py_str(v),
                _ => "lesson".to_string(),
            };
            let s = py_strip(&raw).to_string();
            if s.is_empty() {
                "lesson".to_string()
            } else {
                s
            }
        };
        let about = opt_field(c, "about");
        if rule
            && (!OUTCOMES.contains(&field_or(c, "outcome", "").as_str())
                || !["+", "-"].contains(&field_or(c, "polarity", "").as_str()))
        {
            return Err(ClaimsError::Rule(raised_here(
                "rule has invalid outcome or polarity",
            )));
        }
        let polarity = if field_or(c, "polarity", "+") == "-" {
            "-"
        } else {
            "+"
        };
        let mut outcome = field_or(c, "outcome", "success");
        if !OUTCOMES.contains(&outcome.as_str()) {
            outcome = "success".into();
        }
        let conf = confidence(c, 0.6).map_err(ClaimsError::Uncaught)?;
        out.push(LessonClaim {
            task,
            aspect,
            lesson,
            about,
            polarity: polarity.into(),
            outcome,
            confidence: conf,
        });
        if rule {
            break;
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// split_session_context
// ---------------------------------------------------------------------------

/// `split_session_context(text, cap)`: sequential segments of at most
/// `cap` characters, on line boundaries where possible (a longer line is
/// hard-split), order kept.
pub fn split_session_context(text: &str, cap: i64) -> Vec<String> {
    let cap = usize::try_from(cap.max(1)).unwrap_or(usize::MAX);
    if text.chars().count() <= cap {
        return vec![text.to_string()];
    }
    let mut parts: Vec<String> = Vec::new();
    let mut current: Vec<String> = Vec::new();
    let mut length = 0usize;
    for line in text.split('\n') {
        let mut line: Vec<char> = line.chars().collect();
        while line.len() > cap {
            if !current.is_empty() {
                parts.push(current.join("\n"));
                current.clear();
                length = 0;
            }
            parts.push(line[..cap].iter().collect());
            line = line[cap..].to_vec();
        }
        let mut extra = line.len() + usize::from(!current.is_empty());
        if length + extra > cap && !current.is_empty() {
            parts.push(current.join("\n"));
            current.clear();
            length = 0;
            extra = line.len();
        }
        current.push(line.into_iter().collect());
        length += extra;
    }
    if !current.is_empty() {
        parts.push(current.join("\n"));
    }
    parts
}

// ---------------------------------------------------------------------------
// The calls
// ---------------------------------------------------------------------------

impl Compat {
    /// The `infer_outcomes` request body.
    pub fn infer_body(&self, context: &str) -> String {
        self.body(prompts::OUTCOME_INFER_SYSTEM_PROMPT, context)
    }

    /// `infer_outcomes(context_text, cap=...)`: a transport failure is an
    /// `ExtractorError`, a malformed reply `Ok(None)`.
    pub async fn infer_outcomes(
        &self,
        context: &str,
        cap: i64,
    ) -> Result<Option<Vec<OutcomeClaim>>, ExtractFailure> {
        let content = self
            .content(self.infer_body(context))
            .await
            .map_err(|e| e.label("infer_outcomes", false))?;
        Ok(parse_outcome_claims(&content, cap)?)
    }

    /// `_lessons_completion(system, user)`: the reply's `lessons` list, or
    /// an empty one for any other shape.
    async fn lessons_completion(
        &self,
        system: &str,
        user: &str,
    ) -> Result<Vec<Value>, ExtractFailure> {
        let parsed = self
            .completion(self.body(system, user))
            .await
            .map_err(|e| e.label("extract_lessons", false))?;
        Ok(match parsed.as_object().and_then(|m| m.get("lessons")) {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        })
    }

    /// The clustering request body (`extract_lessons`).
    pub fn lessons_body(&self, signals: &[Signal]) -> String {
        self.body(prompts::LESSON_SYSTEM_PROMPT, &format_signals(signals))
    }

    /// `extract_lessons(signals)`: one batched call under the clustering
    /// prompt.
    pub async fn extract_lessons(
        &self,
        signals: &[Signal],
    ) -> Result<Vec<LessonClaim>, ExtractFailure> {
        if signals.is_empty() {
            return Ok(Vec::new());
        }
        let raw = self
            .lessons_completion(prompts::LESSON_SYSTEM_PROMPT, &format_signals(signals))
            .await?;
        lesson_claims(&raw, false).map_err(|e| match e {
            ClaimsError::Rule(x) => ExtractFailure::Extractor(x),
            ClaimsError::Uncaught(u) => ExtractFailure::Uncaught(u),
        })
    }

    /// One rule call under the rule prompt.
    async fn rule_call(&self, user: &str) -> Result<Vec<LessonClaim>, ExtractFailure> {
        let raw = self
            .lessons_completion(prompts::RULE_LESSON_SYSTEM_PROMPT, user)
            .await?;
        lesson_claims(&raw, true).map_err(|e| match e {
            ClaimsError::Rule(x) => ExtractFailure::Extractor(x),
            ClaimsError::Uncaught(u) => ExtractFailure::Uncaught(u),
        })
    }

    /// `extract_rules(signals)`: one call per signal, a retry naming the
    /// required values a rule dropped, per-signal failures listed; only a
    /// batch whose every call failed raises. An exception other than an
    /// `ExtractorError` escapes the whole call, as in Python.
    pub async fn extract_rules(&self, signals: &[Signal]) -> Result<Rules, ExtractFailure> {
        let mut out = Rules::default();
        let mut first_error: Option<String> = None;
        for s in signals {
            let user = format_signals(std::slice::from_ref(s));
            let must = must_include(s.detail.as_deref());
            let attempt: Result<Vec<LessonClaim>, ExtractFailure> = async {
                let mut claims = self.rule_call(&user).await?;
                if !must.is_empty() && !claims.is_empty() {
                    let missing = missing_values(&claims[0].lesson, &must);
                    if !missing.is_empty()
                        && !crate::mutants::active("mcp-dream-stage-rule-no-retry")
                    {
                        let nudge = format!(
                            "{user}\n\nYour previous rule omitted these values, which must \
                             appear verbatim: {}. Reply again with the complete rule.",
                            missing.join("; ")
                        );
                        claims = self.rule_call(&nudge).await?;
                        if claims.is_empty() {
                            return Err(raised_here("rule retry returned no rule").into());
                        }
                    }
                }
                if let Some(first) = claims.first() {
                    if !missing_values(&first.lesson, &must).is_empty() {
                        return Err(raised_here("rule omitted required values").into());
                    }
                    let polarity = match nonempty(&s.polarity) {
                        Some(p) => Some(p),
                        None => match s.outcome.as_str() {
                            "success" => Some("+"),
                            "failure" => Some("-"),
                            _ => None,
                        },
                    };
                    if first.outcome != s.outcome || polarity.is_some_and(|p| first.polarity != p) {
                        return Err(raised_here("rule contradicts the source verdict").into());
                    }
                }
                Ok(claims)
            }
            .await;
            let claims = match attempt {
                Ok(claims) => claims,
                Err(ExtractFailure::Extractor(e)) => {
                    out.failed_ids.push(s.id);
                    first_error.get_or_insert(e.message);
                    continue;
                }
                Err(uncaught) => return Err(uncaught),
            };
            if claims.is_empty() {
                out.empty_ids.push(s.id);
            }
            if let Some(mut c) = claims.into_iter().next() {
                c.about = strip_rule_prefix(c.about.as_deref())
                    .or_else(|| strip_rule_prefix(s.about.as_deref()));
                out.claims.push(c);
            }
        }
        if !signals.is_empty() && out.failed_ids.len() == signals.len() {
            return Err(raised_here(&format!(
                "extract_rules failed for every signal: {}",
                first_error.unwrap_or_default()
            ))
            .into());
        }
        Ok(out)
    }

    /// `_DIGEST_SYSTEM_PROMPT.format(target_chars=int(target_chars))`.
    pub fn digest_system(target_chars: i64) -> String {
        format!(
            "{}{target_chars}{}",
            prompts::DIGEST_SYSTEM_PROMPT_HEAD,
            prompts::DIGEST_SYSTEM_PROMPT_TAIL
        )
    }

    /// The `summarize_session` request body.
    pub fn digest_body(&self, context: &str, target_chars: i64) -> String {
        self.body(&Self::digest_system(target_chars), context)
    }

    /// `summarize_session(context_text, target_chars=...)`: a transport
    /// failure is an `ExtractorError`, a malformed or blank reply `Ok(None)`.
    pub async fn summarize_session(
        &self,
        context: &str,
        target_chars: i64,
    ) -> Result<Option<String>, ExtractFailure> {
        let content = self
            .content(self.digest_body(context, target_chars))
            .await
            .map_err(|e| e.label("summarize_session", false))?;
        Ok(parse_digest(&content, Some(context))?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signal(id: i64, about: Option<&str>, detail: Option<&str>) -> Signal {
        Signal {
            id,
            task: "deploy".into(),
            outcome: "success".into(),
            about: about.map(str::to_string),
            detail: detail.map(str::to_string),
            polarity: None,
            origin: None,
            episode_id: None,
            created_at: 1.0,
            used_ids: None,
        }
    }

    #[test]
    fn signals_format_with_python_repr() {
        let mut a = signal(1, Some("it's"), Some("line\nbreak \u{378}"));
        a.polarity = Some("-".into());
        a.origin = Some("inferred".into());
        let mut b = signal(2, Some(""), None);
        b.task = "say \"hi\" and 'bye'".into();
        assert_eq!(
            format_signals(&[a, b]),
            "[machine-inferred] [success] task='deploy' about=\"it's\" \
             detail='line\\nbreak \\u0378' polarity=-\n\
             [success] task='say \"hi\" and \\'bye\\''"
        );
    }

    #[test]
    fn rule_routing_and_required_values() {
        assert!(is_rule_signal(
            &signal(1, Some("  RULE: checkout"), None),
            false
        ));
        assert!(!is_rule_signal(&signal(1, Some("rules"), None), false));
        assert!(is_rule_signal(&signal(1, None, None), true));
        assert_eq!(
            strip_rule_prefix(Some(" Rule:  checkout ")),
            Some("checkout".into())
        );
        assert_eq!(strip_rule_prefix(Some("rule:  ")), None);
        assert_eq!(strip_rule_prefix(None), None);
        assert_eq!(
            must_include(Some(
                "context\r\n  must include: Alpha; ;beta ;\nMUST INCLUDE: x"
            )),
            vec!["Alpha".to_string(), "beta".to_string()]
        );
        assert_eq!(must_include(Some("no list here")), Vec::<String>::new());
        let must = vec!["ALPHA".to_string(), "Gamma".to_string()];
        assert_eq!(missing_values("use alpha first", &must), vec!["Gamma"]);
    }

    #[test]
    fn session_context_splits_on_lines_and_hard_splits_long_ones() {
        assert_eq!(split_session_context("abc", 5), vec!["abc"]);
        assert_eq!(
            split_session_context("aa\nbb\ncc", 5),
            vec!["aa\nbb".to_string(), "cc".to_string()]
        );
        assert_eq!(
            split_session_context("abcdefgh\nij", 4),
            vec!["abcd".to_string(), "efgh".to_string(), "ij".to_string()]
        );
        // Expected values: Python's split_session_context.
        assert_eq!(
            split_session_context("x\nabcdefgh", 4),
            vec!["x".to_string(), "abcd".to_string(), "efgh".to_string()]
        );
        assert_eq!(split_session_context("é\né", 0), vec!["é", "é"]);
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

    fn signals(v: &Value) -> Vec<Signal> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|s| Signal {
                id: s["id"].as_i64().unwrap(),
                task: s["task"].as_str().unwrap().into(),
                outcome: s["outcome"].as_str().unwrap().into(),
                about: s["about"].as_str().map(str::to_string),
                detail: s["detail"].as_str().map(str::to_string),
                polarity: s["polarity"].as_str().map(str::to_string),
                origin: s["origin"].as_str().map(str::to_string),
                episode_id: s["episode_id"].as_str().map(str::to_string),
                created_at: s["created_at"].as_f64().unwrap(),
                used_ids: None,
            })
            .collect()
    }

    /// One reply per connection, in order, as the oracle's endpoint
    /// answered; each request body recorded.
    fn serve_queue(
        replies: Vec<(u16, Vec<u8>)>,
    ) -> (String, std::sync::Arc<std::sync::Mutex<Vec<String>>>) {
        use std::io::{Read, Write};
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let seen2 = seen.clone();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let mut replies = replies.into_iter();
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { return };
                let mut buf = Vec::new();
                let mut chunk = [0u8; 65536];
                let body_at;
                loop {
                    let n = s.read(&mut chunk).unwrap_or(0);
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..end]).to_ascii_lowercase();
                        let len = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .map_or(0, |v| v.trim().parse().unwrap_or(0));
                        if buf.len() >= end + 4 + len || n == 0 {
                            body_at = end + 4;
                            break;
                        }
                    } else if n == 0 {
                        return;
                    }
                }
                seen2
                    .lock()
                    .unwrap()
                    .push(String::from_utf8_lossy(&buf[body_at..]).into_owned());
                let (status, body) = replies.next().unwrap_or((
                    200,
                    br#"{"choices":[{"message":{"content":"{\"outcomes\": []}"}}]}"#.to_vec(),
                ));
                let reason = if status == 500 {
                    "Internal Server Error"
                } else {
                    "OK"
                };
                let head = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = s.write_all(head.as_bytes());
                let _ = s.write_all(&body);
            }
        });
        (format!("http://127.0.0.1:{port}/v1"), seen)
    }

    fn queue(case: &Value) -> Vec<(u16, Vec<u8>)> {
        case["queue"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| {
                let body = r[1]
                    .as_str()
                    .unwrap()
                    .chars()
                    .map(|c| c as u32 as u8)
                    .collect();
                (r[0].as_u64().unwrap() as u16, body)
            })
            .collect()
    }

    /// Run one golden call; `Ok(json text)` or `Err(str(exc))`.
    async fn call(ex: &Compat, case: &Value) -> Result<String, String> {
        let arg = &case["arg"];
        let text = |e: ExtractFailure| match e {
            ExtractFailure::Extractor(x) => x.message,
            ExtractFailure::Uncaught(u) => u.message,
        };
        let got: Value = match case["method"].as_str().unwrap() {
            "infer" => {
                let r = ex
                    .infer_outcomes(arg.as_str().unwrap(), case["extra"].as_i64().unwrap())
                    .await
                    .map_err(text)?;
                r.map_or(Value::Null, |v| {
                    Value::Array(v.iter().map(OutcomeClaim::to_json).collect())
                })
            }
            "lessons" => Value::Array(
                ex.extract_lessons(&signals(arg))
                    .await
                    .map_err(text)?
                    .iter()
                    .map(LessonClaim::to_json)
                    .collect(),
            ),
            "rules" => {
                let r = ex.extract_rules(&signals(arg)).await.map_err(text)?;
                json!({"claims": r.claims.iter().map(LessonClaim::to_json).collect::<Vec<_>>(),
                       "failed": r.failed_ids, "empty": r.empty_ids})
            }
            _ => json!(
                ex.summarize_session(arg.as_str().unwrap(), case["extra"].as_i64().unwrap())
                    .await
                    .map_err(text)?
            ),
        };
        Ok(crate::pyjson::dumps(&got))
    }

    #[tokio::test]
    async fn stage_bodies_match_python_byte_for_byte() {
        let g = golden();
        let cases = g["stage_bodies"].as_array().unwrap();
        assert_eq!(cases.len(), 10);
        for case in cases {
            let rules_queue = if case["method"] == "rules" {
                // The golden's reply sequence: a rule missing a value, then the
                // complete rule.
                let rule = |lesson: &str| {
                    let content = json!({"lessons": [{"task": "refund", "lesson": lesson,
                        "about": "rule: x", "polarity": "+", "outcome": "success",
                        "confidence": 0.9}]})
                    .to_string();
                    let env = json!({"choices": [{"message": {"content": content}}]});
                    (200u16, env.to_string().into_bytes())
                };
                vec![
                    rule("WHEN refund THEN use Alpha"),
                    rule("WHEN refund THEN use Alpha and BETA"),
                ]
            } else {
                Vec::new()
            };
            let (base, seen) = serve_queue(rules_queue);
            let ex = ctor(case["ctor"].as_str().unwrap(), &base);
            let _ = call(&ex, case).await;
            let want: Vec<String> = case["bodies"]
                .as_array()
                .unwrap()
                .iter()
                .map(|b| b.as_str().unwrap().to_string())
                .collect();
            assert_eq!(
                *seen.lock().unwrap(),
                want,
                "{} {}",
                case["ctor"],
                case["method"]
            );
        }
    }

    #[tokio::test]
    async fn stage_replies_parse_and_fail_as_python() {
        let g = golden();
        for case in g["stage_replies"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let (base, _) = serve_queue(queue(case));
            let got = call(&ctor("primary", &base), case).await;
            match (&case["ok"], &case["error"], got) {
                (Value::String(want), _, Ok(text)) => assert_eq!(&text, want, "{name}"),
                (_, Value::String(want), Err(text)) => assert_eq!(&text, want, "{name}"),
                (_, _, other) => panic!("{name}: {other:?}"),
            }
        }
    }

    #[test]
    fn digest_echo_is_refused_unless_the_record_carries_it() {
        let reply = "{\"digest\": \"In this session it was written in past tense anchored to \
                     the session.\"}";
        assert_eq!(parse_digest(reply, Some("plain record")), Ok(None));
        assert!(
            parse_digest(
                reply,
                Some("we use past   tense anchored to the session here")
            )
            .unwrap()
            .is_some()
        );
        assert_eq!(parse_digest("{\"digest\": \"  \"}", None), Ok(None));
        assert_eq!(
            parse_digest("x {\"digest\": \" ok \"} y", None),
            Ok(Some("ok".into()))
        );
    }
}
