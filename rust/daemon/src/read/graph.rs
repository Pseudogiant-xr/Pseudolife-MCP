//! Console graph reads, with the HTTP defaults and unabridged service bodies.
use super::console::{caller_session, i_param, tool_error_text};
use crate::http::{App, json_response};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::storage::graph as storage;
use axum::{http::HeaderMap, response::Response};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

pub(crate) const PATHS: &[&str] = &[
    "/api/graph",
    "/api/graph/path",
    "/api/graph/projects",
    "/api/graph/digest",
    "/api/graph/entity-provenance",
    "/api/recall",
    "/api/wiki",
    "/api/dream/status",
];
fn s<'a>(q: &'a HashMap<String, String>, k: &str) -> Option<&'a str> {
    q.get(k).map(String::as_str).filter(|s| !s.is_empty())
}
fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}
fn rows(v: &Value) -> &[Value] {
    v.as_array().map(Vec::as_slice).unwrap_or(&[])
}
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}

pub(crate) async fn route(
    app: &App,
    path: &str,
    q: &HashMap<String, String>,
    h: &HeaderMap,
) -> Response {
    let ready = match app.service.ensure_init().await {
        Ok(r) => r,
        Err(e) => return json_response(500, &json!({"error": e})),
    };
    let (svc, path, q) = (app.service.clone(), path.to_string(), q.clone());
    let (header_session, transport_session) = caller_session(h);
    let ident = crate::recall::Ident {
        header_session,
        transport_session,
    };
    // Finish access accounting even if the browser disconnects, as Python does.
    let task = tokio::spawn(async move {
        if path == "/api/recall" {
            return crate::recall::recall(
                &svc,
                &ready,
                s(&q, "q").unwrap_or(""),
                Some(i_param(&q, "hops", 3)),
                Some(i_param(&q, "top_k", 5)),
                None,
                &ident,
            )
            .await
            .map(Value::Object);
        }
        if path == "/api/dream/status" {
            return crate::dream::service::status(&ready, &svc.config).await;
        }
        let _ws = ready.write.lock().await;
        read(&svc, &ready, &path, &q).await
    });
    match task.await {
        Ok(Ok(v)) => json_response(200, &v),
        Ok(Err(e)) => {
            let status = if matches!(e, ToolError::Invalid(_)) {
                400
            } else {
                500
            };
            json_response(status, &json!({"error":tool_error_text(e)}))
        }
        Err(e) => json_response(500, &json!({"error":e.to_string()})),
    }
}
async fn read(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    path: &str,
    q: &HashMap<String, String>,
) -> Result<Value, ToolError> {
    let db = ready.storage.client();
    match path {
        "/api/graph" => {
            let mut v = crate::graph_read_tools::graph_neighborhood(
                ready,
                s(q, "entity").unwrap_or(""),
                i_param(q, "depth", 1),
                super::search_route::tribool(q, "include_facts") != Some(false),
                s(q, "to"),
            )
            .await?;
            if s(q, "entity").is_none() {
                filter_graph(
                    &mut v,
                    s(q, "scope").unwrap_or("all"),
                    i_param(q, "limit", 2000),
                )?;
            }
            Ok(v)
        }
        "/api/graph/digest" => {
            let v = crate::txn::with_client(db, |c| async move {
                c.query_opt("SELECT value FROM meta WHERE key=$1", &[&"graph_digest"])
                    .await
                    .map(|r| r.and_then(|r| r.get::<_, Option<Value>>(0)))
            })
            .await
            .map_err(|e| ToolError::Internal(e.into()))?;
            Ok(match v.filter(crate::board::store::py_truthy) {
                Some(v) => json!({"available":true,"digest":v}),
                None => json!({"available":false,"reason":"no_digest"}),
            })
        }
        "/api/graph/projects" => {
            let projects = crate::txn::with_client(db, |c| async move {
                c.query("SELECT source, COUNT(DISTINCT entity_id) AS entities FROM entity_sources GROUP BY source ORDER BY entities DESC, source",&[]).await
            }).await.map_err(|e| ToolError::Internal(e.into()))?;
            let roll: HashMap<String, String> = svc
                .config
                .memory
                .scopes_rollup
                .iter()
                .map(|(k, v)| {
                    (
                        storage::py_lower(crate::storage::py_strip(k)),
                        storage::py_lower(crate::storage::py_strip(v)),
                    )
                })
                .collect();
            let projects: Vec<Value> = projects
                .iter()
                .map(|r| {
                    let source: String = r.get(0);
                    let mut v = json!({"source": source, "entities": r.get::<_, i64>(1)});
                    if let Some(parent) =
                        roll.get(&source).filter(|p| !p.is_empty() && **p != source)
                    {
                        v["parent"] = json!(parent);
                    }
                    v
                })
                .collect();
            Ok(json!({"projects": projects}))
        }
        "/api/graph/path" => {
            graph_path(
                ready,
                s(q, "source"),
                s(q, "target"),
                i_param(q, "max_hops", 8),
            )
            .await
        }
        "/api/graph/entity-provenance" | "/api/wiki" => {
            let entity = s(q, "entity");
            let e = storage::find_entity(db, &storage::norm_name(entity.unwrap_or("")))
                .await
                .map_err(ToolError::Internal)?;
            if e.is_null() {
                return Ok(if path.ends_with("provenance") {
                    json!({"found":false,"entity":entity,"sources":[],"entries":[]})
                } else {
                    json!({"found":false,"entity":entity})
                });
            }
            let id = e["id"].as_i64().unwrap();
            let sources = sources(ready, id).await?;
            let entries = entries(
                ready,
                id,
                if path.ends_with("provenance") {
                    i_param(q, "limit", 20)
                } else {
                    20
                },
            )
            .await?;
            if path.ends_with("provenance") {
                Ok(json!({"found":true,"entity":e["display"],"sources":sources,"entries":entries}))
            } else {
                wiki(ready, e, sources, entries).await
            }
        }
        _ => unreachable!(),
    }
}

