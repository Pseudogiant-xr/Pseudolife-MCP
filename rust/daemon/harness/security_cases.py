"""Source-derived HTTP security admission cases; no storage handler calls."""

TOKEN = "tok-security-fixture-0001"
JS = ("Content-Type", "application/json")
OVERSIZE = b" " * 262145
CONTROLS = {
    "security-origin-open": ("security-open", "foreign origin", 403, 404),
    "security-host-open": ("security-open", "foreign host", 403, 404),
    "security-browser-last": ("security-open", "duplicate origin foreign first", 403, 404),
    "security-auth-first": ("security-closed", "duplicate authorization valid last", 404, 503),
    "security-drop-latin1": ("security-encodings", "UTF8 wire REST", 404, 503),
    "security-unavailable-401": ("security-closed", "unknown MCP token", 503, 401),
}


def expect(case, name, method, path, headers=(), body=None, status=404,
           error="not_found", priority="admission before dispatch"):
    item = case(name, method, path, headers, body)
    item.update(expected_status=status, expected_error=error,
                security_priority=priority)
    return item


def open_cases(case):
    def c(name, headers=(), status=404, error="not_found", method="GET", body=None,
          priority="Origin then Host before method and body"):
        return expect(case, name, method, "/api/nope", headers, body, status, error, priority)
    out = [c("foreign origin", [("Origin", "http://evil.example")], 403, "forbidden_origin"),
           c("foreign host", [("Host", "evil.example:8765")], 403, "forbidden_host"),
           c("origin before host and method", [("Origin", "http://evil.example"),
                                               ("Host", "evil.example")], 403,
             "forbidden_origin", "PUT", OVERSIZE),
           c("origin before media and body", [("Origin", "null"), JS], 403,
             "forbidden_origin", "POST", OVERSIZE),
           c("duplicate origin foreign first", [("Origin", "http://evil.example"),
                                                 ("Origin", "http://localhost")], 403,
             "forbidden_origin"),
           c("duplicate origin loopback first", [("Origin", "http://localhost"),
                                                  ("Origin", "http://evil.example")])]
    for value in ("http://localhost:5173", "http://127.0.0.1:8765", "http://[::1]:8765",
                  "https://localhost:8765"):
        out.append(c("loopback Origin " + value, [("Origin", value)]))
    for value in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765", "LOCALHOST:8765"):
        out.append(c("loopback Host " + value, [("Host", value)]))
    for value in ("http://user@localhost", "http://localhost.evil.example", "null", "",
                  "http://127.0.0.2:8765", "http://[::2]:8765", "http://localhost.:8765"):
        out.append(c("refused Origin " + value, [("Origin", value)], 403, "forbidden_origin"))
    for value in ("localhost.evil.example", "127.0.0.2:8765", "[::2]:8765", "localhost."):
        out.append(c("refused Host " + value, [("Host", value)], 403, "forbidden_host"))
    for value in ("http://[::1", "http://[localhost]", "http://[127.0.0.1]"):
        out.append(c("malformed security refusal " + value, [("Origin", value)], 500, None))
    out += [expect(case, "hook browser refusal has no hint", "POST", "/api/hook/session-end",
                   [("Origin", "http://evil.example")], OVERSIZE, 403, "forbidden_origin",
                   "browser before hook method/auth/body"),
            expect(case, "pair refuses empty Origin", "POST", "/api/pair", [("Origin", ""), JS],
                   OVERSIZE, 403, "forbidden_origin", "method then any Origin before media/body"),
            expect(case, "pair method before Origin", "GET", "/api/pair",
                   [("Origin", "http://evil.example"), JS], OVERSIZE, 405,
                   "method_not_allowed", "pair method before any Origin"),
            expect(case, "hook method before bearer in open mode", "PUT", "/api/hook/session-end",
                   body=OVERSIZE, status=405, error="method_not_allowed")]
    return out


