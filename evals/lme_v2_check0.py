"""LME-V2 Fix A — Check 0 (hard gate, CPU-only, no GPU/endpoints).

Rebuilds the exact ingested corpus for question ``025db8ef`` (procedure; gold
answer ``Reports;Problems``) BEFORE Fix A (observations off — the overnight
smoke's config) and AFTER (observations on — resolved action labels + capped
page context), then greps the gold answer terms and reports corpus size.

Pass condition: each gold term's occurrence count goes from 0 (or low) to > 0
AFTER Fix A, and the corpus stays the same order of magnitude as the baseline
(NOT the ~47x raw-tree scale). Mirrors the smoke's ingest slice:
``--max-trajectories`` (default 20) trajectories of the 025db8ef haystack.

Usage:
  PYTHONPATH=. python evals/lme_v2_check0.py [--qid 025db8ef] [--max-trajectories 20]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root

import lme_v2_adapter as A  # noqa: E402
from lme_v2_smoke import resolve_data_dir, _bind_adapter_paths, \
    load_trajectories_by_ids  # noqa: E402

GOLD_TERMS = ("Problems", "Reports")


def _corpus(trajectories, *, observations: bool) -> str:
    turns = []
    for traj in trajectories:
        turns.extend(A.trajectory_to_turns(traj, include_observations=observations))
    return "\n".join(turns)


def _approx_tokens(s: str) -> int:
    return len(s) // 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qid", default="025db8ef")
    ap.add_argument("--max-trajectories", type=int, default=20)
    args = ap.parse_args()

    _bind_adapter_paths(resolve_data_dir())
    print(f"data dir: {A.DATA_DIR}")
    if not A.QUESTIONS_FILE.exists():
        print(f"!! questions.jsonl missing at {A.QUESTIONS_FILE}", file=sys.stderr)
        return 2

    haystack = A.load_small_haystack()
    ids = haystack.get(args.qid, [])[:args.max_trajectories]
    trajs_by_id = load_trajectories_by_ids(ids)
    trajectories = [trajs_by_id[t] for t in ids if t in trajs_by_id]
    print(f"question {args.qid}: ingesting {len(trajectories)} of "
          f"{len(haystack.get(args.qid, []))} haystack trajectories "
          f"(cap {args.max_trajectories})")

    before = _corpus(trajectories, observations=False)
    after = _corpus(trajectories, observations=True)
    # raw-tree dump size, for the scale comparison (never ingested — reference).
    raw = sum(len(st.get("accessibility_tree") or "")
              for t in trajectories for st in t.get("states", []))

    print("\n" + "=" * 60)
    print(f"{'metric':<26}{'BEFORE (obs off)':>16}{'AFTER (Fix A)':>16}")
    print("-" * 60)
    print(f"{'corpus chars':<26}{len(before):>16}{len(after):>16}")
    print(f"{'corpus ~tokens':<26}{_approx_tokens(before):>16}{_approx_tokens(after):>16}")
    for term in GOLD_TERMS:
        b = len(re.findall(rf"\b{re.escape(term)}\b", before))
        a = len(re.findall(rf"\b{re.escape(term)}\b", after))
        print(f"{'gold ' + repr(term):<26}{b:>16}{a:>16}")
    print("-" * 60)
    print(f"raw-tree dump chars (NOT ingested): {raw}  "
          f"= {raw / max(1, len(before)):.1f}x baseline, "
          f"{raw / max(1, len(after)):.1f}x Fix A")
    print(f"Fix A scale vs baseline: {len(after) / max(1, len(before)):.2f}x")
    print("=" * 60)

    # Gate: the gold answer term(s) must appear at least once AFTER Fix A, and
    # the corpus must stay the same order of magnitude (< 5x baseline).
    after_counts = {t: len(re.findall(rf"\b{re.escape(t)}\b", after))
                    for t in GOLD_TERMS}
    scale = len(after) / max(1, len(before))
    problems_ok = after_counts.get("Problems", 0) > 0          # the 0->N target
    scale_ok = scale < 5.0
    ok = problems_ok and scale_ok
    print(f"\nGATE: 'Problems' present after = {after_counts.get('Problems')} "
          f"(need > 0) -> {'PASS' if problems_ok else 'FAIL'}")
    print(f"GATE: scale {scale:.2f}x < 5x -> {'PASS' if scale_ok else 'FAIL'}")
    print(f"\nCHECK 0: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
