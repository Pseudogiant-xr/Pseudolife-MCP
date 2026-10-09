//! Possible-conflict detection (`memory/contradiction.py`), spec W1.7.
//!
//! Paths 0-3 of `detect_contradictions`; path 4 (NLI) needs a scorer the
//! service never constructs. Patterns go through `slots::PyRegex` for
//! Python's `\b`, `\s` and IGNORECASE. Each cue list is one alternation: a
//! search for `p1|p2|…` finds a match exactly when some `pi` does, which is
//! all `_matches_any` / `_has_negation` ask.
#![allow(dead_code)]

use crate::http::py_strip;
use crate::slots::{PyRegex, Slot};
use serde_json::Value;
use std::cell::OnceCell;
use std::collections::{HashMap, HashSet};
use std::sync::{LazyLock, OnceLock};
use unicode_casefold::UnicodeCaseFold;

// contradiction.py:61-77. Compared against the f32 cosine widened to f64,
// as Python compares `tensor.tolist()` floats.
const NEGATION_SIM_THRESHOLD: f64 = 0.70;
const REPLACEMENT_SIM_THRESHOLD: f64 = 0.80;
const REPLACEMENT_TOKEN_OVERLAP_THRESHOLD: f64 = 0.50;
const STATE_TRANSITION_SIM_THRESHOLD_SLOT: f64 = 0.15;
const STATE_TRANSITION_SIM_THRESHOLD_CONTENT: f64 = 0.35;
const STATE_TRANSITION_SIM_THRESHOLD_PRONOUN: f64 = 0.35;

fn any_of(patterns: &[&str], ignorecase: bool, dotall: bool) -> PyRegex {
    let joined: Vec<String> = patterns.iter().map(|p| format!("(?:{p})")).collect();
    PyRegex::new(&joined.join("|"), ignorecase, dotall)
}

/// `_NEGATION_PATTERNS` (contradiction.py:84-114).
static NEGATION: LazyLock<PyRegex> = LazyLock::new(|| {
    any_of(
        &[
            r"\bnot\b",
            r"\bno\b",
            r"\bnever\b",
            r"\bdon'?t\b",
            r"\bdoesn'?t\b",
            r"\bwon'?t\b",
            r"\bcan'?t\b",
            r"\bisn'?t\b",
            r"\baren'?t\b",
            r"\bwasn'?t\b",
            r"\bweren'?t\b",
            r"\bneither\b",
            r"\bnor\b",
            r"\bactually\b",
            r"\binstead\b",
            r"\brather\b",
            r"\bcontrary\b",
            r"\bincorrect\b",
            r"\bwrong\b",
            r"\bfalse\b",
            r"\bmistaken\b",
            r"\bcorrect(?:ion|ed|ly)?\b",
            r"\bupdate[ds]?\b",
            r"\bchange[ds]?\b",
            r"\bno longer\b",
            r"\bused to\b",
        ],
        true,
        false,
    )
});

/// `POSSESSION_LOSS_PATTERNS` (contradiction.py:205-238).
static LOSS: LazyLock<PyRegex> = LazyLock::new(|| {
    any_of(
        &[
            r"\bgave\b.{0,40}?\baway\b",
            r"\bgave\s+up\b",
            r"\bgiv(?:ing|e[ns]?)\b.{0,40}?\baway\b",
            r"\bgot\s+rid\s+of\b",
            r"\bsold\b",
            r"\blost\b",
            r"\brehomed\b",
            r"\bdonated\b",
            r"\bpassed\s+away\b",
            r"\bdied\b",
            r"\bno\s+longer\s+(?:have|own|with|around|together|here)\b",
            r"\bis\s+(?:no\s+longer|not)\s+with\s+(?:us|me|you|them)\b",
            r"\bis\s+gone\b",
            r"\bisn'?t\s+(?:here|around|with)\b",
            r"\bused\s+to\s+have\b",
            r"\bused\s+to\s+own\b",
            r"\bgot\s+divorced\b",
            r"\bbroke\s+up\b",
            r"\bmoved\s+(?:out|away)\b",
            r"\bquit\b",
            r"\bthrew\b.{0,40}?\b(?:away|out)\b",
            r"\bdiscard(?:ed|ing)?\b",
            r"\babandon(?:ed|ing)?\b",
            r"\bupgrad(?:ed|ing)\s+(?:to|from)\b",
            r"\bswitch(?:ed|ing)\s+(?:to|from)\b",
            r"\breplac(?:ed|ing)\b.{0,40}?\bwith\b",
            r"\btrad(?:ed|ing)\b.{0,40}?\bfor\b",
        ],
        true,
        true,
    )
});

