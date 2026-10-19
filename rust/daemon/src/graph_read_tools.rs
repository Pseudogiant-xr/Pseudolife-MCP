//! `memory_graph` (spec GR1-GR10): an entity's graph neighborhood, or the
//! whole graph for an empty entity. `mcp_server.memory_graph` (2647-2677) →
//! `service.graph_neighborhood` (`service.py:8289-8405`) and `_whole_graph`
//! (8224-8287), over the ported GraphStore read model
//! (`storage::graph::subgraph`, `graph_read::build_subgraph`).
//!
//! Read-only. Python holds the service lock across the call, so it never
//! sees half of a relate; the tool holds the writer lock for the same reason.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::service::Ready;
use crate::storage::graph::{self, norm_name};
use crate::store::round4;

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// `graph.MAX_DEPTH`.
const MAX_DEPTH: i64 = 3;
/// `graph_review._TAG_AMBIGUOUS_CONF`.
const TAG_AMBIGUOUS_CONF: f64 = 0.5;

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![("memory_graph", mcp_memory_graph)]
}

fn opt_str<'a>(a: &'a Map<String, Value>, k: &str) -> Option<&'a str> {
    a.get(k).and_then(Value::as_str)
}

/// `memory_graph` on the MCP surface: the service read, then the
/// `relation_filter` substring cut over a non-empty edge list.
fn mcp_memory_graph<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let entity = opt_str(a, "entity").unwrap_or("");
        let depth = a.get("depth").map_or(1, int_saturating);
        let include_facts = a
            .get("include_facts")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let to = opt_str(a, "to");
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let _ws = ready.write.lock().await;
        let mut out = graph_neighborhood(&ready, entity, depth, include_facts, to).await?;
        if let Some(rf) = opt_str(a, "relation_filter").filter(|s| !s.is_empty())
            && let Some(Value::Array(edges)) = out.get_mut("edges")
            && !edges.is_empty()
        {
            let rf = graph::py_lower(rf);
            edges.retain(|e| {
                let relation = e.get("relation").and_then(Value::as_str).unwrap_or("");
                graph::py_lower(relation).contains(&rf)
                    || crate::mutants::active("mcp-graph-no-relation-filter")
            });
        }
        Ok(out)
    })
}

/// A bound integer as `i64`, saturating past its range: Python's
/// `max(1, min(int(depth), 3))` clamps any integer the binder admits.
fn int_saturating(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| {
        if v.to_string().starts_with('-') {
            i64::MIN
        } else {
            i64::MAX
        }
    })
}

/// `classify_edge` (`memory/graph_review.py:100-113`) without a proposal:
/// user/action origins are EXTRACTED, a confidence below 0.5 AMBIGUOUS,
/// anything else INFERRED.
fn classify_edge(origin: Option<&str>, confidence: Option<f64>) -> &'static str {
    let origin = graph::py_lower(origin.unwrap_or(""));
    if origin == "user" || origin == "action" {
        return "EXTRACTED";
    }
    if confidence.is_some_and(|c| c < TAG_AMBIGUOUS_CONF)
        && !crate::mutants::active("mcp-graph-tag-no-ambiguous")
    {
        return "AMBIGUOUS";
    }
    "INFERRED"
}

/// `load_communities()["assignment"]` (`storage/postgres.py:4471-4475`).
async fn community_assignment(client: &Client) -> Result<BTreeMap<i64, i64>, ToolError> {
    crate::txn::with_client(client, |c| async move {
        Ok::<_, anyhow::Error>(
            c.query(
                "SELECT entity_id, community_id FROM entity_communities",
                &[],
            )
            .await?
            .iter()
            .map(|r| (r.get::<_, i64>(0), r.get::<_, i64>(1)))
            .collect(),
        )
    })
    .await
    .map_err(internal)
}

/// `entity_sources_map` (`storage/postgres.py:4262-4268`).
async fn entity_sources(client: &Client) -> Result<BTreeMap<i64, Vec<String>>, ToolError> {
    crate::txn::with_client(client, |c| async move {
        let mut out: BTreeMap<i64, Vec<String>> = BTreeMap::new();
        for r in c
            .query(
                "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source",
                &[],
            )
            .await?
        {
            out.entry(r.get(0)).or_default().push(r.get(1));
        }
        Ok::<_, anyhow::Error>(out)
    })
    .await
    .map_err(internal)
}

