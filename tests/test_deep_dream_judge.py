"""Autonomous Step-C judge (2026-08-16 design): the sweep shadow-judges
pending merge proposals with the configured model; auto-apply is gated by
``deep_dream.judge_mode`` and confidence. Contracts:

* shadow mode records verdicts on the rows and applies NOTHING;
* auto-reject mode applies only reject verdicts at/above the confidence
  floor (``decided_by='dream-judge'`` in merge_decisions, pair dismissed);
  accept verdicts are never applied by the judge at any mode;
* already-judged proposals are not re-sent; a judge failure never raises.

PG-backed (skips without the bench server).
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    with s._lock:
        s._ensure_init()
    yield s
    s.flush()


class _StubJudge:
    """Fixed verdict per (from, into) display pair; records what it saw."""

    model = "stub-judge"

    def __init__(self, verdicts):
        # {(from, into): (verdict, conf[, relation])}
        self._verdicts = verdicts
        self.seen: list[tuple[str, str]] = []

    def judge_merges(self, proposals):
        out = []
        for p in proposals:
            key = (p["from"]["display"], p["into"]["display"])
            self.seen.append(key)
            v = self._verdicts.get(key)
            if v is not None:
                out.append({"n": p["n"], "verdict": v[0],
                            "confidence": v[1], "note": "stub",
                            "relation": v[2] if len(v) > 2 else None})
        return out


def _propose(svc, frm, into):
    import time
    st = svc._storage
    st.ensure_entity(frm, display=frm)
    st.ensure_entity(into, display=into)
    a = st.find_entity(frm)["id"]
    b = st.find_entity(into)["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "test", time.time())
    assert pid is not None
    return pid


def _row(svc, pid):
    return next((p for p in svc._storage.pending_entity_proposals()
                 if p["id"] == pid), None)


# ── the storage-level gate under the sweep (schema v30) ──────────────────

def test_judgment_round_trips_and_gates_on_pending(pg_conn, pg_url):  # noqa: F811
    """The verdict is an OPINION recorded on a PENDING row. Once a decision
    path ratifies the row, the verdict freezes with it — a later judge call
    must be refused rather than rewriting the history of a decided merge."""
    import time

    from pseudolife_memory.storage.postgres import PostgresStorage

    st = PostgresStorage(pg_url)
    st.ensure_entity("alpha", display="alpha")
    st.ensure_entity("alpha service", display="alpha service")
    a = st.find_entity("alpha")["id"]
    b = st.find_entity("alpha service")["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.9, "test", time.time())
    assert st.set_entity_proposal_judgment(
        pid, verdict="reject", confidence=0.9, note="siblings",
        model="stub", at=time.time())
    row = next(p for p in st.pending_entity_proposals() if p["id"] == pid)
    assert row["judge_verdict"] == "reject"
    assert row["judge_confidence"] == 0.9
    assert row["judge_note"] == "siblings"
    # A decided row can no longer be re-judged (the verdict froze with it).
    st.set_entity_proposal_status(pid, "rejected")
    assert not st.set_entity_proposal_judgment(
        pid, verdict="accept", confidence=0.5, note=None, model="stub",
        at=time.time())


# ── judge modes ──────────────────────────────────────────────────────────

def test_shadow_mode_records_and_applies_nothing(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "alpha svc", "alpha service")
    judge = _StubJudge({("alpha svc", "alpha service"): ("reject", 0.95)})
    out = svc.deep_dream_judge(judge)
    assert out["judged"] == 1 and out.get("auto_rejected", 0) == 0
    row = _row(svc, pid)
    assert row is not None and row["status"] == "pending"    # still queued
    assert row["judge_verdict"] == "reject"
    assert row["judge_model"] == "stub-judge"


def test_rows_sharing_an_endpoint_beyond_the_batch_still_record(svc):
    """A row's review fingerprint must not depend on which other rows share
    its batch. refresh() signed the whole pending queue and validate() only
    the judged batch, and the evidence pack's ``group`` (the endpoint a row
    shares with OTHER pending rows) came out set over the queue and None
    over a batch that left the sibling out, so the row never validated. On
    the live bank the shadow judge re-sent the same 8 rows ~125 times a day
    from 2026-09-22 17:56 on and recorded nothing."""
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pids = [_propose(svc, "alpha svc", "alpha service"),
            _propose(svc, "alpha srv", "alpha service")]
    judge = _StubJudge({("alpha svc", "alpha service"): ("reject", 0.9),
                        ("alpha srv", "alpha service"): ("reject", 0.9)})
    out = svc.deep_dream_judge(judge, limit=1)
    assert out["judged"] == 1
    assert sum(bool(_row(svc, pid)["judge_verdict"]) for pid in pids) == 1


def test_second_opinion_on_a_split_group_still_records(svc):
    """The second-opinion batch validates through the same fingerprint, so
    it wedged the same way once a group's rows were split across batches."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = True
    pids = [_propose(svc, "alpha svc", "alpha service"),
            _propose(svc, "alpha srv", "alpha service")]
    judge = _StubJudge({("alpha svc", "alpha service"): ("reject", 0.9),
                        ("alpha srv", "alpha service"): ("reject", 0.9)})
    assert svc.deep_dream_judge(judge, limit=2)["judged"] == 2
    out = svc.deep_dream_judge(judge, limit=1, second_extractor=judge)
    assert out["second_opinions"] == 1
    assert sum(bool(_row(svc, pid)["judge2_verdict"]) for pid in pids) == 1


