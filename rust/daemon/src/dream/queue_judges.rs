//! The review-queue judges the sweep runs after the merge judge
//! (`memory/dream.py` `run_sweep_once` 2513-2530): here the junk judge,
//! `service_dream.deep_dream_judge_junk` (5329-5422) with its evidence pack
//! (`_junk_evidence_locked` / `_enrich_junk_proposals_from`, 5244-5327),
//! the request and reply parsing (`memory/dream.py` `format_junk_proposal`
//! 592-602, `judge_junk` 1905-1923, `_verdict_head` / `_text` 1825-1846)
//! and the dream-judge decisions it applies (`service.py`
//! `_graph_accept_entity_junk_locked` 9118-9144 and the junk branch of
//! `_graph_reject_entity_proposal_locked` 9151-9195).
//!
//! The link, curation and candidate judges join this module in their own
//! changes (task P3b); until then the sweep reports them as not run.
//!
//! Lock windows follow Python's, as the merge judge's do: the model call
//! runs with the writer lock released, the reads and writes under it, and
//! each verdict's writes are one transaction.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde_json::{Map, Value, json};
use tokio::sync::watch;
use tokio_postgres::Client;

use super::extractor::{self, ExtractFailure, py_str, truthy as py_truthy};
use super::judge::{
    JudgeEx, STOPPED, err_text, judge_extractor, model_name, or_stop, reject_locked,
    set_first_judgment,
};
use super::prompts::JUNK_JUDGE_SYSTEM_PROMPT;
use super::review_judgments::{self as rj, Queue, Review, Row, id_of, internal, now, vi, vs};
use crate::config::DeepDreamConfig;
use crate::graph_consolidation::py_slice_chars;
use crate::graph_review::token_set;
use crate::graph_review_tools::actions::{in_txn, reads};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::storage::graph::norm_name;

// ── the reply ────────────────────────────────────────────────────────────

/// `_verdict_head(v, known, allowed)`: `(n, verdict, confidence)` of a
/// well-formed row whose id the batch knows and whose verdict is allowed.
/// `Err` is the exception Python does not catch there (`int()` of an
/// infinity, `float()` of a huge integer).
pub(crate) fn verdict_head(
    v: &Value,
    known: &HashSet<usize>,
    allowed: &[&str],
) -> Result<Option<(usize, String, f64)>, String> {
    let Value::Object(v) = v else {
        return Ok(None);
    };
    let n = match extractor::py_int(v.get("id").unwrap_or(&Value::Null)).map_err(|u| u.message)? {
        Some(n) => n,
        None => return Ok(None),
    };
    let verdict = crate::storage::graph::py_lower(&extractor::field(v, "verdict"));
    let n = match usize::try_from(n) {
        Ok(n) if known.contains(&n) => n,
        _ => return Ok(None),
    };
    if !allowed.contains(&verdict.as_str()) {
        return Ok(None);
    }
    let confidence = extractor::confidence(v, 0.5).map_err(|u| u.message)?;
    Ok(Some((n, verdict, confidence)))
}

/// `_text(v, key, cap=400)`: `str(val)[:cap]` unless it is None or "".
pub(crate) fn text_field(v: &Value, key: &str, cap: usize) -> Option<String> {
    match v.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(val) => Some(py_str(val).chars().take(cap).collect()),
    }
}

/// `_snips(items, k, chars)`: `"; ".join(str(x)[:chars] for x in items[:k])`.
fn snips(items: Option<&Value>, k: usize, cap: usize) -> String {
    match items {
        Some(Value::Array(items)) => items
            .iter()
            .take(k)
            .map(|x| py_str(x).chars().take(cap).collect::<String>())
            .collect::<Vec<_>>()
            .join("; "),
        _ => String::new(),
    }
}

/// `p.get(key) or default` rendered with `str()`.
fn or_default(p: &Row, key: &str, default: &str) -> String {
    match p.get(key) {
        Some(v) if py_truthy(v) => py_str(v),
        _ => default.to_string(),
    }
}