/// The entities and aliases of a loaded graph, as `alias_canonical_map`
/// takes them.
fn alias_map(entities: &[Value], aliases: &Map<String, Value>) -> BTreeMap<String, String> {
    let ents: Vec<crate::graph_read::Entity> = entities
        .iter()
        .map(|e| crate::graph_read::Entity {
            id: e["id"].as_i64().unwrap_or(0),
            canonical: e["canonical"].as_str().unwrap_or("").to_string(),
        })
        .collect();
    let al: BTreeMap<i64, Vec<String>> = aliases
        .iter()
        .filter_map(|(k, v)| {
            Some((
                k.parse().ok()?,
                v.as_array()?
                    .iter()
                    .filter_map(|s| s.as_str().map(str::to_string))
                    .collect(),
            ))
        })
        .collect();
    crate::graph_read::alias_canonical_map(&ents, &al)
}

/// The resident cortex's current facts, keyed by the canonical their entity
/// resolves to through the alias table, in record order. `labels` adds the
/// v35 `authority` / `distortion_tolerance` keys when set (the rooted path
/// only).
fn facts_by_canonical(
    ready: &Ready,
    amap: &BTreeMap<String, String>,
    labels: bool,
) -> BTreeMap<String, Vec<Value>> {
    let cortex = ready.cortex.snapshot();
    let mut out: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for rec in cortex.current_records() {
        let mut fd = Map::new();
        fd.insert("attribute".into(), json!(rec.attribute));
        fd.insert("value".into(), json!(rec.value));
        fd.insert("origin".into(), json!(rec.origin()));
        fd.insert("confidence".into(), json!(round4(rec.confidence)));
        if labels && !crate::mutants::active("mcp-graph-no-labels") {
            if let Some(a) = rec.authority.as_deref().filter(|s| !s.is_empty()) {
                fd.insert("authority".into(), json!(a));
            }
            if let Some(d) = rec
                .distortion_tolerance
                .as_deref()
                .filter(|s| !s.is_empty())
            {
                fd.insert("distortion_tolerance".into(), json!(d));
            }
        }
        let n = norm_name(&rec.entity);
        let key = if crate::mutants::active("mcp-graph-facts-no-alias") {
            n
        } else {
            amap.get(&n).cloned().unwrap_or(n)
        };
        out.entry(key).or_default().push(Value::Object(fd));
    }
    out
}

/// `service.graph_neighborhood` (`service.py:8289-8405`); an empty entity
/// is `_whole_graph(scope=None, max_nodes=None)`.
pub(crate) async fn graph_neighborhood(
    ready: &Ready,
    entity: &str,
    depth: i64,
    include_facts: bool,
    to: Option<&str>,
) -> Result<Value, ToolError> {
    if entity.is_empty() {
        return whole_graph(ready, include_facts).await;
    }
    let client = ready.storage.client();
    let comm = community_assignment(client).await?;
    let root = graph::find_entity(client, &norm_name(entity))
        .await
        .map_err(internal)?;
    if root.is_null() {
        return Ok(json!({"found": false, "entity": entity}));
    }
    let mut to_id = None;
    let mut to_missing = None;
    if let Some(to) = to.filter(|t| !t.is_empty()) {
        let t = graph::find_entity(client, &norm_name(to))
            .await
            .map_err(internal)?;
        if t.is_null() {
            to_missing = Some(to);
        } else {
            to_id = t["id"].as_i64();
        }
    }
    let root_id = root["id"].as_i64().unwrap_or(0);
    let sub = graph::subgraph(client, root_id, depth, to_id)
        .await
        .map_err(internal)?;
    let empty = Map::new();
    let by_id = sub["entities"].as_object().unwrap_or(&empty);
    let aliases = sub["aliases"].as_object().unwrap_or(&empty);
    let facts = if include_facts {
        let entities: Vec<Value> = by_id.values().cloned().collect();
        facts_by_canonical(ready, &alias_map(&entities, aliases), true)
    } else {
        BTreeMap::new()
    };
    let disp = |nid: i64| -> String {
        by_id
            .get(&nid.to_string())
            .and_then(|e| e["display"].as_str())
            .map_or_else(|| nid.to_string(), str::to_string)
    };

    let mut nodes = Vec::new();
    // `sorted(sub["nodes"])`: the subgraph's node set is ordered by id.
    for nid in sub["nodes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_i64)
    {
        let Some(e) = by_id.get(&nid.to_string()) else {
            continue;
        };
        let mut node = Map::new();
        node.insert("entity".into(), e["display"].clone());
        node.insert("canonical".into(), e["canonical"].clone());
        node.insert("etype".into(), e["etype"].clone());
        node.insert(
            "aliases".into(),
            aliases
                .get(&nid.to_string())
                .cloned()
                .unwrap_or_else(|| json!([])),
        );
        if include_facts {
            let canonical = e["canonical"].as_str().unwrap_or("");
            node.insert(
                "facts".into(),
                json!(facts.get(canonical).cloned().unwrap_or_default()),
            );
        }
        node.insert("community".into(), json!(comm.get(&nid)));
        nodes.push(Value::Object(node));
    }

    let mut edges = Vec::new();
    for e in sub["edges"].as_array().into_iter().flatten() {
        let derived = e["derived"].as_bool().unwrap_or(false);
        let mut row = Map::new();
        row.insert("src".into(), json!(disp(e["src"].as_i64().unwrap_or(0))));
        row.insert("relation".into(), e["relation"].clone());
        row.insert("dst".into(), json!(disp(e["dst"].as_i64().unwrap_or(0))));
        row.insert("derived".into(), json!(derived));
        if derived {
            row.insert("via".into(), e["via"].clone());
        } else {
            let confidence = e["confidence"].as_f64();
            row.insert(
                "confidence".into(),
                json!(round4(confidence.unwrap_or(0.0))),
            );
            let origin = e["origin"].as_str().filter(|o| !o.is_empty());
            if let Some(o) = origin {
                row.insert("origin".into(), json!(o));
            }
            row.insert("tag".into(), json!(classify_edge(origin, confidence)));
        }
        edges.push(Value::Object(row));
    }
    let paths: Vec<Vec<String>> = sub["paths"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|p| {
            p.as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_i64)
                .map(disp)
                .collect()
        })
        .collect();
    let mut result = Map::new();
    result.insert("found".into(), json!(true));
    result.insert("entity".into(), root["display"].clone());
    result.insert("depth".into(), json!(depth.clamp(1, MAX_DEPTH)));
    result.insert("nodes".into(), json!(nodes));
    result.insert("edges".into(), json!(edges));
    result.insert("paths".into(), json!(paths));
    if let Some(t) = to_missing {
        result.insert("to_not_found".into(), json!(t));
    }
    Ok(Value::Object(result))
}

