//! The review-queue judges the sweep runs after the merge judge
//! (`memory/dream.py` `run_sweep_once` 2513-2530).
//!
//! The link judge: `service_dream.deep_dream_judge_links` (5003-5240) with
//! its second opinion and two-vote apply (`_link_two_vote_action`,
//! `_stored_second_distinct`, `_link_two_vote_replay`, 4176-4239), its
//! evidence pack (`_link_evidence_locked` / `_enrich_link_proposals_from`,
//! 4936-5001, with `graph_consolidation.shared_mention_entries`), the
//! request and reply parsing (`memory/dream.py` `format_link_proposal`
//! 567-582, `judge_links` 1848-1873) and the dream-judge decisions it applies
//! (`service.py` `_graph_accept_proposal_locked` without a retype and
//! `_graph_reject_proposal_locked`, 8979-9058).
//!
//! The junk judge: `service_dream.deep_dream_judge_junk` (5329-5422) with
//! its evidence pack (`_junk_evidence_locked` /
//! `_enrich_junk_proposals_from`, 5244-5327), the request and reply parsing
//! (`format_junk_proposal` 592-602, `judge_junk` 1905-1923, `_verdict_head` /
//! `_text` 1825-1846) and the dream-judge decisions it applies (`service.py`
//! `_graph_accept_entity_junk_locked` 9118-9144 and the junk branch of
//! `_graph_reject_entity_proposal_locked` 9151-9195).
//!
//! The curation and candidate judges join this module in their own changes
//! (task P3b); until then the sweep does not run them.
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
    JUDGE_ALIASES, JudgeEx, STOPPED, bump, check_stop, encodable, err_text, fixed2,
    judge_extractor, judge_second_extractor, model_name, or_stop, real, reject_locked,
    report_served_model, same_endpoint, second_opinion_distinct, set_first_judgment,
};
use super::prompts::{JUNK_JUDGE_SYSTEM_PROMPT, LINK_JUDGE_SYSTEM_PROMPT};
use super::review_judgments::{self as rj, Queue, Review, Row, id_of, internal, now, vf, vi, vs};
use crate::config::DeepDreamConfig;
use crate::graph_consolidation::py_slice_chars;
use crate::graph_review::token_set;
use crate::graph_review_tools::actions::{
    in_txn, reads, reviewed_edge_confidence, set_proposal_status_body,
};
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
pub(crate) async fn junk_evidence_body(
    c: &Client,
    rows: &[Row],
) -> Result<JunkEvidence, ToolError> {
    let g = crate::storage::graph::load_graph_body(c)
        .await
        .map_err(internal)?;
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let entities: Vec<(i64, String, String)> = g["entities"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| {
            (
                e["id"].as_i64().unwrap_or(0),
                s(&e["canonical"]),
                s(&e["display"]),
            )
        })
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
                    r.get::<_, Option<String>>(0)
                        .unwrap_or_else(|| "None".into()),
                    r.get::<_, Option<String>>(1)
                        .unwrap_or_else(|| "None".into()),
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
pub(crate) fn enrich_junk_from(
    rows: &[Row],
    ev: &JunkEvidence,
    cfg: &DeepDreamConfig,
) -> Vec<Value> {
    let cap = cfg.snippet_max_chars;
    let disp: HashMap<i64, &str> = ev
        .entities
        .iter()
        .map(|(i, _, d)| (*i, d.as_str()))
        .collect();
    let canon: HashMap<i64, &str> = ev
        .entities
        .iter()
        .map(|(i, c, _)| (*i, c.as_str()))
        .collect();
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
            facts_by_norm
                .get(&norm_name(&display))
                .copied()
                .unwrap_or(0),
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
async fn accept_junk_locked(
    c: &Client,
    review: &Review,
    row: &Row,
    id: i64,
) -> Result<bool, ToolError> {
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
        prop.get("reason")
            .map_or_else(|| "None".to_string(), py_str)
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
            let (rv, model, served, snap) = (&review, &model, served.as_deref(), &mut snap);
            let mode = mode.as_str();
            let outcome = in_txn(client, |c| async move {
                if !rv.current(c, p, false, false).await? {
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
                    rv.record(c, p, served).await?;
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
                    let rejected = reject_locked(c, rv, p, id).await?;
                    if rejected {
                        if !crate::mutants::active("mcp-dream-junk-keep-unrecorded") {
                            rj::record_proposal_terminal(c, rv, p, &prior, served).await?;
                        }
                    } else {
                        rv.retry(c, p).await?;
                    }
                    return Ok((true, rejected));
                }
                let under_bar = (degree <= cfg.junk_max_auto_degree && facts <= 1)
                    || crate::mutants::active("mcp-dream-junk-no-evidence-bar");
                if v.verdict == "delete"
                    && v.confidence >= cfg.junk_delete_min_confidence
                    && under_bar
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
                        rv.retry(c, p).await?;
                        return Ok((true, false));
                    }
                    let accepted = accept_junk_locked(c, rv, p, id).await?;
                    if !accepted {
                        rv.retry(c, p).await?;
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

// ── the link judge ───────────────────────────────────────────────────────

/// `format_link_proposal(p)`.
pub(crate) fn format_link_proposal(p: &Row) -> String {
    let get = |k: &str| p.get(k).map_or_else(|| "?".to_string(), py_str);
    let none_if_empty = |s: String| if s.is_empty() { "none".to_string() } else { s };
    let mut out = format!(
        "[{}] SRC '{}' (scopes {}) --{}--> DST '{}' (scopes {})\n    detector: {}\n    src edges: {}\n    dst edges: {}",
        p.get("n").map_or_else(String::new, py_str),
        get("src"),
        or_default(p, "src_scopes", "[]"),
        get("relation"),
        get("dst"),
        or_default(p, "dst_scopes", "[]"),
        or_default(p, "rationale", "?"),
        none_if_empty(snips(p.get("src_edges"), 8, 80)),
        none_if_empty(snips(p.get("dst_edges"), 8, 80)),
    );
    let both = p.get("co_mentions").filter(|v| py_truthy(v));
    if let Some(both) = both {
        out.push_str(&format!(
            "\n    notes naming both: {}",
            snips(Some(both), 3, 240)
        ));
    } else {
        out.push_str(&format!(
            "\n    src notes: {}\n    dst notes: {}",
            none_if_empty(snips(p.get("src_mentions"), 2, 240)),
            none_if_empty(snips(p.get("dst_mentions"), 2, 240)),
        ));
    }
    out
}

/// One link verdict (`judge_links`' dict): `relation` is set only on a
/// retype.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct LinkVerdict {
    pub n: usize,
    pub verdict: String,
    pub confidence: f64,
    pub note: Option<String>,
    pub relation: Option<String>,
}

/// `judge_links`' validation loop: a retype naming no relation cannot be
/// applied and degrades to a leave.
pub(crate) fn parse_link_verdicts(
    raw: &[Value],
    known: &HashSet<usize>,
) -> Result<Vec<LinkVerdict>, String> {
    let mut out = Vec::new();
    for v in raw {
        let Some((n, mut verdict, confidence)) =
            verdict_head(v, known, &["accept", "retype", "reject", "leave"])?
        else {
            continue;
        };
        let relation = if verdict == "retype" {
            text_field(v, "relation", 64)
        } else {
            None
        };
        if verdict == "retype" && relation.is_none() {
            verdict = "leave".into();
        }
        out.push(LinkVerdict {
            n,
            verdict,
            confidence,
            note: text_field(v, "note", 400),
            relation,
        });
    }
    Ok(out)
}

/// `ex.judge_links(rows)`: one call, as `judge_junk`.
async fn judge_links(ex: &mut JudgeEx, rows: &[Row]) -> Result<Vec<LinkVerdict>, String> {
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let user: Vec<String> = rows.iter().map(format_link_proposal).collect();
    let (served, result) = ex
        .compat
        .judge_request(
            LINK_JUDGE_SYSTEM_PROMPT,
            &user.join("\n\n"),
            rows.len(),
            "judge_links",
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
    parse_link_verdicts(&raw, &known)
}

/// `_stamp_skipped(verdicts, rows, relation=None)`.
fn stamp_skipped_links(verdicts: &mut Vec<LinkVerdict>, rows: &[Row]) {
    let returned: HashSet<usize> = verdicts.iter().map(|v| v.n).collect();
    for r in rows {
        let n = r.get("n").and_then(Value::as_u64).unwrap_or(0) as usize;
        if !returned.contains(&n) {
            verdicts.push(LinkVerdict {
                n,
                verdict: "leave".into(),
                confidence: 0.0,
                note: Some("model returned no verdict".into()),
                relation: None,
            });
        }
    }
}

/// `_link_evidence_locked(pending)`: the storage reads behind the pack.
pub(crate) struct LinkEvidence {
    /// `(id, display)` in id order.
    entities: Vec<(i64, String)>,
    /// `(src, relation, dst)` of the live edges, in id order.
    edges: Vec<(i64, String, i64)>,
    scopes: BTreeMap<i64, Vec<String>>,
    /// `(source, text)` in id order (`load_entry_texts`).
    entries: Vec<(Option<String>, String)>,
}

/// Caller holds the lock.
pub(crate) async fn link_evidence_body(c: &Client) -> Result<LinkEvidence, ToolError> {
    let g = crate::storage::graph::load_graph_body(c)
        .await
        .map_err(internal)?;
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let entities = g["entities"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| (e["id"].as_i64().unwrap_or(0), s(&e["display"])))
        .collect();
    let edges = g["edges"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| {
            (
                e["src_id"].as_i64().unwrap_or(0),
                s(&e["relation"]),
                e["dst_id"].as_i64().unwrap_or(0),
            )
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
    Ok(LinkEvidence {
        entities,
        edges,
        scopes,
        entries,
    })
}

/// `graph_consolidation.shared_mention_entries(entries, a, b, limit,
/// tokens=...)`: the texts of the entries naming both entities, in order.
fn shared_mention_entries<'a>(
    entries: &'a [(Option<String>, String)],
    tokens: &[BTreeSet<String>],
    a: &str,
    b: &str,
    limit: usize,
) -> Vec<&'a str> {
    let (wa, wb) = (token_set(a), token_set(b));
    if wa.is_empty() || wb.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    for ((_, text), toks) in entries.iter().zip(tokens) {
        if wa.is_subset(toks) && wb.is_subset(toks) {
            out.push(text.as_str());
            if out.len() >= limit {
                break;
            }
        }
    }
    out
}

/// `_enrich_link_proposals_from(pending, evidence)`: pure.
pub(crate) fn enrich_link_from(
    rows: &[Row],
    ev: &LinkEvidence,
    cfg: &DeepDreamConfig,
) -> Vec<Value> {
    let cap = cfg.snippet_max_chars;
    let disp: HashMap<i64, &str> = ev.entities.iter().map(|(i, d)| (*i, d.as_str())).collect();
    let label = |id: i64| disp.get(&id).copied().unwrap_or("?");
    let mut outs: HashMap<i64, Vec<String>> = HashMap::new();
    let mut ins: HashMap<i64, Vec<String>> = HashMap::new();
    for (src, relation, dst) in &ev.edges {
        outs.entry(*src)
            .or_default()
            .push(format!("{relation}>{}", label(*dst)));
        ins.entry(*dst)
            .or_default()
            .push(format!("{}>{relation}", label(*src)));
    }
    let edges_of = |eid: i64| -> Vec<String> {
        outs.get(&eid)
            .into_iter()
            .flatten()
            .chain(ins.get(&eid).into_iter().flatten())
            .take(8)
            .cloned()
            .collect()
    };
    let tokens: Vec<BTreeSet<String>> = if rows.is_empty() {
        Vec::new()
    } else {
        ev.entries.iter().map(|(_, t)| token_set(t)).collect()
    };
    let mentions_of = |display: &str| -> Vec<String> {
        let want = token_set(display);
        if want.is_empty() {
            return Vec::new();
        }
        let mut found = Vec::new();
        for ((src, text), toks) in ev.entries.iter().zip(&tokens) {
            if want.is_subset(toks) {
                let head = match src.as_deref() {
                    Some(s) if !s.is_empty() => format!("[{s}] "),
                    _ => String::new(),
                };
                found.push(format!("{head}{}", py_slice_chars(text, cap)));
                if found.len() >= 2 {
                    break;
                }
            }
        }
        found
    };
    let scopes_of = |eid: i64| -> Vec<String> {
        let mut s = ev.scopes.get(&eid).cloned().unwrap_or_default();
        s.sort();
        s
    };
    let mut out = Vec::new();
    for (i, p) in rows.iter().enumerate() {
        let src = vs(p, "src").unwrap_or_default();
        let dst = vs(p, "dst").unwrap_or_default();
        let both: Vec<String> = shared_mention_entries(&ev.entries, &tokens, src, dst, 3)
            .into_iter()
            .map(|t| py_slice_chars(t, cap))
            .collect();
        let (src_mentions, dst_mentions) = if both.is_empty() {
            (mentions_of(src), mentions_of(dst))
        } else {
            (Vec::new(), Vec::new())
        };
        let (src_id, dst_id) = (vi(p, "src_id").unwrap_or(0), vi(p, "dst_id").unwrap_or(0));
        out.push(json!({
            "n": i + 1,
            "src": p.get("src").cloned().unwrap_or(Value::Null),
            "relation": p.get("relation").cloned().unwrap_or(Value::Null),
            "dst": p.get("dst").cloned().unwrap_or(Value::Null),
            "rationale": p.get("rationale").cloned().unwrap_or(Value::Null),
            "src_edges": edges_of(src_id),
            "dst_edges": edges_of(dst_id),
            "src_scopes": scopes_of(src_id),
            "dst_scopes": scopes_of(dst_id),
            "co_mentions": both,
            "src_mentions": src_mentions,
            "dst_mentions": dst_mentions,
        }));
    }
    out
}

/// `set_proposal_judgment` (3599-3613).
#[allow(clippy::too_many_arguments)]
async fn set_link_judgment(
    c: &Client,
    id: i64,
    v: &LinkVerdict,
    model: &str,
    at: f64,
    served: Option<&str>,
) -> Result<bool, ToolError> {
    encodable(&[
        Some(&v.verdict),
        v.note.as_deref(),
        Some(model),
        v.relation.as_deref(),
        served,
    ])?;
    let confidence = real(v.confidence)?;
    Ok(c.execute(
        "UPDATE edge_proposals SET judge_verdict=$1, \
         judge_confidence=$2, judge_note=$3, judge_model=$4, \
         judged_at=$5, judge_relation=$6, judge_served_model=$7 \
         WHERE id=$8 AND status='pending'",
        &[
            &v.verdict,
            &confidence,
            &v.note,
            &model,
            &at,
            &v.relation,
            &served,
            &id,
        ],
    )
    .await
    .map_err(internal)?
        > 0)
}

/// `set_proposal_second_judgment` (3615-3638): the note, served model and
/// policy are kept when None.
#[allow(clippy::too_many_arguments)]
async fn set_link_second_judgment(
    c: &Client,
    id: i64,
    verdict: Option<&str>,
    confidence: Option<f64>,
    model: Option<&str>,
    relation: Option<&str>,
    at: Option<f64>,
    note: Option<&str>,
    served: Option<&str>,
    policy: Option<&str>,
) -> Result<bool, ToolError> {
    encodable(&[verdict, model, relation, note, served, policy])?;
    let confidence = confidence.map(real).transpose()?;
    Ok(c.execute(
        "UPDATE edge_proposals SET judge2_verdict=$1, \
         judge2_confidence=$2, judge2_model=$3, judged2_at=$4, \
         judge2_relation=$5, judge_note = COALESCE($6, judge_note), \
         judge2_served_model = COALESCE($7, judge2_served_model), \
         judge2_policy = COALESCE($8, judge2_policy) \
         WHERE id=$9 AND status='pending'",
        &[
            &verdict,
            &confidence,
            &model,
            &at,
            &relation,
            &note,
            &served,
            &policy,
            &id,
        ],
    )
    .await
    .map_err(internal)?
        > 0)
}

/// `_graph_accept_proposal_locked(id, decided_by="dream-judge",
/// _review_guard=...)` with no retype, in the caller's transaction: the
/// edge (floored, a confirming action), then the status.
async fn accept_link_locked(
    c: &Client,
    review: &Review,
    row: &Row,
    id: i64,
) -> Result<bool, ToolError> {
    if !review.current(c, row, false, false).await? {
        return Ok(false);
    }
    let Some(prop) = rj::get_link_proposal(c, id).await? else {
        return Ok(false);
    };
    if vs(&prop, "status") != Some("pending") {
        return Ok(false);
    }
    let confidence = reviewed_edge_confidence(vf(&prop, "confidence").unwrap_or(0.0));
    crate::storage::graph::upsert_edge_body(
        c,
        crate::storage::graph::Edge {
            src_id: vi(&prop, "src_id").unwrap_or(0),
            relation: vs(&prop, "relation").unwrap_or_default(),
            dst_id: vi(&prop, "dst_id").unwrap_or(0),
            confidence,
            origin: Some("action"),
            revive: true,
            source_entry_ids: &[],
        },
    )
    .await
    .map_err(internal)?;
    set_proposal_status_body(c, id, "accepted", "dream-judge", now()).await?;
    Ok(true)
}

/// `_graph_reject_proposal_locked(id, decided_by="dream-judge",
/// _review_guard=...)`: no human confirmation for the judge's own reject.
async fn reject_link_locked(
    c: &Client,
    review: &Review,
    row: &Row,
    id: i64,
) -> Result<bool, ToolError> {
    if !review.current(c, row, false, false).await? {
        return Ok(false);
    }
    set_proposal_status_body(c, id, "rejected", "dream-judge", now()).await
}

/// `review.apply(row, graph_accept_proposal | graph_reject_proposal, id,
/// decided_by="dream-judge")`, in the caller's transaction. The caller
/// prunes the validated rows sharing an endpoint after an accept
/// (`Review::invalidate_endpoints`).
async fn apply_link(
    c: &Client,
    review: &Review,
    row: &Row,
    accept: bool,
    ex_served: Option<&str>,
) -> Result<bool, ToolError> {
    let id = id_of(row);
    let done = if accept {
        accept_link_locked(c, review, row, id).await?
    } else {
        let prior = rj::capture_proposal_pair(c, Queue::Link, row).await?;
        let rejected = reject_link_locked(c, review, row, id).await?;
        if rejected {
            rj::record_proposal_terminal(c, review, row, &prior, ex_served).await?;
        }
        rejected
    };
    if !done {
        review.retry(c, row).await?;
    }
    Ok(done)
}

const TWO_VOTE_REFUSAL: &str = "two-vote apply needs a distinct second model";

/// `_link_two_vote_action(p, cfg, mode)`: the action two agreeing recorded
/// votes take, else None. A retype never applies.
fn link_two_vote_action(p: &Row, cfg: &DeepDreamConfig, mode: &str) -> Option<&'static str> {
    let (v1, v2) = (vs(p, "judge_verdict"), vs(p, "judge2_verdict"));
    if !(cfg.link_judge_two_vote == "auto" && mode == "auto" && v1 == v2) {
        return None;
    }
    let action = match v1 {
        Some("accept") => "accept",
        Some("reject") => "reject",
        _ => return None,
    };
    let mean = (vf(p, "judge_confidence").unwrap_or(0.0)
        + vf(p, "judge2_confidence").unwrap_or(0.0))
        / 2.0;
    (mean >= cfg.link_two_vote_min_confidence).then_some(action)
}

/// `_stored_second_distinct(review, row, configured2)`: the distinct-model
/// check over what the row stores.
async fn stored_second_distinct(
    c: &Client,
    review: &Review,
    row: &Row,
    configured2: Option<&str>,
) -> Result<bool, ToolError> {
    if crate::mutants::active("mcp-dream-link-two-vote-same-model") {
        return Ok(true);
    }
    let first_model = vs(row, "judge_model").unwrap_or("");
    let first_served = review.observed(c, row).await?.filter(|s| !s.is_empty());
    let served2 = vs(row, "judge2_served_model").filter(|s| !s.is_empty());
    let alias_free_same = configured2
        .is_some_and(|m| !m.is_empty() && !JUDGE_ALIASES.contains(&m) && first_model == m);
    Ok(
        matches!((first_served.as_deref(), served2), (Some(a), Some(b)) if a != b)
            && Some(first_model) != vs(row, "judge2_model")
            && !alias_free_same,
    )
}

/// `deep_dream_judge_links()`: never fails into the sweep; a failure is the
/// answer's `error` field.
pub(crate) async fn deep_dream_judge_links(
    service: &Service,
    stop: &watch::Receiver<bool>,
) -> Map<String, Value> {
    let cfg = &service.config.memory.deep_dream;
    if !cfg.judges_enabled {
        return zero("skipped", json!("judges_disabled"));
    }
    if !matches!(cfg.link_judge_mode.as_str(), "shadow" | "auto") {
        return zero("skipped", json!("disabled"));
    }
    let result = match service.ensure_init_inner().await {
        Ok(ready) => link_tick(service, &ready, stop).await,
        Err(e) => Err(e),
    };
    match result {
        Ok(out) => out,
        Err(e) if e == STOPPED => zero("skipped", json!("stopping")),
        Err(e) => {
            eprintln!("deep-dream link judge failed: {e}");
            zero("error", json!(e))
        }
    }
}

fn nonempty(v: &Option<String>) -> bool {
    v.as_deref().is_some_and(|s| !s.is_empty())
}

fn add(out: &mut Map<String, Value>, key: &str, n: bool) {
    bump(out, key, i64::from(n));
}

/// The link judge's `try` body.
async fn link_tick(
    service: &Service,
    ready: &Ready,
    stop: &watch::Receiver<bool>,
) -> Result<Map<String, Value>, String> {
    let cfg = &service.config.memory.deep_dream;
    let mode = cfg.link_judge_mode.clone();
    let two_vote = cfg.link_judge_two_vote.clone();
    let client = ready.storage.client();
    let e2s = |e: ToolError| err_text(&e);
    let cap = cfg.judge_batch.max(1);
    let (pending, has_terminals) = {
        let _lock = ready.write.lock().await;
        reads(client, |c| async move {
            Ok((
                rj::pending_rows(c, Queue::Link).await?,
                rj::has_active_decisions(c, Queue::Link).await?,
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
        Queue::Link,
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
            r.pending = rj::pending_rows(c, Queue::Link).await?;
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
        review.pending.clone()
    };
    let first: Vec<Row> = pending
        .iter()
        .filter(|p| !rj::truthy(p, "judge_verdict"))
        .cloned()
        .collect();
    let gate = |v: Option<&str>| match v {
        Some("accept") => Some(cfg.link_accept_min_confidence),
        Some("reject") => Some(cfg.link_reject_min_confidence),
        _ => None,
    };
    // Only an accept or a reject the single-vote gate left pending can
    // become a two-vote decision.
    let awaiting: Vec<Row> = if cfg.judge_second_opinion
        && matches!(two_vote.as_str(), "shadow" | "auto")
    {
        pending
            .iter()
            .filter(|p| {
                gate(vs(p, "judge_verdict")).is_some_and(|g| {
                    !rj::truthy(p, "judge2_verdict") && vf(p, "judge_confidence").unwrap_or(0.0) < g
                })
            })
            .cloned()
            .collect()
    } else {
        Vec::new()
    };
    let same_model_second = !nonempty(&cfg.judge_second_url)
        && (!nonempty(&cfg.judge_second_model)
            || cfg.judge_second_model.as_deref() == Some(ex.compat.model.as_str()));
    let mut second: Vec<Row> = if same_model_second {
        Vec::new()
    } else {
        awaiting.clone()
    };
    let skipped = if same_model_second { awaiting.len() } else { 0 };
    let refusal_tail = format!("| {TWO_VOTE_REFUSAL}");
    let replay: Vec<Row> = pending
        .iter()
        .filter(|p| {
            rj::truthy(p, "judge2_verdict")
                && link_two_vote_action(p, cfg, &mode).is_some()
                && !vs(p, "judge_note")
                    .unwrap_or("")
                    .contains(refusal_tail.as_str())
        })
        .take(cap as usize)
        .cloned()
        .collect();
    if first.is_empty() && second.is_empty() && replay.is_empty() {
        let mut idle = Map::new();
        idle.insert("judged".into(), json!(0));
        idle.insert("reconsideration".into(), reconsidered);
        if skipped > 0 {
            idle.insert("second_opinion_skipped_same_model".into(), json!(skipped));
        }
        return Ok(idle);
    }
    let ex_model = if ex.compat.model.is_empty() {
        "OpenAICompatExtractor".to_string()
    } else {
        ex.compat.model.clone()
    };
    let mut out = Map::new();
    for (k, v) in [
        ("judged", json!(0)),
        ("applied", json!(0)),
        ("pending_unjudged", json!(0)),
        ("second_opinions", json!(0)),
        ("two_vote_applied", json!(0)),
        ("two_vote_refused_same_model", json!(0)),
        ("second_opinion_skipped_same_model", json!(skipped)),
        ("served_model_mismatch", json!(0)),
        ("served_model_mismatches", json!([])),
        ("model", json!(ex_model)),
        ("mode", json!(mode)),
        ("two_vote", json!(two_vote)),
        ("reconsideration", reconsidered),
    ] {
        out.insert(k.into(), v);
    }
    if !replay.is_empty() {
        check_stop(stop)?;
        two_vote_replay(
            ready,
            cfg,
            &mut out,
            &mut review,
            &replay,
            ex.served.as_deref(),
        )
        .await
        .map_err(e2s)?;
    }

    // Second opinions first; they take at most half the batch while first
    // opinions wait.
    let second_cap = (cap - (first.len() as i64).min(cap / 2)).max(0) as usize;
    let mut ex2: Option<JudgeEx> = None;
    let mut verdicts2: Vec<LinkVerdict> = Vec::new();
    let batch2: Vec<Row> = second.iter().take(second_cap).cloned().collect();
    let mut second_observed: Option<String> = None;
    if !second.is_empty() {
        check_stop(stop)?;
        ex2 = or_stop(stop, judge_second_extractor(service)).await?;
        eprintln!(
            "deep-dream link judge: second opinion on {} pending link proposal(s) (mode {mode}, two-vote {two_vote})",
            batch2.len()
        );
        let rows2 = review.rows(&batch2);
        let result = match ex2.as_mut() {
            Some(x2) => {
                second_observed =
                    or_stop(stop, rj::observed_model(&x2.compat, x2.served.as_deref())).await?;
                or_stop(stop, judge_links(x2, &rows2)).await?
            }
            None => {
                second_observed =
                    or_stop(stop, rj::observed_model(&ex.compat, ex.served.as_deref())).await?;
                or_stop(stop, judge_links(&mut ex, &rows2)).await?
            }
        };
        match result {
            Ok(v) => {
                verdicts2 = v;
                stamp_skipped_links(&mut verdicts2, &rows2);
            }
            Err(err) => {
                if same_endpoint(ex2.as_ref(), &ex) {
                    return Err(err);
                }
                eprintln!(
                    "deep-dream link judge: second opinion failed, first opinions continue: {err}"
                );
                out.insert("second_opinion_error".into(), json!(err));
                second.clear();
            }
        }
    }
    if !second.is_empty() {
        report_served_model(&mut out, "second", ex2.as_ref().unwrap_or(&ex));
        let model2 = model_name(ex2.as_ref().unwrap_or(&ex), None);
        let at = now();
        let _lock = ready.write.lock().await;
        {
            let other = ex2.as_ref().unwrap_or(&ex);
            let served2 = other.served.clone();
            let compat2 = other.compat.clone();
            let observed2 = second_observed.clone();
            let r = &mut review;
            let b = &batch2;
            let config = &service.config;
            reads(client, |c| async move {
                r.validate(
                    c,
                    config,
                    b,
                    served2.as_deref(),
                    Some(&compat2),
                    observed2.as_deref(),
                    "second",
                )
                .await
            })
            .await
            .map_err(e2s)?;
        }
        let mut order = verdicts2.clone();
        order.sort_by_key(|v| !matches!(v.verdict.as_str(), "reject" | "keep"));
        for v in &order {
            let Some(p) = batch2.get(v.n.wrapping_sub(1)) else {
                continue;
            };
            let (rv, x1, x2) = (&review, &ex, ex2.as_ref());
            let (model2, mode, two_vote) = (model2.as_str(), mode.as_str(), two_vote.as_str());
            let served2 = x2.unwrap_or(x1).served.clone();
            let outcome = in_txn(client, |c| async move {
                let mut o = (false, false, false);
                if !rv.current(c, p, true, false).await? {
                    return Ok(o);
                }
                let v1 = vs(p, "judge_verdict");
                let c1 = vf(p, "judge_confidence").unwrap_or(0.0);
                let (v2, c2) = (v.verdict.as_str(), v.confidence);
                let rel2 = v.relation.as_deref();
                let token2 = match rel2 {
                    Some(r) if v2 == "retype" => format!("retype:{r}"),
                    _ => v2.to_string(),
                };
                let tag = if v1 == Some(v2) { "agree" } else { "split" };
                let note = format!(
                    "{} | 2nd ({model2}): {token2} {} [{tag}]",
                    py_slice_chars(vs(p, "judge_note").unwrap_or(""), 160),
                    fixed2(c2)
                );
                if !set_link_second_judgment(
                    c,
                    id_of(p),
                    Some(v2),
                    Some(c2),
                    Some(model2),
                    rel2,
                    Some(at),
                    Some(&note),
                    served2.as_deref(),
                    rv.second_policy.as_deref(),
                )
                .await?
                {
                    return Ok(o);
                }
                o.0 = true;
                if !(two_vote == "auto"
                    && mode == "auto"
                    && v1 == Some(v2)
                    && matches!(v2, "accept" | "reject")
                    && (c1 + c2) / 2.0 >= cfg.link_two_vote_min_confidence)
                {
                    return Ok(o);
                }
                let distinct = crate::mutants::active("mcp-dream-link-two-vote-same-model")
                    || second_opinion_distinct(c, rv, p, x2, x1, model2).await?;
                if !distinct {
                    set_link_second_judgment(
                        c,
                        id_of(p),
                        Some(v2),
                        Some(c2),
                        Some(model2),
                        rel2,
                        Some(at),
                        Some(&format!("{note} | {TWO_VOTE_REFUSAL}")),
                        None,
                        None,
                    )
                    .await?;
                    o.1 = true;
                    return Ok(o);
                }
                o.2 = apply_link(c, rv, p, v2 == "accept", x1.served.as_deref()).await?;
                Ok(o)
            })
            .await
            .map_err(e2s)?;
            add(&mut out, "second_opinions", outcome.0);
            add(&mut out, "two_vote_refused_same_model", outcome.1);
            add(&mut out, "two_vote_applied", outcome.2);
            add(&mut out, "applied", outcome.2);
            if outcome.2 && v.verdict == "accept" {
                review.invalidate_endpoints(p);
            }
        }
    }

    let taken2 = second.iter().take(second_cap).count() as i64;
    if !first.is_empty() && cap > taken2 {
        check_stop(stop)?;
        let batch: Vec<Row> = first
            .iter()
            .take((cap - taken2) as usize)
            .cloned()
            .collect();
        eprintln!(
            "deep-dream link judge: judging {} pending link proposal(s) (mode {mode})",
            batch.len()
        );
        let rows = review.rows(&batch);
        let mut verdicts = or_stop(stop, judge_links(&mut ex, &rows)).await??;
        stamp_skipped_links(&mut verdicts, &rows);
        report_served_model(&mut out, "first", &ex);
        let model = model_name(&ex, None);
        out.insert("model".into(), json!(model));
        let at = now();
        let served = ex.served.clone();
        let _lock = ready.write.lock().await;
        {
            let r = &mut review;
            let (b, sv) = (&batch, served.as_deref());
            let config = &service.config;
            reads(client, |c| async move {
                r.validate(c, config, b, sv, None, None, "second").await
            })
            .await
            .map_err(e2s)?;
        }
        let mut order = verdicts.clone();
        order.sort_by_key(|v| !matches!(v.verdict.as_str(), "reject" | "keep"));
        for v in &order {
            let Some(p) = batch.get(v.n.wrapping_sub(1)) else {
                continue;
            };
            let (rv, model, served, mode) = (&review, &model, served.as_deref(), mode.as_str());
            let outcome = in_txn(client, |c| async move {
                if !rv.current(c, p, false, false).await? {
                    return Ok((false, false));
                }
                if !set_link_judgment(c, id_of(p), v, model, at, served).await? {
                    return Ok((false, false));
                }
                rv.record(c, p, served).await?;
                if mode != "auto" {
                    return Ok((true, false));
                }
                let retype_as_accept = v.verdict == "retype"
                    && crate::mutants::active("mcp-dream-link-retype-applied");
                // A retype is recorded, never auto-written: the corrected
                // edge waits for a reviewer.
                let applied = if (v.verdict == "accept" || retype_as_accept)
                    && v.confidence >= cfg.link_accept_min_confidence
                {
                    apply_link(c, rv, p, true, served).await?
                } else if v.verdict == "reject" && v.confidence >= cfg.link_reject_min_confidence {
                    apply_link(c, rv, p, false, served).await?
                } else {
                    false
                };
                Ok((true, applied))
            })
            .await
            .map_err(e2s)?;
            add(&mut out, "judged", outcome.0);
            add(&mut out, "applied", outcome.1);
            if outcome.1 && v.verdict != "reject" {
                review.invalidate_endpoints(p);
            }
        }
    }
    let judged = out.get("judged").and_then(Value::as_i64).unwrap_or(0);
    out.insert(
        "pending_unjudged".into(),
        json!((first.len() as i64 - judged).max(0)),
    );
    Ok(out)
}

/// `_link_two_vote_replay(out, review, rows)`: the two-vote gate over votes
/// already recorded (the two-vote mode promoted to auto after they were
/// taken), with no model call.
async fn two_vote_replay(
    ready: &Ready,
    cfg: &DeepDreamConfig,
    out: &mut Map<String, Value>,
    review: &mut Review,
    rows: &[Row],
    ex_served: Option<&str>,
) -> Result<(), ToolError> {
    let client = ready.storage.client();
    let _lock = ready.write.lock().await;
    {
        let r = &mut *review;
        reads(
            client,
            |c| async move { r.validate_recorded(c, rows).await },
        )
        .await?;
    }
    let configured2 = cfg.judge_second_model.as_deref().filter(|m| !m.is_empty());
    let mode = cfg.link_judge_mode.as_str();
    for p in rows {
        // Set before the apply reads them; only an apply uses them.
        review.second_policy = vs(p, "judge2_policy").map(str::to_string);
        review.second_model = vs(p, "judge2_served_model").map(str::to_string);
        let rv = &*review;
        let outcome = in_txn(client, |c| async move {
            if !rv.current(c, p, true, false).await? {
                return Ok((false, None));
            }
            let Some(action) = link_two_vote_action(p, cfg, mode) else {
                return Ok((false, None));
            };
            if rj::vote_endpoint_moved(c, Queue::Link, p, "second", None).await? {
                return Ok((false, None));
            }
            if !stored_second_distinct(c, rv, p, configured2).await? {
                let note = format!("{} | {TWO_VOTE_REFUSAL}", vs(p, "judge_note").unwrap_or(""));
                let refused = set_link_second_judgment(
                    c,
                    id_of(p),
                    vs(p, "judge2_verdict"),
                    vf(p, "judge2_confidence"),
                    vs(p, "judge2_model"),
                    vs(p, "judge2_relation"),
                    vf(p, "judged2_at"),
                    Some(&note),
                    None,
                    None,
                )
                .await?;
                return Ok((refused, None));
            }
            let done = apply_link(c, rv, p, action == "accept", ex_served).await?;
            Ok((false, Some((done, action))))
        })
        .await?;
        add(out, "two_vote_refused_same_model", outcome.0);
        if let Some((done, action)) = outcome.1 {
            add(out, "two_vote_applied", done);
            add(out, "applied", done);
            if done && action == "accept" {
                review.invalidate_endpoints(p);
            }
        }
    }
    Ok(())
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
