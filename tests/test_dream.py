"""Dream pass — pluggable extractor, driver, status, MCP wiring.

Three tiers:
* pure config + RegexExtractor logic (no embedder, no PG — fast);
* PG-backed driver/status tests with the real embedder (skip cleanly
  without a test server).
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from tests.dream_helpers import (StubExtractor as _StubExtractor,
                                 StubHandler as _StubHandler,
                                 chat_payload as _chat_payload,
                                 chat_relations_payload as _chat_relations_payload,
                                 stub_server as _stub_server)
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


def test_extractor_extra_body_merges_into_every_chat_request():
    """``extra_body`` rides into the request body verbatim; absent, the body
    is unchanged. Exists for the bench's ``cache_prompt: false`` pin — the
    llama-server default prompt cache changes output on identical
    temperature-0 input once the server is warm (measured 2026-08-09:
    19 claims/0.1 stale fresh vs 26/0.2 warm, restored exactly by the pin;
    ``evals/results/warm-cache-probe-0809.json``)."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    captured: list[dict] = []

    class _Capture(_StubHandler):
        # Full override — the parent do_POST reads the body itself, so a
        # capture-then-super() would block on a second read.
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length", 0))
            captured.append(json.loads(self.rfile.read(n)))
            status, body = type(self).responder()
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    payload = _chat_payload([])
    handler = type("H", (_Capture,), {"responder": staticmethod(
        lambda: (200, payload))})
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        OpenAICompatExtractor(base, "m",
                              extra_body={"cache_prompt": False}).extract(
            ["note"], [], [])
        OpenAICompatExtractor(base, "m").extract(["note"], [], [])
    finally:
        srv.shutdown()
    assert captured[0]["cache_prompt"] is False
    assert "cache_prompt" not in captured[1]
    # extra_body must not clobber the real fields.
    assert captured[0]["messages"] and captured[0]["temperature"] == 0


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
    assert c.exclude_sources == ["consolidation", "reflection", "status",
                                 "log", "digest"]
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

    # The stub sleeps 6x the client's timeout: enough that the client always
    # gives up first, short enough that _stub_server's shutdown — which blocks
    # on the in-flight handler — does not hold the suite for the full sleep.
    # Was sleep(1.0)/timeout 0.2, which cost ~0.8s of pure teardown wait.
    def slow():
        time.sleep(0.3)
        return (200, _chat_payload([]))

    with _stub_server(slow) as base_url:
        ext = OpenAICompatExtractor(base_url, "m", timeout_seconds=0.05)
        with pytest.raises(ExtractorError, match="timed out"):
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

    def prune_dream_runs(self):
        # Mirrors MemoryService.prune_dream_runs (v27 journal retention,
        # also every sweep tick).
        self.pruned = getattr(self, "pruned", 0) + 1
        return 2

    def prune_retrieval_log(self):
        # Mirrors MemoryService.prune_retrieval_log (v31 retention). Real
        # name, not a stand-in — getattr-guarded in run_sweep_once, so a
        # rename that silently dropped the guard's match would otherwise
        # never go red.
        self.retrieval_pruned = getattr(self, "retrieval_pruned", 0) + 1
        return 3

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


def test_run_sweep_once_disabled_still_prunes_retrieval_log():
    """Issue #178: memory.dream.enabled only gates the automatic
    backlog-triggered dream trigger, not the write paths that feed
    compaction/dream-run-journal/retrieval-log. A manual `memory_dream`
    run, an end-of-session dream (`_fire_and_forget_dream` never checks
    dream.enabled), and memory_fact_set/memory_search all stay live on a
    dream-disabled bank — so a dream-disabled sweep tick must still run
    all three reapers, not just skip out early."""
    from pseudolife_memory.memory.dream import run_sweep_once

    svc = _FakeService(enabled=False)
    out = run_sweep_once(svc)
    assert out["fired"] is False and out["reason"] == "disabled" and not svc.ran
    assert svc.pruned == 1, "prune_dream_runs must fire even when dream disabled"
    assert svc.retrieval_pruned == 1, (
        "prune_retrieval_log must fire even when dream disabled — the "
        "retrieval log has no other reaper"
    )
    assert out["retrieval_pruned"] == 3, (
        "the sweep result must surface that retention ran, not just the "
        "fake's internal call count"
    )


