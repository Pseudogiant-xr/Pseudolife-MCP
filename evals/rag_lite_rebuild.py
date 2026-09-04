"""Add the token-matched rag arms to an ALREADY-EXTRACTED LongMemEval run.

Neither existing path can do this, which is worth stating plainly because
both look like they should:

  * ``--phase answer`` only answers the context keys a row already
    persisted, and only for rows that are not yet judged. It cannot mint
    an arm that was never served.
  * ``rebuild_contexts.py`` rebuilds the cortex and hybrid arms from the
    dumped fact banks, but copies the rag context VERBATIM — and the
    dumps hold facts only, so the rag arm's ranked turn list is not in
    them. Splitting the persisted rag block back into turns does not
    recover it either: turn texts contain blank lines, so only 6 of the
    78 ceiling-v38 rows split into the 6 turns that were served.

What IS recoverable is the ranking itself: the rag arm is a plain cosine
search over the raw turns, the haystack is static, and re-ingesting it
costs the CPU embedder and no extractor at all. So this tool re-ingests
each question's turns, re-runs the control's pinned search, and REFUSES
to write unless the re-derived rag context matches the judged one byte
for byte — the arms it adds are prefixes of the control that was actually
scored, or the run does not happen.

    PYTHONPATH=. python evals/rag_lite_rebuild.py --dataset oracle \\
        --extractor qwen-27b --src-tag ceiling-v38 --out-tag raglite-v38 \\
        --rag-lite-top-k 1,2 --rag-budget-tokens 100,400

``--slug all`` points both the source and the destination at the
500-question six-type family (``longmemeval-all-...``) instead of the
78-question knowledge-update one.

Then answer the rebuilt tag (this is the only GPU step):

    PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle \\
        --extractor qwen-27b --tag raglite-v38 --phase answer

Every arm's verdict is stripped, so that answer phase re-judges the whole
row — the rag/cortex/hybrid arms included. That is deliberate: a
within-run paired comparison needs every arm judged by one instrument in
one pass, and on the reproducible server config the carried-over arms
re-score identically.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")               # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from ladder_sweep import build_service  # noqa: E402
import longmemeval_bench as lmb  # noqa: E402
from replicate import is_judge_field  # noqa: E402


def question_turns(q: dict) -> list[str]:
    """The stored turn texts, in ingest order.

    Read through ``archive_from_lme_question`` rather than re-formatting
    them here: that function is already pinned turn-for-turn against
    ``ingest_and_dream`` by test_lme_comparator_arms, so this tool cannot
    drift from what the original run stored.
    """
    return [r.text for r in lmb.archive_from_lme_question(q).records]


def rederive_raw_texts(q: dict) -> list[str]:
    """The rag control's ranked turns for one question, re-derived offline.

    Same pinned search call ``build_contexts`` makes for the control
    (Phase-1 knobs off), over a fresh bench service holding the same
    turns. No extractor: the rag arm never touched one.
    """
    tmp = Path(tempfile.mkdtemp(prefix="raglite_"))
    svc = build_service(tmp)
    try:
        for text in question_turns(q):
            svc.store(text, source="bench")
        got = svc.search(q["question"], top_k=lmb.RAG_TOP_K,
                         contiguity_neighbors=0, timeline=False)
        return [e.get("text", "") for e in got.get("entries", [])]
    finally:
        svc.flush()
        # One bank per question, and a 500-row rebuild would otherwise
        # leave 500 of them behind in the temp area. ignore_errors because
        # a bank the service still holds open must not fail the rebuild —
        # the run's result is the artifact, not the scratch directory.
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default="oracle")
    ap.add_argument("--extractor", default="qwen-27b")
    # The knowledge-update runs carry the ``ku`` slug and the 500-question
    # six-type sweeps carry ``all``. This was a separate wrapper module
    # that monkeypatched ``lmb.out_file`` globally (untested, and a
    # half-applied patch would read one family and write the other); it is
    # one option applied to BOTH ends instead.
    ap.add_argument("--slug", default="ku", choices=("ku", "all"),
                    help="run family: ku (78 knowledge-update questions, "
                         "the default) or all (the 500-question six-type "
                         "sweeps)")
    ap.add_argument("--src-tag", required=True)
    ap.add_argument("--out-tag", required=True)
    ap.add_argument("--rag-lite-top-k", default=None,
                    help="comma-separated narrower budgets, e.g. '1,2'")
    # A LIST here, unlike the two harness CLIs (which take one int): this
    # tool's whole job is composing arms onto an existing run, and every
    # extra budget added in a second pass costs another full CPU re-ingest
    # of the haystack for nothing.
    ap.add_argument("--rag-budget-tokens", default=None,
                    help="comma-separated token budgets, e.g. '100,400': "
                         "adds arms ragb100, ragb400")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N rows (a fidelity smoke; every row "
                         "written is stamped partial=true, which the "
                         "bench's --report carries into the summary as "
                         "partial: true, and the progress denominator is "
                         "the limited slice — so neither the short file "
                         "nor its summary can be mistaken for a complete "
                         "run)")
    args = ap.parse_args(argv)

    top_ks = lmb.parse_rag_lite_top_ks(args.rag_lite_top_k)
    budgets = lmb.parse_rag_lite_top_ks(args.rag_budget_tokens,
                                        "--rag-budget-tokens")
    for budget in budgets or (None,):
        lmb.validate_rag_lite(top_ks, budget, lmb.RAG_TOP_K)
    if not top_ks and not budgets:
        raise SystemExit("nothing to add — pass --rag-lite-top-k and/or "
                         "--rag-budget-tokens")
    if args.src_tag == args.out_tag:
        raise SystemExit("--out-tag must differ from --src-tag; never "
                         "overwrite a canonical result file")

    src = lmb.out_file(args.dataset, args.extractor, args.src_tag,
                       slug=args.slug)
    dst = lmb.out_file(args.dataset, args.extractor, args.out_tag,
                       slug=args.slug)
    if dst.exists():
        raise SystemExit(f"refusing to overwrite {dst.name}")
    rows = lmb.load_rows(src)
    if not rows:
        raise SystemExit(f"no rows in {src}")
    by_id = {q["question_id"]: q
             for q in lmb.load_questions(args.dataset, lmb.ALL_TYPES)}

    out_rows, mismatched = [], []
    t0 = time.perf_counter()
    # The denominator is the slice actually being rebuilt: under --limit
    # the run never intends to reach len(rows), and a "[1/78]" line on a
    # 5-row smoke reads as a stall rather than a plan.
    todo = rows[:args.limit] if args.limit else rows
    for i, row in enumerate(todo, 1):
        qid = row["question_id"]
        q = by_id.get(qid)
        if q is None:
            raise SystemExit(f"{qid} is not in the {args.dataset} dataset")
        raw_texts = rederive_raw_texts(q)
        if "\n\n".join(raw_texts) != row["contexts"]["rag"]:
            # Fail on the FIRST mismatch rather than tallying: one row that
            # re-derives differently already means the retrieval stack has
            # moved, and the alternative is paying ~25 CPU-minutes to learn
            # the same thing with a count attached.
            mismatched.append(qid)
            break
        row["contexts"].update(lmb.rag_lite_contexts(raw_texts, top_ks, None))
        for budget in budgets:
            row["contexts"].update(
                lmb.rag_lite_contexts(raw_texts, (), budget))
        for key in [k for k in row if is_judge_field(k)]:
            row.pop(key)                 # every arm re-judged in one pass
        if args.limit:
            # A limited rebuild writes a SHORT file under a perfectly
            # normal name; nothing downstream could otherwise tell it
            # apart from a complete run.
            row["partial"] = True
        out_rows.append(row)
        print(f"[{i}/{len(todo)}] {qid} ok", flush=True)

    if mismatched:
        # Loud, and nothing written: arms that are not prefixes of the
        # judged control measure retrieval drift, not budget.
        raise SystemExit(
            f"{mismatched[0]} re-derived a DIFFERENT rag context than the "
            f"one that was judged (row {len(out_rows) + 1} of "
            f"{len(todo)}). "
            "The retrieval stack has moved since that run, so these arms "
            "would measure drift, not budget — re-extract instead of "
            "rebuilding.")

    lmb.rewrite_rows(dst, out_rows)
    added = lmb.rag_lite_arm_names(top_ks, None) + tuple(
        name for b in budgets for name in lmb.rag_lite_arm_names((), b))
    print(f"rebuilt {len(out_rows)} rows -> {dst.name} "
          f"(+{', '.join(added)}; every rag context re-derived byte-exact; "
          f"{round(time.perf_counter() - t0, 1)}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
