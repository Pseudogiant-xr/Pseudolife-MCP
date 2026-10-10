//! GET /api/search ranking over the resident bank (spec S1-S6).

#[cfg(test)]
use crate::bank::DIM;
use crate::bank::{Bank, Entry};
use serde_json::{Map, Value, json};
use std::collections::HashSet;

pub const ROUTE_TOP_K: i64 = 12; // web/routes.py:358
const ASSISTANT_MULT: f64 = 0.85; // cms.py:813
const SUPERSEDED_MULT: f64 = 0.55; // cms.py:825

/// `memory.bm25` (`BM25Config`, utils/config.py:188-204).
#[derive(Clone, Copy)]
pub struct Bm25Knobs {
    pub k1: f64,
    pub b: f64,
    pub weight: f64,
    pub top_n: usize,
    pub min_norm: f64,
}

pub struct Params {
    pub query: String,
    pub k: usize,
    pub sources: Option<HashSet<String>>,
    pub tags: Option<HashSet<String>>,
    pub min_score: Option<f64>,
    /// `memory.search.min_score`: the floor when no explicit one is given.
    pub default_floor: f64,
    /// BM25 knobs when the `bm25` tribool resolves on (config default on).
    pub bm25: Option<Bm25Knobs>,
    /// `memory.hide_superseded`.
    pub hide_superseded: bool,
    /// Bands to search (validated by the caller); None searches all.
    pub bands: Option<HashSet<String>>,
}

/// One served hit: entry index and its ranking score.
pub struct Hit {
    pub idx: usize,
    pub score: f64,
}

/// Superseded digests never surface (cms.py:899-904, 1913).
fn not_superseded_digest(e: &Entry) -> bool {
    !(e.source == "digest" && e.superseded_at.is_some())
}

pub fn rank(bank: &Bank, q: &[f32], p: &Params) -> Vec<Hit> {
    let filtered = |e: &Entry| {
        not_superseded_digest(e)
            && p.bands.as_ref().is_none_or(|b| b.contains(&e.band))
            && p.sources.as_ref().is_none_or(|s| s.contains(&e.source))
            && p.tags
                .as_ref()
                .is_none_or(|t| e.tags.iter().any(|x| t.contains(x)))
    };
    // `_dense_eligible` adds hide_superseded; the slot pool does not.
    let eligible = |e: &Entry| filtered(e) && !(p.hide_superseded && e.superseded_at.is_some());
    let filter_active =
        p.hide_superseded || p.sources.is_some() || p.tags.is_some() || p.bands.is_some();
    let mut cand: Vec<(usize, f32)> = bank
        .matrix
        .chunks_exact(bank.dim)
        .enumerate()
        .filter(|(i, _)| !filter_active || eligible(&bank.entries[*i]))
        .map(|(i, row)| (i, row.iter().zip(q).map(|(a, b)| a * b).sum::<f32>()))
        .collect();
    // top-k by cosine; ties keep resident (id) order.
    cand.sort_by(|a, b| b.1.total_cmp(&a.1));
    cand.truncate(p.k);

    let floor = p.min_score.unwrap_or(p.default_floor);
    let mut seen: HashSet<&str> = HashSet::new();
    let mut hits = Vec::new();
    for (idx, cos) in cand {
        let e = &bank.entries[idx];
        if seen.contains(e.text.as_str()) || !eligible(e) {
            continue;
        }
        let relevance = cos as f64; // recency boost is 0 on the flat preset
        let src = if e.source == "assistant" {
            ASSISTANT_MULT
        } else {
            1.0
        };
        let sup = if e.superseded_at.is_some() {
            SUPERSEDED_MULT
        } else {
            1.0
        };
        if relevance >= floor {
            seen.insert(&e.text);
            hits.push(Hit {
                idx,
                score: relevance * src * sup,
            });
        }
    }
    // Slot pool (cms.py:1135, 1851): entries whose slot (entity, value)
    // tokens overlap the query's content tokens.
    let qtok = content_tokens(&p.query);
    if !qtok.is_empty() {
        let mut slot_hits: Vec<Hit> = Vec::new();
        for (idx, e) in bank.entries.iter().enumerate() {
            if e.slots.is_empty() || seen.contains(e.text.as_str()) || !filtered(e) {
                continue;
            }
            let stok = slot_tokens(e);
            let overlap = qtok.intersection(&stok).count();
            if overlap == 0 {
                continue;
            }
            let conf = overlap as f64 / stok.len().max(1) as f64;
            let mut score = (0.55 + 0.35 * conf).min(0.95);
            if e.superseded_at.is_some() {
                score *= SUPERSEDED_MULT;
            }
            slot_hits.push(Hit { idx, score });
        }
        slot_hits.sort_by(|a, b| b.score.total_cmp(&a.score));
        slot_hits.truncate(p.k);
        for h in slot_hits {
            if p.min_score.is_some_and(|m| h.score < m) {
                continue;
            }
            seen.insert(&bank.entries[h.idx].text);
            hits.push(h);
        }
    }
    if let Some(knobs) = p.bm25
        && !p.query.is_empty()
    {
        bm25_fuse(bank, p, knobs, &eligible, &mut seen, &mut hits);
    }
    hits.sort_by(|a, b| b.score.total_cmp(&a.score)); // stable
    hits.truncate(p.k);
    hits
}

