//! The request admission cases the daemon's security gate is held to, as
//! data shared by the in-process tests (`src/http_security_tests.rs`) and
//! the process test (`tests/security_admission.rs`).
//!
//! The rows are `rust/daemon/harness/security_cases.py`'s `open_cases` and
//! `closed_cases`, in the same order, so row `i` is response `i` of
//! `harness/goldens/security-open.json` and `security-closed.json`. The
//! expected answers are written here by kind, from that file's contract
//! (status and error), not copied from the goldens; a test checks the two
//! agree.
#![allow(dead_code)]

/// The singular token of the closed configuration.
pub const TOKEN: &str = "tok-security-fixture-0001";

/// The environment of each configuration the cases run under, as the
/// harness's `security-remote-open` and `security-remote-auth` start the
/// daemon (the harness's loopback goldens run the same cases without the
/// host).
pub const OPEN_ENV: &[(&str, &str)] = &[
    ("PSEUDOLIFE_MCP_HOST", "0.0.0.0"),
    ("PSEUDOLIFE_MCP_TRUST_BIND", "On"),
];
pub const CLOSED_ENV: &[(&str, &str)] = &[
    ("PSEUDOLIFE_MCP_HOST", "0.0.0.0"),
    ("PSEUDOLIFE_MCP_TOKEN", TOKEN),
    // Neither entry parses (no colon; a reserved principal), so the map adds
    // nothing and the singular token keeps the daemon closed.
    ("PSEUDOLIFE_MCP_TOKENS", "junk,rejected-map-token:default"),
];

