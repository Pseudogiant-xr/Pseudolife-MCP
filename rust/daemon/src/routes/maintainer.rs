//! The maintainer routes and `/api/config`, behind the operator gate. The
//! three maintainer reads (`read/maintainer.rs`) and `/api/config`
//! (`config_io.rs`, after `http.rs`'s operator gate) are served; the
//! maintainer POSTs are not yet.

use super::console::Request;
use crate::http::json_response;
use axum::response::Response;

/// The Console registrations this family serves in full.
#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("GET", "/api/maintainer"),
    ("GET", "/api/maintainer/sent"),
    ("GET", "/api/maintainer/inbox"),
    ("GET", "/api/config"),
    ("POST", "/api/config"),
];

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    match (req.method, req.path) {
        ("GET", path) if crate::read::maintainer::PATHS.contains(&path) => {
            Some(crate::read::maintainer::route(req.app, path, req.params, req.headers).await)
        }
        ("GET", "/api/config") => {
            let svc = &req.app.service;
            Some(
                match crate::config_io::read_config(
                    &svc.config,
                    &crate::config_io::process_env,
                    &svc.data_dir,
                ) {
                    Ok(body) => json_response(200, &body),
                    // An exception `read_config` lets escape: the generic
                    // handler's 500 with its text.
                    Err(text) => json_response(500, &serde_json::json!({"error": text})),
                },
            )
        }
        ("POST", "/api/config") => {
            // `b.get("patch") or b`.
            let body = std::mem::take(&mut req.body);
            let patch = match body.get("patch") {
                Some(p) if crate::config_io::truthy(p) => p.clone(),
                _ => serde_json::Value::Object(body),
            };
            Some(
                match crate::config_io::write_config(
                    &crate::config_io::process_env,
                    &req.app.service.data_dir,
                    &patch,
                ) {
                    Ok(answer) => json_response(200, &answer),
                    Err(crate::config_io::WriteError::Invalid(text)) => {
                        json_response(400, &serde_json::json!({"error": text}))
                    }
                    Err(crate::config_io::WriteError::Failed(text)) => {
                        json_response(500, &serde_json::json!({"error": text}))
                    }
                },
            )
        }
        _ => None,
    }
}
