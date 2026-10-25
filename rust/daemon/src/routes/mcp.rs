//! Every path outside `/health`, `/ui`, `/api` and the hooks: the bearer
//! gate, then the MCP app (`/mcp` is its only route).

use crate::auth::Resolved;
use crate::http::{
    App, RequestBody, not_implemented, principals_unavailable, resolve, text_response, unauthorized,
};
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
        // Tokenless MCP awaits the accepted Host/Origin and identity policy.
        if !app.auth_configured() {
            return not_implemented(path);
        }
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
