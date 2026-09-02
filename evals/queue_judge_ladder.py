"""Review-queue judge ladder: can arm X reproduce the ratified panel on
every queue the daemon now judges autonomously?

Runs the SHIPPED judge code paths (``OpenAICompatExtractor.judge_merges /
judge_links / judge_junk / judge_candidates / judge_slot_pairs`` — same
system prompts, same row serialization, same batch size and token budget
as the sweep) against ``evals/data/queue_judge_eval_20260902.json`` and
scores each queue against the blind-panel labels, then simulates what the
shipped auto modes would have applied at the configured gates.

The evidence pack is PRIVATE: it freezes memory-bank text (hostnames,
project scopes, session notes) and lives outside the tree under the
gitignored ``evals/data/``. What is committed is its scrubbed derivative
``evals/results/queue-judge-panel-20260902.json`` (labels, gate table,
per-row votes — no bank text) and this harness's output artifact, whose
per-row records likewise carry verdicts only. Replicates are judged in a
different (seeded) row order each, so replicate 2 is a genuine second
opinion — the same independence the sweep gets from re-batching.

Per queue (majority vote across replicates):
  * merges     — accept/reject precision; single-vote auto-reject at
                 ``--reject-gate``; with ``--replicates >= 2`` the two-vote
                 simulation: rejects at mean >= ``--reject-gate-2`` and
                 accepts on non-low-differential rows at mean >=
                 ``--accept-gate`` (replicates 1 and 2 stand in for the
                 sweep's first + second opinion).
  * links      — accept / reject precision (retype must name the panel's
                 relation), auto-accept + auto-reject at ``--link-gate``.
  * junk       — keep / delete precision; auto-delete at ``--junk-gate``
                 UNDER the evidence bar (degree <= 2, facts <= 1 — the
                 shipped junk_max_auto_degree) — the daemon never deletes
                 above it. The bar is structural, not a precision claim:
                 16 of the 20 panel rows pass it, including 5 keeps.
  * candidates — propose / dismiss precision at ``--candidate-gate``.
  * curation   — distinct / duplicate precision at the curation gates.

Usage (one arm per invocation; results append into one JSON artifact):
    python evals/queue_judge_ladder.py --arm opus --base-url http://127.0.0.1:8082/v1 \
        --model claude-opus-5 [--queues merges,links,junk,candidates,curation] \
        [--replicates 1] [--batch 8] [--out evals/results/queue-judge-ladder-20260902.json]

Persists by default (a bench that only prints was never measured).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
from pseudolife_memory.memory.dream import (  # noqa: E402
    ExtractorError, OpenAICompatExtractor,
)

DATA = Path(__file__).parent / "data" / "queue_judge_eval_20260902.json"
DEFAULT_OUT = Path(__file__).parent / "results" / "queue-judge-ladder-20260902.json"
QUEUES = ("merges", "links", "junk", "candidates", "curation")


def _split(s, sep):
    return [x.strip() for x in str(s or "").split(sep) if x.strip()]


# ── row shaping: the frozen evidence pack -> the shipped judge inputs ──────

# The merge judge's per-snippet evidence cap. None = the frozen 240-char
# serialization (every published number's exact prompt), which is ALSO the
# shipped deep_dream.judge_snippet_max_chars default (read it from
# utils/config.py, never from a note). Pass --snippet-chars only to measure
# a different cap on a pack whose snippets were built at full length
# (evals/queue_judge_fulllen_pack.py) — the 2026-09-03 run at 3000 is
# evals/results/queue-judge-ladder-20260903-fulllen.json.
SNIPPET_CHARS: int | None = None


def shape_merge(r, n):
    out = {"n": n, "from": r["from"], "into": r["into"], "reason": r.get("reason"),
           "score": r.get("score"), "low_differential": r.get("low_differential")}
    if SNIPPET_CHARS is not None:
        out["snippet_chars"] = SNIPPET_CHARS
    return out


def shape_link(r, n):
    return {"n": n, "src": r["src"], "relation": r["relation"], "dst": r["dst"],
            "rationale": r.get("rationale"),
            "src_edges": (_split(r.get("src_out"), " ; ") + _split(r.get("src_in"), " ; "))[:8],
            "dst_edges": (_split(r.get("dst_out"), " ; ") + _split(r.get("dst_in"), " ; "))[:8],
            "src_scopes": _split(r.get("src_scopes"), ","),
            "dst_scopes": _split(r.get("dst_scopes"), ","),
            "co_mentions": r.get("co_mentions") or [],
            "src_mentions": r.get("src_mentions") or [],
            "dst_mentions": r.get("dst_mentions") or []}


def shape_junk(r, n):
    edges = _split(r.get("edges"), " ; ")
    lesson_object = bool(edges) and all(
        ("-prefers->" in e or "-avoids->" in e) and "[action]" in e for e in edges)
    return {"n": n, "display": r["display"], "reason": r.get("reason"),
            "degree": int(r.get("deg") or 0), "edges": edges[:8],
            "facts": int(r.get("facts") or 0),
            "fact_text": _split(r.get("fact_text"), " ; ")[:3],
            "lesson_object": lesson_object,
            "scopes": _split(r.get("sources"), ","),
            "mentions": r.get("mentions") or []}


def shape_candidate(r, n):
    return {"n": n, "src": r["src"], "dst": r["dst"], "similarity": r.get("similarity"),
            "src_snippets": r.get("src_snippets") or [],
            "dst_snippets": r.get("dst_snippets") or []}


def shape_slot(r, n):
    return {"n": n, "store": r["store"], "similarity": r.get("similarity"),
            "a": r["a"], "b": r["b"], "a_key": r["a_key"], "b_key": r["b_key"]}


SHAPE = {"merges": (shape_merge, "judge_merges"),
         "links": (shape_link, "judge_links"),
         "junk": (shape_junk, "judge_junk"),
         "candidates": (shape_candidate, "judge_candidates"),
         "curation": (shape_slot, "judge_slot_pairs")}


def run_replicate(ex, queue, rows, batch, seed=None):
    """One pass; ``seed`` shuffles the ROW ORDER (hence batch composition)
    so a second replicate sees each row among different neighbours —
    the independence the sweep gets from re-batching across ticks."""
    import random
    shape, method = SHAPE[queue]
    order = list(range(len(rows)))
    if seed is not None:
        random.Random(seed).shuffle(order)
    out: list[dict | None] = [None] * len(rows)
    for start in range(0, len(order), batch):
        idx = order[start:start + batch]
        shaped = [shape(rows[i], k + 1) for k, i in enumerate(idx)]
        try:
            verdicts = getattr(ex, method)(shaped)
        except ExtractorError as exc:
            print(f"  {queue} batch {start // batch}: FAILED ({exc}) — rows skipped", flush=True)
            continue
        for v in verdicts:
            out[idx[v["n"] - 1]] = v
    return out


def _vote_only(v):
    """A per-row vote stripped of model prose (notes/rationales can quote
    bank text) — the committed artifact carries verdicts, not evidence."""
    if not v:
        return None
    return {k: v[k] for k in ("verdict", "confidence", "relation", "keep")
            if k in v and v[k] is not None}


def majority(votes):
    votes = [v for v in votes if v]
    if not votes:
        return None
    counts = collections.Counter(v["verdict"] for v in votes)
    top, n = counts.most_common(1)[0]
    if n * 2 <= len(votes):
        return {"verdict": "leave", "confidence": 0.0}
    agreeing = [v for v in votes if v["verdict"] == top]
    merged = dict(agreeing[0])
    merged["confidence"] = sum(v["confidence"] for v in agreeing) / len(agreeing)
    return merged


def _rate(num, den):
    return round(num / den, 4) if den else None


def _prec(rows, final, verdict, label_ok, gate=None, extra=None):
    """Precision of ``verdict`` (optionally at/above ``gate`` and under an
    ``extra`` row predicate) against ``label_ok(row)``."""
    hit = bad = 0
    for r, v in zip(rows, final):
        if v is None or v["verdict"] != verdict:
            continue
        if gate is not None and v["confidence"] < gate:
            continue
        if extra is not None and not extra(r):
            continue
        hit += 1
        bad += not label_ok(r, v)
    return {"n": hit, "bad": bad, "precision": _rate(hit - bad, hit)}


def score_merges(rows, reps, args):
    final = [majority([rep[i] for rep in reps]) for i in range(len(rows))]
    lab = lambda r, v: r["label"] == v["verdict"]  # noqa: E731
    out = {"rows": len(rows),
           "decided": sum(1 for v in final if v and v["verdict"] in ("accept", "reject")),
           "accept_precision": _prec(rows, final, "accept", lab),
           "reject_precision": _prec(rows, final, "reject", lab),
           "auto_reject_single": _prec(rows, final, "reject", lab, gate=args.reject_gate)}
    if len(reps) >= 2:
        a, b = reps[0], reps[1]

        def two(verdict, gate, extra=None):
            hit = bad = 0
            for i, r in enumerate(rows):
                va, vb = a[i], b[i]
                if not va or not vb or va["verdict"] != verdict or vb["verdict"] != verdict:
                    continue
                if (va["confidence"] + vb["confidence"]) / 2 < gate:
                    continue
                if extra and not extra(r):
                    continue
                hit += 1
                bad += r["label"] != verdict
            return {"n": hit, "bad": bad, "precision": _rate(hit - bad, hit)}
        out["two_vote_reject"] = two("reject", args.reject_gate_2)
        out["two_vote_accept_not_lowdiff"] = two(
            "accept", args.accept_gate, lambda r: not r.get("low_differential"))
        out["two_vote_accept_any"] = two("accept", args.accept_gate)
    return out


def score_links(rows, reps, args):
    final = [majority([rep[i] for rep in reps]) for i in range(len(rows))]

    def ok(r, v):
        if v["verdict"] == "retype":
            return r["label"] == "retype" and (r.get("retype") or {}).get("relation") == v.get("relation")
        return r["label"] == v["verdict"]
    return {"rows": len(rows),
            "decided": sum(1 for v in final if v and v["verdict"] != "leave"),
            "accept_precision": _prec(rows, final, "accept", ok),
            "retype_precision": _prec(rows, final, "retype", ok),
            "reject_precision": _prec(rows, final, "reject", ok),
            "auto_accept": _prec(rows, final, "accept", ok, gate=args.link_gate),
            "auto_retype": _prec(rows, final, "retype", ok, gate=args.link_gate),
            "auto_reject": _prec(rows, final, "reject", ok, gate=args.link_gate)}


def score_junk(rows, reps, args):
    final = [majority([rep[i] for rep in reps]) for i in range(len(rows))]
    lab = lambda r, v: r["label"] == v["verdict"]  # noqa: E731
    under_bar = lambda r: int(r.get("deg") or 0) <= 2 and int(r.get("facts") or 0) <= 1  # noqa: E731  (= junk_max_auto_degree)
    return {"rows": len(rows),
            "keep_precision": _prec(rows, final, "keep", lab),
            "delete_precision": _prec(rows, final, "delete", lab),
            "auto_keep": _prec(rows, final, "keep", lab, gate=args.junk_keep_gate),
            "auto_delete_under_bar": _prec(rows, final, "delete", lab,
                                           gate=args.junk_gate, extra=under_bar)}


def score_candidates(rows, reps, args):
    final = [majority([rep[i] for rep in reps]) for i in range(len(rows))]
    lab = lambda r, v: r["label"] == v["verdict"]  # noqa: E731
    strict = lambda r, v: r["label"] == "propose" and r.get("relation") == v.get("relation")  # noqa: E731
    return {"rows": len(rows),
            "propose_precision": _prec(rows, final, "propose", lab),
            "propose_relation_precision": _prec(rows, final, "propose", strict),
            "dismiss_precision": _prec(rows, final, "dismiss", lab),
            "auto_propose": _prec(rows, final, "propose", lab, gate=args.candidate_gate),
            "auto_dismiss": _prec(rows, final, "dismiss", lab, gate=args.candidate_gate)}


def score_curation(rows, reps, args):
    final = [majority([rep[i] for rep in reps]) for i in range(len(rows))]
    lab = lambda r, v: r["label"] == v["verdict"]  # noqa: E731
    keep_ok = lambda r, v: r["label"] == "duplicate" and r.get("keep") == v.get("keep")  # noqa: E731
    return {"rows": len(rows),
            "distinct_precision": _prec(rows, final, "distinct", lab),
            "duplicate_precision": _prec(rows, final, "duplicate", lab),
            "duplicate_keep_precision": _prec(rows, final, "duplicate", keep_ok),
            "auto_distinct": _prec(rows, final, "distinct", lab, gate=args.distinct_gate),
            "auto_forget": _prec(rows, final, "duplicate", keep_ok, gate=args.forget_gate)}


SCORE = {"merges": score_merges, "links": score_links, "junk": score_junk,
         "candidates": score_candidates, "curation": score_curation}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--queues", default=",".join(QUEUES))
    ap.add_argument("--replicates", type=int, default=1)
    ap.add_argument("--batch", type=int, default=8,
                    help="rows per call; keep = deep_dream.judge_batch")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--max-tokens", type=int, default=2048,
                    help="extractor max_tokens; 2048 = DreamConfig."
                         "extractor_max_tokens, what the sweep's judge is "
                         "built with, so the payload matches production")
    ap.add_argument("--seed", type=int, default=20260902,
                    help="base seed for per-replicate row shuffles "
                         "(replicate i uses seed + i)")
    ap.add_argument("--force", action="store_true",
                    help="replace an arm record that already exists in "
                         "--out (never silent: a canonical result file is "
                         "not overwritten on a rerun by default)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--data", type=Path, default=DATA,
                    help="evidence pack to replay (default: the frozen "
                         "2026-09-02 pack)")
    ap.add_argument("--snippet-chars", type=int, default=None,
                    help="merge-judge per-snippet cap stamped on each "
                         "proposal (0 = unbounded); omit for the frozen "
                         "240-char serialization")
    # Gates mirror DeepDreamConfig defaults; pass the deployed values.
    ap.add_argument("--reject-gate", type=float, default=0.8)
    ap.add_argument("--reject-gate-2", type=float, default=0.7)
    ap.add_argument("--accept-gate", type=float, default=0.6)
    ap.add_argument("--link-gate", type=float, default=0.8)
    ap.add_argument("--junk-gate", type=float, default=0.85)
    ap.add_argument("--junk-keep-gate", type=float, default=0.8)
    ap.add_argument("--candidate-gate", type=float, default=0.6)
    ap.add_argument("--distinct-gate", type=float, default=0.8)
    ap.add_argument("--forget-gate", type=float, default=0.9)
    args = ap.parse_args()

    global SNIPPET_CHARS
    SNIPPET_CHARS = args.snippet_chars
    raw = args.data.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    ex = OpenAICompatExtractor(args.base_url, args.model,
                               max_tokens=args.max_tokens,
                               timeout_seconds=args.timeout)
    result = {"arm": args.arm, "model": args.model, "base_url": args.base_url,
              "replicates": args.replicates, "batch": args.batch,
              "max_tokens": args.max_tokens, "seed": args.seed,
              "snippet_chars": args.snippet_chars,
              "data": args.data.name,
              "data_sha256": __import__("hashlib").sha256(raw).hexdigest(),
              "gates": {k: v for k, v in vars(args).items()
                        if k.endswith("gate") or k.endswith("gate_2")},
              "queues": {}}
    for queue in [q.strip() for q in args.queues.split(",") if q.strip()]:
        rows = data[queue]
        reps = []
        for i in range(args.replicates):
            t0 = time.time()
            reps.append(run_replicate(ex, queue, rows, args.batch,
                                      seed=args.seed + i))
            n = sum(1 for v in reps[-1] if v)
            print(f"{queue}: replicate {i + 1}/{args.replicates}: {n}/{len(rows)} "
                  f"verdicts in {time.time() - t0:.0f}s", flush=True)
        scored = SCORE[queue](rows, reps, args)
        scored["per_row"] = [
            {"id": r.get("id", r.get("pid")), "label": r.get("label"),
             "votes": [_vote_only(rep[i]) for rep in reps]}
            for i, r in enumerate(rows)]
        result["queues"][queue] = scored
        slim = {k: v for k, v in scored.items() if k != "per_row"}
        print(queue, json.dumps(slim, indent=1))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc = (json.loads(args.out.read_text(encoding="utf-8"))
           if args.out.exists() else {"data": DATA.name, "arms": {}})
    if args.arm in doc.get("arms", {}) and not args.force:
        raise SystemExit(f"arm '{args.arm}' already exists in {args.out}; "
                         "pass --force to replace it or pick a new arm name")
    doc["arms"][args.arm] = result
    args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
