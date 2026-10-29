//! Console read routes: search and the read projections.

use super::console::Request;
use axum::response::Response;

/// The Console registrations this family serves in full.
#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("GET", "/api/search"),
    ("GET", "/api/facts"),
    ("GET", "/api/facts/history"),
    ("GET", "/api/world"),
    ("GET", "/api/lessons"),
    ("GET", "/api/sources"),
    ("GET", "/api/recent"),
    ("GET", "/api/chain"),
    ("GET", "/api/entry"),
    ("GET", "/api/episodes"),
    ("GET", "/api/episodes/summary"),
    ("GET", "/api/consolidation"),
    ("GET", "/api/overview"),
    ("GET", "/api/trace"),
];

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    match (req.method, req.path) {
        ("GET", "/api/search") => {
            Some(crate::read::search_route::route(req.app, req.raw_query, req.headers).await)
        }
        ("GET", "/api/trace") => {
            Some(crate::read::trace::route(req.app, req.params, req.headers).await)
        }
        ("GET", path) if crate::read::console::PATHS.contains(&path) => {
            Some(crate::read::console::route(req.app, path, req.params, req.headers).await)
        }
        _ => None,
    }
}
