"""Supersede-at-discovery: aged/contested facts carry a ready-made correction.

The failure that motivated this (2026-07-29, outcome signal 327): a session
recalled a world fact describing work as pending, discovered from the code
that it had shipped 11 days earlier, reported the contradiction in prose —
and never corrected the record. The briefing's TRUST ORDER instruction alone
demonstrably does not produce supersede-at-discovery; the affordance has to
sit in the tool response, at the moment of recall, naming the exact call for
the exact slot so acting costs a copy-paste rather than a recalled procedure.

Gate design note: the incident fact was volatile and 11 days old — NOT yet
``stale`` (2xTTL = 42 days). A gate on the existing stale flag would have
missed it, so the affordance fires at TTL/3 (volatile -> 7d, slow -> 90d,
evergreen -> never), plus always on stale or contested facts.
"""

from __future__ import annotations

import importlib
import time

import pytest

DAY = 86400.0


def _reload_mcp_filemode(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def _cortex_fact(entity, attribute, value, *, age_days, freshness_class=None,
                 contested=False, stale=False):
    ts = time.time() - age_days * DAY
    d = {
        "entity": entity, "attribute": attribute, "value": value,
        "polarity": "+", "status": "current", "confidence": 0.9,
        "origin": "agent", "support": [], "provenance": [],
        "asserted_at": ts, "last_confirmed": ts,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "tx_time": ts, "valid_time": None,
        "writer_id": "w", "session_id": "s", "age": f"{age_days}d ago",
        "score": 0.6, "contested": contested,
    }
    if contested:
        d["contender_value"] = "rival"
        d["contender_origin"] = "agent"
    if freshness_class:
        d["freshness_class"] = freshness_class
        d["effective_confidence"] = 0.6
        d["stale"] = stale
    return d


def _world_fact(entity, attribute, value, *, age_days,
                freshness_class="volatile", stale=False):
    ts = time.time() - age_days * DAY
    return {
        "entity": entity, "attribute": attribute, "value": value,
        "polarity": "+", "status": "current", "confidence": 0.85,
        "effective_confidence": 0.6, "stale": stale, "origin": "world",
        "freshness_class": freshness_class,
        "source_url": "https://example.com/spec",
        "source_quote": "quoted claim",
        "retrieved_at": ts, "asserted_at": ts, "last_confirmed": ts,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "score": 0.7,
    }


def _search_with_cortex(mod, monkeypatch, facts):
    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": False,
    })
    monkeypatch.setattr(mod.service, "cortex_search",
                        lambda *a, **k: {"entries": facts})
    return mod.memory_search(query="what is the current state")


# ── the TTL/3 gate (pure freshness unit) ──────────────────────────────────


def test_nudge_gate_fires_on_the_incident_shape():
    """Volatile + 11 days old: not yet stale, but past TTL/3 (7d) — this is
    exactly the fact the 2026-07-29 session recalled and left uncorrected."""
    from pseudolife_memory.memory.freshness import is_stale, needs_correction_nudge
    now = time.time()
    anchor = now - 11 * DAY
    assert is_stale("volatile", anchor, now=now) is False
    assert needs_correction_nudge("volatile", anchor, now=now) is True


def test_nudge_gate_quiet_on_fresh_and_evergreen():
    from pseudolife_memory.memory.freshness import needs_correction_nudge
    now = time.time()
    assert needs_correction_nudge("volatile", now - 1 * DAY, now=now) is False
    assert needs_correction_nudge("evergreen", now - 500 * DAY, now=now) is False
    assert needs_correction_nudge("evergreen", None, now=now) is False


def test_nudge_gate_slow_class_threshold_scales_with_ttl():
    """Slow facts rot across ~9 months; a 30-day-old one does not need a
    weekly 'still true?' tag, a 100-day-old one does (TTL/3 = 90d)."""
    from pseudolife_memory.memory.freshness import needs_correction_nudge
    now = time.time()
    assert needs_correction_nudge("slow", now - 30 * DAY, now=now) is False
    assert needs_correction_nudge("slow", now - 100 * DAY, now=now) is True


# ── memory_search cortex block ────────────────────────────────────────────


