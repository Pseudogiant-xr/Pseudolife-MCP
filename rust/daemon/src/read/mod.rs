//! Slice W2-D: the search and read routes (spec.md, section "W2-D").

pub mod chronicle;
pub mod cortex_search;
pub mod embed_cache;
pub mod identity;
pub mod mcp_search;
pub mod rerank;
pub mod search_route;

use crate::http::App;

/// Keeps resident access accrual and its response telemetry in search order.
/// Database operations use `txn::with_client`; this mutex protects only the
/// search ordering across CPU ranking and the cortex block.
pub fn search_guard() -> &'static tokio::sync::Mutex<()> {
    static GUARD: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
    GUARD.get_or_init(|| tokio::sync::Mutex::new(()))
}
use axum::http::HeaderMap;
use axum::response::Response;

/// The GET Console routes this slice serves, after every gate and the
/// route-table check (`web/api.py:790-841`). `None` leaves the path to the
/// caller (still not implemented).
pub async fn get(
    app: &App,
    path: &str,
    raw_query: Option<&str>,
    h: &HeaderMap,
) -> Option<Response> {
    match path {
        "/api/search" => Some(search_route::route(app, raw_query, h).await),
        _ => None,
    }
}
