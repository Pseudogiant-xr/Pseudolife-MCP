//! GET /api/search ranking over the resident bank (spec S1-S19):
//! `ContinuumMemorySystem.retrieve` (`memory/cms.py:740-1760`).

use crate::bank::{Bank, DIM, Entry};
use serde_json::{Map, Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::Ordering;

pub const ROUTE_TOP_K: i64 = 12; // web/routes.py:358
const ASSISTANT_MULT: f64 = 0.85; // cms.py:813
const SUPERSEDED_MULT: f64 = 0.55; // cms.py:825
const RRF_K: f64 = 60.0; // cms.py:866
const TIMELINE_TOP_N: usize = 6; // cms.py:1325
const TIMELINE_WEIGHT: f64 = 0.3; // cms.py:1326

/// `memory.bm25` (`BM25Config`, utils/config.py:188-204).
#[derive(Clone, Copy)]
pub struct Bm25Knobs {
    pub k1: f64,
    pub b: f64,
    pub weight: f64,
    pub top_n: usize,
    pub min_norm: f64,
}

/// `memory.reranker` (`RerankerConfig`, utils/config.py:208-252).
#[derive(Clone)]
pub struct RerankKnobs {
    pub top_n: i64,
    pub fusion_weight: f64,
    pub skip_margin: f64,
    pub model_name: String,
}

/// The cross-encoder as `retrieve` sees it: `None` scores mean the call
/// failed or the model is unavailable (`reranker.py:rerank` returns `[]`).
pub trait CrossEncoder {
    /// `is_available()`: false once a load has failed.
    fn available(&self) -> bool;
    fn scores(&self, query: &str, texts: &[&str]) -> Vec<f64>;
}

pub struct Params {
    pub query: String,
    pub k: usize,
    pub sources: Option<HashSet<String>>,
    pub tags: Option<HashSet<String>>,
    pub episodes: Option<HashSet<String>>,
    pub min_score: Option<f64>,
    /// `memory.search.min_score`: the floor when no explicit one is given.
    pub default_floor: f64,
    /// BM25 knobs (scorer parameters); `bm25_enabled` says whether the
    /// channel runs. The timeline channel reuses k1 and b either way.
    pub bm25: Bm25Knobs,
    pub bm25_enabled: bool,
    /// `memory.hide_superseded`.
    pub hide_superseded: bool,
    /// Band-name filter (validated by the caller); None searches all.
    pub bands: Option<HashSet<String>>,
    /// The preset's band names in depth order.
    pub band_order: Vec<String>,
    pub disable_recency_boost: bool,
    pub recency_boost_enabled: bool,
    pub recency_base_half_life_s: f64,
    pub pool_multiplier: i64,
    pub fusion_rrf: bool,
    pub fusion_name: String,
    pub timeline_enabled: bool,
    /// The rerank tribool resolved against config.
    pub rerank_enabled: bool,
    pub rerank: RerankKnobs,
    /// False for the warmup probe (S17): no access accrual.
    pub count_access: bool,
    /// Wall clock for recency (`time.time()`).
    pub now: f64,
    /// `params.filters` as the caller passed them (S8b): band and source
    /// lists in caller order, episodes sorted, tags normalised.
    pub filters_json: Value,
}

/// One served hit: entry index, score, channel marker and the fusion inputs
/// logged with the retrieval event (S8a).
pub struct Hit {
    pub idx: usize,
    pub score: f64,
    pub via: Option<&'static str>,
}

pub struct Ranked {
    pub hits: Vec<Hit>,
    /// Per served text, the components dict (cms.py `comps`).
    pub comps: HashMap<String, Map<String, Value>>,
    /// `RetrievalResult.params` (S8b), without `contiguity_neighbors`.
    pub params: Value,
}

/// Superseded digests never surface (cms.py:899-904, 1913).
fn not_superseded_digest(e: &Entry) -> bool {
    !(e.source == "digest" && e.superseded_at.is_some())
}

/// `_recency_weight` (cms.py:2878).
fn recency_weight(now: f64, ts: f64, half_life: f64) -> f64 {
    let age = (now - ts).max(0.0);
    2f64.powf(-age / half_life)
}

fn cosine(bank: &Bank, idx: usize, q: &[f32]) -> f32 {
    bank.matrix[idx * DIM..(idx + 1) * DIM]
        .iter()
        .zip(q)
        .map(|(a, b)| a * b)
        .sum::<f32>()
}

/// `torch.topk` over a candidate list: descending by score, ties keep the
/// list order (torch makes no promise there; ties are free).
fn top_k(mut cand: Vec<(usize, f32)>, k: usize) -> Vec<(usize, f32)> {
    cand.sort_by(|a, b| b.1.total_cmp(&a.1));
    cand.truncate(k);
    cand
}

pub fn rank(bank: &Bank, q: &[f32], p: &Params, ce: Option<&dyn CrossEncoder>) -> Ranked {
    let explicit_floor = p.min_score.is_some();
    let floor = p.min_score.unwrap_or(p.default_floor);
    let k = p.k;
    let pool_mult = p.pool_multiplier.max(1) as usize;
    let pool_k = k * pool_mult;
    let rerank_before_cut = pool_mult > 1 && p.rerank_enabled;
    let n = p.band_order.len();

    let keep =
        |e: &Entry| not_superseded_digest(e) && !(p.hide_superseded && e.superseded_at.is_some());
    let meta_ok = |e: &Entry| {
        p.sources.as_ref().is_none_or(|s| s.contains(&e.source))
            && p.episodes
                .as_ref()
                .is_none_or(|s| e.episode_id.as_ref().is_some_and(|x| s.contains(x)))
            && p.tags
                .as_ref()
                .is_none_or(|t| e.tags.iter().any(|x| t.contains(x)))
    };
    let dense_eligible = |e: &Entry| keep(e) && meta_ok(e);
    let dense_filter_active =
        p.hide_superseded || p.sources.is_some() || p.episodes.is_some() || p.tags.is_some();
    let band_ok = |name: &str| p.bands.as_ref().is_none_or(|b| b.contains(name));

    // Resident entries per band in hydration (id) order: the band walk.
    let mut by_band: Vec<Vec<usize>> = vec![Vec::new(); n];
    for (i, e) in bank.entries.iter().enumerate() {
        if let Some(d) = p.band_order.iter().position(|b| *b == e.band) {
            by_band[d].push(i);
        }
    }

    // (idx, score, surprise) in insertion order, as `neural`.
    let mut neural: Vec<(usize, f64)> = Vec::new();
    let mut seen: HashSet<&str> = HashSet::new();
    let mut comps: HashMap<String, Map<String, Value>> = HashMap::new();
    let mut dense_rank_src: Vec<(&str, f64)> = Vec::new();
    let mut slot_rank: Vec<&str> = Vec::new();
    let mut bm25_rank: Vec<&str> = Vec::new();
    let mut timeline_rank: Vec<&str> = Vec::new();
    let mut via_map: HashMap<&str, &'static str> = HashMap::new();
    let mut pool_size = 0usize;

    // ── Pool 1: dense, per band, recency-weighted by depth ──
    for (depth, members) in by_band.iter().enumerate() {
        let name = p.band_order[depth].as_str();
        if !band_ok(name) {
            continue;
        }
        let (boost, half_life) = if n == 1 || p.disable_recency_boost || !p.recency_boost_enabled {
            (0.0, f64::INFINITY)
        } else {
            let frac = depth as f64 / (n - 1) as f64;
            (
                0.4 * (1.0 - frac),
                p.recency_base_half_life_s * 2f64.powi(depth as i32),
            )
        };
        let cand: Vec<(usize, f32)> = members
            .iter()
            .copied()
            .filter(|&i| !dense_filter_active || dense_eligible(&bank.entries[i]))
            .map(|i| (i, cosine(bank, i, q)))
            .collect();
        let got = top_k(cand, pool_k);
        pool_size = pool_size.max(got.len());
        for (idx, cos) in got {
            let e = &bank.entries[idx];
            if seen.contains(e.text.as_str()) || !dense_eligible(e) {
                continue;
            }
            let score = cos as f64;
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
            let (recency, relevance) = if boost > 0.0 {
                let r = recency_weight(p.now, e.ts, half_life);
                (r, score * (1.0 + boost * r))
            } else {
                (0.0, score)
            };
            if relevance >= floor {
                neural.push((idx, relevance * src * sup));
                dense_rank_src.push((&e.text, relevance));
                seen.insert(&e.text);
                comps.insert(
                    e.text.clone(),
                    obj(json!({
                        "channel": "dense", "dense": score, "recency": recency,
                        "recency_boost": boost, "source_mult": src,
                        "supersession_mult": sup, "surprise": e.surprise as f64,
                        "band": name, "band_depth": depth,
                    })),
                );
            }
        }
    }

    // ── Pool 1.5: slot channel (cms.py:1133-1160, 1851-1951) ──
    let qtok = content_tokens(&p.query);
    if !p.query.is_empty() && !qtok.is_empty() {
        let mut slot_hits: Vec<(usize, f64)> = Vec::new();
        for members in &by_band {
            for &idx in members {
                let e = &bank.entries[idx];
                if e.slots.is_empty() || seen.contains(e.text.as_str()) {
                    continue;
                }
                if !not_superseded_digest(e) || !band_ok(&e.band) || !meta_ok(e) {
                    continue;
                }
                let stok = slot_tokens(e);
                if stok.is_empty() {
                    continue;
                }
                let overlap = qtok.intersection(&stok).count();
                if overlap == 0 {
                    continue;
                }
                let conf = overlap as f64 / stok.len().max(1) as f64;
                let mut score = (0.55 + 0.35 * conf).min(0.95);
                if e.superseded_at.is_some() {
                    score *= SUPERSEDED_MULT;
                }
                slot_hits.push((idx, score));
            }
        }
        slot_hits.sort_by(|a, b| b.1.total_cmp(&a.1));
        slot_hits.truncate(k);
        for (idx, score) in slot_hits {
            if explicit_floor && score < floor {
                continue;
            }
            let e = &bank.entries[idx];
            neural.push((idx, score));
            slot_rank.push(&e.text);
            seen.insert(&e.text);
            comps.insert(
                e.text.clone(),
                obj(json!({"channel": "slot", "slot": score, "surprise": 0.0, "band": e.band})),
            );
        }
    }

    // ── Pools 1.75 / 1.9: BM25 and timeline over one lexical candidate pool ──
    let timeline_fired = p.timeline_enabled && !p.query.is_empty() && has_temporal_cue(&p.query);
    let mut candidates: Vec<usize> = Vec::new();
    if !p.query.is_empty() && (p.bm25_enabled || timeline_fired) {
        for (depth, members) in by_band.iter().enumerate() {
            if !band_ok(&p.band_order[depth]) {
                continue;
            }
            candidates.extend(
                members
                    .iter()
                    .copied()
                    .filter(|&i| dense_eligible(&bank.entries[i])),
            );
        }
    }
    if p.bm25_enabled && !p.query.is_empty() && !candidates.is_empty() {
        let knobs = p.bm25;
        let norm = normalize_scores(bm25_score(
            bank,
            &candidates,
            &p.query,
            knobs.k1,
            knobs.b,
            knobs.top_n,
        ));
        let mut lookup: HashMap<&str, f64> = HashMap::new();
        for &(i, s) in &norm {
            if s >= knobs.min_norm {
                lookup.insert(bank.entries[i].text.as_str(), s);
                bm25_rank.push(bank.entries[i].text.as_str());
            }
        }
        for (idx, score) in neural.iter_mut() {
            let text = bank.entries[*idx].text.as_str();
            let boost = *lookup.get(text).unwrap_or(&0.0);
            if boost > 0.0 && !p.fusion_rrf {
                *score += knobs.weight * boost;
            }
            if let Some(c) = comps.get_mut(text) {
                c.insert("bm25".into(), json!(boost));
            }
        }
        for (i, s) in norm {
            let e = &bank.entries[i];
            if seen.contains(e.text.as_str()) || s < knobs.min_norm {
                continue;
            }
            let injected = knobs.weight * s;
            if explicit_floor && injected < floor {
                continue;
            }
            neural.push((i, injected));
            seen.insert(&e.text);
            comps.insert(
                e.text.clone(),
                obj(json!({"channel": "bm25", "bm25": s, "surprise": 0.0, "band": e.band})),
            );
        }
    }
    if timeline_fired && !candidates.is_empty() {
        let norm = normalize_scores(bm25_score(
            bank,
            &candidates,
            &p.query,
            p.bm25.k1,
            p.bm25.b,
            TIMELINE_TOP_N,
        ));
        for (i, s) in norm {
            let e = &bank.entries[i];
            if seen.contains(e.text.as_str()) || s <= 0.0 {
                continue;
            }
            let injected = TIMELINE_WEIGHT * s;
            if explicit_floor && injected < floor {
                continue;
            }
            neural.push((i, injected));
            seen.insert(&e.text);
            comps.insert(
                e.text.clone(),
                obj(json!({"channel": "timeline", "bm25": s, "surprise": 0.0, "band": e.band})),
            );
            via_map.insert(&e.text, "timeline");
            timeline_rank.push(&e.text);
        }
    }

    if p.fusion_rrf && !crate::mutants::active("search-rrf-off") {
        // Stable sort of the dense list by relevance, then 1/(60 + rank).
        let mut dense_sorted = dense_rank_src.clone();
        dense_sorted.sort_by(|a, b| b.1.total_cmp(&a.1));
        let mut fused: HashMap<&str, f64> = HashMap::new();
        let dense_texts: Vec<&str> = dense_sorted.iter().map(|(t, _)| *t).collect();
        for order in [&dense_texts, &slot_rank, &bm25_rank, &timeline_rank] {
            for (rank0, text) in order.iter().enumerate() {
                *fused.entry(text).or_insert(0.0) += 1.0 / (RRF_K + rank0 as f64 + 1.0);
            }
        }
        for (idx, score) in neural.iter_mut() {
            let e = &bank.entries[*idx];
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
            *score = fused.get(e.text.as_str()).copied().unwrap_or(0.0) * src * sup;
        }
    }

    // Stable sort: ties keep the insertion order (band walk, slot, BM25, timeline).
    neural.sort_by(|a, b| b.1.total_cmp(&a.1));
    if !rerank_before_cut {
        neural.truncate(k);
    }
    // Pool 2 (reference documents) is a declared divergence: none here.
    let mut combined = neural;

    // ── Pool 3: cross-encoder rerank (cms.py:1466-1600) ──
    let top_n = p.rerank.top_n;
    let mut rerank_log = obj(json!({
        "enabled": p.rerank_enabled, "fired": false, "skip_reason": null,
        "top_n": top_n, "candidate_count": combined.len(), "scored_candidates": 0,
        "scoring_policy": "complete_pool_or_skip",
    }));
    let ce_ready = ce.filter(|c| c.available());
    if p.rerank_enabled
        && let Some(ce) = ce_ready
        && !p.query.is_empty()
        && !combined.is_empty()
        && combined.len() as i64 <= top_n
    {
        let texts: Vec<&str> = combined
            .iter()
            .map(|(i, _)| bank.entries[*i].text.as_str())
            .collect();
        let orig: Vec<f64> = combined.iter().map(|(_, s)| *s).collect();
        rerank_log.insert("top_n".into(), json!(top_n));
        rerank_log.insert("skip_margin".into(), json!(p.rerank.skip_margin));
        rerank_log.insert("fusion_weight".into(), json!(p.rerank.fusion_weight));
        rerank_log.insert("model".into(), json!(p.rerank.model_name));
        let mut skip_for_margin = false;
        if p.rerank.skip_margin > 0.0 {
            let mut sorted = orig.clone();
            sorted.sort_by(|a, b| b.total_cmp(a));
            let margin = if sorted.len() >= 2 {
                sorted[0] - sorted[1]
            } else {
                f64::INFINITY
            };
            skip_for_margin = margin >= p.rerank.skip_margin;
            rerank_log.insert(
                "margin".into(),
                if margin.is_finite() {
                    json!(margin)
                } else {
                    Value::Null
                },
            );
        }
        let scores = if skip_for_margin {
            rerank_log.insert("skip_reason".into(), json!("unambiguous_margin"));
            Vec::new()
        } else {
            ce.scores(&p.query, &texts)
        };
        for (i, t) in texts.iter().enumerate() {
            if let Some(c) = comps.get_mut(*t) {
                c.insert("ce".into(), scores.get(i).map_or(Value::Null, |s| json!(s)));
            }
        }
        if !scores.is_empty() {
            rerank_log.insert("fired".into(), json!(true));
            rerank_log.insert("scored_candidates".into(), json!(scores.len()));
            let w = if crate::mutants::active("search-rerank-unfused") {
                1.0
            } else {
                p.rerank.fusion_weight
            };
            let mut reranked: Vec<(usize, f64)> = combined
                .iter()
                .zip(&scores)
                .map(|(&(i, o), &c)| (i, w * c + (1.0 - w) * o))
                .collect();
            reranked.sort_by(|a, b| b.1.total_cmp(&a.1));
            combined = reranked;
        } else if !skip_for_margin {
            rerank_log.insert("skip_reason".into(), json!("rerank_failed_or_unavailable"));
        }
    } else if p.rerank_enabled {
        let over_budget = ce.is_some() && !p.query.is_empty() && combined.len() as i64 > top_n;
        let reason = if over_budget {
            "candidate_budget_exceeded"
        } else {
            "unavailable"
        };
        rerank_log.insert("skip_reason".into(), json!(reason));
    }
    if rerank_before_cut {
        // No reference documents: the deferred cut keeps the first k memories.
        combined.truncate(k);
    }

    let mut bm25_params = obj(json!({"enabled": p.bm25_enabled}));
    if p.bm25_enabled {
        let b = p.bm25;
        bm25_params.insert("weight".into(), json!(b.weight));
        bm25_params.insert("min_score".into(), json!(b.min_norm));
        bm25_params.insert("k1".into(), json!(b.k1));
        bm25_params.insert("b".into(), json!(b.b));
        bm25_params.insert("top_n".into(), json!(b.top_n));
    }
    let params = json!({
        "top_k": k,
        "min_score": floor,
        "min_score_explicit": explicit_floor,
        "band_count": n,
        "recency_boost": n > 1 && !p.disable_recency_boost && p.recency_boost_enabled,
        "recency_base_half_life_s": p.recency_base_half_life_s,
        "hide_superseded": p.hide_superseded,
        "bm25": bm25_params,
        "candidate_pool": {
            "multiplier": pool_mult,
            "pool_size": pool_size,
            "fusion": p.fusion_name,
            "rerank_position": if rerank_before_cut { "before_cut" } else { "after_cut" },
        },
        "reranker": rerank_log,
        "timeline": {"enabled": p.timeline_enabled, "fired": timeline_fired},
        "filters": p.filters_json,
    });

    // Timeline presentation: the memory part in stream order (timestamp, seq).
    if timeline_fired && !crate::mutants::active("search-timeline-unsorted") {
        combined.sort_by(|a, b| {
            bank.entries[a.0]
                .ts
                .total_cmp(&bank.entries[b.0].ts)
                .then(a.0.cmp(&b.0))
        });
    }
    if p.count_access && !crate::mutants::active("search-no-access-bump") {
        for (i, _) in &combined {
            bank.entries[*i]
                .access_count
                .fetch_add(1, Ordering::Relaxed);
        }
    }
    let hits = combined
        .into_iter()
        .map(|(idx, score)| Hit {
            idx,
            score,
            via: via_map.get(bank.entries[idx].text.as_str()).copied(),
        })
        .collect();
    Ranked {
        hits,
        comps,
        params,
    }
}

fn obj(v: Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m,
        _ => Map::new(),
    }
}