/// `POSSESSION_GAIN_PATTERNS` (contradiction.py:243-255).
static GAIN: LazyLock<PyRegex> = LazyLock::new(|| {
    any_of(
        &[
            r"\bi\s+have\b",
            r"\bi\s+own\b",
            r"\bi\s+got\b",
            r"\bi'?ve\s+got\b",
            r"\bi'?ve\s+had\b",
            r"\bmy\s+[A-Za-z]",
            r"\bnamed\b",
            r"\bcalled\b",
        ],
        true,
        false,
    )
});

/// `_STOP_WORDS` (contradiction.py:137-145).
const STOP_WORDS: [&str; 69] = [
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "am", "i", "you", "he",
    "she", "it", "we", "they", "me", "him", "her", "us", "them", "my", "your", "his", "its", "our",
    "their", "this", "that", "these", "those", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "as", "by", "from", "into", "about", "so", "do", "does", "did", "have", "has",
    "had", "will", "would", "could", "should", "can", "may", "might", "s", "t", "ll", "re", "ve",
    "d", "m",
];

static WORD: LazyLock<PyRegex> =
    LazyLock::new(|| PyRegex::new(r"[A-Za-z][A-Za-z'\-]*|\d+", false, false));
/// `_SLOT_VALUE_RE` (contradiction.py:153-161), VERBOSE comments dropped.
static SLOT_VALUE: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(
        r#""[^"]+"|'[^']+'|\b[A-Z][A-Za-z\-]+\b|\b\d+(?:\.\d+)?\b"#,
        false,
        false,
    )
});
static REFERENT_PRONOUN: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(
        r"\b(?:it|he|she|him|her|them|they|his|hers|its|their|theirs)\b",
        true,
        false,
    )
});
/// `cortex._KEY_SEP_RE` (cortex.py:682).
static KEY_SEP: LazyLock<PyRegex> = LazyLock::new(|| PyRegex::new(r"[\s._\-/]+", false, false));

// ---- cortex normalisation (cortex.py:685-706) -------------------------------

/// Python `str.casefold()` (full case folding). `unicode-casefold` carries
/// Unicode 9 tables; a code point it leaves alone that newer Unicode gives a
/// lowercase (Georgian Mtavruli, Medefaidrin, Vithkuqi, Latin additions)
/// folds to that lowercase, as CaseFolding.txt does for every cased letter
/// added since, except the Cherokee capitals, which fold to themselves.
fn casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        let before = out.len();
        out.extend(c.case_fold());
        if out[before..].chars().eq([c]) && !('\u{13A0}'..='\u{13F5}').contains(&c) {
            let mut lower = c.to_lowercase();
            if let (Some(l), None) = (lower.next(), lower.next()) {
                out.truncate(before);
                out.push(l);
            }
        }
    }
    out
}

/// `cortex._norm_key`: casefold, every run of separators to one hyphen,
/// hyphens trimmed.
pub fn norm_key(s: &str) -> String {
    let folded = casefold(py_strip(s));
    KEY_SEP
        .replace_all(&folded, "-")
        .trim_matches('-')
        .to_string()
}

/// `cortex._norm_value`.
pub fn norm_value(s: &str) -> String {
    casefold(py_strip(s))
}

// ---- Text features (contradiction.py:117-196, 258-345) ----------------------

fn content_tokens(text: &str) -> HashSet<String> {
    WORD.findall(text)
        .into_iter()
        .map(str::to_lowercase)
        .filter(|t| !STOP_WORDS.contains(&t.as_str()))
        .collect()
}

/// `_jaccard_overlap`; an empty token list is empty as a set too.
fn jaccard(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let inter = a.intersection(b).count();
    let union = a.len() + b.len() - inter;
    inter as f64 / union as f64
}

fn slot_values(text: &str) -> HashSet<String> {
    SLOT_VALUE
        .findall(text)
        .into_iter()
        .map(|tok| {
            let quoted = tok.starts_with(['"', '\'']) && tok.ends_with(['"', '\'']);
            let tok = if quoted { &tok[1..tok.len() - 1] } else { tok };
            tok.to_lowercase()
        })
        .collect()
}

/// `(gain, loss)` cues of a text.
fn cues(text: &str) -> (bool, bool) {
    (GAIN.is_match(text), LOSS.is_match(text))
}

/// One existing entry as the scan sees it.
pub struct Candidate<'a> {
    pub text: &'a str,
    /// Stored rows; a row that is not four strings makes the entry slotless
    /// here (Python's unpacking would raise and fail the whole store).
    pub slots: &'a [Value],
    pub superseded: bool,
    /// Cosine with the new text, both sides L2-normalised f32.
    pub sim: f32,
    /// The per-entry `(gain, loss)` memo (`MemoryEntry.cue_flags`).
    pub cues: &'a OnceLock<(bool, bool)>,
}

