//! `GET /api/search` (spec S1-S19): the route coercions of `web/routes.py`,
//! `MemoryService.search` around the ranking, the retrieval-event log, the
//! chronicle block and the cortex-first block.

use crate::http::{App, json_response};
use crate::read::{chronicle, cortex_search, embed_cache, identity, rerank};
use crate::search::{self, Bm25Knobs, CrossEncoder, Params, RerankKnobs};
use crate::service::{Ready, Service};
use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, OnceLock};

/// Python `str.strip()`: Unicode whitespace plus the U+001C..U+001F separators.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Python numeric literals allow single underscores between digits only.
fn py_underscores(t: &str) -> Option<String> {
    let b: Vec<char> = t.chars().collect();
    for (i, c) in b.iter().enumerate() {
        if *c == '_'
            && !(i > 0 && i + 1 < b.len() && b[i - 1].is_ascii_digit() && b[i + 1].is_ascii_digit())
        {
            return None;
        }
    }
    Some(t.replace('_', ""))
}

pub fn py_int(s: &str) -> Option<i64> {
    py_underscores(py_strip(s))?.parse::<i64>().ok()
}

pub fn py_float(s: &str) -> Option<f64> {
    py_underscores(py_strip(s))?.parse::<f64>().ok()
}

/// `_tribool` (web/routes.py:55): None follows config.
pub fn tribool(q: &HashMap<String, String>, key: &str) -> Option<bool> {
    let v = q.get(key)?;
    if matches!(v.as_str(), "" | "null" | "auto") {
        return None;
    }
    Some(matches!(
        v.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    ))
}

/// `_list` (web/routes.py:63): comma list, stripped, empties dropped.
pub fn list(q: &HashMap<String, String>, key: &str) -> Option<Vec<String>> {
    let items: Vec<String> = q
        .get(key)?
        .split(',')
        .map(|s| py_strip(s).to_string())
        .filter(|s| !s.is_empty())
        .collect();
    (!items.is_empty()).then_some(items)
}

/// `normalize_tags`: lowercased, stripped, de-duplicated in order.
fn normalize_tags(tags: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for t in tags {
        let t = py_strip(t).to_lowercase();
        if !t.is_empty() && !out.contains(&t) {
            out.push(t);
        }
    }
    out
}

/// The cross-encoder, loaded on first use from `PSEUDOLIFE_DAEMON_RERANK_DIR`
/// (`reranker.py:_ensure_loaded`): a failed load disables it for the life of
/// the process.
struct LazyReranker {
    state: Mutex<Option<Option<Arc<rerank::Reranker>>>>,
}

impl LazyReranker {
    fn get() -> &'static LazyReranker {
        static SLOT: OnceLock<LazyReranker> = OnceLock::new();
        SLOT.get_or_init(|| LazyReranker {
            state: Mutex::new(None),
        })
    }

    fn model(&self) -> Option<Arc<rerank::Reranker>> {
        let mut st = self.state.lock().expect("reranker slot");
        if st.is_none() {
            let loaded = std::env::var_os("PSEUDOLIFE_DAEMON_RERANK_DIR").and_then(|dir| {
                match rerank::Reranker::load(std::path::Path::new(&dir), 1) {
                    Ok(r) => Some(Arc::new(r)),
                    Err(e) => {
                        eprintln!("Cross-encoder reranker failed to load ({e}) — disabling.");
                        None
                    }
                }
            });
            *st = Some(loaded);
        }
        st.clone().flatten()
    }

    fn disabled(&self) -> bool {
        matches!(*self.state.lock().expect("reranker slot"), Some(None))
    }
}

impl CrossEncoder for LazyReranker {
    fn available(&self) -> bool {
        !self.disabled()
    }