def test_run_sweep_once_prunes_retrieval_log():
    """v31 retrieval-event retention rides every sweep tick beside
    compaction/dream-run pruning — including ticks where no dream fires
    (the log accrues on every search, independent of dream activity). The
    sweep result surfaces the prune count under "retrieval_pruned" in
    every branch (disabled/below_threshold/fired)."""
    from pseudolife_memory.memory.dream import run_sweep_once

    quiet = _FakeService(would_fire=False)
    out = run_sweep_once(quiet)
    assert quiet.retrieval_pruned == 1 and out["retrieval_pruned"] == 3

    firing = _FakeService(would_fire=True)
    out = run_sweep_once(firing)
    assert firing.retrieval_pruned == 1 and out["retrieval_pruned"] == 3


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


def test_run_sweep_once_prunes_dream_runs():
    """v27 journal retention rides every sweep tick beside compaction —
    including ticks where no dream fires (a quiet bank must still prune)."""
    from pseudolife_memory.memory.dream import run_sweep_once

    quiet = _FakeService(would_fire=False)
    out = run_sweep_once(quiet)
    assert out["runs_pruned"] == 2 and quiet.pruned == 1

    firing = _FakeService(would_fire=True)
    out = run_sweep_once(firing)
    assert out["runs_pruned"] == 2 and firing.pruned == 1


def test_run_sweep_once_reports_phase_timings(caplog):
    """Every sweep tick reports per-phase durations and logs one ledger
    line (2026-09-01). The 2026-08-31 hook-timeout forensics misattributed
    a stall to the judging tick precisely because only phase COMPLETIONS
    are logged — with no tick start/duration in the ledger, a completion
    timestamp invites reading the whole preceding window as that phase.
    Timings ride every branch (disabled/below-threshold/fired) so the
    ledger has no silent tick shapes."""
    import logging

    from pseudolife_memory.memory.dream import run_sweep_once

    for svc in (_FakeService(enabled=False),
                _FakeService(would_fire=False),
                _FakeService(would_fire=True)):
        with caplog.at_level(logging.INFO):
            out = run_sweep_once(svc)
        t = out["timings"]
        assert set(t) >= {"compact", "prune_runs", "prune_retrieval",
                          "total"}, f"missing phases: {t}"
        assert all(isinstance(v, float) and v >= 0.0 for v in t.values())
        assert t["total"] >= max(v for k, v in t.items() if k != "total")
        assert any("sweep tick" in r.message for r in caplog.records), (
            "each tick must leave one ledger line")
        caplog.clear()
    # The fired branch additionally times the dream itself.
    assert "dream" in out["timings"]


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


# ── claim-level op (Task 7): set membership ──────────────────────────────

def test_dream_run_op_add_lands_member(svc):
    svc.store("added Rosa's Diner to the restaurants tried list", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "user", "attribute": "restaurants tried", "value": "Rosa's Diner",
        "confidence": 0.8, "origin": "agent", "op": "add"}]))
    assert out["member_added"] == 1
    got = svc.cortex_lookup("user", "restaurants tried")
    assert got is not None and got["kind"] == "set"
    assert [m["value"] for m in got["members"]] == ["Rosa's Diner"]