/// `format_junk_proposal(p)`.
pub(crate) fn format_junk_proposal(p: &Row) -> String {
    let none_if_empty = |s: String| if s.is_empty() { "none".to_string() } else { s };
    format!(
        "[{}] '{}' \u{2014} detector: {}; degree {}; facts {}; lesson-minted object: {}; scopes {}\n    edges: {}\n    facts: {}\n    notes: {}",
        p.get("n").map_or_else(String::new, py_str),
        p.get("display").map_or_else(|| "?".to_string(), py_str),
        or_default(p, "reason", "?"),
        p.get("degree").map_or_else(|| "0".to_string(), py_str),
        p.get("facts").map_or_else(|| "0".to_string(), py_str),
        if p.get("lesson_object").is_some_and(py_truthy) {
            "yes"
        } else {
            "no"
        },
        or_default(p, "scopes", "[]"),
        none_if_empty(snips(p.get("edges"), 8, 100)),
        none_if_empty(snips(p.get("fact_text"), 3, 120)),
        none_if_empty(snips(p.get("mentions"), 3, 240)),
    )
}

/// One junk verdict (`judge_junk`'s dict): the note is None when the model
/// gave none.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct JunkVerdict {
    pub n: usize,
    pub verdict: String,
    pub confidence: f64,
    pub note: Option<String>,
}

/// `judge_junk`'s validation loop.
pub(crate) fn parse_junk_verdicts(
    raw: &[Value],
    known: &HashSet<usize>,
) -> Result<Vec<JunkVerdict>, String> {
    let mut out = Vec::new();
    for v in raw {
        let Some((n, verdict, confidence)) = verdict_head(v, known, &["delete", "keep", "leave"])?
        else {
            continue;
        };
        out.push(JunkVerdict {
            n,
            verdict,
            confidence,
            note: text_field(v, "note", 400),
        });
    }
    Ok(out)
}

/// `ex.judge_junk(rows)`: one call; the endpoint's served model is updated
/// as Python's `_judge_request` stores it. `Err` is `str(exc)`.
async fn judge_junk(ex: &mut JudgeEx, rows: &[Row]) -> Result<Vec<JunkVerdict>, String> {
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let user: Vec<String> = rows.iter().map(format_junk_proposal).collect();
    let (served, result) = ex
        .compat
        .judge_request(
            JUNK_JUDGE_SYSTEM_PROMPT,
            &user.join("\n\n"),
            rows.len(),
            "judge_junk",
        )
        .await;
    if let Some(s) = served {
        ex.served = s;
    }
    let raw = result.map_err(|e| match e {
        ExtractFailure::Extractor(x) => x.message,
        ExtractFailure::Uncaught(u) => u.message,
    })?;
    let known: HashSet<usize> = rows
        .iter()
        .filter_map(|r| r.get("n").and_then(Value::as_u64))
        .map(|n| n as usize)
        .collect();
    parse_junk_verdicts(&raw, &known)
}

/// `_stamp_skipped(verdicts, rows)`: a row the model skipped is a
/// zero-confidence leave.
fn stamp_skipped(verdicts: &mut Vec<JunkVerdict>, rows: &[Row]) {
    let returned: HashSet<usize> = verdicts.iter().map(|v| v.n).collect();
    for r in rows {
        let n = r.get("n").and_then(Value::as_u64).unwrap_or(0) as usize;
        if !returned.contains(&n) {
            verdicts.push(JunkVerdict {
                n,
                verdict: "leave".into(),
                confidence: 0.0,
                note: Some("model returned no verdict".into()),
            });
        }
    }
}

// ── the evidence pack ────────────────────────────────────────────────────

/// One live edge as the pack reads it.
#[derive(Debug, Clone)]
pub(crate) struct JEdge {
    src: i64,
    dst: i64,
    relation: String,
    origin: Option<String>,
}

/// `_junk_evidence_locked(pending)`: the storage reads behind the pack.
pub(crate) struct JunkEvidence {
    /// `(id, canonical, display)` in id order.
    entities: Vec<(i64, String, String)>,
    edges: Vec<JEdge>,
    scopes: BTreeMap<i64, Vec<String>>,
    /// `(source, text)` in id order (`load_entry_texts`).
    entries: Vec<(Option<String>, String)>,
    facts: HashMap<i64, i64>,
    fact_texts: Vec<(String, i64)>,
    /// `entity_fact_rows(entity_id, canonical)` per pending entity.
    fact_rows: HashMap<i64, Vec<(String, String)>>,
}