/// `memory/bm25.py:tokenize`: identifier-aware, lowercased, tiny stop list.
pub fn bm25_tokens(text: &str) -> Vec<String> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| {
        regex::Regex::new(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*|\d+(?:\.\d+)*").unwrap()
    });
    re.find_iter(text)
        .map(|m| m.as_str().to_ascii_lowercase())
        .filter(|t| !matches!(t.as_str(), "a" | "an" | "the" | "is" | "are"))
        .collect()
}

/// Weighted-sum BM25 fusion (cms.py:1204-1260): boost dense/slot hits whose
/// text BM25 also found, then inject BM25-only hits at `weight * norm`.
fn bm25_fuse<'a>(
    bank: &'a Bank,
    p: &Params,
    knobs: Bm25Knobs,
    eligible: &dyn Fn(&Entry) -> bool,
    seen: &mut HashSet<&'a str>,
    hits: &mut Vec<Hit>,
) {
    let docs: Vec<(usize, Vec<String>)> = bank
        .entries
        .iter()
        .enumerate()
        .filter(|(_, e)| eligible(e))
        .map(|(i, e)| (i, bm25_tokens(&e.text)))
        .collect();
    if docs.is_empty() {
        return;
    }
    let n = docs.len() as f64;
    let avg = docs.iter().map(|(_, t)| t.len()).sum::<usize>() as f64 / n;
    let mut df: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
    for (_, toks) in &docs {
        let uniq: HashSet<&str> = toks.iter().map(String::as_str).collect();
        for t in uniq {
            *df.entry(t).or_default() += 1;
        }
    }
    let q = bm25_tokens(&p.query);
    if q.is_empty() {
        return;
    }
    let mut scored: Vec<(usize, f64)> = Vec::new();
    for (idx, toks) in &docs {
        if toks.is_empty() {
            continue;
        }
        let mut tf: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
        for t in toks {
            *tf.entry(t.as_str()).or_default() += 1;
        }
        let norm = 1.0 - knobs.b + knobs.b * (toks.len() as f64 / avg);
        let mut s = 0.0;
        for qt in &q {
            let f = *tf.get(qt.as_str()).unwrap_or(&0) as f64;
            let d = *df.get(qt.as_str()).unwrap_or(&0) as f64;
            if f == 0.0 || d == 0.0 {
                continue;
            }
            let idf = (1.0 + (n - d + 0.5) / (d + 0.5)).ln();
            if idf <= 0.0 {
                continue;
            }
            s += idf * (f * (knobs.k1 + 1.0)) / (f + knobs.k1 * norm);
        }
        if s > 0.0 {
            scored.push((*idx, s));
        }
    }
    scored.sort_by(|a, b| b.1.total_cmp(&a.1)); // stable
    scored.truncate(knobs.top_n);
    // normalize_scores: min-max; one hit or a flat set is 1.0.
    let (mx, mn) = scored
        .iter()
        .fold((f64::MIN, f64::MAX), |(mx, mn), (_, s)| {
            (mx.max(*s), mn.min(*s))
        });
    let normed: Vec<(usize, f64)> = scored
        .iter()
        .map(|&(i, s)| {
            (
                i,
                if scored.len() == 1 || mx - mn <= 0.0 {
                    1.0
                } else {
                    (s - mn) / (mx - mn)
                },
            )
        })
        .collect();
    // Text-keyed lookup; a later duplicate text overwrites an earlier one, as a dict does.
    let mut lookup: std::collections::HashMap<&str, f64> = std::collections::HashMap::new();
    for &(i, s) in &normed {
        if s >= knobs.min_norm {
            lookup.insert(bank.entries[i].text.as_str(), s);
        }
    }
    for h in hits.iter_mut() {
        let boost = *lookup
            .get(bank.entries[h.idx].text.as_str())
            .unwrap_or(&0.0);
        if boost > 0.0 {
            h.score += knobs.weight * boost;
        }
    }
    for (i, s) in normed {
        let text = bank.entries[i].text.as_str();
        if seen.contains(text) || s < knobs.min_norm {
            continue;
        }
        let injected = knobs.weight * s;
        if p.min_score.is_some_and(|m| injected < m) {
            continue;
        }
        seen.insert(text);
        hits.push(Hit {
            idx: i,
            score: injected,
        });
    }
}

