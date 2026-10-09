//! Structured slot extraction (`memory/slots.py`), spec W1.6.
//!
//! The regexes are written in Python syntax and translated by [`PyRegex`],
//! because Python's `re` str patterns differ from the regex crate in four
//! places: `\w` is `str.isalnum()` plus `_` (letters and numbers; no marks,
//! no connector punctuation but `_`), `\s` adds U+001C..U+001F to
//! White_Space, `\b` is built on that `\w`, and `re.IGNORECASE` also matches
//! `ı` (U+0131) and `İ` (U+0130) to `i`. The golden
//! (`harness/goldens/w2e-slots.json`) checks each class over every code point.
//! Unicode-version gap: the regex crate's tables (Unicode 16) classify code
//! points that Python's (3.11: 14.0, 3.12: 15.0) leaves unassigned.
#![allow(dead_code)]

use crate::http::py_strip;
use fancy_regex::{Regex, RegexBuilder};
use serde_json::Value;
use std::collections::HashSet;
use std::sync::LazyLock;

/// One `(entity, attribute, value, polarity)` triple (slots.py:37-44).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slot {
    pub entity: String,
    pub attribute: String,
    pub value: String,
    pub polarity: String,
}

impl Slot {
    fn new(entity: &str, attribute: &str, value: &str) -> Slot {
        Slot {
            entity: entity.to_string(),
            attribute: attribute.to_string(),
            value: value.to_string(),
            polarity: "+".to_string(),
        }
    }

    /// The stored JSONB row `[entity, attribute, value, polarity]`.
    pub fn to_json(&self) -> Value {
        Value::Array(
            [&self.entity, &self.attribute, &self.value, &self.polarity]
                .map(|s| Value::String(s.clone()))
                .to_vec(),
        )
    }

    /// A stored row back; None unless it is a 4-element array of strings.
    pub fn from_json(v: &Value) -> Option<Slot> {
        let [e, a, val, p] = v.as_array()?.as_slice() else {
            return None;
        };
        Some(Slot {
            entity: e.as_str()?.to_string(),
            attribute: a.as_str()?.to_string(),
            value: val.as_str()?.to_string(),
            polarity: p.as_str()?.to_string(),
        })
    }
}

// ---- Python `re` compatibility ----------------------------------------------

/// Python `\w` for str patterns, as a class body.
const PY_W: &str = r"\p{L}\p{N}_";
/// Python `\s` for str patterns (`str.isspace`), as a class body.
const PY_S: &str = r"\s\x1C-\x1F";

/// Code points where the regex crate's `\w`, which its `\b` is built on, and
/// Python's `\w` disagree: marks, connector punctuation but `_`, ZWNJ/ZWJ,
/// other-alphabetic symbols (`Ⓐ`), `No` numbers (`²`, `½`).
static DIVERGENT: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(&format!(r"[[\w--[{PY_W}]][[{PY_W}]--\w]]")).expect("divergent class")
});

/// A Python `re` str pattern, compiled twice: on the regex crate with its own
/// `\b`, and on fancy-regex with Python's `\b` built from lookaround. Both
/// see the same boundaries in a text with no [`DIVERGENT`] code point, so
/// the backtracking engine runs only on texts with one. Measured 2026-10-09
/// (release, a warm contradiction scan over 4,236 docs texts): 1.3 s with
/// fancy-regex alone, 13 ms with the fast engine.
pub(crate) struct PyRegex {
    fast: regex::Regex,
    /// None when the pattern has no `\b`: the fast engine is then exact.
    exact: Option<Regex>,
}

impl PyRegex {
    /// `ignorecase` / `dotall` are `re.IGNORECASE` / `re.DOTALL`.
    pub(crate) fn new(pattern: &str, ignorecase: bool, dotall: bool) -> PyRegex {
        let (src, _) = py_translate(pattern, ignorecase, dotall, r"\b");
        let fast = regex::Regex::new(&src)
            .unwrap_or_else(|e| panic!("bad translated pattern {src:?}: {e}"));
        let py_b = format!("(?-i:(?<![{PY_W}])(?=[{PY_W}])|(?<=[{PY_W}])(?![{PY_W}]))");
        let (src, has_b) = py_translate(pattern, ignorecase, dotall, &py_b);
        let exact = has_b.then(|| {
            RegexBuilder::new(&src)
                // Bounded patterns; the default 1M limit would turn a long
                // text into a silent non-match.
                .backtrack_limit(usize::MAX)
                .build()
                .unwrap_or_else(|e| panic!("bad translated pattern {src:?}: {e}"))
        });
        PyRegex { fast, exact }
    }

