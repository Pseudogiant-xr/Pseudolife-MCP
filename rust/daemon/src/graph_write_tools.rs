//! The graph store's write tools (spec GW1-GW12): `memory_graph_relate`,
//! `memory_graph_unrelate`, `memory_alias` and `memory_relation_define`.
//! `mcp_server.py:2582-2644,2954-2981` → `service.graph_relate`,
//! `graph_unrelate`, `graph_alias` and `relation_define`
//! (`service.py:7659-7750,8146-8200`) over the ported store layer
//! (`storage/graph.rs`).
//!
//! Python holds the service lock across each call but opens no outer
//! transaction: every store call commits on its own, so a relate that mints
//! two entities and writes the edge is three commits. These bodies keep that
//! shape under the writer lock. Python keeps no resident graph, so there is
//! nothing to publish.

use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::service::Ready;
use crate::storage::graph::{self, Edge, Relation, norm_name};
use crate::storage::py_strip;
use crate::store::round4;

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![
        ("memory_graph_relate", mcp_memory_graph_relate),
        ("memory_graph_unrelate", mcp_memory_graph_unrelate),
        ("memory_alias", mcp_memory_alias),
        ("memory_relation_define", mcp_memory_relation_define),
    ]
}

fn opt_str<'a>(a: &'a Map<String, Value>, k: &str) -> Option<&'a str> {
    a.get(k).and_then(Value::as_str)
}

fn req_str<'a>(a: &'a Map<String, Value>, k: &str) -> &'a str {
    opt_str(a, k).unwrap_or("")
}

/// Run `body` detached from the request (a client disconnect must not cancel
/// a half-applied write) under the writer lock, as Python's service lock.
pub(crate) fn spawn_locked<'a, F, Fut>(
    app: &'a crate::http::App,
    args: &Map<String, Value>,
    body: F,
) -> ToolFuture<'a>
where
    F: FnOnce(Arc<Ready>, Map<String, Value>) -> Fut + Send + 'static,
    Fut: std::future::Future<Output = Result<Value, ToolError>> + Send + 'static,
{
    let args = args.clone();
    Box::pin(async move {
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move {
                let _ws = r2.write.lock().await;
                body(r2.clone(), args).await
            })
            .await
            .map_err(internal)?
    })
}

fn mcp_memory_graph_relate<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    spawn_locked(app, call.args, |ready, a| async move {
        relate(&ready, &a).await
    })
}

fn mcp_memory_graph_unrelate<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    spawn_locked(app, call.args, |ready, a| async move {
        unrelate(&ready, &a).await
    })
}

fn mcp_memory_alias<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    spawn_locked(
        app,
        call.args,
        |ready, a| async move { alias(&ready, &a).await },
    )
}

fn mcp_memory_relation_define<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    spawn_locked(app, call.args, |ready, a| async move {
        relation_define(&ready, &a).await
    })
}

/// The relation registry, in `load_relations` order (`ORDER BY name`).
async fn registry(client: &Client) -> Result<Vec<Value>, ToolError> {
    match graph::load_relations(client).await.map_err(internal)? {
        Value::Array(rows) => Ok(rows),
        _ => Ok(Vec::new()),
    }
}

fn names(registry: &[Value]) -> Vec<String> {
    registry
        .iter()
        .filter_map(|r| r.get("name").and_then(Value::as_str).map(str::to_string))
        .collect()
}

/// `find_fact_slot_entity` (`storage/postgres.py:2885-2894`): the entity of a
/// current fact whose slot key equals `key`. Python's query has no ORDER BY;
/// several owners of one key are declared free.
async fn find_fact_slot_entity(client: &Client, key: &str) -> Result<Option<String>, ToolError> {
    crate::txn::with_client(client, |c| async move {
        find_fact_slot_entity_body(c, key).await
    })
    .await
    .map_err(internal)
}

/// The statement of [`find_fact_slot_entity`], on a client the caller holds.
async fn find_fact_slot_entity_body(
    c: &Client,
    key: &str,
) -> Result<Option<String>, tokio_postgres::Error> {
    Ok(c.query_opt(
        "SELECT entity FROM facts WHERE status = 'current' AND entity_norm || '-' || attribute_norm = $1 LIMIT 1",
        &[&key],
    )
    .await?
    .map(|r| r.get::<_, String>(0)))
}

/// `_resolve_or_create_entity` (`service.py:7570-7599`) without the dream's
/// write-dedup proposals (`propose_dupes` is false on these paths). Returns
/// the node as Python's dict: `id`, `canonical`, `display`, `etype`, and
/// `created`.
pub(crate) async fn resolve_or_create(
    client: &Client,
    name: &str,
    etype: Option<&str>,
) -> Result<Value, ToolError> {
    resolve_or_create_folded(client, name, etype)
        .await
        .map(|(v, _)| v)
}

