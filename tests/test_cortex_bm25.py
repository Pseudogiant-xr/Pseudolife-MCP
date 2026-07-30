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

Like test_cortex_service.py this builds a real MemoryService (offline
embedder) against a throwaway data dir.
"""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService

# Filler facts so the BM25 index has a corpus with real IDF spread.
_FILLER = [
    ("dinner party menu", "selected side dishes", "kimchi and bokkeumbap"),
    ("basmati rice", "cooking tips", "soak before cooking, correct ratio"),
    ("user", "backup setup status", "completed"),
    ("payments-db", "host", "10.0.0.7"),
    ("deploy pipeline", "gate", "regression suite green"),
]


def _seed(svc: MemoryService) -> None:
    for e, a, v in _FILLER:
        svc.cortex_write(e, a, v, provenance=["seed"])
    # The target: an identifier-style token with no semantic neighbours.
    svc.cortex_write("ticket PRB052840832", "workflow",
                     "Knowledge Search; Problems; Private Task",
                     provenance=["seed"])


def test_bm25_serves_lexical_fact_the_dense_floor_drops():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        _seed(svc)
        # min_score=0.99: no dense cosine hit survives, so anything
        # returned came through the lexical channel.
        got = svc.cortex_search("redistribute PRB052840832", top_k=5,
                                min_score=0.99, bm25=True)["entries"]
        assert any("PRB052840832" in e["entity"] for e in got), got
        # Same call without the lexical channel: starved.
        got_off = svc.cortex_search("redistribute PRB052840832", top_k=5,
                                    min_score=0.99, bm25=False)["entries"]
        assert got_off == []


def test_bm25_cortex_defaults_off_even_when_turn_pool_is_on():
    """The 2026-07-30 pre-registered _s A/B failed (bm25-ab-confirmation.json:
    56/78 contexts changed, zero accuracy/commit-rate movement, ~1 question
    cost on the oracle gate slice), so the cortex-side channel ships OPT-IN:
    `memory.bm25.cortex_enabled = False` by default, independent of the turn
    pool's `enabled = True`."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        _seed(svc)
        assert svc.config.memory.bm25.enabled is True          # turn pool on
        assert svc.config.memory.bm25.cortex_enabled is False  # facts off
        # Default call: dense only — the lexical fact channel must not fire.
        assert svc.cortex_search("redistribute PRB052850000 PRB052840832",
                                 top_k=5, min_score=0.99)["entries"] == []
        # Config opt-in turns it on without a per-call override.
        svc.config.memory.bm25.cortex_enabled = True
        got = svc.cortex_search("redistribute PRB052850000 PRB052840832",
                                top_k=5, min_score=0.99)["entries"]
        assert any("PRB052840832" in e["entity"] for e in got)
        # Per-call False overrides config True (tri-state preserved).
        assert svc.cortex_search("redistribute PRB052850000 PRB052840832",
                                 top_k=5, min_score=0.99,
                                 bm25=False)["entries"] == []


def test_bm25_boost_raises_score_of_lexical_match():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        _seed(svc)
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


def test_bm25_entries_keep_cortex_shape():
    """Lexically-injected entries carry the same dict shape as dense hits
    (entity/attribute/value/score/contested), so consumers cannot tell
    the channels apart structurally."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        _seed(svc)
        got = svc.cortex_search("PRB052840832", top_k=3, min_score=0.99,
                                bm25=True)["entries"]
        assert got, "lexical channel should have served the identifier fact"
        entry = got[0]
        for key in ("entity", "attribute", "value", "score", "contested"):
            assert key in entry, f"missing {key!r} in {entry}"


def test_rebuild_fact_ranking_matches_service_fusion():
    """Lockstep guard: evals/rebuild_contexts.py re-implements cortex fact
    ranking offline (it ranks dumped banks, not a live store). The 2026-07-30
    regression-gate run proved why this must be pinned: the gate 'passed'
    the BM25 channel without ever executing it, because the rebuild had its
    own dense-only ranking. Any fusion change must land in both places or
    this test goes red."""
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

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        _seed(svc)
        want = [e["entity"] for e in svc.cortex_search(
            query, top_k=4, min_score=0.2, bm25=True)["entries"]]
        emb = svc._embedder  # same pipeline the service ranks with
        lines = rebuild_fact_lines(
            {"facts": facts, "question": query}, emb,
            top_k=4, min_score=0.2, bm25=True)
        got = [ln.split(" — ")[0] for ln in lines]
    assert got == want, f"rebuild={got} service={want}"


def test_rebuild_fact_ranking_matches_service_fusion_set_slot():
    """Task 6 extension of the lockstep guard above: a set-valued slot must
    collapse to ONE grouped entry identically on both paths. The bank's
    member facts carry ``"kind": "member"`` (what ``svc.cortex_dump()`` now
    emits for every current member row); the live side is seeded through
    ``svc.set_add`` so both paths embed the exact same
    ``f"{entity} {attribute} {value}"`` text."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "evals"))
    from rebuild_contexts import rebuild_fact_lines

    query = "what bikes does the user own"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        _seed(svc)
        svc.set_add("user", "bikes owned", "road bike")
        svc.set_add("user", "bikes owned", "gravel bike")
        svc.set_add("user", "bikes owned", "hybrid bike")

        want_entries = svc.cortex_search(query, top_k=6, min_score=0.1,
                                         bm25=True)["entries"]
        set_entries = [e for e in want_entries if e.get("kind") == "set"]
        assert set_entries, "the set slot should have ranked for this query"
        want = [e["entity"] for e in want_entries]

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
        lines = rebuild_fact_lines(
            {"facts": facts, "question": query}, emb,
            top_k=6, min_score=0.1, bm25=True)
        got = [ln.split(" — ")[0] for ln in lines]

    assert got == want, f"rebuild={got} service={want}"
    # The grouped set line itself must carry the composed multi-member value
    # (score-descending, full membership) on the rebuild side too — not just
    # entity ordering.
    set_lines = [ln for ln in lines if ln.startswith("user — bikes owned")]
    assert len(set_lines) == 1
    assert set_lines[0].endswith("(3 members)")
    for member in ("road bike", "gravel bike", "hybrid bike"):
        assert member in set_lines[0]


def test_rebuild_fact_lines_legacy_bank_byte_identical():
    """Hard regression requirement (Task 6): a bank dumped before set slots
    existed carries no ``"kind"`` key on any fact — rebuild_fact_lines must
    treat every one of them as scalar and rebuild BYTE-IDENTICALLY to
    before the set-grouping branch was added. Pinned against a real dumped
    bank (a small, synthetic-persona LongMemEval fixture — no real user
    data — committed at ``tests/fixtures/rebuild_fact_lines_legacy_bank.json.gz``
    since ``evals/results/banks/`` itself is gitignored and would not
    survive a fresh checkout). The expected lines below were captured by
    running this exact function against this exact fixture BEFORE the
    set-slot grouping branch existed."""
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

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc._ensure_init()
        emb = svc._embedder

    lines = rebuild_fact_lines(bank, emb, top_k=8, min_score=0.0, bm25=False)
    assert lines == [
        "user's postcard collection — total new additions since restarting: 25 postcards",
        "user's postcard collection — recent acquisition count: 8 postcards",
        "user's postcard collection — planned categorization method: by theme",
        "user's postcard collection — planned initial display method: simple postcard rack",
        "user's vintage camera collection — planned display combination: mix of "
        "wall-mounted shelves and glass-top display cases",
    ]
    # No line carries the set-grouping's "(N members)" marker — the
    # grouping branch never fired for this all-scalar bank.
    assert not any("members)" in ln for ln in lines)