def test_review_signatures_do_not_depend_on_batch_composition(svc):
    """Every review queue signs a row twice, among different rows: with the
    rows prepare() picked for refresh(), and with the judged batch at
    validate(). An evidence field
    computed across rows makes the two disagree whenever the batch leaves
    out a row it relates to, and that row then never records (the merge
    ``group`` did, 2026-09-22). Pinned for every ReviewJudgments kind, with
    rows that share endpoints."""
    import time

    from pseudolife_memory.memory.review_judgments import ReviewJudgments
    st = svc._storage
    _propose(svc, "alpha svc", "alpha service")
    _propose(svc, "alpha srv", "alpha service")
    _propose(svc, "beta svc", "beta service")
    for dst in ("beta service", "gamma queue"):
        assert svc.graph_propose_links([{
            "src": "alpha service", "relation": "related-to", "dst": dst,
            "rationale": "t"}])["proposed"] == 1
    for name in ("junk one", "junk two"):
        st.ensure_entity(name, display=name)
        assert st.insert_entity_proposal(
            "junk", st.find_entity(name)["id"], None, None, "list-artifact",
            time.time()) is not None
    entity_rows = st.pending_entity_proposals()
    queues = {
        "merge": [p for p in entity_rows if p.get("kind") == "merge"],
        "link": st.pending_proposals(),
        "junk": [p for p in entity_rows if p.get("kind") == "junk"],
    }
    for kind, pending in queues.items():
        assert len(pending) >= 2, kind
        review = ReviewJudgments(svc, kind, _StubJudge({}), pending)
        with svc._lock:
            whole = review.signatures(pending)
            for p in pending:
                key = str(p["id"])
                assert review.signatures([p]) == {key: whole[key]}, (kind, key)


def test_merge_pack_matches_the_whole_graph_computation(svc):
    """Deploy continuity (2026-09-23). The merge pack now resolves mentions
    for the rows' own entities, from entry texts without embeddings. It must
    equal the old computation over every graph entity from full entries:
    any drift changes every fingerprint, so the first tick after a deploy
    would clear every recorded verdict and reopen every automatic reject.
    Covers trace-backed mentions, the token fallback, an entity below
    min_entity_mentions and one over max_fallback_mentions, and the one
    case where the mentions pass changes the pack at all: an entity with
    more fallback notes than the pack's own 12-note scan reaches."""
    import time

    import numpy as np

    from pseudolife_memory.memory import graph_consolidation as gc
    cfg = svc.config.memory.deep_dream
    cfg.max_fallback_mentions = 20
    st = svc._storage

    def note(text):
        return st.insert_entry({"band": "flat", "text": text, "source": "t",
                                "embedding": np.zeros(1024, dtype=np.float32),
                                "surprise": 0.5, "ts": time.time(),
                                "access_count": 0})
    # 14 notes name 'alpha service'; only the 14th also names 'alpha svc'.
    # The mentions pass sees all 14, so the 'alpha svc' side's evidence sits
    # inside the other side's (low_differential); the 12-note scan misses it.
    for i in range(13):
        note(f"alpha service note {i}")
    note("alpha svc is the alpha service renamed")
    for i in range(21):                  # over max_fallback_mentions
        note(f"beta hub fact {i}")
    traced = [note("beta svc deploys from main"), note("beta svc pages on failure")]
    note("filler 0 and filler 1 share a note")
    _propose(svc, "alpha svc", "alpha service")
    _propose(svc, "beta svc", "beta hub")
    _propose(svc, "gamma one", "gamma two")
    for i in range(6):
        st.ensure_entity(f"filler {i}", display=f"filler {i}")
    canonical = st.find_entity("beta svc")["canonical"]
    for entry_id in traced:
        st.add_trace(canonical, "role", entry_id, time.time())
    rows = [p for p in st.pending_entity_proposals() if p.get("kind") == "merge"]
    with svc._lock:
        g = st.load_graph()
        scopes, traces = st.entity_sources_map(), st.traces_by_entity_norm()
        entries, facts = st.load_entries(), st.entity_fact_counts()
        evidence = svc._judge_evidence_locked(rows)
    _, mentions = gc.entity_context_vectors(
        g["entities"], entries, traces, min_mentions=cfg.min_entity_mentions,
        max_fallback_mentions=cfg.max_fallback_mentions or None)
    reference = svc._enrich_merge_proposals(
        rows, g["entities"], g["edges"], entries, traces, mentions, scopes,
        cfg.max_context_snippets, cfg.judge_snippet_max_chars, True,
        fact_counts=facts)
    alpha = next(r for r in reference
                 if {r["from"]["display"], r["into"]["display"]}
                 == {"alpha svc", "alpha service"})
    assert alpha["from"]["snippets"] and alpha["into"]["snippets"]
    assert alpha["low_differential"], "the scenario must reach the 14th note"
    assert svc._judge_enrich_from(rows, evidence) == reference
    for row, expected in zip(rows, reference):
        assert svc._judge_enrich_from([row], evidence) == [expected]


