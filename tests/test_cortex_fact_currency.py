"""`memory_search`'s cortex block must let the reader judge currency.

The cortex is the layer an agent trusts most — one *current* value per
slot — so a stale fact there is worse than a stale entry. Entries carry a
timestamp; the cortex block did not, and not even ``verbose=True`` added
one.

The failure that motivated this (2026-07-26): a query for which extractor
prompt is deployed returned, in one block and both ``contested: false``,

    extraction-prompt-system-prompt / version    -> "v2 (live on shim ...)"
    Sonnet sidecar / primary-extractor           -> "... (v1 prompt)"

The v1 fact was ten days older and simply never updated, because it sits
at a *different* ``(entity, attribute)`` slot — supersession is keyed on
that pair and is structurally blind to the same fact recorded under a
second entity name. With no date on either, the stale one is
indistinguishable from the fresh one, and an agent picked v1.

Entity canonicalisation is the real fix and is a separate piece of work.
This is the cheap half: `service.cortex_search` already returns
``asserted_at`` / ``last_confirmed`` / ``age`` — the MCP tool was
projecting them away. Showing them is what makes the staleness visible.
"""

from __future__ import annotations

import time

import pytest

from tests.helpers import reload_mcp_filemode as _reload_mcp_filemode


def _fact(entity, attribute, value, *, age_days):
    ts = time.time() - age_days * 86400.0
    return {
        "entity": entity, "attribute": attribute, "value": value,
        "polarity": "+", "status": "current", "confidence": 0.9,
        "origin": "agent", "support": [], "provenance": [],
        "asserted_at": ts, "last_confirmed": ts,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "tx_time": ts, "valid_time": None,
        "writer_id": "w", "session_id": "s", "age": f"{age_days}d ago",
        "score": 0.6,
    }


def _set_fact(entity, attribute, value, *, age_days, members):
    """The set-slot shape ``cortex_search`` produces (Task 6 re-review):
    ``asserted_at``/``last_confirmed``/``age`` are all backed by the same
    anchor (max ``tx_time or asserted_at`` over current members) rather
    than one canonical record's own stamp. Kept here as a HAND-authored
    shape (this file never calls the real service — every test stubs
    ``cortex_search`` — so it deliberately mirrors the production dict,
    not the other way round)."""
    ts = time.time() - age_days * 86400.0
    return {
        "kind": "set", "entity": entity, "attribute": attribute,
        "value": value, "members": members, "score": 0.6,
        "contested": False,
        "asserted_at": ts, "last_confirmed": ts, "age": f"{age_days}d ago",
    }


@pytest.fixture()
def two_versions(tmp_path, monkeypatch):
    """The real 2026-07-26 shape: same underlying fact, two entity names,
    ten days apart, neither contested. Also carries a set-slot entry
    (review finding: the pre-existing fixtures here were scalar-only, so
    the currency guard below was structurally blind to whether a set
    entry carries asserted_at/age at all — ``cortex_search`` did not add
    them until this same review round)."""
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    facts = [
        _fact("extraction-prompt-system-prompt", "version", "v2", age_days=1),
        _fact("Sonnet sidecar", "primary-extractor", "shim (v1 prompt)",
              age_days=11),
        _set_fact("user", "bikes owned",
                  "road bike; gravel bike (2 members)", age_days=5,
                  members=[{"value": "road bike"}, {"value": "gravel bike"}]),
    ]
    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": False,
    })
    monkeypatch.setattr(mod.service, "cortex_search",
                        lambda *a, **k: {"entries": facts})
    return mod


def _cortex_block(mod, **kw):
    return mod.memory_search(query="which prompt is deployed", **kw)["cortex"]


def test_cortex_facts_carry_an_assertion_timestamp(two_versions):
    """Without this the reader cannot tell a July-11 fact from a July-21 one."""
    block = _cortex_block(two_versions)

    assert block, "no cortex facts returned"
    for f in block:
        assert "asserted_at" in f, f"no asserted_at on {f['entity']!r}"


def test_timestamps_are_iso_to_the_second(two_versions):
    """Epoch floats are unreadable in-context; the agent should not have to
    convert. Second precision, so two same-day writes are orderable."""
    block = _cortex_block(two_versions)

    for f in block:
        ts = f["asserted_at"]
        assert isinstance(ts, str), f"asserted_at is {type(ts).__name__}, want str"
        # 2026-07-26T15:23:03 — date, T, time to the second.
        assert len(ts) >= 19 and ts[10] == "T" and ts[13] == ":" and ts[16] == ":", ts


def test_cortex_facts_carry_a_human_age(two_versions):
    """A relative age is what actually catches the eye mid-task; the
    absolute stamp is for pinning it down."""
    block = _cortex_block(two_versions)

    for f in block:
        assert f.get("age"), f"no age on {f['entity']!r}"


def test_the_stale_rival_is_distinguishable_from_the_fresh_one(two_versions):
    """The whole point: two facts about the same thing, ten days apart,
    must not look equally authoritative."""
    block = _cortex_block(two_versions)
    by_entity = {f["entity"]: f for f in block}

    fresh = by_entity["extraction-prompt-system-prompt"]["asserted_at"]
    stale = by_entity["Sonnet sidecar"]["asserted_at"]
    assert fresh > stale, (
        "cannot order the two facts by assertion time — this is exactly the "
        "state in which an agent picks the stale one")