def test_aged_volatile_cortex_fact_carries_the_exact_correction_call(
        tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    out = _search_with_cortex(mod, monkeypatch, [
        _cortex_fact("daemon", "deployed-version", "v0.8", age_days=11,
                     freshness_class="volatile"),
    ])

    (fact,) = out["cortex"]
    cw = fact.get("correct_with")
    assert cw, "aged volatile fact has no correct_with affordance"
    assert cw.startswith("memory_fact_set("), cw
    assert "'daemon'" in cw and "'deployed-version'" in cw, cw


def test_fresh_and_evergreen_cortex_facts_are_not_nagged(tmp_path, monkeypatch):
    """The affordance must stay rare enough to mean something: a fresh
    volatile fact and a durable evergreen one carry nothing extra."""
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    out = _search_with_cortex(mod, monkeypatch, [
        _cortex_fact("daemon", "deployed-version", "v0.8", age_days=1,
                     freshness_class="volatile"),
        _cortex_fact("user", "birthday", "March 3", age_days=300),
    ])

    for fact in out["cortex"]:
        assert "correct_with" not in fact, fact["entity"]
    assert "correction_note" not in out


def test_contested_fact_carries_the_affordance_regardless_of_age(
        tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    out = _search_with_cortex(mod, monkeypatch, [
        _cortex_fact("daemon", "deployed-version", "v0.8", age_days=0,
                     contested=True),
    ])

    (fact,) = out["cortex"]
    assert fact.get("correct_with", "").startswith("memory_fact_set("), fact


def test_response_carries_one_norm_note_when_any_fact_is_flagged(
        tmp_path, monkeypatch):
    """The per-fact template gives the mechanics; the response-level note
    states the norm at the moment it applies — correct NOW, not in prose."""
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    out = _search_with_cortex(mod, monkeypatch, [
        _cortex_fact("daemon", "deployed-version", "v0.8", age_days=11,
                     freshness_class="volatile"),
        _cortex_fact("user", "birthday", "March 3", age_days=300),
    ])

    note = out.get("correction_note", "")
    assert "correct_with" in note
    assert "now" in note.lower()


# ── memory_world_search ───────────────────────────────────────────────────


def test_aged_world_fact_carries_a_world_set_call_with_citation_slot(
        tmp_path, monkeypatch):
    """The incident fact: volatile, 11 days old, stale=False. The compact
    (default) projection must keep the affordance — that is the shape agents
    actually read."""
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service, "world_search", lambda *a, **k: {
        "count": 1, "entries": [
            _world_fact("MCP spec 2026-07-28", "sessionless_identity_implications",
                        "work pending", age_days=11),
        ]})
    out = mod.memory_world_search(query="mcp spec sessionless identity")

    (entry,) = out["entries"]
    cw = entry.get("correct_with")
    assert cw, "aged world fact has no correct_with affordance"
    assert cw.startswith("memory_world_set("), cw
    assert "'MCP spec 2026-07-28'" in cw, cw
    assert "'sessionless_identity_implications'" in cw, cw
    assert "source_url" in cw, cw
    assert "correct_with" in out.get("correction_note", "")


def test_stale_world_fact_is_flagged_even_if_class_boundaries_move(
        tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service, "world_search", lambda *a, **k: {
        "count": 1, "entries": [
            _world_fact("lib", "latest-version", "1.2", age_days=50, stale=True),
        ]})
    out = mod.memory_world_search(query="lib latest version")

    assert out["entries"][0].get("correct_with", "").startswith(
        "memory_world_set(")


def test_fresh_and_evergreen_world_facts_are_not_nagged(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service, "world_search", lambda *a, **k: {
        "count": 2, "entries": [
            _world_fact("lib", "latest-version", "1.2", age_days=2),
            _world_fact("paper", "finding", "X holds", age_days=400,
                        freshness_class="evergreen"),
        ]})
    out = mod.memory_world_search(query="anything")

    for entry in out["entries"]:
        assert "correct_with" not in entry, entry["entity"]
    assert "correction_note" not in out


# ── memory_fact_get ───────────────────────────────────────────────────────


def test_fact_get_attaches_affordance_to_an_aged_record(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    rec = _cortex_fact("daemon", "deployed-version", "v0.8", age_days=11,
                       freshness_class="volatile")
    monkeypatch.setattr(mod.service, "cortex_lookup", lambda *a, **k: rec)
    monkeypatch.setattr(mod.service, "cortex_contenders",
                        lambda *a, **k: {"contenders": []})
    monkeypatch.setattr(mod.service, "entity_ref", lambda *a, **k: None)
    out = mod.memory_fact_get(entity="daemon", attribute="deployed-version")

    assert out["record"].get("correct_with", "").startswith("memory_fact_set(")
    assert "correct_with" in out.get("correction_note", "")


def test_fact_get_leaves_a_fresh_record_alone(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    rec = _cortex_fact("daemon", "deployed-version", "v0.8", age_days=1,
                       freshness_class="volatile")
    monkeypatch.setattr(mod.service, "cortex_lookup", lambda *a, **k: rec)
    monkeypatch.setattr(mod.service, "cortex_contenders",
                        lambda *a, **k: {"contenders": []})
    monkeypatch.setattr(mod.service, "entity_ref", lambda *a, **k: None)
    out = mod.memory_fact_get(entity="daemon", attribute="deployed-version")

    assert "correct_with" not in out["record"]
    assert "correction_note" not in out


# ── briefing pairing (mechanism 2) ────────────────────────────────────────


def test_trust_order_teaches_the_affordance():
    """The briefing is where the norm is taught, the affordance is where it
    is applied — TRUST ORDER must name `correct_with` and frame correction
    as part of discovery, not a follow-up. (The examples/CLAUDE.memory.md
    byte-pin in test_plugin_packaging keeps both halves identical.)"""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    assert "correct_with" in MEMORY_LOOP_BLOCK
    assert "memory_outcome" in MEMORY_LOOP_BLOCK
