"""Consolidation quarantine — the two-man rule for low-trust dream claims
(spec docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md,
PR #122; MAFIA-informed: defenses key on WHO wrote, never on what the text
says).

``memory.dream.quarantine_low_trust`` (ships OFF): when on, a dream claim
whose backing entry is agent-tier (``_origin_from_source(entry.source) ==
"agent"``) and whose source is outside ``memory.dream.trusted_sources``
never takes ``current`` directly — it parks via the existing contender
machinery, promotable only by explicit ``memory_fact_resolve(accept=True)``
or by an independent second witness (a later matching claim backed by a
different witness token, or by a non-agent origin). The same witness
restating does not count.

Witness identity is derived from persisted entry metadata only (the prereg
mandates no schema change): ``ep:<episode_id>`` when the entry has one,
else ``src:<source>``; a claim with no resolvable backing entry falls back
to its own ``origin`` field and — being agent-asserted with no witness at
all — quarantines under the same rule.

Scope v1 (documented): scalar claims only. Member ops keep their existing
guards (members are never contested by design; the aggregate guard already
parks the dangerous member-over-scalar case).

The composition corner — the quarantine's interaction with the span gate,
gate 5 of the 2026-08-12 stance+span-gate design — lives at the bottom of
the file-mode section.
"""
from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.service import MemoryService
from tests.dream_helpers import StubExtractor as _StubExtractor


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d)
        yield s


def _claim(value, origin="agent", source_idx=0, entity="payments-db",
           attribute="host"):
    return {"entity": entity, "attribute": attribute, "value": value,
            "confidence": 0.9, "origin": origin, "source": source_idx}