def test_dream_run_op_remove_removes_member(svc):
    svc.set_add("user", "restaurants tried", "Rosa's Diner")
    svc.store("no longer counting Rosa's Diner as tried", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "user", "attribute": "restaurants tried", "value": "Rosa's Diner",
        "confidence": 0.8, "origin": "agent", "op": "remove"}]))
    assert out["member_removed"] == 1
    got = svc.cortex_lookup("user", "restaurants tried")
    assert got is not None
    assert got["members"] == []
    assert [m["value"] for m in got["removed"]] == ["Rosa's Diner"]


def test_dream_run_scalar_claim_dropped_on_set_slot(svc, caplog):
    """Spec rule 2: an extractor scalar claim (no op) targeting a slot that
    holds current members must be dropped, not crash the dream and not
    silently convert/overwrite the set."""
    svc.set_add("user", "restaurants tried", "Rosa's Diner")
    before = sorted(m["value"] for m in
                    svc.cortex_lookup("user", "restaurants tried")["members"])
    svc.store("the restaurant tried is Rosa's Diner v2", source="notes")
    with caplog.at_level("INFO", logger="pseudolife_memory.service_dream"):
        out = svc.dream_run(_StubExtractor([{
            "entity": "user", "attribute": "restaurants tried",
            "value": "Rosa's Diner v2", "confidence": 0.8, "origin": "agent"}]))
    after = sorted(m["value"] for m in
                   svc.cortex_lookup("user", "restaurants tried")["members"])
    assert after == before                        # store unchanged
    assert out.get("inserted", 0) == 0 and out.get("confirmed", 0) == 0
    assert out["dropped_set_slot"] == 1
    assert any(
        "dropped scalar claim for set slot" in rec.message
        and "user.restaurants tried" in rec.message for rec in caplog.records)


def test_dream_run_malformed_op_falls_back_to_scalar_with_warning(svc, caplog):
    """A malformed op value must not crash the dream — it degrades to the
    scalar path (bit-identical to no op) with a warning naming the value."""
    svc.store("the team mascot is a fox", source="notes")
    with caplog.at_level("WARNING", logger="pseudolife_memory.service_dream"):
        out = svc.dream_run(_StubExtractor([{
            "entity": "team", "attribute": "mascot", "value": "fox",
            "confidence": 0.8, "origin": "agent", "op": "update"}]))
    assert out["inserted"] == 1
    got = svc.cortex_lookup("team", "mascot")
    assert got is not None and got["value"] == "fox"
    assert any("malformed op" in rec.message and "update" in rec.message
               for rec in caplog.records)


def test_dream_run_multi_member_add_from_one_entry_lands_both(svc):
    """F1 regression: the has_trace guard is keyed by (slot, source entry)
    with no member value, so it must not fire for member ops — else a
    SECOND op:"add" for the same slot from the same source entry reads as
    "already formed this slot" and is silently dropped (member ops are
    idempotent on retry themselves: re-add -> member_confirmed, re-remove ->
    member_not_found, which is the property the guard exists to protect for
    scalars)."""
    svc.store("tried Rosa's Diner and also Luigi's this week", source="notes")
    out = svc.dream_run(_StubExtractor([
        {"entity": "user", "attribute": "restaurants tried", "value": "Rosa's Diner",
         "confidence": 0.8, "origin": "agent", "op": "add"},
        {"entity": "user", "attribute": "restaurants tried", "value": "Luigi's",
         "confidence": 0.8, "origin": "agent", "op": "add"},
    ]))
    assert out["member_added"] == 2
    got = svc.cortex_lookup("user", "restaurants tried")
    assert sorted(m["value"] for m in got["members"]) == ["Luigi's", "Rosa's Diner"]


def test_dream_run_op_add_threads_confidence(svc):
    """F2: an op:"add" claim's confidence must reach the stored member, same
    as the scalar sibling path (confidence=float(c.get("confidence", 0.55)))
    — the extraction prompt solicits confidence on op claims, so the apply
    path must not silently discard it."""
    svc.store("tried Rosa's Diner, very sure about this one", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "user", "attribute": "restaurants tried", "value": "Rosa's Diner",
        "confidence": 0.95, "origin": "agent", "op": "add"}]))
    got = svc.cortex_lookup("user", "restaurants tried")
    member = next(m for m in got["members"] if m["value"] == "Rosa's Diner")
    assert member["confidence"] == 0.95


