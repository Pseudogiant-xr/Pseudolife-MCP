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

Two goldens pin the default path, both captured by running this module as
a script against the commit BEFORE these knobs existed (7595ce6f):

* ``GOLDEN`` — four un-boosted dense cosines, the plain ranking.
* ``GOLDEN_MIXED`` — the same path with the channels this diff touched
  actually firing: a served entry carrying a nonzero BM25 boost (the one
  default-path line the diff changed, the ``fusion_mode == "weighted_sum"``
  guard on the boost) and a served slot hit on its own 0.55-0.95 scale.

Together they pin the shipped weighted-sum path with the dense, lexical
and slot channels live. They do NOT pin the reranker or the reference
pool at multiplier 1 — those are covered by behavioural tests below, not
by a captured golden. Regenerate a golden only when you INTEND the
shipped default path to change.
"""

from __future__ import annotations

import math

import pytest
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.reranker import CrossEncoderReranker
from pseudolife_memory.utils.config import (
    MemoryConfig,
    SearchConfig,
    load_config,
)

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


# A third bank exercising the channels the plain GOLDEN never touches:
# the first row shares three content tokens with the query (so it is a
# served DENSE hit carrying a nonzero BM25 boost), and the last row is the
# only slot-bearing entry (``Jacque.type=cat`` / ``Jacque.breed=Ragdoll``),
# whose cosine is far too low to reach the dense pool — it can only arrive
# through the slot channel, at that channel's own 0.55-0.95 confidence
# scale. The distractor cosines sit low enough that the slot hit is not
# cut by the truncation to ``top_k``.
MIXED_FIXTURE: list[tuple[str, float]] = [
    ("the gateway rollout owner is the platform team", 0.95),
    ("deployment note eta about the pipeline schedule", 0.62),
    ("deployment note theta about the pipeline schedule", 0.58),
    ("deployment note iota about the pipeline schedule", 0.54),
    ("I have a Ragdoll cat named Jacque", 0.30),
]
MIXED_QUERY = "who owns the gateway rollout and what breed is Jacque"


# Captured on the pre-knob commit (7595ce6f); see the module docstring.
GOLDEN: list[tuple[str, float]] = [
    ("deployment note zeta about the pipeline schedule", 0.95),
    ("deployment note eta about the pipeline schedule", 0.9),
    ("deployment note theta about the pipeline schedule", 0.85),
    ("deployment note iota about the pipeline schedule", 0.8),
]

# Ditto, over MIXED_FIXTURE: 1.25 is 0.95 dense + 0.3 x 1.0 BM25 (a served
# entry with a live lexical boost), 0.666667 is the slot channel's
# 0.55 + 0.35 x (1/3) confidence (a served slot hit), and the tail is plain
# dense. One capture, three channels.
GOLDEN_MIXED: list[tuple[str, float]] = [
    ("the gateway rollout owner is the platform team", 1.25),
    ("I have a Ragdoll cat named Jacque", 0.666667),
    ("deployment note eta about the pipeline schedule", 0.62),
    ("deployment note theta about the pipeline schedule", 0.58),
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


def test_multiplier_one_matches_captured_prechange_output_with_all_channels():
    """The BM25-boost and slot channels are byte-identical too.

    ``GOLDEN`` alone only covers un-boosted cosines, which leaves the one
    default-path line this change touched — the ``weighted_sum`` guard on
    the BM25 boost — unpinned. This fixture serves an entry that carries a
    nonzero boost and an entry that arrives only through the slot channel.
    """
    cms = _cms(MIXED_FIXTURE)
    res, trace = cms.retrieve_with_trace(
        _query(), top_k=4, query_text=MIXED_QUERY)
    got = [(e.text, round(float(s), 6)) for e, s in zip(res.entries, res.scores)]
    assert got == [(t, round(s, 6)) for t, s in GOLDEN_MIXED]
    # Both channels really fired — a fixture that quietly stopped boosting
    # or stopped hitting a slot would still match a golden captured from
    # it, so the golden's value depends on this staying true.
    assert res.params["candidate_pool"]["multiplier"] == 1
    assert [h["text_preview"] for h in trace["slot_pool"]] == [
        "I have a Ragdoll cat named Jacque"]
    assert got[0][1] == pytest.approx(0.95 + 0.3, abs=1e-9)


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


def test_bad_fusion_mode_is_rejected_at_config_load():
    """A typo'd mode fails at STARTUP, not once per query.

    ``_build(fusion=...)`` below reaches ``retrieve`` by setattr, which
    skips ``__post_init__`` — the belt. This is the load-time gate a
    config.yaml typo actually meets."""
    with pytest.raises(ValueError, match="fusion"):
        SearchConfig(fusion="nonsense")


def test_bad_fusion_mode_in_yaml_fails_the_daemon_at_startup(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "memory:\n  search:\n    fusion: rff\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="fusion"):
        load_config(cfg)


def test_bad_fusion_mode_is_rejected_loudly():
    """The belt: a config object that never ran ``__post_init__`` (per-
    attribute setattr, eval harnesses, anything hand-built) still cannot
    serve a query under an unknown mode."""
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


# ── The rrf scale hazard: rrf x reranker x reference pool ────────────────
# The CAUTION on ``SearchConfig.fusion`` names four thresholds that change
# meaning when the served score drops from the cosine scale to
# ~0.016-0.05. ``search_confidence_floor`` is documented at its own site;
# these pin the other three. They assert TODAY'S behaviour, which is the
# hazardous behaviour — nothing here is rescaled, because rescaling would
# be an unmeasured change to a path the judged verdict never covered
# (rrf was measured with the reranker OFF and an empty reference bank).
# The pins exist so the hazard cannot be discovered a third time by
# surprise, and so a future rescaling has to move a test on purpose.

_HAZARD_FIXTURE = [("alpha gateway rollout note", 0.95),
                   ("beta gateway rollout note", 0.60)]
_HAZARD_QUERY = "gateway rollout note"


class _RealFuseReranker(CrossEncoderReranker):
    """Stub that keeps the SHIPPED fusion arithmetic.

    Subclasses the real reranker and overrides only the model call, so
    ``fuse`` is literally ``w * ce + (1 - w) * orig`` as served — a
    hand-rolled pass-through of ``ce`` (the ``_StubReranker`` above) would
    hide exactly the term under test.
    """

    def __init__(self, ce_by_text: dict[str, float], **kwargs) -> None:
        super().__init__(**kwargs)
        self.ce_by_text = ce_by_text
        self.seen: list[list[str]] = []

    def is_available(self) -> bool:
        return True

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.seen.append(list(candidates))
        return [self.ce_by_text[c] for c in candidates]


def _hazard_cms(ce_by_text, *, fusion, reference=None, skip_margin=0.0):
    cfg = MemoryConfig(embedding_dim=DIM)
    cfg.search.fusion = fusion
    cfg.reranker.enabled = True
    cfg.reranker.skip_margin = skip_margin
    stub = _RealFuseReranker(ce_by_text)
    cms = ContinuumMemorySystem(cfg, reference_bank=reference, reranker=stub)
    for text, cos in _HAZARD_FIXTURE:
        cms.store(text, _vec(cos), source="user")
    return cms, stub


def _hazard_serve(cms):
    # bm25=False isolates the dense channel: with both fixture entries
    # matching the query lexically, the BM25 boost would be an equal
    # constant and only add noise to the scale comparison.
    return cms.retrieve(_query(), top_k=4, query_text=_HAZARD_QUERY,
                        bm25=False)


def test_rrf_collapses_the_bi_encoder_term_of_the_reranker_fusion():
    """``fusion_weight`` stops mixing anything under rrf.

    ``fuse`` is ``0.7 * ce + 0.3 * orig``. On cosines the bi-encoder term
    spans 0.3 x (0.95 - 0.60) = 0.105, enough to hold a 0.02 cross-encoder
    difference off. On rrf scores it spans 0.3 x (1/61 - 1/62) = 0.00008,
    so the same 0.02 flips the ranking: rrf + reranker is cross-encoder-
    only ordering, whatever ``fusion_weight`` says."""
    ce = {"alpha gateway rollout note": 0.50,
          "beta gateway rollout note": 0.52}

    ws, _ = _hazard_cms(ce, fusion="weighted_sum")
    rrf, _ = _hazard_cms(ce, fusion="rrf")
    ws_order = [e.text for e in _hazard_serve(ws).entries]
    rrf_res = _hazard_serve(rrf)
    rrf_order = [e.text for e in rrf_res.entries]

    assert ws_order[0].startswith("alpha"), ws_order
    assert rrf_order[0].startswith("beta"), rrf_order
    # The served score is the real fusion of a ce score and an rrf score:
    # 0.7 x 0.52 + 0.3 x 1/62.
    assert float(rrf_res.scores[0]) == pytest.approx(
        0.7 * 0.52 + 0.3 * (1 / 62), abs=1e-9)


def test_rrf_makes_a_cosine_scaled_skip_margin_unreachable():
    """``skip_margin`` inverts: the gate that should fire, never does.

    A margin tuned on cosines (0.15) skips the ~200ms cross-encoder pass
    on a decisively separated head. Fused rrf scores are ~0.016 apart at
    the very top, so the gate can never be reached — the pass it exists to
    avoid runs on every query instead."""
    ce = {"alpha gateway rollout note": 0.50,
          "beta gateway rollout note": 0.52}

    ws, ws_stub = _hazard_cms(ce, fusion="weighted_sum", skip_margin=0.15)
    ws_res = _hazard_serve(ws)
    # 0.95 - 0.60 = 0.35 >= 0.15: skipped, as designed.
    assert ws_stub.seen == []
    assert ws_res.params["reranker"]["skip_reason"] == "unambiguous_margin"

    rrf, rrf_stub = _hazard_cms(ce, fusion="rrf", skip_margin=0.15)
    rrf_res = _hazard_serve(rrf)
    # 1/61 - 1/62 = 0.00026 < 0.15: the same decisively-separated head
    # reranks anyway.
    assert len(rrf_stub.seen) == 1, rrf_stub.seen
    assert rrf_res.params["reranker"]["fired"] is True
    assert rrf_res.params["reranker"]["margin"] == pytest.approx(
        1 / 61 - 1 / 62, abs=1e-9)


def test_rrf_lets_un_rescaled_reference_cosines_overturn_the_reranker():
    """Pool 2 keeps RAW cosines (~0.99) and is never rescaled.

    With the reranker off they simply trail. With it on, the ``0.3 x orig``
    term hands every reference document ~0.29 of unearned score — worth a
    0.42 cross-encoder gap — so the cross-encoder's verdict is overturned
    by the scale mismatch alone. Same ce scores, same pool, only the
    fusion mode differs."""
    ce = {"alpha gateway rollout note": 0.65,
          "beta gateway rollout note": 0.65,
          _StubReferenceBank.DOCS[0]: 0.30,
          _StubReferenceBank.DOCS[1]: 0.30}

    ws, _ = _hazard_cms(ce, fusion="weighted_sum",
                        reference=_StubReferenceBank())
    rrf, _ = _hazard_cms(ce, fusion="rrf", reference=_StubReferenceBank())
    ws_order = [e.text for e in _hazard_serve(ws).entries]
    rrf_order = [e.text for e in _hazard_serve(rrf).entries]

    # Cosine scale: the cross-encoder prefers the memory, and gets its way.
    assert ws_order[0].startswith("alpha"), ws_order
    # RRF scale: both reference documents sort above every memory.
    assert rrf_order[:2] == list(_StubReferenceBank.DOCS), rrf_order


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
    def _dump(name, result) -> None:
        print(f"{name}: list[tuple[str, float]] = [")
        for entry, score in zip(result.entries, result.scores):
            print(f"    ({entry.text!r}, {round(float(score), 6)}),")
        print("]")

    _dump("GOLDEN", _serve(_build()))
    _dump("GOLDEN_MIXED", _cms(MIXED_FIXTURE).retrieve(
        _query(), top_k=4, query_text=MIXED_QUERY))
