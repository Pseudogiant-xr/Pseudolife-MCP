"""Service-level cortex wiring: read/write/supersede/search roundtrips, history,
co-persistence, and contenders — the provenance guard surfaces a conflicting
agent value as a contender against a user fact, and resolve() promotes/retires
it.

Runs against a real MemoryService (offline embedder), never production state.
Most tests take conftest's shared ``pristine_service`` — the cortex is a
slot-keyed store and the fixture empties it per test, so a private data dir
buys nothing but a service construction. The three that DO build their own
service say why at the call site: two re-open a second service on the same
data dir, and one breaks the service's save path irreversibly.
"""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService


def test_cortex_write_then_lookup_roundtrip_through_service(pristine_service):
    svc = pristine_service
    r = svc.cortex_write("grid", "size", "41", provenance=["ep1"])
    assert r["action"] == "inserted"
    assert r["value"] == "41"
    got = svc.cortex_lookup("grid", "size")
    assert got is not None
    assert got["value"] == "41"
    assert got["status"] == "current"
    assert got["provenance"] == ["ep1"]


def test_cortex_supersede_then_search_returns_current_only(pristine_service):
    svc = pristine_service
    svc.cortex_write("grid", "size", "40", provenance=["ep1"])
    svc.cortex_write("grid", "size", "41", provenance=["ep2"])
    assert svc.cortex_lookup("grid", "size")["value"] == "41"
    entries = svc.cortex_search("grid size", top_k=10)["entries"]
    values = [e["value"] for e in entries]
    assert "41" in values
    assert "40" not in values


def test_cortex_copersists_across_service_restart():
    # Own data dir: re-opens a SECOND service on it — the restart is the
    # subject, so the shared fixture cannot stand in.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("user", "city", "Sydney", provenance=["epX"])
        svc.save()
        svc2 = MemoryService(data_dir=d)
        got = svc2.cortex_lookup("user", "city")
        assert got is not None
        assert got["value"] == "Sydney"
        assert got["provenance"] == ["epX"]


def test_cortex_read_includes_relative_age_and_stamp(pristine_service):
    svc = pristine_service
    svc.cortex_write("server", "port", "8080", support="user")
    got = svc.cortex_lookup("server", "port")
    assert got is not None
    assert got.get("age") == "just now"          # written moments ago
    assert got["tx_time"] and got["writer_id"]   # temporal stamp surfaced


def test_failed_cortex_save_surfaces_as_persistence_error():
    """A durable-save failure must NOT be swallowed: it surfaces to the caller
    and bumps the health-visible persist-error counter (F3)."""
    from pseudolife_memory.service import MemoryService, PersistenceError

    # Own service: it replaces ``_cortex.save`` with a raising stub and bumps
    # ``_persist_errors``, neither of which the bank clear undoes — on the
    # shared service both would leak into every later test in the module.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc._ensure_init()
        assert svc._cortex is not None

        def _boom(*a, **k):
            raise OSError("disk full")

        svc._cortex.save = _boom  # force the durable write to fail
        raised = False
        try:
            svc.cortex_write("server", "port", "8080", support="user")
        except PersistenceError:
            raised = True
        assert raised, "a failed cortex save must surface, not be swallowed"
        assert svc._persist_errors >= 1


def test_memory_history_returns_version_timeline(pristine_service):
    svc = pristine_service
    svc.cortex_write("server", "port", "8080", support="user", now=1000.0)
    svc.cortex_write("server", "port", "9090", support="user", now=2000.0)
    hist = svc.history("server", "port")
    assert hist["count"] >= 2
    values = [v["value"] for v in hist["versions"]]
    assert "8080" in values and "9090" in values
    for v in hist["versions"]:               # each version is attributed
        assert "writer_id" in v and "tx_time" in v
    txs = [v["tx_time"] for v in hist["versions"]]
    assert txs == sorted(txs)                # oldest -> newest


# ── memory_history as_of (per-slot point-in-time read) ───────────────────

def test_history_as_of_filters_versions(pristine_service):
    svc = pristine_service
    # Injected write times rather than sleeps — as_of compares against
    # the stamp, so the ordering contract needs no wall-clock gap.
    svc.cortex_write("team", "mascot", "fox", confidence=0.9,
                     support="user", now=1000.0)
    mid = 1500.0
    svc.cortex_write("team", "mascot", "owl", confidence=0.9,
                     support="user", now=2000.0)

    full = svc.history("team", "mascot")
    assert full["count"] == 2 and "as_of" not in full

    at_mid = svc.history("team", "mascot", as_of=mid)
    assert at_mid["count"] == 1
    assert at_mid["versions"][0]["value"] == "fox"
    assert at_mid["as_of"] == mid

    later = svc.history("team", "mascot", as_of=3000.0)
    assert later["count"] == 2