def test_dream_run_member_invalid_skips_trace_and_reinforcement(svc):
    """F3: a member_invalid result must never reach the trace-write +
    reinforcement-bump block — it would trace a member that was never
    stored, and (combined with F1's bug) mask the slot from a later,
    legitimate add of the same source entry."""
    svc.store("tried to log an empty item in bikes owned", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "user", "attribute": "bikes owned", "value": "   ",
        "confidence": 0.8, "origin": "agent", "op": "add"}]))
    assert out["member_invalid"] == 1
    assert out["traces"] == 0


def test_dream_run_scalar_conflict_skips_trace_and_reinforcement(svc):
    """Regression pin (review minor 1): the trace-skip tuple in
    dream_run's claim-apply loop (`pseudolife_memory/service_dream.py`,
    `_dream_run_locked`) was deliberately broadened to cover EVERY `action="contested"`
    result, not just the aggregate-conversion guard's blocked add — a
    plain weaker-tier scalar conflict from write_fact's own tier guard
    (`CortexStore.write_fact`, `tier_ok = ... _rank(sup) >= _rank(cur.origin)`)
    is "contested" too and never populated the slot, so it must not reach
    the trace-write + reinforcement-bump block either. True RED is not
    obtainable here — this behavior already ships (the "contested" branch
    predates this feature); this is a regression pin for the broadened
    skip, not a TDD RED for new behavior.

    Seed the slot at user tier (rank 3), then have dream_run apply a
    lower-tier ("agent", rank 1) scalar claim with a DIFFERENT value on
    the same slot: tier_ok is False, so write_fact parks it as a
    contender (action="contested") and the current user-tier value is
    untouched."""
    svc.cortex_write("project", "language", "go", support="user")
    svc.store("someone floated that the language might be rust", source="notes")
    db_id = svc.dream_pull()["entries"][-1]["db_id"]
    before = svc._storage.get_entry(db_id)["reinforcements"]
    out = svc.dream_run(_StubExtractor([{
        "entity": "project", "attribute": "language", "value": "rust",
        "confidence": 0.8, "origin": "agent"}]))
    assert out["contested"] == 1
    assert out["traces"] == 0
    after = svc._storage.get_entry(db_id)["reinforcements"]
    assert after == before                    # no reinforcement bump either
    assert svc.cortex_lookup("project", "language")["value"] == "go"


def test_dream_run_higher_tier_claim_supersedes_the_agent_value(svc):
    """The mirror of the case above: escalating tier, not weakening it. An
    agent claim inserts into the empty slot, and a later user-origin claim
    for the same slot clears write_fact's tier guard and supersedes it —
    the pull -> extract -> cortex_write path end to end, no live model."""
    # The stub's values are absent from the stub notes; pin the literal
    # gate to observe-only so this test keeps exercising its own concern
    # under the "enforce" default (2026-08-02).
    svc.config.memory.dream.literal_gate = "log"

    def _claim(value, *, origin):
        return {"entity": "checkout-service", "attribute": "default port",
                "value": value, "confidence": 0.8, "origin": origin}

    svc.store("checkout-service default port note", source="notes")
    svc.dream_run(_StubExtractor([_claim("9090", origin="agent")]))
    assert svc.cortex_lookup(
        "checkout-service", "default port")["value"] == "9090"

    svc.store("checkout-service default port revised", source="notes")
    out = svc.dream_run(_StubExtractor([_claim("9595", origin="user")]))
    assert out["superseded"] == 1
    assert svc.cortex_lookup(
        "checkout-service", "default port")["value"] == "9595"


