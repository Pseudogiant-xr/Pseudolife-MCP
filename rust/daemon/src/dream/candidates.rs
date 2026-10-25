//! The Step-C candidate judge the sweep runs after the junk judge
//! (`memory/dream.py` `run_sweep_once` 2513-2530):
//! `service_dream.deep_dream_judge_candidates` (5779-6034), with the
//! candidate scan's generation and pair-local evidence fingerprints
//! (`memory/review_judgments.py` `candidate_inputs`,
//! `candidate_generation`, `candidate_current`, 133-197;
//! `memory/review_decisions.py` `candidate_evidence_fingerprints`
//! 248-355), the request and reply parsing (`memory/dream.py`
//! `format_candidate`, `judge_candidates`) and the decisions it applies
//! (`service.py` `_graph_propose_links_locked` 8921-8968 with source
//! `deep-dream-judge`, `_graph_dismiss_duplicate_locked` 8021-8054).
//!
//! The fingerprints are Python's byte for byte: `json.dumps(sort_keys=True,
//! separators=(",", ":"))` over the same values, an int-keyed dict sorted
//! by its integer keys, a set by `str`, and an embedding as
//! `{"shape", "dtype", "bytes"}` (the SHA-256 of its float32 bytes), so a
//! memo, watermark or decision Python wrote stays valid here.

use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::watch;
use tokio_postgres::Client;

use super::extractor::{ExtractFailure, py_str, truthy as py_truthy};
use super::judge::{JudgeEx, STOPPED, check_stop, err_text, judge_extractor, model_name, or_stop};
use super::prompts::CANDIDATE_JUDGE_SYSTEM_PROMPT;
use super::queue_judges::{snips, text_field, verdict_head};
use super::review_judgments::{self as rj, Row, internal, now, vi, vs};
use crate::config::DeepDreamConfig;
use crate::graph_review::token_set;
use crate::graph_review_tools::actions::{
    dismiss_canonicals, dismiss_pair_body, in_txn, propose_body, reads,
};
use crate::mcp::dispatch::ToolError;
use crate::service::Service;
use crate::storage::graph::norm_name;

const VERDICTS_KEY: &str = "deep_candidate_verdicts";
const JUDGED_KEY: &str = "deep_candidates_judged";
const MEMO_CAP: usize = 500;

// ── Python's fingerprint serialization ───────────────────────────────────

/// A value as `fingerprint()` dumps it, where the key order of a dict
/// depends on its key type.
pub(crate) enum Fp {
    /// A JSON value (string-keyed dicts sort by key).
    V(Value),
    /// A string-keyed dict of fingerprinted values.
    Obj(Vec<(String, Fp)>),
    /// An int-keyed dict: sorted by the integer, rendered as its text.
    IntObj(Vec<(i64, Fp)>),
}

fn write_fp(out: &mut String, fp: &Fp) {
    match fp {
        Fp::V(v) => out.push_str(&crate::pyjson::dumps_sorted_compact(v)),
        Fp::Obj(items) => {
            let mut sorted: Vec<&(String, Fp)> = items.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (k, v)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&crate::pyjson::dumps_sorted_compact(&Value::String(
                    k.clone(),
                )));
                out.push(':');
                write_fp(out, v);
            }
            out.push('}');
        }
        Fp::IntObj(items) => {
            let mut sorted: Vec<&(i64, Fp)> = items.iter().collect();
            sorted.sort_by_key(|(k, _)| *k);
            out.push('{');
            for (i, (k, v)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&format!("\"{k}\":"));
                write_fp(out, v);
            }
            out.push('}');
        }
    }
}

