//! `memory_graph_review` on the MCP surface. `action="list"` (the default)
//! is served: `mcp_server.memory_graph_review` (2309-2431) →
//! `service.graph_review(scope)` (`service.py:7812-7895`) over the analyzer
//! in `graph_review.rs`. The mutating actions (`propose`, `relate`,
//! `dismiss_pair` and the five id actions) live in `graph_review_actions.rs`,
//! the slot actions (`dismiss_slot_pair`, `restore_slot`) in
//! `graph_review_slots.rs`. A lesson restore that would need the
//! audit-snapshot fallback answers the declared `not_implemented` stub,
//! byte for byte what an unregistered tool answers.
//!
//! The list is read-only. Python takes the service lock in two blocks (the
//! loads, then the decision audit) and runs the analyzer between them; this
//! holds the writer lock across the whole list body instead, so it never
//! answers from two different graph states. Inside it, the two blocks keep
//! Python's order.

use std::collections::{BTreeMap, HashMap, HashSet};

use serde_json::{Map, Number, Value, json};
use tokio_postgres::Client;

use crate::config::DeepDreamConfig;
use crate::graph_review::{
    self as gr, Edge, Entity, EntityProposal, LinkProposal, ReviewInput, round3,
};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::service::Ready;
use crate::storage::graph;

#[path = "graph_review_actions.rs"]
mod actions;
#[path = "graph_review_slots.rs"]
mod slots;

const TOOL: &str = "memory_graph_review";
/// `review_decisions._INDEX_KEY`.
const DECISION_INDEX_KEY: &str = "automatic_review_decisions_v1";
/// `review_decisions._RECORD_PREFIX`.
const DECISION_RECORD_PREFIX: &str = "automatic_review_decision_v1:";
/// `review_decisions._RECENT_CAP`.
const DECISION_RECENT_CAP: usize = 100;
/// `recent_decisions(storage, limit=20)` and `recent_entity_decisions()`.
const RECENT_LIMIT: usize = 20;

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![(TOOL, mcp_memory_graph_review)]
}

/// The refusal an unregistered tool answers (`dispatch::not_implemented`),
/// as a body error: `envelope::error_payload` renders it to the same object.
fn unserved() -> ToolError {
    let stub = crate::mcp::dispatch::not_implemented(TOOL);
    let message = stub["message"].as_str().unwrap_or_default();
    ToolError::refused("not_implemented", message.strip_prefix("not_implemented: "))
}

fn mcp_memory_graph_review<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    let a = call.args;
    match a.get("action").and_then(Value::as_str).unwrap_or("list") {
        "list" => Box::pin(async move {
            let scope = a.get("scope").and_then(Value::as_str);
            let ready = app.service.ensure_init().await.map_err(internal)?;
            let _ws = ready.write.lock().await;
            graph_review(&ready, &app.service.config.memory.deep_dream, scope).await
        }),
        action @ ("dismiss_slot_pair" | "restore_slot") => match slots::plan(action, a) {
            Err(answer) => Box::pin(async move { Ok(answer) }),
            Ok(plan) => {
                let svc = app.service.clone();
                let caller = crate::episodes::Caller {
                    header_session: call.ident.header_session.clone(),
                    transport_session: call.ident.transport_session.clone(),
                };
                let writer = call.ident.writer.clone();
                Box::pin(async move {
                    let ready = svc.ensure_init().await.map_err(internal)?;
                    let r2 = ready.clone();
                    ready
                        .writes
                        .spawn(async move {
                            let ws = r2.write.lock().await;
                            slots::run(&svc, &r2, &ws, &caller, &writer, plan).await
                        })
                        .await
                        .map_err(internal)?
                })
            }
        },
        action => match actions::plan(action, a) {
            // The argument refusals `mcp_server` answers before any service call.
            Err(answer) => Box::pin(async move { Ok(answer) }),
            Ok(plan) => {
                crate::graph_write_tools::spawn_locked(app, a, move |ready, _| async move {
                    actions::run(&ready, plan).await
                })
            }
        },
    }
}

// ── storage readers (storage/postgres.py) ────────────────────────────────