def test_history_as_of_accepts_iso_string(pristine_service):
    from datetime import datetime, timedelta

    svc = pristine_service
    svc.cortex_write("team", "mascot", "fox", confidence=0.9,
                     support="user")
    tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
    out = svc.history("team", "mascot", as_of=tomorrow)
    assert out["count"] == 1
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    out = svc.history("team", "mascot", as_of=yesterday)
    assert out["count"] == 0


def test_history_as_of_set_slot(pristine_service):
    # set_add takes no injected clock, so this one keeps real gaps.
    import time as _t

    svc = pristine_service
    svc.set_add("user", "tags", "alpha")
    _t.sleep(0.02)
    mid = _t.time()
    _t.sleep(0.02)
    svc.set_add("user", "tags", "beta")
    out = svc.history("user", "tags", as_of=mid)
    assert out["kind"] == "set"
    assert [v["value"] for v in out["versions"]] == ["alpha"]


def test_fact_get_miss_returns_candidates(pristine_service):
    svc = pristine_service
    svc.cortex_write("server", "port", "8080", support="user")
    got = svc.cortex_candidates("server", "nonexistent-attr")
    assert got and got[0]["why"] == "same_entity"
    assert got[0]["attribute"] == "port"
    # A genuinely similar slot surfaces via embeddings too.
    sim = svc.cortex_candidates("srv", "port number")
    assert any(c["why"] == "similar_slot" and c["entity"] == "server"
               for c in sim)


def test_store_agent_fact_parks_contender_against_user_fact(pristine_service):
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    out = svc.cortex_write("project", "language", "rust", support="agent")
    assert out["action"] == "contested"
    assert out["current"]["value"] == "go"      # user fact still current
    assert out["value"] == "rust"               # the contender (flat record)
    conts = svc.cortex_contenders("project", "language")["contenders"]
    assert len(conts) == 1 and conts[0]["value"] == "rust"


def test_cortex_resolve_accept_then_lookup_returns_new_value_and_persists():
    # Own data dir: the persistence half re-opens a second service on it.
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


def test_cortex_resolve_reject_keeps_current(pristine_service):
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    svc.cortex_write("project", "language", "rust", support="agent")
    res = svc.cortex_resolve("project", "language", accept=False)
    assert res["resolved"] is True and res["accepted"] is False
    assert svc.cortex_lookup("project", "language")["value"] == "go"
    assert svc.cortex_contenders("project", "language")["contenders"] == []


def test_cortex_resolve_no_contender(pristine_service):
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    res = svc.cortex_resolve("project", "language", accept=True)
    assert res["resolved"] is False and res["reason"] == "no_contender"


def test_compression_echo_confirms_instead_of_contesting(pristine_service):
    # The dream re-extracting a slot from the same status entry emits a
    # terser re-statement of the standing value; that is corroboration, not
    # a conflict, and must not park a contender (2026-08-05 audit: 4 of 7
    # contested slots were exactly this echo shape).
    svc = pristine_service
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


def test_novel_number_is_never_treated_as_echo(pristine_service):
    # A shorter value that introduces a new digit-bearing token is new
    # information (a changed version/number), so the conflict path must be
    # preserved — the agent write parks as a contender against a user fact.
    svc = pristine_service
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


def test_cortex_search_flags_contested_entries(pristine_service):
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    svc.cortex_write("project", "language", "rust", support="agent")
    entries = svc.cortex_search("project language", top_k=5)["entries"]
    assert entries and entries[0]["contested"] is True
    assert entries[0]["contender_value"] == "rust"


# ── cortex_dump carries the contested flag (the Console's /api/facts path) ──
#
# Before 2026-09-05 only ``cortex_search`` set ``contested`` /
# ``contender_value``; ``cortex_dump`` never did, so the Console's Cortex
# view (which reads the dump) never showed Accept/Discard against a real
# bank and the Observatory's ``facts_contested`` read 0. The web fixtures
# synthesised the flag, which is why the demo looked right. These run
# against the REAL service, never FixtureService.

def test_cortex_dump_flags_contested_scalar_slot(pristine_service):
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    svc.cortex_write("project", "language", "rust", support="agent")
    svc.cortex_write("project", "license", "apache-2.0", support="user")
    rows = {(r["entity"], r["attribute"]): r
            for r in svc.cortex_dump()["entries"]}
    lang = rows[("project", "language")]
    assert lang["value"] == "go"                  # the current value is served
    assert lang["contested"] is True
    assert lang["contender_value"] == "rust"
    assert lang["contender_origin"] == "agent"
    lic = rows[("project", "license")]
    assert lic["contested"] is False
    assert "contender_value" not in lic           # same shape cortex_search serves