def test_split_group_auto_rejects_are_not_reopened(svc):
    """Reconsideration re-signs automatic terminal decisions over yet another
    row set. With the cross-row ``group`` signed, two auto-rejected rows
    sharing an endpoint read as changed whenever they were reconsidered
    apart, and the reject was reopened, deleting its dismissed pair."""
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    pids = [_propose(svc, "delta svc", "delta service"),
            _propose(svc, "delta srv", "delta service")]
    judge = _StubJudge({("delta svc", "delta service"): ("reject", 0.99),
                        ("delta srv", "delta service"): ("reject", 0.99)})
    assert svc.deep_dream_judge(judge, limit=2)["auto_rejected"] == 2
    for _ in range(3):
        out = svc.deep_dream_judge(_StubJudge({}), limit=1)
        assert out["reconsideration"]["reopened"] == 0, out
    assert all(svc._storage.get_entity_proposal(pid)["status"] == "rejected"
               for pid in pids)


def test_ungrouped_auto_reject_survives_the_fingerprint_fix(svc, monkeypatch):
    """The fix pins ``group`` to None instead of dropping it, so a row that
    never shared an endpoint keeps the fingerprint it was signed with
    before 2026-09-23: its automatic reject stays settled across the deploy
    instead of reopening. Only rows that did carry a group re-sign once."""
    from pseudolife_memory.memory.review_judgments import ReviewJudgments
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    pid = _propose(svc, "beta svc", "beta service")
    judge = _StubJudge({("beta svc", "beta service"): ("reject", 0.99)})
    monkeypatch.setattr(ReviewJudgments, "_CROSS_ROW", {})   # pre-fix signing
    assert svc.deep_dream_judge(judge)["auto_rejected"] == 1
    monkeypatch.undo()
    out = svc.deep_dream_judge(_StubJudge({}))
    assert out["reconsideration"]["reopened"] == 0, out
    assert svc._storage.get_entity_proposal(pid)["status"] == "rejected"


def test_ungrouped_shadow_verdict_survives_the_fingerprint_fix(svc, monkeypatch):
    """Same continuity for a recorded shadow verdict: an ungrouped row is not
    cleared and re-judged by the first sweep after the deploy."""
    from pseudolife_memory.memory.review_judgments import ReviewJudgments
    svc.config.memory.deep_dream.judge_mode = "shadow"
    svc.config.memory.deep_dream.judge_second_opinion = False
    pid = _propose(svc, "beta svc", "beta service")
    judge = _StubJudge({("beta svc", "beta service"): ("reject", 0.9)})
    monkeypatch.setattr(ReviewJudgments, "_CROSS_ROW", {})   # pre-fix signing
    assert svc.deep_dream_judge(judge)["judged"] == 1
    monkeypatch.undo()
    rejudge = _StubJudge({})
    svc.deep_dream_judge(rejudge)
    assert rejudge.seen == []
    assert _row(svc, pid)["judge_verdict"] == "reject"


def test_judge_logs_batch_start(svc, caplog):
    """The judge must announce a batch BEFORE calling the model, not only
    log the completed verdicts (2026-09-01). The 2026-08-31 hook-timeout
    forensics misplaced a ~50s incident window inside the judge because
    the completion line was the only trace the tick left — a start line
    brackets the long lock-free LLM wait in the ledger."""
    import logging

    svc.config.memory.deep_dream.judge_mode = "shadow"
    _propose(svc, "beta svc", "beta service")
    judge = _StubJudge({("beta svc", "beta service"): ("leave", 0.5)})
    with caplog.at_level(logging.INFO):
        out = svc.deep_dream_judge(judge)
    assert out["judged"] == 1
    starts = [r.message for r in caplog.records
              if "judging" in r.message and "1" in r.message]
    assert starts, "the judge must log the batch size before the model call"


def test_auto_reject_applies_only_confident_rejects(svc):
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    svc.config.memory.deep_dream.judge_reject_min_confidence = 0.8
    hi = _propose(svc, "beta svc", "beta harness")
    lo = _propose(svc, "gamma svc", "gamma harness")
    acc = _propose(svc, "delta svc", "delta service")
    judge = _StubJudge({
        ("beta svc", "beta harness"): ("reject", 0.9),      # applies
        ("gamma svc", "gamma harness"): ("reject", 0.5),    # below floor
        ("delta svc", "delta service"): ("accept", 0.99),   # never applied
    })
    out = svc.deep_dream_judge(judge)
    assert out["judged"] == 3 and out["auto_rejected"] == 1
    assert _row(svc, hi) is None                             # rejected, gone
    assert _row(svc, lo)["status"] == "pending"
    assert _row(svc, acc)["status"] == "pending"             # accept = opinion
    # The applied reject is a durable dream-judge decision + dismissed pair.
    decisions = svc._storage.recent_entity_decisions(limit=10)
    assert any(d["decided_by"] == "dream-judge"
               and d["status"] == "rejected" for d in decisions)
    assert ("beta harness", "beta svc") in {
        tuple(sorted(p)) for p in svc._storage.dismissed_pairs()}


