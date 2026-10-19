//! `_mentions` (`memory/recall.py:20-103`): a word-bounded, case-insensitive
//! test of whether a text names an entity. Python compiles the name into an
//! `re.IGNORECASE` str pattern (`_mention_re`, `recall.py:44-53`); this is a
//! hand-written matcher over the same `_sre` Unicode 14 data
//! (`recall_unicode14.rs`), since the `regex` crate's `\w`, `\s` and case
//! folding are not Python's.
//!
//! The pattern is `BEFORE body AFTER`, where body is literal characters and,
//! for each separator run between two non-empty name pieces, `[\s_\-/]+`.
//! Each literal consumes exactly one character. A separator run is always
//! followed by a literal that is not a separator and cannot match one (the
//! generator checks this), so the greedy run never gives a character back:
//! the match from a given start is unique, and so is its end.

use crate::recall_unicode14 as data;

/// `_MARK_REACH` (`recall.py:65`).
const MARK_REACH: usize = 3;

fn contains(ranges: &[(u32, u32)], c: char) -> bool {
    let code = u32::from(c);
    let index = ranges.partition_point(|&(lo, _)| lo <= code);
    index > 0 && code <= ranges[index - 1].1
}

/// `\w` for str patterns: `Py_UNICODE_ISALNUM` or `_`.
pub(crate) fn is_word(c: char) -> bool {
    contains(data::WORD, c)
}

/// `[\s_\-/]`, also `_NAME_SEP`'s split class (`recall.py:38`).
fn is_sep(c: char) -> bool {
    matches!(c, '_' | '-' | '/') || contains(data::SPACE, c)
}

fn is_apostrophe(c: char) -> bool {
    matches!(c, '\'' | '\u{2019}')
}

pub(crate) fn is_mark(c: char) -> bool {
    contains(data::MARK, c)
}

/// `_sre.unicode_tolower`: the simple (single code point) lowercase.
fn lower(c: char) -> char {
    let code = u32::from(c);
    let index = data::LOWER.partition_point(|&(start, ..)| start <= code);
    if index == 0 {
        return c;
    }
    let (start, end, step, delta) = data::LOWER[index - 1];
    if code > end || (code - start) % step != 0 {
        return c;
    }
    char::from_u32(code.wrapping_add_signed(delta)).expect("generated lowercase is a char")
}

/// One literal of the compiled name under `re.IGNORECASE`
/// (`re/_compiler.py:_compile`, the LITERAL branch).
enum Literal {
    /// Uncased (`_sre.unicode_iscased` false): only the character itself.
    Exact(char),
    /// Text characters whose lowercase is one of these: the literal's own
    /// lowercase plus its `_EXTRA_CASES` class.
    Folded(Vec<char>),
}

impl Literal {
    fn new(c: char) -> Self {
        if !contains(data::CASED, c) {
            return Literal::Exact(c);
        }
        let lo = lower(c);
        let mut accept = vec![lo];
        if let Ok(index) = data::FIXES.binary_search_by_key(&u32::from(lo), |&(code, _)| code) {
            accept.extend(
                data::FIXES[index]
                    .1
                    .iter()
                    .map(|&k| char::from_u32(k).expect("generated case fix is a char")),
            );
        }
        Literal::Folded(accept)
    }

    fn matches(&self, c: char) -> bool {
        match self {
            Literal::Exact(want) => c == *want,
            Literal::Folded(accept) => accept.contains(&lower(c)),
        }
    }
}

enum Unit {
    Literal(Literal),
    /// `[\s_\-/]+`, greedy.
    Separators,
}

/// `_mention_re`'s body (`recall.py:44-53`): `re.split` on `_NAME_SEP` with
/// the run captured; a run between two non-empty pieces becomes the class,
/// every other piece (leading and trailing runs included) is literal.
fn compile(name: &[char]) -> Vec<Unit> {
    let mut pieces: Vec<(&[char], bool)> = Vec::new();
    let mut start = 0;
    while start <= name.len() {
        let text_end = name[start..]
            .iter()
            .position(|&c| is_sep(c))
            .map_or(name.len(), |offset| start + offset);
        pieces.push((&name[start..text_end], false));
        if text_end == name.len() {
            break;
        }
        let sep_end = name[text_end..]
            .iter()
            .position(|&c| !is_sep(c))
            .map_or(name.len(), |offset| text_end + offset);
        pieces.push((&name[text_end..sep_end], true));
        start = sep_end;
    }
    let mut units = Vec::new();
    for (index, &(piece, separator)) in pieces.iter().enumerate() {
        if separator && !pieces[index - 1].0.is_empty() && !pieces[index + 1].0.is_empty() {
            units.push(Unit::Separators);
        } else {
            units.extend(piece.iter().map(|&c| Unit::Literal(Literal::new(c))));
        }
    }
    units
}

/// The body's end when it matches at `start`, else `None`.
fn body_end(units: &[Unit], text: &[char], start: usize) -> Option<usize> {
    let mut pos = start;
    for unit in units {
        match unit {
            Unit::Literal(literal) => {
                if !text.get(pos).is_some_and(|&c| literal.matches(c)) {
                    return None;
                }
                pos += 1;
            }
            Unit::Separators => {
                let run = text[pos..].iter().take_while(|&&c| is_sep(c)).count();
                if run == 0 {
                    return None;
                }
                pos += run;
            }
        }
    }
    Some(pos)
}