/// `BM25Index(candidates).score(query, top_k)` (`memory/bm25.py:110-204`).
fn bm25_score(
    bank: &Bank,
    cand: &[usize],
    query: &str,
    k1: f64,
    b: f64,
    top_n: usize,
) -> Vec<(usize, f64)> {
    let docs: Vec<(usize, Vec<String>)> = cand
        .iter()
        .map(|&i| (i, bm25_tokens(&bank.entries[i].text)))
        .collect();
    let n = docs.len() as f64;
    if docs.is_empty() {
        return Vec::new();
    }
    let q = bm25_tokens(query);
    if q.is_empty() {
        return Vec::new();
    }
    let total: usize = docs.iter().map(|(_, t)| t.len()).sum();
    let avg = total as f64 / n;
    let mut df: HashMap<&str, usize> = HashMap::new();
    for (_, toks) in &docs {
        let uniq: HashSet<&str> = toks.iter().map(String::as_str).collect();
        for t in uniq {
            *df.entry(t).or_default() += 1;
        }
    }
    let mut scored: Vec<(usize, f64)> = Vec::new();
    for (idx, toks) in &docs {
        if toks.is_empty() {
            continue;
        }
        let mut tf: HashMap<&str, usize> = HashMap::new();
        for t in toks {
            *tf.entry(t.as_str()).or_default() += 1;
        }
        let norm = 1.0 - b
            + b * (if avg != 0.0 {
                toks.len() as f64 / avg
            } else {
                0.0
            });
        let mut s = 0.0;
        for qt in &q {
            let f = *tf.get(qt.as_str()).unwrap_or(&0) as f64;
            if f == 0.0 {
                continue;
            }
            let d = *df.get(qt.as_str()).unwrap_or(&0) as f64;
            if d == 0.0 {
                continue;
            }
            let idf = (1.0 + (n - d + 0.5) / (d + 0.5)).ln();
            if idf <= 0.0 {
                continue;
            }
            s += idf * (f * (k1 + 1.0)) / (f + k1 * norm);
        }
        if s > 0.0 {
            scored.push((*idx, s));
        }
    }
    scored.sort_by(|a, b| b.1.total_cmp(&a.1)); // stable
    scored.truncate(top_n);
    scored
}

