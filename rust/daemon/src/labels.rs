//! Write-time label heuristics (`memory/labels.py`), spec W1.1.
//!
//! The regexes are Python's, verbatim, compiled with `(?i)`. Python's `re`
//! and the Rust engines disagree on three things, so the text is first
//! remapped one char for one char ([`py_regex_text`]) into a form on which
//! they agree: word chars (Python: L, N and `_`; Rust: Alphabetic, M, Nd, Pc,
//! Join_Control), `\s` (Python adds U+001C..U+001F) and `re.I` (Python also
//! matches U+0130 and U+0131 against `i`). The heuristics read at most 400
//! code points, which keeps fancy-regex's backtracking far inside its limits.
#![allow(dead_code)]

use std::borrow::Cow;
use std::fmt::Write as _;
use std::sync::OnceLock;

use fancy_regex::Regex;
use unicode_casefold::UnicodeCaseFold;

/// labels.py:65-68.
pub const AUTHORITY_VALUES: [&str; 3] = ["directive", "observation", "quoted"];
pub const DISTORTION_VALUES: [&str; 5] = [
    "constraint",
    "procedural",
    "belief",
    "preference",
    "episodic",
];
pub const AUTO: &str = "auto";
/// labels.py:74: the rule-sized gate, in code points.
pub const AUTO_MAX_CHARS: usize = 400;

/// What a caller's `authority` / `distortion_tolerance` argument resolves to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resolved {
    /// Python's `INHERIT` sentinel: keep what the landing record has.
    Inherit,
    /// A value to store; `None` is an explicit clear.
    Set(Option<String>),
}

// labels.py:101-105.
const STRONG: &str = concat!(
    r"\b((?<!\ba\s)(?<!\ban\s)(?<!\bthe\s)must(?!-)(?:\s+not|n't)?|",
    r"shall(?:\s+not)?|forbidden|prohibited|",
    r"non-negotiable|no exceptions|under no circumstances|hard rule|",
    r"standing (?:rule|instruction)|mandatory)\b",
);
// labels.py:106-108.
const FRAMING: &str = concat!(
    r"(?:^|\n)\W*(?:rule|constraint|policy|invariant|hard rule|",
    r"standing rule|non-negotiable|mandatory)\s*[:\-—]",
);
// labels.py:114-116. `^` without `(?m)` anchors at the text start, so a
// search is Python's `.match`.
const IMPERATIVE_OPENER: &str = concat!(
    r"^\W*(?:\[[^\]]*\]\s*)?(?:never|always|do not|don't|must|you must)\s+",
    r"([A-Za-z][A-Za-z\-]*)",
);
// labels.py:117-125.
#[rustfmt::skip]
const NOT_BARE: &[&str] = &[
    "be", "been", "being", "was", "were", "is", "are", "am", "had", "has", "have", "did", "does",
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "before", "after", "again",
    "available", "this", "that", "these", "those", "it", "its", "my", "your", "our", "their",
    "any", "all", "one", "once", "ever", "yet", "quite", "fully", "really", "actually", "just",
    "only", "even", "also", "not", "resident", "visible", "reached", "gone", "true", "false",
    "up", "out",
];
// labels.py:133-145.
#[rustfmt::skip]
const IRREGULAR_PAST: &[&str] = &[
    "ate", "arose", "awoke", "became", "began", "bought", "broke", "brought", "built", "came",
    "caught", "chose", "dealt", "did", "done", "drank", "drew", "drove", "fed", "fell", "felt",
    "flew", "forgave", "forgot", "fought", "froze", "gave", "got", "gotten", "grew", "held",
    "kept", "knew", "laid", "led", "left", "lost", "made", "meant", "met", "mistook", "overcame",
    "paid", "ran", "rode", "rose", "said", "sang", "sank", "sat", "shook", "shrank", "slept",
    "slid", "sold", "sought", "spent", "spoke", "stole", "stood", "struck", "stuck", "swam",
    "taught", "threw", "thought", "told", "took", "tore", "understood", "undertook", "went",
    "withdrew", "woke", "won", "wore", "wrote",
];
// labels.py:146-147.
const FIRST_PERSON_HABIT: &str = r"\bi (?:always|never|usually|prefer|like|tend to)\b";
// labels.py:152-159.
const REPORTED: &str = concat!(
    r"\b(?:according to|as per|per the (?!usual\b|plan\b|schedule\b|above\b|below\b)|",
    r"(?:the\s+)?(?:paper|docs?|documentation|spec|runbook|readme|article|",
    r"guide|manual|page|post|thread|vendor|upstream|authors?|he|she|they|",
    r"someone|colleague|friend)\s+",
    r"(?:said|says|state[sd]|prescribes?|recommends?|reports?|claims?|",
    r"argues?|writes?|wrote|told|asked|mentioned|notes?|suggests?|",
    r"advises?|warns?))\b",
);
// labels.py:160-162.
const ADDRESSED: &str = concat!(
    r"^\W*(?:please|you must)\b|",
    r"\byou (?:must|should|need to|may not|are not allowed to)\b",
);