    fn exact_for(&self, text: &str) -> Option<&Regex> {
        self.exact.as_ref().filter(|_| DIVERGENT.is_match(text))
    }

    /// `pattern.search(text) is not None`. fancy-regex errors (unreachable
    /// with no backtrack limit) count as no match.
    pub(crate) fn is_match(&self, text: &str) -> bool {
        match self.exact_for(text) {
            Some(re) => re.is_match(text).unwrap_or(false),
            None => self.fast.is_match(text),
        }
    }

    /// `pattern.finditer(text)`: each match's groups by index (0 = whole).
    pub(crate) fn captures_all<'t>(&self, text: &'t str) -> Vec<Vec<Option<&'t str>>> {
        match self.exact_for(text) {
            Some(re) => re
                .captures_iter(text)
                .filter_map(Result::ok)
                .map(|c| (0..c.len()).map(|i| c.get(i).map(|m| m.as_str())).collect())
                .collect(),
            None => self
                .fast
                .captures_iter(text)
                .map(|c| c.iter().map(|m| m.map(|m| m.as_str())).collect())
                .collect(),
        }
    }

    /// `pattern.findall(text)` for a pattern without groups, as spans.
    pub(crate) fn find_spans(&self, text: &str) -> Vec<(usize, usize)> {
        match self.exact_for(text) {
            Some(re) => re
                .find_iter(text)
                .filter_map(Result::ok)
                .map(|m| (m.start(), m.end()))
                .collect(),
            None => self
                .fast
                .find_iter(text)
                .map(|m| (m.start(), m.end()))
                .collect(),
        }
    }

    pub(crate) fn findall<'t>(&self, text: &'t str) -> Vec<&'t str> {
        self.find_spans(text)
            .into_iter()
            .map(|(s, e)| &text[s..e])
            .collect()
    }

    /// `pattern.sub(rep, text)` for a pattern without `\b`.
    pub(crate) fn replace_all<'t>(&self, text: &'t str, rep: &str) -> std::borrow::Cow<'t, str> {
        assert!(self.exact.is_none(), "replace_all is fast-engine only");
        self.fast.replace_all(text, regex::NoExpand(rep))
    }
}

/// Translate a Python `re` str pattern to regex-crate syntax with Python's
/// classes, `\b` as `boundary`; also says whether the pattern had a `\b`.
/// `\w` is case-locked (`(?-i:`): under `(?i)` the regex crate closes `\p{L}`
/// over case folding and would admit U+0345, which Python's `\w` excludes.
/// Python's `$` also matches before a final `\n`; the one pattern using it
/// runs on `splitlines()` output, so `$` is kept.
fn py_translate(pattern: &str, ignorecase: bool, dotall: bool, boundary: &str) -> (String, bool) {
    let mut has_b = false;
    let mut out = String::new();
    if ignorecase {
        out.push_str("(?i)");
    }
    if dotall {
        out.push_str("(?s)");
    }
    let cs: Vec<char> = pattern.chars().collect();
    let mut i = 0;
    // Source of the character class being copied, if inside one.
    let mut class: Option<Vec<char>> = None;
    while i < cs.len() {
        let c = cs[i];
        if c == '\\' {
            let n = cs[i + 1];
            i += 2;
            match (n, class.as_mut()) {
                ('w', None) => out.push_str(&format!("(?-i:[{PY_W}])")),
                ('s', None) => out.push_str(&format!("[{PY_S}]")),
                ('S', None) => out.push_str(&format!("[^{PY_S}]")),
                ('b', None) => {
                    has_b = true;
                    out.push_str(boundary);
                }
                ('w', Some(_)) if !ignorecase => out.push_str(PY_W),
                ('s', Some(_)) => out.push_str(PY_S),
                ('w' | 'S' | 'b', Some(_)) => panic!("unsupported escape in class: {pattern}"),
                (_, cls) => {
                    out.push('\\');
                    out.push(n);
                    if let Some(cls) = cls {
                        cls.extend(['\\', n]);
                    }
                }
            }
            continue;
        }
        i += 1;
        match class.as_mut() {
            Some(cls) if c == ']' && !cls.is_empty() => {
                if ignorecase {
                    assert!(cls[0] != '^', "negated class under IGNORECASE: {pattern}");
                    if class_covers_i(cls) {
                        out.push_str("\u{131}\u{130}");
                    }
                }
                out.push(']');
                class = None;
            }
            Some(cls) => {
                cls.push(c);
                out.push(c);
            }
            None if c == '[' => {
                class = Some(Vec::new());
                out.push('[');
            }
            None if c == '(' && cs[i..].starts_with(&['?', 'P', '<']) => {
                // Copy a group name through, untouched by the `i` rewrite.
                let end = i + cs[i..].iter().position(|&x| x == '>').expect("group name");
                out.push('(');
                out.extend(&cs[i..=end]);
                i = end + 1;
            }
            None if ignorecase && (c == 'i' || c == 'I') => out.push_str("[iI\u{131}\u{130}]"),
            None => out.push(c),
        }
    }
    (out, has_b)
}

