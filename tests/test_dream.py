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


def _chat_relations_payload(relations):
    return json.dumps({"choices": [{"message": {
        "content": json.dumps({"relations": relations})}}]})


def test_openai_extractor_parses_relations():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_relations_payload([
        {"src": "checkout-service", "relation": "runs-on", "dst": "host-1",
         "confidence": 0.8}])
    with _stub_server(lambda: (200, payload)) as base_url:
        rels = OpenAICompatExtractor(base_url, "m").extract_relations(
            ["whatever"], [("runs-on", "src executes on host dst")])
    assert rels == [{"src": "checkout-service", "relation": "runs-on",
                     "dst": "host-1", "confidence": 0.8}]


def test_openai_extractor_relations_raises_on_malformed():
    from pseudolife_memory.memory.dream import ExtractorError, OpenAICompatExtractor

    bad = json.dumps({"choices": [{"message": {"content": "not json"}}]})
    with _stub_server(lambda: (200, bad)) as base_url:
        with pytest.raises(ExtractorError):
            OpenAICompatExtractor(base_url, "m").extract_relations(
                ["x"], [("runs-on", "d")])


# ── config ───────────────────────────────────────────────────────────────

def test_dream_config_defaults():
    from pseudolife_memory.utils.config import DreamConfig, MemoryConfig

    c = DreamConfig()
    assert c.enabled is True
    assert c.exclude_sources == ["consolidation", "reflection", "status", "log"]
    assert c.eligible_sources is None          # None => all-but-excluded
    assert c.min_batch == 8 and c.idle_seconds == 600.0
    assert MemoryConfig().dream.max_batch == 40
    assert c.known_facts_window == 0            # known-facts window off by default


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


def test_noop_extractor_returns_empty():
    from pseudolife_memory.memory.dream import NoOpExtractor

    # Even on clearly slot-shaped text, the no-op writes nothing (single-writer:
    # the LLM dream is the sole automatic cortex writer).
    assert NoOpExtractor().extract(["the build timeout is 4500 seconds"], vocab=[]) == []


# ── OpenAICompatExtractor + factory (Tier 2) ─────────────────────────────

def test_openai_extractor_parses_claims():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([{"entity": "svc", "attribute": "port",
                              "value": "8080", "confidence": 0.9}])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(["whatever"], vocab=[])
    assert claims == [{"entity": "svc", "attribute": "port", "value": "8080",
                       "confidence": 0.9, "origin": "agent"}]


def test_openai_extractor_numbers_notes_and_parses_source():
    # Batched extraction: the notes are numbered in the prompt so the model can
    # cite which note each claim came from ("source", 1-based); the extractor
    # maps it back to a 0-based index. Out-of-range/missing sources are dropped.
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    seen_bodies = []

    class _CapturingHandler(_StubHandler):
        @staticmethod
        def responder():
            return (200, _chat_payload([
                {"entity": "svc", "attribute": "port", "value": "8080",
                 "confidence": 0.9, "source": 2},
                {"entity": "svc", "attribute": "host", "value": "h1",
                 "confidence": 0.9, "source": "1"},      # string form accepted
                {"entity": "svc", "attribute": "os", "value": "linux",
                 "confidence": 0.9, "source": 99},        # out of range -> dropped
            ]))

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            seen_bodies.append(json.loads(self.rfile.read(length).decode()))
            status, body = self.responder()
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        claims = OpenAICompatExtractor(base_url, "m").extract(
            ["first note", "second note"], vocab=[])
    finally:
        srv.shutdown()
    user_msg = seen_bodies[0]["messages"][1]["content"]
    assert "[1] first note" in user_msg and "[2] second note" in user_msg
    by_attr = {c["attribute"]: c for c in claims}
    assert by_attr["port"]["source"] == 1        # 1-based 2 -> index 1
    assert by_attr["host"]["source"] == 0        # "1" -> index 0
    assert "source" not in by_attr["os"]         # 99 out of range


