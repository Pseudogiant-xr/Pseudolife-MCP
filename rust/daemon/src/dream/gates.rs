//! The dream pass's pure decisions (`service_dream.py`, `memory/dream.py`,
//! `memory/labels.py`, `memory/graph_review.py`, `service.py`): the citation
//! check that bounds a claim's write tier, the literal-faithfulness and
//! provenance-span gates, the constraint carrier's token measures, the
//! source-to-tier map and the quarantine's trust inputs, the slot-key
//! repair, the write-dedup name matcher and the scope folding.
//!
//! Text predicates follow CPython 3.11: `\w` and category M come from the
//! Unicode 14 `_sre` tables (`recall_unicode14.rs`), casefolding from
//! `board::text::casefold`. The date-like mask uses the `regex` crate's
//! Unicode `\b`, `\d` and `\s` and its case folding, which differ from
//! `_sre` only on characters outside the scripts a note carries (declared).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::OnceLock;

use unicode_normalization::UnicodeNormalization;

use crate::recall_mentions::{is_mark, is_word};
use crate::stores::cortex::py_isspace;

/// Python `str.casefold` (Unicode 14).
fn casefold(s: &str) -> String {
    crate::board::text::casefold(s)
}

/// Python `str.split()`: runs of whitespace, empty pieces dropped.
fn py_split(s: &str) -> impl Iterator<Item = &str> {
    s.split(py_isspace).filter(|t| !t.is_empty())
}

/// `" ".join(s.split())`.
fn collapse(s: &str) -> String {
    py_split(s).collect::<Vec<_>>().join(" ")
}

// ---------------------------------------------------------------------------
// Citation backing (`service_dream.py:240-292`)
// ---------------------------------------------------------------------------

/// `_citation_text`: NFC, casefolded, whitespace-collapsed.
pub fn citation_text(s: &str) -> String {
    let nfc: String = s.nfc().collect();
    collapse(&casefold(&nfc))
}

/// `_word_char`: letters, digits and combining marks of any script, and `_`.
fn word_char(c: char) -> bool {
    is_word(c) || is_mark(c)
}

/// `_phrase_starts`: byte offsets where `phrase` occurs in `text` with no
/// word character on either side.
fn phrase_starts(phrase: &str, text: &str) -> Vec<usize> {
    let mut out = Vec::new();
    if phrase.is_empty() {
        return out;
    }
    let mut from = 0;
    while let Some(rel) = text[from..].find(phrase) {
        let at = from + rel;
        let end = at + phrase.len();
        let before_ok = text[..at].chars().next_back().is_none_or(|c| !word_char(c));
        let after_ok = text[end..].chars().next().is_none_or(|c| !word_char(c));
        if before_ok && after_ok {
            out.push(at);
        }
        // `text.find(phrase, at + 1)`: the next code point.
        from = at + text[at..].chars().next().map_or(1, char::len_utf8);
        if from > text.len() {
            break;
        }
    }
    out
}

fn has_phrase(phrase: &str, text: &str) -> bool {
    !phrase_starts(phrase, text).is_empty()
}

const NEGATORS: [&str; 7] = ["not", "no", "never", "without", "neither", "nor", "cannot"];

/// `_CLAUSE_BREAK_RE`: `[.;:,!?()\[\]{}–—]`.
fn clause_break(c: char) -> bool {
    matches!(
        c,
        '.' | ';'
            | ':'
            | ','
            | '!'
            | '?'
            | '('
            | ')'
            | '['
            | ']'
            | '{'
            | '}'
            | '\u{2013}'
            | '\u{2014}'
    )
}

/// `_negated_at`: a negator among the three words before `at` in its clause.
fn negated_at(text: &str, at: usize) -> bool {
    let clause = text[..at].rsplit(clause_break).next().unwrap_or("");
    // `[\w'’]+`
    let words: Vec<&str> = clause
        .split(|c: char| !(is_word(c) || c == '\'' || c == '\u{2019}'))
        .filter(|w| !w.is_empty())
        .collect();
    words
        .iter()
        .rev()
        .take(3)
        .any(|w| NEGATORS.contains(w) || w.ends_with("n't") || w.ends_with("n\u{2019}t"))
}