/// Everything `graph_review` reads in its first locked block after
/// `load_graph`.
struct Loaded {
    /// `entity_sources_map` (4262-4268), ordered by entity id then source.
    src_map: BTreeMap<i64, Vec<String>>,
    proposals: Vec<LinkProposal>,
    entity_proposals: Vec<EntityProposal>,
    /// `review_proposal_states()["links"]`.
    link_states: HashMap<(i64, String, i64), String>,
    /// `review_proposal_states()["entities"]`, the `("merge", lo, hi)` keys
    /// (the only ones `graph_review` looks up).
    merge_states: HashMap<(i64, i64), String>,
    dismissed: HashSet<(String, String)>,
    lesson_ids: HashSet<i64>,
    fact_counts: HashMap<i64, i64>,
}

/// REAL columns go through their text form, as psycopg decodes them.
const PENDING_PROPOSALS_SQL: &str = "SELECT p.id, s.display, p.relation, d.display, \
     p.confidence::text::double precision, p.similarity::text::double precision, \
     p.rationale, p.source, p.judge_verdict, p.judge_confidence::text::double precision, \
     p.judge_note, p.judge_model, p.judge_relation, p.judge2_verdict, \
     p.judge2_confidence::text::double precision, p.judge2_model, p.judge2_relation \
     FROM edge_proposals p \
     JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id \
     WHERE p.status = 'pending' ORDER BY p.confidence DESC, p.id";

const PENDING_ENTITY_PROPOSALS_SQL: &str = "SELECT p.id, p.kind, p.entity_id, p.into_id, \
     p.score::text::double precision, p.reason, p.judge_verdict, \
     p.judge_confidence::text::double precision, p.judge_note, p.judge_model, \
     p.judge2_verdict, p.judge2_confidence::text::double precision, p.judge2_model, \
     p.judge3_verdict, p.judge3_confidence::text::double precision, p.judge3_model, \
     e.display, i.display \
     FROM entity_proposals p \
     JOIN entities e ON e.id = p.entity_id \
     LEFT JOIN entities i ON i.id = p.into_id \
     WHERE p.status = 'pending' ORDER BY p.kind, p.score DESC NULLS LAST, p.id";

async fn load_block(c: &Client) -> anyhow::Result<Loaded> {
    let mut src_map: BTreeMap<i64, Vec<String>> = BTreeMap::new();
    for r in c
        .query(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source",
            &[],
        )
        .await?
    {
        src_map.entry(r.get(0)).or_default().push(r.get(1));
    }
    // `pending_proposals` (3431-3455).
    let proposals = c
        .query(PENDING_PROPOSALS_SQL, &[])
        .await?
        .iter()
        .map(|r| LinkProposal {
            id: r.get(0),
            src: r.get(1),
            relation: r.get(2),
            dst: r.get(3),
            confidence: r.get(4),
            similarity: r.get(5),
            rationale: r.get(6),
            source: r.get(7),
            judge_verdict: r.get(8),
            judge_confidence: r.get(9),
            judge_note: r.get(10),
            judge_model: r.get(11),
            judge_relation: r.get(12),
            judge2_verdict: r.get(13),
            judge2_confidence: r.get(14),
            judge2_model: r.get(15),
            judge2_relation: r.get(16),
        })
        .collect();
    // `pending_entity_proposals` (3777-3801).
    let entity_proposals = c
        .query(PENDING_ENTITY_PROPOSALS_SQL, &[])
        .await?
        .iter()
        .map(|r| EntityProposal {
            id: r.get(0),
            kind: r.get(1),
            entity_id: Some(r.get(2)),
            into_id: r.get(3),
            score: r.get(4),
            reason: r.get(5),
            judge_verdict: r.get(6),
            judge_confidence: r.get(7),
            judge_note: r.get(8),
            judge_model: r.get(9),
            judge2_verdict: r.get(10),
            judge2_confidence: r.get(11),
            judge2_model: r.get(12),
            judge3_verdict: r.get(13),
            judge3_confidence: r.get(14),
            judge3_model: r.get(15),
            entity: r.get(16),
            into: r.get(17),
        })
        .collect();
    // `review_proposal_states` (3746-3764): unordered scans, a repeated key
    // keeping the last row, as the Python dict comprehension does.
    let mut link_states = HashMap::new();
    for r in c
        .query(
            "SELECT src_id, relation, dst_id, status FROM edge_proposals",
            &[],
        )
        .await?
    {
        link_states.insert((r.get(0), r.get(1), r.get(2)), r.get(3));
    }
    let mut merge_states = HashMap::new();
    for r in c
        .query(
            "SELECT kind, entity_id, into_id, status FROM entity_proposals",
            &[],
        )
        .await?
    {
        let kind: String = r.get(0);
        let (eid, into): (i64, Option<i64>) = (r.get(1), r.get(2));
        if kind == "merge"
            && let Some(into) = into
        {
            merge_states.insert((eid.min(into), eid.max(into)), r.get(3));
        }
    }
    // `dismissed_pairs` (3427-3429).
    let dismissed = c
        .query("SELECT a_norm, b_norm FROM dismissed_pairs", &[])
        .await?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    // `lesson_entity_ids` (4228-4238).
    let lesson_ids = c
        .query(
            "SELECT entity_id FROM lessons WHERE entity_id IS NOT NULL \
             UNION \
             SELECT object_entity_id FROM lessons \
             WHERE object_entity_id IS NOT NULL",
            &[],
        )
        .await?
        .iter()
        .map(|r| r.get(0))
        .collect();
    let fact_counts = entity_fact_counts(c).await?;
    Ok(Loaded {
        src_map,
        proposals,
        entity_proposals,
        link_states,
        merge_states,
        dismissed,
        lesson_ids,
        fact_counts,
    })
}

