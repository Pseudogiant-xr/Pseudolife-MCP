//! Text checks the board applies before it keeps anything (spec BS1-BS2):
//! `_string`, the category-C test, `reserved_name`'s skeleton, digest
//! excerpts and the board name. Unicode predicates come from the shim's
//! pinned CPython 3.11 tables (Unicode 14); see the declared divergence.

use super::error::{BoardError, Result};
use super::unicode14;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;
use unicode_normalization::char::canonical_combining_class;

fn in_ranges(ranges: &[(u32, u32)], c: char) -> bool {
    let point = c as u32;
    let index = ranges.partition_point(|(start, _)| *start <= point);
    index > 0 && point <= ranges[index - 1].1
}

/// `unicodedata.category(c)[0] == "C"` (control, format, surrogate,
/// private use, unassigned).
pub fn category_c(c: char) -> bool {
    in_ranges(unicode14::CATEGORY_C, c)
}

/// `str.isalnum()` for one character.
pub fn alnum(c: char) -> bool {
    in_ranges(unicode14::ALPHANUMERIC, c)
}

/// `str.isspace()` for one character.
pub fn py_space(c: char) -> bool {
    matches!(c as u32, 0x09..=0x0d | 0x1c..=0x20 | 0x85 | 0xa0 | 0x1680 | 0x2000..=0x200a
        | 0x2028..=0x2029 | 0x202f | 0x205f | 0x3000)
}

/// `str.strip()`.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

/// `" ".join(s.split())`.
pub fn collapse_spaces(s: &str) -> String {
    s.split(py_space)
        .filter(|p| !p.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// `str.casefold()` (CPython 3.11).
pub fn casefold(s: &str) -> String {
    s.chars()
        .flat_map(|c| {
            match unicode14::FOLD_CORRECTIONS.binary_search_by_key(&c, |(source, _)| *source) {
                Ok(index) => unicode14::FOLD_CORRECTIONS[index]
                    .1
                    .chars()
                    .collect::<Vec<_>>(),
                Err(_) => c.to_string().case_fold().collect::<Vec<_>>(),
            }
        })
        .collect()
}

/// `len(s)` in code points.
pub fn py_len(s: &str) -> usize {
    s.chars().count()
}

/// `_string(value, limit, field, empty=...)`: the value already is a str
/// here (the caller refuses other types with the same code). Rust strings
/// cannot hold a surrogate, so only the C0 test remains.
pub fn check_string(value: &str, limit: usize, field: &str, empty: bool) -> Result<()> {
    if py_len(value) > limit
        || (!empty && py_strip(value).is_empty())
        || value.chars().any(|c| (c as u32) < 32)
    {
        return Err(BoardError::code(format!("invalid_{field}")));
    }
    Ok(())
}

/// `_string` for a JSON value: not a str is the same refusal.
pub fn check_value<'a>(
    value: &'a serde_json::Value,
    limit: usize,
    field: &str,
    empty: bool,
) -> Result<&'a str> {
    let Some(s) = value.as_str() else {
        return Err(BoardError::code(format!("invalid_{field}")));
    };
    check_string(s, limit, field, empty)?;
    Ok(s)
}

/// `_CONFUSABLES` (`storage/coordination.py:408-422`), applied before casefolding.
fn confusable(c: char) -> char {
    match c {
        'а' => 'a',
        'е' => 'e',
        'о' => 'o',
        'р' => 'p',
        'с' => 'c',
        'у' => 'y',
        'х' => 'x',
        'і' => 'i',
        'ј' => 'j',
        'ѕ' => 's',
        'ԁ' => 'd',
        'һ' => 'h',
        'ӏ' => 'l',
        'ԛ' => 'q',
        'ԝ' => 'w',
        'А' => 'A',
        'В' => 'B',
        'Е' => 'E',
        'К' => 'K',
        'М' => 'M',
        'Н' => 'H',
        'О' => 'O',
        'Р' => 'P',
        'С' => 'C',
        'Т' => 'T',
        'Х' => 'X',
        'І' => 'I',
        'Ј' => 'J',
        'Ѕ' => 'S',
        'У' => 'Y',
        'α' => 'a',
        'ι' => 'i',
        'κ' => 'k',
        'ν' => 'v',
        'ο' => 'o',
        'ρ' => 'p',
        'υ' => 'u',
        'χ' => 'x',
        'Α' => 'A',
        'Β' => 'B',
        'Ε' => 'E',
        'Ζ' => 'Z',
        'Η' => 'H',
        'Ι' => 'I',
        'Κ' => 'K',
        'Μ' => 'M',
        'Ν' => 'N',
        'Ο' => 'O',
        'Ρ' => 'P',
        'Τ' => 'T',
        'Υ' => 'Y',
        'Χ' => 'X',
        'ı' => 'i',
        'l' => 'i',
        '0' => 'o',
        '1' => 'i',
        c => c,
    }
}