/// `_TOKEN_STOP` (`memory/labels.py`).
pub const TOKEN_STOP: &[&str] = &[
    "the", "and", "for", "with", "that", "this", "from", "into", "are", "was", "were", "has",
    "have", "not", "any", "all", "you", "your", "its", "our", "their", "than", "then", "when",
    "never", "always", "must", "should", "never", "do", "don", "t",
];

fn stop_word(t: &str) -> bool {
    TOKEN_STOP.contains(&t)
}

/// `_citation_backs`: whether a cited note mentions the claim's whole value
/// (not negated, unless the claim is a retraction) and names its slot.
pub fn citation_backs(
    value: &str,
    note: &str,
    entity: &str,
    attribute: &str,
    affirmative: bool,
) -> bool {
    let v = citation_text(value);
    let n = citation_text(note);
    if v.is_empty() || n.is_empty() {
        return false;
    }
    let starts = phrase_starts(&v, &n);
    if starts.is_empty() || (affirmative && starts.iter().all(|&at| negated_at(&n, at))) {
        return false;
    }
    let slot = citation_text(&format!("{entity} {attribute}"));
    slot.split(|c: char| matches!(c, '-' | '_' | '.') || py_isspace(c))
        .filter(|t| t.chars().count() >= 2 && !stop_word(t))
        .any(|t| has_phrase(t, &n))
}

// ---------------------------------------------------------------------------
// Literal-faithfulness gate (`memory/dream.py:716-858`)
// ---------------------------------------------------------------------------

const MONTHS: &str = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec";
const STRIP_PUNCT: &[char] = &[
    '.', ',', ';', ':', '!', '?', '(', ')', '[', ']', '{', '}', '<', '>', '"', '\'', '`', '#', '*',
];
const SPELLED_NUMBERS: [(&str, &str); 30] = [
    ("zero", "0"),
    ("one", "1"),
    ("two", "2"),
    ("three", "3"),
    ("four", "4"),
    ("five", "5"),
    ("six", "6"),
    ("seven", "7"),
    ("eight", "8"),
    ("nine", "9"),
    ("ten", "10"),
    ("eleven", "11"),
    ("twelve", "12"),
    ("thirteen", "13"),
    ("fourteen", "14"),
    ("fifteen", "15"),
    ("sixteen", "16"),
    ("seventeen", "17"),
    ("eighteen", "18"),
    ("nineteen", "19"),
    ("twenty", "20"),
    ("thirty", "30"),
    ("forty", "40"),
    ("fifty", "50"),
    ("sixty", "60"),
    ("seventy", "70"),
    ("eighty", "80"),
    ("ninety", "90"),
    ("hundred", "100"),
    ("thousand", "1000"),
];

/// `_DATE_LIKE_RE`.
pub fn date_like_re() -> &'static regex::Regex {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| {
        let pattern = format!(
            r"(?i)\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}\b|\b\d{{4}}[/-]\d{{1,2}}[/-]\d{{1,2}}\b|\b\d{{1,2}}-[a-z]{{3}}-\d{{2,4}}\b|\b(?:{m})[a-z]*\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|\d{{4}})\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{m})[a-z]*\.?(?:,?\s+\d{{4}})?\b",
            m = MONTHS
        );
        regex::Regex::new(&pattern).expect("date-like pattern")
    })
}

/// `str.isdigit()` for one character: the decimal digits plus the common
/// digit-valued superscripts, subscripts and circled digits (an
/// approximation of `Numeric_Type=Digit`, declared).
fn py_isdigit(c: char) -> bool {
    crate::graph_review::is_decimal(c)
        || matches!(c as u32,
            0xB2 | 0xB3 | 0xB9 | 0x2070 | 0x2074..=0x2079 | 0x2080..=0x2089
            | 0x2460..=0x2468 | 0x2474..=0x247C | 0x2488..=0x2490 | 0x24EA
            | 0x24F5..=0x24FD | 0x24FF | 0x2776..=0x277E | 0x2780..=0x2788
            | 0x278A..=0x2792 | 0x1369..=0x1371 | 0x19DA | 0x1F100..=0x1F10A)
}

fn str_isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(py_isdigit)
}

