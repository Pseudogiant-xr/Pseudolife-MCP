//! The maintainer routes and `/api/config`, behind the operator gate. The
//! three maintainer reads are served (`read/maintainer.rs`); the POSTs and
//! `/api/config` are not yet.

use super::console::Request;
use axum::response::Response;

/// The Console registrations this family serves in full.
#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("GET", "/api/maintainer"),
    ("GET", "/api/maintainer/sent"),
    ("GET", "/api/maintainer/inbox"),
];

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    match (req.method, req.path) {
        ("GET", path) if crate::read::maintainer::PATHS.contains(&path) => {
            Some(crate::read::maintainer::route(req.app, path, req.params, req.headers).await)
        }
        _ => None,
    }
}
