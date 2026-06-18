"""Dream pass — pluggable extractor, driver, status, MCP wiring.

Three tiers:
* pure config + RegexExtractor logic (no embedder, no PG — fast);
* PG-backed driver/status tests with the real embedder (skip cleanly
  without a test server).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


# ── stub OpenAI-compatible server (Tier 2 tests; no PG, no embedder) ──────

class _StubHandler(http.server.BaseHTTPRequestHandler):
    responder = None  # (status, body_str) callable, set per subclass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        status, body = type(self).responder()
        data = body.encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


@contextlib.contextmanager
def _stub_server(responder):
    handler = type("H", (_StubHandler,), {"responder": staticmethod(responder)})
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _chat_payload(claims):
    return json.dumps({"choices": [{"message": {
        "content": json.dumps({"claims": claims})}}]})


# ── config ───────────────────────────────────────────────────────────────

def test_dream_config_defaults():
    from pseudolife_memory.utils.config import DreamConfig, MemoryConfig

    c = DreamConfig()
    assert c.enabled is True
    assert c.exclude_sources == ["consolidation", "reflection"]
    assert c.eligible_sources is None          # None => all-but-excluded
    assert c.min_batch == 8 and c.idle_seconds == 1800.0
    assert MemoryConfig().dream.max_batch == 40


# ── RegexExtractor (no LLM, no embedder) ─────────────────────────────────

def test_regex_extractor_pulls_slot_claims():
    from pseudolife_memory.memory.dream import RegexExtractor

    claims = RegexExtractor().extract(
        ["the build timeout is 4500 seconds", "unrelated chatter"], vocab=[],
    )
    assert any(c["attribute"] == "timeout" and "4500" in c["value"] for c in claims)
    assert all({"entity", "attribute", "value", "confidence", "origin"} <= c.keys()
               for c in claims)


def test_regex_extractor_empty_on_no_slots():
    from pseudolife_memory.memory.dream import RegexExtractor

    assert RegexExtractor().extract(["hello there"], vocab=[]) == []


# ── OpenAICompatExtractor + factory (Tier 2) ─────────────────────────────

def test_openai_extractor_parses_claims():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([{"entity": "svc", "attribute": "port",
                              "value": "8080", "confidence": 0.9}])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(["whatever"], vocab=[])
    assert claims == [{"entity": "svc", "attribute": "port", "value": "8080",
                       "confidence": 0.9, "origin": "agent"}]


def test_openai_extractor_empty_on_timeout():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    def slow():
        time.sleep(1.0)
        return (200, _chat_payload([]))

    with _stub_server(slow) as base_url:
        ext = OpenAICompatExtractor(base_url, "m", timeout_seconds=0.2)
        assert ext.extract(["x"], vocab=[]) == []


def test_openai_extractor_empty_on_malformed():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    bad = json.dumps({"choices": [{"message": {"content": "not json at all"}}]})
    with _stub_server(lambda: (200, bad)) as base_url:
        assert OpenAICompatExtractor(base_url, "m").extract(["x"], vocab=[]) == []


def test_build_extractor_selects_by_config(monkeypatch):
    from pseudolife_memory.memory.dream import (
        OpenAICompatExtractor, RegexExtractor, build_extractor,
    )
    from pseudolife_memory.utils.config import DreamConfig

    monkeypatch.delenv("PSEUDOLIFE_DREAM_BASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_DREAM_MODEL", raising=False)
    # Unconfigured => regex floor.
    assert isinstance(build_extractor(DreamConfig()), RegexExtractor)
    # Configured via dataclass => Tier 2.
    cfg = DreamConfig(extractor_base_url="http://x", extractor_model="m")
    assert isinstance(build_extractor(cfg), OpenAICompatExtractor)
    # Env overrides the (empty) dataclass.
    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "http://env")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "envm")
    ext = build_extractor(DreamConfig())
    assert isinstance(ext, OpenAICompatExtractor) and ext.base_url == "http://env"


# ── sweep gate (pure; fake service) ──────────────────────────────────────

class _FakeService:
    def __init__(self, *, enabled=True, would_fire=True, backlog=5):
        from pseudolife_memory.utils.config import AppConfig
        self.config = AppConfig()
        self.config.memory.dream.enabled = enabled
        self._would_fire = would_fire
        self._backlog = backlog
        self.ran = False

    def dream_status(self):
        return {"backlog": self._backlog, "idle_seconds": 0.0,
                "dream_cursor": 0.0, "would_fire": self._would_fire}

    def dream_run(self, extractor):
        self.ran = True
        return {"pulled": 1, "claims": 1, "inserted": 1, "confirmed": 0,
                "contested": 0, "superseded": 0, "cursor": 123.0}


def test_run_sweep_once_disabled():
    from pseudolife_memory.memory.dream import run_sweep_once

    svc = _FakeService(enabled=False)
    out = run_sweep_once(svc)
    assert out["fired"] is False and out["reason"] == "disabled" and not svc.ran


def test_run_sweep_once_below_threshold():
    from pseudolife_memory.memory.dream import run_sweep_once

    svc = _FakeService(would_fire=False, backlog=2)
    out = run_sweep_once(svc)
    assert out["fired"] is False and out["backlog"] == 2 and not svc.ran


def test_run_sweep_once_fires():
    from pseudolife_memory.memory.dream import run_sweep_once

    svc = _FakeService(would_fire=True)
    out = run_sweep_once(svc)
    assert out["fired"] is True and out["inserted"] == 1 and svc.ran


# ── driver / status (PG-backed; real embedder) ───────────────────────────

@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


def test_dream_pull_includes_non_conversation_sources(svc):
    svc.store("the widget port is 9999", source="notes")          # newly eligible
    svc.store("a consolidated synthesis", source="consolidation")  # stays excluded
    out = svc.dream_pull(limit=10)
    texts = [e["text"] for e in out["entries"]]
    assert any("widget port" in t for t in texts)
    assert all("consolidated synthesis" not in t for t in texts)


def test_dream_run_promotes_and_advances_cursor(svc):
    from pseudolife_memory.memory.dream import RegexExtractor

    svc.store("the gadget version is 3.2", source="notes")
    out = svc.dream_run(RegexExtractor())
    assert out["pulled"] >= 1
    assert out["inserted"] + out["confirmed"] >= 1
    assert out["cursor"] > 0
    fact = svc.cortex_lookup("gadget", "version")
    assert fact is not None and "3.2" in fact["value"]
    # Idempotent: a second run over the same (now-consolidated) tail is a no-op.
    again = svc.dream_run(RegexExtractor())
    assert again["pulled"] == 0


def test_dream_status_would_fire_on_idle(svc):
    svc.config.memory.dream.min_batch = 100        # never fires on batch
    svc.config.memory.dream.idle_seconds = 0.0     # everything counts as idle
    svc.store("the relay port is 4001", source="notes")
    st = svc.dream_status()
    assert st["backlog"] >= 1
    assert st["would_fire"] is True
    assert "dream_cursor" in st and "idle_seconds" in st


class _StubExtractor:
    """Returns a fixed claim list regardless of input (drives dream_run)."""
    def __init__(self, claims):
        self._claims = claims
    def extract(self, texts, vocab):
        return [dict(c) for c in self._claims]


def test_dream_resolves_paraphrased_slot_and_supersedes(svc):
    svc.config.memory.cortex.dream_slot_match_threshold = 0.3  # on
    svc.store("payments-db host is db-prod-1", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "payments-db", "attribute": "host",
        "value": "db-prod-1", "confidence": 0.6, "origin": "agent"}]))
    svc.store("payments database host is db-prod-2", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "payments database", "attribute": "host",
        "value": "db-prod-2", "confidence": 0.6, "origin": "agent"}]))
    # paraphrased entity resolved onto the existing slot -> supersede, not fork
    assert out["superseded"] >= 1
    cur = svc.cortex_lookup("payments-db", "host")
    assert cur is not None and "db-prod-2" in cur["value"]
    assert svc.cortex_lookup("payments database", "host") is None  # no sibling slot


def test_dream_threshold_off_forks_sibling(svc):
    svc.config.memory.cortex.dream_slot_match_threshold = 0.0  # off (default)
    svc.store("payments-db host is db-prod-1", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "payments-db", "attribute": "host",
        "value": "db-prod-1", "confidence": 0.6, "origin": "agent"}]))
    svc.store("payments database host is db-prod-2", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "payments database", "attribute": "host",
        "value": "db-prod-2", "confidence": 0.6, "origin": "agent"}]))
    a = svc.cortex_lookup("payments-db", "host")
    b = svc.cortex_lookup("payments database", "host")
    assert a is not None and "db-prod-1" in a["value"]   # NOT superseded
    assert b is not None and "db-prod-2" in b["value"]   # separate sibling slot
