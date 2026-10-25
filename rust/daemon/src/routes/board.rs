//! The board's Console routes: the coordination view of `/api/agents` and
//! `POST /api/daemon-notice`.

use super::console::Request;
use axum::response::Response;

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    match (req.method, req.path) {
        ("GET", "/api/agents")
            if req.params.get("view").map(String::as_str) == Some("coordination") =>
        {
            Some(crate::board::http::console(req.app, req.headers, req.params).await)
        }
        ("POST", "/api/daemon-notice") => Some(
            crate::board::http::daemon_notice(req.app, req.headers, std::mem::take(&mut req.body))
                .await,
        ),
        _ => None,
    }
}