    fn scores(&self, query: &str, texts: &[&str]) -> Vec<f64> {
        if texts.is_empty() || py_strip(query).is_empty() {
            return Vec::new();
        }
        match self.model() {
            Some(m) => m.rerank(query, texts).unwrap_or_else(|e| {
                eprintln!("Cross-encoder rerank failed mid-call ({e}); falling back.");
                Vec::new()
            }),
            None => Vec::new(),
        }
    }
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// What a search call asks for beyond the query (the route's coercions, or
/// the warmup probe's fixed arguments).
pub struct Request {
    pub query: String,
    pub top_k: i64,
    pub sources: Option<Vec<String>>,
    pub bands: Option<Vec<String>>,
    pub tags: Option<Vec<String>>,
    pub min_score: Option<f64>,
    pub disable_recency_boost: bool,
    pub rerank: Option<bool>,
    pub bm25: Option<bool>,
    pub count_access: bool,
    /// `X-PL-Session` (last value), for the event's identity.
    pub header_session: Option<String>,
}

/// `MemoryService.search` result plus the event id (never served).
pub struct Searched {
    pub body: Map<String, Value>,
    pub event_id: Option<i64>,
}

/// `MemoryService.search` (service.py:1877-2076), after the band check and
/// the blank-query answer.
pub async fn run(svc: &Service, ready: &Arc<Ready>, req: Request) -> anyhow::Result<Searched> {
    let cfg = &svc.config.memory;
    let k = match req.top_k {
        0 => cfg.top_k.max(0) as usize,
        n if n < 0 => anyhow::bail!("top_k must not be negative"),
        n => n as usize,
    };
    let b = &cfg.bm25;
    let s = &cfg.search;
    let tags_norm = req.tags.as_ref().map(|t| normalize_tags(t));
    let params = Params {
        query: req.query.clone(),
        k,
        sources: req
            .sources
            .as_ref()
            .map(|v| v.iter().cloned().collect::<HashSet<_>>()),
        tags: tags_norm
            .as_ref()
            .map(|v| v.iter().cloned().collect::<HashSet<_>>()),
        episodes: None,
        min_score: req.min_score,
        default_floor: s.min_score,
        bm25: Bm25Knobs {
            k1: b.k1,
            b: b.b,
            weight: b.weight,
            top_n: b.top_n.max(0) as usize,
            min_norm: b.min_score,
        },
        bm25_enabled: req.bm25.unwrap_or(b.enabled),
        hide_superseded: cfg.hide_superseded,
        bands: req.bands.as_ref().map(|v| v.iter().cloned().collect()),
        band_order: cfg.bands.clone(),
        disable_recency_boost: req.disable_recency_boost,
        recency_boost_enabled: cfg.recency_boost_enabled,
        recency_base_half_life_s: cfg.recency_base_half_life_s,
        pool_multiplier: s.candidate_pool_multiplier,
        fusion_rrf: s.fusion == "rrf",
        fusion_name: s.fusion.clone(),
        timeline_enabled: s.timeline_channel,
        rerank_enabled: req.rerank.unwrap_or(cfg.reranker_enabled),
        rerank: RerankKnobs {
            top_n: cfg.reranker.top_n,
            fusion_weight: cfg.reranker.fusion_weight,
            skip_margin: cfg.reranker.skip_margin,
            model_name: cfg.reranker.model_name.clone(),
        },
        count_access: req.count_access,
        now: now(),
        filters_json: json!({
            "bands": req.bands,
            "sources": req.sources,
            "episodes": null,
            "tags": tags_norm,
            "min_logical_turn": null,
        }),
    };
    let n_ctg = if crate::mutants::active("search-no-contiguity") {
        0
    } else {
        s.contiguity_neighbors.max(0) as usize
    };
    let hide_superseded = cfg.hide_superseded;
    let floor = cfg.search_confidence_floor;
    let r2 = ready.clone();
    let cache_size = svc.config.embedding.cache_size;
    let (entries_out, served_comps, params_json, direct_scores) =
        tokio::task::spawn_blocking(move || {
            let qv = embed_cache::encode_query(&r2.embedder, &params.query, cache_size)?;
            let bank = r2.bank.as_ref();
            let ranked = search::rank(bank, &qv, &params, Some(LazyReranker::get()));
            let direct_scores: Vec<f64> = ranked.hits.iter().map(|h| h.score).collect();
            // (index, score, via, components) with contiguity neighbours placed
            // around their parent hit (service.py:1984-2004).
            let mut rows: Vec<(usize, f64, Option<&'static str>, Option<Value>)> = ranked
                .hits
                .iter()
                .map(|h| {
                    let text = &bank.entries[h.idx].text;
                    (
                        h.idx,
                        h.score,
                        h.via,
                        ranked.comps.get(text).cloned().map(Value::Object),
                    )
                })
                .collect();
            if n_ctg > 0 && !rows.is_empty() {
                let mut seen: HashSet<&str> = rows
                    .iter()
                    .map(|r| bank.entries[r.0].text.as_str())
                    .collect();
                let mut expanded = Vec::new();
                for row in rows {
                    let (before, after) =
                        search::temporal_neighbors(bank, row.0, n_ctg, hide_superseded);
                    let neighbour = |i: usize| {
                        (
                            i,
                            0.0,
                            Some("contiguity"),
                            Some(json!({"channel": "contiguity"})),
                        )
                    };
                    for nb in before {
                        if seen.insert(bank.entries[nb].text.as_str()) {
                            expanded.push(neighbour(nb));
                        }
                    }
                    expanded.push(row);
                    for nb in after {
                        if seen.insert(bank.entries[nb].text.as_str()) {
                            expanded.push(neighbour(nb));
                        }
                    }
                }
                rows = expanded;
            }
            let mut out: Vec<(usize, Value)> = rows
                .iter()
                .map(|(i, s, via, _)| {
                    let mut d = search::entry_json(&bank.entries[*i], *s);
                    if let (Some(v), Value::Object(m)) = (via, &mut d) {
                        m.insert("via".into(), json!(v));
                    }
                    (*i, d)
                })
                .collect();
            search::annotate_supersession_in(bank, &mut out);
            let comps: Vec<Option<Value>> = rows.into_iter().map(|r| r.3).collect();
            Ok::<_, anyhow::Error>((
                out.into_iter().map(|(_, d)| d).collect::<Vec<Value>>(),
                comps,
                ranked.params,
                direct_scores,
            ))
        })
        .await??;

    let db = ready.storage.client();
    let mut body = Map::new();
    body.insert("query".into(), json!(req.query));
    body.insert("count".into(), json!(entries_out.len()));
    let best = direct_scores
        .iter()
        .copied()
        .fold(f64::NEG_INFINITY, f64::max);
    let low = direct_scores.is_empty() || (floor > 0.0 && best < floor);
    body.insert("low_confidence".into(), json!(low));
    match chronicle::events_for(db, &req.query).await {
        Ok(Some(ev)) => {
            body.insert("events".into(), Value::Array(ev.events));
            if let Some(t) = ev.total {
                body.insert("events_total".into(), json!(t));
            }
        }
        Ok(None) => {}
        Err(e) => return Err(e),
    }
    let mut params_json = params_json;
    params_json["contiguity_neighbors"] = json!(n_ctg);
    let event_id = if cfg.retrieval_log_enabled && !crate::mutants::active("search-skip-event") {
        log_event(db, &req, &entries_out, &served_comps, &params_json).await
    } else {
        None
    };
    body.insert("entries".into(), Value::Array(entries_out));
    Ok(Searched { body, event_id })
}

/// `_log_retrieval_event` (service.py:5892-5946): never fails the search.
async fn log_event(
    db: &tokio_postgres::Client,
    req: &Request,
    entries: &[Value],
    comps: &[Option<Value>],
    params: &Value,
) -> Option<i64> {
    let served: Vec<Value> = entries
        .iter()
        .zip(comps)
        .enumerate()
        .filter_map(|(rank, (d, comp))| {
            let id = d.get("id").and_then(Value::as_i64)?;
            let mut row = json!({
                "entry_id": id, "score": d.get("score").cloned().unwrap_or(Value::Null),
                "rank": rank, "via": d.get("via").cloned().unwrap_or(Value::Null),
                "bank": d.get("bank").cloned().unwrap_or(Value::Null),
            });
            if let Some(c) = comp {
                row["components"] = c.clone();
            }
            Some(row)
        })
        .collect();
    let session = identity::session_id(db, req.header_session.as_deref(), now()).await;
    let episode = match &session {
        Some(s) => identity::open_leaf_episode(db, s).await,
        None => None,
    };
    let served = Value::Array(served);
    let res = db
        .query_one(
            "INSERT INTO retrieval_events (query_text, origin, session_id, episode_id, served, \
             params, created_at) VALUES ($1, 'search', $2, $3, $4, $5, $6) RETURNING id",
            &[&req.query, &session, &episode, &served, params, &now()],
        )
        .await;
    match res {
        Ok(row) => Some(row.get(0)),
        Err(e) => {
            eprintln!("retrieval-event log failed: {e}");
            None
        }
    }
}

/// `ConsoleRoutes._search` (web/routes.py:355-379).
pub async fn route(app: &App, raw_query: Option<&str>, h: &HeaderMap) -> Response {
    let q = crate::http::parse_query(raw_query);
    let cfg = &app.service.config.memory;
    let ready = match app.service.ensure_init().await {
        Ok(r) => r,
        Err(e) => return json_response(500, &json!({"error": e})),
    };
    let bands = list(&q, "band");
    if let Some(bands) = &bands {
        let unknown: Vec<&String> = bands.iter().filter(|b| !cfg.bands.contains(b)).collect();
        if !unknown.is_empty() {
            let mut valid = cfg.bands.clone();
            valid.sort();
            let msg = format!("unknown band name(s) {unknown:?} — this preset has {valid:?}");
            return json_response(400, &json!({"error": msg}));
        }
    }
    let query = q
        .get("q")
        .map(|s| py_strip(s).to_string())
        .unwrap_or_default();
    if query.is_empty() {
        return json_response(
            200,
            &json!({"entries": [], "query": "", "count": 0, "low_confidence": true}),
        );
    }
    let header_session = h
        .get_all("x-pl-session")
        .iter()
        .next_back()
        .map(|v| v.as_bytes().iter().map(|&b| b as char).collect::<String>());
    let req = Request {
        query: query.clone(),
        top_k: q
            .get("top_k")
            .filter(|s| !s.is_empty())
            .and_then(|s| py_int(s))
            .unwrap_or(search::ROUTE_TOP_K),
        sources: list(&q, "source"),
        bands,
        tags: list(&q, "tag"),
        min_score: q
            .get("min_score")
            .filter(|s| !s.is_empty())
            .and_then(|s| py_float(s)),
        disable_recency_boost: tribool(&q, "disable_recency_boost") == Some(true),
        rerank: tribool(&q, "rerank"),
        bm25: tribool(&q, "bm25"),
        count_access: true,
        header_session,
    };
    let mut out = match run(&app.service, &ready, req).await {
        Ok(s) => s,
        Err(e) => return json_response(500, &json!({"error": e.to_string()})),
    };
    let cc = &cfg.cortex;
    if cc.enabled && cc.search_first && !crate::mutants::active("search-no-cortex") {
        let knobs = cortex_search::CortexKnobs::from_config(&app.service.config);
        let r2 = ready.clone();
        let cache_size = app.service.config.embedding.cache_size;
        let q2 = query.clone();
        let qv = tokio::task::spawn_blocking(move || {
            embed_cache::encode_query(&r2.embedder, &q2, cache_size)
        })
        .await;
        let facts = match qv {
            Ok(Ok(qv)) => {
                cortex_search::cortex_search(
                    &ready.cortex,
                    ready.storage.client(),
                    &qv,
                    &query,
                    5,
                    cc.guard_min_score,
                    &knobs,
                    now(),
                )
                .await
            }
            Ok(Err(e)) => Err(e),
            Err(e) => Err(e.into()),
        };
        match facts {
            Ok(facts) if !facts.is_empty() => {
                if let Some(id) = out.event_id
                    && let Err(e) =
                        cortex_search::attach_served_facts(ready.storage.client(), id, &facts).await
                {
                    eprintln!("attach_served_facts failed: {e}");
                }
                out.body.insert("cortex".into(), Value::Array(facts));
            }
            Ok(_) => {}
            Err(e) => return json_response(500, &json!({"error": e.to_string()})),
        }
    }
    json_response(200, &Value::Object(out.body))
}

/// The warmup probe (S17): `search("warmup probe", top_k=1, count_access=False)`.
pub async fn warmup_probe(svc: &Service, ready: &Arc<Ready>) {
    let req = Request {
        query: "warmup probe".into(),
        top_k: 1,
        sources: None,
        bands: None,
        tags: None,
        min_score: None,
        disable_recency_boost: false,
        rerank: None,
        bm25: None,
        count_access: false,
        header_session: None,
    };
    if let Err(e) = run(svc, ready, req).await {
        eprintln!("warmup search failed: {e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn coercions_follow_python() {
        assert_eq!(py_int(" 1_0 "), Some(10));
        assert_eq!(py_int("1__0"), None);
        assert_eq!(py_float("_0.5"), None);
        let q: HashMap<String, String> = [
            ("tag".to_string(), " A, ,b ".to_string()),
            ("bm25".into(), "auto".into()),
        ]
        .into();
        assert_eq!(
            list(&q, "tag"),
            Some(vec!["A".to_string(), "b".to_string()])
        );
        assert_eq!(tribool(&q, "bm25"), None);
        assert_eq!(
            normalize_tags(&["A".into(), "a".into(), " b".into()]),
            vec!["a", "b"]
        );
    }
}
