//! Console graph actions and review, over the shared graph services.

use super::console::Request;
use crate::graph_review_tools::{actions, slots};
use crate::http::json_response;
use crate::mcp::dispatch::ToolError;
use crate::service::Ready;
use crate::storage::graph;
use axum::response::Response;
use serde_json::{Map, Value, json};

const POSTS: &[(&str, &[&str])] = &[
    ("rejudge", &[]),
    ("assign-scope", &["entity", "source"]),
    ("unrelate", &["src", "relation", "dst"]),
    ("relate", &["src", "relation", "dst"]),
    ("bless-edge", &["src", "relation", "dst"]),
    ("dismiss-duplicate", &["a", "b"]),
    ("delete-entity", &["entity"]),
    ("merge", &["from", "into"]),
    ("accept-proposal", &["id"]),
    ("reject-proposal", &["id"]),
    ("accept-entity-merge", &["id"]),
    ("accept-entity-junk", &["id"]),
    ("reject-entity-proposal", &["id"]),
];

#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("POST", "/api/graph/rejudge"),
    ("POST", "/api/graph/assign-scope"),
    ("POST", "/api/graph/unrelate"),
    ("POST", "/api/graph/relate"),
    ("POST", "/api/graph/bless-edge"),
    ("POST", "/api/graph/dismiss-duplicate"),
    ("POST", "/api/graph/delete-entity"),
    ("POST", "/api/graph/merge"),
    ("POST", "/api/graph/accept-proposal"),
    ("POST", "/api/graph/reject-proposal"),
    ("POST", "/api/graph/accept-entity-merge"),
    ("POST", "/api/graph/accept-entity-junk"),
    ("POST", "/api/graph/reject-entity-proposal"),
    ("POST", "/api/curation/dismiss-duplicate"),
    ("GET", "/api/graph/review"),
    ("GET", "/api/curation/duplicates"),
];

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|n| n != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn text<'a>(a: &'a Map<String, Value>, key: &str) -> &'a str {
    a.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Python's `(value or "").strip()` errors, checked at the service's use site.
fn check_text(a: &Map<String, Value>, keys: &[&str]) -> Result<(), String> {
    for key in keys {
        let v = &a[*key];
        if !v.is_string() && truthy(v) {
            let kind = match v {
                Value::Bool(_) => "bool",
                Value::Number(n) if n.is_i64() || n.is_u64() => "int",
                Value::Number(_) => "float",
                Value::Array(_) => "list",
                _ => "dict",
            };
            return Err(format!("'{kind}' object has no attribute 'strip'"));
        }
    }
    Ok(())
}

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    let path = req.path;
    let action = path.strip_prefix("/api/graph/").unwrap_or("");
    let fields = if req.method == "POST" && path == "/api/curation/dismiss-duplicate" {
        &[
            "store",
            "a_entity",
            "a_attribute",
            "b_entity",
            "b_attribute",
        ][..]
    } else if req.method == "POST" {
        POSTS.iter().find(|(name, _)| *name == action)?.1
    } else if req.method == "GET"
        && (path == "/api/graph/review" || path == "/api/curation/duplicates")
    {
        &[]
    } else {
        return None;
    };
    // Python's route lambda raises KeyError, which api.py maps to 405.
    if fields.iter().any(|k| !req.body.contains_key(*k)) {
        return Some(json_response(
            405,
            &json!({"error":"method_not_allowed", "path":path}),
        ));
    }
    let mut args = Map::new();
    for &key in fields {
        args.insert(key.into(), req.body[key].clone());
    }
    if action == "rejudge" {
        args = req.body.clone();
    }
    let actor = if req.body.get("decided_by").and_then(Value::as_str) == Some("agent") {
        "agent"
    } else {
        "human"
    };
    let scope = req.params.get("scope").filter(|s| !s.is_empty()).cloned();
    let path = path.to_string();
    let cfg = req.app.service.config.memory.deep_dream.clone();
    let result =
        crate::graph_write_tools::spawn_locked(req.app, &args, move |ready, a| async move {
            match path.as_str() {
                "/api/graph/review" => {
                    crate::graph_review_tools::graph_review(&ready, &cfg, scope.as_deref()).await
                }
                "/api/curation/duplicates" => {
                    crate::dream::curation_judge::refresh_auto_dismissals(
                        ready.storage.client(),
                        &ready,
                        None,
                    )
                    .await?;
                    duplicates(&ready, &cfg).await
                }
                "/api/curation/dismiss-duplicate" => {
                    if !matches!(text(&a, "store"), "lesson" | "world") {
                        return Ok(
                            json!({"dismissed":false,"reason":"bad_store","store":a["store"]}),
                        );
                    }
                    check_text(&a, &["a_entity", "a_attribute", "b_entity", "b_attribute"])
                        .map_err(internal)?;
                    slots::dismiss(
                        &ready,
                        text(&a, "store"),
                        &(text(&a, "a_entity").into(), text(&a, "a_attribute").into()),
                        &(text(&a, "b_entity").into(), text(&a, "b_attribute").into()),
                    )
                    .await
                }
                _ => {
                    write(
                        &ready,
                        &cfg,
                        path.trim_start_matches("/api/graph/"),
                        &a,
                        actor,
                    )
                    .await
                }
            }
        })
        .await;
    Some(match result {
        Ok(value) => json_response(200, &value),
        Err(ToolError::Invalid(message)) => json_response(400, &json!({"error":message})),
        Err(ToolError::Internal(error)) => json_response(500, &json!({"error":error.to_string()})),
        Err(error) => json_response(500, &json!({"error":format!("{error:?}")})),
    })
}