fn compiled(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(&format!("(?i){pattern}")).expect("static label pattern"))
}

macro_rules! pattern {
    ($name:ident, $src:expr) => {
        fn $name() -> &'static Regex {
            static CELL: OnceLock<Regex> = OnceLock::new();
            compiled(&CELL, $src)
        }
    };
}

pattern!(strong, STRONG);
pattern!(framing, FRAMING);
pattern!(imperative_opener, IMPERATIVE_OPENER);
pattern!(first_person_habit, FIRST_PERSON_HABIT);
pattern!(reported, REPORTED);
pattern!(addressed, ADDRESSED);

/// `re.search` on gated text. The 400-code-point gate bounds the work far
/// below fancy-regex's backtrack and stack limits, so an error is a bug.
fn hit(re: &Regex, text: &str) -> bool {
    re.is_match(text)
        .expect("gated text stays inside the backtrack limit")
}

/// `str.isspace()`, which is also what `str.strip()` and `re`'s `\s` use:
/// Unicode White_Space plus U+001C..U+001F.
pub(crate) fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()` with no argument.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// Remaps `text` one char for one char so that a Python `re.I` pattern,
/// compiled verbatim under `(?i)` by `regex` or `fancy-regex`, matches where
/// Python's would. Char positions are preserved; byte offsets are not.
///
/// - A Python word char (L, N, `_`) that Rust's `\w` lacks (No, e.g. `²`)
///   becomes `0`; a Rust word char that Python's lacks (marks, `ⓐ`, ZWJ,
///   `‿`) becomes `#`. Neither placeholder occurs in any pattern.
/// - U+0130 and U+0131 become `i`: Python's `re.I` matches both against
///   `i`, Rust's simple case folding neither (it does match U+212A and
///   U+017F against `k` and `s`, as Python does).
/// - U+001C..U+001F become U+2000: whitespace to both `\s`, and like the
///   originals not a literal space or newline.
///
/// Classes are as of the `regex` crate's Unicode tables (16.0); the oracle's
/// Python 3.11 uses 14.0, so code points assigned since can still differ.
pub(crate) fn py_regex_text(text: &str) -> Cow<'_, str> {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        regex::Regex::new(concat!(
            r"([[\p{L}\p{N}_]--\w])|([\w--[\p{L}\p{N}_]])|",
            r"([\x{130}\x{131}])|([\x1C-\x1F])",
        ))
        .expect("static remap pattern")
    });
    re.replace_all(text, |c: &regex::Captures<'_>| {
        if c.get(1).is_some() {
            "0"
        } else if c.get(2).is_some() {
            "#"
        } else if c.get(3).is_some() {
            "i"
        } else {
            "\u{2000}"
        }
    })
}

