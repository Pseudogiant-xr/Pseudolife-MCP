"""Retrieve-then-rerank shape: candidate pool width, RRF fusion, cut order.

Three knobs, all default-OFF so the shipped path stays byte-identical:

* ``memory.search.candidate_pool_multiplier`` — the dense pool each band
  contributes becomes ``k * multiplier`` (band-size capped). Under the
  shipped ``preset: flat`` there is ONE band, so before this the dense
  candidate pool for the whole bank was exactly the served width.
* ``memory.search.fusion`` — ``"weighted_sum"`` (today's raw sort over
  incommensurate channel scores) or ``"rrf"`` (reciprocal rank fusion).
* Rerank-then-cut — under a widened pool the cross-encoder sees the fused
  pool BEFORE the truncation to ``k`` instead of after it.

``test_multiplier_one_matches_captured_prechange_output`` is the
byte-identity pin: ``GOLDEN`` was captured by running this module as a
script against the commit BEFORE these knobs existed (7595ce6f).
Regenerate it only when you INTEND the shipped default path to change.
"""

from __future__ import annotations

import math

import pytest
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig, SearchConfig

DIM = 16

# 12 entries whose cosine to the query descends in even steps. The
# distractors share NO token with the query, so the lexical channel scores
# exactly one document — the WEAKEST dense entry. That entry is therefore
# invisible to a narrow dense pool and can only reach the result through
# the lexical channel, which is what these knobs are about.
TARGET = "retry storm ERR-4471 traced to the gateway rollout"
FIXTURE: list[tuple[str, float]] = [
    ("deployment note zeta about the pipeline schedule", 0.95),
    ("deployment note eta about the pipeline schedule", 0.90),
    ("deployment note theta about the pipeline schedule", 0.85),
    ("deployment note iota about the pipeline schedule", 0.80),
    ("deployment note kappa about the pipeline schedule", 0.75),
    ("deployment note lambda about the pipeline schedule", 0.70),
    ("deployment note mu about the pipeline schedule", 0.65),
    ("deployment note nu about the pipeline schedule", 0.60),
    ("deployment note xi about the pipeline schedule", 0.55),
    ("deployment note omicron about the pipeline schedule", 0.50),
    ("deployment note rho about the pipeline schedule", 0.45),
    (TARGET, 0.40),
]

QUERY_TEXT = "ERR-4471 retry storm on the gateway rollout"

# A second bank for the pure-widening demonstration: the lexical target
# sits mid-pack on cosine, close enough that admitting it to the dense pool
# (rather than injecting it at ``weight x normalised``) lifts it to the top.
LEX_TARGET = "retry storm ERR-4471 traced to the gateway rollout"
LEX_FIXTURE: list[tuple[str, float]] = [
    ("deployment note zeta about the pipeline schedule", 0.95),
    ("deployment note eta about the pipeline schedule", 0.90),
    ("deployment note theta about the pipeline schedule", 0.85),
    ("deployment note iota about the pipeline schedule", 0.80),
    ("deployment note kappa about the pipeline schedule", 0.76),
    (LEX_TARGET, 0.72),
]


def _vec(cos: float) -> torch.Tensor:
    """Unit vector in the (e0, e1) plane at the requested cosine to e0."""
    v = torch.zeros(DIM)
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


def _query() -> torch.Tensor:
    v = torch.zeros(DIM)
    v[0] = 1.0
    return v


def _cms(rows, *, reranker=None, **search_kwargs) -> ContinuumMemorySystem:
    cfg = MemoryConfig(embedding_dim=DIM)
    for key, value in search_kwargs.items():
        setattr(cfg.search, key, value)
    if reranker is not None:
        cfg.reranker.enabled = True
    cms = ContinuumMemorySystem(cfg, reranker=reranker)
    for text, cos in rows:
        cms.store(text, _vec(cos), source="user")
    return cms


def _build(**search_kwargs) -> ContinuumMemorySystem:
    return _cms(FIXTURE, **search_kwargs)


def _serve(cms: ContinuumMemorySystem, *, top_k: int = 4, **kwargs):
    return cms.retrieve(_query(), top_k=top_k, query_text=QUERY_TEXT, **kwargs)


# Captured on the pre-knob commit (7595ce6f); see the module docstring.
GOLDEN: list[tuple[str, float]] = [
    ("deployment note zeta about the pipeline schedule", 0.95),
    ("deployment note eta about the pipeline schedule", 0.9),
    ("deployment note theta about the pipeline schedule", 0.85),
    ("deployment note iota about the pipeline schedule", 0.8),
]


# ── Config surface ───────────────────────────────────────────────────────


def test_defaults_are_off():
    cfg = SearchConfig()
    assert cfg.candidate_pool_multiplier == 1
    assert cfg.fusion == "weighted_sum"


