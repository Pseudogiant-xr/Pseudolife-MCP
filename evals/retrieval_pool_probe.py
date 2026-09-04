#!/usr/bin/env python
"""Offline retrieval-proxy probe for the candidate-pool / fusion knobs.

**This is a proxy, not a verdict.** It measures whether the gold-bearing
turn reaches the served window under each knob setting — nothing about
whether an answerer then gets the question right. The judged verdict
is a full ``--phase extract`` run with the sanctioned env overrides, NOT
``evals/regression_gate.ps1`` — its stage 1 cannot reach ``memory.search``
(see "Scope warning" in ``evals/README.md``). That run happened on
2026-09-04 and the knobs lost; they ship OFF.

What it does
------------
Ingests the synthetic knowledge-update corpus from :mod:`ladder_sweep`
(10 update-pairs + 6 distractors, ingested initials → distractors →
updates, so the gold turn is always the most recent statement and the
stale one is buried mid-stream) into an IN-MEMORY
:class:`~pseudolife_memory.memory.cms.ContinuumMemorySystem`, then runs
each pair's question under the full grid::

    candidate_pool_multiplier in {1, 4}
    x fusion in {weighted_sum, rrf}
    x reranker in {off, on}

and reports, per cell:

* ``recall_at_6`` — fraction of the 10 questions whose top-6 contains the
  GOLD-BEARING turn (the ``update`` statement that carries the current
  value). Containment is by :func:`ladder_sweep.value_present`, the same
  word-boundary match the ladder scores with — the synthetic analogue of
  LongMemEval's per-turn ``has_answer`` marker.
* ``stale_leak`` — fraction whose top-6 still contains the superseded
  value. A recall gain paid for entirely in stale leak is not a gain.
* ``latency_ms`` — mean wall-clock per ``retrieve()`` call.

Why not the LongMemEval banks
-----------------------------
``evals/results/banks/`` is gitignored and absent from a fresh worktree,
and none of its dumps can replay this path anyway: ``dump_bank`` persists
CORTEX FACTS only (``source_entries`` stripped), which reconstructs
``cortex_search`` but not ``cms.retrieve``. The one band-state dump
(``band_ablation.py`` replay, ``banks/s-qwen-27b-ablbands-flat``) carries
384-d embeddings from the retired MiniLM backbone and no gold-turn labels,
so it cannot be scored for recall against today's embedder either. The
synthetic corpus is the honest fallback, and it is small (10 gold queries
over 26 turns) — read the numbers as direction, not magnitude.

No GPU, no Postgres, no judge, no network. Writes its artifact by default.

    python evals/retrieval_pool_probe.py
    python evals/retrieval_pool_probe.py --out evals/results/my-run.json
"""
from __future__ import annotations

import os

# CPU + offline before torch/service import, same as every other eval here.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

from ladder_sweep import DISTRACTORS, PAIRS, value_present  # noqa: E402

TOP_K = 6
MULTIPLIERS = (1, 4)
FUSIONS = ("weighted_sum", "rrf")
# Real conversational turns to bury the gold in. These are the band-state
# dumps band_ablation.py writes; only their TEXT is used (their 384-d
# embeddings are from the retired MiniLM backbone and are re-encoded here
# with the current one). Gitignored, so a worktree has to copy the
# directory from the main checkout — absent, the probe runs synthetic-only
# and says so in the artifact.
HAYSTACK_DIR = Path("evals/results/banks/s-qwen-27b-ablbands-flat")
HAYSTACK_N = 400