def test_openai_extractor_raises_on_timeout():
    # Failure must RAISE (not return []) so the dream can tell it apart from a
    # genuine empty result and avoid advancing the cursor past these memories.
    from pseudolife_memory.memory.dream import ExtractorError, OpenAICompatExtractor

    def slow():
        time.sleep(1.0)
        return (200, _chat_payload([]))

    with _stub_server(slow) as base_url:
        ext = OpenAICompatExtractor(base_url, "m", timeout_seconds=0.2)
        with pytest.raises(ExtractorError):
            ext.extract(["x"], vocab=[])


def test_openai_extractor_raises_on_malformed():
    from pseudolife_memory.memory.dream import ExtractorError, OpenAICompatExtractor

    bad = json.dumps({"choices": [{"message": {"content": "not json at all"}}]})
    with _stub_server(lambda: (200, bad)) as base_url:
        with pytest.raises(ExtractorError):
            OpenAICompatExtractor(base_url, "m").extract(["x"], vocab=[])


def test_build_extractor_selects_by_config(monkeypatch):
    from pseudolife_memory.memory.dream import (
        NoOpExtractor, OpenAICompatExtractor, build_extractor,
    )
    from pseudolife_memory.utils.config import DreamConfig

    monkeypatch.delenv("PSEUDOLIFE_DREAM_BASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_DREAM_MODEL", raising=False)
    # Unconfigured => no-op (the regex floor is no longer an automatic cortex writer).
    assert isinstance(build_extractor(DreamConfig()), NoOpExtractor)
    # Configured via dataclass => Tier 2.
    cfg = DreamConfig(extractor_base_url="http://x", extractor_model="m")
    assert isinstance(build_extractor(cfg), OpenAICompatExtractor)
    # Env overrides the (empty) dataclass.
    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "http://env")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "envm")
    ext = build_extractor(DreamConfig())
    assert isinstance(ext, OpenAICompatExtractor) and ext.base_url == "http://env"
    # Default timeout is CPU-realistic (a full 1024-tok gen at ~30 tok/s ≈ 30s).
    assert ext.timeout >= 120.0
    # Env overrides timeout + max_tokens (junk values fall back to the dataclass).
    monkeypatch.setenv("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS", "200")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MAX_TOKENS", "256")
    ext2 = build_extractor(DreamConfig())
    assert ext2.timeout == 200.0 and ext2.max_tokens == 256
    monkeypatch.setenv("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS", "notanumber")
    assert build_extractor(DreamConfig()).timeout == DreamConfig().extractor_timeout_seconds


def test_build_extractor_config_source_ignores_env(monkeypatch):
    """extractor_source="config" (the Console's Extractor panel) must win over
    the PSEUDOLIFE_DREAM_* env vars the compose file always sets — except the
    api key, which stays env-honoured in both modes (secrets never live in
    config.yaml)."""
    from pseudolife_memory.memory.dream import (
        NoOpExtractor, OpenAICompatExtractor, build_extractor,
    )
    from pseudolife_memory.utils.config import DreamConfig

    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "http://env")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "envm")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS", "200")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MAX_TOKENS", "256")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_API_KEY", "sekret")
    cfg = DreamConfig(extractor_source="config",
                      extractor_base_url="http://cfg", extractor_model="cfgm",
                      extractor_timeout_seconds=99.0, extractor_max_tokens=512)
    ext = build_extractor(cfg)
    assert isinstance(ext, OpenAICompatExtractor)
    assert ext.base_url == "http://cfg" and ext.model == "cfgm"
    assert ext.timeout == 99.0 and ext.max_tokens == 512
    assert ext.api_key == "sekret"
    # config mode with no endpoint set => NoOp, even though env points somewhere.
    assert isinstance(build_extractor(DreamConfig(extractor_source="config")),
                      NoOpExtractor)


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

    def compact_superseded(self):
        # Mirrors MemoryService.compact_superseded (runs on every sweep tick).
        return {"facts": 0, "world_facts": 0, "lessons": 0, "total": 0}

    def dream_run(self, extractor):
        self.ran = True
        return {"pulled": 1, "claims": 1, "inserted": 1, "confirmed": 0,
                "contested": 0, "superseded": 0, "cursor": 123.0}

    def dream_run_auto(self, *, limit=None):
        return {**self.dream_run(None), "extractor": "primary"}


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


