//! The interface between the MCP surface (W2-G) and the tool bodies
//! (W2-D reads, W2-E writes, W2-F board), agreed on the board 2026-10-09.
//!
//! W2-G owns everything generic in `mcp_server.py`: argument binding, the
//! success and error envelopes, tiers and `memory_toolset`, and identity.
//! Each body slice owns its tools' bodies, MCP-layer post-processing
//! included, and exports `pub fn mcp_tools() -> Vec<(&'static str, ToolFn)>`
//! from its own module; `registry()` adds one line per slice. A tool nobody
//! registered answers the declared stub (`divergences.md`).

use axum::http::HeaderMap;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::OnceLock;

/// Who is calling, resolved once per request (`writer_context.py`).
pub struct CallIdentity {
    /// The bearer's principal: `default` for the singular token or an open
    /// install, else the mapped or stored principal's name.
    pub principal: String,
    /// `resolve_writer_detailed`: a named principal, else `X-PL-Writer`,
    /// else `PSEUDOLIFE_WRITER_ID`, else `unknown`.
    pub writer: String,
    /// `X-PL-Session` as sent (identity tier 1). The effective session (the
    /// active-session pointer fallback) is resolved by W2-E's episodes module.
    pub header_session: Option<String>,
    /// The transport's `mcp-session-id`, only under the retired
    /// `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` hatch (identity tier 4).
    pub transport_session: Option<String>,
    /// The request's headers, for anything else a body reads (the board's
    /// `X-PL-Agent`, `X-PL-Agent-Key`, `X-PL-Bank`, `X-PL-Principal`).
    pub headers: HeaderMap,
}

pub struct ToolCall<'a> {
    /// Bound arguments: unknown names refused, values checked against the
    /// tool's inputSchema, and every parameter present (defaults filled).
    pub args: &'a Map<String, Value>,
    pub ident: &'a CallIdentity,
}

/// A refused or failed call, mapped to the error payload by
/// `envelope::error_payload` exactly as `mcp_server._error_payload` maps the
/// Python exception.
#[derive(Debug)]
pub enum ToolError {
    /// A ValueError carrying a code: a `code` attribute, or a `code` /
    /// `code: detail` message. Optional `param`, `accepted`, `action`.
    Refused {
        code: String,
        detail: Option<String>,
        param: Option<Value>,
        accepted: Option<Value>,
        action: Option<Value>,
    },
    /// A ValueError or TypeError in prose. Its text still goes through the
    /// `code: detail` match, as Python's does.
    Invalid(String),
    /// FileNotFoundError: the caller named a path.
    FileNotFound(String),
    /// Anything else: `internal_error`, the cause stays in the daemon log.
    Internal(anyhow::Error),
}

impl ToolError {
    pub fn refused(code: &str, detail: Option<&str>) -> Self {
        ToolError::Refused {
            code: code.to_string(),
            detail: detail.map(str::to_string),
            param: None,
            accepted: None,
            action: None,
        }
    }
}

pub type ToolResult = Result<Value, ToolError>;
pub type ToolFuture<'a> = Pin<Box<dyn Future<Output = ToolResult> + Send + 'a>>;
pub type ToolFn = for<'a> fn(&'a crate::http::App, ToolCall<'a>) -> ToolFuture<'a>;

/// Bodies registered by the slices that own them.
pub fn registry() -> &'static HashMap<&'static str, ToolFn> {
    static REGISTRY: OnceLock<HashMap<&'static str, ToolFn>> = OnceLock::new();
    REGISTRY.get_or_init(|| {
        #[allow(unused_mut)]
        let mut map: HashMap<&'static str, ToolFn> = HashMap::new();
        // One line per body slice, as each lands:
        // map.extend(crate::read::mcp_tools());
        map
    })
}

/// The declared stub for a tool whose body has not landed (delegate ruling
/// 2026-10-09): the normal refusal envelope, nothing ran, no `mutation` key.
pub fn not_implemented(tool: &str) -> Value {
    serde_json::json!({
        "error": "not_implemented",
        "message": format!("not_implemented: {tool} is not yet served by the Rust daemon"),
    })
}