/// `_whole_graph(scope=None, include_facts, max_nodes=None)`
/// (`service.py:8224-8287`): every entity and live edge, each node with its
/// sources and creation time; no cap applies on the MCP path.
async fn whole_graph(ready: &Ready, include_facts: bool) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let g = graph::load_graph(client).await.map_err(internal)?;
    let comm = community_assignment(client).await?;
    let src_map = entity_sources(client).await?;
    let empty_list = Vec::new();
    let empty = Map::new();
    let entities = g["entities"].as_array().unwrap_or(&empty_list);
    let aliases = g["aliases"].as_object().unwrap_or(&empty);
    let facts = if include_facts {
        facts_by_canonical(ready, &alias_map(entities, aliases), false)
    } else {
        BTreeMap::new()
    };
    let mut by_id: BTreeMap<i64, Value> = BTreeMap::new();
    let mut nodes = Vec::new();
    for e in entities {
        let id = e["id"].as_i64().unwrap_or(0);
        by_id.insert(id, e["display"].clone());
        let mut node = Map::new();
        node.insert("entity".into(), e["display"].clone());
        node.insert("canonical".into(), e["canonical"].clone());
        node.insert("etype".into(), e["etype"].clone());
        node.insert(
            "aliases".into(),
            aliases
                .get(&id.to_string())
                .cloned()
                .unwrap_or_else(|| json!([])),
        );
        node.insert("community".into(), json!(comm.get(&id)));
        node.insert(
            "sources".into(),
            json!(src_map.get(&id).cloned().unwrap_or_default()),
        );
        node.insert("created_at".into(), e["created_at"].clone());
        if include_facts {
            let canonical = e["canonical"].as_str().unwrap_or("");
            node.insert(
                "facts".into(),
                json!(facts.get(canonical).cloned().unwrap_or_default()),
            );
        }
        nodes.push(Value::Object(node));
    }
    let mut edges = Vec::new();
    for e in g["edges"].as_array().into_iter().flatten() {
        let (Some(src), Some(dst)) = (
            by_id.get(&e["src_id"].as_i64().unwrap_or(0)),
            by_id.get(&e["dst_id"].as_i64().unwrap_or(0)),
        ) else {
            continue;
        };
        let confidence = e["confidence"].as_f64();
        let origin = e["origin"].as_str();
        edges.push(json!({
            "src": src, "relation": e["relation"], "dst": dst, "derived": false,
            "confidence": round4(confidence.unwrap_or(0.0)),
            "origin": origin,
            "asserted_at": e["asserted_at"],
            "tag": classify_edge(origin, confidence),
        }));
    }
    let (total_nodes, total_edges) = (nodes.len(), edges.len());
    Ok(
        json!({"found": true, "entity": null, "scope": "all", "nodes": nodes,
        "edges": edges, "paths": [], "truncated": false,
        "total_nodes": total_nodes, "total_edges": total_edges}),
    )
}