def test_dream_run_blocked_aggregate_add_skips_trace_scalar_claim_not_suppressed(svc):
    """Review finding (FIX 2): an op:"add" claim that the aggregate-conversion
    guard parks as a contender (action "contested") did not populate the
    slot — like member_invalid/member_capped, it must not reach the
    trace-write + reinforcement-bump block. Before the fix, the erroneous
    trace it left behind would then silently suppress a SECOND, same-entry
    scalar claim for the same slot via the has_trace guard (which only
    guards op=None claims): both claims share one source entry here so the
    ordering bug is directly observable in a single dream_run call."""
    svc.store("count is 27 birds total", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "user", "attribute": "birds", "value": "27",
        "confidence": 0.8, "origin": "agent"}]))
    assert svc.cortex_lookup("user", "birds")["value"] == "27"

    svc.store("saw a new bird and the total stayed 27", source="notes")
    out = svc.dream_run(_StubExtractor([
        {"entity": "user", "attribute": "birds", "value": "Northern Flicker",
         "confidence": 0.8, "origin": "agent", "op": "add"},
        {"entity": "user", "attribute": "birds", "value": "27",
         "confidence": 0.8, "origin": "agent"},
    ]))
    # Under the bug: claim 1 (contested) wrongly writes a trace, so claim 2
    # (op=None, same slot+entry) trips the has_trace guard and is silently
    # dropped before ever reaching cortex_write — claims=1, confirmed=0.
    # Fixed: claim 1 writes no trace, claim 2 is processed and confirms.
    assert out["claims"] == 2
    assert out["contested"] == 1
    assert out["confirmed"] == 1
    assert out["traces"] == 1          # exactly one trace — for the confirm, not the block
    assert svc.cortex_lookup("user", "birds")["value"] == "27"


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


def test_dream_run_second_concurrent_caller_skips(svc):
    # Regression (2026-08-10, live runs 68/69): dream_run had no concurrency
    # guard, so two triggers (sweep + session-end/MCP) racing into the same
    # cursor window both pulled and extracted it. The second concurrent
    # caller must skip without touching the cursor or the extractor.
    svc.config.memory.cortex.auto_promote = False
    started = threading.Event()
    release = threading.Event()

    class _BlockingExtractor:
        def extract(self, texts, vocab, known_facts=None):
            started.set()
            release.wait(timeout=30)
            return [{"entity": "gizmo", "attribute": "port", "value": "7001",
                     "source": 0}]

    svc.store("the gizmo port is 7001", source="notes")
    first: dict = {}
    t = threading.Thread(
        target=lambda: first.update(svc.dream_run(_BlockingExtractor())))
    t.start()
    try:
        assert started.wait(timeout=30)     # first run is inside extract()
        second = svc.dream_run(_StubExtractor([]))
        assert second.get("skipped") == "dream_in_progress"
        assert second["pulled"] == 0 and second["claims"] == 0
    finally:
        release.set()
        t.join(timeout=60)
    assert not t.is_alive()
    assert first["pulled"] >= 1             # in-flight run was unaffected
    assert svc.cortex_lookup("gizmo", "port") is not None
    # Guard released: the next call proceeds normally (and sees no backlog).
    after = svc.dream_run(_StubExtractor([]))
    assert "skipped" not in after


class _BatchRecordingExtractor(_StubExtractor):
    """Records each extract() call's texts; returns fixed claims."""

    def __init__(self, claims):
        super().__init__(claims)
        self.calls: list[list[str]] = []

    def extract(self, texts, vocab, known_facts=None):
        self.calls.append(list(texts))
        return super().extract(texts, vocab, known_facts)


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
    # Stub values are not present in the stub notes; pin the literal
    # gate to observe-only so this test keeps exercising its own
    # concern under the "enforce" default (2026-08-02).
    svc.config.memory.dream.literal_gate = "log"
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
    # Stub values are not present in the stub notes; pin the literal
    # gate to observe-only so this test keeps exercising its own
    # concern under the "enforce" default (2026-08-02).
    svc.config.memory.dream.literal_gate = "log"
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

