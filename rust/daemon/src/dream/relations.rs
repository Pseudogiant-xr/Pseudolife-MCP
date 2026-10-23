//! The dream pass's post-commit graph work, unlocked and best-effort as in
//! Python (`service_dream.py` `_dream_extract_relations` 451-466,
//! `_link_dream_relations` 367-449, `_propose_dream_alias_candidates`
//! 1349-1489; `service.py` `_resolve_or_create_entity(propose_dupes=True)`
//! 7570-7599, `_propose_write_dedup` 7601-7657, `graph_backfill_sources`
//! 8407-8422, `retype_quarantined_links` 5268-5348).
//!
//! Model calls run without the writer lock; the statements that follow run
//! under it, as Python's service lock.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use serde_json::{Value, json};
use tokio_postgres::Client;

use super::extractor::Extractor;
use super::gates::{self, NameRow};
use super::run::{PEntry, internal};
use crate::graph_review_tools::actions::{edge_confidence, insert_proposal};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::storage::graph::{self, Edge, norm_name};
use crate::storage::py_repr;
use crate::stores::cortex::dot_f32;

/// `GENERIC_HUB_NORMS` (`memory/relation_quality.py:27`).
const GENERIC_HUB_NORMS: [&str; 4] = ["mcp", "master", "origin-master", "claude-md"];
/// `_ALIAS_SCAN_MAX`, `_ALIAS_MEMO_MAX` (`service_dream.py:350-355`).
const ALIAS_SCAN_MAX: usize = 20;
const ALIAS_MEMO_MAX: usize = 4096;

fn now() -> f64 {
    crate::episodes::now()
}

type Res<T> = Result<T, ToolError>;

/// The relation registry the dream offers the extractor: every relation
/// but `prefers` / `avoids`, as `(name, description)`.
async fn registry(client: &Client) -> Res<Vec<(String, String)>> {
    let rows = graph::load_relations(client).await.map_err(internal)?;
    Ok(rows
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| {
            let name = r.get("name")?.as_str()?.to_string();
            let desc = r.get("description")?.as_str().unwrap_or("").to_string();
            (name != "prefers" && name != "avoids").then_some((name, desc))
        })
        .collect())
}

/// `entity_sources_map` (`storage/postgres.py:4262-4268`).
async fn entity_sources_map(client: &Client) -> Res<HashMap<i64, Vec<String>>> {
    let rows = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source",
            &[],
        )
        .await
    })
    .await
    .map_err(internal)?;
    let mut out: HashMap<i64, Vec<String>> = HashMap::new();
    for r in rows {
        out.entry(r.get(0)).or_default().push(r.get(1));
    }
    Ok(out)
}

/// `upsert_entity_source(entity_id, source, "derived", now)`.
async fn upsert_entity_source(client: &Client, id: i64, source: &str) -> Res<()> {
    let t = now();
    crate::txn::run(client, |c| async move {
        c.execute(
            "INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) \
             VALUES ($1, $2, 1, 'derived', $3) \
             ON CONFLICT (entity_id, source) DO UPDATE SET \
               count = entity_sources.count + 1, \
               updated_at = EXCLUDED.updated_at, \
               origin = CASE WHEN entity_sources.origin = 'manual' \
                             THEN 'manual' ELSE EXCLUDED.origin END",
            &[&id, &source, &t],
        )
        .await
    })
    .await
    .map(drop)
    .map_err(internal)
}

/// `dismissed_pairs` (`storage/postgres.py:3427-3429`).
async fn dismissed_pairs(client: &Client) -> Res<HashSet<(String, String)>> {
    let rows = crate::txn::with_client(client, |c| async move {
        c.query("SELECT a_norm, b_norm FROM dismissed_pairs", &[])
            .await
    })
    .await
    .map_err(internal)?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
}