def test_dream_run_stamps_relation_entities_with_entry_sources(svc):
    # Regression (2026-07-19, caught in live verification): dream_pull's entry
    # dicts dropped the source field, so dream_run's call site silently built
    # an EMPTY batch_sources set and relation endpoints stayed unattributed —
    # the one link in the stamping chain no unit test covered.
    class _BothStub:
        def extract(self, texts, vocab, known_facts=None):
            return []
        def extract_relations(self, texts, registry):
            return [{"src": "stamp-e2e-svc", "relation": "runs-on",
                     "dst": "stamp-e2e-host"}]

    svc.store("stamp-e2e probe mention", source="stamp-e2e-proj")
    out = svc.dream_run(_BothStub())
    assert out["relations"] == 1
    from pseudolife_memory.graph import norm_name
    st = svc._storage
    for name in ("stamp-e2e-svc", "stamp-e2e-host"):
        eid = st.find_entity(norm_name(name))["id"]
        assert "stamp-e2e-proj" in {
            r["source"] for r in st.sources_for_entity(eid)}, name


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


def test_dream_with_noop_extractor_writes_nothing(svc):
    from pseudolife_memory.memory.dream import NoOpExtractor
    svc.config.memory.cortex.auto_promote = False   # no store-path promotion either
    svc.store("the build timeout is 4500 seconds", source="notes")
    out = svc.dream_run(NoOpExtractor())
    assert out["pulled"] >= 1
    assert out["inserted"] == 0 and out["confirmed"] == 0
    assert out["cursor"] > 0                          # cursor still advances
    assert svc.cortex_lookup("build", "timeout") is None


def test_dream_empty_llm_claims_write_nothing(svc):
    # An LLM that emitted no parseable claims must NOT fall back to the regex floor.
    svc.config.memory.cortex.auto_promote = False
    svc.store("the relay port is 4001", source="notes")
    out = svc.dream_run(_StubExtractor([]))
    assert out["inserted"] == 0 and out["confirmed"] == 0
    assert svc.cortex_lookup("relay", "port") is None


class _FailingExtractor:
    """Simulates a transient extractor failure (timeout / network / malformed)."""
    def extract(self, texts, vocab):
        from pseudolife_memory.memory.dream import ExtractorError
        raise ExtractorError("boom")


def test_dream_run_does_not_advance_cursor_on_failure(svc):
    # Regression for the dream-timeout incident: a failed extraction must NOT
    # advance the cursor (else those memories are silently skipped forever).
    svc.config.memory.cortex.auto_promote = False
    svc.store("the relay port is 4001", source="notes")
    before = svc.dream_status()["dream_cursor"]
    out = svc.dream_run(_FailingExtractor())
    assert out.get("extractor_failed") is True and out["claims"] == 0
    assert svc.dream_status()["dream_cursor"] == before     # cursor held
    # The same memory is still pending and a later good run consolidates it.
    again = svc.dream_run(_StubExtractor([
        {"entity": "relay", "attribute": "port", "value": "4001"}]))
    assert again["pulled"] >= 1
    assert svc.cortex_lookup("relay", "port") is not None


class _BatchRecordingExtractor:
    """Records each extract() call's texts; returns fixed claims."""

    def __init__(self, claims):
        self._claims = claims
        self.calls: list[list[str]] = []

    def extract(self, texts, vocab):
        self.calls.append(list(texts))
        return [dict(c) for c in self._claims]


