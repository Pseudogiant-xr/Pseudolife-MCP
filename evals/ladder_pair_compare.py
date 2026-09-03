#!/usr/bin/env python
"""Paired-arm verdict for the chip-5 extraction-ladder gate (PR #245).

Reads the tagged ladder results written by two worktrees — the pre-#245
tree and current master — for each rung and reports whether the
deterministic metrics agree. The gate's prediction (recorded before the
run): the ladder corpus carries no labels, so the TypeCompact carrier and
guard are inert on it and the two arms must be verdict-identical; any
difference is a bug, not a finding.

Compared: gold_recoverable, stale_leak, tokens_per_query and the
consolidation tally (pulled / claims / inserted / superseded / literal_*).
Reported but NOT compared: extract_seconds, search_latency_ms (timing).

    python evals/ladder_pair_compare.py --pre <worktree> --post <worktree> \
        --tag chip5 --out evals/results/ladder-chip5-paired-verdict.json

Each worktree must already hold ``evals/results/<rung>-<tag>-{pre,post}.json``
from ``ladder_sweep.py --rung <rung> --out-tag <tag>-<arm>``. The verdict
records the worktrees by basename and the per-rung files repo-relative,
so it carries no machine paths.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

RUNGS = ("floor", "qwen-27b")
METRICS = ("gold_recoverable", "stale_leak", "tokens_per_query")
TALLY = ("pulled", "claims", "inserted", "superseded",
         "literal_flagged", "literal_dropped")
TIMING = ("extract_seconds", "search_latency_ms")


def sha(wt: str) -> str:
    return subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def load(wt: str, rung: str, tag: str, arm: str) -> tuple[Path, dict | None]:
    p = Path(wt) / "evals" / "results" / f"{rung}-{tag}-{arm}.json"
    return p, (json.loads(p.read_text(encoding="utf-8")) if p.exists() else None)


def compare_rung(pre: dict, post: dict) -> dict:
    diffs = {}
    for m in METRICS:
        if pre.get(m) != post.get(m):
            diffs[m] = {"pre": pre.get(m), "post": post.get(m)}
    tpre, tpost = pre.get("consolidation", {}), post.get("consolidation", {})
    for k in TALLY:
        if tpre.get(k) != tpost.get(k):
            diffs[f"consolidation.{k}"] = {"pre": tpre.get(k), "post": tpost.get(k)}
    return {
        "status": {"pre": pre.get("status"), "post": post.get("status")},
        "metrics": {m: {"pre": pre.get(m), "post": post.get(m)} for m in METRICS},
        "consolidation": {k: {"pre": tpre.get(k), "post": tpost.get(k)} for k in TALLY},
        "timing": {t: {"pre": pre.get(t), "post": post.get(t)} for t in TIMING},
        "differences": diffs,
        "identical": (not diffs and pre.get("status") == "ok"
                      and post.get("status") == "ok"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pre", required=True)
    ap.add_argument("--post", required=True)
    ap.add_argument("--tag", default="chip5")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rungs = {}
    for rung in RUNGS:
        p_pre, pre = load(a.pre, rung, a.tag, "pre")
        p_post, post = load(a.post, rung, a.tag, "post")
        if pre is None or post is None:
            rungs[rung] = {"identical": False, "status": "missing",
                           "pre_file": p_pre.relative_to(a.pre).as_posix(),
                           "pre_present": pre is not None,
                           "post_file": p_post.relative_to(a.post).as_posix(),
                           "post_present": post is not None}
            continue
        rungs[rung] = compare_rung(pre, post)
        # Repo-relative on purpose: the verdict is committed beside the
        # per-rung files it names, and a machine path would only tell a
        # reader where the run's worktree happened to live.
        rungs[rung]["pre_file"] = p_pre.relative_to(a.pre).as_posix()
        rungs[rung]["post_file"] = p_post.relative_to(a.post).as_posix()

    gate = "PASS" if all(r.get("identical") for r in rungs.values()) else "FAIL"
    verdict = {
        "what": ("chip-5 (PR #245 label pair) extraction-ladder paired arms: "
                 "pre-#245 tree vs master, same harness, same corpus, same "
                 "extractor endpoint; prediction = verdict-identical on an "
                 "unlabelled corpus"),
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "tag": a.tag,
        "pre": {"worktree": Path(a.pre).name, "sha": sha(a.pre)},
        "post": {"worktree": Path(a.post).name, "sha": sha(a.post)},
        "compared": list(METRICS) + [f"consolidation.{k}" for k in TALLY],
        "not_compared": list(TIMING),
        "rungs": rungs,
        "gate": gate,
    }
    Path(a.out).write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    return 0 if gate == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