/// Caller holds the lock.
pub(crate) async fn junk_evidence_body(c: &Client, rows: &[Row]) -> Result<JunkEvidence, ToolError> {
    let g = crate::storage::graph::load_graph_body(c)
        .await
        .map_err(internal)?;
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let entities: Vec<(i64, String, String)> = g["entities"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| (e["id"].as_i64().unwrap_or(0), s(&e["canonical"]), s(&e["display"])))
        .collect();
    let edges = g["edges"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| JEdge {
            src: e["src_id"].as_i64().unwrap_or(0),
            dst: e["dst_id"].as_i64().unwrap_or(0),
            relation: s(&e["relation"]),
            origin: e["origin"].as_str().map(str::to_string),
        })
        .collect();
    let mut scopes: BTreeMap<i64, Vec<String>> = BTreeMap::new();
    for r in c
        .query(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source",
            &[],
        )
        .await
        .map_err(internal)?
    {
        scopes.entry(r.get(0)).or_default().push(r.get(1));
    }
    let entries = c
        .query("SELECT source, text FROM entries ORDER BY id", &[])
        .await
        .map_err(internal)?
        .iter()
        .map(|r| (r.get::<_, Option<String>>(0), r.get::<_, String>(1)))
        .collect();
    let facts = c
        .query(
            "SELECT entity_id, COUNT(*) FROM facts \
             WHERE status = 'current' AND entity_id IS NOT NULL GROUP BY entity_id",
            &[],
        )
        .await
        .map_err(internal)?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    let fact_texts = c
        .query(
            "SELECT entity, COUNT(*) FROM facts WHERE status = 'current' GROUP BY entity",
            &[],
        )
        .await
        .map_err(internal)?
        .iter()
        .map(|r| {
            let ent: Option<String> = r.get(0);
            (ent.unwrap_or_else(|| "None".into()), r.get(1))
        })
        .collect();
    let canon: HashMap<i64, &str> = entities
        .iter()
        .map(|(id, c, _)| (*id, c.as_str()))
        .collect();
    let mut fact_rows = HashMap::new();
    for row in rows {
        let Some(eid) = vi(row, "entity_id") else {
            continue;
        };
        let canonical = canon.get(&eid).copied().unwrap_or("");
        let got: Vec<(String, String)> = c
            .query(
                "SELECT attribute, value FROM facts \
                 WHERE (entity_id = $1 OR entity_norm = $2) \
                   AND superseded_at IS NULL ORDER BY id LIMIT 3",
                &[&eid, &canonical],
            )
            .await
            .map_err(internal)?
            .iter()
            .map(|r| {
                (
                    r.get::<_, Option<String>>(0).unwrap_or_else(|| "None".into()),
                    r.get::<_, Option<String>>(1).unwrap_or_else(|| "None".into()),
                )
            })
            .collect();
        fact_rows.insert(eid, got);
    }
    Ok(JunkEvidence {
        entities,
        edges,
        scopes,
        entries,
        facts,
        fact_texts,
        fact_rows,
    })
}