/// Does a class body (Python source) contain `i`/`I` or a range over one?
fn class_covers_i(cls: &[char]) -> bool {
    let mut j = 0;
    while j < cls.len() {
        if cls[j] == '\\' {
            j += 2;
            continue;
        }
        if j + 2 < cls.len() && cls[j + 1] == '-' {
            let (lo, hi) = (cls[j], cls[j + 2]);
            if (lo..=hi).contains(&'i') || (lo..=hi).contains(&'I') {
                return true;
            }
            j += 3;
            continue;
        }
        if cls[j] == 'i' || cls[j] == 'I' {
            return true;
        }
        j += 1;
    }
    false
}

/// Python `str.splitlines()`: no trailing empty piece, `\r\n` is one break.
pub(crate) fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let brk = matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if brk {
            out.push(&s[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && it.peek().is_some_and(|&(_, n)| n == '\n') {
                it.next();
                start += 1;
            }
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

// ---- Patterns (slots.py:55-121) ---------------------------------------------

static NAMED: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(
        r"\b(?:named|called)\s+(?P<name>[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)?)",
        false,
        false,
    )
});

static HAVE_TYPE: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(
        concat!(
            r"\bI\s+(?:have|own|got|drive)\s+a\s+",
            r"(?:(?P<attr>[A-Z][A-Za-z]+|[a-z]+)\s+)?",
            r"(?P<type>cat|dog|car|laptop|phone|gpu|graphics card|computer|bike)",
        ),
        true,
        false,
    )
});

const PRONOUN_GENDER: [(&str, &str); 11] = [
    ("him", "male"),
    ("his", "male"),
    ("he", "male"),
    ("her", "female"),
    ("hers", "female"),
    ("she", "female"),
    ("they", "nonbinary"),
    ("them", "nonbinary"),
    ("their", "nonbinary"),
    ("it", "neuter"),
    ("its", "neuter"),
];

static LOSS: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(
        concat!(
            r"\b(?:giv(?:e|es|ing|en)|gave|",
            r"sell|sells|selling|sold|",
            r"los(?:e|es|ing|t)|",
            r"rehom(?:e|es|ing|ed)|",
            r"donat(?:e|es|ing|ed)|",
            r"no\s+longer\s+have|",
            r"used\s+to\s+(?:have|own)|",
            r"got\s+rid\s+of|",
            r"thr[eo]w(?:s|ing|n)?\s+(?:away|out)|threw\s+(?:away|out))\b",
        ),
        true,
        false,
    )
});

static PRONOUN_TOKEN: LazyLock<PyRegex> =
    LazyLock::new(|| PyRegex::new(r"\b([A-Za-z]+)\b", false, false));