/// `entity_fact_counts` (`storage/postgres.py:4227-4235`).
async fn entity_fact_counts(client: &Client) -> Res<HashMap<i64, i64>> {
    let rows = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT entity_id, COUNT(*) FROM facts WHERE status = 'current' \
             AND entity_id IS NOT NULL GROUP BY entity_id",
            &[],
        )
        .await
    })
    .await
    .map_err(internal)?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
}

/// `insert_entity_proposal` (`storage/postgres.py:3716-3729`): the new id,
/// or None on a conflict or a vanished endpoint.
async fn insert_entity_proposal(
    client: &Client,
    from: i64,
    into: i64,
    score: f64,
    reason: &str,
) -> Res<Option<i64>> {
    let t = now();
    let score = score as f32;
    let r = crate::txn::run(client, |c| async move {
        c.query_opt(
            "INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at) \
             VALUES ('merge', $1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING id",
            &[&from, &into, &score, &reason, &t],
        )
        .await
    })
    .await;
    match r {
        Ok(row) => Ok(row.map(|r| r.get(0))),
        Err(e) if e.code() == Some(&tokio_postgres::error::SqlState::FOREIGN_KEY_VIOLATION) => {
            Ok(None)
        }
        Err(e) => Err(internal(e)),
    }
}

/// `degree_counts(load_graph()["edges"])` and the graph's entities.
fn degrees(g: &Value) -> std::collections::BTreeMap<i64, usize> {
    let edges: Vec<crate::graph_read::StoredEdge> = g["edges"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| crate::graph_read::StoredEdge {
            src_id: e["src_id"].as_i64().unwrap_or(0),
            dst_id: e["dst_id"].as_i64().unwrap_or(0),
        })
        .collect();
    crate::graph_read::degree_counts(&edges)
}

/// `_propose_write_dedup` (`service.py:7601-7657`): advisory merge
/// proposals for a dream-minted entity that near-duplicates an existing
/// name. A failure is logged and swallowed.
async fn propose_write_dedup(svc: &Service, client: &Client, id: i64, name: &str) {
    if let Err(e) = write_dedup(svc, client, id, name).await {
        eprintln!("write-dedup scan skipped ({e:?}): {name:?}");
    }
}

async fn write_dedup(svc: &Service, client: &Client, id: i64, name: &str) -> Res<()> {
    let thr = svc.config.dream.write_dedup_min_jaccard;
    if thr <= 0.0 {
        return Ok(());
    }
    let g = graph::load_graph(client).await.map_err(internal)?;
    let existing: Vec<NameRow> = g["entities"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|e| e["id"].as_i64() != Some(id))
        .map(|e| {
            let eid = e["id"].as_i64().unwrap_or(0);
            NameRow {
                id: eid,
                canonical: e["canonical"].as_str().unwrap_or("").to_string(),
                display: e["display"].as_str().unwrap_or("").to_string(),
                aliases: g["aliases"][eid.to_string()]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|a| a.as_str().map(str::to_string))
                    .collect(),
            }
        })
        .collect();
    let dismissed = dismissed_pairs(client).await?;
    let matches = gates::near_duplicate_names(name, &existing, thr, &dismissed);
    let matches: Vec<_> = matches
        .into_iter()
        .filter(|(_, display, _)| crate::graph_review::merge_veto(name, display).is_none())
        .collect();
    let junk: HashSet<i64> = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT entity_id FROM entity_proposals WHERE status = 'pending' AND kind = 'junk'",
            &[],
        )
        .await
    })
    .await
    .map_err(internal)?
    .iter()
    .map(|r| r.get(0))
    .collect();
    let matches: Vec<_> = matches
        .into_iter()
        .filter(|(eid, _, _)| !junk.contains(eid) && !junk.contains(&id))
        .collect();
    if matches.is_empty() {
        return Ok(());
    }
    let deg = degrees(&g);
    let facts = entity_fact_counts(client).await?;
    let evidence =
        |e: i64| deg.get(&e).copied().unwrap_or(0) as i64 + facts.get(&e).copied().unwrap_or(0);
    for (eid, display, score) in matches.into_iter().take(3) {
        let (mut a, mut b) = (id, eid);
        if evidence(a) > evidence(b) {
            std::mem::swap(&mut a, &mut b);
        }
        let reason = format!("write-dedup: {} ~ {}", py_repr(name), py_repr(&display));
        insert_entity_proposal(client, a, b, score, &reason).await?;
    }
    Ok(())
}