/// `fingerprint(value)` over an `Fp`.
pub(crate) fn fp_hash(fp: &Fp) -> String {
    let mut text = String::new();
    write_fp(&mut text, fp);
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// `_fingerprint_default(embedding)`: a float32 array as its shape, dtype
/// and the SHA-256 of its bytes.
pub(crate) fn embedding_fp(e: Option<&[f32]>) -> Value {
    match e {
        None => Value::Null,
        Some(v) => {
            let mut bytes = Vec::with_capacity(v.len() * 4);
            for x in v {
                bytes.extend_from_slice(&x.to_le_bytes());
            }
            json!({"shape": [v.len()], "dtype": "float32",
                   "bytes": hex::encode(Sha256::digest(&bytes))})
        }
    }
}

// ── the inputs ───────────────────────────────────────────────────────────

/// One entry as the candidate evidence reads it.
pub(crate) struct CEntry {
    pub id: i64,
    pub text: Value,
    pub embedding: Option<Vec<f32>>,
}

/// `candidate_inputs(service)`: everything the generation and the evidence
/// fingerprints consume, read once.
pub(crate) struct CandInputs {
    /// `load_graph()["entities"]` rows (id, canonical, display, etype,
    /// created_at).
    pub entities: Vec<Row>,
    /// `load_graph()["aliases"]`, by entity id.
    pub aliases: Vec<(i64, Vec<String>)>,
    /// `load_graph()["edges"]` rows (id, src_id, relation, dst_id,
    /// confidence, origin, asserted_at).
    pub edges: Vec<Row>,
    pub entries: Vec<CEntry>,
    pub scopes: Vec<(i64, Vec<String>)>,
    pub traces: Vec<(String, Vec<i64>)>,
    pub facts: Vec<(i64, i64)>,
    pub fact_texts: Vec<(String, i64)>,
    /// `pending_entity_proposals()` (every kind).
    pub pending_entities: Vec<Row>,
    pub lesson_refs: Vec<i64>,
    /// `pending_proposals()`.
    pub pending_links: Vec<Row>,
    pub dismissed: Vec<(String, String)>,
}

fn opt_text(v: Option<String>) -> Value {
    v.map_or(Value::Null, Value::String)
}

/// Caller holds the lock.
pub(crate) async fn load_inputs(c: &Client) -> Result<CandInputs, ToolError> {
    let q = |sql: &'static str| async move { c.query(sql, &[]).await.map_err(internal) };
    let entities = q("SELECT id, canonical, display, etype, created_at FROM entities ORDER BY id")
        .await?
        .iter()
        .map(|r| {
            let mut m = Map::new();
            m.insert("id".into(), json!(r.get::<_, i64>(0)));
            m.insert("canonical".into(), opt_text(r.get(1)));
            m.insert("display".into(), opt_text(r.get(2)));
            m.insert("etype".into(), opt_text(r.get(3)));
            m.insert("created_at".into(), rj::fnum(r.get(4)));
            m
        })
        .collect();
    // By entity id (the fingerprint sorts the ids), each list in alias order.
    let mut by_entity: HashMap<i64, Vec<String>> = HashMap::new();
    for r in q("SELECT alias, entity_id FROM entity_aliases ORDER BY alias").await? {
        let (alias, eid): (String, i64) = (r.get(0), r.get(1));
        by_entity.entry(eid).or_default().push(alias);
    }
    let aliases: Vec<(i64, Vec<String>)> = by_entity.into_iter().collect();
    let edges = q(
        "SELECT id, src_id, relation, dst_id, confidence::text::double precision, \
                   origin, asserted_at FROM edges WHERE superseded_at IS NULL ORDER BY id",
    )
    .await?
    .iter()
    .map(|r| {
        let mut m = Map::new();
        m.insert("id".into(), json!(r.get::<_, i64>(0)));
        m.insert("src_id".into(), json!(r.get::<_, i64>(1)));
        m.insert("relation".into(), opt_text(r.get(2)));
        m.insert("dst_id".into(), json!(r.get::<_, i64>(3)));
        m.insert("confidence".into(), rj::fnum(r.get(4)));
        m.insert("origin".into(), opt_text(r.get(5)));
        m.insert("asserted_at".into(), rj::fnum(r.get(6)));
        m
    })
    .collect();
    let entries = q("SELECT id, text, embedding::real[] FROM entries ORDER BY id")
        .await?
        .iter()
        .map(|r| CEntry {
            id: r.get(0),
            text: opt_text(r.get(1)),
            embedding: r.get(2),
        })
        .collect();
    let mut scopes: Vec<(i64, Vec<String>)> = Vec::new();
    for r in q("SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source").await? {
        let (eid, source): (i64, String) = (r.get(0), r.get(1));
        match scopes.last_mut() {
            Some((id, list)) if *id == eid => list.push(source),
            _ => scopes.push((eid, vec![source])),
        }
    }
    let mut traces: Vec<(String, Vec<i64>)> = Vec::new();
    for r in
        q("SELECT entity_norm, entry_id FROM memory_traces ORDER BY entity_norm, entry_id").await?
    {
        let (norm, entry): (String, i64) = (r.get(0), r.get(1));
        match traces.last_mut() {
            Some((n, list)) if *n == norm => list.push(entry),
            _ => traces.push((norm, vec![entry])),
        }
    }
    let facts = q("SELECT entity_id, COUNT(*) FROM facts \
                   WHERE status = 'current' AND entity_id IS NOT NULL GROUP BY entity_id")
    .await?
    .iter()
    .map(|r| (r.get(0), r.get(1)))
    .collect();
    let fact_texts = q(
        "SELECT entity, COUNT(*) FROM facts WHERE status = 'current' \
                        GROUP BY entity",
    )
    .await?
    .iter()
    .map(|r| {
        let ent: Option<String> = r.get(0);
        (ent.unwrap_or_else(|| "None".into()), r.get(1))
    })
    .collect();
    let refs: BTreeSet<i64> = q("SELECT entity_id FROM lessons WHERE entity_id IS NOT NULL \
                                 UNION SELECT object_entity_id FROM lessons \
                                 WHERE object_entity_id IS NOT NULL")
    .await?
    .iter()
    .map(|r| r.get(0))
    .collect();
    let mut lesson_refs: Vec<i64> = refs.into_iter().collect();
    lesson_refs.sort_by_key(|id| id.to_string());
    let mut dismissed: Vec<(String, String)> = q("SELECT a_norm, b_norm FROM dismissed_pairs")
        .await?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    dismissed.sort();
    dismissed.dedup();
    Ok(CandInputs {
        entities,
        aliases,
        edges,
        entries,
        scopes,
        traces,
        facts,
        fact_texts,
        pending_entities: rj::pending_entity_rows(c).await?,
        lesson_refs,
        pending_links: rj::pending_rows(c, rj::Queue::Link).await?,
        dismissed,
    })
}

fn entry_value(e: &CEntry) -> Value {
    json!({"id": e.id, "text": e.text, "embedding": embedding_fp(e.embedding.as_deref())})
}

fn str_map<T: Clone + Into<Value>>(items: &[(String, T)]) -> Value {
    let mut m = Map::new();
    for (k, v) in items {
        m.insert(k.clone(), v.clone().into());
    }
    Value::Object(m)
}

/// `candidate_generation(service, decision_inputs=..., inputs=...)`: pure.
pub(crate) fn generation(
    inputs: &CandInputs,
    cfg: &DeepDreamConfig,
    decision_inputs: bool,
) -> String {
    let graph = Fp::Obj(vec![
        (
            "entities".into(),
            Fp::V(Value::Array(
                inputs.entities.iter().cloned().map(Value::Object).collect(),
            )),
        ),
        (
            "aliases".into(),
            Fp::IntObj(
                inputs
                    .aliases
                    .iter()
                    .map(|(id, a)| (*id, Fp::V(json!(a))))
                    .collect(),
            ),
        ),
        (
            "edges".into(),
            Fp::V(Value::Array(
                inputs.edges.iter().cloned().map(Value::Object).collect(),
            )),
        ),
    ]);
    let traces: Vec<(String, Value)> = inputs
        .traces
        .iter()
        .map(|(k, v)| (k.clone(), json!(v)))
        .collect();
    let mut evidence = vec![
        ("graph".to_string(), graph),
        (
            "entries".into(),
            Fp::V(Value::Array(
                inputs.entries.iter().map(entry_value).collect(),
            )),
        ),
        (
            "scopes".into(),
            Fp::IntObj(
                inputs
                    .scopes
                    .iter()
                    .map(|(id, s)| (*id, Fp::V(json!(s))))
                    .collect(),
            ),
        ),
        ("traces".into(), Fp::V(str_map(&traces))),
        (
            "facts".into(),
            Fp::IntObj(
                inputs
                    .facts
                    .iter()
                    .map(|(id, n)| (*id, Fp::V(json!(n))))
                    .collect(),
            ),
        ),
        ("fact_texts".into(), Fp::V(str_map(&inputs.fact_texts))),
        (
            "pending_entities".into(),
            Fp::V(Value::Array(
                inputs
                    .pending_entities
                    .iter()
                    .map(|p| rj::evidence(&Value::Object(p.clone())))
                    .collect(),
            )),
        ),
        ("lesson_refs".into(), Fp::V(json!(inputs.lesson_refs))),
        (
            "config".into(),
            Fp::V(Value::Object(rj::signed_deep_dream(cfg))),
        ),
    ];
    if decision_inputs {
        evidence.push((
            "pending_links".into(),
            Fp::V(Value::Array(
                inputs
                    .pending_links
                    .iter()
                    .map(|p| rj::evidence(&Value::Object(p.clone())))
                    .collect(),
            )),
        ));
        evidence.push((
            "dismissed".into(),
            Fp::V(json!(
                inputs
                    .dismissed
                    .iter()
                    .map(|(a, b)| json!([a, b]))
                    .collect::<Vec<_>>()
            )),
        ));
    }
    fp_hash(&Fp::Obj(evidence))
}

/// `candidate_evidence_fingerprints(service, candidates, inputs=...)`:
/// pure; no candidates, no work.
pub(crate) fn evidence_fingerprints(inputs: &CandInputs, candidates: &[Row]) -> Vec<String> {
    if candidates.is_empty() {
        return Vec::new();
    }
    let by_id: HashMap<i64, &Row> = inputs
        .entities
        .iter()
        .map(|e| (vi(e, "id").unwrap_or(0), e))
        .collect();
    let mut by_norm: HashMap<String, &Row> = HashMap::new();
    for e in &inputs.entities {
        by_norm
            .entry(vs(e, "canonical").unwrap_or("").to_string())
            .or_insert(e);
        by_norm
            .entry(norm_name(vs(e, "display").unwrap_or("")))
            .or_insert(e);
    }
    let entry_by_id: HashMap<i64, &CEntry> = inputs.entries.iter().map(|e| (e.id, e)).collect();
    let entry_tokens: Vec<(i64, BTreeSet<String>)> = inputs
        .entries
        .iter()
        .map(|e| (e.id, token_set(e.text.as_str().unwrap_or(""))))
        .collect();
    let canonical = |id: Option<i64>| -> Value {
        match id.and_then(|i| by_id.get(&i)) {
            Some(e) => e.get("canonical").cloned().unwrap_or(Value::Null),
            None => json!({"missing_id": id}),
        }
    };
    let mut edge_pending: HashMap<i64, Vec<Value>> =
        by_id.keys().map(|id| (*id, Vec::new())).collect();
    for row in &inputs.pending_links {
        let g = |k: &str| row.get(k).cloned().unwrap_or(Value::Null);
        let projection = json!({
            "id": g("id"), "src": canonical(vi(row, "src_id")), "relation": g("relation"),
            "dst": canonical(vi(row, "dst_id")), "confidence": g("confidence"),
            "similarity": g("similarity"), "rationale": g("rationale"),
            "source": g("source"), "created_at": g("created_at"), "status": g("status"),
        });
        let ends: BTreeSet<Option<i64>> = [vi(row, "src_id"), vi(row, "dst_id")].into();
        for id in ends.into_iter().flatten() {
            if let Some(list) = edge_pending.get_mut(&id) {
                list.push(projection.clone());
            }
        }
    }
    let mut entity_pending: HashMap<i64, Vec<Value>> =
        by_id.keys().map(|id| (*id, Vec::new())).collect();
    for row in &inputs.pending_entities {
        let g = |k: &str| row.get(k).cloned().unwrap_or(Value::Null);
        let into = vi(row, "into_id").filter(|i| *i != 0);
        let projection = json!({
            "id": g("id"), "kind": g("kind"), "entity": canonical(vi(row, "entity_id")),
            "into": into.map_or(Value::Null, |i| canonical(Some(i))),
            "score": g("score"), "reason": g("reason"),
            "created_at": g("created_at"), "status": g("status"),
        });
        let ends: BTreeSet<Option<i64>> = [vi(row, "entity_id"), vi(row, "into_id")].into();
        for id in ends.into_iter().flatten() {
            if let Some(list) = entity_pending.get_mut(&id) {
                list.push(projection.clone());
            }
        }
    }
    let traces: HashMap<&str, &Vec<i64>> =
        inputs.traces.iter().map(|(k, v)| (k.as_str(), v)).collect();
    let scopes: HashMap<i64, &Vec<String>> = inputs.scopes.iter().map(|(k, v)| (*k, v)).collect();
    let facts: HashMap<i64, i64> = inputs.facts.iter().copied().collect();
    let resolve = |cand: &Row, field: &str| -> Option<&Row> {
        if let Some(e) = vi(cand, &format!("{field}_id")).and_then(|i| by_id.get(&i)) {
            return Some(e);
        }
        let name = match cand.get(field) {
            Some(Value::String(s)) => s.clone(),
            Some(v) => py_str(v),
            None => String::new(),
        };
        by_norm.get(&norm_name(&name)).copied()
    };
    let endpoint = |entity: Option<&Row>| -> Value {
        let Some(entity) = entity else {
            return json!({"missing": true});
        };
        let id = vi(entity, "id").unwrap_or(0);
        let trace_ids: Vec<i64> = traces
            .get(vs(entity, "canonical").unwrap_or(""))
            .map(|v| v.to_vec())
            .unwrap_or_default();
        let (mention_ids, source): (Vec<i64>, &str) = if trace_ids.is_empty() {
            let want = token_set(vs(entity, "display").unwrap_or(""));
            (
                entry_tokens
                    .iter()
                    .filter(|(_, toks)| !want.is_empty() && want.is_subset(toks))
                    .map(|(id, _)| *id)
                    .collect(),
                "fallback",
            )
        } else {
            (trace_ids.clone(), "traces")
        };
        let unique: BTreeSet<i64> = mention_ids.into_iter().collect();
        let mentions: Vec<Value> = unique
            .iter()
            .filter_map(|id| entry_by_id.get(id))
            .map(|e| entry_value(e))
            .collect();
        let mut incident: Vec<&Row> = inputs
            .edges
            .iter()
            .filter(|e| vi(e, "src_id") == Some(id) || vi(e, "dst_id") == Some(id))
            .collect();
        incident.sort_by(|a, b| {
            (
                vi(a, "id").unwrap_or(0),
                vi(a, "src_id"),
                vs(a, "relation"),
                vi(a, "dst_id"),
            )
                .cmp(&(
                    vi(b, "id").unwrap_or(0),
                    vi(b, "src_id"),
                    vs(b, "relation"),
                    vi(b, "dst_id"),
                ))
        });
        let mut entity_scopes: Vec<String> =
            scopes.get(&id).map(|s| s.to_vec()).unwrap_or_default();
        entity_scopes.sort();
        let trace_set: BTreeSet<i64> = trace_ids.into_iter().collect();
        json!({
            "entity": Value::Object(entity.clone()),
            "incident_edges": incident.into_iter().map(|e| Value::Object(e.clone())).collect::<Vec<_>>(),
            "scopes": entity_scopes,
            "trace_ids": trace_set.into_iter().collect::<Vec<_>>(),
            "fact_count": facts.get(&id).copied().unwrap_or(0),
            "mention_source": source,
            "mentions": mentions,
            "pending_edges": edge_pending.get(&id).cloned().unwrap_or_default(),
            "pending_entities": entity_pending.get(&id).cloned().unwrap_or_default(),
        })
    };
    candidates
        .iter()
        .map(|cand| {
            rj::fingerprint(&json!({
                "src": endpoint(resolve(cand, "src")),
                "dst": endpoint(resolve(cand, "dst")),
            }))
        })
        .collect()
}

/// `candidate_current(service, candidate)`: the action-lock guard, over
/// live reads in the caller's transaction.
pub(crate) async fn candidate_current(c: &Client, cand: &Row) -> Result<bool, ToolError> {
    let entities: Vec<(i64, String, String)> = c
        .query(
            "SELECT id, canonical, display FROM entities ORDER BY id",
            &[],
        )
        .await
        .map_err(internal)?
        .iter()
        .map(|r| {
            (
                r.get(0),
                r.get::<_, Option<String>>(1).unwrap_or_default(),
                r.get::<_, Option<String>>(2).unwrap_or_default(),
            )
        })
        .collect();
    let mut resolved: Vec<&(i64, String, String)> = Vec::new();
    for field in ["src", "dst"] {
        let eid = cand
            .get(&format!("{field}_id"))
            .filter(|v| !v.is_null())
            .and_then(Value::as_i64);
        let name = norm_name(vs(cand, field).unwrap_or(""));
        let matches: Vec<&(i64, String, String)> = entities
            .iter()
            .filter(|(id, canon, disp)| match eid {
                Some(e) => *id == e,
                None => name == *canon || name == norm_name(disp),
            })
            .collect();
        if matches.len() != 1 {
            return Ok(false);
        }
        resolved.push(matches[0]);
    }
    let ids: HashSet<i64> = resolved.iter().map(|e| e.0).collect();
    if ids.len() != 2 {
        return Ok(false);
    }
    let linked = c
        .query(
            "SELECT src_id, dst_id FROM edge_proposals WHERE status = 'pending' \
             UNION ALL SELECT src_id, dst_id FROM edges WHERE superseded_at IS NULL",
            &[],
        )
        .await
        .map_err(internal)?
        .iter()
        .any(|r| {
            [r.get::<_, i64>(0), r.get::<_, i64>(1)]
                .into_iter()
                .collect::<HashSet<i64>>()
                == ids
        });
    if linked {
        return Ok(false);
    }
    let mut pair = [resolved[0].1.clone(), resolved[1].1.clone()];
    pair.sort();
    let dismissed = c
        .query_opt(
            "SELECT 1 FROM dismissed_pairs WHERE a_norm = $1 AND b_norm = $2",
            &[&pair[0], &pair[1]],
        )
        .await
        .map_err(internal)?
        .is_some();
    Ok(!dismissed)
}

// ── the request and the reply ────────────────────────────────────────────

/// `format_candidate(p)`.
pub(crate) fn format_candidate(p: &Row) -> String {
    let get = |k: &str| p.get(k).map_or_else(|| "?".to_string(), py_str);
    let none_if_empty = |s: String| if s.is_empty() { "none".to_string() } else { s };
    format!(
        "[{}] '{}' vs '{}' (cosine {})\n    src notes: {}\n    dst notes: {}",
        p.get("n").map_or_else(String::new, py_str),
        get("src"),
        get("dst"),
        p.get("similarity")
            .map_or_else(|| "None".to_string(), py_str),
        none_if_empty(snips(p.get("src_snippets"), 3, 240)),
        none_if_empty(snips(p.get("dst_snippets"), 3, 240)),
    )
}

/// `judge_candidates`' validation loop, as Python's dicts (`n`, verdict,
/// confidence, relation, src, dst, rationale): a propose naming no
/// relation degrades to a leave; src and dst default to the pair's own.
pub(crate) fn parse_candidate_verdicts(raw: &[Value], rows: &[Row]) -> Result<Vec<Row>, String> {
    let by_n: HashMap<usize, &Row> = rows
        .iter()
        .filter_map(|r| r.get("n").and_then(Value::as_u64).map(|n| (n as usize, r)))
        .collect();
    let known: HashSet<usize> = by_n.keys().copied().collect();
    let mut out = Vec::new();
    for v in raw {
        let Some((n, mut verdict, confidence)) =
            verdict_head(v, &known, &["propose", "dismiss", "leave"])?
        else {
            continue;
        };
        let (mut relation, mut src, mut dst) = (Value::Null, Value::Null, Value::Null);
        if verdict == "propose" {
            let row_field = |k: &str| by_n[&n].get(k).cloned().unwrap_or(Value::Null);
            relation = text_field(v, "relation", 64).map_or(Value::Null, Value::String);
            src = text_field(v, "src", 200).map_or_else(|| row_field("src"), Value::String);
            dst = text_field(v, "dst", 200).map_or_else(|| row_field("dst"), Value::String);
            if relation.is_null() {
                verdict = "leave".into();
                src = Value::Null;
                dst = Value::Null;
            }
        }
        let mut m = Map::new();
        m.insert("n".into(), json!(n));
        m.insert("verdict".into(), json!(verdict));
        m.insert("confidence".into(), rj::fnum(Some(confidence)));
        m.insert("relation".into(), relation);
        m.insert("src".into(), src);
        m.insert("dst".into(), dst);
        m.insert(
            "rationale".into(),
            text_field(v, "rationale", 400).map_or(Value::Null, Value::String),
        );
        out.push(m);
    }
    Ok(out)
}

/// `ex.judge_candidates(rows)`.
async fn judge_candidates(ex: &mut JudgeEx, rows: &[Row]) -> Result<Vec<Row>, String> {
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let user: Vec<String> = rows.iter().map(format_candidate).collect();
    let (served, result) = ex
        .compat
        .judge_request(
            CANDIDATE_JUDGE_SYSTEM_PROMPT,
            &user.join("\n\n"),
            rows.len(),
            "judge_candidates",
        )
        .await;
    if let Some(s) = served {
        ex.served = s;
    }
    let raw = result.map_err(|e| match e {
        ExtractFailure::Extractor(x) => x.message,
        ExtractFailure::Uncaught(u) => u.message,
    })?;
    parse_candidate_verdicts(&raw, rows)
}

/// `_stamp_skipped(verdicts, rows, relation=None, src=None, dst=None,
/// rationale=None)`.
fn stamp_skipped(verdicts: &mut Vec<Row>, rows: &[Row]) {
    let returned: HashSet<u64> = verdicts
        .iter()
        .filter_map(|v| v.get("n").and_then(Value::as_u64))
        .collect();
    for r in rows {
        let n = r.get("n").and_then(Value::as_u64).unwrap_or(0);
        if !returned.contains(&n) {
            let mut m = Map::new();
            m.insert("n".into(), json!(n));
            m.insert("verdict".into(), json!("leave"));
            m.insert("confidence".into(), json!(0.0));
            m.insert("note".into(), json!("model returned no verdict"));
            for k in ["relation", "src", "dst", "rationale"] {
                m.insert(k.into(), Value::Null);
            }
            verdicts.push(m);
        }
    }
}

fn n_of(v: &Row) -> usize {
    v.get("n").and_then(Value::as_u64).unwrap_or(0) as usize
}

/// Python `==` between two JSON values: numbers by value.
fn json_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| json_eq(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.get(k).is_some_and(|w| json_eq(v, w)))
        }
        _ => a == b,
    }
}

