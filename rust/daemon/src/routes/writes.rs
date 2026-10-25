//! Console writes over services MCP already serves: facts set, resolve and
//! forget, reinforce, delete, supersede, consolidate. None served yet.

use super::console::Request;
use axum::response::Response;

pub(super) async fn route(_req: &mut Request<'_>) -> Option<Response> {
    None
}