# Console absence is enforced in its canonical home,
# tests/test_console_knob_gapfill.py::
# test_gated_off_capabilities_stay_out_of_console — these knobs sit on that
# list because neither has passed the judged gate.


# ── Default identity ─────────────────────────────────────────────────────


def test_multiplier_one_matches_captured_prechange_output():
    """The shipped path is byte-identical to the pre-knob code."""
    res = _serve(_build())
    got = [(e.text, round(float(s), 6)) for e, s in zip(res.entries, res.scores)]
    assert got == [(t, round(s, 6)) for t, s in GOLDEN]


def test_multiplier_one_declares_the_shipped_shape_in_params():
    res = _serve(_build())
    assert res.params["candidate_pool"] == {
        "multiplier": 1, "pool_size": 4,
        "fusion": "weighted_sum", "rerank_position": "after_cut"}


# ── Knob 1: candidate pool width ─────────────────────────────────────────


def test_widened_pool_admits_a_lexical_hit_the_narrow_pool_could_not_reach():
    """The lexical target's cosine puts it 6th of 6; at ``top_k=3`` the
    narrow dense pool never sees it, so it can only enter as a BM25-only
    injection at ``weight x normalised`` (<= 0.3) — below every dense hit,
    hence cut. Widen the pool and it enters as a DENSE candidate, so the
    lexical boost lands on top of its cosine instead of replacing it."""
    narrow = [e.text for e in _cms(LEX_FIXTURE).retrieve(
        _query(), top_k=3, query_text=QUERY_TEXT).entries]
    wide_res = _cms(LEX_FIXTURE, candidate_pool_multiplier=4).retrieve(
        _query(), top_k=3, query_text=QUERY_TEXT)
    wide = [e.text for e in wide_res.entries]

    assert LEX_TARGET not in narrow, narrow
    assert wide[0] == LEX_TARGET, wide
    # 0.72 dense + 0.3 x 1.0 lexical — the boost is additive, not a floor.
    assert float(wide_res.scores[0]) == pytest.approx(1.02, abs=1e-4)


def test_widened_pool_still_truncates_to_k():
    assert len(_serve(_build(candidate_pool_multiplier=4), top_k=3).entries) == 3


def test_widened_pool_respects_the_band_name_filter():
    cms = _build(candidate_pool_multiplier=4)
    assert _serve(cms, top_k=4, bands=[cms.bands[0].name]).entries
    assert _serve(cms, top_k=4, bands=["no-such-band"]).entries == []


def test_widened_pool_reports_the_effective_size_after_the_band_cap():
    res = _serve(_build(candidate_pool_multiplier=4), top_k=4)
    assert res.params["candidate_pool"]["multiplier"] == 4
    # k=4 x 4 = 16 requested, capped by the 12-entry band.
    assert res.params["candidate_pool"]["pool_size"] == 12


def test_multiplier_below_one_is_clamped_not_silently_narrowing():
    res = _serve(_build(candidate_pool_multiplier=0), top_k=4)
    assert res.params["candidate_pool"]["multiplier"] == 1
    assert [e.text for e in res.entries] == [t for t, _ in GOLDEN]


# ── Knob 2: RRF fusion ───────────────────────────────────────────────────


def test_rrf_surfaces_the_lexical_winner_that_weighted_sum_buries():
    """Same widened pool, two fusions. Under weighted sum the target's
    ``0.40 + 0.3`` lands 7th and is cut. Under RRF its rank-1 in the
    lexical list is worth as much as rank-1 in the dense list, so the sum
    of two reciprocal ranks beats the dense leader's one."""
    ws = [e.text for e in _serve(
        _build(candidate_pool_multiplier=4), top_k=4).entries]
    rrf = [e.text for e in _serve(
        _build(candidate_pool_multiplier=4, fusion="rrf"), top_k=4).entries]

    assert TARGET not in ws, ws
    assert rrf[0] == TARGET, rrf


def test_rrf_scores_are_reciprocal_ranks_not_cosines():
    res = _serve(_build(candidate_pool_multiplier=4, fusion="rrf"), top_k=4)
    # RRF_K = 60: one channel at rank 1 scores 1/61 ~ 0.0164, two channels
    # ~0.0328. Nothing on this scale can be read as a cosine.
    assert all(0.0 < float(s) < 0.05 for s in res.scores), res.scores
    assert float(res.scores[0]) == pytest.approx(1 / 61 + 1 / 72, abs=1e-6)
    assert res.params["candidate_pool"]["fusion"] == "rrf"