const ATTR_LEXICON: [&str; 47] = [
    "default",
    "port",
    "version",
    "timeout",
    "host",
    "hostname",
    "address",
    "ip",
    "path",
    "dir",
    "directory",
    "folder",
    "branch",
    "language",
    "status",
    "name",
    "url",
    "endpoint",
    "model",
    "device",
    "scope",
    "threshold",
    "capacity",
    "schema",
    "license",
    "framework",
    "editor",
    "shell",
    "os",
    "database",
    "db",
    "username",
    "email",
    "timezone",
    "gpu",
    "cpu",
    "machine",
    "server",
    "repo",
    "repository",
    "transport",
    "protocol",
    "format",
    "engine",
    "runtime",
    "interval",
    "limit",
];
const ARTICLES: [&str; 7] = ["the", "a", "an", "our", "this", "that", "its"];
const VALUE_STOPWORDS: [&str; 7] = ["that", "because", "when", "why", "how", "what", "if"];
const NEGATION_FIRST: [&str; 6] = ["not", "no", "never", "n't", "isn't", "aren't"];

static FENCE: LazyLock<PyRegex> = LazyLock::new(|| PyRegex::new(r"```.*?```", false, true));
static KV_LINE: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(
        r"^(?P<key>[A-Za-z][\w.\- ]{0,59}?)\s*[:=]\s*(?P<value>\S.{0,79})$",
        false,
        false,
    )
});
static COPULA: LazyLock<PyRegex> = LazyLock::new(|| PyRegex::new(r"\bis\b", true, false));
/// `_SENT_SPLIT_RE` (`(?<=[.!?])\s+|;\s*`) without its lookbehind; see
/// [`sentences`].
static SENT_SEP: LazyLock<PyRegex> = LazyLock::new(|| PyRegex::new(r"\s+|;\s*", false, false));
static DEV_TOKEN: LazyLock<PyRegex> = LazyLock::new(|| PyRegex::new(r"[\w.\-/\\']+", false, false));

// ---- Dev-fact extraction (slots.py:124-227) ---------------------------------

fn lower_in(tok: &str, set: &[&str]) -> bool {
    set.contains(&tok.to_lowercase().as_str())
}

/// `_attr_suffix`: index where a 1-2 word lexicon suffix starts.
fn attr_suffix(tokens: &[&str]) -> Option<usize> {
    let n = tokens.len();
    if n > 0 && lower_in(tokens[n - 1], &ATTR_LEXICON) {
        if n >= 2 && lower_in(tokens[n - 2], &ATTR_LEXICON) {
            return Some(n - 2);
        }
        return Some(n - 1);
    }
    None
}

/// `_clean_entity`; `ent[:-2]` slices code points of the original, after
/// testing the (possibly longer) lowercase.
fn clean_entity(tokens: &[&str]) -> String {
    let skip = tokens.iter().take_while(|t| lower_in(t, &ARTICLES)).count();
    let tokens = &tokens[skip..];
    if tokens.is_empty() || tokens.len() > 4 {
        return String::new();
    }
    let ent = tokens.join(" ");
    if ent.to_lowercase().ends_with("'s") {
        let cut = ent.char_indices().rev().nth(1).map_or(0, |(i, _)| i);
        return ent[..cut].to_string();
    }
    ent
}

/// `_clean_value`.
fn clean_value(raw: &str) -> String {
    let v = py_strip(py_strip(raw).trim_end_matches(['.', ',', ';', '!']));
    let first = v.split(' ').next().unwrap_or("").to_lowercase();
    if v.is_empty()
        || v.chars().count() > 80
        || VALUE_STOPWORDS.contains(&first.as_str())
        || NEGATION_FIRST.contains(&first.as_str())
    {
        return String::new();
    }
    v.to_string()
}

/// `_dev_slot_from_copula`: `<entity> <attr> is <value>` and the my /
/// possessive / of forms.
fn dev_slot_from_copula(sent: &str) -> Option<Slot> {
    if sent.contains('?') {
        return None;
    }
    let (start, end) = *COPULA.find_spans(sent).first()?;
    let value = clean_value(&sent[end..]);
    if value.is_empty() {
        return None;
    }
    let subject = DEV_TOKEN.findall(&sent[..start]);
    if subject.is_empty() {
        return None;
    }
    let low: Vec<String> = subject.iter().map(|t| t.to_lowercase()).collect();

    if low[0] == "my" {
        let rest = &subject[1..];
        if !rest.is_empty() && attr_suffix(rest) == Some(0) && rest.len() <= 2 {
            return Some(Slot::new("user", &rest.join(" ").to_lowercase(), &value));
        }
        return None;
    }

    if let Some(o) = low.iter().position(|t| t == "of") {
        let (attr_part, ent_part) = (&subject[..o], &subject[o + 1..]);
        let stripped: Vec<&str> = attr_part
            .iter()
            .copied()
            .filter(|t| !lower_in(t, &ARTICLES))
            .collect();
        if !stripped.is_empty() && attr_suffix(&stripped) == Some(0) && stripped.len() <= 2 {
            let entity = clean_entity(ent_part);
            if !entity.is_empty() {
                return Some(Slot::new(
                    &entity,
                    &stripped.join(" ").to_lowercase(),
                    &value,
                ));
            }
        }
        return None;
    }

    match attr_suffix(&subject) {
        Some(a) if a > 0 => {
            let entity = clean_entity(&subject[..a]);
            (!entity.is_empty())
                .then(|| Slot::new(&entity, &subject[a..].join(" ").to_lowercase(), &value))
        }
        _ => None,
    }
}

