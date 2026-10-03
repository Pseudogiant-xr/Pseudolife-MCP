"""One semantic mutation per loopback proxy, with scoped server ownership."""
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

from .harness import HttpClient, Policy, DuplicateJSONKey, _base_url, _NoRedirect, compare, strict_json_loads

CONTROL_CASES = {
    "missing-key": "search-http", "ranking-order": "search-http",
    "epoch-milliseconds": "search-http", "integer-float": "search-http",
    "error-as-success": "unknown-tool", "tool-description": "list-tools",
    "duplicate-key": "search-http",
}
EXPECTED_REASONS = dict(zip(CONTROL_CASES, (
    "field_presence", "ranking_order", "epoch_unit", "type",
    "tool_error_envelope", "value", "duplicate_json_key")))


def mutate(control, raw, content_type, case):
    if control not in CONTROL_CASES:
        raise ValueError("unknown graded control")
    if case != CONTROL_CASES[control] or not raw:
        return raw, False
    if "text/event-stream" in content_type:
        lines = raw.decode().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith("data:"):
                changed, applied = mutate(control, line[5:].strip().encode(), "application/json", case)
                if applied:
                    lines[index] = "data: " + changed.decode() + "\n"
                    return "".join(lines).encode(), True
        return raw, False
    body = strict_json_loads(raw)
    if control == "missing-key":
        del body["count"]
    elif control == "ranking-order":
        body["entries"][0], body["entries"][1] = body["entries"][1], body["entries"][0]
    elif control == "epoch-milliseconds":
        body["entries"][0]["timestamp"] *= 1000
    elif control == "integer-float":
        body["count"] = float(body["count"])
    elif control == "error-as-success":
        body["result"]["isError"] = False
    elif control == "tool-description":
        body["result"]["tools"][0]["description"] += " Altered contract."
    elif control == "duplicate-key":
        encoded = json.dumps(body, separators=(",", ":"))
        return (encoded[:-1] + ',"count":' + str(body["count"]) + '}').encode(), True
    return json.dumps(body, separators=(",", ":")).encode(), True


def control_difference(original, mutated, case):
    from .full_bank import Normalizer, normalize_payload
    expected, actual = copy.deepcopy(original), copy.deepcopy(original)
    try:
        actual["body"] = strict_json_loads(mutated)
    except DuplicateJSONKey:
        return [{"path": "/body", "reason": "duplicate_json_key"}]
    if case == "search-http":
        expected["body"] = normalize_payload(Normalizer(), expected["body"], "search")
        actual["body"] = normalize_payload(Normalizer(), actual["body"], "search")
    return compare(expected, actual, Policy(ranking_paths=("/body/entries",)))


def request_case(path, body):
    if path.startswith("/api/search"):
        return "search-http"
    if path == "/mcp" and body:
        rpc = strict_json_loads(body)
        if rpc["method"] == "tools/list":
            return "list-tools"
        if rpc["method"] == "tools/call" and rpc["params"]["name"] == "synthetic_unknown_tool":
            return "unknown-tool"
    return None


@contextmanager
def mutation_proxy(upstream, control):
    upstream = _base_url(upstream)
    state = {"mutations": 0, "server_stopped": False}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in {"host", "content-length", "connection"}}
            request = urllib.request.Request(upstream + self.path, data=body or None,
                                             method=self.command, headers=headers)
            try:
                response = opener.open(request, timeout=120)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read()
                changed, applied = mutate(control, raw, response.headers.get("Content-Type", ""),
                                           request_case(self.path, body))
                state["mutations"] += int(applied)
                if applied:
                    state["upstream_status"] = response.status
                    state["candidate_status"] = response.status
                self.send_response_only(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(changed)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(changed)
                self.close_connection = True

        do_GET = do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("graded control proxy did not stop")
        state["server_stopped"] = True


def capture_controls(corpus, baseline, url, token):
    from .full_bank import Normalizer, normalize_payload
    from .wire import normalize_content_length
    records = {r["id"]: r for r in baseline}
    initialization = [c for c in corpus["cases"] if c["id"] in {"initialize", "initialized"}]
    results = {}
    for control, case_id in CONTROL_CASES.items():
        case = records[case_id]
        headers = {"Authorization": "Bearer " + token, "X-PL-Writer": "synthetic-contract"}
        with mutation_proxy(url, control) as (proxy_url, cleanup):
            client = HttpClient(proxy_url, timeout=120, retain_wire=True)
            if case["surface"] == "mcp":
                for start in initialization:
                    client.execute(start["request"], "mcp", runtime_headers=headers)
            try:
                actual = client.execute(case["request"], case["surface"], runtime_headers=headers)
                raw = actual.pop("_wire_body")
                original_body = copy.deepcopy(actual["body"])
                if case_id == "search-http":
                    actual["body"] = normalize_payload(Normalizer(), actual["body"], "search")
                normalize_content_length(actual, raw, original_body)
                differences = compare(case["response"], actual, Policy(ranking_paths=("/body/entries",)))
            except DuplicateJSONKey:
                differences = [{"path": "/body", "reason": "duplicate_json_key"}]
        reason = EXPECTED_REASONS[control]
        results[control] = {"expected_difference": reason, "differences": differences,
            "rejected": reason in {d["reason"] for d in differences}, "case": case_id,
            "correct_status": cleanup.get("candidate_status") == cleanup.get("upstream_status") == case["response"]["status"],
            "cleanup": cleanup}
        if not results[control]["rejected"] or not results[control]["correct_status"] or cleanup["mutations"] != 1:
            raise RuntimeError("graded control did not produce expected named difference: " + control)
    return results
