"""The dream carries the label pair from source entry to derived fact, and
treats a CONSTRAINT source as zero-distortion (schema v35).

- AuthMem-style pairing (arXiv 2608.01679): the SAME claim text, varying
  only the source entry's ``authority``, yields facts whose served
  ``authority`` differs — and a ``quoted`` (third-party) source never
  yields a ``directive`` fact, whatever the extractor wrote.
- A ``quoted`` source is low-trust for the two-man rule: with the
  consolidation quarantine on, its claim parks as a contender instead of
  taking ``current``. The label only ever DEMOTES — the promote path stays
  keyed on entry metadata, never on claim text.
- TypeCompact (arXiv 2608.22752): a claim derived from a CONSTRAINT entry
  carries the entry's text verbatim — the extractor's paraphrase is
  replaced, not trusted. The post-dream guard verifier reports any
  constraint entry in the window that ended up with no verbatim carrier
  (flag, not hard fail: the raw entry is never lost, and a hard fail would
  hold every other claim in the batch hostage to one un-slottable rule).
- Rollback restores the previous version's labels along with its value.
"""
from __future__ import annotations

import logging
import tempfile

import pytest

from pseudolife_memory.service import MemoryService
from tests.dream_helpers import StubExtractor as _StubExtractor
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        yield MemoryService(data_dir=d)


def _claim(value, source_idx=0, entity="payments-db", attribute="host",
           origin="agent"):
    return {"entity": entity, "attribute": attribute, "value": value,
            "confidence": 0.9, "origin": origin, "source": source_idx}


# ── AuthMem pairing ───────────────────────────────────────────────────────

@pytest.mark.parametrize("authority,want", [
    ("quoted", "quoted"), ("directive", "directive"), (None, None),
])
def test_same_claim_different_source_authority_yields_different_facts(
        svc, authority, want):
    svc.store("payments-db host is db-prod-2", source="notes",
              authority=authority)
    res = svc.dream_run(_StubExtractor([_claim("db-prod-2")]))
    assert res["inserted"] == 1
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec.get("authority") == want
    if want is None:
        assert "authority" not in rec


def test_a_quoted_source_never_yields_a_directive_fact(svc):
    """The extractor's value is imperative-shaped; the fact's authority is
    still the SOURCE's — labels come from the entry, never from model
    output (the same trust class as claim ``origin``)."""
    svc.store("the vendor said: always route through db-prod-2",
              source="notes", authority="quoted")
    svc.dream_run(_StubExtractor([
        _claim("Always route through db-prod-2", attribute="routing-rule")]))
    rec = svc.cortex_lookup("payments-db", "routing-rule")
    assert rec["authority"] == "quoted"


def test_quoted_source_is_low_trust_under_the_two_man_rule(svc):
    svc.config.memory.dream.quarantine_low_trust = True
    try:
        svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                         provenance=["seed"])
        svc.store("payments-db host is db-evil-9", source="conversation",
                  authority="quoted")
        out = svc.dream_run(_StubExtractor([_claim("db-evil-9")]))
        assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
        conts = svc.cortex_contenders("payments-db", "host")["contenders"]
        assert [c["value"] for c in conts] == ["db-evil-9"]
        assert conts[0]["authority"] == "quoted"
        assert out["quarantine_parked"] == 1
    finally:
        svc.config.memory.dream.quarantine_low_trust = False


def test_unlabelled_source_inherits_the_slots_label(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     authority="quoted")
    svc.store("payments-db host is db-prod-2", source="notes")
    svc.dream_run(_StubExtractor([_claim("db-prod-2")]))
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["value"] == "db-prod-2"
    assert rec["authority"] == "quoted"


# ── TypeCompact: constraint entries survive verbatim ──────────────────────

RULE = "Never run docker compose down -v against the bank volumes"