def _haystack(root: Path, want: int) -> list[str]:
    """Background turns from the band-state dumps, oldest-file-first and
    de-duplicated, so the ladder's gold statements sit in a bank of
    realistic size instead of a 26-entry toy."""
    import gzip

    if want <= 0 or not root.is_dir():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            dump = json.load(fh)
        for band in dump.get("bands", []):
            for entry in band.get("entries", []):
                text = (entry.get("text") or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
                    if len(out) >= want:
                        return out
    return out


def _build_corpus(haystack: list[str]) -> list[str]:
    """Ladder ingest order: every initial, then the distractors (plus the
    haystack), then every update. The gold statement is always
    last-written and the stale one is buried mid-stream, which is the
    ordering the knowledge-update task is about."""
    return ([p["initial"] for p in PAIRS]
            + list(DISTRACTORS) + list(haystack)
            + [p["update"] for p in PAIRS])


def _make_reranker():
    """Return a loaded cross-encoder, or None when the model is not cached
    (this probe never reaches the network)."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker
    rr = CrossEncoderReranker()
    try:
        if not rr.rerank("probe", ["probe candidate"]):
            return None
    except Exception:
        return None
    return rr


def _run_cell(dim, corpus, q_embs, *, multiplier: int, fusion: str,
              reranker) -> dict:
    """One grid cell. Embeddings are precomputed and shared across cells —
    encoding a 400-turn haystack eight times would dominate the runtime and
    measure the embedder, not the fusion."""
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.utils.config import MemoryConfig

    cfg = MemoryConfig(embedding_dim=dim)
    cfg.search.candidate_pool_multiplier = multiplier
    cfg.search.fusion = fusion
    cfg.reranker.enabled = reranker is not None
    cms = ContinuumMemorySystem(cfg, reranker=reranker)
    for text, emb in corpus:
        cms.store(text, emb, source="user")

    gold = stale = 0
    lat: list[float] = []
    misses: list[str] = []
    served: dict[str, list[str]] = {}
    for pair in PAIRS:
        q = pair["question"]
        t0 = time.perf_counter()
        res = cms.retrieve(q_embs[q], top_k=TOP_K, query_text=q)
        lat.append((time.perf_counter() - t0) * 1000.0)
        texts = [e.text for e in res.entries]
        served[q] = texts
        if any(value_present(t, pair["gold"]) for t in texts):
            gold += 1
        else:
            misses.append(pair["question"])
        if any(value_present(t, pair["stale"]) for t in texts):
            stale += 1

    n = len(PAIRS)
    return {
        "multiplier": multiplier,
        "fusion": fusion,
        "reranker": "on" if reranker is not None else "off",
        "recall_at_6": round(gold / n, 3),
        "stale_leak": round(stale / n, 3),
        "latency_ms": round(sum(lat) / len(lat), 2),
        "misses": misses,
        "_served": served,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None,
                    help="artifact path (default: "
                         "evals/results/retrieval-pool-probe-<today>.json)")
    ap.add_argument("--haystack", type=int, default=HAYSTACK_N,
                    help="background turns to bury the gold in (0 = the "
                         "26-entry synthetic corpus alone)")
    ap.add_argument("--haystack-dir", default=str(HAYSTACK_DIR),
                    help="band-state dump directory, repo-relative")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    out = Path(args.out) if args.out else (
        repo / "evals" / "results"
        / f"retrieval-pool-probe-{date.today():%Y%m%d}.json")
    hay_dir = Path(args.haystack_dir)
    if not hay_dir.is_absolute():
        hay_dir = repo / hay_dir

    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig
    embedder = EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    dim = embedder.embedding_dim

    haystack = _haystack(hay_dir, args.haystack)
    if args.haystack and not haystack:
        print(f"no band-state dumps under {hay_dir} — synthetic corpus only",
              file=sys.stderr)
    texts = _build_corpus(haystack)
    print(f"encoding {len(texts)} turns + {len(PAIRS)} queries…",
          file=sys.stderr)
    corpus = [(t, embedder.encode_single(t)) for t in texts]
    q_embs = {p["question"]: embedder.encode_query(p["question"])
              for p in PAIRS}

    reranker = _make_reranker()
    if reranker is None:
        print("cross-encoder unavailable offline — reranker arms skipped",
              file=sys.stderr)

    cells: list[dict] = []
    for multiplier in MULTIPLIERS:
        for fusion in FUSIONS:
            for rr in ([None, reranker] if reranker is not None else [None]):
                cell = _run_cell(dim, corpus, q_embs, multiplier=multiplier,
                                 fusion=fusion, reranker=rr)
                cells.append(cell)
                print(f"mult={cell['multiplier']} fusion={cell['fusion']:<12} "
                      f"rerank={cell['reranker']:<3} "
                      f"recall@{TOP_K}={cell['recall_at_6']:.3f} "
                      f"stale={cell['stale_leak']:.3f} "
                      f"{cell['latency_ms']:.1f}ms")

    # Served-set churn against the shipped cell. Recall can be flat while
    # the knobs still change WHICH turns are served — that difference is
    # what a judged gate would score, and a flat recall with zero churn
    # would instead mean the knob did nothing at all.
    base = next(c for c in cells if c["multiplier"] == 1
                and c["fusion"] == "weighted_sum" and c["reranker"] == "off")
    for cell in cells:
        changed = sum(
            len(set(cell["_served"][q]) ^ set(base["_served"][q]))
            for q in base["_served"])
        total = sum(len(v) for v in base["_served"].values())
        cell["churn_vs_shipped"] = round(changed / (2 * total), 3)
    for cell in cells:
        cell.pop("_served", None)

    payload = {
        "probe": "retrieval-pool-probe",
        "date": f"{date.today():%Y-%m-%d}",
        "caveat": (
            "Retrieval proxy, NOT a verdict: recall@6 of the gold-bearing "
            "turn in the served window, on a 10-question synthetic corpus "
            "buried in real conversational turns. It says nothing about "
            "answered accuracy. These knobs are decided by a judged "
            "--phase extract run, not by evals/regression_gate.ps1, whose "
            "stage 1 cannot reach memory.search; the 2026-09-04 judged run "
            "went against them and they ship OFF."),
        "corpus": {
            "source": "evals/ladder_sweep.py PAIRS + DISTRACTORS",
            "haystack_source": (str(hay_dir.relative_to(repo))
                                if haystack else None),
            "haystack_turns": len(haystack),
            "entries": len(texts),
            "questions": len(PAIRS),
            "why_not_longmemeval_gold": (
                "No dump in evals/results/banks/ can score recall over "
                "cms.retrieve(): dump_bank persists cortex facts only (no "
                "turns, source_entries stripped), and the band-state dumps "
                "(band_ablation.py replay) carry no gold-turn labels — the "
                "has_answer markers live in the LongMemEval dataset, not in "
                "the dump. Their TURN TEXT is reused here as a realistic "
                "haystack, re-encoded with the current backbone; their own "
                "384-d MiniLM vectors are not used."),
        },
        "embedder": {"dim": dim, "model": EmbeddingConfig().model_name},
        "top_k": TOP_K,
        "reranker_available": reranker is not None,
        "cells": cells,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
