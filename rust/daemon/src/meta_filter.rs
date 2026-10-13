//! Meta-statement filter (`memory/meta_filter.py`), spec W1.4.
//!
//! The patterns are Python's, verbatim, case-insensitive. They need no
//! lookaround, so they run on the linear-time `regex` crate: the stored text
//! is unbounded, unlike the 400-code-point label heuristics. The text is
//! remapped first so `\b` and `re.I` agree with Python
//! ([`crate::labels::py_regex_text`]).
#![allow(dead_code)]

use std::sync::OnceLock;

use regex::{RegexSet, RegexSetBuilder};

use crate::labels::{py_regex_text, py_strip};

// meta_filter.py:17-40.
const META_PATTERNS: [&str; 16] = [
    r"\bI don'?t have\b.*\b(?:saved|stored|recorded|memory|memories|material|information)\b",
    r"\bnot (?:stored|saved|recorded|in my (?:memory|knowledge))\b",
    r"\bno (?:material|information|data|records?) (?:saved|stored|about)\b",
    r"\bI don'?t (?:recall|remember)\b.*\b(?:any|specific)\b",
    r"\bnot in my (?:memory|knowledge|records?)\b",
    r"\bI (?:have|had) no (?:memory|memories|record|records?) (?:of|about|regarding)\b",
    r"\bmy (?:memory|memories|neural memory|memory banks?) (?:don'?t|doesn'?t|do not)\b",
    r"\bnothing (?:stored|saved|recorded) (?:about|regarding|on)\b",
    r"\bno relevant memories?\b",
    r"\bI (?:can'?t|cannot) find (?:any|that) in my memory\b",
    r"\bmy (?:memory|knowledge) (?:doesn'?t|does not) (?:contain|include|have)\b",
    r"\baccording to my (?:memory|memories|neural memory)\b",
    r"\bbased on (?:my|the) (?:memory|memories|recalled|retrieved)\b",
    r"\bfrom (?:my|the) (?:memory banks?|neural memory|retrieved memories)\b",
    r"\bmy TITANS (?:memory|system)\b",
    r"\bmy (?:instant|short-term|long-term|reference) (?:bank|memory)\b",
];

fn patterns() -> &'static RegexSet {
    static SET: OnceLock<RegexSet> = OnceLock::new();
    SET.get_or_init(|| {
        RegexSetBuilder::new(META_PATTERNS)
            .case_insensitive(true)
            .build()
            .expect("static meta patterns")
    })
}

/// `is_meta_statement` (meta_filter.py:46-72): never for role `"user"`;
/// always for empty stripped text; else any pattern searches the stripped
/// text.
pub fn is_meta_statement(text: &str, role: &str) -> bool {
    if role == "user" {
        return false;
    }
    let stripped = py_strip(text);
    if stripped.is_empty() {
        return true;
    }
    patterns().is_match(&py_regex_text(stripped))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::labels::tests::check_goldens;
    use serde_json::json;

    #[test]
    fn meta_filter_matches_python_goldens() {
        let counts = check_goldens(|name, args| {
            (name == "is_meta_statement").then(|| {
                let text = args[0].as_str().expect("text");
                let role = args[1].as_str().expect("role");
                json!(is_meta_statement(text, role))
            })
        });
        assert!(
            counts.get("is_meta_statement").copied().unwrap_or(0) >= 400,
            "{counts:?}"
        );
    }
}
