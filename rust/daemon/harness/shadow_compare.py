"""Compare read traffic against two running daemons on the same bank.

No write tools or arbitrary HTTP verbs are accepted. Query files are JSON
lists of {"tool": ..., "args": {...}} or {"path": "/api/...", "params": {...}}.
A list of query strings expands to search, lesson, world, recall and Console
search cases. --database-url-env additionally reads the bank's retrieval
log and samples fact slots and graph entities using a read-only transaction.
Replays carry X-PL-Session: shadow-compare:<uuid>. Python still records its
ordinary telemetry; future corpora exclude that reserved session prefix.

Normalization is deliberately narrow: numeric-string IDs and UUID casing;
ISO timestamps versus epoch seconds; absolute score tolerance 0.0002;
timestamp tolerance 2 seconds; elapsed timings and access counters are
reported as comparator noise (Python reads increment the latter). Stable
IDs, missing keys, list membership/order and non-score numbers remain exact.
Every accepted difference is retained in the JSON, with its reason.

Tokens and the optional database URL are read from named environment
variables. HTTP redirects are never followed. Exit 0 means no untriaged
differences/errors, 1 means differences, 2 means invalid input or transport.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urlsplit
import uuid

READ_TOOLS = frozenset({
    "memory_search", "memory_fact_get", "memory_lesson_search", "memory_recall",
    "memory_world_search", "memory_graph", "memory_recent", "memory_get",
    "memory_history", "memory_stats", "memory_episode_summary",
    "memory_consolidation_candidates", "document_search",
})
# Explicit paths, not a GET-is-safe assumption: session-start is a GET write.
READ_PATHS = frozenset({
    "/api/search", "/api/facts", "/api/facts/history", "/api/world",
    "/api/lessons", "/api/recent", "/api/sources", "/api/chain", "/api/entry",
    "/api/episodes", "/api/episodes/summary", "/api/consolidation",
    "/api/overview", "/api/stats", "/api/graph", "/api/agents", "/api/config",
})
SCORE_KEYS = {"score", "dense", "ce", "bm25", "slot", "recency", "margin"}
TIME_KEYS = {"ts", "created_at", "updated_at", "asserted_at", "last_confirmed",
             "last_read_at", "recorded_at", "started_at", "ended_at"}
NOISE_KEYS = {"elapsed_ms", "duration_ms", "access_count"}
SCORE_TOL = 2e-4
TIME_TOL = 2.0
HERE = Path(__file__).resolve().parent
REPLAY_SESSION_PREFIX = "shadow-compare:"


def validate_case(case):
    if not isinstance(case, dict):
        raise ValueError("each case must be an object")
    if "tool" in case:
        if set(case) - {"name", "tool", "args"} or case["tool"] not in READ_TOOLS:
            raise ValueError("case is not an allowed read tool")
        if not isinstance(case.get("args", {}), dict):
            raise ValueError("tool args must be an object")
    elif "path" in case:
        if set(case) - {"name", "path", "params"} or case["path"] not in READ_PATHS:
            raise ValueError("case is not an allowed Console GET route")
        if not isinstance(case.get("params", {}), dict):
            raise ValueError("route params must be an object")
    else:
        raise ValueError("case needs tool or path")
    return case


def query_cases(query):
    return [{"tool": tool, "args": {"query": query}}
            for tool in ("memory_search", "memory_lesson_search", "memory_recall", "memory_world_search")] + [
        {"path": "/api/search", "params": {"q": query}},
        {"tool": "memory_fact_get", "args": {"entity": query, "attribute": "value"}},
        {"tool": "memory_graph", "args": {"entity": query}}]


def corpus(query_file=None, dsn=None, limit=20):
    cases = []
    if query_file:
        data = json.loads(Path(query_file).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("query file must contain a JSON list")
        for item in data:
            cases.extend(query_cases(item) if isinstance(item, str) else [validate_case(item)])
    if dsn:
        import psycopg
        with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = conn.execute("SELECT query_text FROM retrieval_events WHERE query_text <> '' "
                                "AND coalesce(session_id, '') NOT LIKE 'shadow-compare:%%' "
                                "GROUP BY query_text ORDER BY max(created_at) DESC, query_text LIMIT %s", (limit,))
            for (query,) in rows:
                cases.extend(query_cases(query))
            for entity, attribute in conn.execute(
                    "SELECT DISTINCT entity, attribute FROM facts WHERE status = 'current' "
                    "ORDER BY entity, attribute LIMIT %s", (limit,)):
                cases.append({"tool": "memory_fact_get", "args": {"entity": entity, "attribute": attribute}})
                cases.append({"tool": "memory_graph", "args": {"entity": entity}})
            cases.extend({"path": path} for path in sorted(READ_PATHS - {"/api/search", "/api/entry"}))
    if not cases:
        raise ValueError("empty corpus: supply a query file or a bank with retrieval log entries")
    for i, case in enumerate(cases):
        validate_case(case)
        case.setdefault("name", f"{i:04d}:{case.get('tool', case.get('path'))}")
    names = [c["name"] for c in cases]
    if len(set(names)) != len(names):
        raise ValueError("case names must be unique")
    return cases


class Endpoint:
    def __init__(self, base, token=None, timeout=120, replay=True):
        self.url = urlsplit(base)
        if (self.url.scheme not in ("http", "https") or not self.url.hostname
                or self.url.username or self.url.password or self.url.query or self.url.fragment):
            raise ValueError("base URL must be HTTP(S), without credentials, query or fragment")
        self.token, self.timeout, self.sid, self.seq = token, timeout, None, 0
        self.replay_session = REPLAY_SESSION_PREFIX + uuid.uuid4().hex if replay else None

    def request(self, method, path, payload=None):
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.url.hostname, self.url.port, timeout=self.timeout)
        headers = {"Accept": "application/json, text/event-stream"}
        if self.replay_session:
            headers["X-PL-Session"] = self.replay_session
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.sid:
            headers.update({"Mcp-Session-Id": self.sid, "MCP-Protocol-Version": "2025-11-25"})
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, self.url.path.rstrip("/") + path, data, headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            sid = response.getheader("mcp-session-id")
            if sid:
                self.sid = sid
            status = response.status
            if 300 <= status < 400:
                raise ValueError("redirect refused; supply the daemon's final base URL")
            if "text/event-stream" in (response.getheader("content-type") or ""):
                values = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
                body = next((v for v in values if isinstance(v, dict) and v.get("id") == self.seq), None)
            else:
                body = json.loads(raw) if raw.strip() else None
            return status, body
        finally:
            conn.close()

    def rpc(self, method, params):
        self.seq += 1
        return self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params})

    def initialize(self):
        status, answer = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                "clientInfo": {"name": "shadow-compare", "version": "1"}})
        if status != 200 or not isinstance(answer, dict) or "result" not in answer:
            raise ValueError("MCP initialization failed")
        self.request("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer["result"].get("serverInfo")

    def read(self, case):
        validate_case(case)  # Defense at the transport boundary too.
        if "path" in case:
            query = urlencode(case.get("params", {}), doseq=True)
            return self.request("GET", case["path"] + ("?" + query if query else ""))
        status, answer = self.rpc("tools/call", {"name": case["tool"], "arguments": case.get("args", {})})
        if not isinstance(answer, dict) or "result" not in answer:
            return (502 if status < 400 else status), {"rpc_error": answer}
        result = answer["result"]
        body = result.get("structuredContent")
        if body is None:
            texts = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
            try:
                body = json.loads("\n".join(texts))
            except ValueError:
                body = texts
        return (422 if result.get("isError") else status), body


def normalized(value, key):
    if (key == "id" or key.endswith("_id")) and isinstance(value, str):
        if re.fullmatch(r"[0-9]+", value):
            return int(value)
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    if key in TIME_KEYS and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    return value


def differences(left, right, path="$", key=""):
    out = []
    def difference(kind, reason=None, classification="new_bug"):
        out.append({"path": path, "kind": kind, "python": left, "rust": right,
                    "classification": classification, "reason": reason})
    a, b = normalized(left, key), normalized(right, key)
    if not isinstance(a, (dict, list)) and a == b and type(a) is type(b):
        if type(left) is not type(right) or left != right:
            difference("representation", "ID/timestamp representation", "comparator_noise")
        return out
    numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if key in NOISE_KEYS and numeric(a) and numeric(b):
        difference("telemetry", "per-daemon timing or writer-only access counter", "comparator_noise")
    elif key in SCORE_KEYS | TIME_KEYS and numeric(a) and numeric(b) and abs(a - b) <= (SCORE_TOL if key in SCORE_KEYS else TIME_TOL):
        difference("tolerance", "score or timestamp within declared absolute tolerance", "comparator_noise")
    elif isinstance(a, dict) and isinstance(b, dict):
        for child in sorted(a.keys() | b.keys()):
            if child not in a or child not in b:
                out.append({"path": f"{path}.{child}", "kind": "missing_key", "python_present": child in a,
                            "rust_present": child in b, "classification": "new_bug"})
            else:
                out.extend(differences(a[child], b[child], f"{path}.{child}", child))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            difference("length")
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(differences(x, y, f"{path}[{i}]", key))
    else:
        difference("type" if type(a) is not type(b) else "value")
    return out


def compare(python, rust, cases, triage=()):
    # Validate the whole corpus before sending even the first request.
    for case in cases:
        validate_case(case)
    rules = {}
    for item in triage:
        if item["classification"] not in {"known_declared_divergence", "new_bug", "comparator_noise"}:
            raise ValueError("invalid triage classification")
        if item["classification"] == "known_declared_divergence":
            root = (HERE.parent / "divergences").resolve()
            ref = (root / item["reference"]).resolve()
            if not ref.is_relative_to(root) or not ref.is_file() or ref.suffix != ".md" or ref.name == "_section.md":
                raise ValueError("triage reference must name an existing divergence entry")
        if not item.get("reason"):
            raise ValueError("triage needs a reason")
        rules[item["case"], item["path"]] = item
    versions = {}
    for label, endpoint in (("python", python), ("rust", rust)):
        endpoint.replay_session = REPLAY_SESSION_PREFIX + uuid.uuid4().hex
        info = endpoint.initialize()
        status, health = endpoint.request("GET", "/health")
        if status != 200 or not isinstance(health, dict) or not health.get("ready"):
            raise ValueError(f"{label} daemon is not ready")
        if label == "rust" and health.get("read_only") is not True:
            raise ValueError("Rust endpoint is not a read-only shadow")
        versions[label] = {"version": health.get("version"), "build": health.get("build"), "serverInfo": info}
    results = []
    for case in cases:
        a, b = python.read(case), rust.read(case)
        diffs = differences({"status": a[0], "body": a[1]}, {"status": b[0], "body": b[1]})
        if a[0] >= 400 or b[0] >= 400:
            diffs.append({"path": "$", "kind": "read_error", "classification": "new_bug",
                          "python_status": a[0], "rust_status": b[0]})
        for diff in diffs:
            rule = rules.get((case["name"], diff["path"]))
            if rule:
                diff.update({k: rule[k] for k in ("classification", "reason", "reference") if k in rule})
        results.append({"case": case["name"], "request": case, "differences": diffs})
    counts = Counter(d["classification"] for r in results for d in r["differences"])
    kinds = Counter(d["kind"] for r in results for d in r["differences"])
    return {"format": 1, "daemons": versions, "tolerances": {"score_absolute": SCORE_TOL, "timestamp_seconds": TIME_TOL},
            "coverage": dict(Counter(c.get("tool", c.get("path")) for c in cases)),
            "cases": len(results), "equal_cases": sum(not r["differences"] for r in results),
            "differences_by_kind": dict(kinds), "classifications": dict(counts), "results": results}


def digest(summary):
    lines = [f"Shadow comparison: {summary['cases']} cases; {summary['equal_cases']} exactly equal.",
             "Classifications: " + json.dumps(summary["classifications"], sort_keys=True)]
    for label, info in summary["daemons"].items():
        lines.append(f"{label}: version={info['version']} build={json.dumps(info['build'], sort_keys=True)}")
    for result in summary["results"]:
        for diff in result["differences"]:
            lines.append(f"{result['case']} {diff['path']}: {diff['kind']} ({diff['classification']})"
                         + (f" — {diff['reason']}" if diff.get("reason") else ""))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-url", required=True)
    parser.add_argument("--rust-url", required=True)
    parser.add_argument("--bearer-env", default="PSEUDOLIFE_MCP_TOKEN")
    parser.add_argument("--python-bearer-env")
    parser.add_argument("--rust-bearer-env")
    parser.add_argument("--database-url-env")
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--triage", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.limit < 1:
            raise ValueError("limit must be positive")
        cases = corpus(args.queries, os.environ[args.database_url_env] if args.database_url_env else None, args.limit)
        endpoints = [Endpoint(url, os.environ.get(token or args.bearer_env)) for url, token in
                     ((args.python_url, args.python_bearer_env), (args.rust_url, args.rust_bearer_env))]
        triage = json.loads(args.triage.read_text(encoding="utf-8")) if args.triage else []
        summary = compare(*endpoints, cases, triage)
        args.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        args.out.with_suffix(".txt").write_text(digest(summary), encoding="utf-8")
        print(f"{summary['cases']} cases; {summary['classifications']}; JSON: {args.out}")
        return int(summary["classifications"].get("new_bug", 0) > 0)
    except (ValueError, KeyError, OSError, http.client.HTTPException) as exc:
        # DSNs and tokens never appear in diagnostic exception text.
        print(f"shadow comparison failed ({type(exc).__name__}); check input and endpoint readiness", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