/// `_enrich_junk_proposals_from(pending, evidence)`: pure.
pub(crate) fn enrich_junk_from(rows: &[Row], ev: &JunkEvidence, cfg: &DeepDreamConfig) -> Vec<Value> {
    let cap = cfg.snippet_max_chars;
    let disp: HashMap<i64, &str> = ev.entities.iter().map(|(i, _, d)| (*i, d.as_str())).collect();
    let canon: HashMap<i64, &str> = ev.entities.iter().map(|(i, c, _)| (*i, c.as_str())).collect();
    let mut deg: HashMap<i64, i64> = HashMap::new();
    let mut by_ent: HashMap<i64, Vec<&JEdge>> = HashMap::new();
    for ed in &ev.edges {
        *deg.entry(ed.src).or_insert(0) += 1;
        *deg.entry(ed.dst).or_insert(0) += 1;
        by_ent.entry(ed.src).or_default().push(ed);
        by_ent.entry(ed.dst).or_default().push(ed);
    }
    let mut facts_by_norm: HashMap<String, i64> = HashMap::new();
    for (text, n) in &ev.fact_texts {
        let k = norm_name(text);
        if !k.is_empty() {
            *facts_by_norm.entry(k).or_insert(0) += n;
        }
    }
    let mut entry_tokens: Option<Vec<BTreeSet<String>>> = None;
    let mut mentions_of = |display: &str| -> Vec<String> {
        let want = token_set(display);
        if want.is_empty() {
            return Vec::new();
        }
        let tokens = entry_tokens
            .get_or_insert_with(|| ev.entries.iter().map(|(_, t)| token_set(t)).collect());
        let mut found = Vec::new();
        for ((src, text), toks) in ev.entries.iter().zip(tokens.iter()) {
            if want.is_subset(toks) {
                let head = match src.as_deref() {
                    Some(s) if !s.is_empty() => format!("[{s}] "),
                    _ => String::new(),
                };
                found.push(format!("{head}{}", py_slice_chars(text, cap)));
                if found.len() >= 3 {
                    break;
                }
            }
        }
        found
    };
    let mut out = Vec::new();
    for (i, p) in rows.iter().enumerate() {
        let eid = vi(p, "entity_id").unwrap_or(0);
        let display = match disp.get(&eid) {
            Some(d) => (*d).to_string(),
            None => or_default(p, "entity", "?"),
        };
        let edges: &[&JEdge] = by_ent.get(&eid).map(Vec::as_slice).unwrap_or(&[]);
        let lesson_object = !edges.is_empty()
            && edges.iter().all(|ed| {
                matches!(ed.relation.as_str(), "prefers" | "avoids")
                    && ed.origin.as_deref().unwrap_or("") == "action"
            });
        let facts = [
            ev.facts.get(&eid).copied().unwrap_or(0),
            facts_by_norm.get(&norm_name(&display)).copied().unwrap_or(0),
            facts_by_norm
                .get(canon.get(&eid).copied().unwrap_or(""))
                .copied()
                .unwrap_or(0),
        ]
        .into_iter()
        .max()
        .unwrap_or(0);
        let label = |id: i64| disp.get(&id).copied().unwrap_or("?");
        let edge_lines: Vec<String> = edges
            .iter()
            .take(8)
            .map(|ed| {
                let origin = match ed.origin.as_deref() {
                    Some(o) if !o.is_empty() => o,
                    _ => "?",
                };
                format!(
                    "{} -{}-> {} [{origin}]",
                    label(ed.src),
                    ed.relation,
                    label(ed.dst)
                )
            })
            .collect();
        let fact_text: Vec<String> = ev
            .fact_rows
            .get(&eid)
            .map(|rows| {
                rows.iter()
                    .map(|(a, v)| format!("{a}={}", v.chars().take(120).collect::<String>()))
                    .collect()
            })
            .unwrap_or_default();
        let mut scopes = ev.scopes.get(&eid).cloned().unwrap_or_default();
        scopes.sort();
        let mentions = mentions_of(&display);
        out.push(json!({
            "n": i + 1, "display": display, "reason": p.get("reason").cloned().unwrap_or(Value::Null),
            "degree": deg.get(&eid).copied().unwrap_or(0),
            "edges": edge_lines, "facts": facts, "fact_text": fact_text,
            "lesson_object": lesson_object, "scopes": scopes, "mentions": mentions,
        }));
    }
    out
}

// ── the dream-judge decision ─────────────────────────────────────────────

/// `_graph_accept_entity_junk_locked(id, decided_by="dream-judge",
/// _review_guard=...)` in the caller's transaction: the audit (the
/// tombstone that suppresses a re-mint), the delete, the status.
async fn accept_junk_locked(c: &Client, review: &Review, row: &Row, id: i64) -> Result<bool, ToolError> {
    if !review.current(c, row, false, false).await? {
        return Ok(false);
    }
    let Some(prop) = rj::get_entity_proposal(c, id).await? else {
        return Ok(false);
    };
    if vs(&prop, "status") != Some("pending") || vs(&prop, "kind") != Some("junk") {
        return Ok(false);
    }
    let entity = vi(&prop, "entity_id");
    let display = match entity {
        Some(eid) => c
            .query_opt("SELECT display FROM entities WHERE id = $1", &[&eid])
            .await
            .map_err(internal)?
            .map(|r| r.get::<_, String>(0)),
        None => None,
    };
    let votes = crate::graph_review_tools::actions::entity_proposal_votes_body(c, id).await?;
    let reason = format!(
        "junk: {}",
        prop.get("reason").map_or_else(|| "None".to_string(), py_str)
    );
    crate::graph_review_tools::actions::record_merge_decision_by(
        c,
        id,
        display.as_deref().unwrap_or("?"),
        None,
        "accepted",
        prop.get("score").and_then(Value::as_f64).map(|x| x as f32),
        Some(&reason),
        "dream-judge",
        now(),
        votes,
    )
    .await?;
    let ok = match entity {
        Some(eid) => crate::storage::graph::delete_entity_body(c, eid)
            .await
            .map_err(internal)?,
        None => false,
    };
    c.execute(
        "UPDATE entity_proposals SET status = $1, \
         decided_by = COALESCE($2, decided_by), \
         decided_at = COALESCE($3, decided_at) WHERE id = $4",
        &[&"accepted", &"dream-judge", &now(), &id],
    )
    .await
    .map_err(internal)?;
    Ok(ok)
}

