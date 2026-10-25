//! The routes the CLI calls: `/api/briefing`, `/api/episode/start`,
//! `/api/episode/end` and `/api/stats`. None served yet.

use super::console::Request;
use axum::response::Response;

pub(super) async fn route(_req: &mut Request<'_>) -> Option<Response> {
    None
}
