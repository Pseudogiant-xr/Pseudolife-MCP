"""Enrichment work a review-judge tick does, and how much of it holds the
service lock. Counted, never timed, so CI cannot flake on it.

The 2026-09-23 daemon logs held the lock for >= 1 s 636 times in ~23.5 h
from the judge stages alone (879 s; deep_dream_judge max 3.13 s). Every
tick re-enriched the WHOLE pending queue under the lock (~490 merges), each
enrichment decoded every entry embedding (load_entries: ~0.4-0.7 s on the
live bank) and built context vectors for every graph entity (~1.1 s for
7,473 entities) to use the mentions of the few in the rows, and the model
payload was enriched a second time under the lock.

Contracts pinned here, per tick:

* a merge/link/junk tick signs only the rows that carry a verdict (their
  freshness check) plus the rows a batch can draw from, and the model
  payload reuses those rows instead of enriching again;
* the queue-sized part of that work runs with the lock released; only the
  re-validation of the judged batch (atomic with its record/apply) runs
  under it;
* merge mentions are computed for the rows' own entities, not the graph's;
* no merge/link/junk tick decodes entry embeddings (none of them reads one);
* the candidate judge reads the bank once per lock hold and fingerprints it
  with the lock released.

PG-backed (skips without the bench server).
"""
from __future__ import annotations

import pytest

from tests.test_queue_judges_service import (  # noqa: F401  (fixtures)
    _CandidateJudge, _JunkJudge, _LinkJudge, _MergeJudge, _junk, _link,
    _propose, pg_conn, pg_url, svc,
)

CAP = 3          # judge batch for the measured tick
JUDGED = 2       # rows judged on the setup tick (they carry a verdict)
QUEUE = 12       # pending rows in the queue
FILLER = 40      # graph entities no proposal touches


class _Work:
    """Per-call row counts from one hook, split by whether the calling
    thread held the service lock at the time."""

    def __init__(self, svc):
        self._svc = svc
        self.locked: list[int] = []
        self.unlocked: list[int] = []

    def add(self, rows: int) -> None:
        (self.locked if self._svc._lock.locked() else self.unlocked).append(rows)

    @property
    def total(self) -> int:
        return sum(self.locked) + sum(self.unlocked)


def _track(svc, monkeypatch, name) -> _Work:
    """Rows passed to the service method ``name``, per call. The evidence
    reads are reached only through ReviewJudgments' getattr dispatch, which
    the static lock-discipline test cannot follow, so the ticks below check
    their lock state here."""
    work = _Work(svc)
    original = getattr(svc, name)

    def tracked(rows, *args, **kwargs):
        work.add(len(rows))
        return original(rows, *args, **kwargs)
    monkeypatch.setattr(svc, name, tracked)
    return work


def _count_embedding_reads(svc, monkeypatch) -> list[int]:
    calls: list[int] = []
    original = svc._storage.load_entries

    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(svc._storage, "load_entries", counted)
    return calls


def _filler(svc):
    for i in range(FILLER):
        svc._storage.ensure_entity(f"filler-{i}", display=f"filler {i}")


