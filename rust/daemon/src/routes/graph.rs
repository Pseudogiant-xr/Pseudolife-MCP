//! Graph, review, curation and dream routes. None served yet.

use super::console::Request;
use axum::response::Response;

pub(super) async fn route(_req: &mut Request<'_>) -> Option<Response> {
    None
}