/// `_ORDINAL_RE.sub(r"\1", t)`: `^(\d+)(?:st|nd|rd|th)$`.
fn strip_ordinal(t: &str) -> String {
    for suffix in ["st", "nd", "rd", "th"] {
        if let Some(head) = t.strip_suffix(suffix)
            && !head.is_empty()
            && head.chars().all(crate::graph_review::is_decimal)
        {
            return head.to_string();
        }
    }
    t.to_string()
}

/// `_norm_literal`.
fn norm_literal(token: &str) -> String {
    let t = casefold(token);
    let t = t.trim_matches(STRIP_PUNCT);
    let t = t.trim_start_matches(['$', '\u{20ac}', '\u{a3}', '~']);
    let t = t.trim_end_matches(['%', '+']);
    let t: String = t.chars().filter(|&c| c != ',' && c != '_').collect();
    let mut chars = t.chars();
    let t = match (chars.next(), chars.next()) {
        (Some('v'), Some(d)) if py_isdigit(d) => t[1..].to_string(),
        _ => t,
    };
    strip_ordinal(&t)
}

/// `_literal_tokens`.
fn literal_tokens(text: &str, mask_dates: bool, exempt_approx: bool) -> Vec<String> {
    let src = if mask_dates {
        date_like_re().replace_all(text, " ").into_owned()
    } else {
        text.to_string()
    };
    let mut out = Vec::new();
    for raw in py_split(&src) {
        if exempt_approx && casefold(raw).trim_matches(STRIP_PUNCT).starts_with('~') {
            continue;
        }
        let t = norm_literal(raw);
        if t.is_empty() {
            continue;
        }
        if t.trim_matches('-').contains('-') {
            out.extend(
                t.split('-')
                    .filter(|p| p.chars().any(py_isdigit))
                    .map(str::to_string),
            );
        } else if t.chars().any(py_isdigit) {
            out.push(t);
        }
    }
    out
}

/// `hard_literals`.
pub fn hard_literals(value: &str) -> Vec<String> {
    literal_tokens(value, true, true)
}

/// `literal_violations`: the gateable literals of `value` that `corpus`
/// does not back.
pub fn literal_violations(value: &str, corpus: &str) -> Vec<String> {
    if crate::storage::py_strip(corpus).is_empty() {
        return Vec::new();
    }
    let gateable = hard_literals(value);
    if gateable.is_empty() {
        return Vec::new();
    }
    let mut corpus_tokens: HashSet<String> =
        literal_tokens(corpus, false, false).into_iter().collect();
    for raw in py_split(corpus) {
        for word in casefold(raw).trim_matches(STRIP_PUNCT).split('-') {
            if let Some((_, digits)) = SPELLED_NUMBERS.iter().find(|(w, _)| *w == word) {
                corpus_tokens.insert((*digits).to_string());
            }
        }
    }
    let mut bad = Vec::new();
    for tok in gateable {
        if corpus_tokens.contains(&tok) {
            continue;
        }
        let num = super::extractor::py_float_str(&tok);
        let backed = corpus_tokens.iter().any(|ct| {
            if let Some(n) = num
                && super::extractor::py_float_str(ct) == Some(n)
            {
                return true;
            }
            !str_isdigit(&tok)
                && tok.chars().count() >= 2
                && ct.chars().count() >= 2
                && (ct.contains(tok.as_str()) || tok.contains(ct.as_str()))
        });
        if !backed {
            bad.push(tok);
        }
    }
    bad
}

// ---------------------------------------------------------------------------
// Provenance-span gate (`memory/dream.py:860-888`)
// ---------------------------------------------------------------------------

/// `_span_norm`: NFKC of the casefold, every non-word run one space, stripped.
fn span_norm(s: &str) -> String {
    let n: String = casefold(s).nfkc().collect();
    let mut out = String::with_capacity(n.len());
    let mut gap = false;
    for c in n.chars() {
        if is_word(c) {
            if gap {
                out.push(' ');
                gap = false;
            }
            out.push(c);
        } else {
            gap = true;
        }
    }
    if gap {
        out.push(' ');
    }
    crate::storage::py_strip(&out).to_string()
}

