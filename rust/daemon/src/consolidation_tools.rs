//! `memory_consolidation_candidates` (spec CC1-CC9): read-only clusters of
//! near-duplicate memories. `mcp_server.memory_consolidation_candidates` →
//! `service.consolidation_candidates` (`service.py:7294-7460`) →
//! `memory/consolidation.py` `cluster_candidates` (130-189).
//!
//! The query anchor runs `CMS.retrieve` (`search::rank`, with its access
//! accrual) and the episode anchor walks the resident bank; candidates are
//! the entries a correction could address, clustered greedily by cosine.

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value, json};

use crate::bank::{Bank, Entry};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::search::{self, Bm25Knobs, Params, RerankKnobs};
use crate::store::{normalize_tags, round4};

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![(
        "memory_consolidation_candidates",
        mcp_memory_consolidation_candidates,
    )]
}

/// A bound positive integer; past `usize`, the largest.
fn count_arg(a: &Map<String, Value>, k: &str, default: usize) -> usize {
    a.get(k).map_or(default, |v| {
        v.as_u64()
            .map_or(usize::MAX, |n| usize::try_from(n).unwrap_or(usize::MAX))
    })
}

fn strings(v: Option<&Value>) -> Option<Vec<String>> {
    v.and_then(Value::as_array).map(|a| {
        a.iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect()
    })
}

/// `consolidation._cosine` (`consolidation.py:72-83`): torch's float32 dot
/// over the float32 norms, 0.0 when either norm is zero. Vectors of
/// different lengths fail, as `torch.dot` raises.
fn cosine(a: &[f32], b: &[f32]) -> Result<f64, String> {
    if a.len() != b.len() {
        return Err(format!(
            "inconsistent tensor size, expected tensor [{}] and src [{}] to have the same number \
             of elements, but got {} and {} elements respectively",
            a.len(),
            b.len(),
            a.len(),
            b.len()
        ));
    }
    let norm = |v: &[f32]| {
        v.iter()
            .map(|&x| f64::from(x) * f64::from(x))
            .sum::<f64>()
            .sqrt() as f32
    };
    let (an, bn) = (f64::from(norm(a)), f64::from(norm(b)));
    if an == 0.0 || bn == 0.0 {
        return Ok(0.0);
    }
    let dot = a
        .iter()
        .zip(b)
        .map(|(&x, &y)| f64::from(x) * f64::from(y))
        .sum::<f64>() as f32;
    Ok(f64::from(dot / (an * bn) as f32))
}

/// One kept cluster: member indexes into the bank, in pool order.
struct Cluster {
    members: Vec<usize>,
    cohesion: f64,
    seed_score: f64,
}

/// `cluster_candidates` (`consolidation.py:96-189`): seeds in descending
/// relevance (ties in pool order), members at or above `min_cohesion` to
/// the seed, clusters below `min_cluster_size` released, at most
/// `max_clusters` kept, then sorted stably by `cohesion × size`, largest
/// first.
fn cluster_candidates(
    bank: &Bank,
    candidates: &[(usize, f64)],
    min_cohesion: f64,
    min_cluster_size: usize,
    max_clusters: usize,
) -> Result<Vec<Cluster>, String> {
    let mut ordered: Vec<(usize, (usize, f64))> = candidates.iter().copied().enumerate().collect();
    ordered.sort_by(|a, b| b.1.1.total_cmp(&a.1.1).then(a.0.cmp(&b.0)));
    let emb = |i: usize| -> &Entry { &bank.entries[i] };
    let mut clustered: HashSet<usize> = HashSet::new();
    let mut clusters: Vec<Cluster> = Vec::new();
    for i in 0..ordered.len() {
        if clusters.len() >= max_clusters {
            break;
        }
        if clustered.contains(&i) {
            continue;
        }
        let (seed, seed_score) = ordered[i].1;
        let mut members = vec![seed];
        let mut member_pos = vec![i];
        for (j, &(_, (cand, _))) in ordered.iter().enumerate().skip(i + 1) {
            if clustered.contains(&j) {
                continue;
            }
            if cosine(&emb(seed).embedding, &emb(cand).embedding)? >= min_cohesion {
                members.push(cand);
                member_pos.push(j);
            }
        }
        if members.len() < min_cluster_size {
            continue;
        }
        clustered.extend(member_pos);
        // `_cluster_cohesion`: the mean of every distinct pairwise cosine.
        let mut total = 0.0;
        let mut pairs = 0usize;
        for x in 0..members.len() {
            for y in x + 1..members.len() {
                total += cosine(&emb(members[x]).embedding, &emb(members[y]).embedding)?;
                pairs += 1;
            }
        }
        let cohesion = if pairs > 0 { total / pairs as f64 } else { 0.0 };
        clusters.push(Cluster {
            members,
            cohesion,
            seed_score,
        });
    }
    if !crate::mutants::active("mcp-consolidation-unsorted") {
        clusters.sort_by(|a, b| {
            let (ka, kb) = (
                a.cohesion * a.members.len() as f64,
                b.cohesion * b.members.len() as f64,
            );
            kb.total_cmp(&ka)
        });
    }
    Ok(clusters)
}

