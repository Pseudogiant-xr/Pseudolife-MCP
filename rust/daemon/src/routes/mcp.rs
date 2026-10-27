//! Every path outside `/health`, `/ui`, `/api` and the hooks: the bearer
//! gate, then the MCP app (`/mcp` is its only route).

use crate::auth::Resolved;
use crate::http::{App, RequestBody, principals_unavailable, resolve, text_response, unauthorized};
use axum::http::HeaderMap;
use axum::response::Response;
use std::sync::Arc;

pub(crate) async fn route(
    app: &Arc<App>,
    path: &str,
    method: &str,
    h: &HeaderMap,
    raw_query: Option<&str>,
    body: &mut RequestBody,
) -> Response {
    let principal = match resolve(app, h) {
        Resolved::Unavailable => return principals_unavailable(),
        Resolved::None => return unauthorized(),
        Resolved::Principal(p) => p,
    };
    if path == "/mcp" || path.starts_with("/mcp/") {
        // Tokenless too: a binding fails closed without a bearer, and the
        // transport keeps the loopback Host/Origin allowlist (`mcp::security`).
        if let Some(refused) = crate::board::http::mcp_binding(app, h).await {
            return refused;
        }
        return crate::mcp::handle(app, &principal, path, method, h.clone(), raw_query, body).await;
    }
    // The MCP app's own router: `/mcp` is its only route.
    text_response(
        404,
        b"Not Found".to_vec(),
        "text/plain; charset=utf-8",
        None,
    )
}

#[cfg(test)]
mod tests {
    //! The tokenless mount behind the SDK's loopback Host/Origin allowlist
    //! (`mcp_server.transport_security_for(False)`), with each legitimate
    //! near miss beside the refusals it sits next to.

    use crate::http::{App, handle};
    use axum::body::Body;
    use axum::extract::State;
    use axum::http::{Request, StatusCode};
    use std::path::PathBuf;
    use std::sync::Arc;