@pytest.mark.parametrize("second_opinion", [False, True])
def test_merge_tick_enrichment_is_bounded_and_mostly_unlocked(
        svc, monkeypatch, second_opinion):
    from pseudolife_memory.memory import graph_consolidation as gc

    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = second_opinion
    _filler(svc)
    pairs = [(f"svc{i} alpha", f"service{i} alpha") for i in range(QUEUE)]
    for pair in pairs:
        _propose(svc, *pair)
    verdicts = {pair: ("leave", 0.5) for pair in pairs}
    judge = _MergeJudge(verdicts)
    assert svc.deep_dream_judge(judge, limit=JUDGED)["judged"] == JUDGED

    rows = _Work(svc)
    entities: list[int] = []
    enrich = svc._enrich_merge_proposals

    def counted_enrich(pending, *args, **kwargs):
        rows.add(sum(1 for p in pending if p.get("kind") == "merge"))
        return enrich(pending, *args, **kwargs)
    monkeypatch.setattr(svc, "_enrich_merge_proposals", counted_enrich)
    vectors = gc.entity_context_vectors

    def counted_vectors(ents, *args, **kwargs):
        entities.append(len(ents))
        return vectors(ents, *args, **kwargs)
    monkeypatch.setattr(gc, "entity_context_vectors", counted_vectors)
    reads = _count_embedding_reads(svc, monkeypatch)
    evidence = _track(svc, monkeypatch, "_judge_evidence_locked")

    second = _MergeJudge(verdicts) if second_opinion else None
    out = svc.deep_dream_judge(judge, limit=CAP, second_extractor=second)

    assert out["judged"] + out["second_opinions"] == CAP, out
    # Signed once: the verdict rows plus the first CAP unjudged rows. The
    # batch is re-signed once under the lock to validate it. Before
    # 2026-09-23: the whole queue, then the batch twice, all locked.
    assert rows.total <= JUDGED + 2 * CAP, (rows.locked, rows.unlocked)
    assert sum(rows.locked) <= CAP, (rows.locked, rows.unlocked)
    # Mentions for the rows' own entities, never the whole graph's.
    assert entities and max(entities) <= 2 * (JUDGED + CAP), entities
    assert reads == []
    assert evidence.locked and not evidence.unlocked, evidence.unlocked


def test_stale_check_reads_the_response_identity_once(svc, monkeypatch):
    """refresh() runs under the lock and compared every verdict row with the
    last observed response identity, reading that meta row once PER ROW
    (~490 reads a tick for a fully judged shadow merge queue)."""
    from pseudolife_memory.memory import review_judgments

    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = False
    pairs = [(f"svc{i} alpha", f"service{i} alpha") for i in range(6)]
    for pair in pairs:
        _propose(svc, *pair)
    judge = _MergeJudge({pair: ("leave", 0.5) for pair in pairs})
    assert svc.deep_dream_judge(judge, limit=6)["judged"] == 6
    reads: list[int] = []
    identity = review_judgments.last_response_identity

    def counted(*args, **kwargs):
        reads.append(1)
        return identity(*args, **kwargs)
    monkeypatch.setattr(review_judgments, "last_response_identity", counted)
    assert svc.deep_dream_judge(judge, limit=6)["judged"] == 0
    assert len(reads) <= 1, reads


def test_link_tick_enrichment_is_bounded_and_mostly_unlocked(svc, monkeypatch):
    from pseudolife_memory.memory import graph_consolidation as gc

    svc.config.memory.deep_dream.link_judge_mode = "shadow"
    keys = [(f"tool{i}", "uses", f"library{i}") for i in range(QUEUE)]
    for src, relation, dst in keys:
        _link(svc, src, relation, dst)
    judge = _LinkJudge({key: ("leave", 0.5, None) for key in keys})
    assert svc.deep_dream_judge_links(judge, limit=JUDGED)["judged"] == JUDGED

    rows = _Work(svc)
    shared = gc.shared_mention_entries

    def counted(*args, **kwargs):        # called once per enriched link row
        rows.add(1)
        return shared(*args, **kwargs)
    monkeypatch.setattr(gc, "shared_mention_entries", counted)
    reads = _count_embedding_reads(svc, monkeypatch)
    evidence = _track(svc, monkeypatch, "_link_evidence_locked")

    assert svc.deep_dream_judge_links(judge, limit=CAP)["judged"] == CAP
    assert rows.total <= JUDGED + 2 * CAP, (rows.locked, rows.unlocked)
    assert sum(rows.locked) <= CAP, (rows.locked, rows.unlocked)
    assert reads == []
    assert evidence.locked and not evidence.unlocked, evidence.unlocked