async fn duplicates(
    ready: &Ready,
    cfg: &crate::config::DeepDreamConfig,
) -> Result<Value, ToolError> {
    let dismissed = crate::dream::deep::dismissed_pairs(ready.storage.client())
        .await
        .map_err(internal)?;
    let (lessons, world) = crate::dream::deep::curation_records(ready, cfg.snippet_max_chars);
    let one = |records: &[_], store| {
        crate::graph_consolidation::slot_duplicate_candidates(
            records,
            cfg.curation_min_similarity,
            cfg.curation_top_k,
            &crate::dream::deep::store_dismissed(&dismissed, store),
        )
    };
    Ok(json!({"lesson_duplicates":one(&lessons,"lesson"), "world_duplicates":one(&world,"world")}))
}

async fn write(
    ready: &Ready,
    cfg: &crate::config::DeepDreamConfig,
    action: &str,
    a: &Map<String, Value>,
    actor: &str,
) -> Result<Value, ToolError> {
    let c = ready.storage.client();
    match action {
        "merge" => check_text(a, &["from", "into"]).map_err(internal)?,
        "delete-entity" | "assign-scope" => check_text(a, &["entity"]).map_err(internal)?,
        "dismiss-duplicate" => check_text(a, &["a", "b"]).map_err(internal)?,
        "relate" | "unrelate" | "bless-edge" => {
            check_text(a, &["relation"]).map_err(internal)?;
            let registry = graph::load_relations(c).await.map_err(internal)?;
            let known = registry
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|r| r["name"].as_str().map(str::to_string))
                .collect::<Vec<_>>();
            let (relation, suggestions) =
                crate::graph_read::resolve_relation(&known, text(a, "relation"));
            if relation.is_none() {
                let mut out = json!({"error":"unknown_relation","relation":a["relation"],"suggestions":suggestions});
                if action == "relate" {
                    out["hint"] = json!(
                        "Define it with memory_relation_define, or use 'related-to' as the lawful fallback."
                    );
                }
                return Ok(out);
            }
            if action != "relate" {
                check_text(a, &["src", "dst"]).map_err(internal)?;
            }
        }
        _ => {}
    }
    match action {
        "relate" => return crate::graph_write_tools::relate_console(ready, a).await,
        "unrelate" => {
            let mut out = crate::graph_write_tools::unrelate(ready, a).await?;
            if out.get("reason").and_then(Value::as_str) == Some("unknown_entity") {
                out["entity"] = if out["entity"].as_str() == Some(text(a, "src")) {
                    a["src"].clone()
                } else {
                    a["dst"].clone()
                };
            }
            return Ok(out);
        }
        "dismiss-duplicate" => {
            let mut out = actions::dismiss_duplicate(ready, text(a, "a"), text(a, "b")).await?;
            out["a"] = a["a"].clone();
            out["b"] = a["b"].clone();
            return Ok(out);
        }
        "rejudge" => return rejudge(ready, cfg, a).await,
        _ => {}
    }
    let decision = match action {
        "accept-proposal" => Some("accept_link"),
        "reject-proposal" => Some("reject_link"),
        "accept-entity-merge" => Some("accept_merge"),
        "accept-entity-junk" => Some("accept_junk"),
        "reject-entity-proposal" => Some("reject_entity"),
        _ => None,
    };
    if let Some(decision) = decision {
        return actions::console_decide(ready, decision, a["id"].clone(), actor).await;
    }
    if action == "bless-edge" {
        let relations = graph::load_relations(c).await.map_err(internal)?;
        let known = relations
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|r| r["name"].as_str().map(str::to_string))
            .collect::<Vec<_>>();
        let (resolved, suggestions) =
            crate::graph_read::resolve_relation(&known, text(a, "relation"));
        let Some(relation) = resolved else {
            return Ok(
                json!({"error":"unknown_relation","relation":a["relation"],"suggestions":suggestions}),
            );
        };
        let src = graph::find_entity(c, &graph::norm_name(text(a, "src")))
            .await
            .map_err(internal)?;
        let dst = graph::find_entity(c, &graph::norm_name(text(a, "dst")))
            .await
            .map_err(internal)?;
        if src.is_null() || dst.is_null() {
            return Ok(
                json!({"blessed":false,"reason":"unknown_entity","entity":if src.is_null(){&a["src"]}else{&a["dst"]}}),
            );
        }
        let ok = graph::bless_edge(
            c,
            src["id"].as_i64().unwrap(),
            &relation,
            dst["id"].as_i64().unwrap(),
            0.8,
        )
        .await
        .map_err(internal)?;
        let mut out =
            json!({"blessed":ok,"src":src["display"],"relation":relation,"dst":dst["display"]});
        if !ok {
            out["reason"] = json!("edge_not_found");
        }
        return Ok(out);
    }
    if action == "merge" {
        let from = graph::find_entity(c, &graph::norm_name(text(a, "from")))
            .await
            .map_err(internal)?;
        let into = graph::find_entity(c, &graph::norm_name(text(a, "into")))
            .await
            .map_err(internal)?;
        if from.is_null() || into.is_null() {
            return Ok(
                json!({"merged":false,"reason":"unknown_entity","from":a["from"],"into":a["into"]}),
            );
        }
        if from["id"] == into["id"] {
            return Ok(json!({"merged":false,"reason":"same_entity","into":into["display"]}));
        }
        let ok = graph::merge_entity(
            c,
            from["id"].as_i64().unwrap(),
            into["id"].as_i64().unwrap(),
        )
        .await
        .map_err(internal)?;
        return Ok(json!({"merged":ok,"from":from["display"],"into":into["display"]}));
    }
    let entity = graph::find_entity(c, &graph::norm_name(text(a, "entity")))
        .await
        .map_err(internal)?;
    let key = if action == "assign-scope" {
        "assigned"
    } else {
        "deleted"
    };
    if entity.is_null() {
        return Ok(json!({key:false,"reason":"unknown_entity","entity":a["entity"]}));
    }
    let id = entity["id"].as_i64().unwrap();
    if action == "assign-scope" {
        if a["source"].is_object() {
            return Err(internal(
                "cannot adapt type 'dict' using placeholder '%s' (format: AUTO)",
            ));
        }
        let source = actions::text_param(a.get("source"))?;
        actions::in_txn(c, |c| async move {
            c.execute("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) VALUES ($1,$2,1,'manual',$3) ON CONFLICT (entity_id,source) DO UPDATE SET count=entity_sources.count+1, updated_at=EXCLUDED.updated_at, origin='manual'", &[&id,&source,&crate::episodes::now()]).await.map_err(internal)?;
            Ok(())
        }).await?;
        Ok(json!({"assigned":true,"entity":entity["display"],"source":a["source"]}))
    } else {
        let ok = graph::delete_entity(c, id).await.map_err(internal)?;
        Ok(json!({"deleted":ok,"entity":entity["display"]}))
    }
}