/// `_skeleton`.
pub fn skeleton(value: &str) -> String {
    let bare: String = value
        .nfkd()
        .filter(|c| canonical_combining_class(*c) == 0)
        .map(confusable)
        .collect();
    casefold(&bare).chars().filter(|c| alnum(*c)).collect()
}

/// `_edits`: optimal string alignment distance.
pub fn edits(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut previous: Option<Vec<usize>> = None;
    let mut row: Vec<usize> = (0..=b.len()).collect();
    for i in 1..=a.len() {
        let mut current = vec![0; b.len() + 1];
        current[0] = i;
        for j in 1..=b.len() {
            current[j] = (row[j] + 1)
                .min(current[j - 1] + 1)
                .min(row[j - 1] + usize::from(a[i - 1] != b[j - 1]));
            if let Some(prev) = &previous
                && j > 1
                && a[i - 1] == b[j - 2]
                && a[i - 2] == b[j - 1]
            {
                current[j] = current[j].min(prev[j - 2] + 1);
            }
        }
        previous = Some(row);
        row = current;
    }
    row[b.len()]
}

/// `NAME_RULES` in their dict order: (word, contained, edits).
const NAME_RULES: [(&str, bool, usize); 4] = [
    ("maintainer", true, 1),
    ("daemon", false, 1),
    ("passkey", false, 0),
    ("verified", false, 0),
];

/// `reserved_name`.
pub fn reserved_name(value: &str) -> bool {
    let sk = skeleton(value);
    let len = py_len(&sk);
    for (word, contained, allowance) in NAME_RULES {
        if contained && sk.contains(word) {
            return true;
        }
        if len.abs_diff(word.len()) <= allowance && edits(&sk, word) <= allowance {
            return true;
        }
    }
    false
}

/// `PREVIEW_EXCERPT`.
pub const PREVIEW_EXCERPT: usize = 100;

/// `_excerpt`: category-C characters become spaces, whitespace runs
/// collapse, then a hard cut at PREVIEW_EXCERPT characters plus "...".
pub fn excerpt(text: Option<&str>) -> String {
    let Some(text) = text else {
        return String::new();
    };
    let spaced: String = text
        .chars()
        .map(|c| if category_c(c) { ' ' } else { c })
        .collect();
    let cleaned = collapse_spaces(&spaced);
    if py_len(&cleaned) <= PREVIEW_EXCERPT {
        cleaned
    } else {
        let cut: String = cleaned.chars().take(PREVIEW_EXCERPT).collect();
        format!("{cut}...")
    }
}

/// `_board_name`.
pub fn board_name(agent_id: &str, label: Option<&str>, name: Option<&str>) -> String {
    let short: String = agent_id.chars().take(8).collect();
    match name {
        Some(n) if !n.is_empty() => n.to_string(),
        _ => match label {
            Some(l) if !l.is_empty() => format!("{l} {short}"),
            _ => short,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserved_names_follow_python() {
        // storage/coordination.py's own review examples (2026-10-04).
        for label in [
            "maintainer",
            "Mаintainer",
            "maintainer-bot",
            "Maintainer (passkey)",
            "maintainer1",
            "maintaner",
            "daemon",
            "daemn",
            "Passkey",
            "VERIFIED",
            "m a i n t a i n e r",
        ] {
            assert!(reserved_name(label), "{label}");
        }
        for label in [
            "daemon watcher",
            "passkeys",
            "verifier",
            "claude-code",
            "codex",
            "",
        ] {
            assert!(!reserved_name(label), "{label}");
        }
    }

    #[test]
    fn edits_is_osa() {
        assert_eq!(edits("abcd", "abdc"), 1);
        assert_eq!(edits("ca", "abc"), 3);
        assert_eq!(edits("", "abc"), 3);
    }

    #[test]
    fn excerpt_cuts_and_cleans() {
        assert_eq!(excerpt(Some("a\u{202e}b\n\tc")), "a b c");
        let long = "x".repeat(120);
        assert_eq!(excerpt(Some(&long)), format!("{}...", "x".repeat(100)));
        assert_eq!(excerpt(None), "");
    }

    #[test]
    fn board_names() {
        let id = "0123456789abcdef0123456789abcdef";
        assert_eq!(board_name(id, Some("codex"), Some("")), "codex 01234567");
        assert_eq!(board_name(id, Some(""), None), "01234567");
        assert_eq!(board_name(id, Some("codex"), Some("Mine")), "Mine");
    }
}
