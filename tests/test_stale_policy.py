"""Serving-side staleness policy (schema-free; spec
2026-08-09-serving-side-staleness-design.md, PR #121).

The retention-interval eval (ret-0809) measured that the annotation flags
halve unqualified stale serving but the answerer's compliance keys on value
shape — version/quantity-shaped values sail through visible flags. The
policy removes that discretion at the daemon:

* ``memory.search.stale_policy = "annotate"`` — today's behavior, default.
* ``"demote"`` — stale records sort after non-stale records on every list
  surface and carry a top-level ``warning``.
* ``"quarantine"`` — a stale record's ``value`` is replaced by a wrapper
  string and the original moves to ``last_known_value`` (data moved, never
  hidden).

Load-bearing contract (prereg gate 2, asserted structurally here): a
NON-stale record's payload is byte-identical across all three policies —
the policy must never touch fresh data. Version history is an audit chain
and stays raw under every policy.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import time

import pytest

from pseudolife_memory.memory import freshness
from pseudolife_memory.service import MemoryService

DAY = 86400.0
QUERY_OFFSET = 90 * DAY   # volatile (2x21d TTL) is stale; slow (270d) is not


def _result_json(raw) -> dict:
    """Parse a call_tool result across SDK shapes: v1 (content, structured)
    tuple, or v2 CallToolResult (structured when present, else text JSON)."""
    if isinstance(raw, tuple):
        return raw[1]
    structured = getattr(raw, "structured_content", None)
    if structured is not None:
        return structured
    return json.loads("".join(
        c.text for c in raw.content if hasattr(c, "text")))


class _FrozenClock:
    def __init__(self, now: float) -> None:
        self._now = float(now)

    def time(self) -> float:
        return self._now


@contextlib.contextmanager
def _frozen(epoch: float):
    """Freeze the freshness layer's default clock — the single seam every
    default-clock staleness read funnels through (same seam the
    retention-interval harness uses)."""
    real = freshness._time
    freshness._time = _FrozenClock(epoch)
    try:
        yield
    finally:
        freshness._time = real


@contextlib.contextmanager
def _svc(policy: str | None = None):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d)
        if policy is not None:
            s.config.memory.search.stale_policy = policy
        yield s


def _seed_world(svc) -> None:
    # Same query hits both; the volatile fact is the better embedding match
    # for it, so under "annotate" it outranks the slow fact — which is what
    # makes the demote-ordering assertion load-bearing.
    svc.world_write("edge-proxy", "tls-cert-serial", "5c31f2",
                    freshness_class="volatile",
                    source_url="https://bench.invalid/a")
    svc.world_write("edge-proxy", "hosting-vendor", "netlify",
                    freshness_class="slow",
                    source_url="https://bench.invalid/b")


# ── config ───────────────────────────────────────────────────────────────

def test_default_policy_is_annotate():
    from pseudolife_memory.utils.config import SearchConfig
    assert SearchConfig().stale_policy == "annotate"


def test_annotate_serves_todays_payload_shape():
    """No new keys, raw value — byte-for-byte today's behavior."""
    with _svc() as svc:
        _seed_world(svc)
        with _frozen(time.time() + QUERY_OFFSET):
            entries = svc.world_search("edge-proxy tls cert serial",
                                       top_k=5)["entries"]
    stale = [e for e in entries if e["stale"]]
    assert stale, "volatile fact should read stale at +90d"
    for e in entries:
        assert "warning" not in e
        assert "last_known_value" not in e
    assert stale[0]["value"] == "5c31f2"


# ── demote ───────────────────────────────────────────────────────────────

def test_demote_sorts_stale_after_fresh_on_list_surfaces():
    with _svc("demote") as svc:
        _seed_world(svc)
        with _frozen(time.time() + QUERY_OFFSET):
            searched = svc.world_search("edge-proxy tls cert serial",
                                        top_k=5)["entries"]
            dumped = svc.world_dump()["entries"]
    for entries in (searched, dumped):
        flags = [bool(e["stale"]) for e in entries]
        assert flags == sorted(flags), (
            "stale records must sort after non-stale ones")
    # The ordering actually inverted something: under annotate the stale
    # fact wins this query (guarded in the annotate test's stale[0]).
    assert searched[0]["stale"] is False


