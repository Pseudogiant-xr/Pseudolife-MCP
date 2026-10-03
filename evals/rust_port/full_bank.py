"""Synthetic-bank differential corpus; prepare is light, run loads the real daemon."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import random
import re
import sys

from .harness import HttpClient, Policy, compare, isolated_env, write_new
from .provenance import require_historical_source, source_metadata, runtime_metadata

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads(Path(__file__).with_name("full_seed.json").read_text(encoding="utf-8"))["seed"]


def locations(document, pointer):
    """Explicit JSON pointer, with one-segment wildcards; missing fields fail."""
    if not pointer.startswith("/"):
        raise ValueError("normalization pointer must be absolute")
    nodes = [(None, None, document)]
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        next_nodes = []
        for _, _, node in nodes:
            if not isinstance(node, (dict, list)):
                raise ValueError("normalization path crossed scalar")
            keys = list(range(len(node))) if isinstance(node, list) else list(node) if isinstance(node, dict) else []
            if token != "*":
                key = int(token) if isinstance(node, list) and token.isdigit() else token
                if key not in keys:
                    raise ValueError("normalization field missing")
                keys = [key]
            for key in keys:
                next_nodes.append((node, key, node[key]))
        nodes = next_nodes
    return nodes


class Normalizer:
    """Private bijective identity vault; safe transcripts contain only symbols."""
    def __init__(self):
        self.bindings = {}
        self.kinds = {}

    def resolve(self, value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return self.bindings[value["$ref"]]
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    @staticmethod
    def clock(value, kind):
        if kind == "nullable_epoch" and value is None:
            return None
        if kind in {"epoch", "nullable_epoch"}:
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid epoch")
            return value
        if kind == "iso":
            if not isinstance(value, str):
                raise ValueError("invalid ISO clock")
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        if kind == "hlc":
            if not isinstance(value, str) or not re.fullmatch(r"\d+:\d+", value):
                raise ValueError("invalid HLC")
            return tuple(map(int, value.split(":")))
        raise ValueError("unknown clock kind")

    def capture(self, name, kind, value):
        if kind not in {"uuid", "credential", "epoch", "hlc"}:
            raise ValueError("unknown binding kind")
        if kind == "uuid" and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)):
            raise ValueError("invalid identity")
        if kind == "credential" and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value)):
            raise ValueError("invalid credential")
        if kind in {"epoch", "hlc"}:
            self.clock(value, kind)
        if name in self.bindings:
            if (type(self.bindings[name]) is not type(value) or
                    self.bindings[name] != value or self.kinds[name] != kind):
                raise ValueError("binding changed")
        elif kind in {"uuid", "credential"} and any(
                self.kinds[n] == kind and v == value for n, v in self.bindings.items()):
            raise ValueError("identity alias collision")
        else:
            self.bindings[name], self.kinds[name] = value, kind
        return self.symbol(name, kind, value)

    @staticmethod
    def symbol(name, kind, value):
        # Epoch values vary between equivalent banks; their exact JSON numeric
        # types remain part of the response contract after normalization.
        if kind in {"epoch", "nullable_epoch"} and value is not None:
            return f"<{name}:{type(value).__name__}>"
        return f"<{name}>"

    def apply(self, raw, rules):
        document = copy.deepcopy(raw)
        embedded = []
        for path in rules.get("json_text_paths", []):
            for parent, key, value in locations(document, path):
                parent[key] = json.loads(value)
                embedded.append((parent, key))
        for path, kind in rules.get("times", {}).items():
            for _, _, value in locations(document, path):
                self.clock(value, kind)
        for constraint in rules.get("constraints", []):
            if constraint["op"] not in {"lt", "eq"}:
                raise ValueError("unknown constraint operation")
            left = locations(document, constraint["left"])
            right = locations(document, constraint["right"])
            if len(left) != 1 or len(right) != 1:
                raise ValueError("constraint requires singleton fields")
            a, b = left[0][2], right[0][2]
            if constraint["op"] == "lt" and not a < b:
                raise ValueError("clock order changed")
            if constraint["op"] == "eq" and (type(a) is not type(b) or a != b):
                raise ValueError("state constraint changed")
        for path, specification in rules.get("captures", {}).items():
            for parent, key, value in locations(document, path):
                parent[key] = self.capture(specification["name"], specification["kind"], value)
        for path, name in rules.get("references", {}).items():
            for parent, key, value in locations(document, path):
                if type(value) is not type(self.bindings[name]) or value != self.bindings[name]:
                    raise ValueError("identity reference changed")
                parent[key] = self.symbol(name, self.kinds[name], value)
        for path, name in rules.get("cursors", {}).items():
            for parent, key, value in locations(document, path):
                prefix = self.bindings[name] + ":"
                if not isinstance(value, str) or not value.startswith(prefix) or not value[len(prefix):].isdigit():
                    raise ValueError("invalid mailbox cursor")
                parent[key] = f"<{name}>:" + value[len(prefix):]
        for path, kind in rules.get("times", {}).items():
            for parent, key, value in locations(document, path):
                self.clock(value, kind)
                parent[key] = None if value is None else self.symbol(kind, kind, value)
        for parent, key in embedded:
            parent[key] = json.dumps(parent[key], sort_keys=True, separators=(",", ":"))
        return document


def fixture_entries(seed=SEED):
    rng = random.Random(seed)
    sentences = (
        "The apricot rover carries an amber navigation beacon calibrated to {} hertz.",
        "The birch greenhouse irrigates its violet orchids with {} millilitres each morning.",
        "The cobalt observatory stores telescope exposure number {} in its north archive.")
    return [{"text": text.format(rng.randrange(100, 999)), "source": "synthetic-differential", "origin": "agent"}
            for text in sentences]


def full_corpus(seed=SEED):
    cases = []
    def http(name, path, body=None, *, auth="valid", identity=None, status=200, operation=None):
        request = {"path": path, "method": "POST" if body is not None else "GET", "auth": auth}
        if body is not None:
            request["body"] = body
        if identity:
            request["identity"] = identity
        cases.append({"id": name, "surface": "http", "request": request,
                      "expect": {"status": status}, "operation": operation or name})
    def rpc(name, method, params=None, operation=None, notification=False):
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = len(cases) + 1
        if params is not None:
            body["params"] = params
        http(name, "/mcp", body, status=202 if notification else 200, operation=operation)
        cases[-1]["surface"] = "mcp"
    def tool(name, tool_name, arguments, operation=None):
        rpc(name, "tools/call", {"name": tool_name, "arguments": arguments}, operation)
    http("search-unauthorized", "/api/search?q=apricot", auth="absent", status=401)
    http("search-wrong-bearer", "/api/search?q=apricot", auth="invalid", status=401)
    rpc("initialize", "initialize", {"protocolVersion": "2026-07-28", "capabilities": {},
        "clientInfo": {"name": "synthetic-differential", "version": "1"}})
    rpc("initialized", "notifications/initialized", notification=True)
    rpc("list-tools", "tools/list")
    for i, entry in enumerate(fixture_entries(seed)):
        tool(f"seed-store-{i}", "memory_store", entry, "seed-store")
    tool("store-empty", "memory_store", {"text": "", "source": "synthetic-differential"})
    tool("unknown-tool", "synthetic_unknown_tool", {})
    cases[-1]["expect"]["tool_error"] = True
    query = {"query": "apricot rover amber navigation beacon", "top_k": 3,
             "sources": ["synthetic-differential"], "disable_recency_boost": True,
             "rerank": False, "bm25": False, "min_score": -1.0}
    tool("search-mcp", "memory_search", query, "search")
    http("search-http", "/api/search?q=apricot%20rover%20amber&top_k=3&source=synthetic-differential&min_score=-1&disable_recency_boost=true&rerank=false&bm25=false", operation="search")
    fact = {"entity": "synthetic-apricot-rover", "attribute": "beacon_colour", "value": "amber",
            "origin": "user", "confidence": 1.0, "freshness_class": "evergreen"}
    tool("fact-set", "memory_fact_set", fact)
    http("fact-update", "/api/facts/set", {**fact, "value": "violet"})
    tool("fact-get", "memory_fact_get", {"entity": fact["entity"], "attribute": fact["attribute"], "verbose": True})
    tool("fact-history", "memory_history", {"entity": fact["entity"], "attribute": fact["attribute"]})
    for name in ("sender", "recipient"):
        http("register-" + name, "/api/coordination/register", {
            "label": "synthetic-" + name, "project": "synthetic-differential", "task": "wire-contract"})
    for ordinal in ("first", "second"):
        http("send-" + ordinal, "/api/coordination/send", {"to": {"$ref": "recipient.agent_id"},
            "text": "synthetic " + ordinal, "request_id": "synthetic-" + ordinal}, identity="sender")
    http("send-missing-text", "/api/coordination/send", {"to": {"$ref": "recipient.agent_id"},
         "request_id": "synthetic-invalid"}, identity="sender", status=400)
    http("receive-ordered", "/api/coordination/receive", {"limit": 8}, identity="recipient")
    http("ack-first", "/api/coordination/ack", {"message_id": {"$ref": "first.message_id"}}, identity="recipient")
    http("receive-after-ack", "/api/coordination/receive", {"limit": 8}, identity="recipient")
    http("receive-wrong-identity", "/api/coordination/receive", {"limit": 8}, identity="wrong-recipient", status=403)
    return {"schema": 1, "seed": seed, "fixture_entries": fixture_entries(seed), "cases": cases,
            "scope": "real Python daemon, real CPU embedder, two newly minted PostgreSQL banks; no Rust daemon"}


FACT_CLOCKS = {"asserted_at": "epoch", "last_confirmed": "epoch", "superseded_at": "nullable_epoch",
               "tx_time": "epoch", "valid_time": "nullable_epoch"}


def normalize_payload(normal, payload, operation):
    """Only declared response shapes receive clock or identity normalization."""
    rules = {}
    if operation.startswith("register-"):
        actor = operation.removeprefix("register-")
        rules = {"captures": {"/agent_id": {"name": actor + ".agent_id", "kind": "uuid"},
                              "/credential": {"name": actor + ".credential", "kind": "credential"}},
                 "times": {"/created_at": "epoch", "/last_activity": "epoch"}}
        rules["constraints"] = [{"left": "/created_at", "op": "eq", "right": "/last_activity"}]
    elif operation.startswith("send-") or operation == "ack-first":
        ordinal = "first" if operation == "ack-first" else operation.removeprefix("send-")
        rules = {"captures": {"/message_id": {"name": ordinal + ".message_id", "kind": "uuid"},
                              "/created_at": {"name": ordinal + ".created_at", "kind": "epoch"},
                              "/expires_at": {"name": ordinal + ".expires_at", "kind": "epoch"}},
                 "times": {"/acknowledged_at": "nullable_epoch"},
                 "constraints": [{"left": "/created_at", "op": "lt", "right": "/expires_at"}]}
        if operation.startswith("send-"):
            rules["references"] = {"/recipient_agent_id": "recipient.agent_id"}
        if payload["expires_at"] - payload["created_at"] != 86400:
            raise ValueError("mail lifetime changed")
        if payload["state"] != ("acknowledged" if operation == "ack-first" else "queued"):
            raise ValueError("mail receipt state changed")
        if operation == "ack-first" and not payload["created_at"] <= payload["acknowledged_at"] < payload["expires_at"]:
            raise ValueError("mail acknowledgment clock changed")
    elif operation in {"receive-ordered", "receive-after-ack"}:
        expected = ["first", "second"] if operation == "receive-ordered" else ["second"]
        if [m["text"] for m in payload["messages"]] != ["synthetic " + o for o in expected]:
            raise ValueError("mail state or order changed")
        rules = {"references": {}, "captures": {}, "cursors": {"/after": "recipient.agent_id"}}
        clocks = []
        for i, ordinal in enumerate(expected):
            prefix = f"/messages/{i}/"
            rules["references"].update({prefix + "message_id": ordinal + ".message_id",
                prefix + "sender_agent_id": "sender.agent_id", prefix + "recipient_agent_id": "recipient.agent_id",
                prefix + "created_at": ordinal + ".created_at", prefix + "expires_at": ordinal + ".expires_at"})
            rules["captures"][prefix + "hlc"] = {"name": ordinal + ".hlc", "kind": "hlc"}
            clocks.append(normal.clock(payload["messages"][i]["hlc"], "hlc"))
            if payload["messages"][i]["recipient_sequence"] != (1 if ordinal == "first" else 2):
                raise ValueError("mail sequence changed")
        if len(clocks) == 2 and not clocks[0] < clocks[1]:
            raise ValueError("mail HLC order changed")
    elif operation in {"fact-set", "fact-update", "fact-get", "fact-history"}:
        prefixes = ["/versions/" + str(i) for i in range(len(payload["versions"]))] if operation == "fact-history" else ["/record"] if operation == "fact-get" else [""]
        rules = {"times": {}, "captures": {}}
        for prefix in prefixes:
            record = payload if not prefix else locations(payload, prefix)[0][2]
            for key, kind in FACT_CLOCKS.items():
                path = prefix + "/" + key
                if kind == "epoch":
                    rules["captures"][path] = {"name": "fact." + record["value"] + "." + key, "kind": kind}
                else:
                    rules["times"][path] = kind
            if record["asserted_at"] > record["last_confirmed"]:
                raise ValueError("fact confirmation precedes assertion")
        if operation == "fact-set" and payload.get("action") != "inserted":
            raise ValueError("fact insert failed")
        if operation == "fact-update" and payload.get("action") != "superseded":
            raise ValueError("fact update failed")
        if operation == "fact-update" and not normal.bindings["fact.amber.asserted_at"] < payload["asserted_at"]:
            raise ValueError("fact correction clock order failed")
        if operation == "fact-get" and payload["record"]["value"] != "violet":
            raise ValueError("fact read state failed")
        if operation == "fact-history" and [v["value"] for v in payload["versions"]] != ["amber", "violet"]:
            raise ValueError("fact history order failed")
        if operation == "fact-history" and payload["versions"][0]["superseded_at"] != payload["versions"][1]["asserted_at"]:
            raise ValueError("fact supersession clock changed")
    elif operation == "search":
        ids = [entry["id"] for entry in payload["entries"]]
        if len(ids) != 3 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != 3:
            raise ValueError("synthetic retrieval did not return three distinct stored identities")
        # Compact MCP responses have dates; REST responses have epoch clocks.
        rules = {"times": {f"/entries/{i}/{key}": kind for i, entry in enumerate(payload["entries"])
                            for key, kind in {"timestamp": "epoch", "superseded_at": "nullable_epoch"}.items()
                            if key in entry}}
    elif operation == "seed-store" and payload.get("stored") is not True:
        raise ValueError("synthetic seed store rejected")
    elif operation == "store-empty" and (payload.get("stored") is not False or payload.get("reason") != "empty"):
        raise ValueError("empty store contract failed")
    return normal.apply(payload, rules)


def observe(corpus, url, token):
    normal, client, records = Normalizer(), HttpClient(url, timeout=120), []
    for case in corpus["cases"]:
        request = normal.resolve(case["request"])
        auth = request.get("auth", "valid")
        headers = {"Authorization": "Bearer " + (token if auth == "valid" else "synthetic-invalid")} if auth != "absent" else {}
        headers["X-PL-Writer"] = "synthetic-contract"
        identity = request.get("identity")
        if identity:
            actor = "recipient" if identity == "wrong-recipient" else identity
            key_actor = "sender" if identity == "wrong-recipient" else identity
            headers.update({"X-PL-Agent": normal.bindings[actor + ".agent_id"],
                            "X-PL-Agent-Key": normal.bindings[key_actor + ".credential"]})
        response = client.execute(request, case["surface"], runtime_headers=headers)
        if response["status"] != case["expect"]["status"]:
            raise ValueError("response status contract failed: " + case["id"])
        if case["surface"] == "mcp" and response["body"] and "result" in response["body"]:
            result = response["body"]["result"]
            if bool(result.get("isError")) != case["expect"].get("tool_error", False):
                raise ValueError("tool contract failed: " + case["id"])
            if "structuredContent" in result:
                result["structuredContent"] = normalize_payload(normal, result["structuredContent"], case["operation"])
            for item in result.get("content", []):
                if item.get("type") == "text" and not result.get("isError"):
                    payload = normalize_payload(normal, json.loads(item["text"]), case["operation"])
                    item["text"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        elif response["status"] == 200 and case["surface"] == "http":
            response["body"] = normalize_payload(normal, response["body"], case["operation"])
        records.append({**case, "response": response})
        serialized = json.dumps(records[-1], allow_nan=False)
        credentials = [v for name, v in normal.bindings.items() if normal.kinds[name] == "credential"]
        if any(secret in serialized for secret in [token, *credentials]):
            raise ValueError("credential escaped response normalization")
        print(json.dumps({"case": case["id"], "observed": True}), flush=True)
    return records


def environment_metadata(root=ROOT):
    from evals.rust_baseline.common import provenance
    return {**source_metadata(root), "parent_runtime": runtime_metadata(ROOT),
        "baseline_instrument_sha256": provenance(source_root=root)["instrument_sha256"]}


def private_home_overrides(private):
    home = Path(private) / "home"
    env = isolated_env(home)
    names = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")
    overrides = {k: env[k] for k in names}
    # Offline model artifacts are read from an explicit cache, never settings.
    cache_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    overrides["HF_HOME"] = str(home / "huggingface")
    overrides["HF_HUB_CACHE"] = os.environ.get("HF_HUB_CACHE", str(cache_home / "hub"))
    return overrides


def run(corpus, board_checked_at=None, score_abs_tol=1e-6, oracle_root=ROOT, *, offline_resource_checked_at=None):
    require_historical_source(oracle_root)
    from evals.rust_baseline.common import lease_gate
    from evals.rust_baseline.daemon import disposable_database, launched_daemon, private_directory
    from evals.rust_baseline.transport import TOKEN
    import inspect
    if "env_extra" not in inspect.signature(launched_daemon).parameters:
        raise RuntimeError("baseline launcher needs env_extra home-isolation interface")
    if "source_root" not in inspect.signature(launched_daemon).parameters:
        raise RuntimeError("baseline launcher needs source_root oracle interface")
    resource = (lease_gate(board_checked_at, offline_resource_checked_at=offline_resource_checked_at)
                if offline_resource_checked_at else lease_gate(board_checked_at))
    passes, cleanup_results = [], []
    for _ in range(2):
        with disposable_database() as dsn, private_directory() as private:
            with launched_daemon(dsn, private, env_extra=private_home_overrides(private), source_root=oracle_root) as (_, url, cleanup):
                passes.append(observe(corpus, url, TOKEN))
            cleanup_results.append({**cleanup, "database_dropped": False})
        cleanup_results[-1]["database_dropped"] = True
    differences = []
    policy = Policy(abs_tol=score_abs_tol, score_paths=("/body/entries/*/score", "/body/result/structuredContent/entries/*/score",
        "/body/result/content/*/text/entries/*/score"), json_text_paths=("/body/result/content/*/text",),
        ranking_paths=("/body/entries", "/body/result/structuredContent/entries", "/body/result/content/*/text/entries"))
    for expected, actual in zip(*passes):
        case_policy = Policy(abs_tol=score_abs_tol) if expected["expect"].get("tool_error") else policy
        differences.extend({"case": expected["id"], **difference} for difference in compare(
            expected["response"], actual["response"], case_policy))
    return {"schema": 1, "records": passes[0]}, {"schema": 1, "status": "passed" if not differences else "failed",
        "cases": len(passes[0]), "differences": differences, "cleanup": cleanup_results,
        "resource_check": resource, "environment": environment_metadata(oracle_root), "seed": corpus["seed"],
        "scope": corpus["scope"], "score_abs_tolerance": policy.abs_tol,
        "ranking_order": "strict; sequential integer memory IDs retained exactly"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run"))
    parser.add_argument("--out-dir", type=Path, required=True)
    clearance = parser.add_mutually_exclusive_group()
    clearance.add_argument("--board-checked-at")
    clearance.add_argument("--offline-resource-checked-at")
    parser.add_argument("--score-abs-tol", type=float, default=1e-6)
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    args = parser.parse_args()
    corpus = full_corpus()
    write_new(args.out_dir / "corpus.json", corpus)
    if args.mode == "prepare":
        write_new(args.out_dir / "prepared.json", {"schema": 1, "status": "prepared-not-executed",
            "cases": len(corpus["cases"]), "environment": environment_metadata(args.oracle_root.resolve()), "seed": SEED,
            "limitation": "No real daemon or PostgreSQL-bank request has executed in this preparation artifact."})
        return 0
    if not (args.board_checked_at or args.offline_resource_checked_at):
        parser.error("run requires the lead's board or independent offline resource clearance timestamp")
    try:
        transcript, receipt = run(corpus, args.board_checked_at, args.score_abs_tol, args.oracle_root.resolve(),
                                  offline_resource_checked_at=args.offline_resource_checked_at)
    except Exception as exc:
        write_new(args.out_dir / "run.json", {"schema": 1, "status": "failed", "error_type": type(exc).__name__,
                                             "scope": corpus["scope"]})
        raise
    write_new(args.out_dir / "python-oracle.json", transcript)
    write_new(args.out_dir / "run.json", receipt)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
