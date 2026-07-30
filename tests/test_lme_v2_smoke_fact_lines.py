"""Pins evals/lme_v2_smoke.py's ``build_contexts_v2`` fact-line composition
(Task 6 review finding F2).

``build_contexts_v2`` hand-rolled its own copy of the fact-line loop instead
of reusing ``longmemeval_bench._compose_fact_line`` — so a set-slot entry
got the SCALAR "earlier values" garnish computed over the set-shaped
``history()`` output, relabeling currently-current members as if they were
superseded values. It already imported constants from ``longmemeval_bench``,
so this routes it through the same shared helper instead.

Pure-function test: a stub service (no GPU, no Postgres, no endpoints)
returns a fixed ``cortex_search``/``history`` response so the composed line
is deterministic and checkable without a real model.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import lme_v2_smoke as S  # noqa: E402


class _StubService:
    """Just enough of MemoryService's surface for build_contexts_v2."""

    def search(self, query, top_k=None, bm25=None, rerank=None):
        return {"entries": []}

    def cortex_search(self, query, top_k=None, min_score=None):
        return {"entries": [
            {"kind": "set", "entity": "user", "attribute": "bikes owned",
             "value": "gravel bike (1 members)", "score": 0.9,
             "contested": False},
        ]}

    def history(self, entity, attribute):
        return {
            "kind": "set", "entity": entity, "attribute": attribute,
            "count": 2,
            "versions": [
                {"value": "road bike", "event": "added", "at": 1.0},
                {"value": "gravel bike", "event": "added", "at": 2.0},
                {"value": "road bike", "event": "removed", "at": 3.0},
            ],
        }


def test_build_contexts_v2_uses_shared_fact_line_composer_for_set_entries():
    """A set entry's cortex/hybrid line must use the "former members"
    garnish (the shared ``_compose_fact_line`` idiom), never the scalar
    "earlier values" garnish that the old hand-rolled loop would have
    produced by walking the set-shaped ``history()`` as if it were a
    supersession chain."""
    svc = _StubService()
    contexts, _dump = S.build_contexts_v2(svc, "what bikes does the user own", {})

    assert contexts["cortex"] == (
        "user — bikes owned: gravel bike (1 members)  "
        "(former members: road bike)"
    )
    assert "earlier values" not in contexts["cortex"]
    assert contexts["cortex"] in contexts["hybrid"]