/// `normalize_scores` (`memory/bm25.py:207-227`): min-max; one hit or a flat set is 1.0.
fn normalize_scores(scored: Vec<(usize, f64)>) -> Vec<(usize, f64)> {
    if scored.len() <= 1 {
        return scored.into_iter().map(|(i, _)| (i, 1.0)).collect();
    }
    let mx = scored.iter().map(|x| x.1).fold(f64::MIN, f64::max);
    let mn = scored.iter().map(|x| x.1).fold(f64::MAX, f64::min);
    let span = mx - mn;
    scored
        .into_iter()
        .map(|(i, s)| (i, if span <= 0.0 { 1.0 } else { (s - mn) / span }))
        .collect()
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

/// `_TEMPORAL_CUE_RE` (cms.py:69-77): word-bounded, case-insensitive; no "may".
pub fn has_temporal_cue(text: &str) -> bool {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(
            r"(?i)\b(first|last|when|earliest|latest|before|after|since|until|ago|how many times|how long|what order|in order|order in which|sequence|chronolog\w*|timeline|january|february|march|april|june|july|august|september|october|november|december)\b",
        )
        .unwrap()
    })
    .is_match(text)
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
        Some(other) => other.to_string(),
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
pub fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

/// `_entry_to_dict` (service.py:147-190).
pub fn entry_json(e: &Entry, score: f64) -> Value {
    let mut m = Map::new();
    m.insert("id".into(), json!(e.id));
    m.insert("text".into(), json!(e.text));
    m.insert("source".into(), json!(e.source));
    m.insert("bank".into(), json!(e.band));
    m.insert("timestamp".into(), json!(e.ts));
    m.insert(
        "access_count".into(),
        json!(e.access_count.load(Ordering::Relaxed)),
    );
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
    Value::Object(m)
}