/// `entity_fact_counts` (4240-4247).
async fn entity_fact_counts(c: &Client) -> Result<HashMap<i64, i64>, tokio_postgres::Error> {
    Ok(c.query(
        "SELECT entity_id, COUNT(*) FROM facts \
         WHERE status = 'current' AND entity_id IS NOT NULL \
         GROUP BY entity_id",
        &[],
    )
    .await?
    .iter()
    .map(|r| (r.get(0), r.get(1)))
    .collect())
}

/// `recent_entity_decisions(limit=20)` (4070-4082).
async fn recent_entity_decisions(c: &Client) -> anyhow::Result<Vec<Value>> {
    Ok(c.query(
        "SELECT id, proposal_id, entity_display, into_display, status, \
             score::text::double precision, reason, decided_by, decided_at \
             FROM merge_decisions \
             ORDER BY decided_at DESC, id DESC LIMIT $1",
        &[&(RECENT_LIMIT as i64)],
    )
    .await?
    .iter()
    .map(|r| {
        json!({
            "id": r.get::<_, i64>(0),
            "proposal_id": r.get::<_, Option<i64>>(1),
            "entity": r.get::<_, Option<String>>(2),
            "into": r.get::<_, Option<String>>(3),
            "status": r.get::<_, String>(4),
            "score": r.get::<_, Option<f64>>(5),
            "reason": r.get::<_, Option<String>>(6),
            "decided_by": r.get::<_, Option<String>>(7),
            "decided_at": r.get::<_, f64>(8),
        })
    })
    .collect())
}

/// `merge_decision_stats()` (4050-4068).
async fn merge_decision_stats(c: &Client) -> anyhow::Result<Value> {
    let mut counts: HashMap<String, i64> = HashMap::new();
    for r in c
        .query(
            "SELECT status, count(*) FROM merge_decisions \
             WHERE decided_by <> 'dream-auto' \
               AND into_display IS NOT NULL \
               AND status IN ('accepted', 'rejected') \
             GROUP BY status",
            &[],
        )
        .await?
    {
        counts.insert(r.get(0), r.get(1));
    }
    let accepted = counts.get("accepted").copied().unwrap_or(0);
    let rejected = counts.get("rejected").copied().unwrap_or(0);
    let total = accepted + rejected;
    let rate = (total != 0).then(|| round3(accepted as f64 / total as f64));
    Ok(json!({"accepted": accepted, "rejected": rejected, "total": total, "accept_rate": rate}))
}

async fn get_meta(c: &Client, key: &str) -> anyhow::Result<Option<Value>> {
    Ok(
        c.query_opt("SELECT value FROM meta WHERE key = $1", &[&key])
            .await?
            .map(|r| r.get(0)),
    )
}

/// Python truthiness of a decoded JSON value.
fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's type name for a decoded JSON value.
fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if is_int_literal(n) => "int",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn is_int_literal(n: &Number) -> bool {
    !n.to_string().contains(['.', 'e', 'E'])
}