const STOP_WORDS: &[&str] = &[
    "the", "and", "you", "your", "for", "have", "had", "has", "with", "from", "this", "that",
    "what", "where", "when", "who", "why", "how", "are", "was", "were", "been", "being", "into",
    "onto", "out", "did", "does", "doing", "say", "said", "can", "will", "would", "should",
    "could", "may", "might", "any", "some", "all", "not", "yes", "tell", "tells", "told",
];

/// `_content_tokens` (cms.py:179): runs of `[a-z']{3,}` in the lowercased text, minus stop words.
pub fn content_tokens(text: &str) -> HashSet<String> {
    let lower = text.to_lowercase();
    lower
        .split(|c: char| !(c.is_ascii_lowercase() || c == '\''))
        .filter(|t| t.len() >= 3 && !STOP_WORDS.contains(t))
        .map(String::from)
        .collect()
}

fn slot_text(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => "None".into(),
        Some(other) => crate::pyjson::py_str(other),
    }
}

/// `_entry_slot_tokens` (cms.py:187): content tokens of each slot's entity and value.
fn slot_tokens(e: &Entry) -> HashSet<String> {
    let mut out = HashSet::new();
    for s in &e.slots {
        out.extend(content_tokens(&format!(
            "{} {}",
            slot_text(s.get(0)),
            slot_text(s.get(2))
        )));
    }
    out
}

/// Python `round(x, 4)`: correctly rounded from the exact binary value, ties to even.
fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