def test_cortex_dump_flags_contender_parked_against_set_slot(pristine_service):
    """A contender parked before the slot converted to a set stays parked
    against the set (dismissable via ``cortex_resolve(accept=False)`` once
    fix/set-slot-contender-retire lands). Every member row of that slot
    carries the flag, so a reader filtering rows sees the truth per row."""
    svc = pristine_service
    svc.cortex_write("user", "bikes owned", "road bike", support="user")
    svc.cortex_write("user", "bikes owned", "gravel bike", support="agent")
    svc.set_add("user", "bikes owned", "hybrid bike")
    members = [r for r in svc.cortex_dump()["entries"]
               if (r["entity"], r["attribute"]) == ("user", "bikes owned")]
    assert sorted(m["value"] for m in members) == ["hybrid bike", "road bike"]
    assert all(m["kind"] == "member" for m in members)
    assert all(m["contested"] is True for m in members)
    assert all(m["contender_value"] == "gravel bike" for m in members)


def test_cortex_dump_flags_aggregate_guard_contender(pristine_service):
    """The other way a set add parks: a number-led scalar keeps the slot
    scalar and the blocked member sits as its contender."""
    svc = pristine_service
    svc.cortex_write("user", "birds", "27", support="user")
    svc.set_add("user", "birds", "Northern Flicker")
    row = next(r for r in svc.cortex_dump()["entries"]
               if (r["entity"], r["attribute"]) == ("user", "birds"))
    assert row["kind"] == "scalar" and row["value"] == "27"
    assert row["contested"] is True
    assert row["contender_value"] == "Northern Flicker"


def test_overview_facts_contested_counts_real_contested_slots(pristine_service):
    """``/api/overview`` → ``counts.facts_contested`` is the number of
    contested SLOTS on the real service: a set slot with two member rows
    and one contender counts once, not twice."""
    from pseudolife_memory.web.routes import ConsoleRoutes
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    svc.cortex_write("project", "language", "rust", support="agent")
    svc.cortex_write("user", "bikes owned", "road bike", support="user")
    svc.cortex_write("user", "bikes owned", "gravel bike", support="agent")
    svc.set_add("user", "bikes owned", "hybrid bike")
    svc.cortex_write("project", "license", "apache-2.0", support="user")
    ov = ConsoleRoutes(svc).dispatch("GET", "/api/overview", {}, {})
    assert ov["counts"]["facts"] == 4              # go, road, hybrid, apache
    assert ov["counts"]["facts_contested"] == 2


def test_cortex_dump_flag_clears_after_resolve_in_either_direction(pristine_service):
    """The dump's bucket is filtered on ``status == "contested"`` over ALL
    records — retired and superseded contenders live in the same list, so
    that filter is the only thing that stops a settled contest flagging its
    slot forever."""
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    svc.cortex_write("project", "language", "rust", support="agent")
    svc.cortex_resolve("project", "language", accept=False)      # retire
    row = next(r for r in svc.cortex_dump()["entries"]
               if (r["entity"], r["attribute"]) == ("project", "language"))
    assert row["value"] == "go"
    assert row["contested"] is False and "contender_value" not in row

    svc.cortex_write("project", "language", "zig", support="agent")
    svc.cortex_resolve("project", "language", accept=True)       # promote
    row = next(r for r in svc.cortex_dump()["entries"]
               if (r["entity"], r["attribute"]) == ("project", "language"))
    assert row["value"] == "zig"
    assert row["contested"] is False and "contender_value" not in row


def test_api_facts_route_carries_contested_from_real_service(pristine_service):
    """End to end through the route the Console reads (``/api/facts`` →
    ``_limited(cortex_dump)``), not the service method alone."""
    from pseudolife_memory.web.routes import ConsoleRoutes
    svc = pristine_service
    svc.cortex_write("project", "language", "go", support="user")
    svc.cortex_write("project", "language", "rust", support="agent")
    out = ConsoleRoutes(svc).dispatch("GET", "/api/facts", {"limit": "500"}, {})
    row = next(r for r in out["entries"]
               if (r["entity"], r["attribute"]) == ("project", "language"))
    assert row["contested"] is True
    assert row["contender_value"] == "rust"
    assert row["contender_origin"] == "agent"


def test_overview_facts_contested_keys_slots_like_the_store(pristine_service):
    """A set slot's member rows can spell the attribute differently — the
    converted scalar keeps its own strings, a later add keeps the caller's —
    while sharing one normalised key (casefold, separator runs collapsed).
    One slot, one contender, ONE in the count."""
    from pseudolife_memory.web.routes import ConsoleRoutes
    svc = pristine_service
    svc.cortex_write("user", "bikes owned", "road bike", support="user")
    svc.cortex_write("user", "bikes owned", "gravel bike", support="agent")
    svc.set_add("user", "Bikes-Owned", "hybrid bike")
    rows = [r for r in svc.cortex_dump()["entries"]
            if r["contested"] and r["kind"] == "member"]
    assert sorted(r["attribute"] for r in rows) == ["Bikes-Owned", "bikes owned"]
    ov = ConsoleRoutes(svc).dispatch("GET", "/api/overview", {}, {})
    assert ov["counts"]["facts_contested"] == 1
