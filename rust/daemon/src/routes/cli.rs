//! The routes the CLI calls (`web/routes.py:148-165`): `GET /api/briefing`
//! (`session_briefing`), `POST /api/episode/start` (`episode_start_session`)
//! and `POST /api/episode/end` (`episode_end_session`). `/api/stats`, which
//! `move` calls, is batch B10d's.
//!
//! The bodies arrive as Python's route lambdas read them. A field Python
//! would carry into an episode or a registration as some other type than a
//! string is refused before any write (declared in `divergences.md`).

use super::console::Request;
use crate::auth::Resolved;
use crate::http::json_response;
use crate::service::Ready;
use axum::response::Response;
use serde_json::{Map, Value, json};
use std::sync::Arc;

/// The Console registrations this family serves in full.
#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("GET", "/api/briefing"),
    ("POST", "/api/episode/start"),
    ("POST", "/api/episode/end"),
];

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    match (req.method, req.path) {
        ("GET", "/api/briefing") => Some(briefing(req).await),
        ("POST", "/api/episode/start") => Some(episode_start(req).await),
        ("POST", "/api/episode/end") => Some(episode_end(req).await),
        _ => None,
    }
}

/// Python's answer to a handler that raised: 500 with the exception text.
fn raised(e: impl std::fmt::Display) -> Response {
    json_response(500, &json!({"error": e.to_string()}))
}

/// `_i(q, key, default)`: `int()` of the value, the default when it is
/// absent, empty or not an integer.
fn query_int(req: &Request<'_>, key: &str, default: i64) -> i64 {
    req.params
        .get(key)
        .filter(|v| !v.is_empty())
        .and_then(|v| crate::read::search_route::py_int(v))
        .unwrap_or(default)
}

/// The last value of a header, its bytes read as latin-1, as `web/api.py`
/// decodes them.
fn last_header(req: &Request<'_>, name: &str) -> Option<String> {
    req.headers
        .get_all(name)
        .iter()
        .next_back()
        .map(|v| v.as_bytes().iter().map(|&b| b as char).collect())
}

/// `session_briefing(max_unsure=_i(q, "max_unsure", 3), max_lessons=...,
/// max_world=...)`: the full briefing as JSON, with the dream-stall and
/// review-queue lines in its markdown, and, while coordination is enabled,
/// the caller's view of the other open sessions (`coordination_awareness`
/// with the session from the request's own `X-PL-Session`, never the shared
/// pointer).
async fn briefing(req: &mut Request<'_>) -> Response {
    let app = req.app;
    let ready = match app.service.ensure_init().await {
        Ok(ready) => ready,
        Err(e) => return raised(e),
    };
    let coordination =
        if app.board.config.enabled && !crate::mutants::active("briefing-route-no-coordination") {
            let session = last_header(req, "x-pl-session");
            match crate::board::mcp::awareness(app, req.headers, session.as_deref(), None).await {
                Ok(v) => Some(v),
                Err(e) => return refused(e),
            }
        } else {
            None
        };
    let opts = crate::briefing::BriefingOptions {
        max_unsure: query_int(req, "max_unsure", 3),
        max_lessons: query_int(req, "max_lessons", 3),
        max_world: query_int(req, "max_world", 3),
        coordination,
        include_dream_stall: true,
        include_review_queue: true,
    };
    match crate::briefing::session_briefing(&ready, &app.service.config, &opts).await {
        Ok(out) => json_response(200, &Value::Object(out)),
        Err(e) => raised(e),
    }
}

/// `type(v).__name__` for a value loaded from JSON.
fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if !n.to_string().contains(['.', 'e', 'E']) => "int",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `b.get(key)` as an optional string; any other type is refused (the
/// message of the 400 that refuses it).
fn opt_text(body: &Map<String, Value>, key: &str) -> Result<Option<String>, String> {
    match body.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(other) => Err(not_a_string(key, other)),
    }
}

fn not_a_string(key: &str, v: &Value) -> String {
    format!("{key} must be a string, not '{}'", py_type(v))
}

fn refused(message: String) -> Response {
    json_response(400, &json!({"error": message}))
}

/// The caller's principal name, as the transport bound it for the route.
fn principal(req: &Request<'_>) -> Option<String> {
    match crate::http::resolve(req.app, req.headers) {
        Resolved::Principal(name) => Some(name),
        Resolved::None | Resolved::Unavailable => None,
    }
}

/// Run a write to completion in its own task: Python's handler runs on a
/// worker thread a disconnecting client cannot stop.
async fn detached<F, Fut>(ready: &Arc<Ready>, body: F) -> Response
where
    F: FnOnce(Arc<Ready>) -> Fut,
    Fut: std::future::Future<Output = Result<Value, String>> + Send + 'static,
{
    match ready.writes.spawn(body(ready.clone())).await {
        Ok(Ok(v)) => json_response(200, &v),
        // The body's own errors are Python's `ValueError`s (an unparsable
        // session window): 400 with the message, as `web/api.py` answers.
        Ok(Err(e)) => refused(e),
        Err(e) => raised(format!("episode task failed: {e}")),
    }
}

