//! `memory_agents` and `memory_message` on the MCP surface
//! (`mcp_server.memory_agents` → `coordination.agents`,
//! `mcp_server.memory_message` → `coordination.dispatch`). Both run the
//! board's own dispatch, outside the REST hub's admission limit, in a task
//! a disconnecting caller cannot cancel (Python runs them on a worker
//! thread). Spec W2-F "Board tools over MCP".

use super::dispatch::unexpected;
use super::error::BoardError;
use super::http::header_dict;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use serde_json::{Map, Value, json};
use std::collections::HashMap;

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("memory_agents", mcp_memory_agents),
        ("memory_message", mcp_memory_message),
    ]
}

/// What each memory_agents action takes, by the tool's parameter names
/// (`coordination._AGENT_PARAMETERS`), in its declared order.
const AGENT_PARAMETERS: &[(&str, &[&str])] = &[
    ("list", &["project", "task"]),
    (
        "update",
        &[
            "project",
            "task",
            "status",
            "expect",
            "children",
            "name",
            "park_reason",
            "park_needs",
            "park_clear_by",
            "park_resume",
            "park_expires",
        ],
    ),
    (
        "claim",
        &[
            "lease",
            "worktree",
            "repository_id",
            "path",
            "status",
            "expect",
        ],
    ),
    ("release", &["lease", "worktree", "repository_id", "path"]),
];

/// `CLAIM_LEASE_TTL`, `SESSION_LEASE_TTL`.
const CLAIM_LEASE_TTL: i64 = 86400;
const SESSION_LEASE_TTL: i64 = 3600;

/// A board refusal as the MCP error payload builds it from the
/// `CoordinationRefused` Python raises (`_error_payload`).
fn tool_error(e: BoardError) -> ToolError {
    if let BoardError::Internal(text) = &e {
        eprintln!("coordination tool failed: {text}");
    }
    let r = e.public();
    ToolError::Refused {
        code: r.code,
        detail: r.detail,
        param: r.param.map(Value::String),
        accepted: r.accepted.map(|a| json!(a)),
        action: r.action.map(Value::String),
    }
}

/// `coordination.dispatch` with the request's headers; the principal is
/// `authenticated_principal`'s (a configured bearer, never open-loopback
/// naming), as `_dispatch` resolves it when the tool passes none.
async fn dispatch(
    app: &crate::http::App,
    call: &ToolCall<'_>,
    action: &str,
    params: Map<String, Value>,
) -> Result<Value, ToolError> {
    let board = app.board.clone();
    let headers = header_dict(&call.ident.headers);
    let principal = super::http::authenticated_principal(app, &headers).map_err(tool_error)?;
    let action = action.to_string();
    tokio::spawn(async move { board.dispatch(&action, params, &headers, &principal).await })
        .await
        .map_err(|e| ToolError::Internal(anyhow::anyhow!(e)))?
        .map_err(tool_error)
}

fn mcp_memory_message<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let action = a.get("action").and_then(Value::as_str).unwrap_or("send");
        let mut params = Map::new();
        for key in [
            "to",
            "text",
            "request_id",
            "reply_to",
            "after",
            "message_id",
            "clears",
            "urgent",
            "limit",
            "peer",
            "ack",
        ] {
            let value = a.get(key).cloned().unwrap_or(Value::Null);
            // `urgent or None`, `ack or None`: false is never sent.
            let value = match key {
                "urgent" | "ack" if value != Value::Bool(true) => Value::Null,
                _ => value,
            };
            if !value.is_null() {
                params.insert(key.into(), value);
            }
        }
        dispatch(app, &call, action, params).await
    })
}

/// `_present`: the given fields that are not None, in order.
fn present(a: &Map<String, Value>, keys: &[&str]) -> Map<String, Value> {
    keys.iter()
        .filter_map(|k| {
            a.get(*k)
                .filter(|v| !v.is_null())
                .map(|v| (k.to_string(), v.clone()))
        })
        .collect()
}

/// The names of the given fields that are not None.
fn names_of(a: &Map<String, Value>, keys: &[&str]) -> Vec<String> {
    present(a, keys).into_iter().map(|(k, _)| k).collect()
}

fn accepted(action: &str) -> &'static [&'static str] {
    AGENT_PARAMETERS
        .iter()
        .find(|(a, _)| *a == action)
        .map_or(&[], |(_, p)| p)
}

/// `_agents_unexpected`.
fn agents_unexpected(action: &str, names: Vec<String>) -> ToolError {
    tool_error(unexpected(
        action,
        &names,
        accepted(action),
        Some(AGENT_PARAMETERS),
    ))
}

