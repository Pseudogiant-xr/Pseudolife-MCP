"""Generate synthetic fixture entries; never read an existing bank or settings."""
from copy import deepcopy
import json
from pathlib import Path
import random
from types import SimpleNamespace

SEED = json.loads(Path(__file__).with_name("seed.json").read_text(encoding="utf-8"))


def bank(spec=SEED):
    rng = random.Random(spec["seed"])
    texts = ["memory search preserves ranking", "cortex facts supersede values",
             "coordination mail is addressed", "HTTP health reports status",
             "CLI errors carry an exit code", "memory search uses source filters"]
    return [{"id": i + 1, "text": texts[i], "source": "fixture",
             "bank": "flat", "score": round(0.95 - i * 0.07 + rng.random() * 0.01, 8)}
            for i in range(spec["entries"])]


class FixtureService:
    """Injected service seam; Python transport/projection handlers remain real.

    This is a deterministic transport fixture, not an implementation oracle
    for CMS, embedding, storage, cortex or coordination behaviour.
    """
    def __init__(self):
        self.entries = bank()
        self.config = SimpleNamespace(memory=SimpleNamespace(
            mcp=SimpleNamespace(compact_payloads=True, entry_text_chars=1200),
            cortex=SimpleNamespace(enabled=False, search_first=False)))

    def search(self, query, top_k=8, sources=None, **kwargs):
        entries = deepcopy(self.entries)
        if sources is not None:
            entries = [e for e in entries if e["source"] in sources]
        entries = entries[:max(0, top_k)]
        return {"query": query, "count": len(entries), "entries": entries,
                "cortex": [], "low_confidence": not entries}


def corpus():
    score_paths = ["/body/entries/*/score"]
    ranking_paths = ["/body/entries"]
    mcp_policy = {
        "json_text_paths": ["/body/result/content/*/text"],
        "score_paths": ["/body/result/content/*/text/entries/*/score",
                        "/body/result/structuredContent/entries/*/score"],
        "ranking_paths": ["/body/result/content/*/text/entries",
                          "/body/result/structuredContent/entries"],
    }

    def rpc(case_id, method, params=None, request_id=None, policy=None):
        body = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        return {"id": case_id, "surface": "mcp", "request": {
            "method": "POST", "path": "/mcp", "body": body}, "policy": policy or {}}

    return {"schema": 1, "fixture": {**SEED, "kind": "in-memory-transport-seam"}, "cases": [
        {"id": "cli-help", "surface": "cli", "request": {"argv": ["--help"]}},
        {"id": "cli-unknown", "surface": "cli", "request": {"argv": ["bogus"]}},
        {"id": "health", "surface": "http", "request": {"path": "/health"}},
        {"id": "redirect", "surface": "http", "request": {"path": "/"}},
        {"id": "http-search", "surface": "http", "request": {
            "path": "/api/search?q=memory&top_k=3"}, "policy": {
                "score_paths": score_paths, "ranking_paths": ranking_paths}},
        {"id": "http-empty", "surface": "http", "request": {
            "path": "/api/search?q=memory&source=missing"}},
        rpc("mcp-initialize", "initialize", {"protocolVersion": "2026-07-28",
            "capabilities": {}, "clientInfo": {"name": "synthetic-contract", "version": "1"}}, 1),
        rpc("mcp-initialized", "notifications/initialized"),
        rpc("mcp-tools", "tools/list", {}, 2),
        rpc("mcp-search", "tools/call", {"name": "memory_search", "arguments": {
            "query": "memory", "top_k": 3}}, 3, mcp_policy),
        rpc("mcp-missing-tool", "tools/call", {"name": "missing_fixture_tool", "arguments": {}}, 4),
    ]}


if __name__ == "__main__":
    print(json.dumps(corpus(), indent=2, ensure_ascii=False, allow_nan=False))
