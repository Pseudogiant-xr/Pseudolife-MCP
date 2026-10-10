//! `POST /api/_harness/tool/<name>` (spec W0.2): the differential harness's
//! way into a tool body before the MCP surface (W2-G) exists. Compiled only
//! with `--features harness`; a default build has no such route.
//!
//! The answer is the tool's dict with 200, or the error payload Python's
//! `_error_payload` builds for a non-code `ValueError` (400) or any other
//! failure (500). Argument binding is not reproduced: canonical calls only.

use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Value, json};

use crate::episodes::Caller;
use crate::http::{App, RequestBody, json_response, read_body};
use crate::store::{self, ToolError};

const BODY_LIMIT: usize = 4 * 1024 * 1024;

fn last_header(h: &HeaderMap, name: &str) -> Option<String> {
    h.get_all(name)
        .iter()
        .next_back()
        .map(|v| v.as_bytes().iter().map(|&b| b as char).collect())
}

/// `writer_context._transport_session`: only behind the legacy switch.
fn transport_session(h: &HeaderMap) -> Option<String> {
    let on = std::env::var("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION").is_ok_and(|v| {
        matches!(
            v.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    });
    on.then(|| last_header(h, "mcp-session-id")).flatten()
}

pub async fn call(app: &App, path: &str, h: &HeaderMap, body: &mut RequestBody) -> Response {
    let name = path.trim_start_matches("/api/_harness/tool/");
    let Some(raw) = read_body(body, BODY_LIMIT).await else {
        return json_response(413, &json!({"error": "request_too_large"}));
    };
    let args = match serde_json::from_slice::<Value>(&raw) {
        Ok(Value::Object(m)) => m,
        _ => return json_response(400, &json!({"error": "body_must_be_object"})),
    };
    let caller = Caller {
        header_session: last_header(h, "x-pl-session"),
        transport_session: transport_session(h),
    };
    let ready = match app.service.ensure_init().await {
        Ok(r) => r,
        Err(e) => return json_response(500, &json!({"error": "internal_error", "message": e})),
    };
    let cfg = &app.service.config.memory;
    let result = match name {
        "memory_store" => store::memory_store(&ready, cfg, &caller, &args).await,
        _ => return json_response(404, &json!({"error": "unknown_tool", "tool": name})),
    };
    match result {
        Ok(v) => json_response(200, &v),
        Err(ToolError::Invalid(msg)) => {
            json_response(400, &json!({"error": "invalid_argument", "message": msg}))
        }
        Err(ToolError::Internal(msg)) => {
            eprintln!("tool {name} failed: {msg}");
            json_response(
                500,
                &json!({
                    "error": "internal_error",
                    "message": "The server failed while running this call (the cause is in its log); retry once at most, then tell the user.",
                    "mutation": "unknown",
                }),
            )
        }
    }
}