fn refused(code: &str, detail: String, param: &str, action: &str) -> ToolError {
    ToolError::Refused {
        code: code.into(),
        detail: Some(detail),
        param: Some(json!(param)),
        accepted: None,
        action: Some(json!(action)),
    }
}

fn mcp_memory_agents<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let has = |k: &str| a.get(k).is_some_and(|v| !v.is_null());
        let text = |k: &str| a.get(k).and_then(Value::as_str);
        if has("worktree") {
            // Only the local shim can inspect a checkout.
            return Err(ToolError::Invalid(
                "file_claim_requires_local_client".into(),
            ));
        }
        let action = text("action").unwrap_or("list");
        let mut lease = text("lease").map(str::to_string);
        let mut file_claim = None;
        if has("repository_id") || has("path") {
            if !matches!(action, "claim" | "release") {
                let names = present(a, &["repository_id", "path"]).into_iter();
                return Err(agents_unexpected(action, names.map(|(k, _)| k).collect()));
            }
            if lease.is_some() {
                return Err(refused(
                    "unexpected_parameter",
                    format!(
                        "{action} takes one selector: lease, or repository_id with path, not both"
                    ),
                    "lease",
                    action,
                ));
            }
            let name = super::claim_name::claim_name(
                text("repository_id").unwrap_or(""),
                text("path").unwrap_or(""),
            )
            .map_err(|code| ToolError::Invalid(code.into()))?;
            lease = Some(name);
            file_claim = Some(json!({
                "repository_id": a.get("repository_id").cloned().unwrap_or(Value::Null),
                "path": a.get("path").cloned().unwrap_or(Value::Null),
            }));
        }
        let mut park = present(
            a,
            &["park_needs", "park_clear_by", "park_resume", "park_expires"],
        );
        if let Some(reason) = a.get("park_reason").filter(|v| !v.is_null()) {
            // An empty park_reason is the tool's null, which clears a park.
            let reason = match reason {
                Value::String(s) if s.is_empty() => Value::Null,
                other => other.clone(),
            };
            park.insert("park_reason".into(), reason);
        }
        if (has("children") || !park.is_empty() || has("name")) && action != "update" {
            let mut names: Vec<String> = names_of(a, &["children", "name"]);
            names.extend(park.keys().cloned());
            return Err(agents_unexpected(action, names));
        }
        match action {
            "list" => {
                if has("status") || lease.is_some() || has("expect") {
                    let mut names: Vec<String> = names_of(a, &["status"]);
                    if lease.is_some() {
                        names.push("lease".into());
                    }
                    names.extend(names_of(a, &["expect"]));
                    return Err(agents_unexpected(action, names));
                }
                let agent = crate::mcp::identity::header(&call.ident.headers, "x-pl-agent");
                if agent.is_none_or(|v| v.is_empty())
                    && !crate::mutants::active("mcp-agents-no-awareness")
                {
                    return Ok(awareness(app, &call).await);
                }
                dispatch(app, &call, "agents", present(a, &["project", "task"])).await
            }
            "update" => {
                if lease.is_some() {
                    return Err(agents_unexpected(action, vec!["lease".into()]));
                }
                let mut params = present(
                    a,
                    &["project", "task", "status", "expect", "children", "name"],
                );
                params.extend(park);
                dispatch(app, &call, "update", params).await
            }
            _ => {
                let Some(lease) = lease else {
                    return Err(refused(
                        "missing_parameter",
                        format!(
                            "{action} needs lease, or repository_id with path \
                             (worktree with path through a local shim)"
                        ),
                        "lease",
                        action,
                    ));
                };
                if has("project") || has("task") {
                    let names = names_of(a, &["project", "task"]);
                    return Err(agents_unexpected(action, names));
                }
                let result = if action == "release" {
                    if has("status") || has("expect") {
                        let names = names_of(a, &["status", "expect"]);
                        return Err(agents_unexpected(action, names));
                    }
                    let mut params = Map::new();
                    params.insert("name".into(), json!(lease));
                    dispatch(app, &call, "release", params).await?
                } else {
                    let ttl = if lease.starts_with("claim:") {
                        CLAIM_LEASE_TTL
                    } else {
                        SESSION_LEASE_TTL
                    };
                    let mut params = Map::new();
                    params.insert("name".into(), json!(lease));
                    params.insert("ttl".into(), json!(ttl));
                    params.extend(present(a, &["expect"]));
                    if let Some(status) = a.get("status").filter(|v| !v.is_null()) {
                        params.insert("purpose".into(), status.clone());
                    }
                    dispatch(app, &call, "lease", params).await?
                };
                Ok(match (file_claim, result) {
                    (Some(claim), Value::Object(mut m))
                        if !crate::mutants::active("mcp-agents-no-file-claim") =>
                    {
                        m.insert("file_claim".into(), claim);
                        Value::Object(m)
                    }
                    (_, other) => other,
                })
            }
        }
    })
}

