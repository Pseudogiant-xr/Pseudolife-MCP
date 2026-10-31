//! `/api/hook/*`, routed before the bearer gate (`web/api.py:464-697`).

use crate::auth::Resolved;
use crate::http::{
    App, RequestBody, authorization, authorized, browser_gate, json_response, method_not_allowed,
    not_implemented, parse_body_json, parse_query, principals_unavailable, read_body, resolve,
    text_response, unauthorized, uvicorn_500,
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
        // maintainer's conductor's reminder line (`?agent=`) is not ported
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
        // The plugin's SessionStart context; 200 always. The policy text is
        // public; an unauthorized caller's session id and source are dropped
        // (they could otherwise mint or hijack the active-session pointer),
        // while the version handshake still reaches it. A presented bearer
        // that resolves to no principal was rejected, and the answer says
        // so first; one the store cannot check now (`Unavailable`) may be
        // valid, so it is not.
        "/api/hook/session-start" => {
            let (principal, token_rejected) = match resolve(app, h) {
                Resolved::Principal(name) => (Some(name), false),
                Resolved::None => (
                    None,
                    crate::auth::bearer_presented(authorization(h).as_deref()),
                ),
                Resolved::Unavailable => (None, false),
            };
            let params = parse_query(raw_query);
            let get = |k: &str| params.get(k).map(String::as_str);
            let authorized = principal.is_some();
            let text = crate::session_hook::hook_session_start(
                app,
                crate::session_hook::SessionStartQuery {
                    principal: principal.as_deref(),
                    session_id: get("session_id").filter(|_| authorized),
                    source: get("source").filter(|_| authorized),
                    plugin_version: get("plugin_version"),
                    plugin_hooks_digest: get("plugin_hooks_digest"),
                    launcher: get("launcher"),
                    token_rejected,
                },
            )
            .await;
            hook_text(&text)
        }
        _ => not_implemented(path),
    }
}

#[cfg(test)]
mod tests {
    use crate::http::tests::{authed_request, mcp_refusal_app};

    async fn text_of(path: &str) -> (u16, String) {
        let response = crate::http::handle(
            axum::extract::State(mcp_refusal_app()),
            authed_request("GET", path, None),
        )
        .await;
        let status = response.status().as_u16();
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, String::from_utf8(body.to_vec()).unwrap())
    }

    /// With no bank to open, session-start still answers 200 with the
    /// public policy text, after any version notice, as Python's raised
    /// reads leave it.
    #[tokio::test]
    async fn session_start_without_a_bank_serves_the_policy_text() {
        let (status, body) = text_of("/api/hook/session-start?session_id=s&source=startup").await;
        assert_eq!(status, 200);
        assert!(body.starts_with("## Memory at session start\n"), "{body}");
        let (_, body) = text_of("/api/hook/session-start?plugin_version=0.0.1").await;
        assert!(
            body.starts_with("Pseudolife-MCP: plugin 0.0.1 and daemon "),
            "{body}"
        );
        assert!(body.contains("\n\n## Memory at session start\n"), "{body}");
        let (_, body) = text_of("/api/hook/session-start?session_id=s&source=resume").await;
        assert!(
            body.starts_with("Pseudolife-MCP: resumed session, so the startup memory briefing"),
            "{body}"
        );
    }

    /// `tests/test_session_start_rejected_token.py`'s notice, byte for byte.
    const REJECTED: &str = "Pseudolife-MCP: the memory daemon refused this machine's token, so \
        this session has no memory access. Fix: on the daemon host run `pseudolife-mcp invite \
        <name>`, then on this machine `pseudolife-mcp connect <daemon-url> --read-code`.";
    const HEAD: &str = "Pseudolife-MCP: the memory daemon refused this machine's token";

    async fn start_with(
        app: &std::sync::Arc<crate::http::App>,
        authorization: &[&str],
        query: &str,
    ) -> String {
        let mut request = axum::http::Request::builder()
            .method("GET")
            .uri(format!("/api/hook/session-start{query}"));
        for value in authorization {
            request = request.header("authorization", *value);
        }
        let response = crate::http::handle(
            axum::extract::State(app.clone()),
            request.body(axum::body::Body::empty()).unwrap(),
        )
        .await;
        assert_eq!(response.status(), 200);
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        String::from_utf8(body.to_vec()).unwrap()
    }

    /// A loaded (available) principal store, so an unknown bearer resolves
    /// to no principal rather than to `Unavailable`.
    fn loaded_app() -> std::sync::Arc<crate::http::App> {
        let app = mcp_refusal_app();
        app.store
            .refresh(vec![], None, &[], std::time::Instant::now());
        app
    }

    /// A rejected bearer gets the notice, then the no-bearer answer.
    #[tokio::test]
    async fn session_start_says_a_rejected_bearer_was_rejected() {
        let app = loaded_app();
        let plain = start_with(&app, &[], "").await;
        let rejected = start_with(&app, &["Bearer fixture-rotated-token-4f2a"], "").await;
        assert_eq!(rejected, format!("{REJECTED}\n\n{plain}"));
        assert!(!rejected.contains("fixture-rotated"), "{rejected}");
        // Before a version notice, naming the reported launcher.
        let text = start_with(
            &app,
            &["Bearer wrong"],
            "?plugin_version=0.0.1&launcher=/opt/pl/bin/pseudolife-mcp",
        )
        .await;
        assert!(text.starts_with(HEAD), "{text}");
        assert!(
            text.contains("`/opt/pl/bin/pseudolife-mcp connect <daemon-url> --read-code`"),
            "{text}"
        );
        assert!(
            text.find(HEAD).unwrap() < text.find("Pseudolife-MCP: plugin 0.0.1").unwrap(),
            "{text}"
        );
        // The last Authorization header decides, as the resolver reads it.
        let valid_last = start_with(&app, &["Bearer wrong", "Bearer fixture-token"], "").await;
        assert!(!valid_last.contains(HEAD), "{valid_last}");
        let valid_first = start_with(&app, &["Bearer fixture-token", "Bearer wrong"], "").await;
        assert!(valid_first.starts_with(REJECTED), "{valid_first}");
    }

    /// The near-misses keep the no-bearer answer byte for byte: no header,
    /// a bare or blank scheme, another scheme, and a bearer the store cannot
    /// check now (it may be a valid stored one).
    #[tokio::test]
    async fn session_start_without_a_presented_bearer_is_unchanged() {
        let app = loaded_app();
        let plain = start_with(&app, &[], "").await;
        assert!(!plain.contains(HEAD), "{plain}");
        for value in ["Bearer", "Bearer  \t ", "Basic fixture-rotated-token-4f2a"] {
            assert_eq!(start_with(&app, &[value], "").await, plain, "{value}");
        }
        let accepted = start_with(&app, &["Bearer fixture-token"], "").await;
        assert!(!accepted.contains(HEAD), "{accepted}");
        let unloaded = mcp_refusal_app();
        assert_eq!(start_with(&unloaded, &["Bearer wrong"], "").await, plain);
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