fn mcp_memory_consolidation_candidates<'a>(
    app: &'a crate::http::App,
    call: ToolCall<'a>,
) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let query = a.get("query").and_then(Value::as_str).map(str::to_string);
        let episode = a.get("episode").and_then(Value::as_str).map(str::to_string);
        // `set(x) if x else None`: an empty list is no filter.
        let sources = strings(a.get("sources")).filter(|v| !v.is_empty());
        let tags = strings(a.get("tags")).filter(|v| !v.is_empty());
        let top_k = count_arg(a, "top_k", 20);
        let min_cohesion = a
            .get("min_cohesion")
            .and_then(crate::mcp::binding::float_arg)
            .unwrap_or(0.6);
        let min_cluster_size = count_arg(a, "min_cluster_size", 2);
        let max_clusters = count_arg(a, "max_clusters", 10);
        let svc = &app.service;
        let ready = svc.ensure_init().await.map_err(internal)?;
        let _ws = ready.write.lock().await;
        let cfg = &svc.config.memory;

        let query_anchor = query.as_deref().is_some_and(|q| !q.is_empty());
        let episode_anchor = episode.as_deref().is_some_and(|e| !e.is_empty());
        if !query_anchor && !episode_anchor {
            return Ok(json!({"query": null, "episode": null, "count": 0, "clusters": []}));
        }
        let qv = if query_anchor {
            let embedder = ready.embedder.clone();
            let q = query.clone().unwrap_or_default();
            let cache = svc.config.embedding.cache_size;
            Some(
                tokio::task::spawn_blocking(move || {
                    crate::read::embed_cache::encode_query(&embedder, &q, cache)
                })
                .await
                .map_err(internal)?
                .map_err(internal)?,
            )
        } else {
            None
        };

        let mut candidates: Vec<(usize, f64)> = {
            let bank = ready.bank.read().expect("bank lock");
            // Every resident id once, the correction path's uniqueness rule.
            let mut counts: HashMap<i64, usize> = HashMap::new();
            for e in &bank.entries {
                *counts.entry(e.id).or_default() += 1;
            }
            let actionable =
                |e: &Entry| e.superseded_at.is_none() && counts.get(&e.id).copied() == Some(1);
            if let Some(qv) = &qv {
                let b = &cfg.bm25;
                let s = &cfg.search;
                let params = Params {
                    query: query.clone().unwrap_or_default(),
                    k: top_k,
                    sources: sources.as_ref().map(|v| v.iter().cloned().collect()),
                    tags: tags
                        .as_ref()
                        .map(|t| normalize_tags(Some(&json!(t))).into_iter().collect()),
                    episodes: episode
                        .as_ref()
                        .filter(|e| !e.is_empty())
                        .map(|e| [e.clone()].into_iter().collect()),
                    min_score: Some(0.0),
                    default_floor: s.min_score,
                    bm25: Bm25Knobs {
                        k1: b.k1,
                        b: b.b,
                        weight: b.weight,
                        top_n: b.top_n.max(0) as usize,
                        min_norm: b.min_score,
                    },
                    bm25_enabled: b.enabled,
                    hide_superseded: true,
                    bands: None,
                    band_order: cfg.bands.clone(),
                    disable_recency_boost: false,
                    recency_boost_enabled: cfg.recency_boost_enabled,
                    recency_base_half_life_s: cfg.recency_base_half_life_s,
                    pool_multiplier: s.candidate_pool_multiplier,
                    fusion_rrf: s.fusion == "rrf",
                    fusion_name: s.fusion.clone(),
                    timeline_enabled: s.timeline_channel,
                    rerank_enabled: cfg.reranker_enabled,
                    rerank: RerankKnobs {
                        top_n: cfg.reranker.top_n,
                        fusion_weight: cfg.reranker.fusion_weight,
                        skip_margin: cfg.reranker.skip_margin,
                        model_name: cfg.reranker.model_name.clone(),
                    },
                    count_access: true,
                    now: crate::episodes::now(),
                    filters_json: Value::Null,
                };
                let ranked = search::rank(
                    &bank,
                    qv,
                    &params,
                    Some(crate::read::search_route::LazyReranker::get()),
                );
                ranked
                    .hits
                    .iter()
                    .filter(|h| actionable(&bank.entries[h.idx]))
                    .map(|h| (h.idx, h.score))
                    .collect()
            } else {
                // The episode's entries in band order, one per text.
                let ep = episode.clone().unwrap_or_default();
                let band_pos = |name: &str| cfg.bands.iter().position(|b| b == name);
                let mut order: Vec<usize> = (0..bank.entries.len())
                    .filter(|&i| band_pos(&bank.entries[i].band).is_some())
                    .collect();
                order.sort_by_key(|&i| (band_pos(&bank.entries[i].band), i));
                let mut seen: HashSet<&str> = HashSet::new();
                let mut out = Vec::new();
                for i in order {
                    let e = &bank.entries[i];
                    if !actionable(e)
                        || e.episode_id.as_deref() != Some(ep.as_str())
                        || seen.contains(e.text.as_str())
                        || sources.as_ref().is_some_and(|s| !s.contains(&e.source))
                        || tags.as_ref().is_some_and(|t| {
                            !e.tags
                                .iter()
                                .filter_map(Value::as_str)
                                .any(|x| t.iter().any(|y| y == x))
                        })
                    {
                        continue;
                    }
                    out.push((i, 1.0));
                    seen.insert(e.text.as_str());
                }
                out
            }
        };

        // Filter unavailable rows individually; a read failure keeps them.
        if !candidates.is_empty() && !crate::mutants::active("mcp-consolidation-unverified") {
            let ids: Vec<i64> = {
                let bank = ready.bank.read().expect("bank lock");
                candidates
                    .iter()
                    .map(|&(i, _)| bank.entries[i].id)
                    .collect()
            };
            match ready
                .storage
                .client()
                .query("SELECT id FROM entries WHERE id = ANY($1)", &[&ids])
                .await
            {
                Ok(rows) => {
                    let present: HashSet<i64> = rows.iter().map(|r| r.get(0)).collect();
                    candidates = candidates
                        .into_iter()
                        .zip(ids)
                        .filter(|(_, id)| present.contains(id))
                        .map(|(c, _)| c)
                        .collect();
                }
                Err(e) => {
                    eprintln!("consolidation candidates unverified, storage read failed: {e}");
                }
            }
        }
        candidates.truncate(top_k);

        let bank = ready.bank.read().expect("bank lock");
        let clusters = cluster_candidates(
            &bank,
            &candidates,
            min_cohesion,
            min_cluster_size,
            max_clusters,
        )
        .map_err(internal)?;
        let rendered: Vec<Value> = clusters
            .iter()
            .map(|c| {
                let members: Vec<Value> = c
                    .members
                    .iter()
                    .map(|&i| {
                        let mut d = search::entry_json(&bank.entries[i], 0.0);
                        if let Value::Object(m) = &mut d {
                            m.shift_remove("score");
                        }
                        d
                    })
                    .collect();
                json!({
                    "cohesion": round4(c.cohesion),
                    "seed_score": round4(c.seed_score),
                    "size": c.members.len(),
                    "members": members,
                })
            })
            .collect();
        Ok(json!({
            "query": query,
            "episode": episode,
            "count": rendered.len(),
            "clusters": rendered,
        }))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cosine_is_total_and_bounded() {
        assert_eq!(cosine(&[0.0, 0.0], &[1.0, 0.0]).unwrap(), 0.0);
        assert!((cosine(&[1.0, 0.0], &[1.0, 0.0]).unwrap() - 1.0).abs() < 1e-7);
        assert!(cosine(&[1.0], &[1.0, 0.0]).is_err());
    }
}
