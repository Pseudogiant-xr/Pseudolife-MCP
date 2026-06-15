"""Dream pass — pluggable extractor, driver, status, MCP wiring.

Three tiers:
* pure config + RegexExtractor logic (no embedder, no PG — fast);
* PG-backed driver/status tests with the real embedder (skip cleanly
  without a test server).
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


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