/// `_resolve_or_create_entity(name, propose_dupes=True)`: the write-dedup
/// scan runs for a node minted under the name itself (a slot-key fold
/// re-enters without it).
async fn resolve_or_create_dream(svc: &Service, client: &Client, name: &str) -> Res<Value> {
    let (ent, folded) =
        crate::graph_write_tools::resolve_or_create_folded(client, name, None).await?;
    if ent["created"] == json!(true) && !folded {
        propose_write_dedup(svc, client, ent["id"].as_i64().unwrap_or(0), name).await;
    }
    Ok(ent)
}

/// `_dream_extract_relations` then `_link_dream_relations`: edges written.
pub(super) async fn extract_relations(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    extractor: &Extractor,
    texts: &[String],
    batch_sources: &[String],
    entries: &[PEntry],
) -> i64 {
    let cfg = &svc.config.dream;
    let Some(compat) = extractor.as_compat() else {
        return 0;
    };
    if !cfg.extract_relations || texts.is_empty() {
        return 0;
    }
    let reg = {
        let _ws = ready.write.lock().await;
        match registry(ready.storage.client()).await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("dream relation extraction failed ({e:?}); claims kept");
                return 0;
            }
        }
    };
    let rels = match compat.extract_relations(texts, &reg).await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("dream relation extraction failed ({e:?}); claims kept");
            return 0;
        }
    };
    let _ws = ready.write.lock().await;
    match link_relations(svc, ready.storage.client(), &rels, batch_sources, entries).await {
        Ok(n) => n,
        Err(e) => {
            eprintln!("dream relation extraction failed ({e:?}); claims kept");
            0
        }
    }
}

