"""Tier logic unit tests — pure module, no MCP/embedder."""
from __future__ import annotations

import pytest

from pseudolife_memory.toolset_tiers import (
    TIERS, SessionTierState, normalize_tier, parse_tier_map, rank,
    resolve_tier, step,
)


def test_tier_order_and_rank():
    assert TIERS == ("minimal", "core", "full")
    assert rank("minimal") < rank("core") < rank("full")


def test_normalize_tier_lenient():
    assert normalize_tier("core") == "core"
    assert normalize_tier(" FULL ") == "full"
    assert normalize_tier(None) == "full"          # unset -> full (today's default)
    assert normalize_tier("") == "full"
    assert normalize_tier("bogus") == "full"       # unknown warns -> full


def test_parse_tier_map_happy_and_malformed():
    m = parse_tier_map("claude-desktop:minimal, Claude-Code:CORE")
    assert m == {"claude-desktop": "minimal", "claude-code": "core"}
    # malformed entries skipped, never fatal
    assert parse_tier_map("nocolon, :core, x:bogus, ok:full") == {"ok": "full"}
    assert parse_tier_map(None) == {}
    assert parse_tier_map("") == {}


def test_step_ladder_and_floor():
    assert step("minimal", +1) == "core"
    assert step("core", +1) == "full"
    assert step("full", +1) == "full"                      # top no-op
    assert step("full", -1, floor="minimal") == "core"
    assert step("core", -1, floor="core") == "core"        # floors at default
    assert step("minimal", -1, floor="minimal") == "minimal"


def test_session_state_ttl_and_none_key():
    s = SessionTierState(ttl_s=0.0)   # everything instantly stale
    s.set("a", "full")
    assert s.get("a") is None
    s2 = SessionTierState()
    s2.set(None, "core")              # None key -> global bucket
    assert s2.get(None) == "core"
    assert s2.get("other") is None


def test_resolve_tier_precedence():
    state = SessionTierState()
    kw = dict(state=state, tier_map={"claude-desktop": "minimal"}, default_tier="core")
    # env default when nothing else matches
    assert resolve_tier(None, "s1", **kw) == "core"
    # writer map beats default (case/space-insensitive writer)
    assert resolve_tier(" Claude-Desktop ", "s1", **kw) == "minimal"
    # session override beats writer map
    state.set("s1", "full")
    assert resolve_tier("claude-desktop", "s1", **kw) == "full"