/// `_SENT_SPLIT_RE.split(line)`. A whitespace run can only start a Python
/// match at its first character (inside a run the lookbehind sees
/// whitespace), where greed takes all of it, and a run not after `.!?`
/// never matches; so dropping those runs from the lookbehind-free matches
/// leaves exactly Python's.
fn sentences(line: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    for (s, e) in SENT_SEP.find_spans(line) {
        if !line[s..].starts_with(';') && !line[..s].ends_with(['.', '!', '?']) {
            continue;
        }
        out.push(&line[start..s]);
        start = e;
    }
    out.push(&line[start..]);
    out
}

/// Dedupe key of `extract_dev_slots._add` and `extract_slots` step 5.
fn slot_key(s: &Slot) -> (String, String, String) {
    (
        s.entity.to_lowercase(),
        s.attribute.clone(),
        s.value.clone(),
    )
}

/// Deterministic dev-fact extraction (slots.py:198-227).
pub fn extract_dev_slots(text: &str) -> Vec<Slot> {
    let text = FENCE.replace_all(text, " ");
    let mut out: Vec<Slot> = Vec::new();
    let mut seen: HashSet<(String, String, String)> = HashSet::new();
    let mut add = |slot: Option<Slot>| {
        if let Some(slot) = slot
            && seen.insert(slot_key(&slot))
        {
            out.push(slot);
        }
    };
    for line in py_splitlines(&text) {
        let line = py_strip(line);
        // Groups: 1 key, 2 value.
        if let Some(m) = KV_LINE.captures_all(line).first() {
            let key_toks = DEV_TOKEN.findall(m[1].expect("key"));
            if let Some(a) = attr_suffix(&key_toks)
                && a > 0
            {
                let entity = clean_entity(&key_toks[..a]);
                let value = clean_value(m[2].expect("value"));
                if !entity.is_empty() && !value.is_empty() {
                    add(Some(Slot::new(
                        &entity,
                        &key_toks[a..].join(" ").to_lowercase(),
                        &value,
                    )));
                    continue;
                }
            }
        }
        for sent in sentences(line) {
            let sent = py_strip(sent);
            if !sent.is_empty() {
                add(dev_slot_from_copula(sent));
            }
        }
    }
    out
}

// ---- Public API (slots.py:235-308) ------------------------------------------