def test_dream_extracts_batch_in_one_call(svc):
    """Regression for the 2026-06-25 per-entry restructure: extraction must see
    the whole pulled batch in ONE call, so the model names a fact's initial and
    update turns consistently and supersession can fire (per-entry extraction
    fragmented updates onto sibling slots — stale_leak 0.0 -> 0.8 on the
    ladder). Per-claim attribution now travels via the claim's 'source' index."""
    svc.config.memory.cortex.auto_promote = False
    svc.store("alpha-svc listens on port 1111", source="notes")
    svc.store("beta-svc listens on port 2222", source="notes")
    svc.store("gamma-svc listens on port 3333", source="notes")
    ext = _BatchRecordingExtractor([])
    out = svc.dream_run(ext)
    assert out["pulled"] == 3
    assert len(ext.calls) == 1, "all pulled entries must go in one extract call"
    assert len(ext.calls[0]) == 3


def test_dream_attributes_claims_by_source(svc):
    """Claims carry a 0-based 'source' index into the batch; traces must link
    each fact to the entry it actually came from (the point of eec67b1)."""
    svc.config.memory.cortex.auto_promote = False
    svc.store("first: alpha-svc port fact", source="notes")
    svc.store("second: beta-svc host fact", source="notes")
    out = svc.dream_run(_StubExtractor([
        {"entity": "alpha-svc", "attribute": "port", "value": "1111",
         "confidence": 0.6, "origin": "agent", "source": 0},
        {"entity": "beta-svc", "attribute": "host", "value": "h-2",
         "confidence": 0.6, "origin": "agent", "source": 1},
    ]))
    assert out["traces"] == 2
    st = svc._storage  # noqa: SLF001
    rows = st.conn.execute(
        "SELECT id, text FROM entries ORDER BY id").fetchall()
    by_text = {text: eid for eid, text in rows}
    first_facts = st.facts_for_entry(by_text["first: alpha-svc port fact"])
    second_facts = st.facts_for_entry(by_text["second: beta-svc host fact"])
    assert any(f["entity"] == "alpha-svc" for f in first_facts)
    assert any(f["entity"] == "beta-svc" for f in second_facts)
    assert not any(f["entity"] == "beta-svc" for f in first_facts)


class _PoisonExtractor:
    """Fails deterministically when any entry contains 'poison' (a poison entry
    corrupts the whole batched response); extracts a canned relay/port claim
    otherwise. Per-entry isolation calls therefore fail only on the poison."""

    def extract(self, texts, vocab):
        from pseudolife_memory.memory.dream import ExtractorError
        if any("poison" in t for t in texts):
            raise ExtractorError("deterministic parse failure")
        return [{"entity": "relay", "attribute": "port", "value": "4001",
                 "confidence": 0.55, "origin": "agent"}]


def test_poison_entry_quarantined_after_repeated_failures(svc):
    """2026-07-02 review fix: an entry that fails extraction deterministically
    must not stall consolidation forever. After repeated failures it is
    quarantined (skipped) and the cursor advances past it."""
    svc.config.memory.cortex.auto_promote = False
    svc.store("good one relay speaks on some port", source="notes")
    svc.store("poison entry that always breaks extraction", source="notes")
    svc.store("good two also mentions the relay", source="notes")

    ext = _PoisonExtractor()
    r1 = svc.dream_run(ext)
    r2 = svc.dream_run(ext)
    assert r1.get("extractor_failed") is True     # transient-style holds...
    assert r2.get("extractor_failed") is True     # ...and holds again
    r3 = svc.dream_run(ext)                       # third strike: quarantine
    assert not r3.get("extractor_failed"), (
        "a deterministically-failing entry must be quarantined, not retried "
        "forever")
    assert svc.dream_status()["backlog"] == 0     # cursor moved past poison


