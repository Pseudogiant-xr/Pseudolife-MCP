"""Re-judge an existing BEAM run's recorded answers with a frontier judge.

Adapter runs are judged with the local reproducible Qwen server, while every
published BEAM comparator (Cognee, Mem0, MemOS) is judged with a frontier
API model — so the local numbers cannot be read against theirs, and judge
transfer was left as the open decision in
``evals/results/beam-100k-verdict.json`` (2026-08-03). This script isolates
the instrument: it replays the recorded per-arm responses from an existing
run's JSONL through a frontier judge (headless ``claude -p`` on the Max
plan, no API key — same CLI contract as evals/claude_shim.py, but pooled
rather than serialized, and never through the production :8082 shim) using
the same BEAM ``unified_llm_judge_base_prompt``. Retrieval and answering
are not re-run, so any score movement is pure judge effect.

The source artifact is never touched: output goes to
``<source>.rejudge-<tag>.jsonl`` (resumable per question row) plus a
``.summary.json`` pairing original vs re-judged scores per arm and type.
A seeded stability sample (a subset of (row, arm) pairs judged twice) is
reported alongside, because a CLI judge — unlike the pinned q8_0 server —
is not bit-reproducible and the flip rate bounds what a delta can claim.

Usage:
    PYTHONPATH=. python evals/beam_rejudge.py \
        --in evals/results/beam-100K-qwen-27b-beam100k-qwen38.jsonl \
        --beam-root <path-to-BEAM> --tag opus5 \
        [--judge-model claude-opus-5] [--workers 6] [--limit N] \
        [--stability-sample 60] [--arms rag,hybrid]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

from beam_adapter import judge_response, load_judge_prompt  # noqa: E402
from longmemeval_bench import ARMS, load_rows  # noqa: E402

# Canonical arm order (report() convention: hybrid_ev only after the rest).
ALL_ARMS = (*ARMS, "hybrid_ev")
DEFAULT_CLI = (os.environ.get("PSEUDOLIFE_SHIM_CLAUDE_CLI")
               or shutil.which("claude") or "claude")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Windows CreateProcess caps the command line at 32767 chars; leave margin
# (same constant as evals/claude_shim.py).
_MAX_ARGV_SYSTEM = 24000


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a timed-out call and its descendants.

    ``Popen.kill()`` on Windows is ``TerminateProcess`` on the DIRECT child
    only. The CLI is a node program behind a wrapper (``claude.cmd`` →
    ``cmd.exe`` → node), so the real claude survives holding the stdout
    pipe — and the reaping ``communicate()`` then blocks forever. Here that
    permanently eats a pool worker per timed-out call (and keeps the run
    from ever exiting) rather than wedging a serialization lock, but the
    kill is the same as evals/claude_shim.py's.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        # The child leads its own session (start_new_session in _run), so
        # killing the group takes its descendants too. proc.kill() alone
        # leaves a surviving grandchild holding the stdout pipe.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def build_cli_call(cli: str, model: str, system: str,
                   user: str) -> tuple[list[str], str]:
    """The headless ``claude -p`` invocation for one pure completion —
    the claude_shim contract: system goes to ``--system-prompt`` when it
    fits argv, otherwise it is folded onto stdin ahead of the user text."""
    cmd = [cli, "-p", "--model", model, "--output-format", "json",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--tools", ""]
    if system and len(system) <= _MAX_ARGV_SYSTEM:
        cmd += ["--system-prompt", system]
        return cmd, user
    if system:
        return cmd, f"{system}\n\n{user}"
    return cmd, user


def out_path_for(src: Path, tag: str) -> Path:
    return src.with_name(src.name.removesuffix(".jsonl")
                         + f".rejudge-{tag}.jsonl")


def detect_arms(rows: list[dict], only: str | None = None) -> tuple[str, ...]:
    """Arms come off the rows (a chronicle run carries hybrid_ev, a partial
    rerun may lack cortex); ``only`` keeps a subset and is loud about arms
    the source artifact never answered."""
    arms = tuple(a for a in ALL_ARMS if f"{a}_score" in rows[0])
    if only:
        keep = {a.strip() for a in only.split(",") if a.strip()}
        unknown = keep - set(arms)
        if unknown:
            raise SystemExit(f"--arms names {sorted(unknown)} but the source "
                             f"rows only carry {arms}")
        arms = tuple(a for a in arms if a in keep)
    return arms


class CliJudge:
    """Thread-safe ``claude -p`` judge with the chat-callable signature
    ``judge_response`` expects. One subprocess per call, one retry, and a
    final failure returns "" — which parses to None downstream and counts
    as a judge failure rather than aborting the row (the adapter's
    semantics). Unlike claude_shim's deliberately-serialized ClaudeCli,
    calls here run concurrently — the pool size is the caller's throttle."""

    def __init__(self, cli: str, model: str, call_timeout: float):
        self.cli = cli
        self.model = model
        self.call_timeout = call_timeout
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0

    def _run(self, cmd: list[str], payload: bytes) -> tuple[int, bytes, bytes]:
        """Spawn one call. Seam for tests, and the place the timeout
        kill-tree lives (``subprocess.run``'s timeout kills only the direct
        child). Per-call state, so the pooled callers need no lock here."""
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=(os.name != "nt"))
        try:
            out, err = proc.communicate(payload, timeout=self.call_timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()          # reap, so no zombie holds the pipes
            raise
        return proc.returncode, out, err

    def __call__(self, system: str, user: str, **_) -> str:
        cmd, payload = build_cli_call(self.cli, self.model, system, user)
        with self._lock:
            self.calls += 1
        for attempt in (1, 2):
            try:
                rc, stdout, stderr = self._run(cmd,
                                               payload.encode("utf-8"))
                if rc != 0:
                    raise RuntimeError(
                        f"claude -p rc={rc}: "
                        f"{stderr.decode('utf-8', 'replace')[:300]}")
                out = json.loads(stdout.decode("utf-8", "replace"))
                if out.get("is_error"):
                    raise RuntimeError("claude -p error result: "
                                       f"{str(out.get('result'))[:300]}")
                reply = (out.get("result") or "").strip()
                m = _FENCE_RE.match(reply)
                return m.group(1).strip() if m else reply
            except Exception as e:  # noqa: BLE001 — retry once, then degrade
                if attempt == 2:
                    with self._lock:
                        self.errors += 1
                    print(f"beam_rejudge: judge call failed twice: "
                          f"{str(e)[:200]}", file=sys.stderr, flush=True)
                    return ""
        return ""  # unreachable


def rejudge_row(row: dict, arms: tuple[str, ...], judge_prompt: str,
                judge) -> dict:
    """Judge every arm's recorded response afresh; keep the original scores
    beside the new ones so every downstream comparison pairs within-row."""
    out = {k: row.get(k) for k in ("chat_id", "tier", "type", "index",
                                   "question", "difficulty", "rubric")}
    # Provenance carried when the source recorded it (a budget-matched run
    # stamps hybrid_top_k); legacy rows stay legacy rather than gaining
    # explicit-None keys.
    out.update({k: row[k] for k in ("extractor", "hybrid_top_k")
                if k in row})
    for arm in arms:
        try:
            v = judge_response(judge_prompt, row["question"], row["rubric"],
                               row.get(f"{arm}_response", ""), chat=judge)
        except Exception as e:  # noqa: BLE001 — a row never dies mid-run
            print(f"beam_rejudge: arm {arm} failed wholesale: {e}",
                  file=sys.stderr, flush=True)
            v = {"llm_judge_score": 0.0, "llm_judge_score_intfaithful": 0.0,
                 "judge_failures": len(row["rubric"]), "items": []}
        out[f"{arm}_response"] = row.get(f"{arm}_response", "")
        out[f"{arm}_score_orig"] = row.get(f"{arm}_score")
        out[f"{arm}_score_intfaithful_orig"] = row.get(
            f"{arm}_score_intfaithful")
        out[f"{arm}_score"] = v["llm_judge_score"]
        out[f"{arm}_score_intfaithful"] = v["llm_judge_score_intfaithful"]
        out[f"{arm}_judge"] = v["items"]
        out[f"{arm}_judge_failures"] = v["judge_failures"]
    return out


def summarize(rows: list[dict], arms: tuple[str, ...], judge_model: str,
              source: str) -> dict:
    n = len(rows)
    summary = {"benchmark": "BEAM-rejudge", "source": source,
               "judge": judge_model, "n_questions": n,
               "scoring_note": ("paper-faithful float mean; _orig fields are "
                                "the source run's local-judge scores over "
                                "identical responses"),
               "arms": {}, "types": {}}
    if rows[0].get("hybrid_top_k") is not None:
        summary["hybrid_top_k"] = rows[0]["hybrid_top_k"]
    for arm in arms:
        # A row whose items ALL failed to judge scores 0.0 by the
        # max(len(scored), 1) convention — arithmetically identical to a
        # genuine zero. Count those rows next to the mean they sit inside:
        # a nonzero count says the delta carries judge outages, not just
        # judge opinion.
        dead = sum(1 for r in rows
                   if r[f"{arm}_judge_failures"] >= len(r["rubric"]))
        summary["arms"][arm] = {
            "score": round(sum(r[f"{arm}_score"] for r in rows) / n, 4),
            "score_intfaithful": round(
                sum(r[f"{arm}_score_intfaithful"] for r in rows) / n, 4),
            "score_orig": round(
                sum(r[f"{arm}_score_orig"] for r in rows) / n, 4),
            "delta": round(sum(r[f"{arm}_score"] - r[f"{arm}_score_orig"]
                               for r in rows) / n, 4),
            "judge_failures": sum(r[f"{arm}_judge_failures"] for r in rows),
            "rows_all_items_failed": dead,
        }
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for qtype, trows in sorted(by_type.items()):
        entry: dict = {"n": len(trows)}
        for arm in arms:
            entry[arm] = round(sum(r[f"{arm}_score"] for r in trows)
                               / len(trows), 4)
            entry[f"{arm}_orig"] = round(
                sum(r[f"{arm}_score_orig"] for r in trows) / len(trows), 4)
        summary["types"][qtype] = entry
    return summary


def stability_pairs(rows: list[dict], arms: tuple[str, ...],
                    n: int) -> list[tuple]:
    """A seeded, order-independent sample of (chat_id, type, index, arm)
    pairs to judge a second time."""
    import random
    pairs = sorted((r["chat_id"], r["type"], r["index"], arm)
                   for r in rows for arm in arms)
    return random.Random(0).sample(pairs, min(n, len(pairs)))


def stability_report(rows: list[dict], pairs: list[tuple], judge_prompt: str,
                     judge) -> dict:
    """Judge the sampled pairs once more and measure per-item agreement with
    the recorded pass — the CLI judge's flip rate, which bounds what any
    original-vs-rejudge delta can claim."""
    by_key = {(r["chat_id"], r["type"], r["index"]): r for r in rows}
    agree = deltas = total = 0
    detail = []
    for chat_id, qtype, index, arm in pairs:
        row = by_key[(chat_id, qtype, index)]
        v = judge_response(judge_prompt, row["question"], row["rubric"],
                           row.get(f"{arm}_response", ""), chat=judge)
        first = [i["score"] for i in row[f"{arm}_judge"]]
        second = [i["score"] for i in v["items"]]
        for a, b in zip(first, second):
            if a is None or b is None:
                continue
            total += 1
            agree += (a == b)
            deltas += abs(a - b)
        detail.append({"key": [chat_id, qtype, index, arm],
                       "first": first, "second": second})
    return {"n_pairs": len(pairs), "n_items": total,
            "item_agreement": round(agree / total, 4) if total else None,
            "mean_abs_delta": round(deltas / total, 4) if total else None,
            "pairs": detail}


def merge_stability(reports: list[dict], expected_items: int) -> dict:
    """Weighted merge of per-pair stability reports. ``expected_items``
    (the item count the sampled pairs SHOULD have compared) travels with
    the result: a second-pass CLI failure drops its items from n_items,
    and an agreement rate computed over a silent survivor subset would
    otherwise read as clean."""
    merged = {"n_pairs": sum(r["n_pairs"] for r in reports),
              "n_items": sum(r["n_items"] for r in reports),
              "expected_items": expected_items,
              "pairs": [p for r in reports for p in r["pairs"]]}
    items = agree = deltas = 0
    for r in reports:
        if r["n_items"]:
            items += r["n_items"]
            agree += r["item_agreement"] * r["n_items"]
            deltas += r["mean_abs_delta"] * r["n_items"]
    merged["item_agreement"] = round(agree / items, 4) if items else None
    merged["mean_abs_delta"] = round(deltas / items, 4) if items else None
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="src", required=True, type=Path,
                    help="existing BEAM per-question JSONL to re-judge")
    ap.add_argument("--beam-root", required=True, type=Path,
                    help="BEAM checkout (for the judge prompt; never "
                         "committed here)")
    ap.add_argument("--tag", required=True,
                    help="suffix for the output artifact, e.g. opus5")
    ap.add_argument("--judge-model", default="claude-opus-5")
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--call-timeout", type=float, default=240.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--arms", default=None,
                    help="comma-separated subset (default: all in the rows)")
    ap.add_argument("--limit", type=int, default=None,
                    help="re-judge only the first N rows (smoke)")
    ap.add_argument("--stability-sample", type=int, default=60,
                    help="(row, arm) pairs judged twice; 0 disables")
    args = ap.parse_args()

    src_rows = load_rows(args.src)
    if not src_rows:
        sys.exit(f"no rows in {args.src}")
    arms = detect_arms(src_rows, args.arms)
    judge_prompt = load_judge_prompt(args.beam_root)
    out_path = out_path_for(args.src, args.tag)
    if args.limit:
        src_rows = src_rows[:args.limit]

    judge = CliJudge(args.cli, args.judge_model, args.call_timeout)
    # Probe-gated abort (the 2026-07-04 launch-bug lesson): a logged-out or
    # missing CLI must fail the launch loudly, not run 3000 empty calls.
    if "OK" not in judge("", "Reply with exactly: OK"):
        sys.exit(f"judge probe failed: {args.cli} --model {args.judge_model} "
                 "did not answer (logged in? on PATH?)")

    done = {(r["chat_id"], r["type"], r["index"])
            for r in load_rows(out_path)}
    pending = [r for r in src_rows
               if (r["chat_id"], r["type"], r["index"]) not in done]
    print(f"beam_rejudge: {len(src_rows)} rows, arms={arms}, "
          f"judge={args.judge_model}, workers={args.workers} "
          f"({len(done)} already done) -> {out_path.name}", flush=True)

    finished = len(done)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(rejudge_row, r, arms, judge_prompt, judge): r
                   for r in pending}
        for fut in as_completed(futures):
            row = fut.result()          # rejudge_row never raises
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            finished += 1
            print(f"  {finished}/{len(src_rows)} "
                  f"{row['chat_id']}/{row['type']}[{row['index']}] "
                  + " ".join(f"{a}={row[f'{a}_score_orig']}->"
                             f"{row[f'{a}_score']:.2f}" for a in arms)
                  + (f" (cli_errors={judge.errors})" if judge.errors else ""),
                  flush=True)

    all_rows = load_rows(out_path)
    summary = summarize(all_rows, arms, args.judge_model, args.src.name)
    if args.stability_sample:
        print(f"beam_rejudge: stability sample "
              f"({args.stability_sample} pairs)...", flush=True)
        pairs = stability_pairs(all_rows, arms, args.stability_sample)
        by_key = {(r["chat_id"], r["type"], r["index"]): r for r in all_rows}
        expected = sum(len(by_key[p[:3]][f"{p[3]}_judge"]) for p in pairs)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            # One future per pair keeps the pool busy; stability_report
            # itself is sequential, so fan out here instead.
            futs = [pool.submit(stability_report, all_rows, [p],
                                judge_prompt, judge) for p in pairs]
            reports = [f.result() for f in futs]
        summary["stability_sample"] = merge_stability(reports, expected)
    # Counters land AFTER the stability pass so its several hundred calls
    # (and any of their failures) are visible in the artifact.
    summary["cli_calls"] = judge.calls
    summary["cli_errors"] = judge.errors
    summary["date"] = time.strftime("%Y-%m-%d")
    sum_path = out_path.with_name(
        out_path.name.removesuffix(".jsonl") + ".summary.json")
    sum_path.write_text(json.dumps(summary, indent=2) + "\n",
                        encoding="utf-8")
    slim = {k: v for k, v in summary.items() if k != "stability_sample"}
    slim["stability_sample"] = {
        k: v for k, v in summary.get("stability_sample", {}).items()
        if k != "pairs"}
    print(json.dumps(slim, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
