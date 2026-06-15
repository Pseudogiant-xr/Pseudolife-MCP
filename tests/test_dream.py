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
