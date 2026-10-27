"""Keep the foundation harness aligned with the MCP mount boundary."""

import run


def test_authenticated_mcp_compares_transport_instead_of_a_stub():
    case = next(c for c in run.Tokens().cases() if c["name"] == "mcp path, auth")
    response = {
        "status": 406,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "json": {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "Not Acceptable: Client must accept text/event-stream"}},
    }
    assert not run.compare_case(case, response, response)["diffs"]
    stub = {"status": 501, "headers": {
        "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
        "x-content-type-options": "nosniff"},
        "json": {"error": "not_implemented", "path": "/mcp"}}
    assert run.compare_case(case, response, stub)["diffs"]


def test_tokenless_mcp_compares_transport_instead_of_a_stub():
    for scenario, name in ((run.Tokenless(), "tokenless mcp"), (run.Tokenless(), "mcp path, no auth")):
        case = next(c for c in scenario.cases() if c["name"] == name)
        assert case["declared"] is None, name
        response = {"status": 406, "headers": {"content-type": "application/json"}, "json": {
            "jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": "Not Acceptable: Client must accept text/event-stream"}}}
        assert not run.compare_case(case, response, response)["diffs"]
        stub = {"status": 501, "headers": {
            "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
            "x-content-type-options": "nosniff"},
            "json": {"error": "not_implemented", "path": "/mcp"}}
        assert run.compare_case(case, response, stub)["diffs"]