// ── the tick ─────────────────────────────────────────────────────────────

fn zero(key: &str, v: Value) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("judged".into(), json!(0));
    m.insert(key.into(), v);
    m
}

/// `deep_dream_judge_junk()`: never fails into the sweep; a failure is the
/// answer's `error` field.
pub(crate) async fn deep_dream_judge_junk(
    service: &Service,
    stop: &watch::Receiver<bool>,
) -> Map<String, Value> {
    let cfg = &service.config.memory.deep_dream;
    if !cfg.judges_enabled {
        return zero("skipped", json!("judges_disabled"));
    }
    if !matches!(cfg.junk_judge_mode.as_str(), "shadow" | "auto") {
        return zero("skipped", json!("disabled"));
    }
    let result = match service.ensure_init_inner().await {
        Ok(ready) => junk_tick(service, &ready, stop).await,
        Err(e) => Err(e),
    };
    match result {
        Ok(out) => out,
        Err(e) if e == STOPPED => zero("skipped", json!("stopping")),
        Err(e) => {
            eprintln!("deep-dream junk judge failed: {e}");
            zero("error", json!(e))
        }
    }
}

async fn pending_junk(c: &Client) -> Result<Vec<Row>, ToolError> {
    rj::pending_rows(c, Queue::Junk).await
}

