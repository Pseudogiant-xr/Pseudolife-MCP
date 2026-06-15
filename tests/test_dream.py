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