/// `episode_start_session(b.get("session_key"), b.get("title") or
/// "session", b.get("hint"))`, registered as `api`.
async fn episode_start(req: &mut Request<'_>) -> Response {
    let body = std::mem::take(&mut req.body);
    let (key, hint) = match (opt_text(&body, "session_key"), opt_text(&body, "hint")) {
        (Ok(k), Ok(h)) => (k, h),
        (Err(m), _) | (_, Err(m)) => return refused(m),
    };
    // `b.get("title") or "session"`: any falsy title is the default.
    let title = match body.get("title") {
        Some(v) if crate::board::store::py_truthy(v) => match v {
            Value::String(s) => s.clone(),
            other => return refused(not_a_string("title", other)),
        },
        _ => "session".to_string(),
    };
    let principal = principal(req);
    let ready = match req.app.service.ensure_init().await {
        Ok(ready) => ready,
        Err(e) => return raised(e),
    };
    detached(&ready, move |ready| async move {
        let reg = crate::session_register::Registration {
            via: crate::session_register::RegisteredVia::Api,
            principal: principal.as_deref(),
            policy_variant: None,
        };
        crate::session_register::episode_start_session(&ready, key.as_deref(), title, hint, reg)
            .await
            .map(Value::Object)
    })
    .await
}

/// `episode_end_session(b.get("session_key"), run_dream=bool(b.get(
/// "run_dream", True)))`. With no key it closes only the caller's own
/// session (the ownership guard): the header session, else the active
/// session pointer, else the transport session.
async fn episode_end(req: &mut Request<'_>) -> Response {
    let body = std::mem::take(&mut req.body);
    let explicit = match opt_text(&body, "session_key") {
        Ok(k) => k,
        Err(m) => return refused(m),
    };
    let run_dream = body
        .get("run_dream")
        .is_none_or(crate::board::store::py_truthy);
    let last = |name: &str| last_header(req, name);
    let caller = crate::episodes::Caller {
        header_session: last("x-pl-session")
            .filter(|_| !crate::mutants::active("episode-end-ignores-header")),
        transport_session: crate::mcp::identity::legacy_transport_session()
            .then(|| last("mcp-session-id"))
            .flatten(),
    };
    let app = req.app;
    let unowned = || json!({"closed": null, "reason": "no owned open session"});
    let guarded = explicit.is_none();
    // `_resolve_writer` runs before the service initializes: the header,
    // else the active-session pointer (held only once the bank is open),
    // else the transport session.
    let key = match explicit {
        Some(k) => k,
        None => {
            let resolved = match caller.header_session.as_ref().filter(|s| !s.is_empty()) {
                Some(h) => Ok(Some(h.clone())),
                None => match app.service.ready_if_initialized().await {
                    Some(ready) => ready.write.lock().await.resolve_session(&caller),
                    None => Ok(caller.transport_session.clone().filter(|s| !s.is_empty())),
                },
            };
            match resolved {
                Ok(Some(k)) => k,
                Ok(None) => return json_response(200, &unowned()),
                Err(e) => return refused(e),
            }
        }
    };
    let ready = match app.service.ensure_init().await {
        Ok(ready) => ready,
        Err(e) => return raised(e),
    };
    let band_order: Vec<String> = app
        .service
        .config
        .memory
        .band_specs
        .iter()
        .map(|b| b.name.clone())
        .collect();
    let service = app.service.clone();
    detached(&ready, move |ready| async move {
        let stamp = !guarded || crate::mutants::active("episode-end-stamps-guarded");
        let (result, fire, found) =
            crate::session_close::close_session(&ready, &key, run_dream, &band_order, stamp).await;
        if fire && !crate::mutants::active("episode-end-no-dream") {
            service.fire_session_dream();
        }
        Ok(if guarded && !found { unowned() } else { result })
    })
    .await
}

#[cfg(test)]
mod tests {
    use crate::http::tests::{authed_request, mcp_refusal_app};

    /// A field Python would carry into an episode as another type is refused
    /// with 400 before the bank is opened (the fixture has none to open).
    #[tokio::test]
    async fn non_string_fields_are_refused_before_any_write() {
        for (path, body, want) in [
            (
                "/api/episode/start",
                r#"{"session_key": 5}"#,
                "session_key must be a string, not 'int'",
            ),
            (
                "/api/episode/start",
                r#"{"hint": ["x"]}"#,
                "hint must be a string, not 'list'",
            ),
            (
                "/api/episode/start",
                r#"{"title": 1.5}"#,
                "title must be a string, not 'float'",
            ),
            (
                "/api/episode/end",
                r#"{"session_key": {"a": 1}}"#,
                "session_key must be a string, not 'dict'",
            ),
        ] {
            let response = crate::http::handle(
                axum::extract::State(mcp_refusal_app()),
                authed_request("POST", path, Some(body)),
            )
            .await;
            assert_eq!(response.status(), 400, "{path} {body}");
            let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap();
            let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(v["error"], want, "{path} {body}");
        }
    }
}