/// `_link_dream_relations` (`service_dream.py:367-449`).
async fn link_relations(
    svc: &Service,
    client: &Client,
    rels: &[super::extractor::RelationClaim],
    batch_sources: &[String],
    entries: &[PEntry],
) -> Res<i64> {
    if rels.is_empty() {
        return Ok(0);
    }
    let cfg = &svc.config.dream;
    let m = &svc.config.memory;
    let known: Vec<String> = registry(client)
        .await?
        .into_iter()
        .map(|(n, _)| n)
        .collect();
    let floor = cfg.min_relation_confidence;
    let stamp = gates::scope_keys(
        &m.scopes_exclude,
        &m.scopes_rollup,
        batch_sources.iter().map(String::as_str),
    );
    let mut scope_map = entity_sources_map(client).await?;
    let mut n = 0;
    for r in rels {
        let (raw_src, raw_dst) = (r.src.as_str(), r.dst.as_str());
        let (src_n, dst_n) = (norm_name(raw_src), norm_name(raw_dst));
        if src_n.is_empty() || dst_n.is_empty() || src_n == dst_n {
            continue;
        }
        if GENERIC_HUB_NORMS.contains(&src_n.as_str())
            || GENERIC_HUB_NORMS.contains(&dst_n.as_str())
        {
            continue;
        }
        if crate::junk_names::junk_name_reason(raw_src)
            .or_else(|| crate::junk_names::junk_name_reason(raw_dst))
            .is_some()
        {
            continue;
        }
        let (resolved, _) = crate::graph_read::resolve_relation(&known, &r.relation);
        let relation = resolved.unwrap_or_else(|| "related-to".to_string());
        let conf = edge_confidence(raw_src, &relation, raw_dst);
        if conf < floor {
            continue;
        }
        // A reply's lone surrogate fails the endpoint's first statement in
        // Python (psycopg cannot encode it); the pass then keeps no count.
        super::run::encodable(&[raw_src])?;
        let src_e = resolve_or_create_dream(svc, client, raw_src).await?;
        super::run::encodable(&[raw_dst])?;
        let dst_e = resolve_or_create_dream(svc, client, raw_dst).await?;
        for ent in [&src_e, &dst_e] {
            if ent["created"] == json!(true) && !stamp.is_empty() {
                let id = ent["id"].as_i64().unwrap_or(0);
                for s in &stamp {
                    upsert_entity_source(client, id, s).await?;
                }
                scope_map.insert(id, stamp.iter().cloned().collect());
            }
        }
        let (sid, did) = (
            src_e["id"].as_i64().unwrap_or(0),
            dst_e["id"].as_i64().unwrap_or(0),
        );
        let ss: std::collections::BTreeSet<String> = scope_map
            .get(&sid)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .collect();
        let ds: std::collections::BTreeSet<String> = scope_map
            .get(&did)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .collect();
        let quarantine_below = cfg.relation_quarantine_below;
        let low_first = crate::mutants::active("mcp-dream-run-gate-order");
        if low_first && conf < quarantine_below {
            file_low_confidence(client, sid, &relation, did, conf).await?;
            continue;
        }
        if !ss.is_empty() && !ds.is_empty() && ss.is_disjoint(&ds) {
            if relation == "related-to" {
                continue;
            }
            let ssv: Vec<String> = ss.iter().cloned().collect();
            let dsv: Vec<String> = ds.iter().cloned().collect();
            let rationale = format!(
                "cross-project: {} x {}",
                gates::py_list_repr(&ssv),
                gates::py_list_repr(&dsv)
            );
            insert_proposal(
                client,
                sid,
                &relation,
                did,
                conf as f32,
                None,
                Some(rationale),
                "dream-cross-project",
                now(),
            )
            .await?;
            continue;
        }
        if conf < quarantine_below {
            file_low_confidence(client, sid, &relation, did, conf).await?;
            continue;
        }
        let src_cf = crate::board::text::casefold(raw_src);
        let dst_cf = crate::board::text::casefold(raw_dst);
        let evidence: Vec<i64> = entries
            .iter()
            .filter_map(|e| {
                let id = e.db_id?;
                let text = crate::board::text::casefold(&e.text);
                (text.contains(&src_cf) && text.contains(&dst_cf)).then_some(id)
            })
            .collect();
        let edge = graph::upsert_edge(
            client,
            Edge {
                src_id: sid,
                relation: &relation,
                dst_id: did,
                confidence: conf,
                origin: Some("agent"),
                revive: false,
                source_entry_ids: &evidence,
            },
        )
        .await
        .map_err(internal)?;
        if !edge.is_null() {
            n += 1;
        }
    }
    Ok(n)
}

/// The low-confidence quarantine: the claim goes to `edge_proposals`.
async fn file_low_confidence(
    client: &Client,
    sid: i64,
    relation: &str,
    did: i64,
    conf: f64,
) -> Res<()> {
    let rationale = format!("low-confidence dream edge ({})", gates::py_g(conf));
    insert_proposal(
        client,
        sid,
        relation,
        did,
        conf as f32,
        None,
        Some(rationale),
        "dream-low-confidence",
        now(),
    )
    .await
    .map(drop)
}

/// `_propose_dream_alias_candidates` (`service_dream.py:1349-1489`):
/// proposals filed; never fails.
pub(super) async fn propose_alias_candidates(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    new_entities: &[(String, String)],
    known: &HashSet<String>,
) -> i64 {
    match alias_candidates(svc, ready, new_entities, known).await {
        Ok(n) => n,
        Err(e) => {
            eprintln!("dream alias-candidate scan skipped ({e:?})");
            0
        }
    }
}

