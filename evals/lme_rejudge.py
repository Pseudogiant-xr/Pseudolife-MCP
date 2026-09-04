"""Re-judge an existing LongMemEval run's recorded answers with a second
judge family.

The 2026-09-04 500-question run (`raglite-all-fresh`) is the first
whole-benchmark memory-arm win this project has measured — hybrid beats the
naive RAG control by +0.040 (paired sign-flip permutation p 0.015, 41 W /
21 L). It was judged by one instrument only: the local Qwen3.8-27B server.
The standing rule (CLAUDE.md, "Publishing a benchmark number") is that a
claim reaching the README first runs under two independent judge families,
because determinism is not validity — the retired BEAM cascade 0.936
replicated at std 0.0000 three times and still did not survive a judge
swap.

`beam_rejudge.py` does this for BEAM's rubric-scored rows; this is the
LongMemEval half. Retrieval and answering are NOT re-run: the recorded
per-arm responses are replayed through a headless ``claude -p`` judge (the
Max-plan CLI contract, no API key — `CliJudge` is imported from
`beam_rejudge` rather than re-implemented) using the harness's OWN judge
prompts, imported from `longmemeval_bench` so the only term that changes
is the judge model. Any movement is therefore pure judge effect.

The source artifact is never touched. Output goes to

  * ``<source>.rejudge-<tag>.jsonl`` — the same rows with
    ``{arm}_correct_<tag>`` added and the original ``{arm}_correct``
    preserved beside it, so every comparison pairs within-row
  * ``<source>.rejudge-<tag>.summary.json`` — per-arm and per-type accuracy
    under both judges, item-level agreement per arm, the gold-leak
    exclusion (ids recorded), and the CLI call/error counts
  * ``<source>.rejudge-<tag>.arms-vs-rag.json`` — the SAME paired
    comparison the original claim was made from, produced by
    ``beam_within_run_pairs.py`` with ``--score-key correct_<tag>``, so the
    two numbers are read off identical arithmetic

A seeded stability sample (N (row, arm) pairs judged twice) is reported
alongside: a CLI judge, unlike the pinned q8_0 server, is not
bit-reproducible, and its own flip rate is the control floor that bounds
what any judge-to-judge delta can claim.

Usage:
    PYTHONPATH=. python evals/lme_rejudge.py \
        --in evals/results/longmemeval-all-oracle-qwen-27b-raglite-all-fresh.jsonl \
        --tag opus5 --arms rag,hybrid,cortex,rag1 --workers 4 \
        --stability-sample 60 \
        [--judge-model claude-opus-5] [--limit N] [--resume] [--force]
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

import beam_within_run_pairs  # noqa: E402
import leak_check  # noqa: E402
from beam_rejudge import DEFAULT_CLI, CliJudge, out_path_for  # noqa: E402
from longmemeval_bench import (ARMS, _JUDGE_SYSTEM,  # noqa: E402
                               _JUDGE_SYSTEM_GENERIC, load_rows)

# Canonical arm order for reporting. The harness's three, then the two
# opt-in comparator arms; knob-minted arms (rag1/rag2/ragb400) are
# discovered off the rows and sorted after these, the same rule
# ``report()`` uses so a table reads the same in both artifacts.
ALL_ARMS = (*ARMS, "refind", "nomem")
CONTROL = "rag"
# The type whose judge prompt carries the update-specific clause. Rows with
# no ``question_type`` predate --types and keep it, exactly as
# ``answer_and_judge`` does, so canonical results re-judge identically.
KU_TYPE = "knowledge-update"
_RESPONSE_SUFFIX = "_response"


# ── naming ────────────────────────────────────────────────────────────────
def summary_path_for(out_path: Path) -> Path:
    return out_path.with_name(
        out_path.name.removesuffix(".jsonl") + ".summary.json")


# ── arms ──────────────────────────────────────────────────────────────────
def detect_arms(rows: list[dict], only: str | None = None) -> tuple[str, ...]:
    """Arms come off the rows' ``{arm}_response`` keys — a run carries
    whatever arms it answered — and ``only`` narrows, loudly, so a typo in
    ``--arms`` costs nothing instead of 500 judge calls on nothing."""
    present = {k[:-len(_RESPONSE_SUFFIX)] for k in rows[0]
               if k.endswith(_RESPONSE_SUFFIX)}
    ordered = [a for a in ALL_ARMS if a in present]
    ordered += sorted(present.difference(ordered))
    if only:
        keep = {a.strip() for a in only.split(",") if a.strip()}
        unknown = keep - set(ordered)
        if unknown:
            raise SystemExit(f"--arms names {sorted(unknown)} but the source "
                             f"rows only carry {tuple(ordered)}")
        ordered = [a for a in ordered if a in keep]
    return tuple(ordered)


# ── the judge call: the harness's prompts, a different model ──────────────
def judge_system_for(row: dict):
    """The harness's judge system prompt for this row's question type.

    Returns the imported object, never a copy: the whole point of the
    re-judge is that the prompt is held fixed while the model varies.
    """
    return (_JUDGE_SYSTEM if row.get("question_type", KU_TYPE) == KU_TYPE
            else _JUDGE_SYSTEM_GENERIC)


def judge_user(row: dict, response: str) -> str:
    """The judge user message, byte-identical to ``answer_and_judge``'s.

    The one difference from the harness's call is the absence of its
    ``max_tokens=8`` cap, which the CLI judge has no equivalent for; the
    verdict parse below reads only the first word either way.
    """
    return (f"Question: {row['question']}\n"
            f"Correct answer: {row['answer']}\n"
            f"Model response: {response}")


def parse_verdict(text) -> bool:
    """The harness's verdict rule, verbatim. A failed judge call returns ""
    from ``CliJudge``, which lands here as False and is counted as a CLI
    error in the summary rather than aborting the row."""
    return str(text or "").strip().lower().startswith("yes")


# ── one row ───────────────────────────────────────────────────────────────
# Row fields carried into the re-judged artifact. The served contexts are
# deliberately NOT carried: they are ~7MB of the source file and nothing
# downstream re-reads them, while {arm}_context_tokens (which IS carried)
# is what beam_within_run_pairs reports the cost column from.
CARRY = ("question_id", "question", "question_type", "question_date",
         "answer", "gold_in_question", "abstention")


def rejudge_row(row: dict, arms: tuple[str, ...], tag: str, judge) -> dict:
    """Judge every arm's recorded response afresh; keep the original
    verdict beside the new one so every downstream comparison pairs
    within-row."""
    out = {k: row[k] for k in CARRY if k in row}
    system = judge_system_for(row)
    for arm in arms:
        response = row.get(f"{arm}_response", "")
        try:
            verdict = judge(system, judge_user(row, response))
        except Exception as e:  # noqa: BLE001 — a row never dies mid-run
            print(f"lme_rejudge: arm {arm} failed wholesale: {e}",
                  file=sys.stderr, flush=True)
            verdict = ""
        out[f"{arm}_response"] = response
        out[f"{arm}_correct"] = bool(row.get(f"{arm}_correct"))
        out[f"{arm}_correct_{tag}"] = parse_verdict(verdict)
        if f"{arm}_context_tokens" in row:
            out[f"{arm}_context_tokens"] = row[f"{arm}_context_tokens"]
    return out


# ── output contract ───────────────────────────────────────────────────────
def open_output(out_path: Path, *, resume: bool, force: bool,
                arms: tuple[str, ...] = (), tag: str = "") -> set[str]:
    """Prepare the output artifact and report which rows it already holds.

    Never overwrite a canonical result file on a rerun: an existing
    artifact is refused unless the caller says which they meant —
    ``--resume`` (continue it) or ``--force`` (discard it).

    Resuming with a WIDER ``--arms`` than the run that wrote the file is
    also refused: the rows already on disk carry no verdict column for the
    added arm, and ``summarize`` would read that absence as a run of False
    verdicts rather than fail. ``arms``/``tag`` are optional so the naming
    contract stays testable on its own.
    """
    if not out_path.exists():
        return set()
    if force:
        out_path.unlink()
        return set()
    if resume:
        rows = load_rows(out_path)
        if rows and arms:
            missing = [a for a in arms if f"{a}_correct_{tag}" not in rows[0]]
            if missing:
                raise SystemExit(
                    f"{out_path} was written without {missing}; resuming "
                    "would leave those arms unjudged on every row already "
                    "in it. Re-run with the original --arms, or with "
                    "--force under a new --tag.")
        return {r["question_id"] for r in rows if r.get("question_id")}
    raise SystemExit(
        f"{out_path} already exists; pass --resume to continue it or "
        "--force to discard it (or re-run under a different --tag)")


def pending_rows(rows: list[dict], done: set[str]) -> list[dict]:
    return [r for r in rows if r.get("question_id") not in done]


# ── summary ───────────────────────────────────────────────────────────────
def _mean(values: list[bool]) -> float:
    return round(sum(1 for v in values if v) / len(values), 4) if values \
        else 0.0


def summarize(rows: list[dict], arms: tuple[str, ...], tag: str,
              judge_model: str, source: str, note: str | None = None) -> dict:
    """Per-arm and per-type accuracy under both judges, the item-level
    agreement between them, and the gold-leak exclusion.

    Headline means span ALL rows, including the ones the leak check flags —
    the same convention the source run's summary uses, so the two tables
    are read against each other without a footnote. The leak-free reads sit
    in the ``leak_check`` block beside the excluded ids.
    """
    n = len(rows)
    summary: dict = {
        "benchmark": "LongMemEval-rejudge", "source": source,
        "judge": judge_model, "tag": tag, "n_questions": n,
        "arms": {}, "types": {},
    }
    if note:
        summary["note"] = note
    for arm in arms:
        orig = [bool(r.get(f"{arm}_correct")) for r in rows]
        new = [bool(r.get(f"{arm}_correct_{tag}")) for r in rows]
        summary["arms"][arm] = {
            "n": n,
            "accuracy": _mean(new),
            "accuracy_orig": _mean(orig),
            "delta": round(_mean(new) - _mean(orig), 4),
            "agreement": (round(sum(1 for a, b in zip(orig, new) if a == b)
                                / n, 4) if n else None),
        }
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r.get("question_type", KU_TYPE), []).append(r)
    for qtype, trows in sorted(by_type.items()):
        entry: dict = {"n": len(trows)}
        for arm in arms:
            entry[arm] = _mean([bool(r.get(f"{arm}_correct_{tag}"))
                                for r in trows])
            entry[f"{arm}_orig"] = _mean([bool(r.get(f"{arm}_correct"))
                                          for r in trows])
        summary["types"][qtype] = entry
    summary["leak_check"] = _leak_block(rows, arms, tag)
    return summary


def _leak_block(rows: list[dict], arms: tuple[str, ...], tag: str) -> dict:
    """The gold-answer leak exclusion, using the tree's own checker.

    ``leak_check.check_row`` trusts the run's recorded ``gold_in_question``
    flag over a re-derivation — it saw the exact question text it served —
    which is why the flag is carried into the re-judged rows.
    """
    verdicts = [leak_check.check_row(r, position=i)
                for i, r in enumerate(rows)]
    leaked = [v["id"] for v in verdicts if v["leak"]]
    clean = [r for r, v in zip(rows, verdicts) if not v["leak"]]
    block: dict = {
        "check": "gold-answer leak (SR-TTT arXiv 2603.06642)",
        "n_rows": len(rows), "n_leaked": len(leaked), "leaked": leaked,
        "n_leak_free": len(clean),
        "note": ("headline means above span every row; these exclude the "
                 "rows whose question names its own gold answer"),
        "arms": {},
    }
    for arm in arms:
        new = _mean([bool(r.get(f"{arm}_correct_{tag}")) for r in clean])
        orig = _mean([bool(r.get(f"{arm}_correct")) for r in clean])
        block["arms"][arm] = {"n": len(clean), "accuracy": new,
                              "accuracy_orig": orig,
                              "delta": round(new - orig, 4)}
    return block


# ── stability sample: the second judge's own floor ────────────────────────
def stability_pairs(rows: list[dict], arms: tuple[str, ...],
                    n: int) -> list[tuple[str, str]]:
    """A seeded, order-independent sample of (question_id, arm) pairs to
    judge a second time."""
    pairs = sorted((r["question_id"], arm) for r in rows for arm in arms)
    return random.Random(0).sample(pairs, min(n, len(pairs)))


def stability_report(rows: list[dict], pairs: list[tuple[str, str]], tag: str,
                     judge) -> dict:
    """Judge the sampled pairs once more and measure agreement with the
    recorded pass — the CLI judge's flip rate against ITSELF, which bounds
    what any original-vs-rejudge delta can claim."""
    by_id = {r["question_id"]: r for r in rows}
    agree = 0
    detail = []
    for qid, arm in pairs:
        row = by_id[qid]
        second = parse_verdict(judge(judge_system_for(row),
                                     judge_user(row,
                                                row.get(f"{arm}_response",
                                                        ""))))
        first = bool(row.get(f"{arm}_correct_{tag}"))
        agree += (first == second)
        detail.append({"key": [qid, arm], "first": first, "second": second})
    return {"n_pairs": len(pairs),
            "agreement": round(agree / len(pairs), 4) if pairs else None,
            "pairs": detail}


def merge_stability(reports: list[dict]) -> dict:
    """Pair-count-weighted merge of per-pair stability reports."""
    total = sum(r["n_pairs"] for r in reports)
    agree = sum(r["agreement"] * r["n_pairs"] for r in reports
                if r["n_pairs"] and r["agreement"] is not None)
    return {"n_pairs": total,
            "agreement": round(agree / total, 4) if total else None,
            "pairs": [p for r in reports for p in r["pairs"]]}


# ── the paired comparison, from the same producer as the original ─────────
def pairing_argv(out_path: Path, arms: tuple[str, ...], tag: str,
                 note: str | None = None) -> list[str]:
    """The ``beam_within_run_pairs`` invocation that pairs the re-judged
    arms against the ``rag`` control under the second judge's verdicts.

    The whole re-judge stem is the tool's ``--tag`` (with an empty
    ``--prefix``), which lands its artifact beside the rows it was computed
    from as ``<stem>.arms-vs-rag.json``. The derived ``cascade`` arm and the
    token-matched ``cortex:rag1`` pairing ride along whenever the run
    re-judged the arms they need.
    """
    if CONTROL not in arms:
        raise SystemExit(
            f"the paired comparison is against the {CONTROL!r} control, "
            f"which this re-judge did not cover (arms={arms}); re-run with "
            f"{CONTROL} in --arms or pass --skip-pairs")
    paired = [a for a in arms if a != CONTROL]
    if "cortex" in arms:
        paired.append(beam_within_run_pairs.CASCADE)
    argv = ["--prefix", "", "--tag", out_path.name.removesuffix(".jsonl"),
            "--arms", ",".join(paired),
            "--score-key", f"correct_{tag}", "--type-key", "question_type"]
    if "cortex" in arms and "rag1" in arms:
        argv += ["--pairs", "cortex:rag1"]
    if note:
        argv += ["--note", note]
    return argv


def _git_rev() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, timeout=15,
                             check=False)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="src", required=True, type=Path,
                    help="existing LongMemEval per-question JSONL")
    ap.add_argument("--tag", required=True,
                    help="suffix for the output artifacts and the new "
                         "verdict column, e.g. opus5")
    ap.add_argument("--judge-model", default="claude-opus-5")
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--call-timeout", type=float, default=240.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--arms", default=None,
                    help="comma-separated subset (default: all in the rows)")
    ap.add_argument("--limit", type=int, default=None,
                    help="re-judge only the first N rows (smoke)")
    ap.add_argument("--stability-sample", type=int, default=60,
                    help="(row, arm) pairs judged twice; 0 disables")
    ap.add_argument("--resume", action="store_true",
                    help="continue an existing output artifact")
    ap.add_argument("--force", action="store_true",
                    help="discard an existing output artifact and restart")
    ap.add_argument("--skip-pairs", action="store_true",
                    help="do not write the paired arms-vs-rag artifact")
    ap.add_argument("--note", default=None,
                    help="a sentence recorded in both artifacts")
    args = ap.parse_args(argv)

    src_rows = load_rows(args.src)
    if not src_rows:
        raise SystemExit(f"no rows in {args.src}")
    arms = detect_arms(src_rows, args.arms)
    out_path = out_path_for(args.src, args.tag)
    done = open_output(out_path, resume=args.resume, force=args.force,
                       arms=arms, tag=args.tag)
    if args.limit:
        src_rows = src_rows[:args.limit]
    pending = pending_rows(src_rows, done)

    judge = CliJudge(args.cli, args.judge_model, args.call_timeout)
    # Probe-gated abort (the 2026-07-04 launch-bug lesson): a logged-out or
    # missing CLI must fail the launch loudly, not run 2000 empty calls
    # whose every verdict parses to False.
    if "OK" not in judge("", "Reply with exactly: OK"):
        raise SystemExit(
            f"judge probe failed: {args.cli} --model {args.judge_model} did "
            "not answer (logged in? on PATH?)")

    print(f"lme_rejudge: {len(src_rows)} rows, arms={arms}, "
          f"judge={args.judge_model}, workers={args.workers} "
          f"({len(done)} already done) -> {out_path.name}", flush=True)
    started = time.time()
    finished = len(done)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(rejudge_row, r, arms, args.tag, judge)
                   for r in pending]
        for fut in as_completed(futures):
            row = fut.result()          # rejudge_row never raises
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            finished += 1
            print(f"  {finished}/{len(src_rows)} {row['question_id']} "
                  + " ".join(f"{a}={row[f'{a}_correct']}->"
                             f"{row[f'{a}_correct_{args.tag}']}"
                             for a in arms)
                  + (f" (cli_errors={judge.errors})" if judge.errors else ""),
                  flush=True)

    all_rows = load_rows(out_path)
    summary = summarize(all_rows, arms, args.tag, args.judge_model,
                        args.src.name, note=args.note)
    if args.stability_sample:
        print(f"lme_rejudge: stability sample "
              f"({args.stability_sample} pairs)...", flush=True)
        pairs = stability_pairs(all_rows, arms, args.stability_sample)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            # One future per pair keeps the pool busy; stability_report is
            # sequential, so fan out here instead.
            futs = [pool.submit(stability_report, all_rows, [p], args.tag,
                                judge) for p in pairs]
            summary["stability_sample"] = merge_stability(
                [f.result() for f in futs])
    # Counters land AFTER the stability pass so its calls (and any of their
    # failures) are visible in the artifact.
    summary["cli_calls"] = judge.calls
    summary["cli_errors"] = judge.errors
    summary["wall_seconds"] = round(time.time() - started, 1)
    summary["seconds_per_call"] = (
        round(summary["wall_seconds"] / judge.calls, 2) if judge.calls
        else None)
    summary["date"] = time.strftime("%Y-%m-%d")
    summary["git_rev"] = _git_rev()
    sum_path = summary_path_for(out_path)
    sum_path.write_text(json.dumps(summary, indent=2) + "\n",
                        encoding="utf-8")

    # beam_within_run_pairs resolves its source under evals/results; a
    # re-judge written anywhere else has to be paired by hand rather than
    # silently paired against the wrong file.
    if not args.skip_pairs and (out_path.parent.resolve()
                                != beam_within_run_pairs.RESULTS_DIR):
        print(f"lme_rejudge: {out_path.parent} is not the results dir; "
              "skipping the paired comparison", file=sys.stderr, flush=True)
        args.skip_pairs = True
    if not args.skip_pairs:
        pair_argv = pairing_argv(out_path, arms, args.tag, note=args.note)
        summary["pairing_command"] = ["beam_within_run_pairs.py", *pair_argv]
        sum_path.write_text(json.dumps(summary, indent=2) + "\n",
                            encoding="utf-8")
        print(f"lme_rejudge: pairing -> {' '.join(pair_argv)}", flush=True)
        try:
            beam_within_run_pairs.main(pair_argv)
        except SystemExit as e:
            print(f"lme_rejudge: paired comparison not written: {e}",
                  file=sys.stderr, flush=True)

    slim = {k: v for k, v in summary.items() if k != "stability_sample"}
    if "stability_sample" in summary:
        slim["stability_sample"] = {
            k: v for k, v in summary["stability_sample"].items()
            if k != "pairs"}
    print(json.dumps(slim, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