/// `span_unbacked`: `quote_missing`, `quote_unverified`, or None.
pub fn span_unbacked(quote: Option<&str>, corpus: &str) -> Option<&'static str> {
    let q = span_norm(quote.unwrap_or(""));
    if q.is_empty() {
        return Some("quote_missing");
    }
    if span_norm(corpus).contains(&q) {
        None
    } else {
        Some("quote_unverified")
    }
}

// ---------------------------------------------------------------------------
// TypeCompact measures (`memory/labels.py:280-308`)
// ---------------------------------------------------------------------------

/// `content_tokens`: lowercased ASCII alphanumeric runs of three or more,
/// minus the stop list.
pub fn content_tokens(text: &str) -> BTreeSet<String> {
    text.split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|t| !t.is_empty())
        .map(str::to_ascii_lowercase)
        .filter(|t| t.len() >= 3 && !stop_word(t))
        .collect()
}

/// `contains_verbatim`: whitespace-collapsed, case-preserving containment.
pub fn contains_verbatim(haystack: &str, needle: &str) -> bool {
    let n = collapse(needle);
    !n.is_empty() && collapse(haystack).contains(&n)
}

// ---------------------------------------------------------------------------
// Tiers and the quarantine's trust inputs (`service.py:533-538,603,704-735`)
// ---------------------------------------------------------------------------

/// `_origin_from_source`.
pub fn origin_from_source(source: Option<&str>) -> Option<&'static str> {
    match crate::storage::graph::py_lower(crate::storage::py_strip(source.unwrap_or(""))).as_str() {
        "conversation" | "user" => Some("user"),
        "claude" | "assistant" | "agent" => Some("agent"),
        "tool" | "action" => Some("action"),
        _ => None,
    }
}

/// `_TIER_RANK` (`cortex.py:254`).
pub fn tier_rank(tier: &str) -> i32 {
    match tier {
        "user" => 3,
        "action" => 2,
        "agent" => 1,
        _ => 0,
    }
}

/// The backing entry fields the quarantine reads.
pub struct TrustEntry<'a> {
    pub source: Option<&'a str>,
    pub episode_id: Option<&'a str>,
    pub authority: Option<&'a str>,
}

/// `_quarantine_low_trust`.
pub fn quarantine_low_trust(src: Option<&TrustEntry<'_>>, trusted: &[String]) -> bool {
    let Some(e) = src else { return true };
    if e.authority == Some("quoted") {
        return true;
    }
    let source = e.source.unwrap_or("");
    origin_from_source(Some(source)) == Some("agent") && !trusted.iter().any(|t| t == source)
}

/// `_quarantine_witness`.
pub fn quarantine_witness(src: Option<&TrustEntry<'_>>) -> String {
    match src {
        Some(e) => match e.episode_id.filter(|ep| !ep.is_empty()) {
            Some(ep) => format!("ep:{ep}"),
            None => format!("src:{}", e.source.unwrap_or("")),
        },
        None => "unbacked".into(),
    }
}

// ---------------------------------------------------------------------------
// Slot-key repair (`memory/dream.py:634-667`)
// ---------------------------------------------------------------------------

