"""BM25 lexical channel on cortex fact retrieval (service layer).

The turn-path pool (cms.py) has had hybrid dense+BM25 retrieval since
0.9.0; cortex fact retrieval stayed pure dense cosine. That asymmetry is
measurable: on the 2026-07-30 ceiling-e2e run, a "How many Korean
restaurants…" question was served 7 facts about basmati rice — the dense
channel bridges neither identifiers nor rare exact tokens, which is the
documented reason BM25 exists for the turn pool.

These tests pin the service-level contract for `cortex_search(bm25=...)`:
same tri-state override as `memory_search`, same `memory.bm25` config
family, and — the load-bearing piece — lexical hits are gated by the
normalised `bm25.min_score`, NOT by the caller's dense `min_score` floor,
so a fact the dense channel scores below the floor can still be served
when the query names it exactly.

Like test_cortex_contenders.py this runs against a real MemoryService
(offline embedder). Most tests here only READ an identical seeded corpus, so
they share one module-scoped seeded service; the two that seed a different
corpus take conftest's ``pristine_service`` instead.
"""
from __future__ import annotations

import pytest

from pseudolife_memory.service import MemoryService

# Filler facts so the BM25 index has a corpus with real IDF spread.
# Fillers are deliberately spread across semantic distance from the test
# queries (one ops-adjacent, the rest remote): three ops-flavored fillers
# once clustered within 0.004 fused score of each other against the
# ops-flavored query below, which put the lockstep assertions at the mercy
# of cross-environment embedding numerics (the 2026-08 CI flap). The
# knife-edge guard inside the lockstep tests enforces the separation.
_FILLER = [
    ("dinner party menu", "selected side dishes", "kimchi and bokkeumbap"),
    ("basmati rice", "cooking tips", "soak before cooking, correct ratio"),
    ("hallway repaint", "chosen color", "sage green"),
    ("payments-db", "host", "10.0.0.7"),
    ("garden birdfeeder", "refill cadence", "weekly, sunflower hearts"),
]


def _seed(svc: MemoryService) -> None:
    for e, a, v in _FILLER:
        svc.cortex_write(e, a, v, provenance=["seed"])
    # The target: an identifier-style token with no semantic neighbours.
    svc.cortex_write("ticket PRB052840832", "workflow",
                     "Knowledge Search; Problems; Private Task",
                     provenance=["seed"])


@pytest.fixture(scope="module")
def seeded_service(tmp_path_factory):
    """One service per module, seeded ONCE with ``_seed``'s six facts, for the
    tests that only READ that corpus.

    Deliberately its own service rather than conftest's ``warm_service``: the
    two set-slot tests below take ``pristine_service``, which empties
    warm_service's bank, so sharing one service would make this corpus depend
    on file order. Any test that WRITES must take ``pristine_service``, not
    this fixture — a write here leaks into every later reader.
    """
    svc = MemoryService(data_dir=tmp_path_factory.mktemp("cortex-bm25"))
    _seed(svc)
    return svc


def test_bm25_serves_lexical_fact_the_dense_floor_drops(seeded_service):
    svc = seeded_service
    # min_score=0.99: no dense cosine hit survives, so anything
    # returned came through the lexical channel.
    got = svc.cortex_search("redistribute PRB052840832", top_k=5,
                            min_score=0.99, bm25=True)["entries"]
    assert any("PRB052840832" in e["entity"] for e in got), got
    # Same call without the lexical channel: starved.
    got_off = svc.cortex_search("redistribute PRB052840832", top_k=5,
                                min_score=0.99, bm25=False)["entries"]
    assert got_off == []


def test_bm25_cortex_defaults_off_even_when_turn_pool_is_on(seeded_service):
    """The 2026-07-30 pre-registered _s A/B failed (bm25-ab-confirmation.json:
    56/78 contexts changed, zero accuracy/commit-rate movement, ~1 question
    cost on the oracle gate slice), so the cortex-side channel ships OPT-IN:
    `memory.bm25.cortex_enabled = False` by default, independent of the turn
    pool's `enabled = True`."""
    svc = seeded_service
    assert svc.config.memory.bm25.enabled is True          # turn pool on
    assert svc.config.memory.bm25.cortex_enabled is False  # facts off
    # Default call: dense only — the lexical fact channel must not fire.
    assert svc.cortex_search("redistribute PRB052850000 PRB052840832",
                             top_k=5, min_score=0.99)["entries"] == []
    # Config opt-in turns it on without a per-call override.
    svc.config.memory.bm25.cortex_enabled = True
    try:
        got = svc.cortex_search("redistribute PRB052850000 PRB052840832",
                                top_k=5, min_score=0.99)["entries"]
        assert any("PRB052840832" in e["entity"] for e in got)
        # Per-call False overrides config True (tri-state preserved).
        assert svc.cortex_search("redistribute PRB052850000 PRB052840832",
                                 top_k=5, min_score=0.99,
                                 bm25=False)["entries"] == []
    finally:
        # The service is module-scoped: leaving the opt-in on would silently
        # change the channel default for every later test in this file.
        svc.config.memory.bm25.cortex_enabled = False