/// labels.py:165-173. `str.lower()` and Rust's `to_lowercase` agree on every
/// char the opener's `[A-Za-z]` class can capture (ASCII, U+0130, U+0131,
/// U+017F, U+212A).
fn bare_verb(word: &str) -> bool {
    let w = word.to_lowercase();
    if NOT_BARE.contains(&w.as_str()) || IRREGULAR_PAST.contains(&w.as_str()) {
        return false;
    }
    if ["ed", "en", "ing", "ly"].iter().any(|s| w.ends_with(s)) {
        return false;
    }
    !w.ends_with('s') || w.ends_with("ss")
}

/// labels.py:176-178. `text` is the stripped original and `remapped` its
/// [`py_regex_text`]; the captured verb is read back from the original at the
/// same char positions, because the remap changes U+0130 / U+0131.
fn opener_imperative(text: &str, remapped: &str) -> bool {
    let caps = imperative_opener()
        .captures(remapped)
        .expect("gated text stays inside the backtrack limit");
    let Some(g) = caps.and_then(|c| c.get(1)) else {
        return false;
    };
    let start = remapped[..g.start()].chars().count();
    let len = g.as_str().chars().count();
    let word: String = text.chars().skip(start).take(len).collect();
    bare_verb(&word)
}

/// The stripped text when it is non-empty and rule-sized (labels.py:185-187).
fn gated(text: &str) -> Option<&str> {
    let t = py_strip(text);
    (!t.is_empty() && t.chars().count() <= AUTO_MAX_CHARS).then_some(t)
}

/// `infer_distortion_tolerance` (labels.py:181-192): `"constraint"` for a
/// rule-sized deontic or imperative statement, else `None`.
pub fn infer_distortion_tolerance(text: &str) -> Option<&'static str> {
    let t = gated(text)?;
    let r = py_regex_text(t);
    if hit(first_person_habit(), &r) {
        return None; // a habit is a preference, not a rule
    }
    (hit(strong(), &r) || hit(framing(), &r) || opener_imperative(t, &r)).then_some("constraint")
}

/// `infer_authority` (labels.py:195-214): `"quoted"` on a reporting
/// construction (it wins), `"directive"` on an instruction to the reader,
/// else `None` (observation).
pub fn infer_authority(text: &str) -> Option<&'static str> {
    let t = gated(text)?;
    let r = py_regex_text(t);
    if hit(reported(), &r) {
        return Some("quoted");
    }
    (hit(addressed(), &r) || hit(framing(), &r) || opener_imperative(t, &r)).then_some("directive")
}

/// `str.isprintable()` for one non-ASCII char: false for categories C* and Z*.
fn py_isprintable(c: char) -> bool {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"[\p{C}\p{Z}]").expect("static pattern"));
    !re.is_match(c.encode_utf8(&mut [0; 4]))
}

/// Python's `repr()` of a `str` (`unicode_repr`, Objects/unicodeobject.c).
pub(crate) fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c < ' ' || c == '\x7f' => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if c.is_ascii() || py_isprintable(c) => out.push(c),
            c if (c as u32) <= 0xff => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if (c as u32) <= 0xffff => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => {
                let _ = write!(out, "\\U{:08x}", c as u32);
            }
        }
    }
    out.push(quote);
    out
}

/// `_normalize` (labels.py:217-224): `str(v).strip().casefold()`, checked
/// against the vocabulary; the error is Python's `ValueError` text.
fn normalize(value: &str, vocabulary: &[&str], name: &str) -> Result<String, String> {
    let v: String = py_strip(value).case_fold().collect();
    if vocabulary.contains(&v.as_str()) {
        return Ok(v);
    }
    let list: Vec<String> = vocabulary.iter().map(|w| py_repr(w)).collect();
    Err(format!(
        "{name} must be one of [{}] (or None), got {}",
        list.join(", "),
        py_repr(value)
    ))
}