def test_batch_retry_does_not_ratchet_confidence(svc):
    """A re-extraction of the SAME source entry (batch retry after a
    mid-batch failure, or a rewound cursor) must be a no-op on the slot,
    not a confirmation — the pre-fix behavior ratcheted agent guesses
    toward 1.0 on every 600s sweep while consolidation was stalled."""
    svc.config.memory.cortex.auto_promote = False
    svc.store("relay speaks on some port", source="notes")

    stub = _StubExtractor([{"entity": "relay", "attribute": "port",
                            "value": "4001", "confidence": 0.55,
                            "origin": "agent"}])
    svc.dream_run(stub)                     # writes relay.port@0.55 + trace
    first = svc.cortex_lookup("relay", "port")["confidence"]
    svc._cortex.dream_cursor = 0.0          # noqa: SLF001 — force a re-dream
    again = svc.dream_run(stub)             # re-extracts the same source entry
    assert again["pulled"] >= 1             # the re-dream really happened
    second = svc.cortex_lookup("relay", "port")["confidence"]
    assert second == pytest.approx(first), (
        "re-dreaming an already-traced (slot, source) pair must not "
        "reinforce confidence")


def test_dream_outage_holds_cursor_without_quarantine(svc):
    """When EVERY entry fails the per-entry isolation pass (endpoint outage,
    not a poison entry), nothing may be quarantined — the cursor holds and
    the whole batch stays pending for the next sweep."""
    svc.config.memory.cortex.auto_promote = False
    svc.store("outage one relay fact", source="notes")
    svc.store("outage two relay fact", source="notes")

    ext = _FailingExtractor()
    for _ in range(4):                      # past the batch-failure threshold
        out = svc.dream_run(ext)
        assert out.get("extractor_failed") is True
    assert svc.dream_status()["backlog"] == 2, (
        "an outage must not quarantine entries")


# ── GAM #2 graph-from-text: _dream_extract_relations (PG-backed) ─────────

class _RelStubExtractor:
    """Stub extractor exposing extract + extract_relations for dream tests."""
    def __init__(self, claims=None, relations=None, fail_relations=False):
        self._claims = claims or []
        self._relations = relations or []
        self._fail = fail_relations
    def extract(self, texts, vocab):
        return [dict(c) for c in self._claims]
    def extract_relations(self, texts, relations):
        if self._fail:
            from pseudolife_memory.memory.dream import ExtractorError
            raise ExtractorError("boom")
        return [dict(r) for r in self._relations]


def test_dream_extract_relations_populates_graph(svc):
    n = svc._dream_extract_relations(_RelStubExtractor(relations=[
        {"src": "checkout-service", "relation": "runs_on", "dst": "host-1"},
        {"src": "Acme", "relation": "no-such-rel", "dst": "Beta"},   # -> related-to
        {"src": "loop", "relation": "uses", "dst": "loop"},          # self-loop dropped
    ]), ["some text"], batch_sources={"relbatch-proj"})
    assert n == 1
    g = svc.graph_neighborhood("checkout-service", depth=1)
    edges = {(e["src"], e["relation"], e["dst"]) for e in g["edges"]}
    assert ("checkout-service", "runs-on", "host-1") in edges  # normalized relation
    # batch provenance travels through the plumbing to minted entities
    from pseudolife_memory.graph import norm_name
    cs_id = svc._storage.find_entity(norm_name("checkout-service"))["id"]
    assert "relbatch-proj" in {
        r["source"] for r in svc._storage.sources_for_entity(cs_id)}
    # the related-to fallback (conf 0.45) is quarantined to edge_proposals,
    # not written live (relation_quarantine_below, 2026-07-19)
    g2 = svc.graph_neighborhood("acme", depth=1)
    assert not any(e["relation"] == "related-to" for e in g2.get("edges", []))
    quarantined = svc._storage.conn.execute(
        "SELECT count(*) FROM edge_proposals "
        "WHERE source = 'dream-low-confidence'").fetchone()[0]
    assert quarantined == 1


def test_relations_prompt_discourages_untyped_fallback():
    # 2026-07-19: the old tail invited 'related-to' as a generic fallback —
    # the source of the ~19/day co-mention faucet the quarantine now diverts.
    # The prompt must prefer typed relations and skip co-occurrence pairs.
    from pseudolife_memory.memory.dream import _relations_prompt
    p = _relations_prompt([("runs-on", "service runs on host")])
    assert "merely appear together" in p
    assert "skip the pair" in p


