//! CallToolResult framing (`mcp_server._tool_error`, `_error_result`, and the
//! SDK's `convert_result` for dict returns).

use super::dispatch::ToolError;
use regex::Regex;
use serde_json::{Map, Value, json};
use std::sync::OnceLock;

/// Tools whose annotations say read-only (`_READ_ONLY_TOOLS`): an internal
/// failure there carries no `mutation: unknown`.
pub const READ_ONLY_TOOLS: &[&str] = &[
    "memory_search",
    "memory_recent",
    "memory_stats",
    "memory_fact_get",
    "memory_history",
    "memory_world_search",
    "memory_lesson_search",
    "memory_episode_summary",
    "memory_consolidation_candidates",
    "memory_graph",
    "memory_recall",
    "document_search",
];

const INTERNAL_MESSAGE: &str = "The server failed while running this call (the cause is in its log); retry once at most, then tell the user.";

fn code_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    // `_ERROR_CODE`, matched with fullmatch and DOTALL.
    RE.get_or_init(|| Regex::new(r"(?s)\A([a-z][a-z0-9_]*)(?::\s*(.+))?\z").unwrap())
}

/// Python `str.strip()`.
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `_error_payload`: the JSON object a refused or failed call returns.
pub fn error_payload(tool: &str, err: &ToolError) -> Value {
    let read_only = READ_ONLY_TOOLS.contains(&tool);
    let mut out = Map::new();
    let (code, detail, extras) = match err {
        ToolError::FileNotFound(text) => {
            let mut text = text.clone();
            if !text.contains("server's filesystem") {
                text.push_str(": paths are resolved on the server's filesystem");
            }
            return json!({"error": "file_not_found", "message": text});
        }
        ToolError::Internal(e) => {
            eprintln!("tool {tool} failed: {e:#}");
            out.insert("error".into(), json!("internal_error"));
            out.insert("message".into(), json!(INTERNAL_MESSAGE));
            if !read_only {
                out.insert("mutation".into(), json!("unknown"));
            }
            return Value::Object(out);
        }
        ToolError::Refused {
            code,
            detail,
            param,
            accepted,
            action,
        } if code_re().is_match(code) && !code.contains(':') => (
            code.clone(),
            detail.clone(),
            [("param", param), ("accepted", accepted), ("action", action)],
        ),
        ToolError::Refused { code, detail, .. } => {
            // A code that is not one: Python falls back to the message text,
            // which for these errors is `code` or `code: detail`.
            let text = match detail {
                Some(d) => format!("{code}: {d}"),
                None => code.clone(),
            };
            return prose(tool, &text);
        }
        ToolError::Invalid(text) => return prose(tool, text),
    };
    out.insert("error".into(), json!(code));
    let message = match detail.as_deref().filter(|d| !d.is_empty()) {
        Some(d) => format!("{code}: {d}"),
        None => format!("{code}: the call was refused."),
    };
    out.insert("message".into(), json!(message));
    for (key, value) in extras {
        if let Some(v) = value {
            out.insert(key.into(), v.clone());
        }
    }
    if code == "coordination_unavailable" && !read_only {
        out.insert("mutation".into(), json!("unknown"));
    }
    Value::Object(out)
}

/// A ValueError whose text is matched against `_ERROR_CODE`; the
/// `coordination_unavailable` rule applies on this path too
/// (`coordination.py` raises it as plain text).
fn prose(tool: &str, text: &str) -> Value {
    let text = py_strip(text);
    match code_re().captures(text) {
        None => json!({
            "error": "invalid_argument",
            "message": if text.is_empty() { "The arguments were refused." } else { text },
        }),
        Some(c) => {
            let code = &c[1];
            let message = match c.get(2) {
                Some(d) => format!("{code}: {}", d.as_str()),
                None => format!("{code}: the call was refused."),
            };
            let mut out = json!({"error": code, "message": message});
            if code == "coordination_unavailable" && !READ_ONLY_TOOLS.contains(&tool) {
                out["mutation"] = json!("unknown");
            }
            out
        }
    }
}

/// `_error_result`: isError true, the payload as indented JSON text and as
/// structured content.
pub fn error_result(payload: &Value) -> Value {
    json!({
        "content": [{"text": pretty(payload), "type": "text"}],
        "isError": true,
        "structuredContent": payload,
    })
}

/// A dict return: the SDK's `pydantic_core.to_json(indent=2)` text plus the
/// same object as structured content.
pub fn success_result(result: &Value) -> Value {
    json!({
        "content": [{"text": pretty(result), "type": "text"}],
        "isError": false,
        "structuredContent": result,
    })
}

/// The SDK's answer for a name that is not a tool.
pub fn unknown_tool(name: &str) -> Value {
    json!({
        "content": [{"text": format!("Unknown tool: {name}"), "type": "text"}],
        "isError": true,
    })
}

fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).expect("serializable")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refusal_codes_and_prose() {
        let p = error_payload("memory_store", &ToolError::Invalid("bad_thing: nope".into()));
        assert_eq!(p, json!({"error": "bad_thing", "message": "bad_thing: nope"}));
        let p = error_payload("memory_store", &ToolError::Invalid("Bad thing happened".into()));
        assert_eq!(
            p,
            json!({"error": "invalid_argument", "message": "Bad thing happened"})
        );
        let p = error_payload("memory_store", &ToolError::Invalid("  ".into()));
        assert_eq!(p["message"], "The arguments were refused.");
        let p = error_payload("memory_search", &ToolError::refused("coordination_unavailable", None));
        assert!(p.get("mutation").is_none());
        let p = error_payload("memory_agents", &ToolError::refused("coordination_unavailable", None));
        assert_eq!(p["mutation"], "unknown");
        assert_eq!(p["message"], "coordination_unavailable: the call was refused.");
    }

    #[test]
    fn prose_codes_keep_python_extras() {
        // Review finding: coordination.py raises ValueError("coordination_unavailable").
        let p = error_payload("memory_agents", &ToolError::Invalid("coordination_unavailable".into()));
        assert_eq!(
            serde_json::to_string(&p).unwrap(),
            r#"{"error":"coordination_unavailable","message":"coordination_unavailable: the call was refused.","mutation":"unknown"}"#
        );
        let p = error_payload("memory_search", &ToolError::Invalid("coordination_unavailable".into()));
        assert!(p.get("mutation").is_none());
        let p = error_payload("memory_store", &ToolError::refused("bad_code", Some("")));
        assert_eq!(p["message"], "bad_code: the call was refused.");
    }

    #[test]
    fn internal_errors_hide_their_cause() {
        let p = error_payload("memory_search", &ToolError::Internal(anyhow::anyhow!("boom")));
        assert_eq!(p, json!({"error": "internal_error", "message": INTERNAL_MESSAGE}));
        let p = error_payload("memory_store", &ToolError::Internal(anyhow::anyhow!("boom")));
        assert_eq!(p["mutation"], "unknown");
    }

    #[test]
    fn refused_extras_keep_python_key_order() {
        let p = error_payload(
            "memory_agents",
            &ToolError::Refused {
                code: "missing_parameter".into(),
                detail: Some("send needs to".into()),
                param: Some(json!("to")),
                accepted: None,
                action: Some(json!("send")),
            },
        );
        assert_eq!(
            serde_json::to_string(&p).unwrap(),
            r#"{"error":"missing_parameter","message":"missing_parameter: send needs to","param":"to","action":"send"}"#
        );
    }
}