/// Bounded pending-opinion reset; terminal decisions remain closed.
async fn rejudge(
    ready: &Ready,
    cfg: &crate::config::DeepDreamConfig,
    a: &Map<String, Value>,
) -> Result<Value, ToolError> {
    const KINDS: [&str; 5] = ["merge", "link", "junk", "curation", "candidate"];
    let queue = a.get("queue").cloned().unwrap_or(json!("all"));
    let q = queue.as_str().unwrap_or("");
    if q != "all" && !KINDS.contains(&q) {
        return Ok(json!({"error":"unknown_queue","queues":KINDS}));
    }
    let limit = a.get("limit").cloned().unwrap_or(json!(32));
    let Some(limit) = limit.as_i64().filter(|n| (1..=100).contains(n)) else {
        return Ok(json!({"error":"limit_must_be_between_1_and_100"}));
    };
    actions::in_txn(ready.storage.client(), |c| async move {
        bounded_requeue(q, limit, |kind, left| async move {
            let count=if kind=="candidate" {
                let mut memo=meta(c,"deep_candidate_verdicts").await?;
                let mut pairs=memo.get("pairs").and_then(Value::as_object).cloned().unwrap_or_default();
                let mut keys=pairs.keys().cloned().collect::<Vec<_>>();keys.sort();keys.truncate(left as usize);
                for key in &keys {pairs.remove(key);}
                memo["pairs"]=json!(pairs); set_meta(c,"deep_candidate_verdicts",&memo).await?;
                let mut done=meta(c,"deep_candidates_judged").await?;
                done["complete"]=json!(false);set_meta(c,"deep_candidates_judged",&done).await?;
                keys.len() as i64
            } else if kind=="curation" {
                let candidates = duplicates(ready, cfg).await?;
                let mut n=0;
                for store in ["lesson","world"] {
                    for pair in candidates[format!("{store}_duplicates")].as_array().unwrap() {
                        if n>=left {break;}
                        let mut keys=[pair["a_key"].as_str().unwrap(),pair["b_key"].as_str().unwrap()];keys.sort();
                        n+=c.execute("DELETE FROM curation_judgments WHERE store=$1 AND a_key=$2 AND b_key=$3",&[&store,&keys[0],&keys[1]]).await.map_err(internal)? as i64;
                    }
                }
                n
            } else {
                let rows=if kind=="link" {
                    c.query("SELECT id FROM edge_proposals WHERE status='pending' AND judge_verdict IS NOT NULL AND judge_verdict<>'' ORDER BY confidence DESC,id LIMIT $1",&[&left]).await
                } else {
                    c.query("SELECT id FROM entity_proposals WHERE status='pending' AND kind=$1 AND judge_verdict IS NOT NULL AND judge_verdict<>'' ORDER BY kind, score DESC NULLS LAST,id LIMIT $2",&[&kind,&left]).await
                }.map_err(internal)?;
                let key=format!("review_judgments_v1_{kind}");
                let mut memo=meta(c,&key).await?;
                for row in &rows {
                    let id:i64=row.get(0);
                    let sql=if kind=="link" {
                        "UPDATE edge_proposals SET judge_verdict=NULL,judge_confidence=NULL,judge_note=NULL,judge_model=NULL,judged_at=NULL,judge_relation=NULL,judge_served_model=NULL,judge2_verdict=NULL,judge2_confidence=NULL,judge2_model=NULL,judged2_at=NULL,judge2_relation=NULL,judge2_served_model=NULL,judge2_policy=NULL WHERE id=$1 AND status='pending'"
                    } else {
                        "UPDATE entity_proposals SET judge_verdict=NULL,judge_confidence=NULL,judge_note=NULL,judge_model=NULL,judged_at=NULL,judge_served_model=NULL,judge2_verdict=NULL,judge2_confidence=NULL,judge2_model=NULL,judged2_at=NULL,judge3_verdict=NULL,judge3_confidence=NULL,judge3_model=NULL,judged3_at=NULL,judge2_served_model=NULL,judge2_policy=NULL,judge3_served_model=NULL,judge3_policy=NULL WHERE id=$1 AND status='pending'"
                    };
                    c.execute(sql,&[&id]).await.map_err(internal)?;
                    memo.as_object_mut().unwrap().remove(&id.to_string());
                }
                set_meta(c,&key,&memo).await?;
                rows.len() as i64
            };
            Ok(count)
        }).await
    }).await
}