// ── the tick ─────────────────────────────────────────────────────────────

fn zero(key: &str, v: Value) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("judged".into(), json!(0));
    m.insert(key.into(), v);
    m
}

/// `deep_dream_judge_candidates()`: never fails into the sweep.
pub(crate) async fn deep_dream_judge_candidates(
    service: &Service,
    stop: &watch::Receiver<bool>,
) -> Map<String, Value> {
    let cfg = &service.config.memory.deep_dream;
    if !cfg.judges_enabled {
        return zero("skipped", json!("judges_disabled"));
    }
    if !matches!(cfg.candidate_judge_mode.as_str(), "shadow" | "auto") {
        return zero("skipped", json!("disabled"));
    }
    let result = candidate_tick(service, stop).await;
    match result {
        Ok(out) => out,
        Err(e) if e == STOPPED => zero("skipped", json!("stopping")),
        Err(e) => {
            eprintln!("deep-dream candidate judge failed: {e}");
            zero("error", json!(e))
        }
    }
}

/// `key(c)`: the pair's sorted graph norms.
fn pair_key(c: &Row) -> String {
    let mut k = [
        norm_name(vs(c, "src").unwrap_or("")),
        norm_name(vs(c, "dst").unwrap_or("")),
    ];
    k.sort();
    k.join("|")
}