/// A stored JSON value as `json.loads` would hand it on: integer literals
/// stay integers (`-0` is `0`), every other number becomes a float written
/// in its shortest form, whatever spelling jsonb kept.
fn py_json(v: Value) -> Value {
    match v {
        Value::Number(n) => {
            let raw = n.to_string();
            if is_int_literal(&n) {
                if raw == "-0" {
                    json!(0)
                } else {
                    Value::Number(n)
                }
            } else {
                raw.parse::<f64>().map_or(Value::Number(n), |f| json!(f))
            }
        }
        Value::Array(a) => Value::Array(a.into_iter().map(py_json).collect()),
        Value::Object(o) => Value::Object(o.into_iter().map(|(k, v)| (k, py_json(v))).collect()),
        other => other,
    }
}

/// `value.get("version") == 1`: `1.0` and `true` pass.
fn version_is_one(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() == Some(1.0),
        _ => false,
    }
}

/// `_index(storage)["recent_ids"]` (`memory/review_decisions.py:26-36`): the
/// stored index at version 1 (`== 1`, so `1.0` and `true` pass), its
/// `recent_ids` through `list(... or [])`, last 100 kept.
fn recent_ids(index: Option<Value>) -> Result<Vec<Value>, String> {
    let Some(Value::Object(m)) = index else {
        return Ok(vec![]);
    };
    if !version_is_one(m.get("version")) {
        return Ok(vec![]);
    }
    let ids: Vec<Value> = match m.get("recent_ids") {
        None => vec![],
        Some(v) if !py_truthy(v) => vec![],
        Some(Value::Array(a)) => a.clone(),
        Some(Value::String(s)) => s.chars().map(|c| json!(c.to_string())).collect(),
        Some(Value::Object(o)) => o.keys().map(|k| json!(k)).collect(),
        Some(other) => {
            return Err(format!("'{}' object is not iterable", py_type(other)));
        }
    };
    let skip = ids.len().saturating_sub(DECISION_RECENT_CAP);
    Ok(ids.into_iter().skip(skip).collect())
}

/// `recent_decisions(storage, limit=20)` (`memory/review_decisions.py:53-68`).
async fn recent_decisions(c: &Client) -> Result<Vec<Value>, ToolError> {
    let index = get_meta(c, DECISION_INDEX_KEY).await.map_err(internal)?;
    let ids = recent_ids(index).map_err(ToolError::Invalid)?;
    let mut rows = Vec::new();
    for id in ids.iter().rev() {
        let Value::String(id) = id else {
            // `_RECORD_PREFIX + decision_id`.
            return Err(ToolError::Invalid(format!(
                "can only concatenate str (not \"{}\") to str",
                py_type(id)
            )));
        };
        let record = get_meta(c, &format!("{DECISION_RECORD_PREFIX}{id}"))
            .await
            .map_err(internal)?;
        let Some(Value::Object(record)) = record else {
            continue;
        };
        if record.is_empty() {
            continue;
        }
        let mut row = Map::new();
        for key in [
            "queue",
            "action",
            "state",
            "proposal_id",
            "pair",
            "recorded_at",
            "ended_at",
            "end_reason",
        ] {
            row.insert(
                key.into(),
                py_json(record.get(key).cloned().unwrap_or(Value::Null)),
            );
        }
        rows.push(Value::Object(row));
        if rows.len() == RECENT_LIMIT {
            break;
        }
    }
    Ok(rows)
}

// ── service.graph_review ─────────────────────────────────────────────────

/// The entities and edges of `graph::load_graph`'s answer.
fn parse_graph(g: &Value) -> (Vec<Entity>, Vec<Edge>) {
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let entities = g["entities"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| Entity {
            id: e["id"].as_i64().unwrap_or(0),
            canonical: s(&e["canonical"]),
            display: s(&e["display"]),
        })
        .collect();
    let edges = g["edges"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| Edge {
            src_id: e["src_id"].as_i64().unwrap_or(0),
            relation: s(&e["relation"]),
            dst_id: e["dst_id"].as_i64().unwrap_or(0),
            confidence: e["confidence"].as_f64().unwrap_or(0.0),
            origin: e["origin"].as_str().map(str::to_string),
        })
        .collect();
    (entities, edges)
}

/// `_fold_direction` (`service.py:8787-8799`) applied to the pending merge
/// rows: a row whose stored from-side now has more evidence (degree over the
/// full graph plus current-fact count) than its target is presented
/// swapped; ties keep the stored direction.
fn fold_merge_rows(rows: &mut [EntityProposal], edges: &[Edge], fact_counts: &HashMap<i64, i64>) {
    let evidence = merge_evidence(edges, fact_counts);
    for p in rows.iter_mut() {
        if p.kind == "merge"
            && should_fold(p.entity_id, p.into_id, &evidence)
            && !crate::mutants::active("mcp-review-no-fold-swap")
        {
            std::mem::swap(&mut p.entity_id, &mut p.into_id);
            std::mem::swap(&mut p.entity, &mut p.into);
        }
    }
}