def test_bm25_boost_raises_score_of_lexical_match(seeded_service):
    svc = seeded_service
    query = "workflow for ticket PRB052840832"

    def score_of(entries):
        for e in entries:
            if "PRB052840832" in e["entity"]:
                return e["score"]
        return None

    on = score_of(svc.cortex_search(query, top_k=6, bm25=True)["entries"])
    off = score_of(svc.cortex_search(query, top_k=6,
                                     bm25=False)["entries"])
    assert on is not None
    # Load-bearing check: with the channel disabled the fused boost
    # disappears — the same fact scores strictly lower (or is absent).
    assert off is None or on > off


def test_bm25_entries_keep_cortex_shape(seeded_service):
    """Lexically-injected entries carry the same dict shape as dense hits
    (entity/attribute/value/score/contested), so consumers cannot tell
    the channels apart structurally."""
    got = seeded_service.cortex_search("PRB052840832", top_k=3,
                                       min_score=0.99, bm25=True)["entries"]
    assert got, "lexical channel should have served the identifier fact"
    entry = got[0]
    for key in ("entity", "attribute", "value", "score", "contested"):
        assert key in entry, f"missing {key!r} in {entry}"


def test_rebuild_fact_ranking_matches_service_fusion(seeded_service):
    """Lockstep guard: evals/rebuild_contexts.py re-implements cortex fact
    ranking offline (it ranks dumped banks, not a live store). The 2026-07-30
    regression-gate run proved why this must be pinned: the gate 'passed'
    the BM25 channel without ever executing it, because the rebuild had its
    own dense-only ranking. Any fusion change must land in both places or
    this test goes red.

    Scope since schema v35: the service also pins in-scope constraint
    facts ahead of the fusion; the rebuild does not mirror that, so this
    lockstep is asserted on an UNLABELLED bank (as every bench bank is)
    and says nothing about a labelled one."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "evals"))
    from rebuild_contexts import rebuild_fact_lines

    facts = [
        {"entity": e, "attribute": a, "value": v, "history": [v]}
        for e, a, v in _FILLER
    ] + [{"entity": "ticket PRB052840832", "attribute": "workflow",
          "value": "Knowledge Search; Problems; Private Task",
          "history": ["Knowledge Search; Problems; Private Task"]}]
    query = "workflow for ticket PRB052840832"

    svc = seeded_service
    res = svc.cortex_search(query, top_k=4, min_score=0.2, bm25=True)
    want = [e["entity"] for e in res["entries"]]
    # Knife-edge guard on the FIXTURE (2026-08-09): this test flapped on
    # CI because two fillers fused within ~1.2e-3 of each other — inside
    # cross-environment embedding-numerics noise, so the compared order
    # was decided by BLAS rounding, not by the fusion under test. The
    # lockstep assertion below needs well-separated scores to mean
    # anything; if a fixture edit re-creates a near-tie, fail HERE with
    # the pair named instead of flapping on CI.
    scores = [e["score"] for e in res["entries"]]
    for a, b, ea, eb in zip(scores, scores[1:], want, want[1:]):
        assert a - b > 0.005, (
            f"fixture knife-edge: {ea!r} ({a}) vs {eb!r} ({b}) fused "
            "within 0.005 — separate the filler facts, don't loosen "
            "the lockstep assertion")
    emb = svc._embedder  # same pipeline the service ranks with
    lines = rebuild_fact_lines(
        {"facts": facts, "question": query}, emb,
        top_k=4, min_score=0.2, bm25=True)
    got = [ln.split(" — ")[0] for ln in lines]
    assert got == want, f"rebuild={got} service={want}"


def test_rebuild_fact_ranking_matches_service_fusion_set_slot(
        pristine_service):
    """Task 6 extension of the lockstep guard above: a set-valued slot must
    collapse to ONE grouped entry identically on both paths. The bank's
    member facts carry ``"kind": "member"`` (what ``svc.cortex_dump()`` now
    emits for every current member row); the live side is seeded through
    ``svc.set_add`` so both paths embed the exact same
    ``f"{entity} {attribute} {value}"`` text.

    Asserts FULL composed-line equality, not just entity ordering + an
    unordered "all members present" check — a weaker assertion stayed
    green across a real live/offline divergence (review finding F1) and
    also stayed green on an earlier, less pointed choice of members/query
    for THIS test (member order happened to coincide with insertion order
    either way, so a naive full-line compare didn't red either). This
    scenario is deliberately engineered to force order-level divergence:

    At ``min_score=0.7`` only "hybrid bike" clears the DENSE floor; "gravel
    bike" and "road bike" both fall below it and can only enter the ranked
    pool through BM25 lexical-only injection — and they share ONE slot key.
    Their real BM25 raw scores (query repeats each member's code a
    different number of times: 3x / 2x / 1x) are genuinely different
    (verified empirically: hybrid=2.94, gravel=1.96, road=0.98), so the
    correct composed order is hybrid, gravel, road (score-descending).

    ``_cortex_bm25_fuse`` used to key its lexical-score dict by
    ``record.key`` (the SLOT identity, shared by gravel/road): since BM25
    hits are processed in score-descending order, the dict comprehension's
    "last write wins" semantics kept the LOWEST-scoring of the two
    (road) and silently evicted gravel — which then never entered the
    ranked hit list at all, falling to the composed value's unranked
    tail instead of its correct rank-2 position. Confirmed RED against the
    unfixed code (stashing the ``id(record)``-keying fix and rerunning this
    exact scenario): the live service produced ``hybrid bike SNHHH999;
    road bike SNRRR555; gravel bike SNGGG111 (3 members)`` — gravel bike
    demoted to last — while the offline rebuild path (whose OWN bm25 fusion
    was always keyed by fact index, never by slot) kept computing the
    correct ``hybrid; gravel; road`` order, so ``got_lines != want_lines``
    failed exactly as this test intends."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "evals"))
    from rebuild_contexts import rebuild_fact_lines

    query = ("recommend the best option for a beginner cyclist SNHHH999 "
             "SNHHH999 SNHHH999 SNGGG111 SNGGG111 SNRRR555")

    svc = pristine_service
    # One unrelated scalar in the corpus (nit, re-review): the isolated
    # 3-member-only universe above proved the order-divergence fix in
    # the tightest possible reproduction, but never exercised the
    # grouped entry's position AMONG scalars — restore one so the
    # grouping/lockstep logic is proven against a mixed corpus too, not
    # just a set-slot vacuum. Scores nowhere near this query (verified:
    # 0.0 dense/lexical), so it must NOT appear in the results — the
    # assertion below pins that the set entry still lands as the sole,
    # first-position entry despite scalar competition existing.
    svc.cortex_write("payments-db", "host", "10.0.0.7", provenance=["seed"])
    svc.set_add("user", "bikes owned", "road bike SNRRR555")
    svc.set_add("user", "bikes owned", "gravel bike SNGGG111")
    svc.set_add("user", "bikes owned", "hybrid bike SNHHH999")

    want_entries = svc.cortex_search(query, top_k=6, min_score=0.7,
                                     bm25=True)["entries"]
    set_entries = [e for e in want_entries if e.get("kind") == "set"]
    assert set_entries, "the set slot should have ranked for this query"
    assert want_entries[0]["kind"] == "set", (
        "the grouped set entry must be pinned at position 0 even with "
        "an (excluded) scalar in the corpus")
    # The full line each path SHOULD serve — entity, attribute, and the
    # (for a set) score-ordered composed value — not just the entity.
    want_lines = [f"{e['entity']} — {e['attribute']}: {e['value']}"
                  for e in want_entries]

    facts = [
        {"entity": "payments-db", "attribute": "host", "value": "10.0.0.7",
         "history": ["10.0.0.7"]},
    ] + [
        {"entity": "user", "attribute": "bikes owned", "value": member,
         "kind": "member"}
        for member in ("road bike SNRRR555", "gravel bike SNGGG111",
                        "hybrid bike SNHHH999")
    ]
    emb = svc._embedder
    got_lines = rebuild_fact_lines(
        {"facts": facts, "question": query}, emb,
        top_k=6, min_score=0.7, bm25=True)

    assert got_lines == want_lines, f"rebuild={got_lines} service={want_lines}"
    assert len(want_lines) == 1, (
        "the unrelated scalar filler must not have cleared the floor")
    # Pin the known-CORRECT order directly (not just live==offline
    # agreement) — a blind spot a cross-comparison alone cannot catch is
    # both paths being wrong in the same way.
    assert want_lines == [
        "user — bikes owned: hybrid bike SNHHH999; gravel bike SNGGG111; "
        "road bike SNRRR555 (3 members)"
    ], want_lines


def test_rebuild_fact_ranking_matches_service_fusion_set_slot_mixed_corpus(
        pristine_service):
    """Companion to the engineered-order-divergence scenario above
    (coordinator follow-up after Task 6 approval): that test deliberately
    isolates a 3-member-only universe at ``min_score=0.7`` to force the F1
    order-divergence reproduction, which trades away coverage of the set
    entry's position AMONG OTHER RANKED SCALARS — the ORIGINAL shape of
    this lockstep case, before it was narrowed for that reproduction.

    Restores it: the standard ``_seed`` corpus (5 filler facts + one
    identifier-style ticket fact) plus the same 3-bike set, queried at
    ``min_score=0.1`` (the shipped default) with ``bm25=True``. Verified
    empirically this yields 4 mixed entries on both paths — the grouped
    set entry at position 0, three scalars trailing it — so the lockstep
    equality here is doing real work interleaving a "kind": "set" entry
    among "kind"-less scalar entries, not just comparing two
    single-entry lists."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "evals"))
    from rebuild_contexts import rebuild_fact_lines

    query = "what bikes does the user own"

    svc = pristine_service
    _seed(svc)
    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_add("user", "bikes owned", "hybrid bike")

    want_entries = svc.cortex_search(query, top_k=6, min_score=0.1,
                                     bm25=True)["entries"]
    assert len(want_entries) == 4, want_entries
    assert want_entries[0]["kind"] == "set", (
        "the grouped set entry must rank at position 0")
    want_lines = [f"{e['entity']} — {e['attribute']}: {e['value']}"
                  for e in want_entries]

    facts = [
        {"entity": e, "attribute": a, "value": v, "history": [v]}
        for e, a, v in _FILLER
    ] + [{"entity": "ticket PRB052840832", "attribute": "workflow",
          "value": "Knowledge Search; Problems; Private Task",
          "history": ["Knowledge Search; Problems; Private Task"]}] + [
        {"entity": "user", "attribute": "bikes owned", "value": member,
         "kind": "member"}
        for member in ("road bike", "gravel bike", "hybrid bike")
    ]
    emb = svc._embedder
    got_lines = rebuild_fact_lines(
        {"facts": facts, "question": query}, emb,
        top_k=6, min_score=0.1, bm25=True)

    assert got_lines == want_lines, f"rebuild={got_lines} service={want_lines}"


def test_rebuild_fact_lines_legacy_bank_byte_identical(seeded_service):
    """Hard regression requirement (Task 6): a bank dumped before set slots
    existed carries no ``"kind"`` key on any fact — rebuild_fact_lines must
    treat every one of them as scalar and rebuild BYTE-IDENTICALLY to
    before the set-grouping branch was added. Pinned against a real dumped
    bank (a small, synthetic-persona LongMemEval fixture — no real user
    data — committed at ``tests/fixtures/rebuild_fact_lines_legacy_bank.json.gz``
    since ``evals/results/banks/`` itself is gitignored and would not
    survive a fresh checkout). Chosen specifically because one of its
    facts (``crash-course-videos-completed``, ``history: ["12", "15"]``)
    has a genuine 2-value supersession chain — the earlier version of this
    fixture had every fact at ``history`` length 1, so the "earlier
    values, oldest first" garnish branch (``older = versions[:-1]...``) was
    never actually exercised by this regression test at all (review
    finding F4).

    The expected lines below are honestly regenerated, not hand-preserved
    from before this fixture swap: captured by running THIS task's
    ``rebuild_fact_lines`` (i.e. current code, scalar branch byte-for-byte
    unchanged by the set-grouping addition) against this exact fixture —
    the scalar branch is untouched by Task 6, so this is equivalent to
    running the pre-set-feature code, just without needing to check out an
    earlier commit to prove it."""
    import gzip
    import json
    from pathlib import Path as _Path

    import sys as _sys
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "evals"))
    from rebuild_contexts import rebuild_fact_lines

    bank_path = (_Path(__file__).resolve().parent / "fixtures"
                / "rebuild_fact_lines_legacy_bank.json.gz")
    with gzip.open(bank_path, "rt", encoding="utf-8") as fh:
        bank = json.load(fh)
    assert all("kind" not in f for f in bank["facts"]), (
        "fixture must model a legacy (pre-Task-6) bank dump")
    assert any(len(f.get("history") or []) >= 2 for f in bank["facts"]), (
        "fixture must exercise the earlier-values garnish branch")

    # Only the embedder is needed — the bank under test comes from the
    # fixture file, not from the service — so this borrows the module's
    # seeded service rather than building one. It writes nothing.
    emb = seeded_service._embedder

    lines = rebuild_fact_lines(bank, emb, top_k=8, min_score=0.0, bm25=False)
    assert lines == [
        "user — crash-course-videos-completed: 15  "
        "(earlier values, oldest first: 12)",
        "user — Python programming course status: completed on edX",
        "user — current podcast interest: How I Built This",
        "user — AWS certification goal: AWS Certified Cloud Practitioner",
        "user — AWS project status: planned",
    ]
    # No line carries the set-grouping's "(N members)" marker — the
    # grouping branch never fired for this all-scalar bank.
    assert not any("members)" in ln for ln in lines)
    # ...and the earlier-values garnish DID fire, for exactly one fact.
    assert sum("earlier values" in ln for ln in lines) == 1
