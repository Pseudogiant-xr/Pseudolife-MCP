#!/usr/bin/env python
"""Paired-arm verdict for an extraction-ladder gate, in two modes.

``identity`` (the default, built for the chip-5 gate, PR #245) reads the
tagged ladder results written by two worktrees — the pre-#245 tree and
current master — for each rung and reports whether the deterministic
metrics AGREE. The gate's prediction (recorded before the run): the ladder
corpus carries no labels, so the TypeCompact carrier and guard are inert on
it and the two arms must be verdict-identical; any difference is a bug, not
a finding.

``threshold`` (2026-09-05) is the predicate for a change that is *expected*
to move the numbers — a prompt change. Identity is the wrong bar there: the
provenance-prompt gate reported ``FAIL`` only because ``tokens_per_query``
moved 13.4 -> 14.2, which the ladder's own rule does not penalise. Threshold
mode applies the ladder rule from ``evals/README.md`` ("Reading the
verdict") to BOTH arms, against ``naive-rag.json`` read from the results
directory:

    stale_leak        <  naive.stale_leak
    gold_recoverable  >  naive.gold_recoverable
    tokens_per_query  <= 0.6 * naive.tokens_per_query

and additionally requires the post arm to be no worse than the pre arm on
the two quality metrics (``post.stale_leak <= pre.stale_leak`` and
``post.gold_recoverable >= pre.gold_recoverable``). Tokens are reported and
bounded only by the 0.6x rule, so a change may cost tokens inside the
budget. Two verdicts come out, because they answer different questions:
``gate`` (did both arms clear the ladder on every rung) and
``no_regression_gate`` (did the post arm avoid going backwards anywhere).
A rung whose PRE arm does not clear the ladder fails ``gate`` while still
reporting its ``no_regression`` result — that is a finding about the rung,
not about the change.

Compared: gold_recoverable, stale_leak, tokens_per_query and the
consolidation tally (pulled / claims / inserted / superseded / literal_*).
Reported but NOT compared: extract_seconds, search_latency_ms (timing).

    python evals/ladder_pair_compare.py --pre <worktree> --post <worktree> \
        --tag chip5 --out evals/results/ladder-chip5-paired-verdict.json

    python evals/ladder_pair_compare.py --pre <worktree> --post <worktree> \
        --tag assistprompt --mode threshold --rungs qwen-27b,e4b-v3 \
        --out evals/results/ladder-assistprompt-paired-verdict-threshold.json

Each worktree must already hold ``evals/results/<rung>-<tag>-{pre,post}.json``
from ``ladder_sweep.py --rung <rung> --out-tag <tag>-<arm>``. ``--post-suffix``
names a post arm written under a different suffix, which is what a re-gate
looks like: the 2026-09-05 provenance prompt was rewritten after its gate
ran, so the shipped text's arm landed as ``…-assistprompt-post2.json``
beside the superseded ``…-post.json`` and the same ``pre`` baseline was
paired against it. The verdict names the arm it read in ``post_arm``, so a
verdict built on a superseded post run cannot be mistaken for a current one.

The verdict records the worktrees by basename and the per-rung files
repo-relative, so it carries no machine paths. Each arm's ``sha`` is read
off that arm's OWN artifacts (``git_rev``, stamped by ``ladder_sweep.py``)
rather than from the worktree's HEAD at compare time — see
``arm_provenance``, and ``sha_source`` for what the tool could establish.
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
# evals/README.md, "Reading the verdict": a rung clears if it reads no more
# than 60% of naive-RAG's tokens per query. Not a tuning constant of this
# tool — it is the ladder's published rule, restated here so the verdict
# artifact can state the bar it applied.
TOKEN_BUDGET_FRACTION = 0.6


def sha(wt: str) -> str:
    # OSError-tolerant to match `ladder_sweep.git_rev`: on a machine with no
    # git the tool should still produce a verdict that says it cannot tell,
    # not die inside the provenance block.
    try:
        return subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def arm_provenance(wt: str, revs: list) -> dict:
    """Which commit produced ONE arm, read from that arm's own artifacts.

    This used to be ``sha(wt)`` — the worktree's HEAD at compare time — and
    that answer is wrong in exactly the case the tool exists for. A re-gate
    runs both arms out of one worktree at different commits, so both arms
    got the same string and neither was the run's. The 2026-09-05 rule-v2
    shim verdict recorded ``7083bc33`` for both while its ``pre`` files had
    been produced at ``0b02e5ea``.

    ``ladder_sweep.py`` stamps ``git_rev`` into every rung artifact, so the
    answer is in the file for any run made after that change. Every artifact
    written before it — which is all of them at the time of writing —
    cannot answer, and ``null`` with a named ``sha_source`` is the honest
    report — a plausible wrong sha is worse than no sha, because nothing
    downstream can tell it apart from a real one.

    ``sha_source``:
      ``artifact``        every rung carries the same ``git_rev``, clean tree
      ``artifact-dirty``  ditto, but the run was made from a dirty worktree,
                          so the sha names a commit the code differed from
      ``mixed``           the rungs disagree, or only some carry one
      ``unstamped``       none carries one (runs predating the stamp)
      ``no_arm_files``    no rung of this arm could be read at all
    """
    if not revs:
        sha_v, source = None, "no_arm_files"
    elif all(revs) and len(set(revs)) == 1:
        sha_v = revs[0]
        # `ladder_sweep.git_rev` suffixes a dirty tree. Reporting that as a
        # plain `artifact` would be the confident-wrong-answer failure again,
        # one hop along: the sha is real, the code that ran was not it.
        source = "artifact-dirty" if sha_v.endswith("-dirty") else "artifact"
    elif not any(revs):
        sha_v, source = None, "unstamped"
    else:
        sha_v, source = None, "mixed"
    return {
        "worktree": Path(wt).name,
        "sha": sha_v,
        "sha_source": source,
        # Kept, but named for what it is: it dates the COMPARE, and says
        # nothing about which commit produced the runs being compared.
        "worktree_head_at_compare": sha(wt) or None,
    }


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


def load_naive(*trees: str) -> tuple[str, dict]:
    """The ladder bar lives in ``naive-rag.json``; without it there is no
    threshold to apply, so a missing baseline aborts rather than passing
    vacuously. Preferring the post tree keeps the bar the one the changed
    arm was measured against when the two trees differ."""
    rel = Path("evals") / "results" / "naive-rag.json"
    for wt in trees:
        p = Path(wt) / rel
        if p.exists():
            return rel.as_posix(), json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit(
        f"threshold mode needs {rel.as_posix()} in the pre or post tree — "
        "run `ladder_sweep.py --rung naive-rag` there first")


def clears_ladder(r: dict, naive: dict) -> list[str]:
    """The ladder rule (evals/README.md, 'Reading the verdict'), returned as
    the list of checks the arm FAILED — empty means it clears."""
    failed = []
    if not r.get("stale_leak", 1.0) < naive["stale_leak"]:
        failed.append("stale_leak")
    if not r.get("gold_recoverable", 0.0) > naive["gold_recoverable"]:
        failed.append("gold_recoverable")
    if not (r.get("tokens_per_query", float("inf"))
            <= TOKEN_BUDGET_FRACTION * naive["tokens_per_query"]):
        failed.append("tokens_per_query")
    return failed


def threshold_rung(pre: dict, post: dict, naive: dict) -> dict:
    """Both arms against the ladder bar, plus a no-going-backwards check on
    the two quality metrics. Tokens are deliberately absent from the
    pre/post comparison: a prompt change that costs tokens inside the
    budget is what the ladder's own rule already permits."""
    out = compare_rung(pre, post)
    pre_failed = clears_ladder(pre, naive)
    post_failed = clears_ladder(post, naive)
    failed = [f"pre.{c}" for c in pre_failed] + [f"post.{c}" for c in post_failed]

    regressions = []
    if post.get("stale_leak", 1.0) > pre.get("stale_leak", 1.0):
        regressions.append("post.stale_leak_vs_pre")
    if post.get("gold_recoverable", 0.0) < pre.get("gold_recoverable", 0.0):
        regressions.append("post.gold_recoverable_vs_pre")

    ok = pre.get("status") == "ok" and post.get("status") == "ok"
    out["pre_clears"] = not pre_failed and ok
    out["post_clears"] = not post_failed and ok
    out["no_regression"] = not regressions and ok
    out["failed_checks"] = failed + regressions
    out["cleared"] = out["pre_clears"] and out["post_clears"] and out["no_regression"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pre", required=True)
    ap.add_argument("--post", required=True)
    ap.add_argument("--tag", default="chip5")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=("identity", "threshold"),
                    default="identity",
                    help="identity: the arms must agree exactly (chip-5, a "
                         "change predicted inert). threshold: both arms must "
                         "clear the ladder bar and the post arm must not go "
                         "backwards (a prompt change, which is expected to "
                         "move the numbers).")
    ap.add_argument("--rungs", default=",".join(RUNGS),
                    help="comma-separated ladder rungs to compare "
                         f"(default: {','.join(RUNGS)})")
    ap.add_argument("--post-suffix", default="post",
                    help="arm suffix of the POST file, i.e. "
                         "<rung>-<tag>-<suffix>.json (default: post). A "
                         "change re-gated after its first run leaves the "
                         "superseded arm in place and lands the new one "
                         "beside it, e.g. `post2`; the pre arm is the same "
                         "baseline either way.")
    a = ap.parse_args()

    selected = [r.strip() for r in a.rungs.split(",") if r.strip()]
    naive_file = naive = None
    if a.mode == "threshold":
        naive_file, naive = load_naive(a.post, a.pre)

    rungs = {}
    # Per-ARM run provenance, gathered off the artifacts as they are read.
    # A rung that could not be read contributes nothing to either arm.
    revs = {"pre": [], "post": []}
    for rung in selected:
        p_pre, pre = load(a.pre, rung, a.tag, "pre")
        p_post, post = load(a.post, rung, a.tag, a.post_suffix)
        for arm, doc in (("pre", pre), ("post", post)):
            if doc is not None:
                revs[arm].append(doc.get("git_rev"))
        if pre is None or post is None:
            rungs[rung] = {"identical": False, "cleared": False,
                           "no_regression": False, "status": "missing",
                           "pre_file": p_pre.relative_to(a.pre).as_posix(),
                           "pre_present": pre is not None,
                           "post_file": p_post.relative_to(a.post).as_posix(),
                           "post_present": post is not None}
            continue
        rungs[rung] = (threshold_rung(pre, post, naive) if a.mode == "threshold"
                       else compare_rung(pre, post))
        # Repo-relative on purpose: the verdict is committed beside the
        # per-rung files it names, and a machine path would only tell a
        # reader where the run's worktree happened to live.
        rungs[rung]["pre_file"] = p_pre.relative_to(a.pre).as_posix()
        rungs[rung]["post_file"] = p_post.relative_to(a.post).as_posix()

    key = "cleared" if a.mode == "threshold" else "identical"
    gate = "PASS" if all(r.get(key) for r in rungs.values()) else "FAIL"
    verdict = {
        "what": ("chip-5 (PR #245 label pair) extraction-ladder paired arms: "
                 "pre-#245 tree vs master, same harness, same corpus, same "
                 "extractor endpoint; prediction = verdict-identical on an "
                 "unlabelled corpus")
        if a.mode == "identity" else
        ("extraction-ladder paired arms under the ladder's own threshold "
         "rule: both arms must beat naive-RAG on staleness and gold "
         "recovery inside 60% of its tokens/query, and the post arm must "
         "not go backwards on either quality metric"),
        "mode": a.mode,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "tag": a.tag,
        # Which post-arm files were read. A verdict that does not say this
        # cannot be told apart from one built on a superseded post run.
        "post_arm": a.post_suffix,
        "pre": arm_provenance(a.pre, revs["pre"]),
        "post": arm_provenance(a.post, revs["post"]),
        "compared": list(METRICS) + [f"consolidation.{k}" for k in TALLY],
        "not_compared": list(TIMING),
        "rungs": rungs,
        "gate": gate,
    }
    if a.mode == "threshold":
        verdict["rule"] = (
            "stale_leak < naive.stale_leak; gold_recoverable > "
            "naive.gold_recoverable; tokens_per_query <= "
            f"{TOKEN_BUDGET_FRACTION} * naive.tokens_per_query — applied to "
            "BOTH arms; plus post.stale_leak <= pre.stale_leak and "
            "post.gold_recoverable >= pre.gold_recoverable. The "
            "consolidation tally is reported, not gated.")
        verdict["naive"] = {
            "file": naive_file,
            "gold_recoverable": naive["gold_recoverable"],
            "stale_leak": naive["stale_leak"],
            "tokens_per_query": naive["tokens_per_query"],
            "token_budget": TOKEN_BUDGET_FRACTION * naive["tokens_per_query"],
        }
        # Two verdicts, because they answer different questions: `gate` is
        # "does this rung clear the ladder at all", which a rung can fail on
        # its BASELINE arm; `no_regression_gate` is "did the change make
        # anything worse", which is the question the change is on trial for.
        verdict["no_regression_gate"] = (
            "PASS" if all(r.get("no_regression") for r in rungs.values())
            else "FAIL")
    Path(a.out).write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    return 0 if gate == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