def test_constraint_entry_yields_a_verbatim_carrier(svc):
    svc.store(RULE, source="notes")            # auto → constraint
    out = svc.dream_run(_StubExtractor([
        _claim("avoid compose down with volumes", entity="deploy",
               attribute="volume-rule")]))
    rec = svc.cortex_lookup("deploy", "volume-rule")
    assert RULE in rec["value"]
    assert rec["distortion_tolerance"] == "constraint"
    assert out["constraint_verbatim"] == 1
    assert out["constraint_misses"] == []


def test_a_claim_already_verbatim_is_left_alone_and_siblings_are_kept(svc):
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        _claim(f"Deploy rule: {RULE}", entity="deploy", attribute="volume-rule"),
        _claim("external", entity="bank-volumes", attribute="kind"),
    ]))
    carrier = svc.cortex_lookup("deploy", "volume-rule")
    sibling = svc.cortex_lookup("bank-volumes", "kind")
    assert carrier["value"] == f"Deploy rule: {RULE}"
    assert carrier["distortion_tolerance"] == "constraint"
    assert sibling["value"] == "external"
    # A paraphrased sibling of a constraint entry is an observation: the
    # zero-tolerance class is earned only by the verbatim carrier, or a
    # plain fact would be pinned ahead of cosine (review finding).
    assert "distortion_tolerance" not in sibling
    assert out["constraint_verbatim"] == 1
    assert out["constraint_misses"] == []


def test_guard_reports_a_constraint_the_extractor_skipped(svc, caplog):
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    svc.store("the bank volumes are external", source="notes")
    with caplog.at_level(logging.WARNING):
        out = svc.dream_run(_StubExtractor([
            _claim("external", source_idx=1, entity="bank-volumes",
                   attribute="kind")]))
    assert out["constraint_verbatim"] == 0
    assert [m["text"] for m in out["constraint_misses"]] == [RULE]
    assert any("constraint" in r.getMessage().lower() for r in caplog.records)
    # the cursor still advanced — flag, not hard fail
    assert out["cursor"] > 0
    assert svc.dream_status()["backlog"] == 0


def test_guard_reds_when_the_carrier_is_disabled(svc, monkeypatch):
    """The verifier is independent of the carrier: with the carrier off, a
    stub extractor that paraphrases the rule leaves the guard reporting the
    miss — which is also the disablement proof that the carrier is
    load-bearing."""
    from pseudolife_memory.service_dream import DreamOps
    monkeypatch.setattr(DreamOps, "_apply_constraint_carrier",
                        lambda self, pairs, entries: 0)
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        _claim("avoid compose down with volumes", entity="deploy",
               attribute="volume-rule")]))
    assert svc.cortex_lookup("deploy", "volume-rule")["value"] == \
        "avoid compose down with volumes"
    assert out["constraint_verbatim"] == 0
    assert [m["text"] for m in out["constraint_misses"]] == [RULE]


def test_carrier_never_overwrites_an_unrelated_standing_fact(svc):
    """Peer review blocker (2026-09-02): the first cut took the FIRST scalar
    claim citing the entry, whatever slot it targeted, and overwrote its
    value with the rule — a correct standing fact (bank-volumes.kind =
    external) was superseded by the rule text, stamped constraint and
    pinned, while the rule's own slot kept the paraphrase and the guard
    reported a clean pass. A carrier may only land on a slot that is EMPTY
    or already a constraint, chosen by token overlap with the rule."""
    svc.cortex_write("bank-volumes", "kind", "external", support="agent")
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        _claim("external", entity="bank-volumes", attribute="kind"),   # first!
        _claim("avoid compose down with volumes", entity="deploy",
               attribute="volume-rule"),
    ]))
    kind = svc.cortex_lookup("bank-volumes", "kind")
    assert kind["value"] == "external"
    assert "distortion_tolerance" not in kind
    rule = svc.cortex_lookup("deploy", "volume-rule")
    assert RULE in rule["value"]
    assert rule["distortion_tolerance"] == "constraint"
    assert out["constraint_verbatim"] == 1
    assert out["constraint_misses"] == []


