"""Rebuild cortex/hybrid contexts offline from dumped fact banks under new
retrieval knobs, emitting a new tagged JSONL ready for ``--phase answer``.

The extract phase persists each question's served contexts at build time, so a
knob change normally needs a full (GPU-expensive) re-extract. But ``--tag
diag`` runs also dump the complete fact bank per question (with history
chains), and cortex search is plain cosine over
``encode_single(f"{entity} {attribute} {value}")`` — so the cortex arm's
context can be rebuilt EXACTLY offline. The rag context is copied verbatim;
the hybrid arm reuses its original raw-memories block verbatim and splices in
the rebuilt fact lines. EVERY arm's judge fields are stripped (comparator arms
included) so the answer phase re-runs the whole row, never a mix of fresh and
carried-over verdicts.

    python evals/rebuild_contexts.py                  # s/qwen-27b diag -> diag-knobs
    python evals/rebuild_contexts.py --top-k 24 --min-score 0.1

Then:
    python evals/longmemeval_bench.py --dataset s --extractor qwen-27b \
        --tag diag-knobs --phase answer

SCOPE — ASSOCIATIVE knobs are NOT covered here. This rebuilds the CORTEX
fact ranking only. Anything under ``memory.search`` (the candidate-pool
multiplier, the fusion mode) changes ``cms.retrieve``, whose output reaches
the ``rag`` context and the hybrid arm's raw-memory block — both of which
this script copies verbatim from the source run, because no band state was
dumped. ``evals/regression_gate.ps1`` runs this as its stage 1, so a GREEN
GATE SAYS NOTHING about those knobs: measuring them needs a full
``--phase extract`` with ``PSEUDOLIFE_BENCH_POOL_MULT`` /
``PSEUDOLIFE_BENCH_FUSION`` set (``ladder_sweep.apply_pool_env``).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from longmemeval_bench import (  # noqa: E402
    CORTEX_MIN_SCORE, CORTEX_TOP_K, bank_dir, load_rows, out_file,
    rewrite_rows,
)
from context_format import FACTS_HEADER, MEMS_HEADER  # noqa: E402
from replicate import is_judge_field  # noqa: E402


def strip_verdicts(row: dict) -> dict:
    """Clear EVERY arm's verdict in place so the answer phase re-runs.

    Not just the canonical three: a rebuilt row that kept a comparator
    arm's verdict would carry a stale judgement beside freshly rebuilt
    contexts, and an interrupted answer phase would then leave a file
    whose arms are judged over different row sets — which leak_check.py,
    unlike report(), does not filter out (2026-09-01 review).
    """
    for key in [k for k in row if is_judge_field(k)]:
        row.pop(key)
    return row


def rebuild_fact_lines(bank: dict, emb, top_k: int, min_score: float,
                       bm25: bool = False) -> list[str]:
    """Rank a dumped fact bank the way ``service.cortex_search`` would.

    Kept in lockstep with the service fusion by
    ``tests/test_cortex_bm25.py::test_rebuild_fact_ranking_matches_service_fusion``
    — the 2026-07-30 gate run passed the cortex-BM25 channel without
    executing it because this function had its own dense-only ranking.
    ``bm25=False`` mirrors the shipped ``BM25Config.cortex_enabled``
    default (the 2026-07-30 A/B measured no end-to-end benefit); lexical
    hits gate on the normalised ``bm25.min_score``, not the dense floor.

    Schema v35: the live ``cortex_search`` also pins in-scope
    CONSTRAINT-labelled facts ahead of this ranking. That step is NOT
    mirrored here, so the lockstep holds only for banks that carry no
    ``distortion_tolerance`` labels — which every committed bench bank
    is (the dumps predate v35, and the bench turn prefix defeats the
    auto heuristic). A labelled bank needs either
    ``memory.cortex.pin_constraints = false`` for the run or a measured
    fire rate beside the result.

    Task 6: a fact dict may carry an optional ``"kind"`` — ``"member"``
    marks one row of a set-valued slot; absent (every bank dumped before
    this change) means scalar, so a legacy bank ranks and composes lines
    exactly as before. Member rows that rank get grouped into ONE line per
    slot post-ranking (mirroring ``service.cortex_search``'s post-fusion
    grouping) via the shared :func:`compose_set_value` — the dumped bank
    only ever contains CURRENT facts (``cortex_dump``), so the "full
    membership" a group composes over is exactly the member rows present
    in ``facts``, same as the live store's ``members()``.
    """
    facts = bank["facts"]
    if not facts:
        return []
    from pseudolife_memory.memory.cortex import _norm_key, compose_set_value

    texts = [f"{f['entity']} {f['attribute']} {f['value']}".strip()
             for f in facts]
    mat = emb.encode(texts)                            # (n, d), normalized
    q = emb.encode_query(bank["question"])
    sims = (mat @ q).tolist()
    ranked = sorted((i for i, s in enumerate(sims) if s >= min_score),
                    key=lambda i: sims[i], reverse=True)[:top_k]
    score_map = {i: sims[i] for i in range(len(sims))}
    if bm25:
        from types import SimpleNamespace

        from pseudolife_memory.memory.bm25 import (BM25Index,
                                                   normalize_scores)
        from pseudolife_memory.utils.config import BM25Config
        cfg = BM25Config()
        idx = BM25Index([SimpleNamespace(text=t, i=i)
                         for i, t in enumerate(texts)],
                        k1=cfg.k1, b=cfg.b)
        norm = normalize_scores(idx.score(bank["question"], top_k=cfg.top_n))
        lex = {d.i: s for d, s in norm if s >= cfg.min_score}
        fused = [(i, sims[i] + cfg.weight * lex.get(i, 0.0)) for i in ranked]
        seen = set(ranked)
        fused += [(i, cfg.weight * s) for i, s in lex.items()
                  if i not in seen]
        fused.sort(key=lambda t: t[1], reverse=True)
        fused = fused[:top_k]
        ranked = [i for i, _ in fused]
        score_map = dict(fused)

    # Full current membership per slot, in original (insertion) order —
    # ``cortex_dump`` sorts its rows by (entity, attribute) but that sort is
    # stable, so member rows of one slot keep their relative order.
    member_idx: dict[tuple[str, str], list[int]] = {}
    for i, f in enumerate(facts):
        if f.get("kind") == "member":
            key = (_norm_key(f.get("entity", "")), _norm_key(f.get("attribute", "")))
            member_idx.setdefault(key, []).append(i)

    groups: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, object]] = []
    for i in ranked:
        f = facts[i]
        if f.get("kind") == "member":
            key = (_norm_key(f.get("entity", "")), _norm_key(f.get("attribute", "")))
            grp = groups.get(key)
            if grp is None:
                grp = {"entity": f.get("entity", ""), "attribute": f.get("attribute", ""),
                       "ranked": []}
                groups[key] = grp
                order.append(("set", key))
            grp["ranked"].append((i, score_map.get(i, 0.0)))
        else:
            order.append(("scalar", i))

    lines = []
    for tag, payload in order:
        if tag == "scalar":
            i = payload
            f = facts[i]
            line = (f"{f.get('entity', '')} — {f.get('attribute', '')}: "
                    f"{f.get('value', '')}")
            older = [v for v in (f.get("history") or [])[:-1]
                     if v and v != f.get("value")]
            if older:
                line += "  (earlier values, oldest first: " + " -> ".join(older) + ")"
            lines.append(line)
        else:
            key = payload
            grp = groups[key]
            idxs = member_idx.get(key, [])
            values = [facts[i].get("value", "") for i in idxs]
            pos_of = {global_i: pos for pos, global_i in enumerate(idxs)}
            ranked_pairs = [(pos_of[gi], sc) for gi, sc in grp["ranked"]
                            if gi in pos_of]
            value, _score = compose_set_value(values, ranked_pairs)
            lines.append(f"{grp['entity']} — {grp['attribute']}: {value}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="s")
    ap.add_argument("--extractor", default="qwen-27b")
    ap.add_argument("--src-tag", default="diag",
                    help="tag of the source run (must have dumped banks)")
    ap.add_argument("--out-tag", default="diag-knobs",
                    help="tag for the rebuilt JSONL")
    ap.add_argument("--top-k", type=int, default=CORTEX_TOP_K)
    ap.add_argument("--min-score", type=float, default=CORTEX_MIN_SCORE)
    ap.add_argument("--bm25", action="store_true",
                    help="opt into BM25 fact fusion (shipped default is "
                         "dense-only, matching bm25.cortex_enabled=False)")
    args = ap.parse_args()

    src = out_file(args.dataset, args.extractor, args.src_tag)
    banks = bank_dir(args.dataset, args.extractor, args.src_tag)
    dst = out_file(args.dataset, args.extractor, args.out_tag)
    rows = load_rows(src)
    if not rows:
        sys.exit(f"no rows in {src}")

    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig
    emb = EmbeddingPipeline(EmbeddingConfig(device="cpu"))

    out_rows = []
    for row in rows:
        bank_path = banks / f"{row['question_id']}.json.gz"
        with gzip.open(bank_path, "rt", encoding="utf-8") as fh:
            bank = json.load(fh)
        fact_lines = rebuild_fact_lines(bank, emb, args.top_k,
                                        args.min_score,
                                        bm25=args.bm25)
        raw_block = row["contexts"]["hybrid"].split(MEMS_HEADER, 1)[-1]
        row["contexts"]["cortex"] = "\n".join(fact_lines)
        row["contexts"]["hybrid"] = (FACTS_HEADER + "\n".join(fact_lines)
                                     + MEMS_HEADER + raw_block)
        strip_verdicts(row)              # every arm -> answer phase re-runs
        out_rows.append(row)

    rewrite_rows(dst, out_rows)
    print(f"rebuilt {len(out_rows)} rows -> {dst.name} "
          f"(top_k={args.top_k}, min_score={args.min_score})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