/// The evidence `_fold_direction` weighs: degree over the given (live)
/// edges plus the current-fact count, `deg.get(eid, 0) + facts.get(eid, 0)`;
/// a NULL id reads 0.
fn merge_evidence<'a>(
    edges: &[Edge],
    fact_counts: &'a HashMap<i64, i64>,
) -> impl Fn(Option<i64>) -> i64 + 'a {
    let deg = gr::degree_counts(edges);
    move |id: Option<i64>| {
        id.map_or(0, |i| {
            deg.get(&i).copied().unwrap_or(0) as i64 + fact_counts.get(&i).copied().unwrap_or(0)
        })
    }
}

/// `_fold_direction`'s test: swap when the stored from-side strictly
/// outweighs the target; ties keep the stored direction.
fn should_fold(
    frm: Option<i64>,
    into: Option<i64>,
    evidence: &impl Fn(Option<i64>) -> i64,
) -> bool {
    evidence(frm) > evidence(into)
}

/// `MemoryService.graph_review(scope)` (`service.py:7812-7895`).
async fn graph_review(
    ready: &Ready,
    dd: &DeepDreamConfig,
    scope: Option<&str>,
) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let g = graph::load_graph(client).await.map_err(internal)?;
    let (all_entities, all_edges) = parse_graph(&g);
    let mut loaded = crate::txn::with_client(client, |c| async move { load_block(c).await })
        .await
        .map_err(internal)?;
    fold_merge_rows(
        &mut loaded.entity_proposals,
        &all_edges,
        &loaded.fact_counts,
    );

    let scoped = scope.filter(|s| !s.is_empty() && *s != "all");
    let (entities, edges): (Vec<Entity>, Vec<Edge>) = match scoped {
        Some(s) => {
            let keep: HashSet<i64> = loaded
                .src_map
                .iter()
                .filter(|(_, ss)| ss.iter().any(|x| x == s))
                .map(|(id, _)| *id)
                .collect();
            (
                all_entities
                    .into_iter()
                    .filter(|e| keep.contains(&e.id))
                    .collect(),
                all_edges
                    .into_iter()
                    .filter(|e| keep.contains(&e.src_id) && keep.contains(&e.dst_id))
                    .collect(),
            )
        }
        None => (all_entities, all_edges),
    };
    let attributed: HashSet<i64> = loaded.src_map.keys().copied().collect();
    let mut out = gr::review(&ReviewInput {
        edges: &edges,
        entities: &entities,
        attributed: &attributed,
        proposals: &loaded.proposals,
        entity_proposals: &loaded.entity_proposals,
        dismissed: &loaded.dismissed,
        lesson_entity_ids: &loaded.lesson_ids,
    })
    .map_err(ToolError::Invalid)?;

    // `{e["display"]: e["id"]}`: a repeated display keeps the last id.
    let mut by_display: HashMap<&str, i64> = HashMap::new();
    for e in &entities {
        by_display.insert(&e.display, e.id);
    }
    let mut counts: [(&str, i64); 5] = [
        ("unfiled", 0),
        ("pending", 0),
        ("gated", 0),
        ("manual", 0),
        ("terminal", 0),
    ];
    if let Some(Value::Array(findings)) = out.get_mut("findings") {
        for finding in findings.iter_mut() {
            let Value::Object(f) = finding else {
                continue;
            };
            let (state, reason) = automation(f, &by_display, &loaded, dd);
            f.insert(
                "automation".into(),
                json!({"state": state, "reason": reason}),
            );
            if let Some(slot) = counts.iter_mut().find(|(k, _)| *k == state) {
                slot.1 += 1;
            }
        }
    }
    let mut automation_counts = Map::new();
    for (k, n) in counts {
        automation_counts.insert(k.into(), json!(n));
    }
    out.insert("automation".into(), Value::Object(automation_counts));

    // The second locked block.
    let (recent, stats) = crate::txn::with_client(client, |c| async move {
        Ok::<_, anyhow::Error>((
            recent_entity_decisions(c).await?,
            merge_decision_stats(c).await?,
        ))
    })
    .await
    .map_err(internal)?;
    out.insert("recent_merges".into(), json!(recent));
    out.insert("merge_decision_stats".into(), stats);
    let decisions = crate::txn::with_client(client, |c| async move {
        Ok::<_, tokio_postgres::Error>(recent_decisions(c).await)
    })
    .await
    .map_err(internal)??;
    out.insert("automatic_decisions".into(), json!(decisions));
    Ok(Value::Object(out))
}