/// `_MENTION_BEFORE` and `_MENTION_AFTER` (`recall.py:32-33`) around a body
/// match, given the characters before it (nearest last) and after it.
fn bounded(before: &[char], after: &[char], s: &Literal) -> bool {
    let n = before.len();
    // (?<![\w.])
    if n >= 1 && (is_word(before[n - 1]) || before[n - 1] == '.') {
        return false;
    }
    // (?<!\w['’])
    if n >= 2 && is_word(before[n - 2]) && is_apostrophe(before[n - 1]) {
        return false;
    }
    let word_at = |k: usize| after.get(k).is_some_and(|&c| is_word(c));
    // (?!\w)
    if word_at(0) {
        return false;
    }
    // (?!\.\w)
    if after.first() == Some(&'.') && word_at(1) {
        return false;
    }
    // (?!['’](?!s(?!\w))\w)
    if after.first().is_some_and(|&c| is_apostrophe(c)) && word_at(1) {
        let possessive = s.matches(after[1]) && !word_at(2);
        if !possessive {
            return false;
        }
    }
    true
}

/// `_marks_as_letters` (`recall.py:68-71`).
fn marks_as_letters(chars: &[char]) -> Vec<char> {
    chars
        .iter()
        .map(|&c| if is_mark(c) { 'a' } else { c })
        .collect()
}

/// `_mentions` (`recall.py:82-103`): does `text` name `name`? Each start is
/// accepted when the pattern matches there in the real text (the
/// `pattern.search` loop) and again with the combining marks within
/// `MARK_REACH` outside the match read as letters (`_mark_bounded`). The body
/// re-matches identically there, so only the lookarounds are re-checked.
pub(crate) fn mentions(text: &str, name: &str) -> bool {
    if text.is_empty() || name.is_empty() {
        return false;
    }
    let name: Vec<char> = name.chars().collect();
    let text: Vec<char> = text.chars().collect();
    let units = compile(&name);
    let s = Literal::new('s');
    (0..=text.len()).any(|start| {
        let Some(end) = body_end(&units, &text, start) else {
            return false;
        };
        if !bounded(&text[..start], &text[end..], &s) {
            return false;
        }
        let before = marks_as_letters(&text[start.saturating_sub(MARK_REACH)..start]);
        let after = marks_as_letters(&text[end..(end + MARK_REACH).min(text.len())]);
        bounded(&before, &after, &s)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    const GOLDEN: &str = include_str!("../harness/goldens/recall-mentions.json");

    #[test]
    fn mentions_matches_python() {
        let cases: Vec<(String, String, bool)> = serde_json::from_str(GOLDEN).unwrap();
        assert!(cases.len() >= 3000, "golden too small: {}", cases.len());
        let mut bad = Vec::new();
        for (text, name, want) in &cases {
            let got = mentions(text, name);
            if got != *want {
                bad.push(format!("{text:?} / {name:?}: rust {got}, python {want}"));
            }
        }
        assert!(
            bad.is_empty(),
            "{} mismatches:\n{}",
            bad.len(),
            bad.join("\n")
        );
    }

    fn chars() -> impl Iterator<Item = char> {
        (0..0x110000_u32).filter_map(char::from_u32)
    }

    fn flag_digest(predicate: impl Fn(char) -> bool) -> String {
        let bytes: Vec<u8> = chars().map(|c| u8::from(predicate(c))).collect();
        hex::encode(Sha256::digest(bytes))
    }

    #[test]
    fn tables_match_entire_oracle_range() {
        assert_eq!(flag_digest(is_word), data::WORD_SHA256);
        assert_eq!(
            flag_digest(|c| contains(data::SPACE, c)),
            data::SPACE_SHA256
        );
        assert_eq!(flag_digest(is_mark), data::MARK_SHA256);
        assert_eq!(
            flag_digest(|c| contains(data::CASED, c)),
            data::CASED_SHA256
        );
        let mut digest = Sha256::new();
        for c in chars() {
            digest.update(u32::from(c).to_le_bytes());
            digest.update(u32::from(lower(c)).to_le_bytes());
        }
        assert_eq!(hex::encode(digest.finalize()), data::LOWER_SHA256);
        let mut digest = Sha256::new();
        for &(lo, extra) in data::FIXES {
            digest.update(lo.to_le_bytes());
            digest.update(u32::try_from(extra.len()).unwrap().to_le_bytes());
            for k in extra {
                digest.update(k.to_le_bytes());
            }
        }
        assert_eq!(hex::encode(digest.finalize()), data::FIXES_SHA256);
    }

    #[test]
    fn ignorecase_follows_sre_not_simple_folding() {
        for (text, name, want) in [
            ("the KELVIN scale", "\u{212a}elvin", true),
            ("\u{212a}elvin scale", "kelvin", true),
            ("gıt push", "git", true),
            ("GİT push", "git", true),
            ("ſerver up", "server", true),
            ("ΟΔΟΣ", "οδος", true),
            ("ϑeta", "θeta", true),
            ("payments_db", "payments db", true),
            ("payments_db", "payments", false),
            ("payments - db", "payments_db", true),
            ("node.js", "node", false),
            ("the bench server.", "bench server", true),
            ("don't", "don", false),
            ("server's config", "server", true),
            ("cafe\u{301}", "cafe", false),
        ] {
            assert_eq!(mentions(text, name), want, "{text:?} / {name:?}");
        }
    }
}