/// `_annotate_supersession` (service.py:213-269): resolve each served
/// superseded entry's successor by exact text over the resident bank.
pub fn annotate_supersession_in(bank: &Bank, served: &mut [(usize, Value)]) {
    for (idx, d) in served.iter_mut() {
        let e = &bank.entries[*idx];
        if e.superseded_at.is_none() {
            continue;
        }
        let mut found: Vec<(usize, &Entry)> = match &e.superseded_by_text {
            Some(t) if !t.is_empty() => bank
                .entries
                .iter()
                .enumerate()
                .filter(|(i, r)| &r.text == t && *i != *idx)
                .collect(),
            _ => Vec::new(),
        };
        if found.len() > 1 {
            found.retain(|(_, r)| r.superseded_at.is_none());
        }
        let one = if found.len() == 1 {
            Some(found[0].1)
        } else {
            None
        };
        if let Value::Object(m) = d {
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
    }
}

/// `temporal_neighbors` (cms.py:620-664): stream-adjacent entries of the
/// hit, same episode (or same source among episode-less entries), ordered
/// by `(timestamp, seq)`; `seq` is resident order.
pub fn temporal_neighbors(
    bank: &Bank,
    idx: usize,
    n_each: usize,
    hide_superseded: bool,
) -> (Vec<usize>, Vec<usize>) {
    if n_each == 0 {
        return (Vec::new(), Vec::new());
    }
    let anchor = &bank.entries[idx];
    let mut seen: HashSet<&str> = HashSet::from([anchor.text.as_str()]);
    let mut pool: Vec<usize> = Vec::new();
    for (i, e) in bank.entries.iter().enumerate() {
        if seen.contains(e.text.as_str()) {
            continue;
        }
        if !not_superseded_digest(e) || (hide_superseded && e.superseded_at.is_some()) {
            continue;
        }
        match &anchor.episode_id {
            Some(ep) if !ep.is_empty() => {
                if e.episode_id.as_deref() != Some(ep.as_str()) {
                    continue;
                }
            }
            _ => {
                if e.episode_id.as_deref().is_some_and(|x| !x.is_empty())
                    || e.source != anchor.source
                {
                    continue;
                }
            }
        }
        pool.push(i);
        seen.insert(&e.text);
    }
    let key = |i: usize| (bank.entries[i].ts, i);
    let a = key(idx);
    let le = |x: (f64, usize)| x.0 < a.0 || (x.0 == a.0 && x.1 <= a.1);
    let mut before: Vec<usize> = pool.iter().copied().filter(|&i| le(key(i))).collect();
    let mut after: Vec<usize> = pool.iter().copied().filter(|&i| !le(key(i))).collect();
    let cmp = |x: &usize, y: &usize| key(*x).0.total_cmp(&key(*y).0).then(x.cmp(y));
    before.sort_by(cmp);
    after.sort_by(cmp);
    let before = before[before.len().saturating_sub(n_each)..].to_vec();
    after.truncate(n_each);
    (before, after)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicI64;

    fn entry(id: i64, text: &str, source: &str, superseded: bool) -> Entry {
        Entry {
            id,
            band: "flat".into(),
            stored_band: "flat".into(),
            text: text.into(),
            surprise: 0.5,
            ts: id as f64,
            access_count: AtomicI64::new(0),
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

    fn params(query: &str, k: usize) -> Params {
        Params {
            query: query.into(),
            k,
            sources: None,
            tags: None,
            episodes: None,
            min_score: None,
            default_floor: 0.25,
            bm25: Bm25Knobs {
                k1: 1.5,
                b: 0.75,
                weight: 0.3,
                top_n: 20,
                min_norm: 0.1,
            },
            bm25_enabled: false,
            hide_superseded: false,
            bands: None,
            band_order: vec!["flat".into()],
            disable_recency_boost: false,
            recency_boost_enabled: false,
            recency_base_half_life_s: 86400.0,
            pool_multiplier: 1,
            fusion_rrf: false,
            fusion_name: "weighted_sum".into(),
            timeline_enabled: false,
            rerank_enabled: false,
            rerank: RerankKnobs {
                top_n: 20,
                fusion_weight: 0.7,
                skip_margin: 0.0,
                model_name: "cross-encoder/ms-marco-MiniLM-L-6-v2".into(),
            },
            count_access: true,
            now: 1e9,
            filters_json: serde_json::json!({}),
        }
    }

    fn ids(bank: &Bank, r: &Ranked) -> Vec<i64> {
        r.hits.iter().map(|h| bank.entries[h.idx].id).collect()
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
        let bank = Bank { entries, matrix };
        let r = rank(&bank, &unit(1.0), &params("", 6), None);
        // 6 (superseded digest) never surfaces; 2 duplicates 1; 5 is under the floor;
        // 3 ranks 0.765, 4 ranks 0.5225.
        assert_eq!(ids(&bank, &r), vec![1, 3, 4]);
        // Served entries accrue one access each.
        assert_eq!(bank.entries[0].access_count.load(Ordering::Relaxed), 1);
        assert_eq!(bank.entries[1].access_count.load(Ordering::Relaxed), 0);
        let mut p = params("", 6);
        p.sources = Some(["assistant".to_string()].into());
        assert_eq!(rank(&bank, &unit(1.0), &p, None).hits.len(), 1);
        let mut p = params("", 6);
        p.count_access = false;
        rank(&bank, &unit(1.0), &p, None);
        assert_eq!(bank.entries[0].access_count.load(Ordering::Relaxed), 2);
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
            entries: vec![e],
            matrix: unit(0.0),
        };
        assert_eq!(
            content_tokens("How do I deploy a NEW version? it's"),
            ["deploy", "new", "version", "it's"]
                .map(String::from)
                .into()
        );
        let r = rank(&bank, &unit(1.0), &params("deploy a new version", 8), None);
        // slot tokens {new, removed, membership, analogue}: overlap 1/4 -> 0.55 + 0.35 * 0.25
        assert_eq!(r.hits.len(), 1);
        assert!((r.hits[0].score - 0.6375).abs() < 1e-9);
        assert_eq!(r.comps["slotted"]["channel"], "slot");
        let mut p = params("deploy a new version", 8);
        p.min_score = Some(0.7);
        assert!(rank(&bank, &unit(1.0), &p, None).hits.is_empty());
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
        let bank = Bank { entries, matrix };
        let mut p = params("zeta_token", 8);
        p.bm25_enabled = true;
        let r = rank(&bank, &unit(1.0), &p, None);
        let got: Vec<(i64, f64)> = r
            .hits
            .iter()
            .map(|h| (bank.entries[h.idx].id, h.score))
            .collect();
        // entry 1 dense 0.9 (no lexical match); entry 2 injected at 0.3 * 1.0
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].0, 1);
        assert_eq!(got[1].0, 2);
        assert!((got[1].1 - 0.3).abs() < 1e-12);
        assert_eq!(r.comps["alpha beta"]["bm25"], 0.0);
        assert_eq!(r.params["bm25"]["top_n"], 20);
    }

    #[test]
    fn temporal_cue_matches_python_lexicon() {
        assert!(has_temporal_cue("When did we deploy?"));
        assert!(has_temporal_cue("the CHRONOLOGICAL list"));
        assert!(has_temporal_cue("what happened in August"));
        assert!(!has_temporal_cue("may I deploy"));
        assert!(!has_temporal_cue("whenever"));
    }

    #[test]
    fn timeline_orders_by_stream_and_marks_via() {
        let entries = vec![
            entry(1, "deploy happened later", "x", false),
            entry(2, "deploy happened first", "x", false),
        ];
        let mut matrix = unit(0.5);
        matrix.extend(unit(0.9));
        let bank = Bank { entries, matrix };
        let mut p = params("when did the deploy happen", 8);
        p.timeline_enabled = true;
        let r = rank(&bank, &unit(1.0), &p, None);
        assert_eq!(ids(&bank, &r), vec![1, 2]);
        assert_eq!(r.params["timeline"]["fired"], true);
    }

    #[test]
    fn rrf_replaces_scores_with_rank_sums() {
        let entries = vec![entry(1, "a", "x", false), entry(2, "b", "assistant", false)];
        let mut matrix = unit(0.9);
        matrix.extend(unit(0.8));
        let bank = Bank { entries, matrix };
        let mut p = params("", 8);
        p.fusion_rrf = true;
        let r = rank(&bank, &unit(1.0), &p, None);
        assert!((r.hits[0].score - 1.0 / 61.0).abs() < 1e-12);
        assert!((r.hits[1].score - 0.85 / 62.0).abs() < 1e-12);
    }

    struct Fixed(Vec<f64>);
    impl CrossEncoder for Fixed {
        fn available(&self) -> bool {
            true
        }
        fn scores(&self, _q: &str, texts: &[&str]) -> Vec<f64> {
            self.0[..texts.len()].to_vec()
        }
    }

    #[test]
    fn rerank_fuses_and_logs() {
        let entries = vec![entry(1, "a", "x", false), entry(2, "b", "x", false)];
        let mut matrix = unit(0.9);
        matrix.extend(unit(0.8));
        let bank = Bank { entries, matrix };
        let mut p = params("q", 8);
        p.rerank_enabled = true;
        let ce = Fixed(vec![0.0, 1.0]);
        let r = rank(&bank, &unit(1.0), &p, Some(&ce));
        assert_eq!(ids(&bank, &r), vec![2, 1]);
        assert_eq!(r.params["reranker"]["fired"], true);
        assert_eq!(r.comps["a"]["ce"], 0.0);
        p.rerank.top_n = 1;
        let r = rank(&bank, &unit(1.0), &p, Some(&ce));
        assert_eq!(
            r.params["reranker"]["skip_reason"],
            "candidate_budget_exceeded"
        );
        assert_eq!(ids(&bank, &r), vec![1, 2]);
    }

    #[test]
    fn neighbors_follow_episode_then_source() {
        let mut entries = vec![
            entry(1, "a", "s", false),
            entry(2, "b", "s", false),
            entry(3, "c", "s", false),
            entry(4, "d", "other", false),
        ];
        entries[3].ts = 2.5;
        let bank = Bank {
            entries,
            matrix: vec![0.0; 4 * DIM],
        };
        let (before, after) = temporal_neighbors(&bank, 1, 1, false);
        assert_eq!(before, vec![0]);
        assert_eq!(after, vec![2]);
    }
}