fn reconsideration(out: &mut Map<String, Value>, r: &Value) {
    out.insert("reconsideration".into(), r.clone());
}

fn nonempty(v: Option<&str>) -> Option<&str> {
    v.filter(|s| !s.is_empty())
}

/// The candidate judge's `try` body.
#[allow(clippy::too_many_lines)]
async fn candidate_tick(
    service: &Service,
    stop: &watch::Receiver<bool>,
) -> Result<Map<String, Value>, String> {
    let cfg = &service.config.memory.deep_dream;
    let mode = cfg.candidate_judge_mode.clone();
    let e2s = |e: ToolError| err_text(&e);
    let Some(mut ex) = or_stop(stop, judge_extractor(service, None)).await? else {
        return Ok(zero("skipped", json!("no_judge_extractor")));
    };
    let mut current_model =
        or_stop(stop, rj::observed_model(&ex.compat, ex.served.as_deref())).await?;
    let policy = rj::fingerprint(&rj::judging_policy(
        &service.config,
        &ex.compat,
        CANDIDATE_JUDGE_SYSTEM_PROMPT,
    ));
    let cap = cfg.judge_batch.max(1) as usize;
    let ready = service.ensure_init_inner().await?;
    let client = ready.storage.client();
    let (last_model, reconsidered, inputs) = {
        let _lock = ready.write.lock().await;
        let pol = &policy;
        let last_model = reads(client, |c| async move {
            rj::last_response_identity(c, "candidate", pol).await
        })
        .await
        .map_err(e2s)?;
        let reconsidered = rj::refresh_candidate_dismissals(
            client,
            &policy,
            nonempty(current_model.as_deref()),
            nonempty(last_model.as_deref()),
            cap as i64,
        )
        .await
        .map_err(e2s)?;
        let inputs = reads(client, |c| async move { load_inputs(c).await })
            .await
            .map_err(e2s)?;
        (last_model, reconsidered, inputs)
    };
    let mut memo_model = nonempty(current_model.as_deref())
        .or(last_model.as_deref())
        .map(str::to_string);
    let decision_generation = generation(&inputs, cfg, false);
    let gen_full = generation(&inputs, cfg, true);
    drop(inputs);
    let models_match = |saved: Option<&Value>, models: &[Option<&str>]| {
        models
            .iter()
            .all(|m| m.is_none_or(str::is_empty) || saved.and_then(Value::as_str) == *m)
    };
    let (mark, done) = {
        let _lock = ready.write.lock().await;
        reads(client, |c| async move {
            Ok((
                rj::get_meta(c, "deep_last_apply").await?,
                rj::get_meta(c, JUDGED_KEY).await?,
            ))
        })
        .await
        .map_err(e2s)?
    };
    let mark = mark.filter(|m| !m.is_null());
    let Some(mark) = mark else {
        return Ok(no_new_apply(&reconsidered));
    };
    if let Some(Value::Object(done)) = &done
        && !done.is_empty()
        && done
            .get("ts")
            .is_some_and(|t| json_eq(t, mark.get("ts").unwrap_or(&Value::Null)))
        && done.get("complete").is_some_and(py_truthy)
        && done.get("policy").and_then(Value::as_str) == Some(policy.as_str())
        && models_match(
            done.get("served_model"),
            &[current_model.as_deref(), last_model.as_deref()],
        )
        && done.get("generation").and_then(Value::as_str) == Some(gen_full.as_str())
    {
        return Ok(no_new_apply(&reconsidered));
    }
    check_stop(stop)?;
    let scan = super::deep::deep_dream(service, &ready, false, true)
        .await
        .map_err(e2s)?;
    let candidates: Vec<Row> = scan
        .get("candidates")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|c| c.as_object().cloned()).collect())
        .unwrap_or_default();
    let pairs: Map<String, Value> = {
        let _lock = ready.write.lock().await;
        reads(
            client,
            |c| async move { rj::get_meta(c, VERDICTS_KEY).await },
        )
        .await
        .map_err(e2s)?
        .and_then(|m| m.get("pairs").and_then(Value::as_object).cloned())
        .unwrap_or_default()
    };
    let read_keys: HashSet<String> = pairs.keys().cloned().collect();
    let at = now();
    let horizon = cfg.candidate_rejudge_days * 86400.0;
    let (source_evidence, gen_now) = {
        let _lock = ready.write.lock().await;
        let inputs = reads(client, |c| async move { load_inputs(c).await })
            .await
            .map_err(e2s)?;
        let gen_now = generation(&inputs, cfg, true);
        let fps = evidence_fingerprints(&inputs, &candidates);
        let map: HashMap<String, String> = candidates.iter().map(pair_key).zip(fps).collect();
        (map, gen_now)
    };
    if gen_now != gen_full {
        return Ok(zero("reason", json!("stale_evidence")));
    }
    let signature = |c: &Row| {
        rj::fingerprint(
            &json!({"candidate": Value::Object(c.clone()), "policy": policy,
                                "evidence": source_evidence.get(&pair_key(c))}),
        )
    };
    let fresh = |c: &Row, current: Option<&str>| -> bool {
        let Some(Value::Object(p)) = pairs.get(&pair_key(c)) else {
            return false;
        };
        let ts = p.get("ts").and_then(Value::as_f64).unwrap_or(0.0);
        if !((at - ts) < horizon
            && p.get("fingerprint").and_then(Value::as_str) == Some(signature(c).as_str()))
        {
            return false;
        }
        let models = [current, last_model.as_deref()];
        if p.get("complete").is_some_and(py_truthy) {
            let memo = if p.contains_key("memo_model") {
                p.get("memo_model")
            } else {
                p.get("served_model")
            };
            models_match(memo, &models)
        } else {
            current.is_some_and(|m| !m.is_empty()) && models_match(p.get("served_model"), &models)
        }
    };
    let complete_in = |p: &Map<String, Value>, c: &Row| {
        p.get(&pair_key(c))
            .and_then(|e| e.get("complete"))
            .is_some_and(py_truthy)
    };
    let todo: Vec<Row> = candidates
        .iter()
        .filter(|c| !fresh(c, current_model.as_deref()) || !complete_in(&pairs, c))
        .cloned()
        .collect();
    if todo.is_empty() {
        let _lock = ready.write.lock().await;
        let (cands, pol, genfp, memo, mark) = (&candidates, &policy, &gen_full, &memo_model, &mark);
        in_txn(client, |c| async move {
            let live = rj::get_meta(c, VERDICTS_KEY)
                .await?
                .and_then(|m| m.get("pairs").and_then(Value::as_object).cloned())
                .unwrap_or_default();
            if cands.iter().all(|cand| live.contains_key(&pair_key(cand))) {
                rj::set_meta(
                    c,
                    JUDGED_KEY,
                    &json!({"ts": mark.get("ts"), "complete": true, "policy": pol,
                            "generation": genfp, "served_model": memo}),
                )
                .await?;
            }
            Ok(())
        })
        .await
        .map_err(e2s)?;
        let mut m = zero("reason", json!("all_judged"));
        reconsideration(&mut m, &reconsidered);
        return Ok(m);
    }
    let chunk: Vec<Row> = todo.iter().take(cap).cloned().collect();
    eprintln!(
        "deep-dream candidate judge: judging {} of {} unjudged link candidate(s) (mode {mode})",
        chunk.len(),
        todo.len()
    );
    let rows: Vec<Row> = chunk
        .iter()
        .enumerate()
        .map(|(i, c)| {
            let list = |k: &str| match c.get(k) {
                Some(v) if py_truthy(v) => v.clone(),
                _ => json!([]),
            };
            let mut m = Map::new();
            m.insert("n".into(), json!(i + 1));
            m.insert("src".into(), c.get("src").cloned().unwrap_or(Value::Null));
            m.insert("dst".into(), c.get("dst").cloned().unwrap_or(Value::Null));
            m.insert(
                "similarity".into(),
                c.get("similarity").cloned().unwrap_or(Value::Null),
            );
            m.insert("src_snippets".into(), list("src_snippets"));
            m.insert("dst_snippets".into(), list("dst_snippets"));
            m
        })
        .collect();
    let mut new_rows: Vec<Row> = Vec::new();
    let mut new_indices: Vec<usize> = Vec::new();
    let mut verdicts: Vec<Row> = Vec::new();
    let mut replayed: HashMap<usize, Value> = HashMap::new();
    for (row, cand) in rows.iter().zip(&chunk) {
        let saved = pairs.get(&pair_key(cand)).cloned().unwrap_or(json!({}));
        let reply = saved.get("reply").filter(|r| py_truthy(r)).cloned();
        let n = n_of(row);
        match reply {
            Some(Value::Object(reply))
                if fresh(cand, current_model.as_deref())
                    && current_model.as_deref().is_some_and(|m| !m.is_empty()) =>
            {
                replayed.insert(n, saved.clone());
                let mut v = reply.clone();
                v.insert("n".into(), json!(n));
                verdicts.push(v);
            }
            _ => {
                new_indices.push(n);
                let mut r = row.clone();
                r.insert("n".into(), json!(new_rows.len() + 1));
                new_rows.push(r);
            }
        }
    }
    if !new_rows.is_empty() {
        check_stop(stop)?;
        let mut new_verdicts = or_stop(stop, judge_candidates(&mut ex, &new_rows)).await??;
        let response_model = ex.served.clone();
        {
            let _lock = ready.write.lock().await;
            let (pol, rm) = (&policy, response_model.as_deref());
            in_txn(client, |c| async move {
                rj::record_response_identity(c, "candidate", pol, rm).await
            })
            .await
            .map_err(e2s)?;
        }
        if current_model.as_deref().is_some_and(|m| !m.is_empty())
            && response_model != current_model
        {
            let mut m = zero("reason", json!("changed_judge_identity"));
            reconsideration(&mut m, &reconsidered);
            return Ok(m);
        }
        current_model = response_model.clone();
        if response_model.as_deref().is_some_and(|m| !m.is_empty()) {
            memo_model = response_model;
        }
        stamp_skipped(&mut new_verdicts, &new_rows);
        for mut v in new_verdicts {
            let n = new_indices
                .get(n_of(&v).wrapping_sub(1))
                .copied()
                .unwrap_or(0);
            v.insert("n".into(), json!(n));
            verdicts.push(v);
        }
    }
    stamp_skipped(&mut verdicts, &rows);
    let model = model_name(&ex, None);
    let current = current_model.clone().filter(|m| !m.is_empty());
    let _lock = ready.write.lock().await;
    let now_policy = rj::fingerprint(&rj::judging_policy(
        &service.config,
        &ex.compat,
        CANDIDATE_JUDGE_SYSTEM_PROMPT,
    ));
    if now_policy != policy {
        return Ok(zero("reason", json!("stale_evidence")));
    }
    let ch = &chunk;
    let (mut live, decision_evidence, entity_ids) = reads(client, |c| async move {
        let live = rj::get_meta(c, VERDICTS_KEY)
            .await?
            .and_then(|m| m.get("pairs").and_then(Value::as_object).cloned())
            .unwrap_or_default();
        let inputs = load_inputs(c).await?;
        let fps = evidence_fingerprints(&inputs, ch);
        let mut ids: HashMap<String, i64> = HashMap::new();
        for e in &inputs.entities {
            let id = vi(e, "id").unwrap_or(0);
            ids.entry(vs(e, "canonical").unwrap_or("").to_string())
                .or_insert(id);
            ids.entry(norm_name(vs(e, "display").unwrap_or("")))
                .or_insert(id);
        }
        Ok((live, fps, ids))
    })
    .await
    .map_err(e2s)?;
    let forgotten: HashSet<String> = read_keys
        .iter()
        .filter(|k| !live.contains_key(*k))
        .cloned()
        .collect();
    let mut changed_entities: HashSet<Option<i64>> = HashSet::new();
    let (mut judged, mut proposed, mut dismissed, mut left) = (0i64, 0i64, 0i64, 0i64);
    for v in &verdicts {
        let n = n_of(v);
        let Some(c) = chunk.get(n.wrapping_sub(1)) else {
            continue;
        };
        let key = pair_key(c);
        if let Some(saved) = replayed.get(&n)
            && !live.get(&key).is_some_and(|l| json_eq(l, saved))
        {
            continue;
        }
        let endpoint = |field: &str| {
            vi(c, &format!("{field}_id"))
                .filter(|i| *i != 0)
                .or_else(|| {
                    entity_ids
                        .get(&norm_name(vs(c, field).unwrap_or("")))
                        .copied()
                })
        };
        let endpoints: HashSet<Option<i64>> = [endpoint("src"), endpoint("dst")].into();
        if source_evidence.get(&key) != decision_evidence.get(n - 1)
            || !endpoints.is_disjoint(&changed_entities)
        {
            live.shift_remove(&key);
            continue;
        }
        judged += 1;
        let conf = v.get("confidence").and_then(Value::as_f64).unwrap_or(0.0);
        let verdict = vs(v, "verdict").unwrap_or("").to_string();
        let mut entry = Map::new();
        entry.insert("verdict".into(), json!(verdict));
        entry.insert("confidence".into(), rj::fnum(Some(conf)));
        entry.insert("ts".into(), rj::fnum(Some(at)));
        entry.insert("mode".into(), json!(mode));
        entry.insert("fingerprint".into(), json!(signature(c)));
        entry.insert("model".into(), json!(model));
        entry.insert("served_model".into(), json!(current_model));
        entry.insert("memo_model".into(), json!(memo_model));
        entry.insert("reply".into(), Value::Object(v.clone()));
        entry.insert("complete".into(), json!(false));
        live.insert(key.clone(), Value::Object(entry));
        if live.len() > MEMO_CAP {
            live = capped(live);
        }
        write_pairs(client, &live).await.map_err(e2s)?;
        if !live.contains_key(&key) {
            // The cap evicted the row being judged (500 newer memos: a
            // clock stepped back): Python's `pairs[key(c)]` raises KeyError
            // here, before any action.
            return Err(format!("'{key}'"));
        }
        if mode != "auto" || current.is_none() {
            set_complete(&mut live, &key, true);
            write_pairs(client, &live).await.map_err(e2s)?;
            left += 1;
            continue;
        }
        let relation = vs(v, "relation").filter(|r| !r.is_empty());
        let propose =
            verdict == "propose" && relation.is_some() && conf >= cfg.candidate_min_confidence;
        let dismiss = verdict == "dismiss" && conf >= cfg.candidate_min_confidence;
        let pick = |k: &str| -> String {
            match v.get(k) {
                Some(x) if py_truthy(x) => py_str(x),
                _ => vs(c, k).unwrap_or("").to_string(),
            }
        };
        if propose {
            let wanted: HashSet<String> = [norm_name(&pick("src")), norm_name(&pick("dst"))].into();
            let pair: HashSet<String> = [
                norm_name(vs(c, "src").unwrap_or("")),
                norm_name(vs(c, "dst").unwrap_or("")),
            ]
            .into();
            if wanted != pair {
                // Python `continue`s inside its transaction: this verdict's
                // memo write waits for the next one.
                set_complete(&mut live, &key, true);
                left += 1;
                continue;
            }
        }
        let proposal = json!([{
            "src": pick("src"), "relation": relation, "dst": pick("dst"),
            "similarity": c.get("similarity").cloned().unwrap_or(Value::Null),
            "rationale": v.get("rationale").cloned().unwrap_or(Value::Null),
        }]);
        let (sig, pol, dgen, ev) = (
            signature(c),
            policy.as_str(),
            decision_generation.as_str(),
            decision_evidence.get(n - 1).cloned(),
        );
        let served = current.as_deref();
        let snapshot = live.clone();
        let (outcome, after) = in_txn(client, |tx| async move {
            let mut live = snapshot;
            let mut o = (0i64, 0i64, 0i64, false);
            if propose {
                let res = if candidate_current(tx, c).await? {
                    propose_body(
                        tx,
                        proposal.as_array().map(Vec::as_slice).unwrap_or(&[]),
                        "deep-dream-judge",
                        true,
                    )
                    .await?
                } else {
                    json!({"proposed": 0, "reason": "stale_review"})
                };
                o.0 = res.get("proposed").and_then(Value::as_i64).unwrap_or(0);
                o.3 = o.0 > 0;
                let complete = res.get("error").is_none_or(|e| !py_truthy(e))
                    && res.get("reason").and_then(Value::as_str) != Some("stale_review");
                set_complete(&mut live, &key, complete);
            } else if dismiss {
                let ok = dismiss_locked(tx, c).await?;
                o.1 = i64::from(ok);
                set_complete(&mut live, &key, ok);
                if ok && !crate::mutants::active("mcp-dream-candidate-dismiss-unrecorded") {
                    rj::record_candidate_dismissal(
                        tx,
                        c,
                        &sig,
                        pol,
                        Some(dgen),
                        served,
                        ev.as_deref(),
                    )
                    .await?;
                }
            } else {
                o.2 = 1;
                set_complete(&mut live, &key, true);
            }
            write_pairs_body(tx, &live).await?;
            Ok((o, live))
        })
        .await
        .map_err(e2s)?;
        live = after;
        proposed += outcome.0;
        dismissed += outcome.1;
        left += outcome.2;
        if outcome.3 && !crate::mutants::active("mcp-dream-candidate-endpoint-stale") {
            // A new pending relation changes incident evidence for later
            // rows sharing either endpoint.
            changed_entities.extend(endpoints);
        }
    }
    if live.len() > MEMO_CAP {
        live = capped(live);
    }
    let todo_keys: HashSet<String> = todo.iter().map(pair_key).collect();
    let remaining = todo.len().saturating_sub(chunk.len())
        + chunk
            .iter()
            .filter(|c| {
                !live
                    .get(&pair_key(c))
                    .and_then(|e| e.get("complete"))
                    .is_some_and(py_truthy)
            })
            .count()
        + candidates
            .iter()
            .filter(|c| {
                let k = pair_key(c);
                forgotten.contains(&k) && !todo_keys.contains(&k)
            })
            .count();
    {
        let (live, pol, genfp, memo) = (&live, &policy, &gen_full, &memo_model);
        let ts = mark.get("ts").cloned();
        in_txn(client, |c| async move {
            write_pairs_body(c, live).await?;
            rj::set_meta(
                c,
                JUDGED_KEY,
                &json!({"ts": ts, "complete": remaining == 0, "policy": pol,
                        "generation": genfp, "served_model": memo}),
            )
            .await
        })
        .await
        .map_err(e2s)?;
    }
    let mut out = Map::new();
    for (k, v) in [
        ("judged", json!(judged)),
        ("proposed", json!(proposed)),
        ("dismissed", json!(dismissed)),
        ("left", json!(left)),
        ("remaining", json!(remaining)),
        ("model", json!(model)),
        ("mode", json!(mode)),
        ("reconsideration", reconsidered),
    ] {
        out.insert(k.into(), v);
    }
    Ok(out)
}

