//! GET /api/search ranking over the resident bank (spec S1-S6).

use crate::bank::{Bank, Entry, DIM};
use serde_json::{json, Map, Value};
use std::collections::HashSet;

pub const DEFAULT_TOP_K: usize = 8; // MemoryConfig.top_k under the MCP overlay
pub const ROUTE_TOP_K: i64 = 12; // web/routes.py:358
pub const MIN_SCORE: f64 = 0.25; // memory.search.min_score
const ASSISTANT_MULT: f64 = 0.85; // cms.py:813
const SUPERSEDED_MULT: f64 = 0.55; // cms.py:825
pub const BANDS: [&str; 1] = ["flat"]; // the default preset

pub struct Params {
    pub query: String,
    pub k: usize,
    pub sources: Option<HashSet<String>>,
    pub tags: Option<HashSet<String>>,
    pub min_score: Option<f64>,
}

/// One served hit: entry index and its ranking score.
pub struct Hit {
    pub idx: usize,
    pub score: f64,
}

fn keep(e: &Entry) -> bool {
    // hide_superseded defaults to false; superseded digests never surface.
    !(e.source == "digest" && e.superseded_at.is_some())
}

pub fn rank(bank: &Bank, q: &[f32], p: &Params) -> Vec<Hit> {
    let eligible = |e: &Entry| {
        keep(e)
            && p.sources.as_ref().is_none_or(|s| s.contains(&e.source))
            && p.tags.as_ref().is_none_or(|t| e.tags.iter().any(|x| t.contains(x)))
    };
    let filter_active = p.sources.is_some() || p.tags.is_some();
    let mut cand: Vec<(usize, f32)> = bank
        .matrix
        .chunks_exact(DIM)
        .enumerate()
        .filter(|(i, _)| !filter_active || eligible(&bank.entries[*i]))
        .map(|(i, row)| (i, row.iter().zip(q).map(|(a, b)| a * b).sum::<f32>()))
        .collect();
    // top-k by cosine; ties keep resident (id) order.
    cand.sort_by(|a, b| b.1.total_cmp(&a.1));
    cand.truncate(p.k);

    let floor = p.min_score.unwrap_or(MIN_SCORE);
    let mut seen: HashSet<&str> = HashSet::new();
    let mut hits = Vec::new();
    for (idx, cos) in cand {
        let e = &bank.entries[idx];
        if seen.contains(e.text.as_str()) || !eligible(e) {
            continue;
        }
        let relevance = cos as f64; // recency boost is 0 on the flat preset
        let src = if e.source == "assistant" { ASSISTANT_MULT } else { 1.0 };
        let sup = if e.superseded_at.is_some() { SUPERSEDED_MULT } else { 1.0 };
        if relevance >= floor {
            seen.insert(&e.text);
            hits.push(Hit { idx, score: relevance * src * sup });
        }
    }
    // Slot pool (cms.py:1135, 1851): entries whose slot (entity, value)
    // tokens overlap the query's content tokens.
    let qtok = content_tokens(&p.query);
    if !qtok.is_empty() {
        let mut slot_hits: Vec<Hit> = Vec::new();
        for (idx, e) in bank.entries.iter().enumerate() {
            if e.slots.is_empty() || seen.contains(e.text.as_str()) || !eligible(e) {
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
    hits.sort_by(|a, b| b.score.total_cmp(&a.score)); // stable
    hits.truncate(p.k);
    hits
}

const STOP_WORDS: &[&str] = &[
    "the", "and", "you", "your", "for", "have", "had", "has", "with", "from", "this", "that", "what",
    "where", "when", "who", "why", "how", "are", "was", "were", "been", "being", "into", "onto", "out",
    "did", "does", "doing", "say", "said", "can", "will", "would", "should", "could", "may", "might",
    "any", "some", "all", "not", "yes", "tell", "tells", "told",
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
        Some(other) => other.to_string(),
    }
}

/// `_entry_slot_tokens` (cms.py:187): content tokens of each slot's entity and value.
fn slot_tokens(e: &Entry) -> HashSet<String> {
    let mut out = HashSet::new();
    for s in &e.slots {
        out.extend(content_tokens(&format!("{} {}", slot_text(s.get(0)), slot_text(s.get(2)))));
    }
    out
}

fn round4(x: f64) -> f64 {
    (x * 10_000.0).round() / 10_000.0
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
            Some(t) => bank.entries.iter().filter(|r| &r.text == t && r.id != e.id).collect(),
            None => Vec::new(),
        };
        if found.len() > 1 {
            found.retain(|r| r.superseded_at.is_none());
        }
        let one = if found.len() == 1 { Some(found[0]) } else { None };
        m.insert("superseded_by_id".into(), json!(one.map(|r| r.id)));
        m.insert(
            "supersession_verified".into(),
            json!(one.is_some_and(|r| r.source == "correction" || r.source == "consolidation")),
        );
        m.insert("superseded_by_current".into(), json!(one.is_some_and(|r| r.superseded_at.is_none())));
    }
    Value::Object(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(id: i64, text: &str, source: &str, superseded: bool) -> Entry {
        Entry {
            id,
            band: "flat".into(),
            text: text.into(),
            surprise: 0.5,
            ts: 0.0,
            access_count: 0,
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
    fn floor_dedup_and_multipliers() {
        let rows = [(1, "a", "x", false, 0.9), (2, "a", "x", false, 0.8), (3, "b", "assistant", false, 0.9),
            (4, "c", "x", true, 0.95), (5, "d", "x", false, 0.2), (6, "e", "digest", true, 0.99)];
        let mut matrix = Vec::new();
        let mut entries = Vec::new();
        for (id, t, s, sup, cos) in rows {
            entries.push(entry(id, t, s, sup));
            matrix.extend(unit(cos));
        }
        let bank = Bank { entries, matrix };
        let p = Params { query: String::new(), k: 6, sources: None, tags: None, min_score: None };
        let hits = rank(&bank, &unit(1.0), &p);
        let ids: Vec<i64> = hits.iter().map(|h| bank.entries[h.idx].id).collect();
        // 6 (superseded digest) never surfaces; 2 duplicates 1; 5 is under the floor;
        // 3 ranks 0.765, 4 ranks 0.5225.
        assert_eq!(ids, vec![1, 3, 4]);
        let p = Params { query: String::new(), k: 6, sources: Some(["assistant".to_string()].into()), tags: None, min_score: None };
        assert_eq!(rank(&bank, &unit(1.0), &p).len(), 1);
    }

    #[test]
    fn slot_pool_injects_by_token_overlap() {
        let mut e = entry(7, "slotted", "x", false);
        e.slots = vec![serde_json::json!(["new removed", "status", "membership analogue", "+"])];
        let bank = Bank { entries: vec![e], matrix: unit(0.0) };
        assert_eq!(content_tokens("How do I deploy a NEW version? it's"), ["deploy", "new", "version", "it's"].map(String::from).into());
        let p = Params { query: "deploy a new version".into(), k: 8, sources: None, tags: None, min_score: None };
        let hits = rank(&bank, &unit(1.0), &p);
        // slot tokens {new, removed, membership, analogue}: overlap 1/4 -> 0.55 + 0.35 * 0.25
        assert_eq!(hits.len(), 1);
        assert!((hits[0].score - 0.6375).abs() < 1e-9);
        let p = Params { min_score: Some(0.7), ..p };
        assert!(rank(&bank, &unit(1.0), &p).is_empty());
    }
}