class _RelStubExtractor(_StubExtractor):
    """Stub extractor exposing extract + extract_relations for dream tests."""
    def __init__(self, claims=None, relations=None, fail_relations=False):
        super().__init__(claims or [])
        self._relations = relations or []
        self._fail = fail_relations
    def extract_relations(self, texts, relations):
        if self._fail:
            from pseudolife_memory.memory.dream import ExtractorError
            raise ExtractorError("boom")
        return [dict(r) for r in self._relations]


def test_unflatten_slot_key_claims_splits_flattened_keys():
    # 2026-07-26: the vocab hint lists slot keys as `entity.attribute`
    # (cortex._norm_key collapses every separator to '-', so exactly ONE dot
    # is the join). Extractors periodically "reuse" a key by copying the whole
    # string into `entity` and writing the literal "value" as the attribute —
    # minting `0-9-0-release.deployment-status` as an entity that duplicates a
    # correctly-shaped fact.
    from pseudolife_memory.memory.dream import unflatten_slot_key_claims
    vocab = ["0-9-0-release.commit", "0-9-0-release.deployment-status"]

    out = unflatten_slot_key_claims(
        [{"entity": "0-9-0-release.deployment-status", "attribute": "value",
          "value": "live", "confidence": 0.9, "origin": "agent"}], vocab)
    assert out[0]["entity"] == "0-9-0-release"
    assert out[0]["attribute"] == "deployment-status"
    assert out[0]["value"] == "live" and out[0]["confidence"] == 0.9

    # a NEW attribute on a KNOWN entity still splits
    out = unflatten_slot_key_claims(
        [{"entity": "0-9-0-release.rollback-caveat", "attribute": "value",
          "value": "x", "confidence": 0.8, "origin": "agent"}], vocab)
    assert (out[0]["entity"], out[0]["attribute"]) == (
        "0-9-0-release", "rollback-caveat")


def test_unflatten_slot_key_claims_leaves_real_dotted_entities_alone():
    # All guards must hold: split only when the attribute is literally "value"
    # AND the prefix is a known entity. `llama.cpp` / `host.docker.internal`
    # are real entities and must survive untouched.
    from pseudolife_memory.memory.dream import unflatten_slot_key_claims
    vocab = ["llama-cpp.build", "0-9-0-release.commit"]
    keep = [
        {"entity": "llama.cpp", "attribute": "build", "value": "b9371",
         "confidence": 0.9, "origin": "agent"},          # attribute != "value"
        {"entity": "host.docker.internal", "attribute": "value", "value": "ok",
         "confidence": 0.9, "origin": "agent"},          # prefix not an entity
        {"entity": "plain-entity", "attribute": "value", "value": "v",
         "confidence": 0.9, "origin": "agent"},          # no dot at all
    ]
    assert unflatten_slot_key_claims([dict(c) for c in keep], vocab) == keep


def test_dream_run_unflattens_slot_key_claims(svc):
    # End-to-end: a flattened claim lands on the real slot and mints no dotted
    # entity.
    class _FlatStub:
        def extract(self, texts, vocab, known_facts=None):
            return [{"entity": "sk-probe.deploy-status", "attribute": "value",
                     "value": "shipped", "confidence": 0.9, "origin": "agent"}]

    svc.cortex_write("sk-probe", "commit", "abc123", support="user")
    svc.store("sk-probe note", source="pseudolife-mcp")
    svc.dream_run(_FlatStub())

    got = svc.cortex_lookup("sk-probe", "deploy-status")
    assert got is not None and got["value"] == "shipped"
    from pseudolife_memory.graph import norm_name
    assert svc._storage.find_entity(norm_name("sk-probe.deploy-status")) is None


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
    from pseudolife_memory.utils.config import MemoryConfig
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


