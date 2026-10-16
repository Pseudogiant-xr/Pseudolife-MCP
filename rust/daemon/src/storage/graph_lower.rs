//! Unicode14 lowercase for graph entity identity, including final sigma.
#[path = "graph_unicode14.rs"]
mod data;

fn contains(ranges: &[(u32, u32)], c: char) -> bool {
    let code = u32::from(c);
    let index = ranges.partition_point(|&(lo, _)| lo <= code);
    index > 0 && code <= ranges[index - 1].1
}

pub fn lower(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut result = String::with_capacity(text.len());
    for (index, &c) in chars.iter().enumerate() {
        if c == 'Σ' {
            let before = chars[..index]
                .iter()
                .rev()
                .find(|&&c| !contains(data::IGNORABLE, c));
            let after = chars[index + 1..]
                .iter()
                .find(|&&c| !contains(data::IGNORABLE, c));
            let final_sigma = before.is_some_and(|&c| contains(data::CASED, c))
                && !after.is_some_and(|&c| contains(data::CASED, c));
            result.push(if final_sigma { 'ς' } else { 'σ' });
        } else if let Ok(index) = data::LOWER.binary_search_by_key(&u32::from(c), |&(code, _)| code)
        {
            result.push_str(data::LOWER[index].1);
        } else {
            result.push(c);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    #[test]
    fn single_character_lowercase_matches_entire_oracle_range() {
        let mut digest = Sha256::new();
        for code in 0..0x110000_u32 {
            if let Some(c) = char::from_u32(code) {
                digest.update(code.to_le_bytes());
                digest.update(lower(&c.to_string()).as_bytes());
                digest.update([0]);
            }
        }
        assert_eq!(hex::encode(digest.finalize()), data::SINGLE_CHAR_SHA256);
    }

    #[test]
    fn sigma_uses_unicode14_context_not_casefold() {
        for (input, expected) in [
            ("ΟΣ", "ος"),
            ("Σ", "σ"),
            ("AΣ\u{301}", "aς\u{301}"),
            ("AΣ\u{301}B", "aσ\u{301}b"),
            ("1\u{345}Σ", "1\u{345}σ"),
            ("A\u{345}Σ", "a\u{345}ς"),
            ("AΣ\u{a7cb}", "aς\u{a7cb}"),
            ("İ", "i\u{307}"),
            ("ẞ", "ß"),
        ] {
            assert_eq!(lower(input), expected);
        }
    }

    #[test]
    fn sigma_context_matches_entire_oracle_range() {
        let mut digest = Sha256::new();
        for code in 0..0x110000_u32 {
            if let Some(c) = char::from_u32(code) {
                digest.update(code.to_le_bytes());
                for text in [format!("AΣ{c}A"), format!("1{c}Σ")] {
                    digest.update(lower(&text).as_bytes());
                    digest.update([0]);
                }
            }
        }
        assert_eq!(hex::encode(digest.finalize()), data::SIGMA_CONTEXT_SHA256);
    }
}