/// The junk judge's `try` body.
async fn junk_tick(
    service: &Service,
    ready: &Ready,
    stop: &watch::Receiver<bool>,
) -> Result<Map<String, Value>, String> {
    let cfg = &service.config.memory.deep_dream;
    let mode = cfg.junk_judge_mode.clone();
    let client = ready.storage.client();
    let e2s = |e: ToolError| err_text(&e);
    let cap = cfg.judge_batch.max(1);
    let (pending, has_terminals) = {
        let _lock = ready.write.lock().await;
        reads(client, |c| async move {
            Ok((
                pending_junk(c).await?,
                rj::has_active_decisions(c, Queue::Junk).await?,
            ))
        })
        .await
        .map_err(e2s)?
    };
    if pending.is_empty() && !has_terminals {
        let mut m = Map::new();
        m.insert("judged".into(), json!(0));
        return Ok(m);
    }
    let Some(mut ex) = or_stop(stop, judge_extractor(service, None)).await? else {
        return Ok(zero("skipped", json!("no_judge_extractor")));
    };
    let current_model = or_stop(stop, rj::observed_model(&ex.compat, ex.served.as_deref())).await?;
    let mut review = Review::new(
        &service.config,
        &ex.compat,
        Queue::Junk,
        pending,
        current_model,
    );
    let reconsidered = {
        let _lock = ready.write.lock().await;
        let reconsidered =
            rj::refresh_proposal_terminals(client, &review, cap, ex.served.as_deref())
                .await
                .map_err(e2s)?;
        let r = &mut review;
        reads(client, |c| async move {
            r.pending = pending_junk(c).await?;
            r.prepare(c, cap).await
        })
        .await
        .map_err(e2s)?;
        reconsidered
    };
    or_stop(stop, review.sign()).await?.map_err(e2s)?;
    let pending: Vec<Row> = {
        let _lock = ready.write.lock().await;
        let r = &mut review;
        let served = ex.served.clone();
        in_txn(
            client,
            |c| async move { r.refresh(c, served.as_deref()).await },
        )
        .await
        .map_err(e2s)?;
        review
            .pending
            .iter()
            .filter(|p| !rj::truthy(p, "judge_verdict"))
            .cloned()
            .collect()
    };
    if pending.is_empty() {
        let mut m = Map::new();
        m.insert("judged".into(), json!(0));
        m.insert("reconsideration".into(), reconsidered);
        return Ok(m);
    }
    let batch: Vec<Row> = pending.iter().take(cap as usize).cloned().collect();
    eprintln!(
        "deep-dream junk judge: judging {} pending junk proposal(s) (mode {mode})",
        batch.len()
    );
    let rows = review.rows(&batch);
    let mut verdicts = or_stop(stop, judge_junk(&mut ex, &rows)).await??;
    stamp_skipped(&mut verdicts, &rows);
    let model = model_name(&ex, None);
    let served = ex.served.clone();
    let mut judged = 0i64;
    let mut applied = 0i64;
    let at = now();
    // `_snapshot_once`: the graph snapshot before the first delete.
    let mut snap: Option<Option<String>> = None;
    {
        let _lock = ready.write.lock().await;
        let r = &mut review;
        let (b, sv) = (&batch, served.as_deref());
        reads(client, |c| async move {
            r.validate(c, &service.config, b, sv, None, None, "second")
                .await
        })
        .await
        .map_err(e2s)?;
        // Keep first, stable (`sorted(..., key=verdict not in (reject, keep))`).
        let mut order = verdicts.clone();
        order.sort_by_key(|v| !matches!(v.verdict.as_str(), "reject" | "keep"));
        for v in &order {
            let Some(p) = batch.get(v.n.wrapping_sub(1)) else {
                continue;
            };
            let Some(r) = rows.get(v.n.wrapping_sub(1)) else {
                continue;
            };
            let (review, model, served, snap) = (&review, &model, served.as_deref(), &mut snap);
            let mode = mode.as_str();
            let outcome = in_txn(client, |c| async move {
                if !review.current(c, p, false, false).await? {
                    return Ok((false, false));
                }
                let id = id_of(p);
                let ok = set_first_judgment(
                    c,
                    id,
                    &v.verdict,
                    v.confidence,
                    v.note.as_deref(),
                    model,
                    at,
                    served,
                )
                .await?;
                if ok {
                    review.record(c, p, served).await?;
                } else {
                    return Ok((false, false));
                }
                if mode != "auto" {
                    return Ok((true, false));
                }
                let degree = r.get("degree").and_then(Value::as_i64).unwrap_or(0);
                let facts = r.get("facts").and_then(Value::as_i64).unwrap_or(0);
                if v.verdict == "keep" && v.confidence >= cfg.junk_keep_min_confidence {
                    let prior = rj::capture_proposal_pair(c, Queue::Junk, p).await?;
                    let rejected = reject_locked(c, review, p, id).await?;
                    if rejected {
                        if !crate::mutants::active("mcp-dream-junk-keep-unrecorded") {
                            rj::record_proposal_terminal(c, review, p, &prior, served).await?;
                        }
                    } else {
                        review.retry(c, p).await?;
                    }
                    return Ok((true, rejected));
                }
                let under_bar = (degree <= cfg.junk_max_auto_degree && facts <= 1)
                    || crate::mutants::active("mcp-dream-junk-no-evidence-bar");
                if v.verdict == "delete" && v.confidence >= cfg.junk_delete_min_confidence && under_bar
                {
                    if snap.is_none() {
                        *snap = Some(
                            super::deep::write_graph_snapshot_body(
                                c,
                                &service.data_dir,
                                cfg.snapshot_keep,
                            )
                            .await?,
                        );
                    }
                    if snap.as_ref().is_some_and(Option::is_none) {
                        review.retry(c, p).await?;
                        return Ok((true, false));
                    }
                    let accepted = accept_junk_locked(c, review, p, id).await?;
                    if !accepted {
                        review.retry(c, p).await?;
                    }
                    return Ok((true, accepted));
                }
                Ok((true, false))
            })
            .await
            .map_err(e2s)?;
            judged += i64::from(outcome.0);
            applied += i64::from(outcome.1);
            if outcome.1 && v.verdict == "delete" {
                // A structural edit changes graph-wide evidence.
                review.valid.clear();
            }
        }
    }
    let mut m = Map::new();
    m.insert("judged".into(), json!(judged));
    m.insert("applied".into(), json!(applied));
    m.insert(
        "pending_unjudged".into(),
        json!((pending.len() as i64 - judged).max(0)),
    );
    m.insert("model".into(), json!(model));
    m.insert("mode".into(), json!(mode));
    m.insert("reconsideration".into(), reconsidered);
    Ok(m)
}

/// `run_sweep_once`'s log condition for a review-queue judge's answer.
pub(crate) fn worth_logging(res: &Map<String, Value>) -> bool {
    let set = |k: &str| res.get(k).is_some_and(py_truthy);
    set("judged")
        || set("applied")
        || set("proposed")
        || set("error")
        || res
            .get("reconsideration")
            .and_then(|r| r.get("reopened"))
            .is_some_and(py_truthy)
        || set("second_opinions")
        || set("two_vote_refused_same_model")
        || set("served_model_mismatch")
        || set("second_opinion_error")
}

#[cfg(test)]
#[path = "queue_judges_tests.rs"]
mod tests;