pub fn entry_json(bank: &Bank, e: &Entry, score: f64) -> Value {
    let mut m = Map::new();
    m.insert("id".into(), json!(e.id));
    m.insert("text".into(), json!(e.text));
    m.insert("source".into(), json!(e.source));
    m.insert("bank".into(), json!(e.band));
    m.insert("timestamp".into(), json!(e.ts));
    m.insert("access_count".into(), json!(e.access_count));
    m.insert("surprise_score".into(), json!(round4(e.surprise as f64)));
    m.insert("superseded".into(), json!(e.superseded_at.is_some()));
    m.insert("superseded_at".into(), json!(e.superseded_at));
    m.insert("superseded_by_text".into(), json!(e.superseded_by_text));
    m.insert("episode_id".into(), json!(e.episode_id));
    m.insert("episode_title".into(), json!(e.episode_title));
    m.insert("tags".into(), json!(e.tags));
    if let Some(a) = e.authority.as_deref().filter(|s| !s.is_empty()) {
        m.insert("authority".into(), json!(a));
    }
    if let Some(d) = e.distortion_tolerance.as_deref().filter(|s| !s.is_empty()) {
        m.insert("distortion_tolerance".into(), json!(d));
    }
    if !e.slots.is_empty() {
        let slots: Vec<Value> = e
            .slots
            .iter()
            .map(|s| {
                let f = |i: usize| s.get(i).cloned().unwrap_or(Value::Null);
                json!({"entity": f(0), "attribute": f(1), "value": f(2), "polarity": f(3)})
            })
            .collect();
        m.insert("slots".into(), Value::Array(slots));
    }
    m.insert("score".into(), json!(round4(score)));
    if e.superseded_at.is_some() {
        // service.py:_annotate_supersession: resolve the successor by exact text.
        let mut found: Vec<&Entry> = match &e.superseded_by_text {
            Some(t) => bank
                .entries
                .iter()
                .filter(|r| &r.text == t && r.id != e.id)
                .collect(),
            None => Vec::new(),
        };
        if found.len() > 1 {
            found.retain(|r| r.superseded_at.is_none());
        }
        let one = if found.len() == 1 {
            Some(found[0])
        } else {
            None
        };
        m.insert("superseded_by_id".into(), json!(one.map(|r| r.id)));
        m.insert(
            "supersession_verified".into(),
            json!(one.is_some_and(|r| r.source == "correction" || r.source == "consolidation")),
        );
        m.insert(
            "superseded_by_current".into(),
            json!(one.is_some_and(|r| r.superseded_at.is_none())),
        );
    }
    Value::Object(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULT_BM25: Bm25Knobs = Bm25Knobs {
        k1: 1.5,
        b: 0.75,
        weight: 0.3,
        top_n: 20,
        min_norm: 0.1,
    };

    fn entry(id: i64, text: &str, source: &str, superseded: bool) -> Entry {
        Entry {
            id,
            embedding: vec![],
            band: "flat".into(),
            stored_band: "flat".into(),
            text: text.into(),
            surprise: 0.5,
            surprise_value: 0.5,
            ts: 0.0,
            access_count: 0,
            reinforcements: 0,
            last_logical_turn: None,
            dream_state: None,
            source: source.into(),
            superseded_at: superseded.then_some(1.0),
            superseded_by_text: None,
            episode_id: None,
            episode_title: None,
            tags: vec![],
            slots: vec![],
            authority: None,
            distortion_tolerance: None,
        }
    }

    fn unit(cos: f32) -> Vec<f32> {
        let mut v = vec![0.0; DIM];
        v[0] = cos;
        v[1] = (1.0 - cos * cos).sqrt();
        v
    }

    #[test]
    fn dense_matrix_rows_follow_the_loaded_embedding_dimension() {
        for dim in [384, 1024] {
            let mut matrix = vec![0.0; 2 * dim];
            matrix[dim] = 1.0;
            let bank = Bank {
                dim,
                matrix,
                entries: vec![
                    entry(1, "first", "x", false),
                    entry(2, "second", "x", false),
                ],
            };
            let mut query = vec![0.0; dim];
            query[0] = 1.0;
            let p = Params {
                query: String::new(),
                k: 2,
                sources: None,
                tags: None,
                min_score: None,
                default_floor: 0.25,
                bm25: None,
                hide_superseded: false,
                bands: None,
            };
            assert_eq!(
                rank(&bank, &query, &p)
                    .iter()
                    .map(|h| bank.entries[h.idx].id)
                    .collect::<Vec<_>>(),
                [2]
            );
        }
    }

    #[test]
    fn floor_dedup_and_multipliers() {
        let rows = [
            (1, "a", "x", false, 0.9),
            (2, "a", "x", false, 0.8),
            (3, "b", "assistant", false, 0.9),
            (4, "c", "x", true, 0.95),
            (5, "d", "x", false, 0.2),
            (6, "e", "digest", true, 0.99),
        ];
        let mut matrix = Vec::new();
        let mut entries = Vec::new();
        for (id, t, s, sup, cos) in rows {
            entries.push(entry(id, t, s, sup));
            matrix.extend(unit(cos));
        }
        let bank = Bank {
            dim: DIM,
            entries,
            matrix,
        };
        let p = Params {
            query: String::new(),
            k: 6,
            sources: None,
            tags: None,
            min_score: None,
            bm25: None,
            default_floor: 0.25,
            hide_superseded: false,
            bands: None,
        };
        let hits = rank(&bank, &unit(1.0), &p);
        let ids: Vec<i64> = hits.iter().map(|h| bank.entries[h.idx].id).collect();
        // 6 (superseded digest) never surfaces; 2 duplicates 1; 5 is under the floor;
        // 3 ranks 0.765, 4 ranks 0.5225.
        assert_eq!(ids, vec![1, 3, 4]);
        let p = Params {
            query: String::new(),
            k: 6,
            sources: Some(["assistant".to_string()].into()),
            tags: None,
            min_score: None,
            bm25: None,
            default_floor: 0.25,
            hide_superseded: false,
            bands: None,
        };
        assert_eq!(rank(&bank, &unit(1.0), &p).len(), 1);
    }

    #[test]
    fn slot_pool_injects_by_token_overlap() {
        let mut e = entry(7, "slotted", "x", false);
        e.slots = vec![serde_json::json!([
            "new removed",
            "status",
            "membership analogue",
            "+"
        ])];
        let bank = Bank {
            dim: DIM,
            entries: vec![e],
            matrix: unit(0.0),
        };
        assert_eq!(
            content_tokens("How do I deploy a NEW version? it's"),
            ["deploy", "new", "version", "it's"]
                .map(String::from)
                .into()
        );
        let p = Params {
            query: "deploy a new version".into(),
            k: 8,
            sources: None,
            tags: None,
            min_score: None,
            bm25: None,
            default_floor: 0.25,
            hide_superseded: false,
            bands: None,
        };
        let hits = rank(&bank, &unit(1.0), &p);
        // slot tokens {new, removed, membership, analogue}: overlap 1/4 -> 0.55 + 0.35 * 0.25
        assert_eq!(hits.len(), 1);
        assert!((hits[0].score - 0.6375).abs() < 1e-9);
        let p = Params {
            min_score: Some(0.7),
            ..p
        };
        assert!(rank(&bank, &unit(1.0), &p).is_empty());
    }

    #[test]
    fn round4_matches_python() {
        // Values checked against CPython 3.11 round(x, 4): an exact tie goes to even,
        // everything else follows the exact binary value.
        assert_eq!(round4(0.03125), 0.0312);
        assert_eq!(round4(0.00005), 0.0001);
        assert_eq!(round4(1.00005), 1.0001);
        assert_eq!(round4(0.12345), 0.1235);
    }

    #[test]
    fn bm25_tokens_match_python() {
        // python: tokenize("The process_chunk_v2 is v1.2.3 and Don't 3.14x")
        assert_eq!(
            bm25_tokens("The process_chunk_v2 is v1.2.3 and Don't 3.14x"),
            ["process_chunk_v2", "v1.2.3", "and", "don", "t", "3.14", "x"].map(String::from)
        );
    }

    #[test]
    fn bm25_injects_lexical_only_hit_and_boosts() {
        let entries = vec![
            entry(1, "alpha beta", "x", false),
            entry(2, "gamma zeta_token delta", "x", false),
            entry(3, "unrelated words here", "x", false),
        ];
        let mut matrix = unit(0.9);
        matrix.extend(unit(0.0));
        matrix.extend(unit(0.0));
        let bank = Bank {
            dim: DIM,
            entries,
            matrix,
        };
        let p = Params {
            query: "zeta_token".into(),
            k: 8,
            sources: None,
            tags: None,
            min_score: None,
            bm25: Some(DEFAULT_BM25),
            default_floor: 0.25,
            hide_superseded: false,
            bands: None,
        };
        let hits = rank(&bank, &unit(1.0), &p);
        let got: Vec<(i64, f64)> = hits
            .iter()
            .map(|h| (bank.entries[h.idx].id, h.score))
            .collect();
        // entry 1 dense 0.9 (no lexical match); entry 2 injected at 0.3 * 1.0
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].0, 1);
        assert_eq!(got[1].0, 2);
        assert!((got[1].1 - 0.3).abs() < 1e-12);
    }
}