pub enum Body {
    None,
    /// 262145 spaces: one byte over the `/api` body limit, so a check that
    /// read the body before refusing would answer 413 instead.
    Oversize,
    Bytes(&'static [u8]),
}

impl Body {
    pub fn bytes(&self) -> Option<Vec<u8>> {
        match self {
            Body::None => None,
            Body::Oversize => Some(vec![b' '; 262_145]),
            Body::Bytes(b) => Some(b.to_vec()),
        }
    }
}

/// An answer: status, content type, `Cache-Control` (if any),
/// `X-Content-Type-Options: nosniff` or not, and the exact body.
pub struct Expect {
    pub kind: &'static str,
    pub status: u16,
    pub content_type: &'static str,
    pub cache_control: Option<&'static str>,
    pub nosniff: bool,
    pub body: &'static [u8],
}

const JSON: &str = "application/json; charset=utf-8";
const TEXT: &str = "text/plain; charset=utf-8";

const fn json(kind: &'static str, status: u16, body: &'static [u8]) -> Expect {
    Expect {
        kind,
        status,
        content_type: JSON,
        cache_control: Some("no-store"),
        nosniff: true,
        body,
    }
}

pub const ORIGIN_HINTED: Expect = json(
    "forbidden_origin with the browser hint",
    403,
    br#"{"error": "forbidden_origin", "hint": "tokenless /api serves loopback browsers only; set PSEUDOLIFE_MCP_TOKEN for remote access"}"#,
);
pub const HOST_HINTED: Expect = json(
    "forbidden_host with the browser hint",
    403,
    br#"{"error": "forbidden_host", "hint": "tokenless /api serves loopback browsers only; set PSEUDOLIFE_MCP_TOKEN for remote access"}"#,
);
/// The hook and pairing routes' own refusal: no hint.
pub const ORIGIN_BARE: Expect = json("forbidden_origin", 403, br#"{"error": "forbidden_origin"}"#);
/// Admitted: the gate let the request through to routing.
pub const NOT_FOUND: Expect = json(
    "admitted (not_found)",
    404,
    br#"{"error": "not_found", "path": "/api/nope"}"#,
);
pub const METHOD: Expect = json(
    "method_not_allowed",
    405,
    br#"{"error": "method_not_allowed"}"#,
);
pub const UNAUTHORIZED: Expect = json(
    "unauthorized",
    401,
    br#"{"error": "unauthorized", "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"}"#,
);
pub const UNAVAILABLE: Expect = json(
    "principals_unavailable",
    503,
    br#"{"error": "principals_unavailable"}"#,
);
/// An Origin `urlsplit` cannot parse: uvicorn's bare 500, never admitted.
pub const SERVER_ERROR: Expect = Expect {
    kind: "uvicorn 500",
    status: 500,
    content_type: TEXT,
    cache_control: None,
    nosniff: false,
    body: b"Internal Server Error",
};
/// An always-200 hook that treats an unavailable store as unauthorized:
/// empty, never the hook's content.
pub const HOOK_EMPTY: Expect = Expect {
    kind: "empty hook answer",
    status: 200,
    content_type: TEXT,
    cache_control: Some("no-store"),
    nosniff: false,
    body: b"",
};

/// The response headers the harness compares (`run.py` `HEADERS_COMPARED`).
/// Each must be what the answer's kind names, and absent where it names none.
pub const HEADERS_COMPARED: [&str; 8] = [
    "content-type",
    "cache-control",
    "location",
    "content-security-policy",
    "x-frame-options",
    "referrer-policy",
    "x-content-type-options",
    "x-pl-board",
];

/// Every difference between an answer and the case's expectation, one line each.
pub fn mismatches(
    case: &Case,
    status: u16,
    headers: &[(String, String)],
    body: &[u8],
) -> Vec<String> {
    let e = &case.expect;
    let got = |name: &str| {
        headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    };
    let want = |name: &str| match name {
        "content-type" => Some(e.content_type),
        "cache-control" => e.cache_control,
        "x-content-type-options" => e.nosniff.then_some("nosniff"),
        _ => None,
    };
    let mut out = Vec::new();
    if status != e.status {
        out.push(format!("status {status}, want {}", e.status));
    }
    if body != e.body {
        out.push(format!(
            "body {:?}, want {:?}",
            String::from_utf8_lossy(body),
            String::from_utf8_lossy(e.body)
        ));
    }
    for name in HEADERS_COMPARED {
        if got(name) != want(name) {
            out.push(format!("{name} {:?}, want {:?}", got(name), want(name)));
        }
    }
    out.into_iter()
        .map(|d| format!("{} ({}): {d}", case.name, e.kind))
        .collect()
}

pub struct Case {
    pub name: &'static str,
    pub method: &'static str,
    pub path: &'static str,
    /// Sent in this order; a `Host` here replaces the client's own.
    pub headers: &'static [(&'static str, &'static [u8])],
    pub body: Body,
    pub expect: Expect,
}

const fn case(
    name: &'static str,
    method: &'static str,
    path: &'static str,
    headers: &'static [(&'static str, &'static [u8])],
    body: Body,
    expect: Expect,
) -> Case {
    Case {
        name,
        method,
        path,
        headers,
        body,
        expect,
    }
}

pub const OPEN: &[Case] = &[
    case(
        "foreign origin",
        "GET",
        "/api/nope",
        &[("Origin", b"http://evil.example")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "foreign host",
        "GET",
        "/api/nope",
        &[("Host", b"evil.example:8765")],
        Body::None,
        HOST_HINTED,
    ),
    case(
        "origin before host and method",
        "PUT",
        "/api/nope",
        &[
            ("Origin", b"http://evil.example"),
            ("Host", b"evil.example"),
        ],
        Body::Oversize,
        ORIGIN_HINTED,
    ),
    case(
        "origin before media and body",
        "POST",
        "/api/nope",
        &[("Origin", b"null"), ("Content-Type", b"application/json")],
        Body::Oversize,
        ORIGIN_HINTED,
    ),
    case(
        "duplicate origin foreign first",
        "GET",
        "/api/nope",
        &[
            ("Origin", b"http://evil.example"),
            ("Origin", b"http://localhost"),
        ],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "duplicate origin loopback first",
        "GET",
        "/api/nope",
        &[
            ("Origin", b"http://localhost"),
            ("Origin", b"http://evil.example"),
        ],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Origin http://localhost:5173",
        "GET",
        "/api/nope",
        &[("Origin", b"http://localhost:5173")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Origin http://127.0.0.1:8765",
        "GET",
        "/api/nope",
        &[("Origin", b"http://127.0.0.1:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Origin http://[::1]:8765",
        "GET",
        "/api/nope",
        &[("Origin", b"http://[::1]:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Origin https://localhost:8765",
        "GET",
        "/api/nope",
        &[("Origin", b"https://localhost:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Host localhost:8765",
        "GET",
        "/api/nope",
        &[("Host", b"localhost:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Host 127.0.0.1:8765",
        "GET",
        "/api/nope",
        &[("Host", b"127.0.0.1:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Host [::1]:8765",
        "GET",
        "/api/nope",
        &[("Host", b"[::1]:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "loopback Host LOCALHOST:8765",
        "GET",
        "/api/nope",
        &[("Host", b"LOCALHOST:8765")],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "refused Origin http://user@localhost",
        "GET",
        "/api/nope",
        &[("Origin", b"http://user@localhost")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Origin http://localhost.evil.example",
        "GET",
        "/api/nope",
        &[("Origin", b"http://localhost.evil.example")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Origin null",
        "GET",
        "/api/nope",
        &[("Origin", b"null")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Origin ",
        "GET",
        "/api/nope",
        &[("Origin", b"")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Origin http://127.0.0.2:8765",
        "GET",
        "/api/nope",
        &[("Origin", b"http://127.0.0.2:8765")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Origin http://[::2]:8765",
        "GET",
        "/api/nope",
        &[("Origin", b"http://[::2]:8765")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Origin http://localhost.:8765",
        "GET",
        "/api/nope",
        &[("Origin", b"http://localhost.:8765")],
        Body::None,
        ORIGIN_HINTED,
    ),
    case(
        "refused Host localhost.evil.example",
        "GET",
        "/api/nope",
        &[("Host", b"localhost.evil.example")],
        Body::None,
        HOST_HINTED,
    ),
    case(
        "refused Host 127.0.0.2:8765",
        "GET",
        "/api/nope",
        &[("Host", b"127.0.0.2:8765")],
        Body::None,
        HOST_HINTED,
    ),
    case(
        "refused Host [::2]:8765",
        "GET",
        "/api/nope",
        &[("Host", b"[::2]:8765")],
        Body::None,
        HOST_HINTED,
    ),
    case(
        "refused Host localhost.",
        "GET",
        "/api/nope",
        &[("Host", b"localhost.")],
        Body::None,
        HOST_HINTED,
    ),
    case(
        "malformed security refusal http://[::1",
        "GET",
        "/api/nope",
        &[("Origin", b"http://[::1")],
        Body::None,
        SERVER_ERROR,
    ),
    case(
        "malformed security refusal http://[localhost]",
        "GET",
        "/api/nope",
        &[("Origin", b"http://[localhost]")],
        Body::None,
        SERVER_ERROR,
    ),
    case(
        "malformed security refusal http://[127.0.0.1]",
        "GET",
        "/api/nope",
        &[("Origin", b"http://[127.0.0.1]")],
        Body::None,
        SERVER_ERROR,
    ),
    case(
        "hook browser refusal has no hint",
        "POST",
        "/api/hook/session-end",
        &[("Origin", b"http://evil.example")],
        Body::Oversize,
        ORIGIN_BARE,
    ),
    case(
        "pair refuses empty Origin",
        "POST",
        "/api/pair",
        &[("Origin", b""), ("Content-Type", b"application/json")],
        Body::Oversize,
        ORIGIN_BARE,
    ),
    case(
        "pair method before Origin",
        "GET",
        "/api/pair",
        &[
            ("Origin", b"http://evil.example"),
            ("Content-Type", b"application/json"),
        ],
        Body::Oversize,
        METHOD,
    ),
    case(
        "hook method before bearer in open mode",
        "PUT",
        "/api/hook/session-end",
        &[],
        Body::Oversize,
        METHOD,
    ),
];

pub const CLOSED: &[Case] = &[
    case(
        "missing /api/nope",
        "POST",
        "/api/nope",
        &[],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "wrong scheme /api/nope",
        "POST",
        "/api/nope",
        &[("Authorization", b"Basic tok-security-fixture-0001")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "empty bearer /api/nope",
        "POST",
        "/api/nope",
        &[("Authorization", b"Bearer \t")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "unknown /api/nope",
        "POST",
        "/api/nope",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        UNAVAILABLE,
    ),
    case(
        "missing /mcp",
        "POST",
        "/mcp",
        &[],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "wrong scheme /mcp",
        "POST",
        "/mcp",
        &[("Authorization", b"Basic tok-security-fixture-0001")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "empty bearer /mcp",
        "POST",
        "/mcp",
        &[("Authorization", b"Bearer \t")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "unknown MCP token",
        "POST",
        "/mcp",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        UNAVAILABLE,
    ),
    case(
        "missing /mcp/subpath",
        "POST",
        "/mcp/subpath",
        &[],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "wrong scheme /mcp/subpath",
        "POST",
        "/mcp/subpath",
        &[("Authorization", b"Basic tok-security-fixture-0001")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "empty bearer /mcp/subpath",
        "POST",
        "/mcp/subpath",
        &[("Authorization", b"Bearer \t")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "unknown /mcp/subpath",
        "POST",
        "/mcp/subpath",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        UNAVAILABLE,
    ),
    case(
        "missing /whatever",
        "POST",
        "/whatever",
        &[],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "wrong scheme /whatever",
        "POST",
        "/whatever",
        &[("Authorization", b"Basic tok-security-fixture-0001")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "empty bearer /whatever",
        "POST",
        "/whatever",
        &[("Authorization", b"Bearer \t")],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "unknown /whatever",
        "POST",
        "/whatever",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        UNAVAILABLE,
    ),
    case(
        "duplicate authorization valid last",
        "GET",
        "/api/nope",
        &[
            ("Authorization", b"Bearer unknown-security-fixture"),
            ("Authorization", b"Bearer tok-security-fixture-0001"),
        ],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "duplicate authorization valid first",
        "GET",
        "/api/nope",
        &[
            ("Authorization", b"Bearer tok-security-fixture-0001"),
            ("Authorization", b"Bearer unknown-security-fixture"),
        ],
        Body::None,
        UNAVAILABLE,
    ),
    case(
        "auth before method",
        "PUT",
        "/api/nope",
        &[],
        Body::Oversize,
        UNAUTHORIZED,
    ),
    case(
        "method before body",
        "PUT",
        "/api/nope",
        &[("Authorization", b"Bearer tok-security-fixture-0001")],
        Body::Oversize,
        METHOD,
    ),
    case(
        "configured bearer permits remote browser",
        "GET",
        "/api/nope",
        &[
            ("Authorization", b"Bearer tok-security-fixture-0001"),
            ("Host", b"evil.example"),
            ("Origin", b"http://evil.example"),
        ],
        Body::None,
        NOT_FOUND,
    ),
    case(
        "configured gate still needs bearer",
        "GET",
        "/api/nope",
        &[("Origin", b"http://evil.example")],
        Body::None,
        UNAUTHORIZED,
    ),
    case(
        "skipped reserved token cannot inherit default",
        "GET",
        "/api/nope",
        &[("Authorization", b"Bearer rejected-map-token")],
        Body::None,
        UNAVAILABLE,
    ),
    case(
        "unavailable hook empty /api/hook/memory-changes",
        "GET",
        "/api/hook/memory-changes",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        HOOK_EMPTY,
    ),
    case(
        "unavailable hook empty /api/hook/park-gate",
        "GET",
        "/api/hook/park-gate",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        HOOK_EMPTY,
    ),
    case(
        "unavailable hook empty /api/hook/woke",
        "POST",
        "/api/hook/woke",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        HOOK_EMPTY,
    ),
    case(
        "unavailable hook empty /api/hook/subagent",
        "POST",
        "/api/hook/subagent",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        HOOK_EMPTY,
    ),
    case(
        "unavailable session-end",
        "POST",
        "/api/hook/session-end",
        &[("Authorization", b"Bearer unknown-security-fixture")],
        Body::Oversize,
        UNAVAILABLE,
    ),
    case(
        "hook method before auth",
        "PUT",
        "/api/hook/session-end",
        &[],
        Body::Oversize,
        METHOD,
    ),
    case(
        "api-reserved-number-key-object",
        "POST",
        "/api/nope",
        &[
            ("Authorization", b"Bearer tok-security-fixture-0001"),
            ("Content-Type", b"application/json"),
        ],
        Body::Bytes(b"{\"$serde_json::private::Number\":\"1\"}"),
        NOT_FOUND,
    ),
    case(
        "api-reserved-number-key-text",
        "POST",
        "/api/nope",
        &[
            ("Authorization", b"Bearer tok-security-fixture-0001"),
            ("Content-Type", b"application/json"),
        ],
        Body::Bytes(b"{\"$serde_json::private::Number\":\"text\"}"),
        NOT_FOUND,
    ),
    case(
        "api-reserved-number-key-nested",
        "POST",
        "/api/nope",
        &[
            ("Authorization", b"Bearer tok-security-fixture-0001"),
            ("Content-Type", b"application/json"),
        ],
        Body::Bytes(b"{\"nested\":{\"$serde_json::private::Number\":\"text\"}}"),
        NOT_FOUND,
    ),
    case(
        "api-reserved-number-key-mixed",
        "POST",
        "/api/nope",
        &[
            ("Authorization", b"Bearer tok-security-fixture-0001"),
            ("Content-Type", b"application/json"),
        ],
        Body::Bytes(b"{\"$serde_json::private::Number\":\"1\",\"ordinary\":2}"),
        NOT_FOUND,
    ),
];
