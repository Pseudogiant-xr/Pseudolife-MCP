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
            && p.tags.as_ref().is_none_or(|t| {
                e.tags
                    .iter()
                    .filter_map(Value::as_str)
                    .any(|x| t.contains(x))
            })
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
    let surprise = if crate::mutants::active("search-surprise-float32") {
        f64::from(e.surprise)
    } else {
        e.surprise_value
    };
    m.insert("surprise_score".into(), json!(round4(surprise)));
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
    fn mixed_tags_keep_json_types_and_match_string_filters() {
        let mut e = entry(1, "mixed tags", "agent", false);
        e.tags = vec![json!("valid"), json!(7), json!(true)];
        let bank = Bank {
            dim: DIM,
            entries: vec![e],
            matrix: unit(1.0),
        };
        let params = Params {
            query: String::new(),
            k: 1,
            sources: None,
            tags: Some(["valid".to_string()].into()),
            min_score: Some(0.0),
            default_floor: 0.0,
            bm25: None,
            hide_superseded: false,
            bands: None,
        };
        assert_eq!(rank(&bank, &unit(1.0), &params).len(), 1);
        assert_eq!(
            entry_json(&bank, &bank.entries[0], 1.0)["tags"],
            json!(["valid", 7, true])
        );
        let params = Params {
            tags: Some(["7".to_string(), "true".to_string()].into()),
            ..params
        };
        assert!(rank(&bank, &unit(1.0), &params).is_empty());
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

    fn mutation_params() -> Params {
        Params {
            query: String::new(),
            k: 8,
            sources: None,
            tags: None,
            min_score: None,
            default_floor: 0.25,
            bm25: None,
            hide_superseded: false,
            bands: None,
        }
    }

    fn mutation_bank(entries: Vec<Entry>, cosines: &[f32]) -> Bank {
        assert_eq!(entries.len(), cosines.len());
        Bank {
            dim: 2,
            entries,
            matrix: cosines.iter().flat_map(|&c| [c, 0.0]).collect(),
        }
    }

    #[test]
    fn filters_apply_before_dense_slot_and_lexical_caps() {
        // cms.py:920-947,998,1135-1143,1204-1221.
        for channel in 0..3 {
            for filter in 0..3 {
                let mut excluded = entry(1, "needle", "assistant", false);
                excluded.band = "deep".into();
                excluded.tags = vec![json!("bad")];
                let mut included = entry(2, "needle alpha beta", "agent", false);
                included.tags = vec![json!("good")];
                excluded.slots = vec![json!(["needle", "kind", "value", "+"])];
                included.slots = excluded.slots.clone();
                let bank = mutation_bank(
                    vec![excluded, included],
                    if channel == 0 {
                        &[1.0, 0.5]
                    } else {
                        &[0.0, 0.0]
                    },
                );
                let mut p = mutation_params();
                p.k = 1;
                if channel != 0 {
                    p.query = "needle".into();
                }
                if channel == 2 {
                    // Exercise the lexical channel without slot injection.
                    p.query = "needle".into();
                    p.bm25 = Some(DEFAULT_BM25);
                }
                match filter {
                    0 => p.bands = Some(["flat".into()].into()),
                    1 => p.sources = Some(["agent".into()].into()),
                    _ => p.tags = Some(["good".into()].into()),
                }
                let mut bank = bank;
                if channel == 2 {
                    for row in &mut bank.entries {
                        row.slots.clear();
                    }
                }
                let hits = rank(&bank, &[1.0, 0.0], &p);
                assert_eq!(
                    hits.iter()
                        .map(|h| bank.entries[h.idx].id)
                        .collect::<Vec<_>>(),
                    [2],
                    "channel {channel}, filter {filter}"
                );
            }
        }
    }

    #[test]
    fn dense_floor_is_inclusive_before_source_and_supersession_weights() {
        // cms.py:1036-1067 gates relevance before adjusted source/supersession score.
        let bank = mutation_bank(
            vec![
                entry(1, "current", "assistant", false),
                entry(2, "history", "agent", true),
            ],
            &[0.5, 0.5],
        );
        let p = Params {
            min_score: Some(0.5),
            ..mutation_params()
        };
        let hits = rank(&bank, &[1.0, 0.0], &p);
        assert_eq!(hits.iter().map(|h| h.idx).collect::<Vec<_>>(), [0, 1]);
        assert_eq!(hits[0].score, 0.425);
        assert_eq!(hits[1].score, 0.275);
        let hidden = Params {
            hide_superseded: true,
            k: 1,
            ..p
        };
        let bank = mutation_bank(bank.entries, &[0.5, 1.0]);
        assert_eq!(rank(&bank, &[1.0, 0.0], &hidden)[0].idx, 0);
        let above = Params {
            min_score: Some(0.500001),
            ..hidden
        };
        assert!(rank(&bank, &[1.0, 0.0], &above).is_empty());
    }

    #[test]
    fn slot_pool_keeps_history_and_skips_superseded_digests() {
        // cms.py:1851-1954 and 1145-1155: own scale, explicit floor only.
        let mut history = entry(1, "history", "agent", true);
        history.slots = vec![json!(["needle", "ignored", "", "+"])];
        let mut digest = entry(2, "digest", "digest", true);
        digest.slots = history.slots.clone();
        let bank = mutation_bank(vec![history, digest], &[0.0, 0.0]);
        let p = Params {
            query: "needle".into(),
            hide_superseded: true,
            default_floor: 0.99,
            ..mutation_params()
        };
        let hits = rank(&bank, &[1.0, 0.0], &p);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].idx, 0);
        assert!((hits[0].score - 0.495).abs() < 1e-12);
        let equal_floor = Params {
            query: "needle".into(),
            hide_superseded: true,
            min_score: Some(0.49500000000000005),
            ..mutation_params()
        };
        assert_eq!(rank(&bank, &[1.0, 0.0], &equal_floor).len(), 1);
        assert!(
            rank(
                &bank,
                &[1.0, 0.0],
                &Params {
                    min_score: Some(0.5),
                    ..p
                }
            )
            .is_empty()
        );
        assert_eq!(
            slot_text(Some(&json!([true, null, "needle"]))),
            "[True, None, 'needle']"
        );
        assert_eq!(slot_text(Some(&json!(false))), "False");
        assert_eq!(slot_text(Some(&json!(null))), "None");
        assert_eq!(slot_text(None), "None");
        let mut current = entry(3, "current", "agent", false);
        current.slots = vec![json!(["needle", "kind", "", "+"])];
        let dense_bank = mutation_bank(vec![current], &[0.5]);
        let dense_params = Params {
            query: "needle".into(),
            ..mutation_params()
        };
        let dense_hits = rank(&dense_bank, &[1.0, 0.0], &dense_params);
        assert_eq!(dense_hits.len(), 1);
        assert_eq!(dense_hits[0].score, 0.5);

        assert_eq!(
            content_tokens("An AB abc abcde O'NEIL your YOU 123 xyz"),
            ["abc", "abcde", "o'neil", "xyz"].map(String::from).into()
        );
    }

    #[test]
    fn bm25_normalized_scores_match_python_for_nonuniform_documents() {
        // memory/bm25.py:152-181,219-248; cms.py:1226-1233,1277-1289.
        // Python BM25Index + normalize_scores, measured 2026-10-10 on five documents.
        // Large finite k1 exposes floating-point underflow in algebraic rescaling.
        for (k1, b, middle) in [
            (1.5, 0.75, 0.5699109960117469),
            (0.0, 0.0, 1.0),
            (2.2, 0.25, 0.8644745252830662),
            (1e200, 0.75, 0.3697540973920596),
        ] {
            let rows = [
                "alpha alpha beta",
                "alpha beta beta gamma delta",
                "beta gamma",
                "unrelated",
                "the is are",
            ];
            let bank = mutation_bank(
                rows.iter()
                    .enumerate()
                    .map(|(i, t)| entry(i as i64 + 1, t, "agent", false))
                    .collect(),
                &[0.0; 5],
            );
            let p = Params {
                query: "alpha beta beta".into(),
                bm25: Some(Bm25Knobs {
                    k1,
                    b,
                    min_norm: 0.0,
                    ..DEFAULT_BM25
                }),
                ..mutation_params()
            };
            let hits = rank(&bank, &[1.0, 0.0], &p);
            assert_eq!(
                hits.iter()
                    .map(|h| bank.entries[h.idx].id)
                    .collect::<Vec<_>>(),
                [1, 2, 3]
            );
            for (hit, expected) in hits.iter().zip([0.3, 0.3 * middle, 0.0]) {
                assert!(
                    (hit.score - expected).abs() < 1e-12,
                    "{k1} {b}: {}",
                    hit.score
                );
            }
            let boosted_bank = mutation_bank(bank.entries, &[0.0, 0.5, 0.0, 0.0, 0.0]);
            let boosted = rank(&boosted_bank, &[1.0, 0.0], &p);
            assert_eq!(boosted.len(), 3);
            assert_eq!(boosted[0].idx, 1);
            assert!((boosted[0].score - (0.5 + 0.3 * middle)).abs() < 1e-12);
        }
    }

    #[test]
    fn bm25_boosts_dense_hits_and_applies_inclusive_injection_gates() {
        // cms.py:1257-1262,1277-1289: boost, then lexical-only explicit-floor gate.
        let bank = mutation_bank(
            vec![
                entry(1, "needle", "agent", false),
                entry(2, "needle", "agent", false),
            ],
            &[0.5, 0.0],
        );
        let p = Params {
            query: "needle".into(),
            bm25: Some(Bm25Knobs {
                min_norm: 1.0,
                ..DEFAULT_BM25
            }),
            ..mutation_params()
        };
        let hits = rank(&bank, &[1.0, 0.0], &p);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].idx, 0);
        assert_eq!(hits[0].score, 0.8);
        let bank = mutation_bank(vec![entry(1, "needle", "agent", false)], &[0.0]);
        let p = Params {
            min_score: Some(0.3),
            ..p
        };
        assert_eq!(rank(&bank, &[1.0, 0.0], &p)[0].score, 0.3);
        assert!(
            rank(
                &bank,
                &[1.0, 0.0],
                &Params {
                    min_score: Some(0.300001),
                    ..p
                }
            )
            .is_empty()
        );
    }

    #[test]
    fn bm25_handles_empty_pools_stop_words_and_top_n() {
        // memory/bm25.py:164-167,184-214,219-248.
        let empty = mutation_bank(vec![], &[]);
        let mut p = Params {
            query: "needle".into(),
            bm25: Some(DEFAULT_BM25),
            ..mutation_params()
        };
        assert!(rank(&empty, &[1.0, 0.0], &p).is_empty());
        let bank = mutation_bank(
            vec![
                entry(1, "needle", "agent", false),
                entry(2, "needle needle extra", "agent", false),
            ],
            &[0.0, 0.0],
        );
        p.bm25.as_mut().unwrap().top_n = 0;
        assert!(rank(&bank, &[1.0, 0.0], &p).is_empty());
        p.bm25.as_mut().unwrap().top_n = 1;
        assert_eq!(rank(&bank, &[1.0, 0.0], &p).len(), 1);
        for query in ["", "the is are", "missing"] {
            p.query = query.into();
            assert!(rank(&bank, &[1.0, 0.0], &p).is_empty(), "{query}");
        }
        let bank = mutation_bank(vec![entry(1, "the is are", "agent", false)], &[0.0]);
        p.query = "needle".into();
        assert!(rank(&bank, &[1.0, 0.0], &p).is_empty());
    }

    #[test]
    fn search_json_preserves_metadata_and_python_rounding() {
        // service.py:147-195,213-259; sync.py:82-103.
        let mut row = entry(7, "note", "agent", false);
        row.ts = 12.5;
        row.access_count = 3;
        row.surprise_value = 0.12345;
        row.episode_id = Some("episode".into());
        row.episode_title = Some("title".into());
        row.tags = vec![json!("tag"), json!(7)];
        row.slots = vec![json!(["entity", "attribute", true, "+"])];
        row.authority = Some("observed".into());
        row.distortion_tolerance = Some("constraint".into());
        let bank = mutation_bank(vec![row], &[0.0]);
        assert_eq!(
            entry_json(&bank, &bank.entries[0], 0.03125),
            json!({
                "id":7,"text":"note","source":"agent","bank":"flat","timestamp":12.5,
                "access_count":3,"surprise_score":0.1235,"superseded":false,
                "superseded_at":null,"superseded_by_text":null,"episode_id":"episode",
                "episode_title":"title","tags":["tag",7],"authority":"observed",
                "distortion_tolerance":"constraint","score":0.0312,
                "slots":[{"entity":"entity","attribute":"attribute","value":true,"polarity":"+"}]
            })
        );
        let mut bank = bank;
        bank.entries[0].authority = Some("".into());
        bank.entries[0].distortion_tolerance = Some("".into());
        bank.entries[0].slots.clear();
        let served = entry_json(&bank, &bank.entries[0], 0.0);
        for key in [
            "authority",
            "distortion_tolerance",
            "slots",
            "superseded_by_id",
        ] {
            assert!(served.get(key).is_none(), "{key}");
        }
    }

    #[test]
    fn supersession_resolves_only_an_unambiguous_other_entry() {
        // service.py:213-259: exclude self, choose sole current twin, do not follow chains.
        for (sources, retired, want_id, verified, current) in [
            (vec![], vec![], None, false, false),
            (vec!["correction"], vec![true], Some(2), true, false),
            (vec!["consolidation"], vec![false], Some(2), true, true),
            (vec!["agent"], vec![false], Some(2), false, true),
            (
                vec!["correction", "agent"],
                vec![true, false],
                Some(3),
                false,
                true,
            ),
            (
                vec!["correction", "agent"],
                vec![false, false],
                None,
                false,
                false,
            ),
            (
                vec!["correction", "agent"],
                vec![true, true],
                None,
                false,
                false,
            ),
        ] {
            let mut subject = entry(1, "replacement", "agent", true);
            subject.superseded_by_text = Some("replacement".into());
            let mut rows = vec![subject];
            for (i, (source, retired)) in sources.iter().zip(retired).enumerate() {
                rows.push(entry(i as i64 + 2, "replacement", source, retired));
            }
            let bank = mutation_bank(rows, &vec![0.0; sources.len() + 1]);
            let value = entry_json(&bank, &bank.entries[0], 0.5);
            assert_eq!(value["superseded_by_id"], json!(want_id));
            assert_eq!(value["supersession_verified"], json!(verified));
            assert_eq!(value["superseded_by_current"], json!(current));
        }
        let bank = mutation_bank(vec![entry(1, "no successor", "agent", true)], &[0.0]);
        assert_eq!(
            entry_json(&bank, &bank.entries[0], 0.0)["superseded_by_id"],
            json!(null)
        );
    }
}