/// `resolve_or_create`, and whether the slot-key fold re-entered with the
/// owner's name (Python's recursion drops `propose_dupes` there).
pub(crate) async fn resolve_or_create_folded(
    client: &Client,
    name: &str,
    etype: Option<&str>,
) -> Result<(Value, bool), ToolError> {
    resolve_or_create_with(client, name, etype, false).await
}

/// `resolve_or_create` inside a transaction the caller holds (lesson
/// synthesis' `lesson_synthesis_transaction`, where Python's own
/// `_txn` blocks nest as savepoints): the same statements on the borrowed
/// client.
pub(crate) async fn resolve_or_create_in(
    c: &Client,
    name: &str,
    etype: Option<&str>,
) -> Result<Value, ToolError> {
    resolve_or_create_with(c, name, etype, true)
        .await
        .map(|(v, _)| v)
}

async fn resolve_or_create_with(
    client: &Client,
    name: &str,
    etype: Option<&str>,
    in_txn: bool,
) -> Result<(Value, bool), ToolError> {
    let mut name = name.to_string();
    let mut folded = false;
    loop {
        let n = norm_name(&name);
        let mut found = if in_txn {
            graph::find_entity_body(client, &n).await
        } else {
            graph::find_entity(client, &n).await
        }
        .map_err(internal)?;
        if !found.is_null() {
            // `if etype and not found.get("etype")`: the stored type is
            // backfilled (COALESCE keeps an empty string), and the answer
            // carries the caller's type either way.
            let stored_falsy = found
                .get("etype")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty);
            if let Some(t) = etype.filter(|t| !t.is_empty())
                && stored_falsy
                && !crate::mutants::active("mcp-graph-no-etype-backfill")
            {
                let canonical = found["canonical"].as_str().unwrap_or("").to_string();
                if in_txn {
                    graph::ensure_entity_body(client, &canonical, None, Some(t)).await
                } else {
                    graph::ensure_entity(client, &canonical, None, Some(t)).await
                }
                .map_err(internal)?;
                found["etype"] = json!(t);
            }
            found["created"] = json!(false);
            return Ok((found, folded));
        }
        // The slot-key fold: a name that is a current fact's slot key
        // resolves to that slot's owner.
        let owner = if crate::mutants::active("mcp-graph-no-slot-fold") {
            None
        } else if in_txn {
            find_fact_slot_entity_body(client, &n)
                .await
                .map_err(internal)?
        } else {
            find_fact_slot_entity(client, &n).await?
        };
        if let Some(owner) = owner
            && norm_name(&owner) != n
        {
            name = owner;
            folded = true;
            continue;
        }
        let display = py_strip(&name).to_string();
        let id = if in_txn {
            graph::ensure_entity_body(client, &n, Some(&display), etype).await
        } else {
            graph::ensure_entity(client, &n, Some(&display), etype).await
        }
        .map_err(internal)?;
        return Ok((
            json!({"id": id, "canonical": n, "display": display,
            "etype": etype, "aliases": [], "created": true}),
            folded,
        ));
    }
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

/// `service.graph_relate` (`service.py:7659-7713`).
pub(crate) async fn relate(ready: &Ready, a: &Map<String, Value>) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let relation = req_str(a, "relation");
    let reg = registry(client).await?;
    let (resolved, suggestions) = crate::graph_read::resolve_relation(&names(&reg), relation);
    let Some(resolved) = resolved else {
        return Ok(json!({
            "error": "unknown_relation",
            "relation": relation,
            "suggestions": suggestions,
            "hint": "Define it with memory_relation_define, or use 'related-to' as the lawful fallback.",
        }));
    };
    let src_e = resolve_or_create(client, req_str(a, "src"), opt_str(a, "src_type")).await?;
    let dst_e = resolve_or_create(client, req_str(a, "dst"), opt_str(a, "dst_type")).await?;
    let rmeta = reg
        .iter()
        .find(|r| r.get("name").and_then(Value::as_str) == Some(resolved.as_str()))
        .cloned()
        .unwrap_or(Value::Null);
    let mut warnings: Vec<String> = Vec::new();
    for (side, ent, key) in [("src", &src_e, "src_type"), ("dst", &dst_e, "dst_type")] {
        let want = rmeta.get(key).and_then(Value::as_str).unwrap_or("");
        let have = s(ent, "etype");
        if !want.is_empty()
            && !have.is_empty()
            && have != want
            && !crate::mutants::active("mcp-graph-no-warnings")
        {
            warnings.push(format!(
                "{side} '{}' has type '{have}' but relation '{resolved}' expects '{want}' — edge stored anyway",
                s(ent, "display")
            ));
        }
    }
    let confidence = a
        .get("confidence")
        .and_then(crate::mcp::binding::float_arg)
        .unwrap_or(0.8);
    let edge = graph::upsert_edge(
        client,
        Edge {
            src_id: src_e["id"].as_i64().unwrap_or(0),
            relation: &resolved,
            dst_id: dst_e["id"].as_i64().unwrap_or(0),
            confidence,
            origin: opt_str(a, "origin"),
            revive: true,
            source_entry_ids: &[],
        },
    )
    .await
    .map_err(internal)?;
    let stored = edge
        .get("confidence")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    Ok(json!({
        "src": s(&src_e, "display"),
        "relation": resolved,
        "dst": s(&dst_e, "display"),
        "confidence": round4(stored),
        "warnings": warnings,
    }))
}