/// Python `_whole_graph`: scope first, then peel by current degree and name.
fn filter_graph(v: &mut Value, scope: &str, limit: i64) -> Result<(), ToolError> {
    let mut nodes = rows(&v["nodes"]).to_vec();
    if scope != "all" {
        nodes.retain(|n| rows(&n["sources"]).iter().any(|s| s == scope));
    }
    let mut alive: BTreeSet<String> = nodes
        .iter()
        .map(|n| text(&n["entity"]).to_string())
        .collect();
    let mut edges: Vec<Value> = rows(&v["edges"])
        .iter()
        .filter(|e| alive.contains(text(&e["src"])) && alive.contains(text(&e["dst"])))
        .cloned()
        .collect();
    v["total_nodes"] = json!(nodes.len());
    v["total_edges"] = json!(edges.len());
    v["scope"] = json!(scope);
    if limit != 0 && (nodes.len() as i64) > limit {
        // Python's heap peel raises IndexError after exhausting a negative cap.
        if limit < 0 {
            return Err(internal("index out of range"));
        }
        let mut adj: BTreeMap<String, BTreeSet<String>> =
            alive.iter().map(|n| (n.clone(), BTreeSet::new())).collect();
        for e in &edges {
            let (a, b) = (text(&e["src"]), text(&e["dst"]));
            if a != b {
                adj.get_mut(a).unwrap().insert(b.into());
                adj.get_mut(b).unwrap().insert(a.into());
            }
        }
        while alive.len() > limit as usize {
            let name = alive
                .iter()
                .min_by_key(|n| (adj[*n].len(), *n))
                .unwrap()
                .clone();
            alive.remove(&name);
            for nb in adj[&name].clone() {
                adj.get_mut(&nb).unwrap().remove(&name);
            }
        }
        nodes.retain(|n| alive.contains(text(&n["entity"])));
        edges.retain(|e| alive.contains(text(&e["src"])) && alive.contains(text(&e["dst"])));
        v["truncated"] = json!(true);
    }
    v["nodes"] = json!(nodes);
    v["edges"] = json!(edges);
    Ok(())
}
/// `graph_path`: undirected BFS, with stored edge orientation in the answer.
async fn graph_path(
    ready: &Ready,
    source: Option<&str>,
    target: Option<&str>,
    hops: i64,
) -> Result<Value, ToolError> {
    let db = ready.storage.client();
    let a = storage::find_entity(db, &storage::norm_name(source.unwrap_or("")))
        .await
        .map_err(ToolError::Internal)?;
    let b = storage::find_entity(db, &storage::norm_name(target.unwrap_or("")))
        .await
        .map_err(ToolError::Internal)?;
    if a.is_null() {
        return Ok(json!({"found":false,"missing":source}));
    }
    if b.is_null() {
        return Ok(json!({"found":false,"missing":target}));
    }
    let g = storage::load_graph(db).await.map_err(ToolError::Internal)?;
    let stored: Vec<crate::graph_read::StoredEdge> = rows(&g["edges"])
        .iter()
        .map(|e| crate::graph_read::StoredEdge {
            src_id: e["src_id"].as_i64().unwrap(),
            dst_id: e["dst_id"].as_i64().unwrap(),
        })
        .collect();
    let p = crate::graph_read::shortest_path(
        &stored,
        a["id"].as_i64().unwrap(),
        b["id"].as_i64().unwrap(),
        hops,
    );
    let disp: HashMap<i64, Value> = rows(&g["entities"])
        .iter()
        .map(|e| (e["id"].as_i64().unwrap(), e["display"].clone()))
        .collect();
    let label = |n: i64| {
        disp.get(&n)
            .cloned()
            .unwrap_or_else(|| json!(n.to_string()))
    };
    let rel: HashMap<(i64, i64), &Value> = rows(&g["edges"])
        .iter()
        .map(|e| {
            (
                (e["src_id"].as_i64().unwrap(), e["dst_id"].as_i64().unwrap()),
                &e["relation"],
            )
        })
        .collect();
    let mut edges = Vec::new();
    if let Some(p) = &p {
        for w in p.windows(2) {
            let (a, b) = if rel.contains_key(&(w[0], w[1])) {
                (w[0], w[1])
            } else {
                (w[1], w[0])
            };
            if let Some(r) = rel.get(&(a, b)) {
                edges.push(json!({"src":label(a),"relation":r,"dst":label(b)}));
            }
        }
    }
    Ok(
        json!({"found":true,"source":source,"target":target,"path":p.as_ref().map(|p|p.iter().map(|n|label(*n)).collect::<Vec<_>>()).unwrap_or_default(),"edges":edges,"hops":p.as_ref().map(|p|p.len()-1)}),
    )
}
/// `sources_for_entity`: attribution ordered by weight, then source name.
async fn sources(ready: &Ready, id: i64) -> Result<Vec<Value>, ToolError> {
    crate::txn::with_client(ready.storage.client(), |c| async move {
        let rows = c
            .query(
                "SELECT source, count, origin FROM entity_sources \
             WHERE entity_id=$1 ORDER BY count DESC, source",
                &[&id],
            )
            .await?;
        Ok::<_, anyhow::Error>(
            rows.iter()
                .map(|r| {
                    json!({
                        "source": r.get::<_, String>(0),
                        "count": r.get::<_, i32>(1),
                        "origin": r.get::<_, String>(2),
                    })
                })
                .collect(),
        )
    })
    .await
    .map_err(ToolError::Internal)
}