def test_demote_adds_warning_to_stale_records_only():
    with _svc("demote") as svc:
        _seed_world(svc)
        with _frozen(time.time() + QUERY_OFFSET):
            entries = svc.world_search("edge-proxy tls cert serial",
                                       top_k=5)["entries"]
    by_stale = {bool(e["stale"]): e for e in entries}
    assert by_stale[True]["warning"] == (
        "stale — re-verify before relying on this value")
    assert "warning" not in by_stale[False]
    # Demote never touches the value itself.
    assert by_stale[True]["value"] == "5c31f2"


# ── quarantine ───────────────────────────────────────────────────────────

def test_quarantine_moves_value_never_hides_it():
    with _svc("quarantine") as svc:
        _seed_world(svc)
        with _frozen(time.time() + QUERY_OFFSET):
            entries = svc.world_search("edge-proxy tls cert serial",
                                       top_k=5)["entries"]
            single = svc.world_lookup("edge-proxy", "tls-cert-serial")
    stale = [e for e in entries if e["stale"]]
    for rec in (*stale, single):
        assert rec["value"] == "(stale — re-verify; last known value below)"
        assert rec["last_known_value"] == "5c31f2"


def test_quarantine_applies_on_cortex_surfaces_too():
    """The policy sits at the shared render sites, so the personal cortex —
    including the fact_get single read — behaves identically."""
    with _svc("quarantine") as svc:
        svc.cortex_write("deploy", "status", "pending",
                         freshness_class="volatile", provenance=["seed"])
        with _frozen(time.time() + QUERY_OFFSET):
            got = svc.cortex_search("deploy status", top_k=3)["entries"]
            one = svc.cortex_lookup("deploy", "status")
    hit = next(e for e in got if e["entity"] == "deploy")
    for rec in (hit, one):
        assert rec["value"] == "(stale — re-verify; last known value below)"
        assert rec["last_known_value"] == "pending"


# ── the load-bearing structural contract ─────────────────────────────────

def test_fresh_records_are_byte_identical_across_policies():
    """Prereg gate 2, structural half: the policy must not touch non-stale
    records AT ALL. Same seeds, same frozen clock, three policies — the
    fresh (slow-class, within TTL) record's payload must not differ by one
    byte. Timestamps are seed-time-dependent, so all three arms render from
    one service, switching the knob between renders."""
    with _svc() as svc:
        _seed_world(svc)
        now = time.time() + QUERY_OFFSET
        rendered = {}
        for policy in ("annotate", "demote", "quarantine"):
            svc.config.memory.search.stale_policy = policy
            with _frozen(now):
                entries = svc.world_search("edge-proxy tls cert serial",
                                           top_k=5)["entries"]
            fresh = [e for e in entries if not e["stale"]]
            assert fresh, "slow-class fact must remain non-stale at +90d"
            rendered[policy] = json.dumps(fresh, sort_keys=True)
        assert rendered["annotate"] == rendered["demote"] == \
            rendered["quarantine"]


def test_history_stays_raw_under_quarantine():
    """Version history is the audit chain — quarantining it would destroy
    the record of what was actually stored (and gate 3's recovery path)."""
    with _svc("quarantine") as svc:
        svc.cortex_write("deploy", "status", "pending",
                         freshness_class="volatile", provenance=["seed"])
        svc.cortex_write("deploy", "status", "done",
                         freshness_class="volatile", provenance=["seed"],
                         support="user")
        with _frozen(time.time() + QUERY_OFFSET):
            hist = svc.history("deploy", "status")
    values = [v["value"] for v in hist["versions"]]
    assert "pending" in values and "done" in values
    for v in hist["versions"]:
        assert "last_known_value" not in v


# ── compact search block propagation (mcp_server) ────────────────────────

def test_compact_search_block_propagates_policy_fields(tmp_path, monkeypatch):
    """memory_search's cortex-first block re-selects keys from cortex_search
    output; the quarantine fields must survive that selection, or the
    most-used read surface silently serves the wrapper with no original."""
    import importlib

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    mod.service.config.memory.search.stale_policy = "quarantine"
    mod.service.cortex_write("deploy", "status", "pending",
                             freshness_class="volatile", provenance=["seed"])
    import asyncio
    with _frozen(time.time() + QUERY_OFFSET):
        raw = asyncio.run(mod.mcp.call_tool(
            "memory_search", {"query": "deploy status"}))
    structured = _result_json(raw)
    facts = [f for f in structured["cortex"] if f["entity"] == "deploy"]
    assert facts, structured["cortex"]
    f = facts[0]
    assert f["value"] == "(stale — re-verify; last known value below)"
    assert f["last_known_value"] == "pending"


