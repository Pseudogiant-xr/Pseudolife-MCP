//! `GET /api/trace` (`web/routes.py`; `MemoryService.trace`,
//! `service.py:2212-2267`, over `cms.retrieve_with_trace`): a search's
//! entries plus the structured ranking trace, so a caller can see why a
//! memory did or did not surface. It logs no retrieval event and tracks
//! no slot reads; the served entries' resident access counts accrue, as
//! `retrieve` bumps them.

use std::collections::HashMap;
use std::sync::Arc;

use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};

use crate::http::{App, json_response};
use crate::read::search_route::{Request, list, params_for, py_strip, tribool, unknown_bands};
use crate::search::{self, Item};
use crate::service::{Ready, Service};

/// `_i(params, "top_k", 8)`.
const ROUTE_TOP_K: i64 = 8;

pub(crate) async fn route(app: &App, q: &HashMap<String, String>, _h: &HeaderMap) -> Response {
    let cfg = &app.service.config.memory;
    let ready = match app.service.ensure_init().await {
        Ok(r) => r,
        Err(e) => return json_response(500, &json!({"error": e})),
    };
    let bands = list(q, "band");
    if let Some(msg) = unknown_bands(cfg, bands.as_deref()) {
        return json_response(400, &json!({"error": msg}));
    }
    let query = py_strip(q.get("q").map_or("", String::as_str)).to_string();
    if query.is_empty() {
        return json_response(
            200,
            &json!({"query": "", "count": 0, "entries": [], "trace": null}),
        );
    }
    let top_k = crate::read::console::i_param(q, "top_k", ROUTE_TOP_K);
    let req = Request {
        query,
        top_k,
        top_k_beyond: None,
        sources: list(q, "source"),
        bands,
        tags: None,
        episodes: None,
        min_score: None,
        disable_recency_boost: false,
        rerank: tribool(q, "rerank"),
        bm25: tribool(q, "bm25"),
        count_access: true,
        header_session: None,
        transport_session: None,
    };
    // The work runs to completion in its own task, as the search route's
    // does: Python finishes the handler in the executor regardless.
    let svc = app.service.clone();
    let task = tokio::spawn(async move { trace(&svc, &ready, req).await });
    match task.await {
        Ok(Ok(v)) => json_response(200, &v),
        Ok(Err(e)) => json_response(500, &json!({"error": e})),
        Err(e) => json_response(500, &json!({"error": e.to_string()})),
    }
}

/// `cms.retrieve_with_trace`'s skeleton: the keys `retrieve` fills, in
/// Python's order (`slot_pool` and `timeline` join at the end when their
/// channel runs).
fn skeleton(svc: &Service, req: &Request) -> Map<String, Value> {
    let cfg = &svc.config.memory;
    // `top_k or self.config.top_k`.
    let top_k = if req.top_k == 0 { cfg.top_k } else { req.top_k };
    let Value::Object(m) = json!({
        "config": {
            "preset": cfg.preset,
            // No config field sets it any more: `getattr(..., False)`.
            "chain_residual": false,
            "top_k": top_k,
        },
        "filters": {
            "bands": req.bands,
            "sources": req.sources,
            "episodes": null,
            "tags": null,
            "min_logical_turn": null,
        },
        "tiers": [],
        "candidate_pool": {},
        "chain_residual": {"enabled": false, "synthetic_hits": []},
        "bm25": {"fired": false, "hits": []},
        "reference_pool": [],
        "reranker": {"fired": false, "candidates": []},
        "final_topk": [],
    }) else {
        unreachable!("an object literal");
    };
    m
}

/// `MemoryService.trace` after the band check and the blank-query answer.
async fn trace(svc: &Arc<Service>, ready: &Arc<Ready>, req: Request) -> Result<Value, String> {
    let cfg = &svc.config.memory;
    let k = match req.top_k {
        0 => cfg.top_k.max(0) as usize,
        // `band.retrieve`'s `torch.topk` refuses a negative width.
        n if n < 0 => return Err("selected index k out of range".into()),
        n => usize::try_from(n).unwrap_or(usize::MAX),
    };
    // Python's service lock covers the whole call; this keeps resident
    // access accrual in order with the other searches.
    let _guard = crate::read::search_guard().lock().await;
    let params = params_for(svc, &req, k);
    let mut trace = skeleton(svc, &req);
    let embedder = ready.embedder.clone();
    let query_text = params.query.clone();
    let cache_size = svc.config.embedding.cache_size;
    let qv = tokio::task::spawn_blocking(move || {
        crate::read::embed_cache::encode_query(&embedder, &query_text, cache_size)
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| format!("{e:#}"))?;
    let refs = {
        let q = qv.clone();
        crate::txn::with_client(ready.storage.client(), |db| async move {
            Ok::<_, tokio_postgres::Error>(crate::documents::reference_pool(db, &q, cfg).await)
        })
        .await
        .unwrap_or_else(|e| {
            eprintln!("reference pool skipped: {e}");
            Vec::new()
        })
    };
    let ref_k = cfg.ref_top_k;
    // A served reference hit's `access_count` (its entry is built per call).
    let ref_accesses = i64::from(!crate::mutants::active("search-no-access-bump"));
    let r2 = ready.clone();
    let query = req.query.clone();
    let out = tokio::task::spawn_blocking(move || {
        let bank = r2.bank.read().expect("bank lock");
        let ranked = search::rank_traced(
            &bank,
            &qv,
            &params,
            Some(crate::read::search_route::LazyReranker::get()),
            &refs,
            ref_k,
            if crate::mutants::active("trace-no-trace") {
                None
            } else {
                Some(&mut trace)
            },
        );
        let entries: Vec<Value> = ranked
            .hits
            .iter()
            .map(|h| match h.item {
                Item::Entry(i) => search::entry_json(&bank.entries[i], h.score),
                Item::Reference(i) => {
                    crate::documents::hit_json(&refs[i].nearest, h.score, ref_accesses)
                }
            })
            .collect();
        json!({
            "query": query,
            "count": entries.len(),
            "entries": entries,
            "trace": Value::Object(trace),
        })
    })
    .await
    .map_err(|e| e.to_string())?;
    Ok(out)
}
