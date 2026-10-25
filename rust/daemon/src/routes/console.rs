//! The Console routes (`ConsoleRoutes`, `web/routes.py`) once `http.rs`
//! has run every gate and parsed the body: the route-table check, then
//! each route family's handlers, and `501 not_implemented` for every
//! registered route no family serves yet (a declared divergence).

use super::{RouteTable, board, cli, episodes, graph, maintainer, read, writes};
use crate::http::{App, json_response, not_implemented};
use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};
use std::collections::HashMap;

/// One Console request as a route family sees it.
pub(crate) struct Request<'a> {
    pub app: &'a App,
    pub method: &'a str,
    pub path: &'a str,
    pub headers: &'a HeaderMap,
    pub raw_query: Option<&'a str>,
    pub params: &'a HashMap<String, String>,
    /// A POST's JSON object, empty for an empty body or a GET. A handler
    /// that consumes it takes it (`std::mem::take`).
    pub body: Map<String, Value>,
}

pub(crate) async fn route(mut req: Request<'_>, maintainer_path: bool) -> Response {
    let (method, path) = (req.method, req.path);
    let table = RouteTable::get();
    if !table.handles(method, path) {
        if maintainer_path && table.has(path) {
            return json_response(400, &json!({"error": "invalid_request"}));
        }
        return if table.has(path) && !crate::mutants::active("route-405-as-404") {
            json_response(405, &json!({"error": "method_not_allowed", "path": path}))
        } else {
            json_response(404, &json!({"error": "not_found", "path": path}))
        };
    }
    // Each family answers only its own (method, path) pairs, so the order
    // of these calls does not matter.
    if let Some(r) = read::route(&mut req).await {
        return r;
    }
    if let Some(r) = board::route(&mut req).await {
        return r;
    }
    if let Some(r) = writes::route(&mut req).await {
        return r;
    }
    if let Some(r) = graph::route(&mut req).await {
        return r;
    }
    if let Some(r) = maintainer::route(&mut req).await {
        return r;
    }
    if let Some(r) = episodes::route(&mut req).await {
        return r;
    }
    if let Some(r) = cli::route(&mut req).await {
        return r;
    }
    not_implemented(path)
}