def test_quarantine_ships_off_and_off_means_todays_behavior(svc):
    """Same-tier newer-wins is today's contract: an agent claim supersedes
    an agent-origin current when the knob is off (and the knob IS off by
    default)."""
    assert svc.config.memory.dream.quarantine_low_trust is False
    assert svc.config.memory.dream.trusted_sources == []

    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store("payments-db host is db-prod-2", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-prod-2")]))
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-2"


def test_low_trust_claim_parks_and_current_is_unchanged(svc):
    """The load-bearing smoke (prereg gate 1): the poisoning path the
    provenance guard does NOT cover is same-tier agent-over-agent
    supersession — with quarantine on, the hostile claim parks."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store("payments-db host is db-evil-9", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-evil-9")]))

    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert [c["value"] for c in conts] == ["db-evil-9"]
    assert "quarantine:low_trust" in conts[0]["provenance"]
    assert out["quarantine_parked"] == 1


def test_user_origin_fact_stays_protected_too(svc):
    """Prereg gate 1 as literally written: hostile agent claim against a
    user-origin fact parks (the provenance guard would also catch this;
    quarantine must not regress it)."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user",
                     provenance=["seed"])
    svc.store("payments-db host is db-evil-9", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-evil-9")]))
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"


def test_empty_slot_low_trust_claim_parks_without_current(svc):
    """'Never takes current directly' includes brand-new slots: the claim
    parks as a currentless contender, visible and resolvable."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-new-1", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-new-1")]))

    assert svc.cortex_lookup("payments-db", "host") is None
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert [c["value"] for c in conts] == ["db-new-1"]
    assert out["quarantine_parked"] == 1

    # Route 1: explicit resolve promotes it.
    res = svc.cortex_resolve("payments-db", "host", accept=True)
    assert res["resolved"] is True
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-new-1"


def test_independent_second_witness_promotes(svc):
    """Route 2: a matching claim from a DIFFERENT witness token (here a
    different agent-tier source) is the second man — the parked value is
    promoted, and the promotion is stamped agent-supported, never
    masqueraded as a user confirmation."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-new-1", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-new-1")]))
    svc.store("payments database host confirmed db-new-1", source="claude")
    out = svc.dream_run(_StubExtractor([_claim("db-new-1")]))

    cur = svc.cortex_lookup("payments-db", "host")
    assert cur is not None and cur["value"] == "db-new-1"
    assert out["quarantine_promoted"] == 1
    assert "user" not in cur["support"]


def test_same_witness_restating_does_not_promote(svc):
    """Support breadth, not repetition: the same source re-asserting the
    parked value confirms the contender but never promotes it."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-new-1", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-new-1")]))
    svc.store("payments-db host is still db-new-1", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-new-1")]))

    assert svc.cortex_lookup("payments-db", "host") is None
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert [c["value"] for c in conts] == ["db-new-1"]
    assert out["quarantine_held"] == 1
    assert out.get("quarantine_promoted", 0) == 0


def test_non_agent_claim_matching_parked_value_promotes(svc):
    """Route 2, non-agent flavor: a conversation-backed (user-tier) claim
    matching the parked value promotes it."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-new-1", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-new-1")]))
    svc.store("the payments-db host is db-new-1", source="conversation")
    out = svc.dream_run(_StubExtractor([_claim("db-new-1", origin="user")]))

    cur = svc.cortex_lookup("payments-db", "host")
    assert cur is not None and cur["value"] == "db-new-1"
    assert out["quarantine_promoted"] == 1


def test_trusted_source_bypasses_quarantine(svc):
    """Operators opt sources in: a trusted agent-tier source consolidates
    exactly as today."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.config.memory.dream.trusted_sources = ["agent"]
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store("payments-db host is db-prod-2", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-prod-2")]))

    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-2"
    assert out.get("quarantine_parked", 0) == 0


def test_unbacked_agent_claim_quarantines_on_its_own_origin(svc):
    """A claim with no resolvable backing entry has no witness at all —
    it follows its own origin field and quarantines (documented fallback;
    the entries table persists no per-entry origin, so this is the most
    metadata the schema can offer)."""
    svc.config.memory.dream.quarantine_low_trust = True
    # Two entries in one batch + a claim citing neither -> src_entry None.
    svc.store("alpha note one", source="agent")
    svc.store("alpha note two", source="agent")
    claim = _claim("db-new-1")
    del claim["source"]
    out = svc.dream_run(_StubExtractor([claim]))

    assert svc.cortex_lookup("payments-db", "host") is None
    assert out["quarantine_parked"] == 1


def test_non_agent_sources_are_never_low_trust(svc):
    """conversation/user/tool-tier and unknown sources consolidate as
    today — the rule targets agent-tier writes only, as preregistered."""
    svc.config.memory.dream.quarantine_low_trust = True
    for source, value in (("conversation", "db-a"), ("tool", "db-b"),
                          ("notes", "db-c")):
        svc.store(f"payments-db host is {value}", source=source)
        out = svc.dream_run(_StubExtractor([_claim(value, origin="user")]))
        assert out.get("quarantine_parked", 0) == 0, source
    assert svc.cortex_lookup("payments-db", "host") is not None


# ── composition with the span gate: fidelity must not launder trust ──────
#
# Gate 5 of the 2026-08-12 stance+span-gate design. The span gate checks
# fidelity-to-source; this quarantine checks trustworthiness-of-source.
# They are orthogonal by design, and the dangerous composition error would
# be treating a PASSING span check as evidence of trust: a poisoned note
# quotes itself perfectly (the MAFIA-class attacker controls the note text,
# so fidelity is free for them).

_HOSTILE_NOTE = "payments-db host is db-evil-9"


def _hostile_claim(quote=None):
    c = {"entity": "payments-db", "attribute": "host", "value": "db-evil-9",
         "confidence": 0.95, "origin": "agent", "source": 0}
    if quote is not None:
        c["quote"] = quote
    return c


def test_perfect_self_quote_still_parks_under_two_man_rule(svc):
    """Gate 5 as preregistered: fidelity passing must not launder trust."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.config.memory.dream.span_gate = "contend"
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store(_HOSTILE_NOTE, source="agent")
    out = svc.dream_run(_StubExtractor(
        [_hostile_claim(quote=_HOSTILE_NOTE)]))

    # The quote IS verbatim — the span gate has nothing to flag...
    assert out["span_flagged"] == 0 and out["span_parked"] == 0
    # ...and the claim still parks, because trust is the quarantine's axis.
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert [c["value"] for c in conts] == ["db-evil-9"]
    assert "quarantine:low_trust" in conts[0]["provenance"]
    assert out["quarantine_parked"] == 1


def test_without_quarantine_the_same_claim_supersedes(svc):
    """The load-bearing-hook proof for the test above: span gate alone
    (contend, perfect quote) does NOT protect against a trusted-looking
    same-tier supersede — that protection is the quarantine's, and
    disabling it goes red on the previous test's assertion."""
    assert svc.config.memory.dream.quarantine_low_trust is False
    svc.config.memory.dream.span_gate = "contend"
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store(_HOSTILE_NOTE, source="agent")
    out = svc.dream_run(_StubExtractor(
        [_hostile_claim(quote=_HOSTILE_NOTE)]))

    assert out["span_flagged"] == 0
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-evil-9"


def test_low_trust_and_unbacked_quote_park_once_via_quarantine(svc):
    """Both axes failing must not double-park or double-count: quarantine
    routing owns the claim (it routes before the write), the span counter
    still reports the fidelity failure honestly."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.config.memory.dream.span_gate = "contend"
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store(_HOSTILE_NOTE, source="agent")
    out = svc.dream_run(_StubExtractor(
        [_hostile_claim(quote="a span that appears nowhere")]))

    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert len(conts) == 1 and conts[0]["value"] == "db-evil-9"
    assert out["quarantine_parked"] == 1
    assert out["span_flagged"] == 1
    # Parked by the quarantine route, so the span-parked counter stays 0 —
    # counters attribute outcomes to the hook that produced them.
    assert out["span_parked"] == 0


# ── PG-backed: promotion is journaled and reversible (schema v27) ────────

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401,E402  (fixtures)


@pytest.fixture()
def pg_svc(pg_conn, pg_url):  # noqa: F811
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def test_promotion_is_journaled_and_rolls_back(pg_svc):
    """A two-man promotion must be as auditable and revertible as any
    other dream write: journaled under its own action, and — having
    promoted onto an empty slot — reversed like an insert."""
    svc = pg_svc
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-new-1", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-new-1")]))
    svc.store("payments database host db-new-1 confirmed", source="claude")
    out = svc.dream_run(_StubExtractor([_claim("db-new-1")]))
    assert out["quarantine_promoted"] == 1
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-new-1"

    run = svc._storage.latest_committed_dream_run()
    journal = svc._storage.dream_run_journal(run["id"])
    assert [r["action"] for r in journal] == ["quarantine_promoted"]
    assert journal[0]["prev_status"] is None      # promoted onto empty slot

    rb = svc.dream_rollback()
    assert rb["reverted"] >= 1, rb
    assert svc.cortex_lookup("payments-db", "host") is None


def test_park_is_journaled_as_contested_and_rolls_back(pg_svc):
    """The park itself rides the existing contested journal row: rollback
    retires the parked contender."""
    svc = pg_svc
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-new-1", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-new-1")]))
    assert out["quarantine_parked"] == 1

    run = svc._storage.latest_committed_dream_run()
    journal = svc._storage.dream_run_journal(run["id"])
    assert [r["action"] for r in journal] == ["contested"]

    rb = svc.dream_rollback()
    assert rb["reverted"] >= 1, rb
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert conts == []


# ── gate-3 replay classifier (pure; producer evals/quarantine_replay.py) ─

def test_replay_classifier_counts_and_blind_spot():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from quarantine_replay import replay

    rows = [
        # Canonical-effect rows: the only ones that can represent friction.
        {"kind": "scalar", "action": "inserted", "src_entry_id": 1,
         "entry_source": "agent"},                       # would park
        {"kind": "scalar", "action": "superseded", "src_entry_id": 2,
         "entry_source": "claude"},                      # would park
        {"kind": "scalar", "action": "inserted", "src_entry_id": 3,
         "entry_source": "conversation"},                # user tier: no
        {"kind": "scalar", "action": "inserted", "src_entry_id": 4,
         "entry_source": "notes"},                       # no tier: no
        # Unbacked claim: fallback quarantines -> would park, flagged.
        {"kind": "scalar", "action": "inserted", "src_entry_id": None,
         "entry_source": None},
        # Evicted backing entry: honestly unresolvable.
        {"kind": "scalar", "action": "inserted", "src_entry_id": 99,
         "entry_source": None},
        # Confirm/contest rows behave the same under the rule: excluded.
        {"kind": "scalar", "action": "confirmed", "src_entry_id": 7,
         "entry_source": "agent"},
        {"kind": "scalar", "action": "contested", "src_entry_id": 8,
         "entry_source": "agent"},
        # Out of scope kinds.
        {"kind": "member", "action": "member_added", "src_entry_id": 9,
         "entry_source": "agent"},
        {"kind": "event", "action": "event_inserted", "src_entry_id": 9,
         "entry_source": "agent"},
    ]
    out = replay(rows, trusted=set())
    assert out["scalar_rows"] == 8
    assert out["would_park"] == 3          # agent + claude + unbacked
    assert out["no_backing"] == 1
    assert out["unresolvable"] == 1
    assert out["excluded_confirmed"] == 1
    assert out["excluded_contested"] == 1
    assert out["by_source"] == {"agent": 1, "claude": 1}

    trusted = replay(rows, trusted={"agent"})
    assert trusted["would_park"] == 2      # claude + unbacked


# ── review findings (2026-08-09 pass): tier gate + honest counters ───────

def test_two_agent_witnesses_cannot_supersede_a_user_fact(svc):
    """Review BLOCKER: promotion must be tier-gated. On master a
    user-origin fact can only lose its slot to a user-tier write or an
    explicit human resolve; the two-man rule must not weaken that — two
    independent agent witnesses stay parked against a user current."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user",
                     provenance=["seed"])
    svc.store("payments-db host is db-evil-9", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-evil-9")]))
    svc.store("payments-db host now db-evil-9", source="claude")
    out = svc.dream_run(_StubExtractor([_claim("db-evil-9")]))

    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
    assert out.get("quarantine_promoted", 0) == 0
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert [c["value"] for c in conts] == ["db-evil-9"]


def test_low_trust_claim_matching_current_confirms_without_marker(svc):
    """Review SHOULD-FIX: a low-trust claim corroborating the CANONICAL
    value is not a conflict — it confirms, counts as nothing, and must
    not stamp quarantine provenance onto the current record."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user",
                     provenance=["seed"])
    svc.store("payments-db host is db-prod-1", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-prod-1")]))

    cur = svc.cortex_lookup("payments-db", "host")
    assert cur["value"] == "db-prod-1"
    assert "quarantine:low_trust" not in cur["provenance"]
    assert out.get("quarantine_parked", 0) == 0
    assert out.get("quarantine_held", 0) == 0


def test_matching_an_ordinary_contender_does_not_convert_it(svc):
    """Review BLOCKER (secondary route): a low-trust claim whose value
    matches an ORDINARY tier-conflict contender must not stamp the
    quarantine marker onto it — an explicit-resolve-only contender must
    never become witness-promotable by restatement."""
    svc.config.memory.dream.quarantine_low_trust = False
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user",
                     provenance=["seed"])
    # Ordinary park: agent write against user current (tier_downgrade).
    svc.cortex_write("payments-db", "host", "db-alt-2", support="agent",
                     provenance=["othersrc"])
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert conts and "quarantine:low_trust" not in conts[0]["provenance"]

    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("payments-db host is db-alt-2", source="agent")
    out = svc.dream_run(_StubExtractor([_claim("db-alt-2")]))

    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert conts and "quarantine:low_trust" not in conts[0]["provenance"]
    assert out.get("quarantine_promoted", 0) == 0


def test_promotion_onto_occupied_slot_rolls_back(pg_svc):
    """Review coverage gap: the quarantine_promoted reversal's occupied
    branch (_rewrite_prev) — an agent-over-agent promotion superseded a
    current, and rollback restores it."""
    svc = pg_svc
    svc.config.memory.dream.quarantine_low_trust = True
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store("payments-db host is db-next-2", source="agent")
    svc.dream_run(_StubExtractor([_claim("db-next-2")]))
    svc.store("payments database host now db-next-2", source="claude")
    out = svc.dream_run(_StubExtractor([_claim("db-next-2")]))
    assert out["quarantine_promoted"] == 1
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-next-2"

    run = svc._storage.latest_committed_dream_run()
    journal = svc._storage.dream_run_journal(run["id"])
    assert journal[-1]["action"] == "quarantine_promoted"
    assert journal[-1]["prev_value"] == "db-prod-1"

    rb = svc.dream_rollback()
    assert rb["reverted"] >= 1, rb
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