def closed_cases(case):
    auth = ("Authorization", "Bearer " + TOKEN)
    unknown = ("Authorization", "Bearer unknown-security-fixture")
    out = []
    for path in ("/api/nope", "/mcp", "/mcp/subpath", "/whatever"):
        for name, headers, status, error in (
                ("missing", [], 401, "unauthorized"),
                ("wrong scheme", [("Authorization", "Basic " + TOKEN)], 401, "unauthorized"),
                ("empty bearer", [("Authorization", "Bearer \t")], 401, "unauthorized"),
                ("unknown", [unknown], 503, "principals_unavailable")):
            label = "unknown MCP token" if name == "unknown" and path == "/mcp" else name + " " + path
            out.append(expect(case, label, "POST", path, headers, OVERSIZE, status, error,
                              "auth and unavailable snapshot before route/body"))
    out += [expect(case, "duplicate authorization valid last", "GET", "/api/nope", [unknown, auth]),
            expect(case, "duplicate authorization valid first", "GET", "/api/nope", [auth, unknown],
                   status=503, error="principals_unavailable"),
            expect(case, "auth before method", "PUT", "/api/nope", body=OVERSIZE,
                   status=401, error="unauthorized", priority="REST bearer before unsupported method"),
            expect(case, "method before body", "PUT", "/api/nope", [auth], OVERSIZE, 405,
                   "method_not_allowed"),
            expect(case, "configured bearer permits remote browser", "GET", "/api/nope",
                   [auth, ("Host", "evil.example"), ("Origin", "http://evil.example")]),
            expect(case, "configured gate still needs bearer", "GET", "/api/nope",
                   [("Origin", "http://evil.example")], status=401, error="unauthorized"),
            expect(case, "skipped reserved token cannot inherit default", "GET", "/api/nope",
                   [("Authorization", "Bearer rejected-map-token")], status=503,
                   error="principals_unavailable")]
    for path, method in (("/api/hook/memory-changes", "GET"), ("/api/hook/park-gate", "GET"),
                         ("/api/hook/woke", "POST"), ("/api/hook/subagent", "POST")):
        out.append(expect(case, "unavailable hook empty " + path, method, path, [unknown], OVERSIZE,
                          200, None, "always-200 hook treats unavailable as unauthorized"))
    out += [expect(case, "unavailable session-end", "POST", "/api/hook/session-end", [unknown],
                   OVERSIZE, 503, "principals_unavailable"),
            expect(case, "hook method before auth", "PUT", "/api/hook/session-end", body=OVERSIZE,
                   status=405, error="method_not_allowed", priority="hook method before bearer/body")]
    for name, body in (
            ("api-reserved-number-key-object", b'{"$serde_json::private::Number":"1"}'),
            ("api-reserved-number-key-text", b'{"$serde_json::private::Number":"text"}'),
            ("api-reserved-number-key-nested", b'{"nested":{"$serde_json::private::Number":"text"}}'),
            ("api-reserved-number-key-mixed", b'{"$serde_json::private::Number":"1","ordinary":2}')):
        out.append(expect(case, name, "POST", "/api/nope", [auth, JS], body,
                          priority="ordinary object keys survive JSON admission"))
    return out


def encoding_cases(case):
    out = []
    for name, wire in (("UTF8 wire", b"Bearer caf\xc3\xa9"), ("Latin1 wire", b"Bearer caf\xe9"),
                       ("HTTP whitespace", b"bEaReR \tcaf\xc3\xa9 \t")):
        out.append(expect(case, name + " REST", "GET", "/api/nope", [("Authorization", wire)]))
        out.append(expect(case, name + " MCP front gate", "GET", "/whatever",
                          [("Authorization", wire)], status=404, error=None))
    out.append(expect(case, "unmatched nonASCII wire", "GET", "/api/nope",
                      [("Authorization", b"Bearer \xff\xfe")], status=503,
                      error="principals_unavailable"))
    out.append(expect(case, "map before singular authority", "GET", "/api/hook/coordination-start",
                      [("Authorization", b"Bearer caf\xc3\xa9")], status=200, error=None,
                      priority="map is checked across both encodings before singular token"))
    return out


def ending_cases(case):
    # UTF8's last byte is NBSP/NEL when decoded as Latin1. Only SP/HTAB trim.
    return [expect(case, "UTF8 terminal " + suffix, "GET", "/api/nope",
                   [("Authorization", wire)]) for suffix, wire in
            (("NBSP byte", b"Bearer caf\xc3\xa0"),)]


def startup_cases():
    # Refused before listening, including with an explicitly trusted external boundary.
    return [("reserved maintainer-only map", {"PSEUDOLIFE_MCP_TOKENS": "x:maintainer"}, None, 2),
            ("invalid map cannot trust-open", {"PSEUDOLIFE_MCP_TOKENS": "junk,x:default",
                                              "PSEUDOLIFE_MCP_HOST": "0.0.0.0",
                                              "PSEUDOLIFE_MCP_TRUST_BIND": "1"}, None, 2),
            ("trust flag whitespace is not true", {"PSEUDOLIFE_MCP_HOST": "0.0.0.0",
                                                   "PSEUDOLIFE_MCP_TRUST_BIND": " true "}, None, 2),
            ("trust flag zero refuses", {"PSEUDOLIFE_MCP_HOST": "0.0.0.0",
                                         "PSEUDOLIFE_MCP_TRUST_BIND": "0"}, None, 2)]