# Two entries that both win the lexical channel and rank 1/2 on cosine, and
# that the store-path contradiction detector leaves alone — "port 8080" vs
# "port 9090" is auto-superseded on store, which would make a supersession
# test pass without the flag under test doing any work.
_SUPERSESSION_PAIR = [("alpha note about the gateway rollout", 0.95),
                      ("beta note about the gateway rollout", 0.90)]
_SUPERSESSION_Q = "notes about the gateway rollout"


def _pair_cms():
    cms = _cms(_SUPERSESSION_PAIR, candidate_pool_multiplier=4, fusion="rrf")
    assert all(e.superseded_at is None for b in cms.bands for e in b.entries), (
        "fixture drifted: the store path auto-superseded one of the pair, so "
        "the flag under test would not be load-bearing")
    return cms


def test_rrf_keeps_the_supersession_demotion():
    """Supersession stays a ranking-only multiplier — applied to the FUSED
    score, so the superseded entry still surfaces (v0.7.3) but below its
    successor."""
    cms = _pair_cms()
    next(e for b in cms.bands for e in b.entries
         if "alpha" in e.text).superseded_at = 1.0

    res = cms.retrieve(_query(), top_k=4, query_text=_SUPERSESSION_Q)
    texts = [e.text for e in res.entries]
    assert any("alpha" in t for t in texts), texts
    assert ([i for i, t in enumerate(texts) if "beta" in t][0]
            < [i for i, t in enumerate(texts) if "alpha" in t][0]), texts
    # The multiplier is applied ONCE, to the fused score. alpha still ranks
    # 1st on cosine (the dense channel ranks on ``relevance``, which carries
    # recency but NOT the ranking-only multipliers) and 1st on BM25, so its
    # fused score is 2/61 before the 0.55. Feeding the already-multiplied
    # ``adjusted`` score into the dense rank instead would demote it to rank
    # 2 there AND multiply again — this literal is what catches that.
    alpha = next(s for t, s in zip(texts, res.scores) if "alpha" in t)
    assert float(alpha) == pytest.approx((1 / 61 + 1 / 61) * 0.55, abs=1e-9)


def test_rrf_reads_supersession_live_at_query_time():
    """The multiplier comes off the live entry, not a snapshot taken when
    the pool was built: flipping the flag between two queries on the SAME
    cms must move the entry."""
    cms = _pair_cms()
    before = [e.text for e in cms.retrieve(
        _query(), top_k=4, query_text=_SUPERSESSION_Q).entries]
    assert "alpha" in before[0], before
    next(e for b in cms.bands for e in b.entries
         if "alpha" in e.text).superseded_at = 1.0
    after = [e.text for e in cms.retrieve(
        _query(), top_k=4, query_text=_SUPERSESSION_Q).entries]
    assert "beta" in after[0], after


def test_rrf_gates_each_channel_on_its_native_score_not_the_fused_one():
    """``min_score`` stays a contract over the result set, applied per
    channel on that channel's own scale: the dense floor bounds cosines,
    the injection floor bounds ``weight x normalised``. Fused RRF scores
    live on a ~0.016 scale — comparing THOSE to a cosine floor would empty
    every result set, and would drop lexical-only hits on a gate they were
    never scored by."""
    cms = _cms([("quarterly planning summary for the finance team", 0.90),
                ("ERR-9912 stack trace in the nightly job", 0.10),
                ("unrelated grocery list with milk and bread", 0.10)],
               candidate_pool_multiplier=4, fusion="rrf")
    res = cms.retrieve(_query(), top_k=4, query_text="ERR-9912 failure",
                       min_score=0.25)
    texts = [e.text for e in res.entries]

    # Cosine 0.10 but the sole lexical hit: injected at 0.3 x 1.0 >= 0.25,
    # so the explicit floor admits it even though its cosine is below.
    assert any("ERR-9912" in t for t in texts), texts
    # Cosine 0.10 and no lexical signal: gated by the dense floor.
    assert not any("grocery" in t for t in texts), texts
    # And no served score was ever compared against 0.25.
    assert all(float(s) < 0.25 for s in res.scores), res.scores


def test_bad_fusion_mode_is_rejected_loudly():
    with pytest.raises(ValueError, match="fusion"):
        _serve(_build(fusion="nonsense"))


# ── Knob 3: rerank-then-cut ──────────────────────────────────────────────


class _StubReranker:
    """Records the head it was handed and scores by pool position, so the
    cross-encoder's verdict is unambiguous and needs no model.

    ``keep_order=True`` scores the head DESCENDING, i.e. the cross-encoder
    agrees with the bi-encoder — which leaves the trailing reference pool at
    the bottom, the arrangement that exposes a positional cut.
    """

    def __init__(self, keep_order: bool = False) -> None:
        self.seen: list[list[str]] = []
        self.keep_order = keep_order

    def is_available(self) -> bool:
        return True

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.seen.append(list(candidates))
        n = len(candidates)
        if self.keep_order:
            return [float(n - i) for i in range(n)]   # first wins
        return [float(i) for i in range(n)]           # last wins

    def fuse(self, original: list[float], ce: list[float]) -> list[float]:
        return [float(c) for c in ce]


