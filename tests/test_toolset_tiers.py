"""Tier logic unit tests — pure module, no MCP/embedder."""
from __future__ import annotations

from pseudolife_memory.toolset_tiers import (
    TIERS, PrincipalTierState, normalize_tier, parse_tier_map, rank,
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


def test_principal_state_ttl_and_none_key():
    s = PrincipalTierState(ttl_s=0.0)   # everything instantly stale
    s.set("a", "full")
    assert s.get("a") is None
    s2 = PrincipalTierState()
    s2.set(None, "core")              # None key -> global bucket
    assert s2.get(None) == "core"
    assert s2.get("other") is None


def test_principal_state_normalizes_keys():
    # Principals share the writer-id namespace (lowercased); a shim-asserted
    # "Hermes-Box" and the token-map's "hermes-box" are the same bucket.
    s = PrincipalTierState()
    s.set(" Hermes-Box ", "full")
    assert s.get("hermes-box") == "full"
    assert s.get("") == s.get(None)   # empty collapses to the global bucket


def test_resolve_tier_precedence():
    state = PrincipalTierState()
    kw = dict(state=state, tier_map={"claude-desktop": "minimal"},
              default_tier="core")
    # env default when nothing else matches
    assert resolve_tier(None, **kw) == "core"
    # tier map beats default (case/space-insensitive principal)
    assert resolve_tier(" Claude-Desktop ", **kw) == "minimal"
    # principal override beats the map
    state.set("claude-desktop", "full")
    assert resolve_tier("claude-desktop", **kw) == "full"
    # other principals untouched by that override
    assert resolve_tier("hermes-box", **kw) == "core"
