//! Caller identity for one MCP request (`writer_context.py`,
//! `mcp_server._tier_principal`).

use super::dispatch::CallIdentity;
use axum::http::HeaderMap;

pub const DEFAULT_PRINCIPAL: &str = "default";

/// First value of a header, decoded as latin-1 (Starlette's `Headers.get`).
pub fn header(h: &HeaderMap, name: &str) -> Option<String> {
    h.get(name)
        .map(|v| v.as_bytes().iter().map(|&b| b as char).collect())
}

/// `PSEUDOLIFE_WRITER_ID` when set and non-empty.
pub fn env_writer() -> Option<String> {
    std::env::var("PSEUDOLIFE_WRITER_ID")
        .ok()
        .filter(|w| !w.is_empty())
}

/// `resolve_writer_detailed` (no explicit override exists in the daemon):
/// a named principal is the writer; the default principal keeps the
/// `X-PL-Writer` header, then the process default.
pub fn call_identity(principal: &str, headers: &HeaderMap) -> CallIdentity {
    let header_writer = header(headers, "x-pl-writer").filter(|w| !w.is_empty());
    let writer = if principal != DEFAULT_PRINCIPAL {
        principal.to_string()
    } else {
        header_writer
            .or_else(env_writer)
            .unwrap_or_else(|| "unknown".to_string())
    };
    CallIdentity {
        principal: principal.to_string(),
        writer,
        header_session: header(headers, "x-pl-session"),
        transport_session: legacy_transport_session()
            .then(|| header(headers, "mcp-session-id"))
            .flatten(),
        headers: headers.clone(),
    }
}

/// `_legacy_transport_session_enabled`: explicit truthy values only.
fn legacy_transport_session() -> bool {
    std::env::var("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION").is_ok_and(|v| {
        matches!(v.trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on")
    })
}

/// `_tier_principal`: the tier bucket and map key. A named principal, else
/// the writer id (`X-PL-Writer`, then `PSEUDOLIFE_WRITER_ID`), else none
/// (the shared bucket).
pub fn tier_key(principal: &str, headers: &HeaderMap) -> Option<String> {
    if principal != DEFAULT_PRINCIPAL {
        return Some(principal.to_string());
    }
    header(headers, "x-pl-writer")
        .filter(|w| !w.is_empty())
        .or_else(env_writer)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn h(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut m = HeaderMap::new();
        for (k, v) in pairs {
            m.append(*k, v.parse().unwrap());
        }
        m
    }

    #[test]
    fn named_principal_outranks_the_writer_header() {
        let id = call_identity("alice", &h(&[("x-pl-writer", "mallory"), ("x-pl-session", "s1")]));
        assert_eq!(id.writer, "alice");
        assert_eq!(id.header_session.as_deref(), Some("s1"));
        assert_eq!(tier_key("alice", &h(&[("x-pl-writer", "mallory")])).as_deref(), Some("alice"));
    }

    #[test]
    fn default_principal_keeps_the_writer_header() {
        let id = call_identity(DEFAULT_PRINCIPAL, &h(&[("x-pl-writer", "Writer-M")]));
        assert_eq!(id.writer, "Writer-M");
        assert_eq!(tier_key(DEFAULT_PRINCIPAL, &h(&[("x-pl-writer", "Writer-M")])).as_deref(), Some("Writer-M"));
    }
}