/// `MemoryService.coordination_awareness`: open sessions other than the
/// caller's, from resident episode state, without initializing the bank.
async fn awareness(app: &crate::http::App, call: &ToolCall<'_>) -> Value {
    let cfg = &app.board.config;
    let mut result = Map::new();
    result.insert("enabled".into(), json!(cfg.enabled));
    result.insert("available".into(), json!(false));
    result.insert("peers".into(), json!([]));
    result.insert("truncated".into(), json!(false));
    if !cfg.enabled {
        return Value::Object(result);
    }
    // `_request_principal`: None (not admitted) without an authenticated one.
    let principal = super::http::authenticated_principal(app, &header_dict(&call.ident.headers));
    if !principal.is_ok_and(|p| app.board.admitted(&p)) {
        result.insert("reason".into(), json!("principal_not_allowed"));
        return Value::Object(result);
    }
    let cap = cfg.awareness_limit.clamp(0, 20) as usize;
    let session = call.ident.header_session.as_deref();
    let Some(ready) = app.service.ready_if_initialized().await else {
        result.insert("reason".into(), json!("not_initialized"));
        return Value::Object(result);
    };
    let ws = ready.write.lock().await;
    let log = &ws.log;
    let mut roots: Vec<&crate::episodes::Episode> = log
        .episodes
        .iter()
        .filter(|ep| {
            ep.parent_id.is_none()
                && ep.ended_at.is_none()
                && ep.session_key.as_deref().is_some_and(|k| {
                    !k.is_empty()
                        && (Some(k) != session
                            || crate::mutants::active("mcp-agents-awareness-self"))
                })
        })
        .collect();
    roots.sort_by(|x, y| {
        (y.started_at, &y.id)
            .partial_cmp(&(x.started_at, &x.id))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    if crate::mutants::active("mcp-agents-awareness-order") {
        roots.reverse();
    }
    result.insert("truncated".into(), json!(roots.len() > cap));
    roots.truncate(cap);
    let by_id: HashMap<&str, &crate::episodes::Episode> =
        log.episodes.iter().map(|ep| (ep.id.as_str(), ep)).collect();
    let descends = |ep: &crate::episodes::Episode, root: &str| {
        let mut seen = std::collections::HashSet::new();
        let mut cur = Some(ep);
        while let Some(c) = cur {
            if !seen.insert(c.id.as_str()) {
                break;
            }
            if c.id == root {
                return true;
            }
            cur = c.parent_id.as_deref().and_then(|p| by_id.get(p).copied());
        }
        false
    };
    let mut owner: HashMap<&str, &str> = HashMap::new();
    for root in &roots {
        for ep in &log.episodes {
            if descends(ep, &root.id) {
                owner.insert(ep.id.as_str(), root.id.as_str());
            }
        }
    }
    let mut activity: HashMap<&str, f64> = HashMap::new();
    fn bump<'a>(activity: &mut HashMap<&'a str, f64>, root: &'a str, t: f64) {
        let slot = activity.entry(root).or_insert(t);
        *slot = slot.max(t);
    }
    for (episode, t) in &log.touches {
        if let Some(root) = owner.get(episode.as_str()) {
            bump(&mut activity, root, *t);
        }
    }
    {
        let bank = ready.bank.read().expect("bank lock");
        for entry in &bank.entries {
            if let Some(root) = entry.episode_id.as_deref().and_then(|e| owner.get(e)) {
                bump(&mut activity, root, entry.ts);
            }
        }
    }
    let peers: Vec<Value> = roots
        .iter()
        .map(|root| {
            let title: String = root
                .title
                .split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                .filter(|w| !w.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(160)
                .collect();
            json!({
                "episode_id": root.id,
                "title": title,
                "principal": null, "project": null, "task": null,
                "scope": "unknown", "capability": "unknown",
                "last_reported_at": activity.get(root.id.as_str()),
            })
        })
        .collect();
    result.insert("peers".into(), Value::Array(peers));
    result.insert("available".into(), json!(true));
    Value::Object(result)
}