/// labels.py:235-251. `"auto"` is compared exactly, before normalization.
fn resolve(
    requested: Option<&str>,
    text: &str,
    infer: fn(&str) -> Option<&'static str>,
    vocabulary: &[&str],
    name: &str,
) -> Result<Resolved, String> {
    match requested {
        None => Ok(Resolved::Set(None)),
        Some(AUTO) => Ok(infer(text).map_or(Resolved::Inherit, |v| Resolved::Set(Some(v.into())))),
        Some(v) => normalize(v, vocabulary, name).map(|v| Resolved::Set(Some(v))),
    }
}

/// `resolve_authority` (labels.py:235-243).
pub fn resolve_authority(requested: Option<&str>, text: &str) -> Result<Resolved, String> {
    resolve(
        requested,
        text,
        infer_authority,
        &AUTHORITY_VALUES,
        "authority",
    )
}

/// `resolve_distortion` (labels.py:246-251).
pub fn resolve_distortion(requested: Option<&str>, text: &str) -> Result<Resolved, String> {
    resolve(
        requested,
        text,
        infer_distortion_tolerance,
        &DISTORTION_VALUES,
        "distortion_tolerance",
    )
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use serde_json::{Value, json};

    /// Golden cases from the Python functions (`harness/gen_w2e_labels_goldens.py`).
    pub(crate) const GOLDEN: &str = include_str!("../harness/goldens/w2e-labels.json");

    /// Runs every golden case whose `fn` `run` answers, and fails listing all
    /// mismatches (the first 20 in full). Returns the cases run per function.
    pub(crate) fn check_goldens(
        run: impl Fn(&str, &[Value]) -> Option<Value>,
    ) -> std::collections::BTreeMap<String, usize> {
        let cases: Vec<Value> = serde_json::from_str(GOLDEN).expect("golden parses");
        let mut counts = std::collections::BTreeMap::new();
        let mut bad = Vec::new();
        for case in &cases {
            let name = case["fn"].as_str().expect("fn");
            let args = case["args"].as_array().expect("args");
            let Some(got) = run(name, args) else { continue };
            *counts.entry(name.to_string()).or_insert(0) += 1;
            if got != case["out"] {
                bad.push(format!("{name}{args:?}: python {} rust {got}", case["out"]));
            }
        }
        let total: usize = counts.values().sum();
        let shown: Vec<&String> = bad.iter().take(20).collect();
        assert!(
            bad.is_empty(),
            "{} of {total} cases differ from Python; first {}:\n{}",
            bad.len(),
            shown.len(),
            shown
                .iter()
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join("\n")
        );
        counts
    }

    fn text(v: &Value) -> &str {
        v.as_str().expect("text arg")
    }

    fn resolved(r: Result<Resolved, String>) -> Value {
        match r {
            Ok(Resolved::Inherit) => json!({"inherit": true}),
            Ok(Resolved::Set(v)) => json!({"ok": v}),
            Err(e) => json!({"err": e}),
        }
    }

    #[test]
    fn labels_match_python_goldens() {
        let counts = check_goldens(|name, args| {
            Some(match name {
                "infer_authority" => json!(infer_authority(text(&args[0]))),
                "infer_distortion_tolerance" => json!(infer_distortion_tolerance(text(&args[0]))),
                "resolve_authority" => {
                    resolved(resolve_authority(args[0].as_str(), text(&args[1])))
                }
                "resolve_distortion" => {
                    resolved(resolve_distortion(args[0].as_str(), text(&args[1])))
                }
                _ => return None,
            })
        });
        for name in [
            "infer_authority",
            "infer_distortion_tolerance",
            "resolve_authority",
            "resolve_distortion",
        ] {
            assert!(
                counts.get(name).copied().unwrap_or(0) >= 400,
                "{name}: {counts:?}"
            );
        }
    }
}