def test_judged_rows_are_not_resent(svc):
    # First-opinion idempotence: with the 2026-09-02 second opinion on, a
    # judged row IS re-sent exactly once (test_queue_judges_service covers
    # that contract); this test pins the single-opinion path.
    svc.config.memory.deep_dream.judge_second_opinion = False
    svc.config.memory.deep_dream.judge_mode = "shadow"
    _propose(svc, "eps svc", "eps service")
    judge = _StubJudge({("eps svc", "eps service"): ("leave", 0.4)})
    assert svc.deep_dream_judge(judge)["judged"] == 1
    again = _StubJudge({})
    assert svc.deep_dream_judge(again)["judged"] == 0
    assert again.seen == []                                  # nothing re-sent


def test_skipped_rows_become_zero_confidence_leaves(svc):
    # First-opinion idempotence: with the 2026-09-02 second opinion on, a
    # judged row IS re-sent exactly once (test_queue_judges_service covers
    # that contract); this test pins the single-opinion path.
    svc.config.memory.deep_dream.judge_second_opinion = False
    # A model that returns no verdict for a row must not cause that row to
    # be re-sent every sweep (queue-head starvation): it is recorded as an
    # explicit abstain instead.
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "iota svc", "iota service")
    judge = _StubJudge({})                       # returns nothing for the row
    assert svc.deep_dream_judge(judge)["judged"] == 1
    row = _row(svc, pid)
    assert row["judge_verdict"] == "leave"
    assert row["judge_confidence"] == 0.0
    again = _StubJudge({})
    assert svc.deep_dream_judge(again)["judged"] == 0
    assert again.seen == []                      # not re-sent


