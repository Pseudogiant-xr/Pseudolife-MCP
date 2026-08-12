"""Composition smoke — span fidelity must not launder trust (gate 5 of the
2026-08-12 stance+span-gate design).

The span gate checks fidelity-to-source; the consolidation quarantine
(two-man rule, spec 2026-08-09) checks trustworthiness-of-source. They are
orthogonal by design, and the dangerous composition error would be
treating a PASSING span check as evidence of trust: a poisoned note
quotes itself perfectly (the MAFIA-class attacker controls the note text,
so fidelity is free for them). These tests pin the boundary:

1. quarantine ON + span gate contend: a hostile agent-origin claim with a
   PERFECT self-quote still parks under the two-man rule — span passing
   must not take current, must not promote, must not count as a witness.
2. The load-bearing-hook check for the same scenario: with quarantine OFF
   the identical perfect-quote claim supersedes (today's same-tier
   newer-wins contract) — proving test 1's protection comes from the
   quarantine hook, not accidentally from the span gate.
3. Both failures at once (low trust AND unbacked quote) park exactly once,
   via the quarantine route, with honest counters.
"""
from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.service import MemoryService


class _StubExtractor:
    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab):
        return [dict(c) for c in self._claims]


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d)
        yield s


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
