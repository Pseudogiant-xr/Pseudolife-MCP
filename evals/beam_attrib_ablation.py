"""Attribution ablation: re-answer a BEAM run's persisted contexts with the
pre-Phase-1 answer prompt, holding everything else fixed.

The Phase-1 run (p1-b16) changed three things at once against the original
qwen38 run: the turn budget (6/3 -> 16/16), ordinal-stamped turns, and the
contradiction-surfacing answer prompt. The reader/volume grid measured the
budget term; this script isolates the prompt term by re-answering p1-b16's
byte-persisted contexts (same budget, same ordinals) with the OLD prompt and
judging with the same local judge. The per-row paired delta (source score
minus ablation score) is then the prompt effect alone; the ordinal term
falls out by subtraction from the total.

Zero-subscription-token: answers and judging both run on the local bench
Qwen server (start via ``. evals/qwen_server.ps1; Start-Qwen`` — never
``-Fast`` for judged output).

Usage:
    PYTHONPATH=. python evals/beam_attrib_ablation.py \
        --source evals/results/beam-100K-qwen-27b-p1-b16.jsonl \
        --beam-root ../beam-harness
    ... --report            # summarize only (no GPU needed)

Per-row resumable: rows append as they finish, keyed (chat_id, type, index);
rerunning skips completed keys.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import longmemeval_bench as lme                       # noqa: E402
from longmemeval_bench import _chat, QWEN_URL, load_rows, probe  # noqa: E402
from beam_adapter import (                            # noqa: E402
    _BEAM_ANSWER_SYSTEM, judge_response, load_judge_prompt)

# The exact sentence Phase-1 (commit 936e37aa) inserted into the answer
# prompt; commit 44366163 is the last pre-Phase-1 text. Frozen here (rather
# than read from git at runtime) so the instrument is reproducible offline;
# test_beam_attrib_ablation pins new == old + this sentence as a string
# identity, so a drifted prompt fails loudly instead of silently widening
# the ablation to more than one term.
CONTRADICTION_SENTENCE = (
    "When the context contains genuinely "
    "CONTRADICTORY claims — statements that conflict about whether "
    "something happened or is true, not a value that was simply updated — "
    "say so explicitly and present both sides instead of silently picking "
    "one. "
)

# The literal pre-Phase-1 text (commit 44366163), NOT derived from the live
# prompt — deriving would let a future edit to the live prompt silently
# redefine "old". The pin test cross-checks live == old + sentence, so a
# drift on either side goes red instead of widening the ablation.
OLD_ANSWER_SYSTEM = (
    "You answer questions about a long-running conversation from its "
    "memory context. Use ONLY the provided context. When the context shows "
    "a fact was updated, use the most CURRENT value unless the question "
    "asks about an earlier state. Answer completely — include every part "
    "the question asks for; lists and multi-step answers are fine. If the "
    "context does not contain the information, say exactly: I don't know."
)


def out_path_for(source: Path, tag: str) -> Path:
    return source.with_name(
        source.name.removesuffix(".jsonl") + f"-ablate-{tag}.jsonl")


def pending_rows(rows: list[dict], done: set[tuple]) -> list[dict]:
    return [r for r in rows
            if (r["chat_id"], r["type"], r["index"]) not in done]


def ablate_row(src: dict, judge_prompt: str, chat) -> dict:
    """Re-answer one source row's persisted contexts under the old prompt.

    ``chat`` is the (system, user, **kw) transport for BOTH the answer and
    the judge calls — tests inject a fake; the CLI passes the bench-server
    ``_chat``. Source scores ride along so summarize() can pair without
    re-reading the source artifact."""
    contexts = src.get("contexts")
    if not contexts:
        raise SystemExit(
            f"source row {src.get('chat_id')}/{src.get('type')}"
            f"[{src.get('index')}] has no persisted contexts — the source "
            "run predates context persistence and cannot be ablated")
    row = {"chat_id": src["chat_id"], "tier": src.get("tier"),
           "type": src["type"], "index": src["index"],
           "question": src["question"], "difficulty": src.get("difficulty"),
           "rubric": src["rubric"],
           "rag_top_k": src.get("rag_top_k"),
           "hybrid_top_k": src.get("hybrid_top_k"),
           "answer_system": "pre-phase1 (44366163)"}
    for arm, ctx in contexts.items():
        prompt = (f"Question: {src['question']}\n\n"
                  f"Memory context:\n{ctx or '(empty)'}")
        response = chat(OLD_ANSWER_SYSTEM, prompt, max_tokens=1024)
        verdict = judge_response(judge_prompt, src["question"],
                                 src["rubric"], response, chat=chat)
        row[f"{arm}_response"] = response
        row[f"{arm}_score"] = verdict["llm_judge_score"]
        row[f"{arm}_score_intfaithful"] = \
            verdict["llm_judge_score_intfaithful"]
        row[f"{arm}_judge"] = verdict["items"]
        row[f"{arm}_judge_failures"] = verdict["judge_failures"]
        row[f"source_{arm}_score"] = src.get(f"{arm}_score")
    return row


def summarize(ab_rows: list[dict], source_name: str) -> dict:
    arms = sorted({k.removeprefix("source_").removesuffix("_score")
                   for k in ab_rows[0] if k.startswith("source_")})
    summary = {"ablation": "answer prompt (old vs Phase-1 contradiction)",
               "source_run": source_name,
               "n_questions": len(ab_rows),
               "n_chats": len({r["chat_id"] for r in ab_rows}),
               "note": ("paired per-row: identical contexts (budget + "
                        "ordinals), identical local judge; delta is the "
                        "prompt term alone"),
               "arms": {}, "types": {}}
    for arm in arms:
        diffs = [r[f"source_{arm}_score"] - r[f"{arm}_score"]
                 for r in ab_rows]
        n = len(diffs)
        mean = sum(diffs) / n
        var = (sum((d - mean) ** 2 for d in diffs) / (n - 1)) if n > 1 else 0.0
        summary["arms"][arm] = {
            "score": round(sum(r[f"{arm}_score"] for r in ab_rows) / n, 4),
            "source_score": round(
                sum(r[f"source_{arm}_score"] for r in ab_rows) / n, 4),
            "paired_delta_new_minus_old": round(mean, 4),
            "paired_delta_se": round(math.sqrt(var / n), 4) if n > 1 else 0.0,
        }
    by_type: dict[str, list[dict]] = {}
    for r in ab_rows:
        by_type.setdefault(r["type"], []).append(r)
    for qtype, trows in sorted(by_type.items()):
        entry = {"n": len(trows)}
        for arm in arms:
            entry[arm] = round(
                sum(r[f"{arm}_score"] for r in trows) / len(trows), 4)
            entry[f"{arm}_delta"] = round(
                sum(r[f"source_{arm}_score"] - r[f"{arm}_score"]
                    for r in trows) / len(trows), 4)
        summary["types"][qtype] = entry
    return summary


def write_summary(out_path: Path, summary: dict) -> None:
    out_path.with_name(
        out_path.name.removesuffix(".jsonl") + ".summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, type=Path,
                    help="completed BEAM run JSONL with persisted contexts")
    ap.add_argument("--beam-root", type=Path, default=None,
                    help="BEAM checkout (for the upstream judge prompt); "
                    "required unless --report")
    ap.add_argument("--tag", default="oldprompt")
    ap.add_argument("--limit", type=int, default=None,
                    help="ablate only the first N pending rows (smoke)")
    ap.add_argument("--report", action="store_true",
                    help="summarize the existing ablation artifact only")
    args = ap.parse_args()

    src_rows = load_rows(args.source)
    if not src_rows:
        sys.exit(f"no rows in {args.source}")
    out_path = out_path_for(args.source, args.tag)
    source_name = args.source.name.removesuffix(".jsonl")

    if args.report:
        ab_rows = load_rows(out_path)
        if not ab_rows:
            sys.exit(f"no ablation rows in {out_path}")
        summary = summarize(ab_rows, source_name)
        write_summary(out_path, summary)
        print(json.dumps(summary, indent=2))
        return 0

    if args.beam_root is None:
        sys.exit("--beam-root is required to run (upstream judge prompt)")
    if not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it via "
                 "qwen_server.ps1 Start-Qwen (never -Fast for judged work)")
    judge_prompt = load_judge_prompt(args.beam_root)
    done = {(r["chat_id"], r["type"], r["index"])
            for r in load_rows(out_path)}
    todo = pending_rows(src_rows, done)
    if args.limit:
        todo = todo[:args.limit]
    print(f"ablation over {source_name}: {len(todo)} rows pending "
          f"({len(done)} done)", flush=True)
    for src in todo:
        t0 = time.perf_counter()
        row = ablate_row(src, judge_prompt, _chat)
        row["wall_seconds"] = round(time.perf_counter() - t0, 1)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        arms = [k.removeprefix("source_").removesuffix("_score")
                for k in row if k.startswith("source_")]
        print(f"  {row['chat_id']}/{row['type']}[{row['index']}] " + " ".join(
            f"{a}={row[f'{a}_score']:.2f}(src {row[f'source_{a}_score']:.2f})"
            for a in sorted(arms)), flush=True)
    ab_rows = load_rows(out_path)
    summary = summarize(ab_rows, source_name)
    write_summary(out_path, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "types"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