async fn alias_candidates(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    new_entities: &[(String, String)],
    known: &HashSet<String>,
) -> Res<i64> {
    let thr = svc.config.dream.alias_candidate_min_cosine;
    if thr <= 0.0 || new_entities.is_empty() {
        return Ok(0);
    }
    let existing: Vec<(String, String)> = {
        let _ws = ready.write.lock().await;
        let snap = ready.cortex.snapshot();
        let mut out: Vec<(String, String)> = Vec::new();
        for r in snap.current_records() {
            let k = &r.key().0;
            if known.contains(k) && !out.iter().any(|(e, _)| e == k) {
                out.push((k.clone(), r.entity.clone()));
            }
        }
        out
    };
    if existing.is_empty() {
        return Ok(0);
    }
    let new_items: Vec<&(String, String)> = new_entities.iter().take(ALIAS_SCAN_MAX).collect();
    let mut names: Vec<String> = Vec::new();
    for d in new_items
        .iter()
        .map(|(_, d)| d)
        .chain(existing.iter().map(|(_, d)| d))
    {
        if !names.contains(d) {
            names.push(d.clone());
        }
    }
    let mut memo = ready
        .dream_run
        .alias_memo
        .lock()
        .expect("alias memo")
        .clone();
    let missing: Vec<String> = names
        .iter()
        .filter(|n| !memo.contains_key(*n))
        .cloned()
        .collect();
    if !missing.is_empty() {
        // The tokenizer refuses a lone surrogate (a TypeError in Python):
        // the screen ends there, its memo untouched.
        let refs: Vec<&str> = missing.iter().map(String::as_str).collect();
        super::run::encodable(&refs)?;
        let embedder = ready.embedder.clone();
        let batch = missing.clone();
        let fresh = tokio::task::spawn_blocking(move || embedder.encode(&batch, true))
            .await
            .map_err(internal)?
            .map_err(internal)?;
        for (n, v) in missing.into_iter().zip(fresh) {
            memo.insert(n, Arc::from(v));
        }
    }
    let kept: HashMap<String, Arc<[f32]>> = names
        .iter()
        .take(ALIAS_MEMO_MAX)
        .filter_map(|n| memo.get(n).map(|v| (n.clone(), v.clone())))
        .collect();
    *ready.dream_run.alias_memo.lock().expect("alias memo") = kept;
    let mut matches = Vec::new();
    for (_, disp) in &new_items {
        let a = &memo[disp];
        let mut best = 0;
        let mut score = f32::NEG_INFINITY;
        for (j, (_, ex)) in existing.iter().enumerate() {
            let s = dot_f32(a, &memo[ex]);
            if s > score {
                best = j;
                score = s;
            }
        }
        let score = f64::from(score);
        if score < thr {
            continue;
        }
        let target = existing[best].1.clone();
        let pair = gates::sorted_pair(&norm_name(disp), &norm_name(&target));
        if pair.0 == pair.1 {
            continue;
        }
        if crate::dream::judge_agreement::variant_conflict(disp, &target) {
            continue;
        }
        if crate::graph_review::merge_veto(disp, &target).is_some() {
            continue;
        }
        matches.push((disp.clone(), target, pair, score));
    }
    if matches.is_empty() {
        return Ok(0);
    }
    let _ws = ready.write.lock().await;
    let client = ready.storage.client();
    let dismissed = dismissed_pairs(client).await?;
    let g = graph::load_graph(client).await.map_err(internal)?;
    let deg = degrees(&g);
    let fct = entity_fact_counts(client).await?;
    let evidence =
        |e: i64| deg.get(&e).copied().unwrap_or(0) as i64 + fct.get(&e).copied().unwrap_or(0);
    let mut filed = 0;
    for (disp, target, pair, score) in matches {
        if dismissed.contains(&pair) {
            continue;
        }
        let a = graph::find_entity(client, &norm_name(&disp))
            .await
            .map_err(internal)?;
        let b = graph::find_entity(client, &norm_name(&target))
            .await
            .map_err(internal)?;
        if a.is_null() || b.is_null() {
            continue;
        }
        let (ai, bi) = (a["id"].as_i64().unwrap_or(0), b["id"].as_i64().unwrap_or(0));
        if ai == bi {
            continue;
        }
        let canon = gates::sorted_pair(
            a["canonical"].as_str().unwrap_or(""),
            b["canonical"].as_str().unwrap_or(""),
        );
        if dismissed.contains(&canon) {
            continue;
        }
        let (mut frm, mut into) = (ai, bi);
        if evidence(frm) > evidence(into) {
            std::mem::swap(&mut frm, &mut into);
        }
        let reason = format!(
            "dream-alias: {} ~ {} (cosine {score:.2})",
            py_repr(&disp),
            py_repr(&target)
        );
        if insert_entity_proposal(
            client,
            frm,
            into,
            crate::graph_review::round3(score),
            &reason,
        )
        .await?
        .is_some()
        {
            filed += 1;
        }
    }
    Ok(filed)
}

