//! The board's HTTP handlers, called by W1-A's gate (`crate::http`) once
//! its pre-handler steps have run: `POST /api/coordination/<action>` (spec
//! BT) and the four board hooks (spec BK1-BK3).

use super::error::{BoardError, rest_body, status_for};
use crate::auth::{self, Resolved};
use crate::http::App;
use axum::body::Body;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::Response;
use serde_json::{Map, Value};
use std::collections::HashMap;

/// `CHECKIN_TEXT` (`coordination.py:192-212`), pinned byte for byte.
pub const CHECKIN_TEXT: &str = "Pseudolife coordination: at the first task and on resume, use \
memory_agents(action=list) to check peers and memory_agents(action=update, \
project=<project>, task=<task>, status=<status>) to show your current scope. \
Then use memory_message(action=receive); read each full message and \
memory_message(action=ack, message_id=<id>) after reading. On a \
pending-message hint, receive again. Changed-message alerts are brief; \
receive is the source of full messages. If coordination tools are \
unavailable, say so and continue independently. When to send: Before using \
something shared (anything only one of you can use at a time, or that slows \
down for everyone), look for whoever holds it or has it booked. If someone \
does, message them that you are next and what you need, even when their \
status says when they expect to finish: a status line is not a queue. If \
the board shows it free, use it and say so in your status. Keep your \
status true: what you hold, what you are waiting on, when you expect to \
finish. A peer may not see mail until its next turn. Subagents never send \
board mail. A Claude Code subagent shares its parent's board address, so it \
only reads the board (list, receive without ack, memory_search); a Codex \
subagent has its own address and keeps its own status and mail. The board \
lists a session's subagents by itself; name yours with \
memory_agents(action=update, children=[...]) only if they do not appear.";

/// `PARK_GATE_MESSAGE` (`coordination.py:259-270`).
pub const PARK_GATE_MESSAGE: &str = "Before ending: update your board status with why you stopped and what you need \
(memory_agents update park_reason=... park_needs=... park_clear_by=... \
park_resume=...). Use done only when no follow-up is expected; urgent mail \
from the maintainer, the maintainer's delegate for the project or your named \
clearer still rings it. Waiting on a merge click or a review that may \
still bring fixes? Park needs_approval with park_clear_by set to the \
reviewer's agent id or maintainer, or waiting_peer. A park records intent; automatic wake requires \
a live listener. Check the sender's wake receipt; no_path means mail is \
queued for receive on a later turn. Claude Code's Stop hook keeps listening \
while this session stays open, and a Codex thread needs its doorbell active; \
without either, record that you are reachable on your next turn.";

/// The request headers as `web/api.py` hands them to the board: names
/// lower-cased, values latin-1, the last of a repeated header winning.
pub fn header_dict(h: &HeaderMap) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for (name, value) in h {
        out.insert(
            name.as_str().to_ascii_lowercase(),
            value.as_bytes().iter().map(|&b| b as char).collect(),
        );
    }
    out
}

fn json(status: u16, body: &Value) -> Response {
    crate::http::json_response(status, body)
}

fn refusal(e: &BoardError) -> Response {
    if let BoardError::Internal(text) = e {
        // Python logs the exception type only; the text can carry row
        // data, so it is printed only when a harness asks for it.
        if std::env::var_os("PSEUDOLIFE_DAEMON_DEBUG_BOARD").is_some() {
            eprintln!("coordination handler failed: {text}");
        } else {
            eprintln!("coordination handler failed (internal)");
        }
    }
    let r = e.public();
    json(status_for(&r.code), &rest_body(&r))
}

fn text(body: String, board: Option<String>) -> Response {
    let mut r = Response::new(Body::from(body));
    *r.status_mut() = StatusCode::OK;
    let h = r.headers_mut();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if let Some(state) = board {
        h.insert(
            "x-pl-board",
            HeaderValue::from_str(&state).expect("ASCII board state"),
        );
    }
    r
}

/// `authenticated_principal`: configured bearer auth is required.
pub fn authenticated_principal(app: &App, headers: &HashMap<String, String>) -> Result<String, BoardError> {
    if !app.env_tokens.configured() {
        return Err(BoardError::code("authentication_required"));
    }
    match auth::resolve(headers.get("authorization").map(String::as_str), &app.env_tokens, &app.store) {
        Resolved::Unavailable => Err(BoardError::code("principals_unavailable")),
        Resolved::None => Err(BoardError::code("unauthorized")),
        Resolved::Principal(p) => Ok(p),
    }
}

/// `POST /api/coordination/<action>` after W1-A's body checks; `body` is
/// the parsed object (empty for an empty body).
pub async fn coordination(app: &App, path: &str, h: &HeaderMap, body: Map<String, Value>) -> Response {
    let headers = header_dict(h);
    let principal = match authenticated_principal(app, &headers) {
        Ok(p) => p,
        Err(e) => return refusal(&e),
    };
    let action = path.strip_prefix("/api/coordination/").unwrap_or("");
    match app.board.handle(action, body, &headers, &principal).await {
        Ok(result) => json(200, &result),
        Err(e) => refusal(&e),
    }
}

