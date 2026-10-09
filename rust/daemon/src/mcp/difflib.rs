//! `difflib.get_close_matches` for the unknown-parameter hint
//! (`_StringSafeMetadata._unknown_message`): SequenceMatcher's ratio over
//! characters, cutoff 0.6, best match by `(ratio, candidate)`.

/// `SequenceMatcher(None, a, b).ratio()`. Parameter names are far below the
/// 200-element autojunk threshold, so no element is junk.
pub fn ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }
    2.0 * matched(&a, &b, 0, a.len(), 0, b.len()) as f64 / total as f64
}

/// Sum of `get_matching_blocks` sizes: the longest match, then recurse on
/// both sides of it.
fn matched(a: &[char], b: &[char], alo: usize, ahi: usize, blo: usize, bhi: usize) -> usize {
    let (i, j, k) = longest(a, b, alo, ahi, blo, bhi);
    if k == 0 {
        return 0;
    }
    let mut n = k;
    if alo < i && blo < j {
        n += matched(a, b, alo, i, blo, j);
    }
    if i + k < ahi && j + k < bhi {
        n += matched(a, b, i + k, ahi, j + k, bhi);
    }
    n
}

/// `find_longest_match` without junk: the earliest longest block in `a`,
/// and for that, the earliest in `b`.
fn longest(a: &[char], b: &[char], alo: usize, ahi: usize, blo: usize, bhi: usize) -> (usize, usize, usize) {
    let (mut best_i, mut best_j, mut best_k) = (alo, blo, 0);
    // j2len[j] = length of the match ending at a[i-1], b[j].
    let mut j2len = vec![0usize; b.len() + 1];
    for i in alo..ahi {
        let mut new = vec![0usize; b.len() + 1];
        for j in blo..bhi {
            if a[i] == b[j] {
                let k = if j > 0 { j2len[j - 1] } else { 0 } + 1;
                new[j] = k;
                if k > best_k {
                    best_i = i + 1 - k;
                    best_j = j + 1 - k;
                    best_k = k;
                }
            }
        }
        j2len = new;
    }
    (best_i, best_j, best_k)
}

/// `get_close_matches(word, possibilities, n=1)`: the best candidate scoring
/// at least 0.6, ties going to the larger string (heapq.nlargest on tuples).
pub fn close_match<'a>(word: &str, possibilities: &[&'a str]) -> Option<&'a str> {
    possibilities
        .iter()
        // get_close_matches sets seq2 = word, seq1 = candidate.
        .map(|p| (ratio(p, word), *p))
        .filter(|(r, _)| *r >= 0.6)
        .max_by(|x, y| x.0.partial_cmp(&y.0).unwrap().then(x.1.cmp(y.1)))
        .map(|(_, p)| p)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_cpython() {
        // Values from CPython 3.11 difflib.
        assert!((ratio("actoin", "action") - 0.8333333333333334).abs() < 1e-12);
        assert!((ratio("abcd", "bcde") - 0.75).abs() < 1e-12);
        assert_eq!(close_match("actoin", &["action"]), Some("action"));
        assert_eq!(close_match("qry", &["query", "top_k"]), Some("query"));
        assert_eq!(close_match("zzz", &["query", "top_k"]), None);
        assert_eq!(close_match("ab", &["abc", "abd"]), Some("abd"));
        // Not symmetric (review finding): CPython scores (candidate, word).
        assert_eq!(close_match("iain", &["action"]), Some("action"));
        assert_eq!(close_match("cint", &["action"]), None);
    }
}