/// The new text's side, derived once per scan: Python recomputes these per
/// entry, with identical results.
struct NewSide<'n> {
    text: &'n str,
    /// `(norm_key(entity), norm_key(attribute), norm_value(value), polarity)`.
    slots: Vec<(String, String, String, &'n str)>,
    cues: (bool, bool),
    negation: OnceCell<bool>,
    slot_values: OnceCell<HashSet<String>>,
    content: OnceCell<HashSet<String>>,
}

impl<'n> NewSide<'n> {
    fn new(text: &'n str, slots: &'n [Slot]) -> NewSide<'n> {
        NewSide {
            text,
            slots: slots
                .iter()
                .map(|s| {
                    let polarity = s.polarity.as_str();
                    (
                        norm_key(&s.entity),
                        norm_key(&s.attribute),
                        norm_value(&s.value),
                        polarity,
                    )
                })
                .collect(),
            cues: cues(text),
            negation: OnceCell::new(),
            slot_values: OnceCell::new(),
            content: OnceCell::new(),
        }
    }

    fn negation(&self) -> bool {
        *self.negation.get_or_init(|| NEGATION.is_match(self.text))
    }

    fn slot_values(&self) -> &HashSet<String> {
        self.slot_values.get_or_init(|| slot_values(self.text))
    }

    fn content(&self) -> &HashSet<String> {
        self.content.get_or_init(|| content_tokens(self.text))
    }
}

/// `_slot_contradiction` (contradiction.py:282-312): a different value or
/// polarity at the same normalised `(entity, attribute)`; the last row of a
/// repeated key wins, as in the dict comprehension.
fn slot_contradiction(new: &NewSide, rows: &[Value]) -> bool {
    if new.slots.is_empty() || rows.is_empty() {
        return false;
    }
    let Some(entry) = rows
        .iter()
        .map(Slot::from_json)
        .collect::<Option<Vec<Slot>>>()
    else {
        return false;
    };
    let existing: HashMap<(String, String), (String, &str)> = entry
        .iter()
        .map(|s| {
            let key = (norm_key(&s.entity), norm_key(&s.attribute));
            (key, (norm_value(&s.value), s.polarity.as_str()))
        })
        .collect();
    new.slots.iter().any(|(e, a, v, p)| {
        existing
            .get(&(e.clone(), a.clone()))
            .is_some_and(|(pv, pp)| pv != v || pp != p)
    })
}

/// Paths 0-3 for one non-superseded entry (contradiction.py:568-601).
fn flags_with(new: &NewSide, c: &Candidate) -> bool {
    // Path 0: slot identity, embedding-free.
    if slot_contradiction(new, c.slots) {
        return true;
    }
    let sim = f64::from(c.sim);
    let entry_values: OnceCell<HashSet<String>> = OnceCell::new();
    let entry_content: OnceCell<HashSet<String>> = OnceCell::new();
    let values = || entry_values.get_or_init(|| slot_values(c.text));
    let content = || entry_content.get_or_init(|| content_tokens(c.text));

    // Path 1: negation asymmetry.
    if sim >= NEGATION_SIM_THRESHOLD && new.negation() != NEGATION.is_match(c.text) {
        return true;
    }

    // Path 2: affirmative replacement (`_looks_like_replacement`).
    if sim >= REPLACEMENT_SIM_THRESHOLD {
        let (a, b) = (new.slot_values(), values());
        if a.symmetric_difference(b).next().is_some()
            && (jaccard(new.content(), content()) >= REPLACEMENT_TOKEN_OVERLAP_THRESHOLD
                || !a.is_disjoint(b))
        {
            return true;
        }
    }

    // Path 3: gain/loss asymmetry with an anchor tier
    // (`_state_transition_anchor_kind`).
    let (a_gain, a_loss) = new.cues;
    let (b_gain, b_loss) = *c.cues.get_or_init(|| cues(c.text));
    if !((a_gain && b_loss) || (b_gain && a_loss)) {
        return false;
    }
    let floor = if !new.slot_values().is_disjoint(values()) {
        STATE_TRANSITION_SIM_THRESHOLD_SLOT
    } else if !new.content().is_disjoint(content()) {
        STATE_TRANSITION_SIM_THRESHOLD_CONTENT
    } else {
        let (loss_text, loss_values) = if b_loss {
            (c.text, values())
        } else {
            (new.text, new.slot_values())
        };
        if REFERENT_PRONOUN.is_match(loss_text) && loss_values.is_empty() {
            STATE_TRANSITION_SIM_THRESHOLD_PRONOUN
        } else {
            return false;
        }
    };
    sim >= floor
}

/// `bool(detect_contradictions(...))` for one band: true when any
/// non-superseded candidate is flagged by paths 0-3.
pub fn any_contradiction<'a>(
    new_text: &str,
    new_slots: &[Slot],
    mut candidates: impl Iterator<Item = Candidate<'a>>,
) -> bool {
    let new = NewSide::new(new_text, new_slots);
    candidates.any(|c| !c.superseded && flags_with(&new, &c))
}