def test_dream_extract_relations_failure_is_isolated(svc):
    # A relations failure must not raise — returns 0, leaves the dream intact.
    assert svc._dream_extract_relations(
        _RelStubExtractor(relations=[], fail_relations=True), ["x"]) == 0


def test_dream_extract_relations_disabled(svc):
    svc.config.memory.dream.extract_relations = False
    assert svc._dream_extract_relations(_RelStubExtractor(relations=[
        {"src": "a-svc", "relation": "uses", "dst": "b-svc"}]), ["x"]) == 0


# ── GAM #2 Task 3: dream_run wires _dream_extract_relations ──────────────

def test_dream_run_populates_relations_end_to_end(svc):
    svc.store("checkout-service runs on host-1 and uses redis", source="notes")
    out = svc.dream_run(_RelStubExtractor(
        claims=[{"entity": "checkout-service", "attribute": "role",
                 "value": "payments", "confidence": 0.6}],
        relations=[{"src": "checkout-service", "relation": "runs-on",
                    "dst": "host-1"},
                   {"src": "checkout-service", "relation": "uses",
                    "dst": "redis"}]))
    assert out["claims"] == 1
    assert out["relations"] == 2
    g = svc.graph_neighborhood("checkout-service", depth=1)
    edges = {(e["src"], e["relation"], e["dst"]) for e in g["edges"]}
    assert ("checkout-service", "runs-on", "host-1") in edges
    assert ("checkout-service", "uses", "redis") in edges


def test_dream_run_relations_failure_keeps_claims(svc):
    svc.store("the relay port is 4001", source="notes")
    out = svc.dream_run(_RelStubExtractor(
        claims=[{"entity": "relay", "attribute": "port", "value": "4001",
                 "confidence": 0.6}],
        fail_relations=True))
    assert out["claims"] == 1 and out["relations"] == 0     # claim kept
    assert svc.cortex_lookup("relay", "port") is not None


def test_dream_run_no_entries_returns_relations_key(svc):
    # Regression: the empty-entries early-return must include "relations": 0
    # so callers can rely on a uniform contract shape.
    out = svc.dream_run(_RelStubExtractor())   # no stored memories → pulled==0
    assert out["pulled"] == 0
    assert "relations" in out and out["relations"] == 0


# ── GAM #2 Task 4: multi-hop over text-populated graph (Tier-B capability) ──

def test_dream_relations_enable_multihop(svc):
    # depends-on is transitive: A->B->C should yield a DERIVED A->C edge,
    # i.e. multi-hop works on graph populated purely from ingested text.
    svc.store("mobile-app depends on graphql-gateway; graphql-gateway "
              "depends on user-service", source="notes")
    svc.dream_run(_RelStubExtractor(relations=[
        {"src": "mobile-app", "relation": "depends-on", "dst": "graphql-gateway"},
        {"src": "graphql-gateway", "relation": "depends-on", "dst": "user-service"}]))
    g = svc.graph_neighborhood("mobile-app", depth=3)
    derived = {(e["src"], e["dst"]) for e in g["edges"] if e["derived"]}
    assert ("mobile-app", "user-service") in derived  # transitive multi-hop


def test_dream_relations_reject_lesson_only_predicates(svc):
    # prefers/avoids are lesson-only; graph-from-text must not write them even
    # if the model emits one — it falls back to related-to, which (as an
    # untyped 0.45 edge) is then quarantined to edge_proposals, never live.
    n = svc._dream_extract_relations(_RelStubExtractor(relations=[
        {"src": "deploy-task", "relation": "prefers", "dst": "rsync"}]), ["text"])
    assert n == 0
    g = svc.graph_neighborhood("deploy-task", depth=1)
    assert "prefers" not in {e["relation"] for e in g.get("edges", [])}
    row = svc._storage.conn.execute(
        "SELECT relation FROM edge_proposals "
        "WHERE source = 'dream-low-confidence'").fetchone()
    assert row is not None and row[0] == "related-to"