def test_link_pack_tokenizes_each_entry_once(svc, monkeypatch):
    """validate() builds the judged batch's link pack under the lock. The
    co-mention scan re-tokenized every entry once PER ROW: ~140 ms a row on
    the live bank, so a full 8-row batch would hold the lock ~1.3 s."""
    from pseudolife_memory.memory import graph_consolidation as gc
    from pseudolife_memory.memory import graph_review

    for i in range(6):
        svc.store(f"tool{i} reads library{i} at startup", source="t")
        _link(svc, f"tool{i}", "uses", f"library{i}")
    pending = svc._storage.pending_proposals()
    with svc._lock:
        evidence = svc._link_evidence_locked(pending)
    calls: list[int] = []
    tokenize = graph_review._token_set

    def counted(text):
        calls.append(1)
        return tokenize(text)
    monkeypatch.setattr(graph_review, "_token_set", counted)
    monkeypatch.setattr(gc, "_token_set", counted)
    svc._enrich_link_proposals_from(pending, evidence)
    # Each entry once, plus the two display names of each row, which the
    # co-mention and the per-side scans each tokenize.
    assert len(calls) <= len(evidence["entries"]) + 4 * len(pending), len(calls)


def test_junk_tick_enrichment_is_bounded_and_mostly_unlocked(svc, monkeypatch):
    svc.config.memory.deep_dream.junk_judge_mode = "shadow"
    names = [f"junk item {i}" for i in range(QUEUE)]
    for name in names:
        _junk(svc, name)
    judge = _JunkJudge({name: ("leave", 0.5) for name in names})
    assert svc.deep_dream_judge_junk(judge, limit=JUDGED)["judged"] == JUDGED

    rows = _Work(svc)
    fact_rows = svc._storage.entity_fact_rows

    def counted(*args, **kwargs):        # read once per enriched junk row
        rows.add(1)
        return fact_rows(*args, **kwargs)
    monkeypatch.setattr(svc._storage, "entity_fact_rows", counted)
    reads = _count_embedding_reads(svc, monkeypatch)
    evidence = _track(svc, monkeypatch, "_junk_evidence_locked")
    packs = _track(svc, monkeypatch, "_enrich_junk_proposals_from")

    assert svc.deep_dream_judge_junk(judge, limit=CAP)["judged"] == CAP
    assert rows.total <= JUDGED + 2 * CAP, (rows.locked, rows.unlocked)
    assert sum(packs.locked) <= CAP, (packs.locked, packs.unlocked)
    assert reads == []
    assert evidence.locked and not evidence.unlocked, evidence.unlocked


def test_candidate_idle_tick_reads_the_bank_once_and_signs_unlocked(
        svc, monkeypatch):
    """No deep apply to judge: the tick only refreshes its generation."""
    from pseudolife_memory.memory import review_judgments

    svc.config.memory.deep_dream.candidate_judge_mode = "shadow"
    svc.store("alpha and beta share an owner", source="t")
    generations = _Work(svc)
    generation = review_judgments.candidate_generation

    def counted(*args, **kwargs):
        generations.add(1)
        return generation(*args, **kwargs)
    monkeypatch.setattr(review_judgments, "candidate_generation", counted)
    reads = _count_embedding_reads(svc, monkeypatch)

    out = svc.deep_dream_judge_candidates(_CandidateJudge({}))
    assert out.get("reason") == "no_new_apply", out
    assert len(reads) <= 1, reads
    assert generations.total and not generations.locked, (
        generations.locked, generations.unlocked)


def test_candidate_judging_tick_reads_the_bank_once_per_hold(svc, monkeypatch):
    """Three holds touch the bank on a judging tick: the refresh, the
    pre-inference evidence snapshot, and the post-inference re-check that
    must stay atomic with the writes it guards."""
    svc.config.memory.deep_dream.candidate_judge_mode = "shadow"
    svc.store("alpha and beta share an owner", source="t")
    for name in ("alpha", "beta"):
        svc._storage.ensure_entity(name, display=name)
    rows = [{"src": "alpha", "dst": "beta", "similarity": 0.8}]
    reads = _count_embedding_reads(svc, monkeypatch)
    judge = _CandidateJudge({("alpha", "beta"): ("leave", 0.9, None, None, None)})
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)["judged"] == 1
    assert len(reads) <= 3, reads
