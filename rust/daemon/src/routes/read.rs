//! Console read routes: search and the read projections.

use super::console::Request;
use axum::response::Response;

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    match (req.method, req.path) {
        ("GET", "/api/search") => {
            Some(crate::read::search_route::route(req.app, req.raw_query, req.headers).await)
        }
        _ => None,
    }
}