def test_traces_config_default():
    from pseudolife_memory.utils.config import TracesConfig, MemoryConfig
    assert TracesConfig().enabled is True
    assert MemoryConfig().traces.enabled is True


# ── known-facts window prompt block (spec 2026-07-10) ────────────────────

def test_facts_hint_formats_block_and_empty_is_empty():
    from pseudolife_memory.memory.dream import _facts_hint

    assert _facts_hint(None) == ""
    assert _facts_hint([]) == ""
    block = _facts_hint([("svc", "port", "8080"), ("db", "host", "h1")])
    assert "Current known facts" in block
    assert "never emit a claim the notes do not state" in block
    assert "- svc — port: 8080" in block
    assert "- db — host: h1" in block


def _capture_extract_body(known_facts):
    """Run one extract() against a capturing stub server; return the request
    body the extractor sent (messages etc.)."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    seen_bodies = []

    class _CapturingHandler(_StubHandler):
        @staticmethod
        def responder():
            return (200, _chat_payload([]))

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            seen_bodies.append(json.loads(self.rfile.read(length).decode()))
            status, body = self.responder()
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        ext = OpenAICompatExtractor(base_url, "m")
        if known_facts is None:
            ext.extract(["a note"], vocab=["svc.port"])
        else:
            ext.extract(["a note"], vocab=["svc.port"], known_facts=known_facts)
    finally:
        srv.shutdown()
    return seen_bodies[0]


def test_openai_extractor_renders_known_facts_block():
    body = _capture_extract_body([("svc", "port", "8080")])
    system = body["messages"][0]["content"]
    assert "Current known facts" in system
    assert "- svc — port: 8080" in system


def test_openai_extractor_omits_block_without_known_facts():
    system = _capture_extract_body(None)["messages"][0]["content"]
    assert "Current known facts" not in system


# ── service wiring: _dream_hints + dream_run known-facts window ──────────

class _RecordingExtractor:
    """Records what dream_run passes; returns one fixed claim per call."""

    def __init__(self):
        self.calls = []

    def extract(self, texts, vocab, known_facts=None):
        self.calls.append({"texts": list(texts), "vocab": list(vocab),
                           "known_facts": known_facts})
        return [{"entity": "gadget", "attribute": "version", "value": "3.3",
                 "confidence": 0.8, "origin": "agent"}]


def test_dream_run_window_off_by_default_passes_no_known_facts(svc):
    svc.store("the widget port is 9090", source="notes")
    ext = _RecordingExtractor()
    svc.dream_run(ext)
    assert ext.calls and ext.calls[0]["known_facts"] is None


def test_dream_run_passes_known_facts_window_when_enabled(svc):
    svc.config.memory.dream.known_facts_window = 20
    # Seed a current fact through the normal dream path (no LLM needed).
    svc.store("gadget version is 3.2", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "gadget", "attribute": "version", "value": "3.2",
        "confidence": 0.8, "origin": "agent"}]))
    # Second cycle: the extractor must now SEE the seeded fact's value.
    svc.store("the gadget version is now 3.3", source="notes")
    ext = _RecordingExtractor()
    out = svc.dream_run(ext)
    kf = ext.calls[0]["known_facts"]
    assert kf, "window enabled + non-empty cortex must pass known_facts"
    assert ("gadget", "version", "3.2") in kf
    # And the claim written under the same slot supersedes as usual.
    assert out["superseded"] >= 1
    fact = svc.cortex_lookup("gadget", "version")
    assert fact is not None and "3.3" in fact["value"]


def test_dream_run_window_on_empty_cortex_omits_kwarg(svc):
    # First-ever dream on an empty bank: facts_ranked returns [] and the
    # kwarg must NOT be passed (extractors without it must keep working).
    svc.config.memory.dream.known_facts_window = 20
    svc.store("brand new note about a fresh topic", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "fresh", "attribute": "topic", "value": "noted",
        "confidence": 0.8, "origin": "agent"}]))     # has no known_facts param
    assert out["inserted"] + out["confirmed"] >= 1   # did not blow up
