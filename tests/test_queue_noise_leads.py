"""Three queue-noise leads from the 2026-08-12 /dream judgment sessions:

* Lesson-task entities leaked into deep-dream link candidates —
  ``candidate_pairs`` never had the lesson exclusion ``graph_review`` has,
  so ``<task> <aspect>`` nodes paired with real artifacts and consumed
  top-k slots (five pairs in one session).
* Co-mention cross-products squeaked under the support-overlap drop: one
  shared note pair generated ten candidate pairs whose supports were
  near-subsets (Jaccard 0.67 < 0.8) — containment, not Jaccard, is the
  co-occurrence signal.
* The lesson store re-minted the same deploy/triage lessons at fresh keys
  every session (five folded on 08-12 alone) — synthesis needs a
  cross-key near-duplicate gate at write time, polarity-aware so an
  "avoid" inversion of a "do" lesson is never suppressed.
"""
from __future__ import annotations

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


# ── lead 2: containment overlap (pure) ────────────────────────────────────

def _vec(x, y):
    v = np.array([x, y], dtype=np.float32)
    return v / np.linalg.norm(v)


def test_support_overlap_uses_containment_not_jaccard():
    # The RE-Evidence-Hub shape: side A mentioned in 3 entries, side B in 2,
    # both of B's inside A's — B's context IS the co-mention, Jaccard 0.67.
    from pseudolife_memory.memory import graph_consolidation as gc

    ents = [{"id": 1, "canonical": "a", "display": "a", "etype": None},
            {"id": 2, "canonical": "b", "display": "b", "etype": None}]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0)}
    mentions = {1: frozenset({10, 11, 12}), 2: frozenset({10, 11})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             max_support_overlap=0.8)
    assert out == []                       # containment 1.0 -> co-occurrence


def test_support_overlap_keeps_genuinely_independent_pairs():
    from pseudolife_memory.memory import graph_consolidation as gc

    ents = [{"id": 1, "canonical": "a", "display": "a", "etype": None},
            {"id": 2, "canonical": "b", "display": "b", "etype": None}]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0)}
    mentions = {1: frozenset({1, 2, 3, 4, 5}), 2: frozenset({4, 5, 6, 7, 8})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             max_support_overlap=0.8)
    assert {(c["src_id"], c["dst_id"]) for c in out} == {(1, 2)}   # 2/5 = 0.4


# ── lead 1: lesson entities excluded from deep-dream candidates ───────────

def _stage_link_pair(svc, a, b):
    svc.graph_relate(a, "related-to", f"anchor-{a[:4]}", origin="agent")
    svc.graph_relate(b, "related-to", f"anchor-{b[:4]}", origin="agent")
    for ent in (a, b):
        svc.store(f"{ent} serves the relay endpoint from the container",
                  source="noise-leads-test")
        svc.store(f"{ent} restarted cleanly after the deploy",
                  source="noise-leads-test")


def test_deep_dream_candidates_exclude_lesson_entities(svc):
    _stage_link_pair(svc, "gadget relay", "widget beacon")
    out1 = svc.deep_dream(apply=False, include_snippets=False)
    pairs1 = {frozenset((c["src"], c["dst"])) for c in out1["candidates"]}
    assert frozenset(("gadget relay", "widget beacon")) in pairs1  # control
    # A lesson lands on the 'gadget relay' task — the node is now
    # lesson-owned and must stop consuming candidate slots.
    svc.lesson_write("gadget relay", "approach",
                     "Restart it via the deploy script, never by hand")
    out2 = svc.deep_dream(apply=False, include_snippets=False)
    flat = {n for c in out2["candidates"] for n in (c["src"], c["dst"])}
    assert "gadget relay" not in flat


# ── lead 3: synthesis-time cross-key lesson dedup ─────────────────────────

class _LessonStub:
    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab, known_facts=None):
        return []

    def extract_lessons(self, signals):
        return [dict(c) for c in self._claims]


def _seed_and_signal(svc):
    svc.lesson_write(
        "deploy a change to the pseudolife daemon", "approach",
        "Deploy via ops/update.ps1 so the backup, rollback tag and "
        "daemon-only rebuild happen together")
    svc.record_outcome("some task", "success", about="something",
                       detail="a pending signal so synthesis runs")


def test_synthesis_skips_cross_key_near_duplicate(svc):
    _seed_and_signal(svc)
    out = svc.synthesize_lessons(_LessonStub([
        # Near-verbatim restatement at a FRESH key — the duplicate factory.
        {"task": "deploy daemon changes and verify them", "aspect": "approach",
         "lesson": "Deploy via ops/update.ps1 so the backup, rollback tag "
                   "and daemon-only rebuild happen together",
         "polarity": "+", "outcome": "success", "confidence": 0.7},
        # Genuinely new content — must still land.
        {"task": "fine-tune a small extractor", "aspect": "tool-choice",
         "lesson": "Use QLoRA rank 16 when distilling from a large teacher",
         "polarity": "+", "outcome": "success", "confidence": 0.7},
    ]))
    assert out["lessons"] == 1
    assert out["deduped"] == 1
    keys = {(r.entity, r.attribute) for r in svc._lessons.current_records()}
    assert ("fine-tune a small extractor", "tool-choice") in {
        (e, a) for e, a in keys} or any("fine-tune" in e for e, a in keys)
    assert not any("deploy daemon changes" in e for e, a in keys)


def test_synthesis_never_suppresses_opposite_polarity(svc):
    _seed_and_signal(svc)
    out = svc.synthesize_lessons(_LessonStub([
        # Same text territory but polarity "-": an INVERSION, not a dup.
        {"task": "deploy daemon changes and verify them", "aspect": "pitfall",
         "lesson": "Deploy via ops/update.ps1 so the backup, rollback tag "
                   "and daemon-only rebuild happen together",
         "polarity": "-", "outcome": "failure", "confidence": 0.7},
    ]))
    assert out["lessons"] == 1 and out.get("deduped", 0) == 0


def test_synthesis_same_key_still_supersedes(svc):
    _seed_and_signal(svc)
    out = svc.synthesize_lessons(_LessonStub([
        # SAME key as the seed: supersession is the store's job — the
        # cross-key gate must not interfere.
        {"task": "deploy a change to the pseudolife daemon",
         "aspect": "approach",
         "lesson": "Deploy via ops/update.ps1 so the backup, rollback tag "
                   "and daemon-only rebuild happen together, then verify",
         "polarity": "+", "outcome": "success", "confidence": 0.7},
    ]))
    assert out["lessons"] == 1 and out.get("deduped", 0) == 0
