//! Slice W2-D: the search and read routes (spec.md, section "W2-D").

pub mod chronicle;
pub mod cortex_search;
pub mod embed_cache;
pub mod identity;
pub mod rerank;
pub mod search_route;

use crate::http::App;
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
