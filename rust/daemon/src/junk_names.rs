//! `junk_name_reason` (`memory/graph_consolidation.py:511-552`): the
//! write-time gate that keeps an extraction artifact from becoming a graph
//! entity. `memory_fact_set` consults it before minting the subject's node
//! (`service._ensure_subject_entity`, `service.py:1859`).
//!
//! Python's `str` patterns are Unicode-aware: `\d` is any decimal digit and
//! `\s` is `str.isspace`, which also counts U+001C..U+001F; the classes below
//! spell those separators out because the `regex` crate's `\s` omits them.
//! Two more Python rules are spelled out: `re.IGNORECASE` also matches `i`
//! with U+0130 and U+0131 (sre's case fixes; the crate's simple folding has
//! neither), and `\b` bounds Python's word class (`isalnum` and `_`, so
//! letters and numbers, not marks or connectors), checked after the match.

use regex::Regex;
use std::sync::LazyLock;

/// Python `\s` / `\S` for `str` patterns.
const WS: &str = r"[\s\x1c-\x1f]";
const NWS: &str = r"[^\s\x1c-\x1f]";

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("junk-name regex")
}

/// `_JUNK_STOPWORDS`.
const STOPWORDS: [&str; 12] = [
    "live", "merged", "done", "fixed", "current", "ok", "pending", "wip", "todo", "n/a", "none",
    "null",
];

static BARE_NUMBER: LazyLock<Regex> = LazyLock::new(|| re(r"^\d+$"));
static COUNT_PREFIX: LazyLock<Regex> = LazyLock::new(|| re(&format!(r"^\d+{WS}")));
static BARE_DATE: LazyLock<Regex> = LazyLock::new(|| re(r"^\d{4}-\d{2}-\d{2}$"));
static DUMP_FILE: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\.sql(\.gz)?$"));
static IMAGE_TAG: LazyLock<Regex> = LazyLock::new(|| re(r":\d+\.\d+\.\d+"));
static COMMAND_STRING: LazyLock<Regex> = LazyLock::new(|| {
    re(&format!(
        r"(?i)^(docker|g[i\x{{130}}\x{{131}}]t|python|p[i\x{{130}}\x{{131}}]p|curl|pwsh|npm|pytest){WS}+{NWS}+{WS}+{NWS}"
    ))
});
/// `={WS}*[0-9a-f]{7,}\b`: the run without its `\b`, which `hash_status`
/// checks against Python's word class.
static HASH_RUN: LazyLock<Regex> = LazyLock::new(|| re(&format!(r"={WS}*[0-9a-f]{{7,}}")));
/// Python's `\w` for `str` patterns: `str.isalnum` (letters and numbers) or `_`.
static PY_WORD: LazyLock<Regex> = LazyLock::new(|| re(r"^[\p{L}\p{N}_]$"));
static ACTION_PREFIX: LazyLock<Regex> =
    LazyLock::new(|| re(&format!(r"(?i)^act[i\x{{130}}\x{{131}}]on:{WS}")));
static STATUS_SHARD: LazyLock<Regex> = LazyLock::new(|| re(r"^P\d+[ _]"));
/// `_SENTENCE_TOKENS`.
const SENTENCE_TOKENS: usize = 7;
static DECIMAL_OR_RANGE: LazyLock<Regex> = LazyLock::new(|| re(r"^\d+\.\d+(?:-\d+(?:\.\d+)?)?$"));
static LOWER_TOKEN: LazyLock<Regex> = LazyLock::new(|| re(r"^[a-z][a-z0-9_-]*$"));
/// `_ARROW`: a relation separator captured into a name.
static ARROW: LazyLock<Regex> = LazyLock::new(|| re("<-+>|\u{2194}|->|\u{2192}"));

/// The `hash-status` gate: a hex run of 7 or more after `=`, ending at a
/// Python word boundary. The run is greedy and its last character is a word
/// character, so only the character after the full run decides.
fn hash_status(d: &str) -> bool {
    HASH_RUN.find_iter(d).any(|m| {
        d[m.end()..]
            .chars()
            .next()
            .is_none_or(|c| !PY_WORD.is_match(c.encode_utf8(&mut [0; 4])))
    })
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(crate::stores::cortex::py_isspace)
}

