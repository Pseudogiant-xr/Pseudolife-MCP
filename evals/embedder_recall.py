"""Compare embedding backbones on OUR corpus before swapping the backbone.

Swapping the bi-encoder is a schema migration: the pgvector columns are
declared ``vector(384)`` in four tables, every stored row must be
re-embedded, and every committed eval artifact's embeddings stop being
comparable. That is too much to spend on a literature claim, so this
measures the thing the swap is supposed to buy — can retrieval find the
answer — on the LongMemEval `s` questions we actually evaluate against.

Metric: recall@k of the turns LongMemEval marks ``has_answer``, ranking
every haystack turn of a question by cosine to the question text. Pure
retrieval, no reader, no judge, CPU-only.

BGE models are asymmetric and want an instruction prefix on the QUERY
only; omitting it understates them, so both variants are reported.

    python evals/embedder_recall.py --questions 30
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data"
RESULTS = Path(__file__).resolve().parent / "results"
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def load_questions(n: int) -> list[dict]:
    src = DATA / "longmemeval_s_cleaned.json"
    rows = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for q in rows:
        turns, gold = [], []
        for date, session in zip(q["haystack_dates"], q["haystack_sessions"]):
            for t in session:
                content = (t.get("content") or "").strip()
                if not content:
                    continue
                turns.append(f"[{date}] {t['role']}: {content}")
                gold.append(str(t.get("has_answer", "False")).lower() == "true")
        if any(gold):
            out.append({"qid": q["question_id"], "question": q["question"],
                        "turns": turns, "gold": np.array(gold)})
        if len(out) >= n:
            break
    return out


def recall_at(model, questions, ks, query_prefix=""):
    """Returns (recall_by_k, n_gold, per_gold_hits).

    ``per_gold_hits[k]`` is one bool per gold turn, in a stable order, so
    two arms can be compared with a PAIRED test. Report hits alongside
    rates: gold turns are scarce (roughly one per question), so a recall
    gap of a few points over a small slice can be a single document, and
    the rate alone hides that.
    """
    hits = {k: [] for k in ks}
    total = 0
    for q in questions:
        docs = model.encode(q["turns"], batch_size=64, show_progress_bar=False,
                            normalize_embeddings=True, convert_to_numpy=True)
        qv = model.encode([query_prefix + q["question"]],
                          normalize_embeddings=True, convert_to_numpy=True)[0]
        order = np.argsort(-(docs @ qv))
        gold_idx = sorted(np.where(q["gold"])[0].tolist())
        total += len(gold_idx)
        for k in ks:
            topk = set(order[:k].tolist())
            hits[k].extend(g in topk for g in gold_idx)
    return ({k: sum(hits[k]) / total for k in ks}, total,
            {k: hits[k] for k in ks})


def mcnemar(a: list[bool], b: list[bool]) -> tuple[int, int, float]:
    """Exact two-sided McNemar on paired hit vectors -> (b_only, a_only, p)."""
    from math import comb
    only_b = sum(1 for x, y in zip(a, b) if y and not x)
    only_a = sum(1 for x, y in zip(a, b) if x and not y)
    n = only_a + only_b
    if n == 0:
        return only_b, only_a, 1.0
    lo = min(only_a, only_b)
    tail = sum(comb(n, i) for i in range(lo + 1)) / (2 ** n)
    return only_b, only_a, min(1.0, 2 * tail)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", type=int, default=500)
    ap.add_argument("--with-prefix", action="store_true",
                    help="also run the BGE query-instruction arm")
    ap.add_argument("--ks", type=int, nargs="+", default=[5, 10, 20])
    ap.add_argument("--out", type=Path,
                    default=RESULTS / "embedder-recall-comparison.json")
    args = ap.parse_args()

    from sentence_transformers import SentenceTransformer

    questions = load_questions(args.questions)
    turns = sum(len(q["turns"]) for q in questions)
    print(f"{len(questions)} questions, {turns} haystack turns\n")

    arms = [
        ("all-MiniLM-L6-v2 (shipped)", "sentence-transformers/all-MiniLM-L6-v2", ""),
        ("bge-base-en-v1.5", "BAAI/bge-base-en-v1.5", ""),
    ]
    if args.with_prefix:
        # BGE is asymmetric and its card recommends an instruction prefix on
        # the query only. Whether it helps HERE is unmeasured — off by
        # default because it doubles the bge encode cost, on when you want
        # to settle it.
        arms.append(("bge-base-en-v1.5 (query prefix)",
                     "BAAI/bge-base-en-v1.5", BGE_QUERY_PREFIX))
    rows, hit_vectors = [], {}
    for label, repo, prefix in arms:
        model = SentenceTransformer(repo)
        t0 = time.perf_counter()
        rec, n_gold, per_gold = recall_at(model, questions, args.ks, prefix)
        dt = time.perf_counter() - t0
        hit_vectors[label] = per_gold
        rows.append({"arm": label, "model": repo, "query_prefix": bool(prefix),
                     "recall": {str(k): v for k, v in rec.items()},
                     "hits": {str(k): sum(per_gold[k]) for k in args.ks},
                     "encode_seconds": round(dt, 1),
                     "dim": model.get_sentence_embedding_dimension()})
        print(f"{label:32} " +
              "  ".join(f"R@{k}={rec[k]:.3f} ({sum(per_gold[k])}/{n_gold})"
                        for k in args.ks) +
              f"   ({dt:.0f}s, dim {rows[-1]['dim']})")

    # Paired significance against the shipped model. Aggregate rates over a
    # few dozen gold turns look decisive and are not.
    base = arms[0][0]
    tests = []
    print(f"\npaired McNemar vs {base} (n_gold={n_gold}):")
    for label, _, _ in arms[1:]:
        for k in args.ks:
            won, lost, p = mcnemar(hit_vectors[base][k], hit_vectors[label][k])
            tests.append({"arm": label, "k": k, "gained": won, "lost": lost,
                          "p_value": p})
            print(f"  {label:32} @{k:<3} +{won} -{lost}  p={p:.3f}"
                  + ("" if p < 0.05 else "   (not significant)"))

    args.out.write_text(json.dumps(
        {"questions": len(questions), "haystack_turns": turns,
         "gold_turns": n_gold, "arms": rows, "mcnemar_vs_shipped": tests},
        indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