/// `graph_backfill_sources` (`service.py:8407-8422`) over
/// `backfill_entity_sources` (`storage/postgres.py:4270-4325`).
pub(super) async fn graph_backfill_sources(svc: &Arc<Service>, ready: &Arc<Ready>) -> Res<i64> {
    let m = &svc.config.memory;
    let key = |s: &str| crate::storage::graph::py_lower(crate::storage::py_strip(s));
    let excl: Vec<String> = m.scopes_exclude.iter().map(|s| key(s)).collect();
    let roll: HashMap<String, String> = m
        .scopes_rollup
        .iter()
        .map(|(k, v)| (key(k), key(v)))
        .collect();
    let _ws = ready.write.lock().await;
    let client = ready.storage.client();
    let rows = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT m.entity_id, en.source, COUNT(DISTINCT t.entry_id) AS cnt \
             FROM (SELECT DISTINCT entity_id, entity_norm FROM facts \
                   WHERE entity_id IS NOT NULL AND status = 'current') m \
             JOIN memory_traces t ON t.entity_norm = m.entity_norm \
             JOIN entries en ON en.id = t.entry_id \
             WHERE en.source <> '' \
             GROUP BY m.entity_id, en.source",
            &[],
        )
        .await
    })
    .await
    .map_err(internal)?;
    let mut agg: indexmap::IndexMap<(i64, String), i32> = indexmap::IndexMap::new();
    for r in rows {
        let (id, source, cnt): (i64, String, i64) = (r.get(0), r.get(1), r.get(2));
        let k = key(&source);
        if k.is_empty() || excl.contains(&k) {
            continue;
        }
        *agg.entry((id, k.clone())).or_insert(0) += cnt as i32;
        if let Some(umb) = roll.get(&k)
            && !umb.is_empty()
            && *umb != k
            && !excl.contains(umb)
        {
            *agg.entry((id, umb.clone())).or_insert(0) += cnt as i32;
        }
    }
    let t = now();
    let (excl, agg) = (&excl, &agg);
    crate::txn::run(client, |c| async move {
        c.execute(
            "DELETE FROM entity_sources WHERE origin = 'derived' AND \
             (LOWER(TRIM(source)) = ANY($1) OR source <> LOWER(source))",
            &[excl],
        )
        .await?;
        for ((id, source), cnt) in agg {
            c.execute(
                "INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) \
                 VALUES ($1, $2, $3, 'derived', $4) \
                 ON CONFLICT (entity_id, source) DO UPDATE SET \
                   count = EXCLUDED.count, updated_at = EXCLUDED.updated_at, \
                   origin = CASE WHEN entity_sources.origin = 'manual' \
                                 THEN 'manual' ELSE 'derived' END",
                &[id, source, cnt, &t],
            )
            .await?;
        }
        Ok::<_, tokio_postgres::Error>(())
    })
    .await
    .map_err(internal)?;
    Ok(agg.len() as i64)
}