/// Python `str.split()`: runs of `str.isspace` characters, no empty parts.
fn py_split(s: &str) -> Vec<&str> {
    s.split(crate::stores::cortex::py_isspace)
        .filter(|t| !t.is_empty())
        .collect()
}

/// `_is_concat_artifact`: two names joined by a relation arrow.
pub(crate) fn is_concat_artifact(name: &str) -> bool {
    let parts: Vec<&str> = ARROW.split(name).map(py_strip).collect();
    parts.len() >= 2 && parts.iter().filter(|p| !p.is_empty()).count() >= 2
}

/// `_is_metric_reading`: 2-3 tokens, a decimal or decimal-range tail, every
/// other token lower-case ("stale 0.8").
pub(crate) fn is_metric_reading(name: &str) -> bool {
    let toks = py_split(name);
    if !(2..=3).contains(&toks.len()) || !DECIMAL_OR_RANGE.is_match(toks[toks.len() - 1]) {
        return false;
    }
    toks[..toks.len() - 1]
        .iter()
        .all(|t| LOWER_TOKEN.is_match(t))
}

/// `_split_outside_parens`: comma-separated segments outside brackets.
fn split_outside_parens(s: &str) -> Vec<&str> {
    let mut parts = Vec::new();
    let mut depth = 0usize;
    let mut start = 0usize;
    for (i, ch) in s.char_indices() {
        if "([{".contains(ch) {
            depth += 1;
        } else if ")]}".contains(ch) {
            depth = depth.saturating_sub(1);
        }
        if ch == ',' && depth == 0 {
            parts.push(&s[start..i]);
            start = i + 1;
        }
    }
    parts.push(&s[start..]);
    parts.into_iter().map(py_strip).collect()
}

/// `_is_list_artifact`: at least two non-empty segments outside brackets.
pub(crate) fn is_list_artifact(name: &str) -> bool {
    split_outside_parens(name)
        .iter()
        .filter(|p| !p.is_empty())
        .count()
        >= 2
}

/// `junk_name_reason(name)`: why `name` must never become a graph entity,
/// or None.
pub fn junk_name_reason(name: &str) -> Option<&'static str> {
    let d = py_strip(name);
    if d.is_empty() {
        return Some("empty");
    }
    if is_concat_artifact(d) {
        return Some("concat-artifact");
    }
    if BARE_NUMBER.is_match(d) {
        return Some("bare-number");
    }
    if STOPWORDS.contains(&d.to_lowercase().as_str()) {
        return Some("status-word");
    }
    if BARE_DATE.is_match(d) {
        return Some("bare-date");
    }
    if COUNT_PREFIX.is_match(d) {
        return Some("count-prefix");
    }
    if DUMP_FILE.is_match(d) {
        return Some("dump-file");
    }
    if IMAGE_TAG.is_match(d) {
        return Some("image-tag");
    }
    if COMMAND_STRING.is_match(d) {
        return Some("command-string");
    }
    if hash_status(d) {
        return Some("hash-status");
    }
    if ACTION_PREFIX.is_match(d) {
        return Some("action-prefix");
    }
    if STATUS_SHARD.is_match(d) {
        return Some("status-shard");
    }
    if is_metric_reading(d) {
        return Some("metric-reading");
    }
    if is_list_artifact(d) {
        return Some("list-artifact");
    }
    if py_split(d).len() >= SENTENCE_TOKENS {
        return Some("sentence");
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reasons from `graph_consolidation.junk_name_reason` on CPython 3.11
    /// (`harness/goldens/w2e-junk-names.json` lists the same cases).
    pub(crate) const GOLDEN: &str = include_str!("../harness/goldens/w2e-junk-names.json");

    #[test]
    fn junk_name_reason_matches_python() {
        let cases: Vec<(String, Option<String>)> = serde_json::from_str(GOLDEN).unwrap();
        assert!(cases.len() >= 60, "golden too small: {}", cases.len());
        let mut bad = Vec::new();
        for (name, want) in &cases {
            let got = junk_name_reason(name);
            if got != want.as_deref() {
                bad.push(format!("{name:?}: rust {got:?}, python {want:?}"));
            }
        }
        assert!(
            bad.is_empty(),
            "{} mismatches:\n{}",
            bad.len(),
            bad.join("\n")
        );
    }
}