/// The per-entry decision of `detect_contradictions`.
pub fn flags(new_text: &str, new_slots: &[Slot], c: &Candidate) -> bool {
    !c.superseded && flags_with(&NewSide::new(new_text, new_slots), c)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::slots::tests::{compare_mapping, golden, rows, verdict};
    use serde_json::json;

    #[test]
    fn golden_contradiction_cases_match_python() {
        let g = golden();
        let sims: Vec<f32> = g["contradiction_sims"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| {
                let s = s.as_f64().unwrap();
                assert_eq!(f64::from(s as f32), s, "a sim is not an f32");
                s as f32
            })
            .collect();
        let cases = g["contradiction"].as_array().unwrap();
        assert!(cases.len() >= 300, "corpus shrank to {}", cases.len());
        let (mut checked, mut declared, mut bad) = (0, Vec::new(), Vec::new());
        for c in cases {
            let (new, entry) = (c["new"].as_str().unwrap(), c["entry"].as_str().unwrap());
            if let Some(why) = c.get("declared") {
                declared.push(format!("{new:?} / {entry:?}: {why}"));
                continue;
            }
            let new_slots = rows(&c["new_slots"]);
            let entry_rows = c["entry_slots"].as_array().unwrap();
            let hits = c["hits"].as_str().unwrap();
            for (&sim, want) in sims.iter().zip(hits.chars().map(|h| h == '1')) {
                checked += 1;
                let memo = OnceLock::new();
                let cand = || Candidate {
                    text: entry,
                    slots: entry_rows,
                    superseded: c["superseded"].as_bool().unwrap(),
                    sim,
                    cues: &memo,
                };
                let got = flags(new, &new_slots, &cand());
                let got_any = any_contradiction(new, &new_slots, std::iter::once(cand()));
                if got != want || got_any != want {
                    bad.push(format!(
                        "[{}] new {new:?} entry {entry:?} sim {sim}: flags {got} any {got_any} want {want}",
                        c["origin"]
                    ));
                }
            }
        }
        verdict("contradiction", checked, &declared, &bad);
    }

    #[test]
    fn golden_norm_cases_match_python() {
        let mut bad = Vec::new();
        let cases = golden()["norm"].as_array().unwrap();
        for c in cases {
            let s = c["s"].as_str().unwrap();
            let (key, value) = (norm_key(s), norm_value(s));
            if key != c["key"].as_str().unwrap() || value != c["value"].as_str().unwrap() {
                bad.push(format!("{s:?}: key {key:?} value {value:?}, want {c}"));
            }
        }
        verdict("norm", cases.len(), &[], &bad);
    }

    #[test]
    fn casefold_matches_python_over_every_code_point() {
        compare_mapping("casefold", |c| casefold(&c.to_string()));
    }

    #[test]
    fn cue_memo_is_filled_once_and_read() {
        let gain = "I have a cat named Jacque";
        let memo = OnceLock::new();
        let c = Candidate {
            text: "Jacque died",
            slots: &[],
            superseded: false,
            sim: 0.2,
            cues: &memo,
        };
        assert!(flags(gain, &[], &c));
        assert_eq!(memo.get(), Some(&(false, true)));
        // A memo claiming no cues is believed, as `entry.cue_flags` is.
        let stale = OnceLock::from((false, false));
        let c = Candidate { cues: &stale, ..c };
        assert!(!flags(gain, &[], &c));
    }

    #[test]
    fn malformed_rows_and_superseded_entries() {
        let new = [Slot::from_json(&json!(["a", "b", "x", "+"])).unwrap()];
        let memo = OnceLock::new();
        let good = [json!(["a", "b", "y", "+"])];
        let c = Candidate {
            text: "omega",
            slots: &good,
            superseded: false,
            sim: 0.0,
            cues: &memo,
        };
        assert!(flags("alpha", &new, &c));
        let bad = [json!(["a", "b", "y", "+"]), json!(["a", "b", "y"])];
        assert!(!flags("alpha", &new, &Candidate { slots: &bad, ..c }));
        assert!(!flags(
            "alpha",
            &new,
            &Candidate {
                superseded: true,
                slots: &good,
                ..c
            }
        ));
        let memo2 = OnceLock::new();
        let entries = [
            Candidate {
                text: "omega",
                slots: &good,
                superseded: true,
                sim: 0.0,
                cues: &memo,
            },
            Candidate {
                text: "omega",
                slots: &good,
                superseded: false,
                sim: 0.0,
                cues: &memo2,
            },
        ];
        assert!(any_contradiction("alpha", &new, entries.into_iter()));
        assert!(!any_contradiction("alpha", &new, std::iter::empty()));
    }
}
