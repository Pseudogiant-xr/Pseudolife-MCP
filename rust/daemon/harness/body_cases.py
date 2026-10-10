"""HTTP admission corpus, separate from the handlers owned by other slices."""


def object_body(size):
    return b'{"x":"' + b'a' * (size - 8) + b'"}'


def array_body(size):
    return b'["' + b'a' * (size - 4) + b'"]'


def cases(case, auth):
    js = ("Content-Type", "application/json")
    a = [auth]
    out = []
    def expect(item, status):
        item["expected_status"] = status
        return item
    for path, limit in [("/api/nope", 262144), ("/api/facts/set", 4194304),
                        ("/api/consolidate", 4194304), ("/api/supersede", 4194304),
                        ("/api/coordination/nope", 32768)]:
        body = object_body(limit) if path == "/api/nope" else array_body(limit)
        assert len(body) == limit
        exact_status = 404 if path == "/api/nope" else 400
        out += [expect(case(path + " exact byte limit", "POST", path, a + [js], body), exact_status),
                expect(case(path + " next byte", "POST", path, a + [js], body + b' '), 413),
                expect(case(path + " oversized media priority", "POST", path,
                            a + [("Content-Type", "text/plain")], body + b' '), 413)]
        for suffix, payload in [(" chunked exact", body), (" chunked next byte", body + b' ')]:
            item = case(path + suffix, "POST", path, a + [js], payload)
            item["chunked"] = True
            item["expected_status"] = exact_status if len(payload) == limit else 413
            out.append(item)
    out += [case("control accepts multibyte wire boundary", "POST", "/api/nope", a + [js],
                 b'{"x":"' + b'\xc3\xa9' * 131068 + b'"}'),
            case("control rejects escaped wire bytes", "POST", "/api/nope", a + [js],
                 b'{"x":"' + b'\\u00e9' * 43690 + b'"}' + b' ' * 5),
            case("method before oversized body", "PUT", "/api/nope", a + [js], object_body(262145)),
            case("auth before oversized body", "POST", "/api/nope", [js], object_body(262145)),
            case("bodyless POST without media", "POST", "/api/nope", a),
            case("array error", "POST", "/api/nope", a + [js], b'[]'),
            case("JSON error", "POST", "/api/nope", a + [js], b'{'),
            case("ordinary UTF8 error", "POST", "/api/nope", a + [js], b'\xff'),
            case("board UTF8 error", "POST", "/api/coordination/nope", a + [js], b'\xff')]
    for suffix, size, headers_only, expect_continue in [("65536 full", 65536, False, False),
                                                      ("262145 full", 262145, False, False),
                                                      ("headers only", 262145, True, False),
                                                      ("Expect", 262145, True, True)]:
        headers = [("Authorization", "Bearer unknown-body-fixture")]
        if expect_continue:
            headers.append(("Expect", "100-continue"))
        item = expect(case("unavailable bearer unread " + suffix, "POST", "/api/nope",
                           headers, b' ' * size), 503)
        item["headers_only"] = headers_only
        out.append(item)
    end = "/api/hook/session-end"
    out += [case("end exact limit no session", "POST", end, a, object_body(16384)),
            case("end next byte", "POST", end, a, object_body(16385)),
            case("end empty", "POST", end, a, b''),
            case("end malformed", "POST", end, a, b'{'),
            case("end non-object", "POST", end, a, b'[]'),
            case("end null session", "POST", end, a, b'{"session_id":null}'),
            case("end empty session", "POST", end, a, b'{"session_id":""}'),
            case("end session mutation remains delegated", "POST", end, a,
                 b'{"session_id":"pl-http-session"}', declared="W2-E/F: session-end mutation"),
            case("end nonzero integer beyond float range", "POST", end, a,
                 b'{"session_id":1' + b'0' * 400 + b'}', declared="W2-E/F: session-end mutation"),
            case("end UTF8 error", "POST", end, a, b'\xff'),
            case("end method before body", "GET", end, a, object_body(16385))]
    pair = "/api/pair"
    out += [case("pair exact limit", "POST", pair, [js], b'{}' + b' ' * 1022),
            case("pair next byte", "POST", pair, [js], b'{}' + b' ' * 1023),
            case("pair method before body", "GET", pair, [js], b' ' * 1025),
            case("pair Origin before body", "POST", pair, [js, ("Origin", "")], b' ' * 1025),
            case("pair media before body", "POST", pair, [], b' ' * 1025)]
    for path, limit in [(end, 16384), (pair, 1024)]:
        for suffix, payload in [(" chunked exact", b'{}' + b' ' * (limit - 2)),
                                (" chunked next byte", b'{}' + b' ' * (limit - 1))]:
            item = case(path + suffix, "POST", path, a + [js], payload)
            item["chunked"] = True
            item["expected_status"] = (200 if path == end else 400) if len(payload) == limit else 413
            out.append(item)
    return out


def view_cases(case):
    out = [case("open board " + q, "GET", "/api/agents?" + q) for q in
           ("view=coordination", "view=&view=coordination", "view=default&view=coordination")]
    out += [case("open default " + q, "GET", "/api/agents?" + q,
                 declared="W2-D/F: agents handler") for q in
            ("view=coordination&view=", "view=coordination&view=default", "view=Coordination")]
    out += [case("open board POST", "POST", "/api/agents?view=coordination"),
            case("open board POST JSON before admission", "POST", "/api/agents?view=coordination",
                 [("Content-Type", "application/json")], b'{'),
            case("open agents default POST", "POST", "/api/agents?view=")]
    for suffix, size, headers_only, expect_continue in [("65536 full", 65536, False, False),
                                                      ("262145 full", 262145, False, False),
                                                      ("headers only", 262145, True, False),
                                                      ("Expect", 262145, True, True)]:
        headers = [("Origin", "http://evil.example"), ("Host", "evil.example")]
        if expect_continue:
            headers.append(("Expect", "100-continue"))
        item = case("foreign origin unread " + suffix, "PUT", "/api/nope",
                    headers, b' ' * size)
        item.update(headers_only=headers_only, expected_status=403)
        out.append(item)
    return out


def text_window_cases(case, auth):
    # Just above the control cap, well below the text cap: a wrong text
    # limit produces an HTTP refusal without aborting a multi-megabyte send.
    body = array_body(262145)
    out = []
    for path in ("/api/facts/set", "/api/consolidate", "/api/supersede"):
        for chunked in (False, True):
            item = case(path + (" text window chunked" if chunked else " text window"),
                        "POST", path, [auth, ("Content-Type", "application/json")], body)
            item.update(chunked=chunked, expected_status=400)
            out.append(item)
    return out


def pair_budget_cases(case):
    js = [("Content-Type", "application/json")]
    out = [case("pair oversized consumes reservation", "POST", "/api/pair", js, b' ' * 1025)]
    out += [case("pair remaining reservation " + str(n), "POST", "/api/pair", js, b'{}')
            for n in range(19)]
    out.append(case("pair budget spent after oversized request", "POST", "/api/pair", js, b'{}'))
    return out