/// `entries_for_entity`: the current facts' provenance, newest first.
/// Pass negative limits through so PostgreSQL supplies Python's error text.
async fn entries(ready: &Ready, id: i64, limit: i64) -> Result<Vec<Value>, ToolError> {
    crate::txn::with_client(ready.storage.client(), |c| async move {
        let rows = c
            .query(
                "SELECT DISTINCT en.id, en.band, en.source, en.ts, en.text, en.episode_title \
             FROM facts f JOIN memory_traces t ON t.entity_norm=f.entity_norm \
             JOIN entries en ON en.id=t.entry_id \
             WHERE f.entity_id=$1 AND f.status='current' ORDER BY en.ts DESC LIMIT $2",
                &[&id, &limit],
            )
            .await?;
        Ok::<_, anyhow::Error>(
            rows.iter()
                .map(|r| {
                    json!({
                        "id": r.get::<_, i64>(0), "band": r.get::<_, String>(1),
                        "source": r.get::<_, String>(2), "ts": r.get::<_, f64>(3),
                        "text": r.get::<_, Option<String>>(4),
                        "episode_title": r.get::<_, Option<String>>(5),
                    })
                })
                .collect(),
        )
    })
    .await
    .map_err(ToolError::Internal)
}

/// `wiki_page`: an entity dossier, not a search answer (no re-verify flags).
async fn wiki(
    ready: &Ready,
    e: Value,
    projects: Vec<Value>,
    mentions: Vec<Value>,
) -> Result<Value, ToolError> {
    let db = ready.storage.client();
    let id = e["id"].as_i64().unwrap();
    let g = storage::load_graph(db).await.map_err(ToolError::Internal)?;
    let ents: Vec<crate::graph_read::Entity> = rows(&g["entities"])
        .iter()
        .map(|v| crate::graph_read::Entity {
            id: v["id"].as_i64().unwrap(),
            canonical: text(&v["canonical"]).into(),
        })
        .collect();
    let aliases: BTreeMap<i64, Vec<String>> = g["aliases"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, v)| {
            (
                k.parse().unwrap(),
                rows(v).iter().map(|v| text(v).into()).collect(),
            )
        })
        .collect();
    let amap = crate::graph_read::alias_canonical_map(&ents, &aliases);
    let belongs = |name: &str| {
        let n = storage::norm_name(name);
        amap.get(&n).map(String::as_str).unwrap_or(&n) == text(&e["canonical"])
    };
    let mut facts: Vec<Value> = ready
        .cortex
        .snapshot()
        .current_records()
        .filter(|r| belongs(&r.entity))
        .map(|r| {
            json!({
                "attribute": r.attribute, "value": r.value,
                "confidence": crate::store::round4(r.confidence), "origin": r.origin(),
                "asserted_at": r.asserted_at,
                "history_available": r.supersedes_value.is_some(),
            })
        })
        .collect();
    let mut world: Vec<Value> = ready
        .world
        .snapshot()
        .records
        .iter()
        .filter(|r| r.status == "current" && belongs(&r.entity))
        .map(|r| {
            json!({
                "attribute": r.attribute, "value": r.value,
                "confidence": crate::store::round4(r.confidence),
                "source_url": r.source_url, "retrieved_at": r.retrieved_at,
            })
        })
        .collect();
    facts.sort_by(|a, b| text(&a["attribute"]).cmp(text(&b["attribute"])));
    world.sort_by(|a, b| text(&a["attribute"]).cmp(text(&b["attribute"])));
    let nb =
        crate::graph_read_tools::graph_neighborhood(ready, text(&e["display"]), 1, false, None)
            .await?;
    let (mut outgoing, mut incoming) = (Vec::new(), Vec::new());
    for ed in rows(&nb["edges"]) {
        let derived = ed["derived"].as_bool().unwrap_or(false);
        let mut row = json!({"relation":ed["relation"],"derived":derived});
        if derived {
            row["via"] = ed["via"].clone();
        } else {
            row["confidence"] = ed["confidence"].clone();
            row["tag"] = ed["tag"].clone();
        }
        if ed["src"] == e["display"] {
            row["target"] = ed["dst"].clone();
            outgoing.push(row);
        } else if ed["dst"] == e["display"] {
            row["source"] = ed["src"].clone();
            incoming.push(row);
        }
    }
    let disp: HashMap<i64, Value> = rows(&g["entities"])
        .iter()
        .map(|e| (e["id"].as_i64().unwrap(), e["display"].clone()))
        .collect();
    let label = |v: &Value| {
        v.as_i64()
            .and_then(|id| disp.get(&id))
            .cloned()
            .unwrap_or(Value::Null)
    };
    let mut timeline = vec![
        json!({"ts":e["created_at"],"kind":"entity-created","text":format!("“{}” first seen",text(&e["display"]))}),
    ];
    for ed in rows(&g["edges"]) {
        if ed["src_id"] == id || ed["dst_id"] == id {
            timeline.push(json!({"ts":ed["asserted_at"],"kind":"edge-asserted","text":format!("{} {} {}",text(&label(&ed["src_id"])),text(&ed["relation"]),text(&label(&ed["dst_id"])))}));
        }
    }
    for f in &facts {
        timeline.push(json!({"ts":f["asserted_at"],"kind":"fact-stamped","text":format!("{} = {}",text(&f["attribute"]),text(&f["value"]))}));
    }
    for m in &mentions {
        timeline.push(json!({"ts":m["ts"],"kind":"mention","text":text(&m["text"]).chars().take(120).collect::<String>()}));
    }
    timeline.sort_by(|a, b| {
        b["ts"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&a["ts"].as_f64().unwrap_or(0.0))
    });
    timeline.truncate(30);
    let (community, ep, lp) = crate::txn::with_client(db, |c| async move {
        let community = c
            .query_opt(
                "SELECT community_id FROM entity_communities WHERE entity_id=$1",
                &[&id],
            )
            .await?
            .map(|r| r.get::<_, i64>(0));
        let ep = crate::dream::review_judgments::pending_entity_rows(c)
            .await
            .map_err(|e| anyhow::anyhow!(tool_error_text(e)))?;
        let lp = crate::dream::review_judgments::pending_rows(
            c,
            crate::dream::review_judgments::Queue::Link,
        )
        .await
        .map_err(|e| anyhow::anyhow!(tool_error_text(e)))?;
        Ok::<_, anyhow::Error>((community, ep, lp))
    })
    .await
    .map_err(ToolError::Internal)?;
    let mut flags = Vec::new();
    for p in ep {
        if p["entity_id"] == id || p["into_id"] == id {
            flags.push(json!({"kind":p["kind"],"id":p["id"],"entity":label(&p["entity_id"]),"into":label(&p["into_id"]),"reason":p["reason"],"score":p["score"]}));
        }
    }
    for p in lp {
        if p["src_id"] == id || p["dst_id"] == id {
            flags.push(json!({"kind":"proposed_link","id":p["id"],"src":label(&p["src_id"]),"relation":p["relation"],"dst":label(&p["dst_id"]),"confidence":p["confidence"]}));
        }
    }
    if projects.is_empty() {
        flags.push(json!({"kind":"unattributed"}));
    }
    Ok(
        json!({"found":true,"entity":e["display"],"canonical":e["canonical"],"etype":e["etype"],"aliases":e["aliases"],"projects":projects,"community":community,"first_seen":e["created_at"],"facts":facts,"world_facts":world,"relations":{"out":outgoing,"in":incoming},"mentions":mentions,"timeline":timeline,"flags":flags}),
    )
}