def test_judge_failure_never_raises(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    _propose(svc, "zeta svc", "zeta service")

    class _Boom:
        model = "boom"

        def judge_merges(self, proposals):
            raise RuntimeError("endpoint down")

    out = svc.deep_dream_judge(_Boom())
    assert out["judged"] == 0 and "error" in out


def test_off_mode_is_inert(svc):
    svc.config.memory.deep_dream.judge_mode = "off"
    _propose(svc, "eta svc", "eta service")
    judge = _StubJudge({("eta svc", "eta service"): ("reject", 0.99)})
    assert svc.deep_dream_judge(judge) == {"judged": 0, "skipped": "disabled"}
    assert judge.seen == []


def test_merge_candidates_listing_carries_the_shadow_verdict():
    # The Console's review payload is built by graph_review.merge_candidates
    # — the judge block must survive it, or the human reviewer never sees
    # the pre-judgment (found post-deploy on 2026-08-17: the verdict lived
    # only in the deep response).
    from pseudolife_memory.memory.graph_review import merge_candidates

    rows = [{"id": 7, "kind": "merge", "entity_id": 1, "into_id": 2,
             "entity": "alpha svc", "into": "alpha service", "score": 0.9,
             "reason": "write-dedup", "judge_verdict": "reject",
             "judge_confidence": 0.92, "judge_note": "siblings",
             "judge_model": "claude-opus-5"},
            {"id": 8, "kind": "merge", "entity_id": 3, "into_id": 4,
             "entity": "beta", "into": "beta svc", "score": 0.9,
             "reason": "write-dedup"}]
    merges = merge_candidates(rows)[0]["merges"]
    judged = next(m for m in merges if m["id"] == 7)
    assert judged["judge"] == {"verdict": "reject", "confidence": 0.92,
                               "note": "siblings", "model": "claude-opus-5"}
    assert "judge" not in next(m for m in merges if m["id"] == 8)


def test_review_payload_carries_the_shadow_verdict(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "theta svc", "theta service")
    judge = _StubJudge({("theta svc", "theta service"): ("reject", 0.85)})
    svc.deep_dream_judge(judge)
    deep = svc.deep_dream(apply=False)
    row = next(p for p in deep["merge_proposals"] if p["id"] == pid)
    assert row["judge"]["verdict"] == "reject"
    assert row["judge"]["model"] == "stub-judge"


# ── evidence-quality signal in the judge payload (2026-08-21 shadow) ─────

def test_format_judge_proposal_default_is_byte_identical_and_snippet_chars_lifts_it():
    """Absent key -> the frozen 240-char serialization (every published
    judge number keeps its exact prompt); ``snippet_chars`` on the proposal
    lifts the cap (0 = unbounded) so the sweep's judge reads full evidence."""
    from pseudolife_memory.memory.dream import format_judge_proposal
    long = "x" * 600
    base = {"n": 1, "from": {"display": "a", "snippets": [long]},
            "into": {"display": "b", "snippets": [long]}, "reason": "t"}
    plain = format_judge_proposal(dict(base))
    assert "x" * 240 in plain and "x" * 241 not in plain
    assert "snippet_chars" not in plain
    full = format_judge_proposal({**base, "snippet_chars": 0})
    assert "x" * 600 in full
    capped = format_judge_proposal({**base, "snippet_chars": 300})
    assert "x" * 300 in capped and "x" * 301 not in capped
    assert format_judge_proposal({**base, "snippet_chars": 240}) == plain


def test_judge_reads_full_length_evidence(svc):
    """The 2026-09-02 panel judged merge snippets clipped to 240 chars at
    BUILD time (305/309 were exactly 240) — the judge path builds its
    evidence at ``judge_snippet_max_chars`` and stamps the cap on each
    proposal; the dry-run/Console listing keeps ``snippet_max_chars``."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    # The default stays the frozen 240 (the 2026-09-03 ladder measured
    # 3000 as worse on the auto-fold path); the knob is exercised here.
    assert cfg.judge_snippet_max_chars == 240
    cfg.judge_snippet_max_chars = 3000
    body = " ".join(f"detail{i}" for i in range(80))          # > 240 chars
    svc.store(f"alpha svc handles the alpha path. {body}", source="t")
    svc.store(f"alpha service is the alpha daemon. {body}", source="t")
    pid = _propose(svc, "alpha svc", "alpha service")

    class _Capture:
        model = "stub"
        proposals = []

        def judge_merges(self, proposals):
            self.proposals.extend(proposals)
            return [{"n": p["n"], "verdict": "leave", "confidence": 0.5,
                     "note": ""} for p in proposals]

    judge = _Capture()
    assert svc.deep_dream_judge(judge)["judged"] == 1
    p = judge.proposals[0]
    assert p["snippet_chars"] == cfg.judge_snippet_max_chars == 3000
    snips = p["from"]["snippets"] + p["into"]["snippets"]
    assert snips and max(len(x) for x in snips) > 240
    # the review surface keeps its own (shorter) cap
    cfg.snippet_max_chars = 40
    listed = next(m for m in svc.deep_dream(apply=False)["merge_proposals"]
                  if m["id"] == pid)
    shown = listed["from"]["snippets"] + listed["into"]["snippets"]
    assert shown and all(len(x) <= 40 for x in shown)


def test_format_judge_proposal_marks_low_differential():
    from pseudolife_memory.memory.dream import format_judge_proposal

    base = {"n": 1, "reason": "token-subset", "score": 0.9,
            "from": {"display": "a", "degree": 0, "scopes": [],
                     "snippets": ["shared evidence line"]},
            "into": {"display": "a svc", "degree": 1, "scopes": [],
                     "snippets": ["shared evidence line"]}}
    plain = format_judge_proposal(dict(base))
    flagged = format_judge_proposal({**base, "low_differential": True})
    assert "low-differential" in flagged.lower()
    # Absent key serializes exactly as before — the frozen ladder fixtures
    # (and every published judge number) keep their byte-identical prompts.
    assert "low-differential" not in plain.lower()
    assert flagged != plain


def test_judge_payload_carries_low_differential_flag(svc):
    class _RecordingJudge:
        model = "stub-judge"

        def __init__(self):
            self.proposals = []

        def judge_merges(self, proposals):
            self.proposals = proposals
            return [{"n": p["n"], "verdict": "leave", "confidence": 0.1,
                     "note": "stub"} for p in proposals]

    # Only shared evidence exists for this pair -> the flag must reach the
    # judge payload so the prompt can carry the caution line.
    assert svc.store("beta gadget service exports the metrics feed",
                     source="sq")["stored"]
    assert svc.store("the beta gadget service restarts after deploys",
                     source="sq")["stored"]
    _propose(svc, "beta gadget", "beta gadget service")
    svc.config.memory.deep_dream.judge_mode = "shadow"
    judge = _RecordingJudge()
    out = svc.deep_dream_judge(extractor=judge)
    assert out["judged"] == 1
    assert judge.proposals[0]["low_differential"] is True


# ── the "relate" verdict (2026-09-30) ─────────────────────────────────────
# Most merge rejects in the 2026-09-29 triage (795 of 1,016 proposals) were
# RELATED-but-not-same pairs, and a reject dropped the relationship. The
# judge's reject may now name a link-judge vocabulary relation, FROM as src
# and INTO as dst, recorded as the internal "relate" verdict: reject-class
# for the merge, and a link proposal for the link judge once the reject is
# applied.

def _judge_merges_returning(verdicts):
    """Run the shipped parser over a canned model response."""
    import json
    from unittest import mock

    from pseudolife_memory.memory import dream as D

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            content = json.dumps({"verdicts": verdicts})
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    proposals = [{"n": i + 1, "from": {"display": f"a{i}"},
                  "into": {"display": f"b{i}"}, "reason": "t"}
                 for i in range(len(verdicts))]
    with mock.patch("pseudolife_memory.utils.no_redirect.urlopen",
                    lambda req, timeout=None: _Resp()):
        return D.OpenAICompatExtractor("http://x/v1", "m").judge_merges(proposals)


def test_judge_merges_reads_a_reject_relation_as_relate():
    """The prompt asks for reject plus an optional relation (2026-09-30
    redesign): the reject's confidence is about distinctness alone, and the
    relation only annotates it. The parser folds a reject carrying a
    vocabulary relation into the internal ``relate`` verdict."""
    out = _judge_merges_returning([
        {"id": 1, "verdict": "reject", "relation": "part-of", "confidence": 0.9, "note": "n1"},
        {"id": 2, "verdict": "REJECT", "relation": "Stores Data In", "confidence": 0.8, "note": "n2"},
        {"id": 3, "verdict": "reject", "relation": "sibling-of", "confidence": 0.9, "note": "n3"},
        {"id": 4, "verdict": "reject", "relation": None, "confidence": 0.9, "note": "n4"},
        {"id": 5, "verdict": "reject", "confidence": 0.9, "note": "n5"},
        {"id": 6, "verdict": "accept", "relation": "uses", "confidence": 0.7, "note": "n6"},
        {"id": 7, "verdict": "leave", "relation": "uses", "confidence": 0.4, "note": "n7"},
    ])
    by_n = {v["n"]: v for v in out}
    assert (by_n[1]["verdict"], by_n[1]["relation"]) == ("relate", "part-of")
    assert (by_n[2]["verdict"], by_n[2]["relation"]) == ("relate", "stores-data-in")
    assert by_n[1]["confidence"] == 0.9
    # A relation outside the vocabulary, or none at all, is a plain reject:
    # nothing can be filed as a link, and the pair is still distinct.
    for n in (3, 4, 5):
        assert (by_n[n]["verdict"], by_n[n]["relation"]) == ("reject", None)
        assert by_n[n]["confidence"] == 0.9
    # Only a reject carries a relation.
    assert (by_n[6]["verdict"], by_n[6]["relation"]) == ("accept", None)
    assert (by_n[7]["verdict"], by_n[7]["relation"]) == ("leave", None)


def test_judge_merges_still_reads_an_explicit_relate():
    """A model that answers the #484 draft's ``relate`` verdict is read the
    same way, so a stale prompt cache or a hand-written verdict still works."""
    out = _judge_merges_returning([
        {"id": 1, "verdict": "relate", "relation": "implements", "confidence": 0.9, "note": "n1"},
        {"id": 2, "verdict": "relate", "relation": "sibling-of", "confidence": 0.8, "note": "n2"},
    ])
    by_n = {v["n"]: v for v in out}
    assert (by_n[1]["verdict"], by_n[1]["relation"]) == ("relate", "implements")
    assert (by_n[2]["verdict"], by_n[2]["relation"]) == ("reject", None)


def test_relation_names_are_the_link_judge_vocabulary():
    from pseudolife_memory.memory import dream as D
    assert D._RELATION_NAMES == (
        "depends-on", "part-of", "runs-on", "hosts", "uses", "configures",
        "stores-data-in", "tests", "implements", "superseded-by", "related-to")
    # The merge prompt offers the same vocabulary, as an optional annotation
    # of a reject, not as a verdict of its own: the 2026-09-30 ladder of a
    # separate "relate" verdict measured its confidence at 0.58 on average
    # (0 of 85 votes at the 0.8 single-vote gate), which starved the
    # automatic reject gates.
    assert D._RELATION_VOCAB in D._JUDGE_SYSTEM_PROMPT
    assert '"verdict":"accept"|"reject"|"leave"' in D._JUDGE_SYSTEM_PROMPT
    assert '"relate"' not in D._JUDGE_SYSTEM_PROMPT


def test_relate_note_round_trips_both_opinions():
    from pseudolife_memory.memory.graph_review import (
        merge_verdict_token, relate_note, relate_relations)
    first = relate_note("implements", "code for the concept")
    assert first == "relate:implements | code for the concept"
    assert relate_note("uses", None) == "relate:uses"
    combined = (f"{first[:160]} | 2nd (model (x)): "
                f"{merge_verdict_token('relate', 'part-of')} 0.85 [agree]")
    assert relate_relations(combined) == ("implements", "part-of")
    assert relate_relations("plain note | 2nd (m): reject 0.90 [agree]") == (None, None)
    assert relate_relations(None) == (None, None)
    assert merge_verdict_token("reject", None) == "reject"


def _note(svc, pid):
    return svc._storage.conn.execute(
        "SELECT judge_note FROM entity_proposals WHERE id=%s", (pid,)).fetchone()[0]


def _pending_links(svc):
    return [(p["src"], p["relation"], p["dst"], p["source"])
            for p in svc._storage.pending_proposals()]


def test_single_vote_relate_rejects_the_merge_and_files_a_link(svc):
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    svc.config.memory.deep_dream.judge_reject_min_confidence = 0.8
    pid = _propose(svc, "kappa module", "kappa concept")
    low = _propose(svc, "lambda module", "lambda concept")
    judge = _StubJudge({
        ("kappa module", "kappa concept"): ("relate", 0.9, "implements"),
        ("lambda module", "lambda concept"): ("relate", 0.5, "implements"),
    })
    out = svc.deep_dream_judge(judge)
    assert out["judged"] == 2 and out["auto_rejected"] == 1
    assert out["relate_links_filed"] == 1
    decided = svc._storage.get_entity_proposal(pid)
    assert decided["status"] == "rejected" and decided["decided_by"] == "dream-judge"
    assert decided["judge_verdict"] == "relate"
    assert _note(svc, pid).startswith("relate:implements")
    # The link goes to the link judge's queue, never straight to an edge.
    assert _pending_links(svc) == [
        ("kappa module", "implements", "kappa concept", "merge-judge-relate")]
    assert not svc._storage.load_graph()["edges"]
    # Filed by entity id: these canonicals ("kappa module") are not what
    # norm_name(display) gives, so a by-name filing would mint new nodes.
    (link,) = svc._storage.pending_proposals()
    merge = svc._storage.get_entity_proposal(pid)
    assert (link["src_id"], link["dst_id"]) == (merge["entity_id"], merge["into_id"])
    # Below the gate: an opinion only, nothing filed.
    assert _row(svc, low)["status"] == "pending"
    assert _row(svc, low)["judge_verdict"] == "relate"


def test_shadow_relate_records_and_files_nothing(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "mu module", "mu concept")
    judge = _StubJudge({("mu module", "mu concept"): ("relate", 0.95, "implements")})
    out = svc.deep_dream_judge(judge)
    assert out["judged"] == 1 and out["auto_rejected"] == 0
    assert out["relate_links_filed"] == 0
    assert _row(svc, pid)["judge_verdict"] == "relate"
    assert _pending_links(svc) == []
    # The reviewer sees the relation beside the verdict.
    deep = svc.deep_dream(apply=False)
    row = next(p for p in deep["merge_proposals"] if p["id"] == pid)
    assert row["judge"]["verdict"] == "relate"
    assert row["judge"]["relation"] == "implements"


def test_both_review_payloads_show_the_relation_in_the_judges_order(svc):
    """The relation reads FROM -> INTO as the judge was shown the pair, and
    the pack re-derives that direction from current evidence. Both review
    payloads must list the pair the same way, or the Console row would
    read "A -> B · relate implements" with the relation backwards:
    ``graph_review`` re-orients merge rows by the same rule before
    ``merge_candidates`` builds them (the store keeps the old order)."""
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "pi concept", "pi module")        # stored pi concept -> pi module
    st = svc._storage
    concept = st.find_entity("pi concept")["id"]
    for other in ("pi doc one", "pi doc two"):
        st.ensure_entity(other, display=other)
        svc._graph.upsert_edge(concept, "related-to", st.find_entity(other)["id"],
                               confidence=0.7, origin="action")
    judge = _StubJudge({("pi module", "pi concept"): ("relate", 0.95, "implements")})
    svc.deep_dream_judge(judge)
    assert judge.seen == [("pi module", "pi concept")]   # shown flipped

    queue = next(f for f in svc.graph_review()["findings"]
                 if f["type"] == "merge_candidate")
    listed = next(m for m in queue["merges"] if m["id"] == pid)
    assert (listed["from"], listed["into"]) == ("pi module", "pi concept")
    assert listed["judge"]["verdict"] == "relate"
    assert listed["judge"]["relation"] == "implements"

    deep = svc.deep_dream(apply=False)
    row = next(p for p in deep["merge_proposals"] if p["id"] == pid)
    assert (row["from"]["display"], row["into"]["display"]) == ("pi module", "pi concept")
    assert row["judge"]["relation"] == "implements"


def test_relate_link_follows_the_orientation_the_judge_saw(svc):
    """The pack re-derives fold direction from current evidence: a stored
    FROM with more edges than its INTO is SHOWN as the INTO. The relation
    reads FROM -> INTO as shown, so the link must be filed that way."""
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    pid = _propose(svc, "nu concept", "nu module")        # stored nu concept -> nu module
    st = svc._storage
    concept = st.find_entity("nu concept")["id"]
    for other in ("nu doc one", "nu doc two"):
        st.ensure_entity(other, display=other)
        svc._graph.upsert_edge(concept, "related-to", st.find_entity(other)["id"],
                               confidence=0.7, origin="action")
    judge = _StubJudge({("nu module", "nu concept"): ("relate", 0.9, "implements")})
    out = svc.deep_dream_judge(judge)
    assert judge.seen == [("nu module", "nu concept")]   # shown flipped
    assert out["auto_rejected"] == 1 and out["relate_links_filed"] == 1
    assert svc._storage.get_entity_proposal(pid)["status"] == "rejected"
    assert ("nu module", "implements", "nu concept", "merge-judge-relate") in _pending_links(svc)


def test_a_failing_link_filing_never_undoes_the_reject(svc, monkeypatch):
    """The link is a side effect of the reject: a filing that raises rolls
    back only itself, and the rest of the batch is still judged."""
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    related = _propose(svc, "xi module", "xi concept")
    plain = _propose(svc, "xi svc", "xi harness")

    def boom(*a, **k):
        raise RuntimeError("link filing failed")
    monkeypatch.setattr(svc, "_graph_propose_links_locked", boom)
    judge = _StubJudge({("xi module", "xi concept"): ("relate", 0.9, "implements"),
                        ("xi svc", "xi harness"): ("reject", 0.9)})
    out = svc.deep_dream_judge(judge)
    assert "error" not in out, out
    assert out["auto_rejected"] == 2 and out["relate_links_filed"] == 0
    for pid in (related, plain):
        assert svc._storage.get_entity_proposal(pid)["status"] == "rejected"


def test_relate_files_at_most_one_link_per_pair(svc):
    """An accepted relate link changes both sides' degree, so reconsideration
    reopens the merge reject and the judge sees the pair again. A pair that
    an edge or any link proposal already joins gets no second link: the
    loop would otherwise stack one edge per vocabulary relation."""
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    svc.config.memory.deep_dream.judge_second_opinion = False
    _propose(svc, "rho2 module", "rho2 concept")
    first = _StubJudge({("rho2 module", "rho2 concept"): ("relate", 0.9, "implements")})
    assert svc.deep_dream_judge(first)["relate_links_filed"] == 1
    (link,) = svc._storage.pending_proposals()
    assert svc.graph_accept_proposal(link["id"])["accepted"]
    again = _StubJudge({("rho2 module", "rho2 concept"): ("relate", 0.9, "uses"),
                        ("rho2 concept", "rho2 module"): ("relate", 0.9, "uses")})
    out = svc.deep_dream_judge(again)
    assert out["reconsideration"]["reopened"] == 1, out
    assert again.seen, "the reopened pair is judged again"
    assert out["relate_links_filed"] == 0
    assert svc._storage.pending_proposals() == []
    assert len(svc._storage.load_graph()["edges"]) == 1


def test_an_unregistered_vocabulary_relation_files_no_link(svc):
    """``tests`` is in the judges' vocabulary but not a registered relation;
    the filing gate would silently turn it into related-to. The merge is
    still rejected; no link is filed."""
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    pid = _propose(svc, "tau test", "tau module")
    judge = _StubJudge({("tau test", "tau module"): ("relate", 0.9, "tests")})
    out = svc.deep_dream_judge(judge)
    assert out["auto_rejected"] == 1 and out["relate_links_filed"] == 0
    assert svc._storage.get_entity_proposal(pid)["status"] == "rejected"
    assert _pending_links(svc) == []


def test_public_link_proposals_resolve_by_name_not_id(svc):
    """The by-id filing is internal to the merge judge: a public proposal
    carrying stale ids (a spread candidate row) still files by its names."""
    st = svc._storage
    for name in ("upsilon a", "upsilon b", "upsilon c"):
        st.ensure_entity(name, display=name)
    a, b = st.find_entity("upsilon a")["id"], st.find_entity("upsilon b")["id"]
    out = svc.graph_propose_links([{"src": "upsilon c", "dst": "upsilon b",
                                    "src_id": a, "dst_id": b,
                                    "relation": "uses", "rationale": "t"}])
    assert out["proposed"] == 1
    (link,) = st.pending_proposals()
    assert link["src"] == "upsilon c"


# ── paginated evidence for outside reviewers (GET /api/graph/proposal-evidence) ──

def _queue(svc):
    """Five pending merges; 'hub one' and 'hub two' share the endpoint 'hub'."""
    for frm, into in (("alpha svc", "alpha service"), ("hub one", "hub"),
                      ("beta svc", "beta service"), ("hub two", "hub"),
                      ("gamma svc", "gamma service")):
        _propose(svc, frm, into)
    return [p for p in svc._storage.pending_entity_proposals()
            if p.get("kind") == "merge"]


def test_proposal_evidence_pages_the_judge_pack(svc):
    """Every page is the merge judge's own evidence pack for its rows, in
    queue order, with enough paging metadata to walk the whole queue."""
    rows = _queue(svc)
    with svc._lock:
        evidence = svc._judge_evidence_locked(rows)
    reference = {r["id"]: r for r in svc._judge_enrich_from(rows, evidence)}

    seen, offset = [], 0
    while offset is not None:
        page = svc.merge_proposal_evidence(offset=offset, limit=2)
        assert page["kind"] == "merge" and page["total"] == len(rows)
        assert page["offset"] == offset and page["limit"] == 2
        for item in page["items"]:
            assert item == reference[item["id"]]
        seen += [item["id"] for item in page["items"]]
        offset = page["next_offset"]
    assert seen == [r["id"] for r in rows]


def test_proposal_evidence_groups_span_pages(svc):
    """A group is one accept-at-most-one decision across the WHOLE queue:
    two rows sharing an endpoint keep their group even when a page holds
    only one of them (the judge pack alone would compute it per page)."""
    rows = _queue(svc)
    hub_rows = [r["id"] for r in rows if r["into"] == "hub"]
    groups = {}
    for offset in range(len(rows)):
        (item,) = svc.merge_proposal_evidence(offset=offset, limit=1)["items"]
        groups[item["id"]] = item["group"]
    assert [groups[i] for i in hub_rows] == ["hub", "hub"]
    assert all(g is None for i, g in groups.items() if i not in hub_rows)


def test_proposal_evidence_clamps_paging(svc):
    rows = _queue(svc)
    page = svc.merge_proposal_evidence(offset=-3, limit=1000)
    assert page["offset"] == 0 and page["limit"] == 100
    assert len(page["items"]) == len(rows) and page["next_offset"] is None
    assert svc.merge_proposal_evidence(offset=0, limit=0)["limit"] == 1
    past = svc.merge_proposal_evidence(offset=len(rows) + 5, limit=10)
    assert past["items"] == [] and past["next_offset"] is None
    assert past["total"] == len(rows)
