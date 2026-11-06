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
    /// The principal of the Console session that signed this request in,
    /// when one did (schema v59). The gate already resolved it, so the
    /// handlers that name the caller use it, as Python binds the gate's
    /// principal for the route; `authenticated_principal` and the agent
    /// resolver would refuse a session bearer.
    pub session: Option<&'a str>,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::tests::{assert_declared_501, authed_request, mcp_refusal_app};

    /// Every family's fully served registrations.
    const SERVED: [&[(&str, &str)]; 7] = [
        read::SERVED,
        board::SERVED,
        writes::SERVED,
        graph::SERVED,
        maintainer::SERVED,
        episodes::SERVED,
        cli::SERVED,
    ];

    /// The routes the maintainer retired under D9 (rust/CUTOVER.md,
    /// 2026-10-23; deprecated in 0.21.0): Python keeps serving them until a
    /// later release removes them, and the Rust daemon answers each with its
    /// declared 501. They are the only `ConsoleRoutes` registrations Rust
    /// leaves unserved.
    const D9_RETIRED: [(&str, &str); 5] = [
        ("GET", "/api/curation/retired"),
        ("GET", "/api/graph/communities"),
        ("GET", "/api/graph/proposal-evidence"),
        ("POST", "/api/lessons/restore"),
        ("POST", "/api/world/restore"),
    ];

    /// The declared 501s: every `routes.json` registration that no family
    /// declares served, each probed with no query and no body. Serving a
    /// route means adding it to its family's `SERVED`; only D9's
    /// retirements may stay unserved.
    #[tokio::test]
    async fn unserved_routes_answer_the_declared_501() {
        let table = RouteTable::get();
        let served: Vec<(&str, &str)> = SERVED.iter().flat_map(|f| f.iter().copied()).collect();
        for (i, pair) in served.iter().enumerate() {
            assert!(table.handles(pair.0, pair.1), "{pair:?} is not registered");
            assert!(!served[..i].contains(pair), "{pair:?} is served twice");
        }
        let golden: Value =
            serde_json::from_str(include_str!("../../harness/goldens/routes.json")).unwrap();
        let app = mcp_refusal_app();
        let mut unserved: Vec<(&str, &str)> = Vec::new();
        for method in ["GET", "POST"] {
            for path in golden[method].as_array().unwrap() {
                let path = path.as_str().unwrap();
                if served.contains(&(method, path)) {
                    continue;
                }
                unserved.push((method, path));
                assert_declared_501(&app, authed_request(method, path, None)).await;
            }
        }
        assert_eq!(unserved.len() + served.len(), 72);
        // Every retained route is served; only D9's retirements stay 501.
        assert_eq!(unserved, D9_RETIRED);
        assert!(served.contains(&("POST", "/api/dream/run")));
        // A declared route is answered by its family, not the fall-through.
        for &(method, path) in &served {
            let response = tokio::time::timeout(
                std::time::Duration::from_secs(30),
                crate::http::handle(
                    axum::extract::State(app.clone()),
                    authed_request(method, path, None),
                ),
            )
            .await
            .unwrap();
            assert_ne!(response.status().as_u16(), 501, "{method} {path}");
        }
    }
}
