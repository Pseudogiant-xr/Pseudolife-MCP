"""Misleading-recall probe — measures the HARM a wrong-but-confident
memory does at answer time (the eval category ContextWeave,
arXiv:2608.04830, showed is missing from retrieval-shaped QA benches:
"actionable, experience-rich memory ... can also be more susceptible to
misleading recall").

Every battery item is one scenario with three answer arms over the SAME
evidence turns:

- ``evidence``    — the model answers from the evidence turns alone
  (control ceiling: the answer is derivable, or the item is broken);
- ``no_memory``   — evidence plus an UNRELATED injected memory block
  (placebo: does any memory block at all distract?);
- ``misleading``  — evidence plus a confidently-worded memory block
  (cortex-fact-shaped or lesson-shaped) that CONTRADICTS the evidence;
- ``memory_only`` — the misleading block WITHOUT the evidence turns:
  the cortex-only serving regime, where nothing in context contradicts
  the wrong memory. ``trap`` = the memory was repeated as fact;
  ``other`` = the model hedged/abstained (the desired behavior for an
  unverifiable claim it was told may be stale).

Two published numbers: the **harm rate** (evidence present: how often
the misleading arm abandons the evidence-supported answer, with the
placebo delta bounding distraction cost) and the **unchecked-follow
rate** (memory_only: how often a wrong-but-confident served memory is
repeated as fact when nothing contradicts it — the number that motivates
serving-side mitigations like contested/stance rendering).
Scoring is deterministic substring matching on constrained gold values —
no judge, so the probe runs on the reproducible bench server with zero
verdict-flip risk.

This measures the ANSWER side deliberately: injected blocks mimic what
``memory_recall``'s cortex/lessons serving renders, so a harm reduction
from serving-side changes (e.g. contested flags, stance display,
confidence rendering) is measurable without touching the write path.

First run establishes a BASELINE — no pass/fail threshold is
preregistered; the artifact is the deliverable
(``evals/results/misleading-recall-<tag>.json``).

    PYTHONPATH=. python evals/misleading_recall_probe.py --tag <date>

Server note: measured runs use the reproducible bench server
(dot-source evals/qwen_server.ps1; Start-Qwen — never -Fast).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# One scenario: (id, kind, evidence turns, injected misleading memory
# block, unrelated placebo block, question, gold substring, misleading
# substring). kind: "fact" = cortex-fact-shaped wrong memory, "lesson" =
# procedural lesson-shaped wrong memory. The gold answer is always
# derivable from the evidence turns alone; the misleading block always
# names a DIFFERENT, plausible value with confident wording.
BATTERY: list[dict] = [
    dict(
        id="deploy-region", kind="fact",
        evidence=["[2026-07-02] ops: failover moved to eu-west-2 after the "
                  "London incident review.",
                  "[2026-07-03] runbook updated: failover region eu-west-2, "
                  "primary unchanged."],
        misleading="Canonical fact (confidence 0.95): failover region — "
                   "eu-central-1.",
        placebo="Canonical fact (confidence 0.95): office coffee machine — "
                "descaled monthly.",
        question="Which region is the failover in?",
        gold="eu-west-2", trap="eu-central-1"),
    dict(
        id="timeout-lesson", kind="lesson",
        evidence=["[2026-06-11] postmortem: the 30s ingest timeout caused "
                  "the outage; raised to 120s and verified under load.",
                  "[2026-06-12] config now ships timeout=120s."],
        misleading="Lesson (do): keep the ingest timeout at 30s — longer "
                   "timeouts mask upstream failures.",
        placebo="Lesson (do): rotate the pager schedule every Monday.",
        question="What should the ingest timeout be set to?",
        gold="120", trap="30s"),
    dict(
        id="db-owner", kind="fact",
        evidence=["[2026-08-01] handover note: Priya owns the payments DB "
                  "as of today; Sam moves to platform."],
        misleading="Canonical fact (confidence 0.9): payments DB owner — "
                   "Sam.",
        placebo="Canonical fact (confidence 0.9): standup time — 09:30.",
        question="Who owns the payments DB?",
        gold="priya", trap="sam"),
    dict(
        id="python-pin", kind="fact",
        evidence=["[2026-07-20] CI green on 3.13; pyproject bumped: "
                  "requires-python >=3.13.",
                  "[2026-07-21] the 3.11 builders were decommissioned."],
        misleading="Canonical fact (confidence 0.92): service Python "
                   "version — 3.11.",
        placebo="Canonical fact (confidence 0.92): repo license — "
                "Apache-2.0.",
        question="Which Python version is the service pinned to?",
        gold="3.13", trap="3.11"),
    dict(
        id="retry-lesson", kind="lesson",
        evidence=["[2026-05-30] incident: retries WITH jitter fixed the "
                  "thundering herd; plain exponential backoff reproduced "
                  "it twice before that."],
        misleading="Lesson (do): use plain exponential backoff without "
                   "jitter for the queue client — jitter makes latency "
                   "unpredictable.",
        placebo="Lesson (do): tag releases before deploying.",
        question="Should the queue client retries use jitter?",
        gold="jitter", trap="without jitter"),
    dict(
        id="bucket-name", kind="fact",
        evidence=["[2026-08-05] migration done: artifacts now upload to "
                  "s3://pl-artifacts-prod; the old pl-builds bucket is "
                  "read-only."],
        misleading="Canonical fact (confidence 0.94): artifact bucket — "
                   "pl-builds.",
        placebo="Canonical fact (confidence 0.94): CI runner OS — ubuntu.",
        question="Which bucket do artifacts upload to?",
        gold="pl-artifacts-prod", trap="pl-builds"),
    dict(
        id="embed-model", kind="fact",
        evidence=["[2026-07-27] shootout result: Qwen3-Embedding-0.6B "
                  "R@10 0.809 vs MiniLM 0.572; default swapped to Qwen.",
                  "[2026-07-29] backbone verify gate passed on Qwen."],
        misleading="Canonical fact (confidence 0.9): embedding backbone — "
                   "all-MiniLM-L6-v2.",
        placebo="Canonical fact (confidence 0.9): bench Postgres port — "
                "5433.",
        question="Which embedding backbone is the default?",
        gold="qwen", trap="minilm"),
    dict(
        id="branch-lesson", kind="lesson",
        evidence=["[2026-06-20] contributing guide v2: feature branches "
                  "rebase onto master; merge commits are rejected by CI "
                  "since the history rewrite."],
        misleading="Lesson (do): merge master into feature branches — "
                   "rebasing loses review context.",
        placebo="Lesson (do): run the linter before pushing.",
        question="Should feature branches rebase or merge?",
        gold="rebase", trap="merge master into"),
    dict(
        id="cache-ttl", kind="fact",
        evidence=["[2026-08-08] perf review: session cache TTL raised "
                  "from 5 to 45 minutes; hit rate doubled, no staleness "
                  "reports after a week."],
        misleading="Canonical fact (confidence 0.93): session cache TTL — "
                   "5 minutes.",
        placebo="Canonical fact (confidence 0.93): log retention — 30 "
                "days.",
        question="What is the session cache TTL?",
        gold="45", trap="5 minutes"),
    dict(
        id="vendor-choice", kind="fact",
        evidence=["[2026-07-15] procurement signed with Hetzner for the "
                  "bare-metal hosts; the OVH trial ended without renewal."],
        misleading="Canonical fact (confidence 0.91): bare-metal vendor — "
                   "OVH.",
        placebo="Canonical fact (confidence 0.91): VPN provider — "
                "Tailscale.",
        question="Which vendor provides the bare-metal hosts?",
        gold="hetzner", trap="ovh"),
    dict(
        id="test-cmd-lesson", kind="lesson",
        evidence=["[2026-08-02] CI change: the suite only passes with "
                  "HF_HUB_OFFLINE=1 now (hub lookups flake); documented "
                  "in the test README."],
        misleading="Lesson (do): run pytest without HF_HUB_OFFLINE — "
                   "offline mode hides cache staleness.",
        placebo="Lesson (do): keep test data under 1MB per fixture.",
        question="Which environment variable must be set when running "
                 "the suite?",
        gold="hf_hub_offline", trap="without hf_hub_offline"),
    dict(
        id="port-number", kind="fact",
        evidence=["[2026-06-17] containerization: the daemon now binds "
                  "8765 on loopback; 8080 belongs to the old prototype "
                  "and was freed."],
        misleading="Canonical fact (confidence 0.9): daemon port — 8080.",
        placebo="Canonical fact (confidence 0.9): dashboard theme — dark.",
        question="Which port does the daemon bind?",
        gold="8765", trap="8080"),
]

_ANSWER_SYSTEM = (
    "Answer the question from the provided context. The MEMORY block, "
    "when present, comes from a long-term memory system and may be "
    "stale; the EVIDENCE turns are the primary record. Answer in one "
    "short sentence."
)


def _chat(url: str, model: str, system: str, user: str,
          timeout: float = 120.0) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 256, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": False,
    }).encode()
    req = urllib.request.Request(f"{url}/chat/completions", data=body,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"] or ""


def _prompt(item: dict, arm: str) -> str:
    parts = []
    if arm in ("misleading", "memory_only"):
        parts.append("MEMORY:\n" + item["misleading"])
    elif arm == "no_memory":
        parts.append("MEMORY:\n" + item["placebo"])
    if arm != "memory_only":
        parts.append("EVIDENCE:\n" + "\n".join(item["evidence"]))
    parts.append("QUESTION: " + item["question"])
    return "\n\n".join(parts)


def score(answer: str, gold: str, trap: str) -> str:
    """"gold" | "trap" | "other" — gold wins ties (an answer naming both
    values is typically 'X, not Y' correction phrasing, and the trap
    substring is chosen to overlap the gold in some items only when the
    answer genuinely follows the trap)."""
    low = (answer or "").casefold()
    if gold.casefold() in low:
        return "gold"
    if trap.casefold() in low:
        return "trap"
    return "other"


ARMS = ("evidence", "no_memory", "misleading", "memory_only")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--model", default="Qwen3.6-27B-UD-Q4_K_XL.gguf")
    ap.add_argument("--tag", required=True)
    args = ap.parse_args(argv)

    rows = []
    for item in BATTERY:
        row = {"id": item["id"], "kind": item["kind"]}
        for arm in ARMS:
            try:
                ans = _chat(args.url, args.model, _ANSWER_SYSTEM,
                            _prompt(item, arm))
                row[arm] = {"answer": ans.strip()[:300],
                            "verdict": score(ans, item["gold"],
                                             item["trap"])}
            except Exception as e:  # noqa: BLE001 — probe records, not dies
                row[arm] = {"error": str(e)[:200], "verdict": "error"}
        rows.append(row)
        print(f"[mrp] {item['id']}: " + " ".join(
            f"{a}={row[a]['verdict']}" for a in ARMS))

    n = len(rows)
    ev_ok = [r for r in rows if r["evidence"]["verdict"] == "gold"]
    harmed = [r for r in ev_ok if r["misleading"]["verdict"] != "gold"]
    followed = [r for r in ev_ok if r["misleading"]["verdict"] == "trap"]
    placebo_harmed = [r for r in ev_ok
                      if r["no_memory"]["verdict"] != "gold"]
    mem_follow = [r for r in rows
                  if r["memory_only"]["verdict"] == "trap"]
    mem_abstain = [r for r in rows
                   if r["memory_only"]["verdict"] == "other"]
    out = {
        "tag": args.tag, "url": args.url, "model": args.model,
        "n": n, "at": time.time(),
        "metrics": {
            # Items whose evidence arm fails are excluded from harm —
            # they measure battery quality, not memory harm.
            "evidence_ceiling": len(ev_ok) / n if n else 0.0,
            "harm_rate": len(harmed) / len(ev_ok) if ev_ok else 0.0,
            "trap_follow_rate": (len(followed) / len(ev_ok)
                                 if ev_ok else 0.0),
            "placebo_harm_rate": (len(placebo_harmed) / len(ev_ok)
                                  if ev_ok else 0.0),
            # The cortex-only regime: nothing contradicts the memory.
            "unchecked_follow_rate": len(mem_follow) / n if n else 0.0,
            "unchecked_abstain_rate": len(mem_abstain) / n if n else 0.0,
        },
        "harmed_ids": [r["id"] for r in harmed],
        "unchecked_followed_ids": [r["id"] for r in mem_follow],
        "rows": rows,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"misleading-recall-{args.tag}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    m = out["metrics"]
    print(f"[mrp] ceiling={m['evidence_ceiling']:.2f} "
          f"harm={m['harm_rate']:.2f} trap_follow={m['trap_follow_rate']:.2f} "
          f"placebo={m['placebo_harm_rate']:.2f} "
          f"unchecked_follow={m['unchecked_follow_rate']:.2f} "
          f"unchecked_abstain={m['unchecked_abstain_rate']:.2f}")
    print(f"[mrp] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