    const INIT: &str = r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"mcp","version":"0.1.0"}}}"#;
    const PING: &str = r#"{"jsonrpc":"2.0","id":9,"method":"ping"}"#;
    const ACCEPT: &str = "application/json, text/event-stream";
    const JSON: (&str, &str) = ("content-type", "application/json");
    const ERA: (&str, &str) = ("mcp-protocol-version", "2099-01-01");

    fn app(single: Option<&str>) -> Arc<App> {
        let dsn = "postgresql://unused@127.0.0.1:1/pl_cf_w2g_unused";
        let service = Arc::new(crate::service::Service::new(
            crate::config::Config::default(),
            crate::config::DaemonEnv::from_env(&|_| None).unwrap(),
            PathBuf::from("unused"),
            dsn.into(),
            single.is_some(),
        ));
        let store = Arc::new(crate::auth::PrincipalStore::new());
        let board = Arc::new(crate::board::Board::new(
            service.config.coordination.clone(),
            dsn.into(),
            service.clone(),
            store.clone(),
        ));
        Arc::new(App::new(
            service,
            crate::auth::EnvTokens {
                single: single.map(str::to_string),
                map: vec![],
            },
            store,
            None,
            board,
        ))
    }

    async fn send(
        app: &Arc<App>,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: &str,
    ) -> (StatusCode, Option<String>, String) {
        let mut request = Request::builder().method(method).uri(path);
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        let request = request.body(Body::from(body.to_string())).unwrap();
        let response = handle(State(app.clone()), request).await;
        let status = response.status();
        let sid = response
            .headers()
            .get("mcp-session-id")
            .map(|v| v.to_str().unwrap().to_string());
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
            .await
            .unwrap();
        (status, sid, String::from_utf8_lossy(&bytes).into_owned())
    }

    async fn initialize(app: &Arc<App>, extra: &[(&str, &str)]) -> (StatusCode, String) {
        let mut headers = vec![JSON, ("accept", ACCEPT)];
        headers.extend_from_slice(extra);
        let (status, sid, body) = send(app, "POST", "/mcp", &headers, INIT).await;
        if status == StatusCode::OK && !extra.contains(&ERA) {
            assert!(
                sid.is_some(),
                "an admitted opening request names its session"
            );
        }
        (status, body)
    }

    #[tokio::test]
    async fn loopback_hosts_and_origins_reach_the_transport() {
        let app = app(None);
        for extra in [
            vec![("host", "127.0.0.1:8765")],
            vec![("host", "localhost:1")],
            vec![("host", "[::1]:5")],
            vec![
                ("host", "127.0.0.1:8765"),
                ("origin", "http://localhost:3000"),
            ],
            vec![
                ("host", "localhost:8765"),
                ("origin", "http://127.0.0.1:8765"),
            ],
            vec![("host", "[::1]:8765"), ("origin", "http://[::1]:8765")],
            // Origin may be absent or empty on a same-origin request.
            vec![("host", "127.0.0.1:8765"), ("origin", "")],
        ] {
            let (status, body) = initialize(&app, &extra).await;
            assert_eq!(status, StatusCode::OK, "{extra:?}: {body}");
            assert!(body.contains(r#""protocolVersion":"2025-11-25""#), "{body}");
        }
    }

    #[tokio::test]
    async fn rebinding_hosts_are_refused_before_the_transport() {
        let app = app(None);
        for host in [
            None,
            Some("evil.example:80"),
            Some("evil.example:8765"),
            // Near misses: no port, a longer name, another case, another address.
            Some("127.0.0.1"),
            Some("localhost"),
            Some("[::1]"),
            Some("127.0.0.1.evil:1"),
            Some("localhost.evil.example:1"),
            Some("LOCALHOST:1"),
            Some("127.0.0.2:1"),
            Some("0.0.0.0:8765"),
        ] {
            let extra: Vec<(&str, &str)> = host.map(|h| ("host", h)).into_iter().collect();
            let (status, body) = initialize(&app, &extra).await;
            assert_eq!(status, StatusCode::MISDIRECTED_REQUEST, "{host:?}");
            assert_eq!(body, "Invalid Host header");
        }
    }

    #[tokio::test]
    async fn foreign_origins_are_refused_from_a_loopback_host() {
        let app = app(None);
        for origin in [
            "http://evil.example",
            "http://evil.example:8765",
            "https://127.0.0.1:8765",
            "http://127.0.0.1",
            "http://localhost",
            "null",
            "http://localhost.evil.example:1",
        ] {
            let (status, body) =
                initialize(&app, &[("host", "127.0.0.1:8765"), ("origin", origin)]).await;
            assert_eq!(status, StatusCode::FORBIDDEN, "{origin}");
            assert_eq!(body, "Invalid Origin header");
        }
        // Host is checked before Origin.
        let (status, _) = initialize(
            &app,
            &[
                ("host", "evil.example:1"),
                ("origin", "http://evil.example"),
            ],
        )
        .await;
        assert_eq!(status, StatusCode::MISDIRECTED_REQUEST);
    }

    #[tokio::test]
    async fn content_type_is_checked_before_the_host() {
        let app = app(None);
        let (status, _, body) = send(
            &app,
            "POST",
            "/mcp",
            &[("host", "evil.example:1"), ("content-type", "text/plain")],
            INIT,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body, "Invalid Content-Type header");
    }

    #[tokio::test]
    async fn the_stateless_era_checks_the_host_first() {
        // `handle_modern_request` runs the transport check before anything else.
        let app = app(None);
        let (status, body) = initialize(&app, &[("host", "evil.example:1"), ERA]).await;
        assert_eq!(status, StatusCode::MISDIRECTED_REQUEST, "{body}");
        let (status, body) = initialize(
            &app,
            &[
                ("host", "127.0.0.1:1"),
                ("origin", "http://evil.example"),
                ERA,
            ],
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
        // The near miss still gets the era's own refusal.
        let (status, body) = initialize(&app, &[("host", "127.0.0.1:1"), ERA]).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
        assert!(body.contains("params._meta must be an object"), "{body}");
    }

    #[tokio::test]
    async fn the_stateless_era_checks_content_type_with_a_token() {
        let app = app(Some("fixture-token"));
        let auth = ("authorization", "Bearer fixture-token");
        let (status, _, body) = send(
            &app,
            "POST",
            "/mcp",
            &[
                auth,
                ERA,
                ("host", "evil.example:1"),
                ("content-type", "text/plain"),
            ],
            INIT,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body, "Invalid Content-Type header");
        // Configured auth skips the Host and Origin checks.
        let (status, body) = initialize(
            &app,
            &[
                auth,
                ERA,
                ("host", "evil.example:1"),
                ("origin", "http://evil.example"),
            ],
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
        assert!(body.contains("params._meta must be an object"), "{body}");
    }

    #[tokio::test]
    async fn a_bound_identity_without_a_bearer_fails_closed() {
        // Open installs resolve everyone to the default principal, so a
        // binding is refused before the transport, whatever the Host.
        let app = app(None);
        for host in ["127.0.0.1:8765", "evil.example:1"] {
            let (status, body) = initialize(
                &app,
                &[
                    ("host", host),
                    ("x-pl-bank", "bank"),
                    ("x-pl-principal", "default"),
                ],
            )
            .await;
            assert_eq!(status, StatusCode::UNAUTHORIZED, "{host}: {body}");
            assert!(body.contains("authentication_required"), "{body}");
        }
    }

    #[tokio::test]
    async fn router_answers_precede_the_host_check() {
        // Starlette routes before the transport runs its check.
        let app = app(None);
        let host = ("host", "evil.example:1");
        let (status, _, _) = send(&app, "GET", "/mcp/", &[host], "").await;
        assert_eq!(status, StatusCode::TEMPORARY_REDIRECT);
        let (status, _, body) = send(&app, "GET", "/mcp/x", &[host], "").await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(body, "Not Found");
        // An unknown session id is answered before the check too.
        let sid = ("mcp-session-id", "deadbeefdeadbeefdeadbeefdeadbeef");
        let (status, _, _) = send(
            &app,
            "POST",
            "/mcp",
            &[host, JSON, ("accept", ACCEPT), sid],
            PING,
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn a_live_session_is_checked_on_every_request() {
        let app = app(None);
        let good = ("host", "127.0.0.1:8765");
        let (status, sid, _) = send(
            &app,
            "POST",
            "/mcp",
            &[good, JSON, ("accept", ACCEPT)],
            INIT,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let sid = sid.unwrap();
        for (host, want) in [
            ("evil.example:1", StatusCode::MISDIRECTED_REQUEST),
            ("127.0.0.1:8765", StatusCode::OK),
        ] {
            let headers = [
                ("host", host),
                JSON,
                ("accept", ACCEPT),
                ("mcp-session-id", sid.as_str()),
                ("mcp-protocol-version", "2025-11-25"),
            ];
            let (status, _, body) = send(&app, "POST", "/mcp", &headers, PING).await;
            assert_eq!(status, want, "{host}: {body}");
        }
    }
}
