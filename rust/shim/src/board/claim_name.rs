//! Exact repository file-claim names (`repository_claims.py`:
//! `normalize_claim_path`, `file_claim_name`), shared by the shim and the
//! daemon's `memory_agents` (compiled into both crates from this file).
//! Depends only on the sibling `unicode14` tables.

use super::unicode14;
use serde_json::json;
use sha2::{Digest, Sha256};
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;

pub fn forbidden(c: char) -> bool {
    let point = c as u32;
    let ranges = unicode14::CATEGORY_C;
    let index = ranges.partition_point(|(start, _)| *start <= point);
    index > 0 && point <= ranges[index - 1].1
}
pub fn fold(value: &str) -> String {
    // Refused inputs never acquire identities. Keep the pinned unassigned scalar table stable.
    if value.chars().any(forbidden) {
        return value.to_owned();
    }
    value
        .chars()
        .flat_map(|c| {
            match unicode14::FOLD_CORRECTIONS.binary_search_by_key(&c, |(source, _)| *source) {
                Ok(index) => unicode14::FOLD_CORRECTIONS[index]
                    .1
                    .chars()
                    .collect::<Vec<_>>(),
                Err(_) => c.to_string().case_fold().collect::<Vec<_>>(),
            }
        })
        .nfc()
        .collect()
}
pub fn normalize(path: &str, case_sensitive: bool) -> Result<String, &'static str> {
    let invalid = "invalid_claim_path";
    if path.is_empty()
        || path.chars().count() > 4096
        || path.chars().any(forbidden)
        || path.chars().any(|c| "*?[]{}<>|\":".contains(c))
    {
        return Err(invalid);
    }
    let path = path.replace('\\', "/");
    if path.starts_with('/') || path.ends_with('/') {
        return Err(invalid);
    }
    let parts = path
        .split('/')
        .filter(|part| !matches!(*part, "" | "."))
        .collect::<Vec<_>>();
    if parts.is_empty() {
        return Err(invalid);
    }
    for part in &parts {
        let folded = fold(part);
        let device = fold(part.split('.').next().unwrap_or("").trim_end_matches(' '));
        let number = device
            .strip_prefix("com")
            .or_else(|| device.strip_prefix("lpt"));
        if *part == ".."
            || part.ends_with(['.', ' '])
            || folded == ".git"
            || matches!(device.as_str(), "con" | "prn" | "aux" | "nul")
            || number.is_some_and(|number| {
                matches!(
                    number,
                    "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                )
            })
        {
            return Err(invalid);
        }
    }
    let normalized = parts.join("/").nfc().collect::<String>();
    Ok(if case_sensitive {
        normalized
    } else {
        fold(&normalized)
    })
}
pub fn claim_name(repository: &str, path: &str) -> Result<String, &'static str> {
    if repository.len() != 64
        || !repository
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err("invalid_repository_id");
    }
    if normalize(path, true)? != path {
        return Err("invalid_claim_path");
    }
    Ok(format!(
        "claim:file:{}",
        hex_hash(ascii_json(&json!([repository, path])))
    ))
}
/// `json.dumps(value, ensure_ascii=True, separators=(",", ":"))`.
fn ascii_json(value: &serde_json::Value) -> String {
    let mut out = String::new();
    for c in serde_json::to_string(value)
        .expect("JSON value serializes")
        .chars()
    {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut words = [0u16; 2];
            for word in c.encode_utf16(&mut words).iter() {
                out.push_str(&format!("\\u{word:04x}"));
            }
        }
    }
    out
}

fn hex_hash(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_match_python_file_claim_name() {
        // repository_claims.file_claim_name, CPython 3.11.
        assert_eq!(
            claim_name(&"0".repeat(64), "src/caf\u{e9}/\u{1f600}.rs").unwrap(),
            "claim:file:bb1c2c251e4bf1e55c9c36635f5e0451dc8efc9852d153389a116ba218009836"
        );
        assert_eq!(
            claim_name(&"ab".repeat(32), "docs/a.md").unwrap(),
            "claim:file:8ceb33a2af9d27a33c81c7ea2edbc62daa1757d8bcd8d5ff5ea2c275d1fcf8b6"
        );
        assert_eq!(claim_name("AB", "x"), Err("invalid_repository_id"));
        assert_eq!(
            claim_name(&"0".repeat(64), "./x"),
            Err("invalid_claim_path")
        );
    }
}