def test_default_reranks_after_the_cut():
    stub = _StubReranker()
    res = _serve(_cms(FIXTURE, reranker=stub), top_k=4)
    assert len(stub.seen) == 1
    # Truncate-then-rerank: the cross-encoder only ever saw k candidates,
    # so its top_n=20 budget was never more than ~11 wide in production.
    assert len(stub.seen[0]) == 4, stub.seen[0]
    assert res.params["candidate_pool"]["rerank_position"] == "after_cut"


def test_widened_pool_reranks_before_the_cut():
    stub = _StubReranker()
    res = _serve(_cms(FIXTURE, reranker=stub, candidate_pool_multiplier=4),
                 top_k=4)
    assert len(stub.seen) == 1
    # The cross-encoder saw the WIDENED pool (12 entries, its top_n is 20).
    assert len(stub.seen[0]) == 12, stub.seen[0]
    assert res.params["candidate_pool"]["rerank_position"] == "before_cut"
    # The served result is still k, chosen by the reranker — the stub
    # scores the LAST candidate highest.
    assert len(res.entries) == 4
    assert res.entries[0].text == stub.seen[0][-1]


class _StubReferenceBank:
    """Minimal Pool-2 stand-in: two documents, always retrievable."""

    DOCS = ("reference doc one on gateway rollouts",
            "reference doc two on gateway rollouts")

    def retrieve(self, query_embedding, top_k=3):
        from pseudolife_memory.memory.cms import MemoryEntry, RetrievalResult
        entries = [MemoryEntry(text=t, embedding=_vec(0.99), source="doc",
                               bank="reference")
                   for t in self.DOCS[:top_k]]
        return RetrievalResult(entries=entries,
                               scores=[0.99] * len(entries),
                               surprises=[0.0] * len(entries))


def test_deferred_cut_still_reserves_the_reference_pool_slots():
    """Pool 2's standing guarantee: reference documents are never displaced
    by memories. ``combined`` is ``neural + ref_pool`` CONCATENATED, so a
    plain slice of the widened pool would drop the refs positionally — the
    exact regression the deferred cut invites."""
    cfg = MemoryConfig(embedding_dim=DIM)
    cfg.search.candidate_pool_multiplier = 4
    cfg.reranker.enabled = True
    # keep_order: the cross-encoder agrees with the bi-encoder, so the
    # trailing reference pool stays at the bottom of `combined` — a
    # positional cut would drop it, which is the point of the test.
    stub = _StubReranker(keep_order=True)
    cms = ContinuumMemorySystem(cfg, reference_bank=_StubReferenceBank(),
                                reranker=stub)
    for text, cos in FIXTURE:
        cms.store(text, _vec(cos), source="user")

    res = _serve(cms, top_k=4)
    texts = [e.text for e in res.entries]
    assert res.params["candidate_pool"]["rerank_position"] == "before_cut"
    for doc in _StubReferenceBank.DOCS:
        assert doc in texts, texts
    # k memories + every reference document — the default path's cardinality.
    assert len(texts) == 4 + len(_StubReferenceBank.DOCS), texts


def test_rerank_before_cut_does_not_fire_without_a_reranker():
    res = _serve(_build(candidate_pool_multiplier=4), top_k=4)
    assert res.params["candidate_pool"]["rerank_position"] == "after_cut"


# ── explain=True trace ───────────────────────────────────────────────────


def test_trace_records_pool_size_fusion_and_rerank_position():
    cms = _cms(FIXTURE, reranker=_StubReranker(),
               candidate_pool_multiplier=4, fusion="rrf")
    _res, trace = cms.retrieve_with_trace(
        _query(), top_k=4, query_text=QUERY_TEXT)
    assert trace["candidate_pool"] == {
        "multiplier": 4, "pool_size": 12,
        "fusion": "rrf", "rerank_position": "before_cut"}


def test_trace_default_records_the_shipped_shape():
    _res, trace = _build().retrieve_with_trace(
        _query(), top_k=4, query_text=QUERY_TEXT)
    assert trace["candidate_pool"] == {
        "multiplier": 1, "pool_size": 4,
        "fusion": "weighted_sum", "rerank_position": "after_cut"}


if __name__ == "__main__":  # pragma: no cover - golden capture helper
    result = _serve(_build())
    print("GOLDEN: list[tuple[str, float]] = [")
    for entry, score in zip(result.entries, result.scores):
        print(f"    ({entry.text!r}, {round(float(score), 6)}),")
    print("]")