/// One finding's `{"state", "reason"}` (`service.py:7855-7889`).
fn automation(
    f: &Map<String, Value>,
    by_display: &HashMap<&str, i64>,
    loaded: &Loaded,
    dd: &DeepDreamConfig,
) -> (&'static str, &'static str) {
    let ftype = f.get("type").and_then(Value::as_str).unwrap_or("");
    if matches!(
        ftype,
        "proposed_link" | "merge_candidate" | "junk_candidate"
    ) {
        return ("pending", "filed for review");
    }
    if ftype != "duplicate" {
        return ("manual", "no automated filing path");
    }
    let names: Vec<&str> = f
        .get("entities")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    let id = |i: usize| names.get(i).and_then(|n| by_display.get(n).copied());
    let status = match (id(0), id(1)) {
        (Some(left), Some(right)) => {
            if f.get("action").and_then(Value::as_str) == Some("relate")
                && !crate::mutants::active("mcp-review-relate-state-miss")
            {
                let relation = f
                    .get("suggested_relation")
                    .and_then(Value::as_str)
                    .filter(|r| !r.is_empty())
                    .unwrap_or("related-to");
                loaded.link_states.get(&(left, relation.to_string(), right))
            } else {
                loaded.merge_states.get(&(left.min(right), left.max(right)))
            }
        }
        _ => None,
    };
    match status {
        Some(s) if s == "pending" || crate::mutants::active("mcp-review-terminal-as-pending") => {
            ("pending", "filed for review")
        }
        Some(_) => ("terminal", "already decided; proposal prevents refiling"),
        None if !dd.judges_enabled => ("gated", "review-queue judges disabled"),
        None if !dd.analyzer_file_duplicates => ("gated", "analyzer filing disabled"),
        None => ("unfiled", "awaiting bounded analyzer scan"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unserved_actions_answer_the_unregistered_stub() {
        // `envelope::error_payload` renders a refusal with a bare code and no
        // extras as `{"error": code, "message": "code: detail"}`: the stub.
        let stub = crate::mcp::dispatch::not_implemented(TOOL);
        let ToolError::Refused {
            code,
            detail: Some(detail),
            param: None,
            accepted: None,
            action: None,
        } = unserved()
        else {
            panic!("not a bare refusal");
        };
        let payload = json!({"error": code, "message": format!("{code}: {detail}")});
        assert_eq!(
            serde_json::to_string(&payload).unwrap(),
            serde_json::to_string(&stub).unwrap()
        );
    }

    #[test]
    fn decision_index_reads_like_python() {
        // Version gate: `!= 1` admits 1.0 and true, not 2 or a missing key.
        for (index, n) in [
            (json!({"version": 1, "recent_ids": ["1", "2"]}), 2),
            (json!({"version": 1.0, "recent_ids": ["1"]}), 1),
            (json!({"version": true, "recent_ids": ["1"]}), 1),
            (json!({"version": 2, "recent_ids": ["1"]}), 0),
            (json!({"recent_ids": ["1"]}), 0),
            (json!({"version": 1, "recent_ids": null}), 0),
            (json!({"version": 1, "recent_ids": "ab"}), 2),
            (json!(["x"]), 0),
        ] {
            assert_eq!(recent_ids(Some(index.clone())).unwrap().len(), n, "{index}");
        }
        let many: Vec<String> = (0..150).map(|i| i.to_string()).collect();
        let ids = recent_ids(Some(json!({"version": 1, "recent_ids": many}))).unwrap();
        assert_eq!(ids.len(), 100);
        assert_eq!(ids[0], json!("50"));
        assert!(matches!(
            recent_ids(Some(json!({"version": 1, "recent_ids": 5}))),
            Err(m) if m == "'int' object is not iterable"
        ));
    }

    #[test]
    fn stored_numbers_read_back_as_python_numbers() {
        let raw: Value =
            serde_json::from_str(r#"{"a": 0.00001, "b": -0, "c": 1.10, "d": [1E2, 7]}"#).unwrap();
        // The same text a float the daemon computes would serialize to.
        let want = json!({"a": 1e-5, "b": 0, "c": 1.1, "d": [100.0, 7]});
        assert_eq!(
            serde_json::to_string(&py_json(raw)).unwrap(),
            serde_json::to_string(&want).unwrap()
        );
    }

    fn row(kind: &str, id: i64, from: i64, into: i64) -> EntityProposal {
        EntityProposal {
            id,
            kind: kind.into(),
            entity_id: Some(from),
            into_id: Some(into),
            entity: Some(format!("e{from}")),
            into: Some(format!("e{into}")),
            ..Default::default()
        }
    }

    #[test]
    fn fold_swaps_only_when_the_source_outweighs_the_target() {
        let edges: Vec<Edge> = [(1, 3), (1, 4), (2, 3)]
            .iter()
            .map(|&(s, d)| Edge {
                src_id: s,
                relation: "uses".into(),
                dst_id: d,
                confidence: 0.8,
                origin: None,
            })
            .collect();
        let facts = HashMap::from([(2, 1)]);
        // 1: degree 2; 2: degree 1 + 1 fact (a tie); 5: nothing. Oracle:
        // `MemoryService._fold_direction` with evidence {1: 2, 2: 2, 5: 0}
        // gives (1,5)->(5,1), (2,1)->(2,1), (1,2)->(1,2); junk rows never fold.
        let mut rows = vec![
            row("merge", 1, 1, 5),
            row("merge", 2, 2, 1),
            row("merge", 3, 1, 2),
            row("junk", 4, 1, 5),
        ];
        fold_merge_rows(&mut rows, &edges, &facts);
        let got: Vec<(Option<i64>, Option<i64>, Option<String>)> = rows
            .iter()
            .map(|p| (p.entity_id, p.into_id, p.entity.clone()))
            .collect();
        assert_eq!(
            got,
            vec![
                (Some(5), Some(1), Some("e5".into())),
                (Some(2), Some(1), Some("e2".into())),
                (Some(1), Some(2), Some("e1".into())),
                (Some(1), Some(5), Some("e1".into())),
            ]
        );
    }

    #[test]
    fn automation_states_follow_service_order() {
        let loaded = Loaded {
            src_map: BTreeMap::new(),
            proposals: vec![],
            entity_proposals: vec![],
            link_states: HashMap::from([((1, "implements".to_string(), 2), "pending".to_string())]),
            merge_states: HashMap::from([((3, 4), "rejected".to_string())]),
            dismissed: HashSet::new(),
            lesson_ids: HashSet::new(),
            fact_counts: HashMap::new(),
        };
        let by_display: HashMap<&str, i64> =
            HashMap::from([("f.py", 1), ("f", 2), ("a", 4), ("b", 3), ("c", 5)]);
        let on = DeepDreamConfig::default();
        let finding = |v: Value| v.as_object().unwrap().clone();
        let relate = finding(json!({"type": "duplicate", "entities": ["f.py", "f"],
            "action": "relate", "suggested_relation": "implements"}));
        let decided =
            finding(json!({"type": "duplicate", "entities": ["a", "b"], "action": "merge"}));
        let open = finding(json!({"type": "duplicate", "entities": ["a", "c"], "action": "merge"}));
        let state = |f: &Map<String, Value>, dd: &DeepDreamConfig| {
            automation(f, &by_display, &loaded, dd).0
        };
        assert_eq!(state(&relate, &on), "pending");
        // The merge key is the unordered pair.
        assert_eq!(state(&decided, &on), "terminal");
        assert_eq!(state(&open, &on), "unfiled");
        let judges_off = DeepDreamConfig {
            judges_enabled: false,
            ..on.clone()
        };
        let filing_off = DeepDreamConfig {
            analyzer_file_duplicates: false,
            ..on.clone()
        };
        assert_eq!(
            automation(&open, &by_display, &loaded, &judges_off),
            ("gated", "review-queue judges disabled")
        );
        assert_eq!(
            automation(&open, &by_display, &loaded, &filing_off),
            ("gated", "analyzer filing disabled")
        );
        // A decided pair stays terminal whatever the switches say.
        assert_eq!(state(&decided, &judges_off), "terminal");
        for (t, want) in [
            ("merge_candidate", "pending"),
            ("orphan", "manual"),
            ("test_artifact", "manual"),
        ] {
            assert_eq!(state(&finding(json!({"type": t})), &on), want);
        }
    }
}