/// `unavailable_reason`: why this caller cannot use the board, without
/// storage I/O.
pub fn unavailable_reason(app: &App, headers: &HashMap<String, String>) -> Option<String> {
    if !app.board.config.enabled {
        return Some("disabled".into());
    }
    let principal = match authenticated_principal(app, headers) {
        Ok(p) => p,
        Err(BoardError::Refused(r)) => return Some(r.code),
        Err(_) => return Some("coordination_unavailable".into()),
    };
    if !app.board.admitted(&principal) {
        return Some("principal_not_allowed".into());
    }
    None
}

/// `GET /api/hook/coordination-start` after the browser gate and the verb.
pub fn hook_coordination_start(app: &App, h: &HeaderMap) -> Response {
    let headers = header_dict(h);
    match unavailable_reason(app, &headers) {
        Some(reason) => text(String::new(), Some(format!("off; reason={reason}"))),
        None => text(format!("{CHECKIN_TEXT}\n"), Some("on".into())),
    }
}

fn agent_param(agent: Option<&String>) -> Option<&str> {
    agent
        .map(String::as_str)
        .filter(|a| a.len() == 32 && a.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
}

/// Python `float(s)`: surrounding whitespace, single underscores between
/// digits, and the special spellings.
fn py_float(s: &str) -> Option<f64> {
    let t = super::text::py_strip(s);
    let chars: Vec<char> = t.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_'
            && !(i > 0 && i + 1 < chars.len() && chars[i - 1].is_ascii_digit() && chars[i + 1].is_ascii_digit())
        {
            return None;
        }
    }
    t.replace('_', "").parse::<f64>().ok()
}

/// `GET /api/hook/park-gate` for an authorized caller.
pub async fn hook_park_gate(app: &App, h: &HeaderMap, query: &HashMap<String, String>) -> Response {
    let allow = || text("allow\n".into(), None);
    let headers = header_dict(h);
    if unavailable_reason(app, &headers).is_some() {
        return allow();
    }
    let Some(agent) = agent_param(query.get("agent")) else {
        return allow();
    };
    let since = query
        .get("since")
        .and_then(|s| py_float(s))
        .filter(|s| (0.0..1e12).contains(s));
    let Ok(principal) = authenticated_principal(app, &headers) else {
        return allow();
    };
    let verdict = app.board.with_store(|store, client| {
        let agent = agent.to_string();
        Box::pin(async move { store.park_gate(&*client, &agent, &principal, since).await })
    });
    match verdict.await {
        Ok(v) if v["gate"] == "block" => text(format!("block\n{PARK_GATE_MESSAGE}\n"), None),
        _ => allow(),
    }
}

/// `POST /api/hook/woke` for an authorized caller.
pub async fn hook_woke(app: &App, h: &HeaderMap, query: &HashMap<String, String>) -> Response {
    let headers = header_dict(h);
    if unavailable_reason(app, &headers).is_some() {
        return text(String::new(), None);
    }
    let Some(agent) = agent_param(query.get("agent")) else {
        return text(String::new(), None);
    };
    let Ok(principal) = authenticated_principal(app, &headers) else {
        return text(String::new(), None);
    };
    let agent = agent.to_string();
    let result = app
        .board
        .with_store(|store, client| Box::pin(async move { store.woke(client, &agent, &principal).await }))
        .await;
    recorded(result)
}

/// `POST /api/hook/subagent` for an authorized caller.
pub async fn hook_subagent(app: &App, h: &HeaderMap, query: &HashMap<String, String>) -> Response {
    let headers = header_dict(h);
    if unavailable_reason(app, &headers).is_some() {
        return text(String::new(), None);
    }
    let event = query.get("event").map(String::as_str);
    let (Some(agent), true) = (
        agent_param(query.get("agent")),
        matches!(event, Some("start") | Some("stop")),
    ) else {
        return text(String::new(), None);
    };
    let Ok(principal) = authenticated_principal(app, &headers) else {
        return text(String::new(), None);
    };
    let agent = agent.to_string();
    let child = query.get("child").cloned();
    let kind = query.get("type").cloned().unwrap_or_default();
    let start = event == Some("start");
    let result = app
        .board
        .with_store(|store, client| {
            Box::pin(async move {
                // A missing child is `child=None`, refused as invalid_children.
                let Some(child) = child else {
                    return Err(BoardError::code("invalid_children"));
                };
                if start {
                    store.subagent_started(client, &agent, &principal, &child, &kind).await
                } else {
                    store.subagent_stopped(client, &agent, &principal, &child).await
                }
            })
        })
        .await;
    recorded(result)
}

fn recorded(result: Result<Value, BoardError>) -> Response {
    match result {
        Ok(v) if v.get("recorded") == Some(&Value::Bool(true)) => text("ok\n".into(), None),
        _ => text(String::new(), None),
    }
}