/// `unflatten_slot_key_claims` for one claim: the repaired `(entity,
/// attribute)` when every guard holds.
pub fn unflatten_slot_key(
    entity: &str,
    attribute: &str,
    vocab: &[String],
) -> Option<(String, String)> {
    use crate::stores::cortex::norm_key;
    let keys: HashSet<&str> = vocab.iter().map(String::as_str).collect();
    let entities: HashSet<&str> = keys
        .iter()
        .filter_map(|k| k.split_once('.').map(|(h, _)| h))
        .collect();
    if entities.is_empty() {
        return None;
    }
    let (head, tail) = entity.rsplit_once('.')?;
    if head.is_empty() || tail.is_empty() || norm_key(attribute) != "value" {
        return None;
    }
    let (hn, tn) = (norm_key(head), norm_key(tail));
    if entities.contains(hn.as_str()) || keys.contains(format!("{hn}.{tn}").as_str()) {
        Some((head.to_string(), tail.to_string()))
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Write-time dedup (`memory/graph_review.py:116-155`)
// ---------------------------------------------------------------------------

/// One existing entity as `near_duplicate_names` reads it.
pub struct NameRow {
    pub id: i64,
    pub canonical: String,
    pub display: String,
    pub aliases: Vec<String>,
}

/// `near_duplicate_names`: `(entity_id, display, score)` by score desc
/// (stable).
pub fn near_duplicate_names(
    name: &str,
    existing: &[NameRow],
    min_jaccard: f64,
    dismissed: &HashSet<(String, String)>,
) -> Vec<(i64, String, f64)> {
    use crate::dream::judge_agreement::variant_conflict;
    use crate::graph_review::token_set;
    if min_jaccard <= 0.0 {
        return Vec::new();
    }
    let cand = token_set(name);
    if cand.is_empty() {
        return Vec::new();
    }
    let canon = crate::storage::graph::norm_name(name);
    let mut out = Vec::new();
    for e in existing {
        if dismissed.contains(&sorted_pair(&canon, &e.canonical)) {
            continue;
        }
        if variant_conflict(name, &e.display) || variant_conflict(name, &e.canonical) {
            continue;
        }
        let mut best = 0.0f64;
        let variants = [e.canonical.as_str(), e.display.as_str()]
            .into_iter()
            .chain(e.aliases.iter().map(String::as_str));
        for variant in variants {
            let toks = token_set(variant);
            if toks.is_empty() {
                continue;
            }
            let inter = cand.intersection(&toks).count() as f64;
            let union = cand.union(&toks).count() as f64;
            let jac = inter / union;
            if jac > best {
                best = jac;
            }
        }
        if best >= min_jaccard {
            out.push((e.id, e.display.clone(), crate::graph_review::round3(best)));
        }
    }
    out.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));
    out
}

pub fn sorted_pair(a: &str, b: &str) -> (String, String) {
    if a <= b {
        (a.to_string(), b.to_string())
    } else {
        (b.to_string(), a.to_string())
    }
}

// ---------------------------------------------------------------------------
// Scopes (`utils/config.py:949-966`)
// ---------------------------------------------------------------------------

/// `ScopesConfig.scope_keys`.
pub fn scope_keys<'a>(
    exclude: &[String],
    rollup: &[(String, String)],
    sources: impl IntoIterator<Item = &'a str>,
) -> BTreeSet<String> {
    let key = |s: &str| crate::storage::graph::py_lower(crate::storage::py_strip(s));
    let excl: HashSet<String> = exclude.iter().map(|s| key(s)).collect();
    let roll: HashMap<String, String> = rollup.iter().map(|(k, v)| (key(k), key(v))).collect();
    let mut out = BTreeSet::new();
    for s in sources {
        let k = key(s);
        if k.is_empty() || excl.contains(&k) {
            continue;
        }
        if let Some(umb) = roll.get(&k)
            && !umb.is_empty()
            && *umb != k
            && !excl.contains(umb)
        {
            out.insert(umb.clone());
        }
        out.insert(k);
    }
    out
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/// Python `format(x, "g")`.
pub fn py_g(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.into();
    }
    let strip = |s: &str| -> String {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            s.to_string()
        }
    };
    let sci = format!("{x:.5e}");
    let (mant, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    if !(-4..6).contains(&exp) {
        format!(
            "{}e{}{:02}",
            strip(mant),
            if exp < 0 { '-' } else { '+' },
            exp.unsigned_abs()
        )
    } else {
        let decimals = (5 - exp).max(0) as usize;
        strip(&format!("{x:.decimals$}"))
    }
}