/// Extract the slots of one text; `last_entity_context` is the coreference
/// anchor for pronouns with no antecedent in this text (empty counts as none).
pub fn extract_slots(text: &str, last_entity_context: Option<&str>) -> Vec<Slot> {
    let mut slots: Vec<Slot> = Vec::new();

    // 1. Named entities (group 1: name).
    let named: Vec<&str> = NAMED
        .captures_all(text)
        .iter()
        .map(|c| c[1].expect("name"))
        .collect();

    // 2. `I have a Ragdoll cat` (groups 1: attr, 2: type).
    let mut type_entity: Option<String> = None;
    for m in HAVE_TYPE.captures_all(text) {
        let t = m[2].expect("type").to_lowercase();
        let anchor = named
            .first()
            .map_or_else(|| format!("my {t}"), |n| n.to_string());
        slots.push(Slot::new(&anchor, "type", &t));
        if let Some(attr) = m[1] {
            let upper = attr.chars().next().is_some_and(char::is_uppercase);
            let name = if upper { "breed" } else { "color" };
            slots.push(Slot::new(&anchor, name, attr));
        }
        type_entity = Some(anchor);
    }

    if !named.is_empty() && type_entity.is_none() {
        for name in &named {
            slots.push(Slot::new(name, "name", name));
        }
    }

    // 3. Pronoun gender hint, first pronoun only, when there is a referent.
    let referent: Option<String> = named
        .first()
        .map(|n| n.to_string())
        .or(type_entity)
        .or_else(|| last_entity_context.map(str::to_string))
        .filter(|r| !r.is_empty());
    if let Some(referent) = &referent {
        let gender = PRONOUN_TOKEN.findall(text).into_iter().find_map(|tok| {
            let low = tok.to_lowercase();
            PRONOUN_GENDER
                .iter()
                .find(|(p, _)| *p == low)
                .map(|(_, g)| *g)
        });
        if let Some(gender) = gender {
            slots.push(Slot::new(referent, "gender", gender));
        }
        // 4. Loss / state change.
        if LOSS.is_match(text) {
            slots.push(Slot::new(referent, "owned", "False"));
        }
    }

    // 5. Dev-shaped facts not already present.
    let existing: HashSet<_> = slots.iter().map(slot_key).collect();
    for s in extract_dev_slots(text) {
        if !existing.contains(&slot_key(&s)) {
            slots.push(s);
        }
    }
    slots
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::collections::HashMap;

    pub const GOLDEN: &str = include_str!("../harness/goldens/w2e-slots.json");

    pub fn golden() -> &'static Value {
        static G: LazyLock<Value> =
            LazyLock::new(|| serde_json::from_str(GOLDEN).expect("w2e-slots.json is valid JSON"));
        &G
    }

    pub fn rows(v: &Value) -> Vec<Slot> {
        v.as_array()
            .expect("slot rows")
            .iter()
            .map(|r| Slot::from_json(r).expect("a 4-string row"))
            .collect()
    }

    /// Report the first 20 mismatches, then fail on any.
    pub fn verdict(what: &str, checked: usize, declared: &[String], bad: &[String]) {
        eprintln!(
            "{what}: {checked} checked, {} declared (skipped), {} mismatched",
            declared.len(),
            bad.len()
        );
        for d in declared {
            eprintln!("  declared: {d}");
        }
        assert!(
            bad.is_empty(),
            "{what}: {} mismatches:\n{}",
            bad.len(),
            bad.iter().take(20).cloned().collect::<Vec<_>>().join("\n")
        );
    }

    fn check_slot_cases(cases: &[Value], what: &str) {
        let (mut checked, mut declared, mut bad) = (0, Vec::new(), Vec::new());
        for c in cases {
            let text = c["text"].as_str().expect("text");
            if let Some(why) = c.get("declared") {
                declared.push(format!("{text:?}: {why}"));
                continue;
            }
            checked += 1;
            let ctx = c["ctx"].as_str();
            let got = extract_slots(text, ctx);
            let want = rows(&c["slots"]);
            if got != want {
                bad.push(format!(
                    "extract_slots({text:?}, {ctx:?})\n  got  {got:?}\n  want {want:?}"
                ));
            }
            let got = extract_dev_slots(text);
            let want = rows(&c["dev"]);
            if got != want {
                bad.push(format!(
                    "extract_dev_slots({text:?})\n  got  {got:?}\n  want {want:?}"
                ));
            }
        }
        verdict(what, checked, &declared, &bad);
    }

    #[test]
    fn golden_slot_cases_match_python() {
        let cases = golden()["slots"].as_array().expect("slots");
        assert!(cases.len() >= 500, "corpus shrank to {}", cases.len());
        check_slot_cases(cases, "slots");
    }

    /// The whole docs corpus, from `gen_w2e_slots_goldens.py --full PATH`.
    #[test]
    #[ignore = "needs W2E_FULL_GOLDEN from gen_w2e_slots_goldens.py --full"]
    fn full_docs_corpus() {
        let path = std::env::var("W2E_FULL_GOLDEN").expect("W2E_FULL_GOLDEN");
        let text = std::fs::read_to_string(path).expect("full golden");
        let v: Value = serde_json::from_str(&text).expect("valid JSON");
        check_slot_cases(v["slots"].as_array().expect("slots"), "full docs corpus");
    }

    // ---- Character classes, over every code point ---------------------------

    fn ranges(v: &Value) -> Vec<(u32, u32)> {
        v.as_array()
            .expect("ranges")
            .iter()
            .map(|r| (r[0].as_u64().unwrap() as u32, r[1].as_u64().unwrap() as u32))
            .collect()
    }

    fn in_ranges(r: &[(u32, u32)], cp: u32) -> bool {
        r.binary_search_by(|&(lo, hi)| {
            if hi < cp {
                std::cmp::Ordering::Less
            } else if lo > cp {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
    }

    fn cps(v: &Value) -> HashSet<u32> {
        v.as_array()
            .expect("code points")
            .iter()
            .map(|c| c.as_u64().unwrap() as u32)
            .collect()
    }

    fn all_chars() -> impl Iterator<Item = char> {
        (0..0x11_0000u32).filter_map(char::from_u32)
    }

    /// `differs(c)` describes a Python/Rust disagreement at `c`. Every one
    /// must be a code point the oracle's Unicode leaves unassigned (the Rust
    /// tables are newer); returns their count.
    pub fn diff_over_all(name: &str, differs: impl Fn(char) -> Option<String>) -> usize {
        let unassigned = ranges(&golden()["classes"]["unassigned"]);
        let (mut newer, mut bad) = (0, Vec::new());
        for c in all_chars() {
            if let Some(why) = differs(c) {
                if in_ranges(&unassigned, c as u32) {
                    newer += 1;
                } else {
                    bad.push(format!("U+{:04X}: {why}", c as u32));
                }
            }
        }
        eprintln!(
            "{name}: {newer} differences, all at code points unassigned in the oracle's Unicode"
        );
        verdict(name, 0x11_0000 - 0x800, &[], &bad);
        newer
    }

    fn compare_class(
        name: &str,
        python: impl Fn(char) -> bool,
        rust: impl Fn(char) -> bool,
    ) -> usize {
        diff_over_all(name, |c| {
            (python(c) != rust(c)).then(|| format!("python {} rust {}", python(c), rust(c)))
        })
    }

    fn anchored(class: &str, flags: &str) -> regex::Regex {
        regex::Regex::new(&format!("^{flags}{class}$")).unwrap()
    }

    #[test]
    fn python_classes_match_over_every_code_point() {
        let cl = &golden()["classes"];
        for (key, re) in [
            ("word", anchored(&format!("(?-i:[{PY_W}])"), "(?i)")),
            ("space", anchored(&format!("[{PY_S}]"), "")),
            ("digit", anchored(r"\d", "")),
            ("isspace", anchored(&format!("[{PY_S}]"), "")),
        ] {
            let r = ranges(&cl[key]);
            compare_class(
                key,
                |c| in_ranges(&r, c as u32),
                |c| re.is_match(c.encode_utf8(&mut [0; 4])),
            );
        }
        // `py_strip` is the crate's `str.strip()`.
        let r = ranges(&cl["isspace"]);
        compare_class(
            "strip",
            |c| in_ranges(&r, c as u32),
            |c| py_strip(c.encode_utf8(&mut [0; 4])).is_empty(),
        );
        let breaks = cps(&cl["splitlines"]);
        compare_class(
            "splitlines",
            |c| breaks.contains(&(c as u32)),
            |c| py_splitlines(&format!("a{c}b")).len() == 2,
        );
    }

    #[test]
    fn ignorecase_letters_match_python() {
        let cl = &golden()["classes"];
        let (mut checked, mut bad) = (0, Vec::new());
        for (key, pat) in [("i", "i"), ("s", "s"), ("k", "k"), ("letters", "[A-Za-z]")] {
            let want = cps(&cl["ignorecase"][key]);
            let re = PyRegex::new(&format!("^{pat}$"), true, false);
            for c in all_chars() {
                checked += 1;
                let got = re.is_match(c.encode_utf8(&mut [0; 4]));
                if got != want.contains(&(c as u32)) {
                    bad.push(format!("(?i){pat} U+{:04X}: rust {got}", c as u32));
                }
            }
        }
        // `attr_value[0].isupper()` over the letters IGNORECASE admits.
        let upper = cps(&cl["isupper"]);
        for cp in cps(&cl["ignorecase"]["letters"]) {
            let c = char::from_u32(cp).unwrap();
            if c.is_uppercase() != upper.contains(&cp) {
                bad.push(format!("isupper U+{cp:04X}"));
            }
        }
        verdict("ignorecase", checked, &[], &bad);
    }

    /// The fast engine's native `\b` and the exact one's Python `\b` agree
    /// beside every code point outside `DIVERGENT`, and differ beside some
    /// inside it (so the exact engine is load-bearing).
    #[test]
    fn fast_boundary_is_exact_outside_divergent() {
        let re = PyRegex::new(r"\b", false, false);
        let exact = re
            .exact
            .as_ref()
            .expect("a \\b pattern has an exact engine");
        let (mut divergent_differs, mut bad) = (0, Vec::new());
        for c in all_chars() {
            for text in [
                format!("a{c}"),
                format!("{c}a"),
                format!("{c}"),
                format!(" {c} "),
            ] {
                let fast: Vec<_> = re.fast.find_iter(&text).map(|m| m.start()).collect();
                let slow: Vec<_> = exact.find_iter(&text).map(|m| m.unwrap().start()).collect();
                if fast != slow {
                    if DIVERGENT.is_match(&text) {
                        divergent_differs += 1;
                    } else {
                        bad.push(format!("{text:?}: fast {fast:?} exact {slow:?}"));
                    }
                }
            }
        }
        eprintln!("boundary: {divergent_differs} divergent texts take the exact engine's answer");
        assert!(divergent_differs > 0);
        verdict("boundary", 4 * (0x11_0000 - 0x800), &[], &bad);
    }

    /// `sentences` against the lookbehind split it replaces, on every
    /// golden text.
    #[test]
    fn sentences_match_the_lookbehind_split() {
        let re = Regex::new(&format!(r"(?<=[.!?])[{PY_S}]+|;[{PY_S}]*")).unwrap();
        let mut bad = Vec::new();
        let cases = golden()["slots"].as_array().unwrap();
        for c in cases {
            let text = c["text"].as_str().unwrap();
            let want: Vec<&str> = re.split(text).map(Result::unwrap).collect();
            if sentences(text) != want {
                bad.push(format!("{text:?}"));
            }
        }
        for text in [
            "a.  b",
            "a ;b",
            "a. ;b",
            ";",
            "a!\u{1f}b",
            "x?\u{a0} y",
            "a  b. c",
        ] {
            let want: Vec<&str> = re.split(text).map(Result::unwrap).collect();
            if sentences(text) != want {
                bad.push(format!("{text:?}"));
            }
        }
        verdict("sentences", cases.len(), &[], &bad);
    }

    /// Python's per-code-point mapping (`lower`, `casefold`) against `rust`.
    pub fn compare_mapping(key: &str, rust: impl Fn(char) -> String) -> usize {
        let map: HashMap<u32, &str> = golden()["classes"][key]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (p[0].as_u64().unwrap() as u32, p[1].as_str().unwrap()))
            .collect();
        diff_over_all(key, |c| {
            let want = map
                .get(&(c as u32))
                .map_or_else(|| c.to_string(), |s| s.to_string());
            let got = rust(c);
            (got != want).then(|| format!("rust {got:?} python {want:?}"))
        })
    }

    #[test]
    fn lowercase_matches_python() {
        compare_mapping("lower", |c| c.to_lowercase().to_string());
    }

    #[test]
    fn slot_json_round_trip() {
        let s = Slot::new("Jacque", "type", "cat");
        assert_eq!(
            s.to_json(),
            serde_json::json!(["Jacque", "type", "cat", "+"])
        );
        assert_eq!(Slot::from_json(&s.to_json()), Some(s));
        assert_eq!(Slot::from_json(&serde_json::json!(["a", "b", "c"])), None);
        assert_eq!(
            Slot::from_json(&serde_json::json!(["a", "b", "c", null])),
            None
        );
        assert_eq!(Slot::from_json(&serde_json::json!("abcd")), None);
    }

    #[test]
    fn splitlines_matches_python_shapes() {
        assert_eq!(py_splitlines(""), Vec::<&str>::new());
        assert_eq!(py_splitlines("a\n"), vec!["a"]);
        assert_eq!(py_splitlines("a\r\n\r\nb"), vec!["a", "", "b"]);
        assert_eq!(py_splitlines("a\n\rb"), vec!["a", "", "b"]);
        assert_eq!(py_splitlines("\n"), vec![""]);
    }
}
