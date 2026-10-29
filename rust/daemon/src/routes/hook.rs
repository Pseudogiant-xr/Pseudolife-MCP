//! `/api/hook/*`, routed before the bearer gate (`web/api.py:464-697`).

use crate::auth::Resolved;
use crate::http::{
    App, RequestBody, authorized, browser_gate, json_response, method_not_allowed, not_implemented,
    parse_body_json, parse_query, principals_unavailable, read_body, resolve, text_response,
    unauthorized, uvicorn_500,
};
use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Value, json};

const SESSION_END_BODY_LIMIT: usize = 16 * 1024;

/// The hook paths `web/api.py` routes here; any other `/api/hook/` path
/// falls through to the Console routes.
pub(crate) const PATHS: [&str; 8] = [
    "/api/hook/session-start",
    "/api/hook/memory-policy",
    "/api/hook/memory-changes",
    "/api/hook/session-end",
    "/api/hook/coordination-start",
    "/api/hook/park-gate",
    "/api/hook/woke",
    "/api/hook/subagent",
];

/// Hooks that intentionally answer 200 without reading a rejected body.
pub(crate) const BODYLESS_OK_PATHS: [&str; 6] = [
    "/api/hook/memory-policy",
    "/api/hook/memory-changes",
    "/api/hook/park-gate",
    "/api/hook/woke",
    "/api/hook/subagent",
    "/api/hook/coordination-start",
];

fn hook_text(body: &str) -> Response {
    text_response(
        200,
        body.as_bytes().to_vec(),
        "text/plain; charset=utf-8",
        Some("no-store"),
    )
}

pub(crate) async fn route(
    app: &App,
    path: &str,
    method: &str,
    h: &HeaderMap,
    raw_query: Option<&str>,
    body: &mut RequestBody,
) -> Response {
    match browser_gate(app, h) {
        Err(()) => return uvicorn_500(),
        Ok(Some(denied)) => return json_response(403, &json!({"error": denied})),
        Ok(None) => {}
    }
    let want = match path {
        "/api/hook/session-end" | "/api/hook/woke" | "/api/hook/subagent" => "POST",
        _ => "GET",
    };
    if method != want {
        return method_not_allowed();
    }
    match path {
        // Status notes and lessons are memory content: an unauthorized
        // caller gets the empty body, and the hook keeps its cursor. The
        // maintainer's delegate's reminder line (`?agent=`) is not ported
        // (declared in divergences.md).
        "/api/hook/memory-changes" => {
            if !authorized(app, h) {
                return hook_text("");
            }
            let params = parse_query(raw_query);
            let text = crate::memory_changes::hook_memory_changes(
                app,
                params.get("session_id").map(String::as_str),
                params.get("since").map(String::as_str),
            )
            .await;
            hook_text(&text)
        }
        "/api/hook/park-gate" | "/api/hook/woke" | "/api/hook/subagent" => {
            if !authorized(app, h) {
                return hook_text("");
            }
            let query = parse_query(raw_query);
            match path {
                "/api/hook/park-gate" => crate::board::http::hook_park_gate(app, h, &query).await,
                "/api/hook/woke" => crate::board::http::hook_woke(app, h, &query).await,
                _ => crate::board::http::hook_subagent(app, h, &query).await,
            }
        }
        "/api/hook/coordination-start" => crate::board::http::hook_coordination_start(app, h),
        "/api/hook/session-end" => {
            match resolve(app, h) {
                Resolved::Unavailable => return principals_unavailable(),
                Resolved::None => return unauthorized(),
                Resolved::Principal(..) => {}
            }
            let Some(raw) = read_body(body, SESSION_END_BODY_LIMIT).await else {
                return json_response(413, &json!({"error": "request_too_large"}));
            };
            let Ok(text) = std::str::from_utf8(&raw) else {
                return uvicorn_500();
            };
            // Python accepts malformed closes as success without writes. Refuse
            // them explicitly; an absent body still means no session ID.
            let parsed = if raw.is_empty() {
                json!({})
            } else {
                match parse_body_json(text) {
                    Ok(value) if value.is_object() => value,
                    _ => return json_response(400, &json!({"error": "invalid_json"})),
                }
            };
            let id = parsed.as_object().and_then(|o| o.get("session_id"));
            let present = match id {
                None | Some(Value::Null) => false,
                Some(Value::Bool(b)) => *b,
                Some(Value::Number(n)) => n.as_f64().is_none_or(|n| n != 0.0),
                Some(Value::String(s)) => !s.is_empty(),
                Some(Value::Array(a)) => !a.is_empty(),
                Some(Value::Object(o)) => !o.is_empty(),
            };
            if present && let Some(id) = id {
                crate::session_close::hook_session_end(app, id).await;
            }
            json_response(200, &json!({"ok": true}))
        }
        // The session id and source pick an arm only for an authorized
        // caller, as on session-start.
        "/api/hook/memory-policy" => {
            let params = if authorized(app, h) {
                parse_query(raw_query)
            } else {
                Default::default()
            };
            hook_text(crate::session_hook::hook_memory_policy(
                &app.service.config.memory_policy,
                params.get("session_id").map(String::as_str),
                params.get("source").map(String::as_str),
            ))
        }
        // session-start: its body (the briefing) belongs to a later slice.
        _ => not_implemented(path),
    }
}

#[cfg(test)]
mod tests {
    use crate::http::tests::{assert_declared_501, authed_request, mcp_refusal_app};

    /// The hook answers still owned by later slices.
    #[tokio::test]
    async fn unserved_hooks_answer_the_declared_501() {
        let app = mcp_refusal_app();
        assert_declared_501(&app, authed_request("GET", "/api/hook/session-start", None)).await;
    }

    /// With the default `compact` policy the separate hook serves nothing.
    #[tokio::test]
    async fn memory_policy_is_empty_text_by_default() {
        let response = crate::http::handle(
            axum::extract::State(mcp_refusal_app()),
            authed_request("GET", "/api/hook/memory-policy?session_id=s", None),
        )
        .await;
        assert_eq!(response.status(), 200);
        assert_eq!(
            response.headers().get("content-type").unwrap(),
            "text/plain; charset=utf-8"
        );
        let body = axum::body::to_bytes(response.into_body(), 64)
            .await
            .unwrap();
        assert!(body.is_empty());
    }
}