/// Python `repr(list_of_str)`.
pub fn py_list_repr(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| crate::storage::py_repr(s)).collect();
    format!("[{}]", inner.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected values from the real Python functions (`python -c` at the
    // repo root, CPython 3.11): see the test names.

    #[test]
    fn citation_backs_matches_python() {
        let cases: &[(&str, &str, &str, &str, bool, bool)] = &[
            (
                "emacs",
                "I tried vim but hated it; my editor is emacs",
                "Dev",
                "editor",
                true,
                true,
            ),
            (
                "vim",
                "I do not use vim as my editor",
                "Dev",
                "editor",
                true,
                false,
            ),
            (
                "vim",
                "I no longer use vim as my editor",
                "Dev",
                "editor",
                true,
                false,
            ),
            (
                "vim",
                "no longer serves vim as editor",
                "Dev",
                "editor",
                false,
                true,
            ),
            (
                "vim",
                "my editor is vimscript",
                "Dev",
                "editor",
                true,
                false,
            ),
            (
                "Postgres 17",
                "The staging DB runs POSTGRES   17 now.",
                "staging-db",
                "version",
                true,
                true,
            ),
            (
                "Postgres 17",
                "The staging db runs POSTGRES   17 now.",
                "staging-db",
                "version",
                true,
                true,
            ),
            (
                "prod-eu",
                "The deploy target moved to prod-eu.",
                "deploy",
                "target",
                true,
                true,
            ),
            ("prod-eu", "Moved to prod-eu.", "the", "do", true, false),
            (
                "green tea",
                "Bob prefers green tea, not coffee.",
                "Bob",
                "drink",
                true,
                true,
            ),
            (
                "green tea",
                "Bob drinks green tea, not coffee.",
                "Bob",
                "drink",
                true,
                true,
            ),
            (
                "green tea",
                "Bob's drink is green tea.",
                "Bob",
                "drink",
                true,
                true,
            ),
            ("x", "", "a", "b", true, false),
            ("café", "the cafe\u{301} is open", "the", "café", true, true),
            ("tea", "I don't drink tea", "I", "drink", true, false),
        ];
        for &(v, n, e, a, aff, want) in cases {
            assert_eq!(citation_backs(v, n, e, a, aff), want, "{v:?} / {n:?}");
        }
    }

    #[test]
    fn literal_violations_matches_python() {
        let cases: &[(&str, &str, &[&str])] = &[
            ("Postgres 17", "upgraded to PostgreSQL 17", &[]),
            ("Postgres 16", "upgraded to PostgreSQL 17", &["16"]),
            ("3-week break", "a three week break", &[]),
            ("v2.4.1", "release 2.4.1 shipped", &[]),
            ("PR-81", "merged #81 today", &[]),
            ("81", "merged pr-81 today", &[]),
            ("$1,200", "costs 1200 dollars", &[]),
            ("~3 months", "a while", &[]),
            ("due 2026-09-30", "no dates here", &[]),
            ("3.20", "version 3.2", &[]),
            ("08", "build 8", &[]),
            ("1-3 days", "one to three days", &[]),
            (
                "build-02 host",
                "Our CI cache lives on the build-02 host now.",
                &[],
            ),
            (
                "build-03 host",
                "Our CI cache lives on the build-02 host now.",
                &["03"],
            ),
            ("50%", "half", &["50"]),
            ("x 12", "   ", &[]),
            ("September 30th 2026 and 7", "on Sep 30 we had 7", &[]),
            ("2nd place", "came 2 overall", &[]),
        ];
        for &(v, c, want) in cases {
            let got = literal_violations(v, c);
            assert_eq!(got, want, "{v:?} / {c:?}");
        }
    }

    #[test]
    fn span_unbacked_matches_python() {
        assert_eq!(span_unbacked(None, "x"), Some("quote_missing"));
        assert_eq!(span_unbacked(Some(" !! "), "x"), Some("quote_missing"));
        assert_eq!(
            span_unbacked(Some("Green  TEA!"), "Bob likes green tea."),
            None
        );
        assert_eq!(
            span_unbacked(Some("tea green"), "Bob likes green tea."),
            Some("quote_unverified")
        );
        assert_eq!(span_unbacked(Some("café"), "the cafe\u{301}"), None);
        assert_eq!(span_unbacked(Some("x"), ""), Some("quote_unverified"));
    }

    #[test]
    fn carrier_measures_match_python() {
        let t = content_tokens("Never deploy on Fridays; you MUST tag the release.");
        let want: BTreeSet<String> = ["deploy", "fridays", "tag", "release"]
            .into_iter()
            .map(String::from)
            .collect();
        assert_eq!(t, want);
        assert!(contains_verbatim(
            "a  rule:\nnever deploy on Fridays.",
            "never deploy\ton Fridays"
        ));
        assert!(!contains_verbatim("never deploy", "Never deploy"));
        assert!(!contains_verbatim("x", "   "));
    }

    #[test]
    fn origins_and_trust_match_python() {
        assert_eq!(origin_from_source(Some(" Conversation ")), Some("user"));
        assert_eq!(origin_from_source(Some("claude")), Some("agent"));
        assert_eq!(origin_from_source(Some("tool")), Some("action"));
        assert_eq!(origin_from_source(Some("log")), None);
        assert_eq!(origin_from_source(None), None);
        let e = TrustEntry {
            source: Some("agent"),
            episode_id: None,
            authority: None,
        };
        assert!(quarantine_low_trust(Some(&e), &[]));
        assert!(!quarantine_low_trust(Some(&e), &["agent".into()]));
        assert_eq!(quarantine_witness(Some(&e)), "src:agent");
        let q = TrustEntry {
            source: Some("user"),
            episode_id: Some("abc"),
            authority: Some("quoted"),
        };
        assert!(quarantine_low_trust(Some(&q), &[]));
        assert_eq!(quarantine_witness(Some(&q)), "ep:abc");
        assert!(quarantine_low_trust(None, &[]));
        assert_eq!(quarantine_witness(None), "unbacked");
    }

    #[test]
    fn unflatten_matches_python() {
        let vocab: Vec<String> = ["0-9-0-release.deployment-status", "dev-box.os"]
            .into_iter()
            .map(String::from)
            .collect();
        assert_eq!(
            unflatten_slot_key("0-9-0-release.deployment-status", "Value", &vocab),
            Some(("0-9-0-release".into(), "deployment-status".into()))
        );
        assert_eq!(unflatten_slot_key("llama.cpp", "value", &vocab), None);
        assert_eq!(
            unflatten_slot_key("Dev Box.kernel", "value", &vocab),
            Some(("Dev Box".into(), "kernel".into()))
        );
        assert_eq!(unflatten_slot_key("dev-box.os", "version", &vocab), None);
        assert_eq!(unflatten_slot_key("x.y", "value", &[]), None);
    }

    #[test]
    fn near_duplicate_names_matches_python() {
        let rows = vec![
            NameRow {
                id: 1,
                canonical: "billing-service".into(),
                display: "Billing Service".into(),
                aliases: vec![],
            },
            NameRow {
                id: 2,
                canonical: "billing-service-rewrite".into(),
                display: "billing service rewrite".into(),
                aliases: vec![],
            },
            NameRow {
                id: 3,
                canonical: "qwen3-8b".into(),
                display: "Qwen3 8B".into(),
                aliases: vec![],
            },
            NameRow {
                id: 4,
                canonical: "payments".into(),
                display: "Payments".into(),
                aliases: vec!["billing-svc".into()],
            },
        ];
        let got = near_duplicate_names("Billing Service Rewrite", &rows, 0.6, &HashSet::new());
        assert_eq!(
            got,
            vec![
                (2, "billing service rewrite".into(), 1.0),
                (1, "Billing Service".into(), 0.667)
            ]
        );
        let got = near_duplicate_names("Qwen3 4B", &rows, 0.3, &HashSet::new());
        assert!(got.is_empty());
        let mut dismissed = HashSet::new();
        dismissed.insert(sorted_pair("billing-service-rewrite", "billing-service"));
        let got = near_duplicate_names("Billing Service Rewrite", &rows, 0.6, &dismissed);
        assert_eq!(got, vec![(2, "billing service rewrite".into(), 1.0)]);
        assert!(near_duplicate_names("ab", &rows, 0.6, &HashSet::new()).is_empty());
    }

    #[test]
    fn scope_keys_and_formats_match_python() {
        let got = scope_keys(
            &["status".into(), "claude".into()],
            &[("Pseudolife-MCP".into(), "pseudolife".into())],
            ["Pseudolife-MCP", "status", " Home ", ""],
        );
        let want: BTreeSet<String> = ["pseudolife-mcp", "pseudolife", "home"]
            .into_iter()
            .map(String::from)
            .collect();
        assert_eq!(got, want);
        assert_eq!(py_g(0.45), "0.45");
        assert_eq!(py_g(0.175), "0.175");
        assert_eq!(py_g(0.7), "0.7");
        assert_eq!(py_g(1e-5), "1e-05");
        assert_eq!(py_g(1234567.0), "1.23457e+06");
        assert_eq!(py_g(3.0), "3");
        assert_eq!(py_list_repr(&["a".into(), "b'c".into()]), "['a', \"b'c\"]");
    }
}