def test_openai_extractor_carries_op_through_parse():
    """The C2 e2e gate 'failed' because extract() rebuilt claims with a
    fixed field whitelist and silently STRIPPED the model's op field — the
    op-aware apply loop downstream never saw it, remove-intents landed as
    positive scalar facts, and the miss was attributed to the model
    (corrected 2026-07-31; raw probes show clean adoption). This pins the
    parse layer: op survives when valid, anything else is absent."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([
        {"entity": "user", "attribute": "restaurants tried",
         "value": "Seoul Garden", "op": "add", "confidence": 0.9, "source": 1},
        {"entity": "user", "attribute": "bikes owned",
         "value": "road bike", "op": "remove", "confidence": 0.9, "source": 1},
        {"entity": "user", "attribute": "location",
         "value": "Portland", "op": "set", "confidence": 0.9, "source": 1},
        {"entity": "user", "attribute": "job",
         "value": "Meridian", "op": "banana", "confidence": 0.9, "source": 1},
        {"entity": "user", "attribute": "city",
         "value": "Austin", "confidence": 0.9, "source": 1},
    ])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(["note"], [])
    ops = [c.get("op") for c in claims]
    # add/remove survive; "set", junk, and absent all normalise to absent.
    assert ops == ["add", "remove", None, None, None]


# ── literal-faithfulness gate (2026-08-02 design) ────────────────────────

class _CitingStub:
    """Fixed claims with explicit source indices (drives the gate path)."""
    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab, known_facts=None):
        return [dict(c) for c in self._claims]


def test_literal_gate_enforce_drops_fabricated_number(svc):
    svc.config.memory.dream.literal_gate = "enforce"
    svc.store("saw a flicker at the park, that makes 32 species now",
              source="notes")
    out = svc.dream_run(_CitingStub([{
        "entity": "user", "attribute": "park species count",
        "value": "41", "confidence": 0.9, "origin": "agent", "source": 0}]))
    assert out["literal_flagged"] == 1 and out["literal_dropped"] == 1
    assert svc.cortex_lookup("user", "park species count") is None


def test_literal_gate_log_mode_writes_but_counts(svc):
    svc.config.memory.dream.literal_gate = "log"   # shipped default, explicit
    svc.store("saw a flicker at the park, that makes 32 species now",
              source="notes")
    out = svc.dream_run(_CitingStub([{
        "entity": "user", "attribute": "park species count",
        "value": "41", "confidence": 0.9, "origin": "agent", "source": 0}]))
    assert out["literal_flagged"] == 1 and out["literal_dropped"] == 0
    got = svc.cortex_lookup("user", "park species count")
    assert got is not None and got["value"] == "41"


def test_literal_gate_off_writes_and_counts_nothing(svc):
    svc.config.memory.dream.literal_gate = "off"
    svc.store("saw a flicker at the park, that makes 32 species now",
              source="notes")
    out = svc.dream_run(_CitingStub([{
        "entity": "user", "attribute": "park species count",
        "value": "41", "confidence": 0.9, "origin": "agent", "source": 0}]))
    assert out["literal_flagged"] == 0 and out["literal_dropped"] == 0
    assert svc.cortex_lookup("user", "park species count") is not None


def test_literal_gate_supported_literal_passes(svc):
    svc.config.memory.dream.literal_gate = "enforce"
    svc.store("saw a flicker at the park, that makes 32 species now",
              source="notes")
    out = svc.dream_run(_CitingStub([{
        "entity": "user", "attribute": "park species count",
        "value": "32", "confidence": 0.9, "origin": "agent", "source": 0}]))
    assert out["literal_flagged"] == 0 and out["literal_dropped"] == 0
    got = svc.cortex_lookup("user", "park species count")
    assert got is not None and got["value"] == "32"


def test_literal_gate_skips_claim_without_src_id(svc):
    # A multi-entry batch with an uncited claim resolves no src_id; the gate
    # abstains rather than guessing which note to check against.
    svc.config.memory.dream.literal_gate = "enforce"
    svc.store("first unrelated observation about the garden", source="notes")
    svc.store("second unrelated observation about the weather", source="notes")
    out = svc.dream_run(_CitingStub([{
        "entity": "user", "attribute": "unverifiable count",
        "value": "41", "confidence": 0.9, "origin": "agent"}]))
    assert out["literal_dropped"] == 0
    assert svc.cortex_lookup("user", "unverifiable count") is not None


def test_literal_gate_batch_scope_accepts_cross_note_literal(svc):
    # The claim's correct value comes from note A while source cites note B —
    # the batched call exists precisely so both are seen together
    # (regression pin for the finding-#5 false-drop class).
    svc.config.memory.dream.literal_gate = "enforce"
    assert svc.config.memory.dream.literal_gate_scope == "batch"  # default
    svc.store("the port count rose to 27 this week", source="notes")
    svc.store("the port audit wrapped up today", source="notes")
    pulled = svc.dream_pull(limit=10)["entries"]
    cited = next(i for i, e in enumerate(pulled) if "audit" in e["text"])
    out = svc.dream_run(_CitingStub([{
        "entity": "ports", "attribute": "count",
        "value": "27", "confidence": 0.9, "origin": "agent",
        "source": cited}]))
    assert out["literal_flagged"] == 0 and out["literal_dropped"] == 0
    got = svc.cortex_lookup("ports", "count")
    assert got is not None and got["value"] == "27"


def test_literal_gate_source_scope_drops_cross_note_literal(svc):
    svc.config.memory.dream.literal_gate = "enforce"
    svc.config.memory.dream.literal_gate_scope = "source"
    svc.store("the port count rose to 27 this week", source="notes")
    svc.store("the port audit wrapped up today", source="notes")
    pulled = svc.dream_pull(limit=10)["entries"]
    cited = next(i for i, e in enumerate(pulled) if "audit" in e["text"])
    out = svc.dream_run(_CitingStub([{
        "entity": "ports", "attribute": "count",
        "value": "27", "confidence": 0.9, "origin": "agent",
        "source": cited}]))
    assert out["literal_flagged"] == 1 and out["literal_dropped"] == 1
    assert svc.cortex_lookup("ports", "count") is None


def test_literal_gate_gates_member_ops(svc):
    svc.config.memory.dream.literal_gate = "enforce"
    svc.store("tried the new diner downtown tonight", source="notes")
    out = svc.dream_run(_CitingStub([{
        "entity": "user", "attribute": "restaurants tried",
        "value": "Diner 419", "confidence": 0.8, "origin": "agent",
        "op": "add", "source": 0}]))
    assert out["literal_flagged"] == 1 and out["literal_dropped"] == 1
    assert svc.cortex_lookup("user", "restaurants tried") is None


def test_literal_gate_applies_on_isolation_path(svc):
    # A poison batch degrades to per-entry isolated extraction after the
    # retry budget; claims produced there must be gated identically.
    svc.config.memory.dream.literal_gate = "enforce"

    class _PoisonBatch:
        def extract(self, texts, vocab, known_facts=None):
            if len(texts) > 1:
                raise RuntimeError("poison batch")
            return [{"entity": "user", "attribute": "isolated count",
                     "value": "99", "confidence": 0.9, "origin": "agent",
                     "source": 0}]

    svc.store("counted 12 boxes in the garage", source="notes")
    svc.store("moved the boxes to the attic", source="notes")
    ex = _PoisonBatch()
    for _ in range(2):                       # burn the batch retry budget
        held = svc.dream_run(ex)
        assert held.get("extractor_failed") is True
    out = svc.dream_run(ex)                  # isolation pass
    assert out["literal_dropped"] >= 1
    assert svc.cortex_lookup("user", "isolated count") is None
