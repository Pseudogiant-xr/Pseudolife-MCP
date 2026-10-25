//! The routes the CLI calls: `/api/briefing`, `/api/episode/start`,
//! `/api/episode/end` and `/api/stats`. None served yet.

use super::console::Request;
use axum::response::Response;

/// The Console registrations this family serves in full.
#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[];

pub(super) async fn route(_req: &mut Request<'_>) -> Option<Response> {
    None
}
