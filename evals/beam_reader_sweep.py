"""Phase-0 BEAM reader/volume sweep — frontier answerer over rag contexts.

The 2026-08-22 gap decomposition left two legs of the Cognee-0.79 vs
PseudoLife-0.50 comparison unmeasured: the ANSWERER (they read with GPT-5,
our runs read with a local 27B) and the CONTEXT VOLUME (they serve ~24.7K
tokens/question, our rag arm serves ~3.2K). Both legs are measurable
WITHOUT the GPU, because the rag arm never touches the extractor: banks
are built extraction-free on the CPU embedder, and answering/judging goes
through the headless ``claude -p`` contract from beam_rejudge.

Two phases, both resumable per question row:

  serve   — per chat: fresh bench service, store every turn (no dreams),
            persist the top-``SERVE_TOP_K`` raw entries per question to
            ``beam-readersweep-<tag>.serve.jsonl``. Budget arms are
            slices of this one serve, so retrieval is identical across
            budgets by construction.
  answer  — for each budget arm (rag6/rag16/rag48): answer with the
            frontier CLI model under the BEAM answer system prompt, judge
            per rubric item with the same judge instrument as the
            rejudge-opus5 artifact — so the existing qwen-reader rag row
            (opus-judged, 0.4989) pairs directly against rag6-opus for a
            pure reader effect, and 6->16->48 gives the volume curve
            (48 turns ~= 26K tokens ~= Cognee's budget).

Caveats recorded in the artifact: single replicate; a CLI answerer is not
bit-reproducible (unlike the pinned q8_0 server), so per-type deltas
smaller than the rejudge stability floor (mean |item delta| 0.073) are
not findings.

Usage:
    PYTHONPATH=. python evals/beam_reader_sweep.py --beam-root <BEAM> \
        --tag opus-sweep --phase serve
    PYTHONPATH=. python evals/beam_reader_sweep.py --beam-root <BEAM> \
        --tag opus-sweep --phase answer [--workers 6] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

from beam_adapter import (  # noqa: E402
    _BEAM_ANSWER_SYSTEM, iter_chats, judge_response, load_chat_turns,
    load_judge_prompt, load_questions,
)
from beam_rejudge import CliJudge, DEFAULT_CLI  # noqa: E402
from longmemeval_bench import RESULTS_DIR, load_rows  # noqa: E402

BUDGETS = (6, 16, 48)
SERVE_TOP_K = max(BUDGETS)


def serve_path(tag: str) -> Path:
    return RESULTS_DIR / f"beam-readersweep-{tag}.serve.jsonl"


def result_path(tag: str) -> Path:
    return RESULTS_DIR / f"beam-readersweep-{tag}.jsonl"


def assemble_context(raw_entries: list[str], budget: int) -> str:
    return "\n\n".join(raw_entries[:budget])


def serve(beam_root: Path, tier: str, tag: str,
          limit_chats: int | None) -> None:
    """CPU-only context build: store turns, never dream — the rag arm is
    extraction-independent, which is what makes this phase GPU-free."""
    from ladder_sweep import build_service
    out = serve_path(tag)
    done = {(r["chat_id"], r["type"], r["index"]) for r in load_rows(out)}
    chats = iter_chats(beam_root, tier)
    if limit_chats:
        chats = chats[:limit_chats]
    print(f"serve: {len(chats)} chats, top_k={SERVE_TOP_K} "
          f"({len(done)} rows already done)", flush=True)
    for chat_id, chat_dir in chats:
        questions = load_questions(chat_dir)
        pending = [q for q in questions
                   if (chat_id, q["type"], q["index"]) not in done]
        if not pending:
            continue
        t0 = time.perf_counter()
        import tempfile
        svc = build_service(Path(tempfile.mkdtemp(prefix="beamrs_")))
        for turn in load_chat_turns(chat_dir):
            anchor = (f"[{turn['time_anchor']}] " if turn["time_anchor"]
                      else "")
            svc.store(f"{anchor}{turn['role']}: {turn['content']}",
                      source="beam")
        build_s = round(time.perf_counter() - t0, 1)
        for q in pending:
            entries = svc.search(q["question"], top_k=SERVE_TOP_K,
                                 contiguity_neighbors=0,
                                 timeline=False).get("entries", [])
            row = {"chat_id": chat_id, "tier": tier, "type": q["type"],
                   "index": q["index"], "question": q["question"],
                   "reference_answer": q["answer"],
                   "difficulty": q["difficulty"], "rubric": q["rubric"],
                   "serve_top_k": SERVE_TOP_K, "build_seconds": build_s,
                   "raw_entries": [e.get("text", "") for e in entries]}
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        svc.flush()
        print(f"chat {chat_id}: stored+served {len(pending)} questions "
              f"(bank build {build_s}s)", flush=True)


def process_row(row: dict, budgets: tuple[int, ...], answer_system: str,
                answer_fn, judge_prompt: str, judge_fn) -> dict:
    """Answer + judge one serve row at every budget. Loud when a budget
    exceeds the serve width — a slice would silently repeat the smaller
    arm while the artifact claimed the wider budget (the --hybrid-top-k
    lesson)."""
    out = {k: row[k] for k in ("chat_id", "tier", "type", "index",
                               "question", "difficulty", "rubric")}
    raw = row["raw_entries"]
    for b in budgets:
        if b > row.get("serve_top_k", SERVE_TOP_K):
            raise SystemExit(
                f"budget {b} exceeds the row's serve_top_k "
                f"{row.get('serve_top_k', SERVE_TOP_K)} — the arm would "
                "claim a retrieval width the serve never requested")
        arm = f"rag{b}"
        ctx = assemble_context(raw, b)
        prompt = (f"Question: {row['question']}\n\n"
                  f"Memory context:\n{ctx or '(empty)'}")
        response = answer_fn(answer_system, prompt)
        v = judge_response(judge_prompt, row["question"], row["rubric"],
                           response, chat=judge_fn)
        out[f"{arm}_context_chars"] = len(ctx)
        out[f"{arm}_response"] = response
        out[f"{arm}_score"] = v["llm_judge_score"]
        out[f"{arm}_score_intfaithful"] = v["llm_judge_score_intfaithful"]
        out[f"{arm}_judge"] = v["items"]
        out[f"{arm}_judge_failures"] = v["judge_failures"]
    return out


def summarize(rows: list[dict], budgets: tuple[int, ...], answerer: str,
              baseline: dict | None = None) -> dict:
    n = len(rows)
    arms = [f"rag{b}" for b in budgets]
    s = {"benchmark": "BEAM-readersweep", "answerer": answerer,
         "n_questions": n, "budgets": list(budgets),
         "scoring_note": ("paper-faithful float mean; identical retrieval "
                          "across budgets by construction (one serve, "
                          "sliced); single replicate — CLI answerer is "
                          "not bit-reproducible"),
         "arms": {}, "types": {}}
    for arm in arms:
        dead = sum(1 for r in rows
                   if r[f"{arm}_judge_failures"] >= len(r["rubric"]))
        s["arms"][arm] = {
            "score": round(sum(r[f"{arm}_score"] for r in rows) / n, 4),
            "score_intfaithful": round(
                sum(r[f"{arm}_score_intfaithful"] for r in rows) / n, 4),
            "mean_context_chars": round(
                sum(r[f"{arm}_context_chars"] for r in rows) / n),
            "judge_failures": sum(r[f"{arm}_judge_failures"] for r in rows),
            "rows_all_items_failed": dead,
        }
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for qtype, trows in sorted(by_type.items()):
        s["types"][qtype] = {
            "n": len(trows),
            **{arm: round(sum(r[f"{arm}_score"] for r in trows)
                          / len(trows), 4) for arm in arms},
        }
    if baseline is not None:
        s["baseline_qwen_reader_opus_judged"] = baseline
    return s


def answer(beam_root: Path, tag: str, budgets: tuple[int, ...],
           answer_model: str, judge_model: str, cli: str, workers: int,
           call_timeout: float, limit: int | None,
           baseline_summary: Path | None) -> None:
    src_rows = load_rows(serve_path(tag))
    if not src_rows:
        sys.exit(f"no serve rows — run --phase serve first "
                 f"({serve_path(tag)})")
    if limit:
        src_rows = src_rows[:limit]
    judge_prompt = load_judge_prompt(beam_root)
    answerer = CliJudge(cli, answer_model, call_timeout)
    judge = CliJudge(cli, judge_model, call_timeout)
    if "OK" not in judge("", "Reply with exactly: OK"):
        sys.exit("judge probe failed — CLI logged in and on PATH?")
    out = result_path(tag)
    done = {(r["chat_id"], r["type"], r["index"]) for r in load_rows(out)}
    pending = [r for r in src_rows
               if (r["chat_id"], r["type"], r["index"]) not in done]
    arms = [f"rag{b}" for b in budgets]
    print(f"answer: {len(src_rows)} rows, arms={arms}, "
          f"answerer={answer_model}, judge={judge_model}, "
          f"workers={workers} ({len(done)} already done)", flush=True)
    finished = len(done)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_row, r, budgets,
                               _BEAM_ANSWER_SYSTEM, answerer, judge_prompt,
                               judge): r for r in pending}
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 — skip, keep sweeping
                r = futures[fut]
                print(f"row {r['chat_id']}/{r['type']}[{r['index']}] "
                      f"failed: {e}", file=sys.stderr, flush=True)
                continue
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            finished += 1
            print(f"  {finished}/{len(src_rows)} "
                  f"{row['chat_id']}/{row['type']}[{row['index']}] "
                  + " ".join(f"{a}={row[f'{a}_score']:.2f}" for a in arms),
                  flush=True)
    all_rows = load_rows(out)
    baseline = None
    if baseline_summary and baseline_summary.exists():
        base = json.loads(baseline_summary.read_text(encoding="utf-8"))
        baseline = {"rag": base["arms"]["rag"],
                    "source": baseline_summary.name}
    summary = summarize(all_rows, budgets, answer_model, baseline=baseline)
    summary["judge"] = judge_model
    summary["cli_errors"] = answerer.errors + judge.errors
    summary["date"] = time.strftime("%Y-%m-%d")
    sp = out.with_name(out.name.removesuffix(".jsonl") + ".summary.json")
    sp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--beam-root", required=True, type=Path)
    ap.add_argument("--tier", default="100K")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--phase", choices=("serve", "answer"), required=True)
    ap.add_argument("--budgets", default=",".join(map(str, BUDGETS)))
    ap.add_argument("--answer-model", default="claude-opus-5")
    ap.add_argument("--judge-model", default="claude-opus-5")
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--call-timeout", type=float, default=300.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--limit-chats", type=int, default=None)
    ap.add_argument("--baseline-summary", type=Path, default=RESULTS_DIR /
                    "beam-100K-qwen-27b-beam100k-qwen38.rejudge-opus5"
                    ".summary.json",
                    help="rejudge summary whose opus-judged qwen-reader rag "
                         "row pairs against rag6 (the reader effect)")
    args = ap.parse_args()
    budgets = tuple(int(b) for b in args.budgets.split(","))
    if max(budgets) > SERVE_TOP_K:
        sys.exit(f"budget {max(budgets)} exceeds SERVE_TOP_K={SERVE_TOP_K}")
    if args.phase == "serve":
        serve(args.beam_root, args.tier, args.tag, args.limit_chats)
    else:
        answer(args.beam_root, args.tag, budgets, args.answer_model,
               args.judge_model, args.cli, args.workers, args.call_timeout,
               args.limit, args.baseline_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