async fn meta(c: &tokio_postgres::Client, key: &str) -> Result<Value, ToolError> {
    let row = c
        .query_opt("SELECT value FROM meta WHERE key=$1", &[&key])
        .await
        .map_err(internal)?;
    Ok(row
        .map(|r| r.get::<_, Value>(0))
        .filter(|v| !v.is_null())
        .unwrap_or(json!({})))
}
async fn set_meta(c: &tokio_postgres::Client, key: &str, value: &Value) -> Result<(), ToolError> {
    c.execute("INSERT INTO meta(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",&[&key,&value]).await.map_err(internal)?;
    Ok(())
}

/// Invoke each queue only while capacity remains. In particular, do not
/// construct the quadratic slot-candidate listing for an exhausted budget.
async fn bounded_requeue<F, Fut>(queue: &str, limit: i64, mut reset: F) -> Result<Value, ToolError>
where
    F: FnMut(&'static str, i64) -> Fut,
    Fut: std::future::Future<Output = Result<i64, ToolError>>,
{
    let mut left = limit;
    let mut counts = Map::new();
    for kind in ["merge", "link", "junk", "curation", "candidate"] {
        if left == 0 {
            break;
        }
        if queue != "all" && queue != kind {
            continue;
        }
        let count = reset(kind, left).await?;
        counts.insert(kind.into(), json!(count));
        left -= count;
    }
    Ok(json!({"requeued":limit-left,"queues":counts,"limit":limit}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn exhausted_rejudge_budget_never_enumerates_curation() {
        let mut called = Vec::new();
        let out = bounded_requeue("all", 3, |kind, left| {
            assert_ne!(
                kind, "curation",
                "exhausted budget must not enumerate slot pairs"
            );
            called.push((kind, left));
            std::future::ready(Ok(1))
        })
        .await
        .unwrap();
        assert_eq!(called, [("merge", 3), ("link", 2), ("junk", 1)]);
        assert_eq!(
            out,
            json!({"requeued":3,"queues":{"merge":1,"link":1,"junk":1},"limit":3})
        );
        let mut called = Vec::new();
        bounded_requeue("curation", 2, |kind, left| {
            called.push((kind, left));
            std::future::ready(Ok(1))
        })
        .await
        .unwrap();
        assert_eq!(called, [("curation", 2)]);
    }
}