/// `retype_quarantined_links` (`service.py:5268-5348`).
pub(super) async fn retype_quarantined_links(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    extractor: &Extractor,
    limit: i64,
) -> Res<Value> {
    let zero = json!({"considered": 0, "retyped": 0, "settled": 0});
    let cap = usize::try_from(limit.max(0)).unwrap_or(0);
    let Some(compat) = extractor.as_compat() else {
        return Ok(zero);
    };
    if cap == 0 {
        return Ok(zero);
    }
    let (pending, entries, known) = {
        let _ws = ready.write.lock().await;
        let client = ready.storage.client();
        let pending: Vec<(i64, String, String)> = crate::txn::with_client(client, |c| async move {
            c.query(
                "SELECT p.id, s.display, d.display FROM edge_proposals p \
                 JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id \
                 WHERE p.status = 'pending' AND p.source = 'dream-low-confidence' \
                 ORDER BY p.confidence DESC, p.id",
                &[],
            )
            .await
        })
        .await
        .map_err(internal)?
        .iter()
        .map(|r| (r.get(0), r.get(1), r.get(2)))
        .take(cap)
        .collect();
        if pending.is_empty() {
            return Ok(zero);
        }
        let entries: Vec<String> = crate::txn::with_client(client, |c| async move {
            c.query("SELECT text FROM entries ORDER BY id", &[]).await
        })
        .await
        .map_err(internal)?
        .iter()
        .map(|r| r.get(0))
        .collect();
        (pending, entries, registry(client).await?)
    };
    let names: Vec<String> = known.iter().map(|(n, _)| n.clone()).collect();
    let tokens: Vec<_> = entries
        .iter()
        .map(|t| crate::graph_review::token_set(t))
        .collect();
    let (mut retyped, mut settled) = (0, 0);
    for (pid, src, dst) in &pending {
        let (wa, wb) = (
            crate::graph_review::token_set(src),
            crate::graph_review::token_set(dst),
        );
        if wa.is_empty() || wb.is_empty() {
            continue;
        }
        let texts: Vec<String> = entries
            .iter()
            .zip(&tokens)
            .filter(|(_, t)| wa.is_subset(t) && wb.is_subset(t))
            .map(|(e, _)| e.clone())
            .take(4)
            .collect();
        if texts.is_empty() {
            continue;
        }
        let found = match compat.extract_relations(&texts, &known).await {
            Ok(f) => f,
            Err(e) => {
                eprintln!("retype pass halted ({e:?}); {settled} settled so far");
                break;
            }
        };
        let want: HashSet<String> = [norm_name(src), norm_name(dst)].into_iter().collect();
        let mut typed = None;
        for r in &found {
            let got: HashSet<String> = [norm_name(&r.src), norm_name(&r.dst)].into_iter().collect();
            if got != want {
                continue;
            }
            let (resolved, _) = crate::graph_read::resolve_relation(&names, &r.relation);
            if let Some(rel) = resolved.filter(|rel| rel != "related-to") {
                typed = Some((r.src.clone(), rel, r.dst.clone()));
                break;
            }
        }
        let _ws = ready.write.lock().await;
        let client = ready.storage.client();
        if let Some((ts, rel, td)) = &typed {
            let src_e = crate::graph_write_tools::resolve_or_create(client, ts, None).await?;
            let dst_e = crate::graph_write_tools::resolve_or_create(client, td, None).await?;
            let conf = edge_confidence(ts, rel, td);
            insert_proposal(
                client,
                src_e["id"].as_i64().unwrap_or(0),
                rel,
                dst_e["id"].as_i64().unwrap_or(0),
                conf as f32,
                None,
                Some(format!(
                    "retyped from related-to on {} shared note(s)",
                    texts.len()
                )),
                "dream-retyped",
                now(),
            )
            .await?;
            retyped += 1;
        }
        let pid = *pid;
        let t = now();
        crate::txn::run(client, |c| async move {
            c.execute(
                "UPDATE edge_proposals SET status = 'rejected', \
                 decided_by = COALESCE($1, decided_by), \
                 decided_at = COALESCE($2, decided_at) WHERE id = $3 AND status = 'pending'",
                &[&"dream-auto", &t, &pid],
            )
            .await
        })
        .await
        .map_err(internal)?;
        settled += 1;
    }
    let _ = svc;
    Ok(json!({"considered": pending.len(), "retyped": retyped, "settled": settled}))
}