/// `service.graph_unrelate` (`service.py:7729-7750`): the edge is superseded
/// (kept for audit), never deleted.
async fn unrelate(ready: &Ready, a: &Map<String, Value>) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let relation = req_str(a, "relation");
    let reg = registry(client).await?;
    let (resolved, suggestions) = crate::graph_read::resolve_relation(&names(&reg), relation);
    let Some(resolved) = resolved else {
        return Ok(json!({"error": "unknown_relation", "relation": relation,
            "suggestions": suggestions}));
    };
    let (src, dst) = (req_str(a, "src"), req_str(a, "dst"));
    let src_e = graph::find_entity(client, &norm_name(src))
        .await
        .map_err(internal)?;
    let dst_e = graph::find_entity(client, &norm_name(dst))
        .await
        .map_err(internal)?;
    if src_e.is_null() || dst_e.is_null() {
        let missing =
            if src_e.is_null() && !crate::mutants::active("mcp-graph-unrelate-missing-dst") {
                src
            } else {
                dst
            };
        return Ok(json!({"removed": false, "reason": "unknown_entity", "entity": missing}));
    }
    let removed = graph::supersede_edge(
        client,
        src_e["id"].as_i64().unwrap_or(0),
        &resolved,
        dst_e["id"].as_i64().unwrap_or(0),
    )
    .await
    .map_err(internal)?;
    Ok(
        json!({"removed": removed, "src": s(&src_e, "display"), "relation": resolved,
        "dst": s(&dst_e, "display")}),
    )
}

/// `service.graph_alias` (`service.py:8146-8163`).
async fn alias(ready: &Ready, a: &Map<String, Value>) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let alias = norm_name(req_str(a, "alias"));
    if alias.is_empty() {
        return Ok(json!({"error": "empty_alias"}));
    }
    // The owner is resolved (and possibly minted) before the canonical check,
    // so a refused alias still leaves a minted node, as in Python.
    let ent = resolve_or_create(client, req_str(a, "entity"), None).await?;
    let canonical = s(&ent, "canonical").to_string();
    if alias == canonical && !crate::mutants::active("mcp-graph-alias-canonical") {
        return Ok(json!({"error": "alias_is_canonical", "entity": s(&ent, "display")}));
    }
    graph::add_alias(client, &alias, ent["id"].as_i64().unwrap_or(0))
        .await
        .map_err(internal)?;
    let ent = graph::find_entity(client, &canonical)
        .await
        .map_err(internal)?;
    Ok(
        json!({"entity": s(&ent, "display"), "canonical": s(&ent, "canonical"),
        "aliases": ent.get("aliases").cloned().unwrap_or_else(|| json!([]))}),
    )
}

/// `service.relation_define` (`service.py:8165-8200`).
async fn relation_define(ready: &Ready, a: &Map<String, Value>) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let name = norm_name(req_str(a, "name"));
    let description = py_strip(req_str(a, "description"));
    if name.is_empty() || description.is_empty() {
        return Ok(json!({"error": "name_and_description_required"}));
    }
    let reg = registry(client).await?;
    let builtin = reg.iter().any(|r| {
        r.get("name").and_then(Value::as_str) == Some(name.as_str())
            && r.get("builtin").and_then(Value::as_bool) == Some(true)
    });
    if builtin && !crate::mutants::active("mcp-graph-redefine-builtin") {
        return Ok(json!({"error": "builtin_relation",
            "hint": format!("'{name}' is a builtin and cannot be redefined.")}));
    }
    // `if inverse_of:`: an empty string is no inverse; a blank one
    // normalizes to "" and is refused below.
    let inverse = opt_str(a, "inverse_of")
        .filter(|s| !s.is_empty())
        .map(norm_name);
    if let Some(inv) = &inverse {
        let mut known = names(&reg);
        if !known.contains(inv)
            && *inv != name
            && !crate::mutants::active("mcp-graph-inverse-unchecked")
        {
            known.sort();
            return Ok(json!({"error": "unknown_inverse", "inverse_of": inv, "known": known}));
        }
    }
    let transitive = a
        .get("transitive")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let (src_type, dst_type) = (opt_str(a, "src_type"), opt_str(a, "dst_type"));
    graph::upsert_relation(
        client,
        Relation {
            name: &name,
            description,
            src_type,
            dst_type,
            transitive,
            inverse_of: inverse.as_deref(),
        },
    )
    .await
    .map_err(internal)?;
    Ok(
        json!({"defined": name, "transitive": transitive, "inverse_of": inverse,
        "src_type": src_type, "dst_type": dst_type}),
    )
}
