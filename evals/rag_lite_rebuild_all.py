"""Run ``rag_lite_rebuild`` against an all-six-types LongMemEval run.

``rag_lite_rebuild.py`` resolves its source and destination through
``longmemeval_bench.out_file`` with the default ``ku`` slug, so it can only
see knowledge-update runs. The 500-question sweeps carry the ``all`` slug
(``longmemeval-all-oracle-...``). This thin wrapper pins that slug for both
the source and the destination and otherwise defers to the tool unchanged,
so the byte-exact re-derivation guard still applies row by row.

Usage (from the repo root, PYTHONPATH=.):

  python evals/rag_lite_rebuild_all.py --dataset oracle --extractor qwen-27b \
      --src-tag alltypes-0803 --out-tag raglite-all-0803 \
      --rag-lite-top-k 1,2 --rag-budget-tokens 100,400 [--limit N]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import longmemeval_bench as lmb  # noqa: E402
import rag_lite_rebuild  # noqa: E402

_out_file = lmb.out_file


def _all_out_file(dataset: str, extractor: str, tag: str = "",
                  slug: str = "ku") -> Path:
    return _out_file(dataset, extractor, tag, slug="all")


if __name__ == "__main__":
    lmb.out_file = _all_out_file
    raise SystemExit(rag_lite_rebuild.main())