fn no_new_apply(reconsidered: &Value) -> Map<String, Value> {
    let mut m = zero("reason", json!("no_new_apply"));
    reconsideration(&mut m, reconsidered);
    m
}

fn set_complete(pairs: &mut Map<String, Value>, key: &str, complete: bool) {
    if let Some(Value::Object(e)) = pairs.get_mut(key) {
        e.insert("complete".into(), json!(complete));
    }
}

/// The bounded memo: the newest `MEMO_CAP` pairs (a stable sort by `-ts`).
fn capped(pairs: Map<String, Value>) -> Map<String, Value> {
    let mut items: Vec<(String, Value)> = pairs.into_iter().collect();
    items.sort_by(|a, b| {
        let ts = |v: &Value| v.get("ts").and_then(Value::as_f64).unwrap_or(0.0);
        ts(&b.1)
            .partial_cmp(&ts(&a.1))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    items.truncate(MEMO_CAP);
    items.into_iter().collect()
}

async fn write_pairs_body(c: &Client, pairs: &Map<String, Value>) -> Result<(), ToolError> {
    rj::set_meta(c, VERDICTS_KEY, &json!({"pairs": pairs})).await
}

async fn write_pairs(client: &Client, pairs: &Map<String, Value>) -> Result<(), ToolError> {
    in_txn(client, |c| async move { write_pairs_body(c, pairs).await }).await
}

/// `_graph_dismiss_duplicate_locked(src, dst, _review_guard=...)` in the
/// caller's transaction: the guard is the candidate's own, so no human
/// confirmation; `true` when the pair was dismissed.
async fn dismiss_locked(c: &Client, cand: &Row) -> Result<bool, ToolError> {
    let (an, bn) = (
        norm_name(vs(cand, "src").unwrap_or("")),
        norm_name(vs(cand, "dst").unwrap_or("")),
    );
    if an.is_empty() || bn.is_empty() || an == bn {
        return Ok(false);
    }
    if !candidate_current(c, cand).await? {
        return Ok(false);
    }
    let g = crate::storage::graph::load_graph_body(c)
        .await
        .map_err(internal)?;
    let (an, bn) = dismiss_canonicals(&g, an, bn);
    if an == bn {
        return Ok(false);
    }
    dismiss_pair_body(c, &an, &bn).await?;
    Ok(true)
}

#[cfg(test)]
#[path = "candidates_tests.rs"]
mod tests;