# ── review findings 1+2 (2026-08-09 pass): tool-layer leaks ──────────────

def test_world_search_compact_projection_carries_policy_fields(tmp_path,
                                                               monkeypatch):
    """Finding 1: memory_world_search's default (non-verbose) projection is
    a fixed key allowlist — without `warning`/`last_known_value` it serves
    the quarantine wrapper with the original value destroyed, violating
    'data moved, never hidden' on the shipping world surface."""
    import asyncio
    import importlib

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    mod.service.config.memory.search.stale_policy = "quarantine"
    mod.service.world_write("edge-proxy", "tls-cert-serial", "5c31f2",
                            freshness_class="volatile",
                            source_url="https://bench.invalid/a")
    with _frozen(time.time() + QUERY_OFFSET):
        raw = asyncio.run(mod.mcp.call_tool(
            "memory_world_search", {"query": "edge-proxy tls cert serial"}))
    structured = _result_json(raw)
    stale = [e for e in structured["entries"] if e.get("stale")]
    assert stale, structured["entries"]
    assert stale[0]["value"] == "(stale — re-verify; last known value below)"
    assert stale[0]["last_known_value"] == "5c31f2"

    mod.service.config.memory.search.stale_policy = "demote"
    with _frozen(time.time() + QUERY_OFFSET):
        raw = asyncio.run(mod.mcp.call_tool(
            "memory_world_search", {"query": "edge-proxy tls cert serial"}))
    structured = _result_json(raw)
    stale = [e for e in structured["entries"] if e.get("stale")]
    assert stale[0]["warning"] == (
        "stale — re-verify before relying on this value")


def test_search_restatement_dedup_keys_on_underlying_value(tmp_path,
                                                           monkeypatch):
    """Finding 2: memory_search suppresses recall hits that restate a
    surfaced cortex value. Under quarantine the served value is the wrapper
    string, so keying the dedup on it re-exposes the raw stale value in
    ``entries`` right below the quarantined fact — the exact leak the
    policy exists to prevent."""
    import asyncio
    import importlib

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    mod.service.config.memory.search.stale_policy = "quarantine"
    mod.service.store("keydb-engine", source="notes")
    mod.service.cortex_write("cache-tier", "deployed engine", "keydb-engine",
                             freshness_class="volatile", provenance=["seed"])
    with _frozen(time.time() + QUERY_OFFSET):
        raw = asyncio.run(mod.mcp.call_tool(
            "memory_search", {"query": "cache tier deployed engine"}))
    structured = _result_json(raw)
    facts = [f for f in structured["cortex"] if f["entity"] == "cache-tier"]
    assert facts and facts[0]["last_known_value"] == "keydb-engine"
    leaked = [e for e in structured.get("entries", [])
              if "keydb-engine" in (e.get("text") or "")]
    assert leaked == [], (
        "raw stale value re-exposed in entries below its quarantined fact")


# ── set slots: outside the staleness machinery, pinned (review finding 3) ─

def test_set_slots_are_outside_the_staleness_machinery():
    """Set members are structurally evergreen — a deliberate rule, not a
    default: ``_insert_member`` hardcodes the class, and scalar→set
    conversion drops a non-evergreen scalar's class with an explicit
    ``dropped_freshness_class`` audit stamp (rationale in
    docs/guide/memory-model.md, "Conversion rules"; drop pinned in
    tests/test_cortex_sets.py) — so no set payload can ever be policy-
    transformed. Pin that as a no-harm guard: a set-group search entry
    (including one converted from a VOLATILE scalar) is byte-identical
    across all three policies even past the staleness horizon."""
    with _svc() as svc:
        svc.cortex_write("garage", "bikes", "commuter",
                         freshness_class="volatile", provenance=["seed"])
        svc.set_add("garage", "bikes", "roadbike")
        now = time.time() + QUERY_OFFSET
        rendered = {}
        for policy in ("annotate", "demote", "quarantine"):
            svc.config.memory.search.stale_policy = policy
            with _frozen(now):
                got = svc.cortex_search("garage bikes", top_k=5)["entries"]
            grp = next(e for e in got if e.get("kind") == "set")
            rendered[policy] = json.dumps(grp, sort_keys=True)
    assert rendered["annotate"] == rendered["demote"] == \
        rendered["quarantine"]
    assert "commuter" in json.loads(rendered["annotate"])["value"]
