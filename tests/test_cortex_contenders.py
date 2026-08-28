"""Service-level contenders: the provenance guard surfaces a conflicting agent
value as a contender against a user fact, and resolve() promotes/retires it.

Constructs a real MemoryService (offline embedder) against a throwaway data dir.
"""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService


def test_store_agent_fact_parks_contender_against_user_fact():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        out = svc.cortex_write("project", "language", "rust", support="agent")
        assert out["action"] == "contested"
        assert out["current"]["value"] == "go"      # user fact still current
        assert out["value"] == "rust"               # the contender (flat record)
        conts = svc.cortex_contenders("project", "language")["contenders"]
        assert len(conts) == 1 and conts[0]["value"] == "rust"


def test_cortex_resolve_accept_then_lookup_returns_new_value_and_persists():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        res = svc.cortex_resolve("project", "language", accept=True)
        assert res["resolved"] is True and res["accepted"] is True
        assert svc.cortex_lookup("project", "language")["value"] == "rust"
        # persisted: a fresh service reads the resolved value
        svc2 = MemoryService(data_dir=d)
        assert svc2.cortex_lookup("project", "language")["value"] == "rust"


def test_cortex_resolve_reject_keeps_current():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        res = svc.cortex_resolve("project", "language", accept=False)
        assert res["resolved"] is True and res["accepted"] is False
        assert svc.cortex_lookup("project", "language")["value"] == "go"
        assert svc.cortex_contenders("project", "language")["contenders"] == []


def test_cortex_resolve_no_contender():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        res = svc.cortex_resolve("project", "language", accept=True)
        assert res["resolved"] is False and res["reason"] == "no_contender"


def test_compression_echo_confirms_instead_of_contesting():
    # The dream re-extracting a slot from the same status entry emits a
    # terser re-statement of the standing value; that is corroboration, not
    # a conflict, and must not park a contender (2026-08-05 audit: 4 of 7
    # contested slots were exactly this echo shape).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write(
            "qwen-27b", "answerer-score",
            "0.808/0.731/0.782 (3 replicates, SAME sonnet-5 v1 bank, temp 0)",
            support="user")
        out = svc.cortex_write(
            "qwen-27b", "answerer-score",
            "0.808/0.731/0.782 cortex (3 replicates)", support="agent")
        assert out["action"] == "confirmed"
        cur = svc.cortex_lookup("qwen-27b", "answerer-score")
        assert cur["value"].startswith("0.808/0.731/0.782 (3 replicates, SAME")
        assert svc.cortex_contenders("qwen-27b", "answerer-score")["contenders"] == []


def test_novel_number_is_never_treated_as_echo():
    # A shorter value that introduces a new digit-bearing token is new
    # information (a changed version/number), so the conflict path must be
    # preserved — the agent write parks as a contender against a user fact.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("daemon", "deploy-state",
                         "deployed at v25, verified live on the host",
                         support="user")
        out = svc.cortex_write("daemon", "deploy-state", "v26 verified live",
                               support="agent")
        assert out["action"] == "contested"
        assert svc.cortex_lookup("daemon", "deploy-state")["value"].startswith(
            "deployed at v25")


def test_is_compression_echo_boundaries():
    from pseudolife_memory.memory.cortex import _is_compression_echo
    cur = "FIXED on branch galileo via PR 69, knob retired and replaced"
    # strict compression, all tokens contained
    assert _is_compression_echo("knob retired via PR 69", cur)
    # longer than current -> never an echo
    assert not _is_compression_echo(cur + " and redeployed to the host", cur)
    # mostly-novel wording -> genuine conflict
    assert not _is_compression_echo("wontfix, superseded by redesign", cur)
    # empty new value -> never an echo
    assert not _is_compression_echo("", cur)
    # polarity inversion built entirely from existing tokens -> never an
    # echo: negators disqualify regardless of containment
    cur2 = "knob retired and replaced; old name not reachable"
    assert not _is_compression_echo("knob not retired", cur2)
    # dropping the standing value's negation is an inversion too: a negator
    # on EITHER side disqualifies
    assert not _is_compression_echo(
        "deployed to the host", "not deployed; blocked on the ops rebuild pass")
    # too few tokens to distinguish echo from update
    assert not _is_compression_echo(
        "healthy", "unhealthy after the restart; healthy probe failing")
    assert not _is_compression_echo(
        "probe failing", "unhealthy after the restart; healthy probe failing")


def test_cortex_search_flags_contested_entries():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        entries = svc.cortex_search("project language", top_k=5)["entries"]
        assert entries and entries[0]["contested"] is True
        assert entries[0]["contender_value"] == "rust"