def test_carrier_picks_the_claim_that_overlaps_the_rule_not_the_first(svc):
    """Two NEW slots from one constraint entry: the unrelated one comes
    first in extractor order, the paraphrase second. Overlap with the rule
    text decides, not position; the unrelated claim is written as-is."""
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        _claim("alice", entity="deploy", attribute="owner"),
        _claim("avoid compose down with volumes", entity="deploy",
               attribute="volume-rule"),
    ]))
    owner = svc.cortex_lookup("deploy", "owner")
    assert owner["value"] == "alice" and "distortion_tolerance" not in owner
    rule = svc.cortex_lookup("deploy", "volume-rule")
    assert RULE in rule["value"] and rule["distortion_tolerance"] == "constraint"
    assert out["constraint_verbatim"] == 1 and out["constraint_misses"] == []


def test_carrier_refuses_when_no_claim_is_eligible_and_the_guard_reports(svc):
    """The paraphrase targets an occupied, unlabelled slot: rewriting it
    would destroy a standing fact, so the carrier refuses; the claim is
    written normally (agent supersedes agent) and the guard reports the
    miss — flag-not-fail, the same posture as an extractor that emitted
    nothing."""
    svc.cortex_write("deploy", "policy", "backup first", support="agent")
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        _claim("avoid compose down with volumes", entity="deploy",
               attribute="policy")]))
    pol = svc.cortex_lookup("deploy", "policy")
    assert pol["value"] == "avoid compose down with volumes"
    assert "distortion_tolerance" not in pol
    assert out["constraint_verbatim"] == 0
    assert [m["text"] for m in out["constraint_misses"]] == [RULE]


def test_carrier_may_update_a_slot_that_is_already_a_constraint(svc):
    """A re-extraction of an amended rule lands on the rule's existing
    constraint slot (occupied, but by a constraint) — that is the one
    occupied case the carrier may write into."""
    svc.cortex_write("deploy", "volume-rule", "Never run compose down -v",
                     support="agent", distortion_tolerance="constraint")
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        _claim("avoid compose down with volumes", entity="deploy",
               attribute="volume-rule")]))
    rule = svc.cortex_lookup("deploy", "volume-rule")
    assert RULE in rule["value"] and rule["distortion_tolerance"] == "constraint"
    assert out["constraint_verbatim"] == 1


def test_member_ops_are_never_carriers(svc):
    """v1 scope: only a scalar claim can carry a constraint verbatim; a
    constraint entry whose only claim is a set op is a reported miss, not
    a rewritten member."""
    svc.store(RULE, source="notes", distortion_tolerance="constraint")
    out = svc.dream_run(_StubExtractor([
        {**_claim("compose down -v", entity="deploy", attribute="banned-ops"),
         "op": "add"}]))
    members = svc.cortex_lookup("deploy", "banned-ops")["members"]
    assert [m["value"] for m in members] == ["compose down -v"]
    assert out["constraint_verbatim"] == 0
    assert len(out["constraint_misses"]) == 1


def test_empty_pull_reports_a_stable_shape(svc):
    out = svc.dream_run(_StubExtractor([]))
    assert out["constraint_verbatim"] == 0
    assert out["constraint_misses"] == []


# ── rollback ──────────────────────────────────────────────────────────────

def test_rollback_restores_the_previous_labels(pg_conn, pg_url, tmp_path):  # noqa: F811
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.store("payments-db host is db-prod-2", source="notes",
              authority="quoted", distortion_tolerance="belief")
    svc.dream_run(_StubExtractor([_claim("db-prod-2")]))
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["value"] == "db-prod-2" and rec["authority"] == "quoted"
    res = svc.dream_rollback()
    assert res["reverted"] >= 1
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["value"] == "db-prod-1"
    assert "authority" not in rec and "distortion_tolerance" not in rec
