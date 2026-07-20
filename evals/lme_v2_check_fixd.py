"""LME-V2 Fix D — offline gate (CPU-only, no GPU/endpoints/model).

Fix D captures KB *article body text* so protocol prescriptions enter the
corpus. The sole evidence trajectory for question ``025db8ef`` (procedure; gold
``Reports;Problems``) reads the "Company Protocols - Agent Workload Balancing"
article, whose body prescribes "...access the list of reports... Re-assign the
... problem...". Fix A (page context) never emitted body text, so those phrases
were absent from the corpus and no extractor could recover the procedure.

This gate rebuilds the ingested corpus for ``025db8ef`` over its FULL haystack
(``--max-trajectories`` defaults to the whole thing, not the smoke's 20 cap),
BEFORE Fix D (``include_article_body=False``) and AFTER (``=True``), both with
observations ON (Fix A) so only the article-body delta moves. It then asserts
the article prescription now appears and the corpus stays bounded — article
pages are rare, so the delta must be small.

Pass condition: the phrase "list of reports" and the word "Re-assign" are
present AFTER (> 0) and Fix D INCREASED each (after > before — the article body
is what added them), and the corpus grows < 1.5x (bounded). ("Re-assign"
already occurs a couple of times in agent thoughts/actions, so the gate checks
the Fix-D increase, not a strict 0-before; "list of reports" is the clean
0 -> N case.)

Usage:
  PYTHONPATH=. python evals/lme_v2_check_fixd.py [--qid 025db8ef] [--max-trajectories N]
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

# Phrases from the article body that ground the procedure answer. "list of
# reports" is the exact prescription phrase; "Re-assign" is the article's step-3
# verb — both live ONLY in the article body Fix D newly captures.
GOLD_PHRASES = ("list of reports", "Re-assign")


def _corpus(trajectories, *, article: bool) -> str:
    """Observations ON either way (Fix A). ``article`` toggles Fix D so the
    before/after delta is the article body ALONE."""
    turns = []
    for traj in trajectories:
        turns.extend(A.trajectory_to_turns(
            traj, include_observations=True, include_article_body=article))
    return "\n".join(turns)


def _count(text: str, needle: str) -> int:
    return len(re.findall(re.escape(needle), text))


def _approx_tokens(s: str) -> int:
    return len(s) // 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qid", default="025db8ef")
    ap.add_argument("--max-trajectories", type=int, default=10**9,
                    help="cap trajectories (default: the WHOLE haystack — this "
                         "is a gate, not the smoke)")
    args = ap.parse_args()

    _bind_adapter_paths(resolve_data_dir())
    print(f"data dir: {A.DATA_DIR}")
    if not A.QUESTIONS_FILE.exists():
        print(f"!! questions.jsonl missing at {A.QUESTIONS_FILE}", file=sys.stderr)
        return 2

    haystack = A.load_small_haystack()
    full_ids = haystack.get(args.qid, [])
    ids = full_ids[:args.max_trajectories]
    trajs_by_id = load_trajectories_by_ids(ids)
    trajectories = [trajs_by_id[t] for t in ids if t in trajs_by_id]
    print(f"question {args.qid}: ingesting {len(trajectories)} of "
          f"{len(full_ids)} haystack trajectories "
          f"(cap {args.max_trajectories})")

    before = _corpus(trajectories, article=False)   # Fix A only
    after = _corpus(trajectories, article=True)      # Fix A + Fix D

    # How many article turns did Fix D add, over how many distinct articles?
    art_turns = [t for traj in trajectories
                 for t in A.trajectory_to_turns(
                     traj, include_observations=True, include_article_body=True)
                 if t.startswith("[article] ")]
    art_titles = sorted({t.split(":", 1)[0][len("[article] "):]
                         for t in art_turns})

    print("\n" + "=" * 64)
    print(f"{'metric':<26}{'BEFORE (Fix A)':>18}{'AFTER (Fix D)':>18}")
    print("-" * 64)
    print(f"{'corpus chars':<26}{len(before):>18}{len(after):>18}")
    print(f"{'corpus ~tokens':<26}{_approx_tokens(before):>18}"
          f"{_approx_tokens(after):>18}")
    for phrase in GOLD_PHRASES:
        b, a = _count(before, phrase), _count(after, phrase)
        print(f"{'occ ' + repr(phrase):<26}{b:>18}{a:>18}")
    print("-" * 64)
    grow = len(after) / max(1, len(before))
    print(f"article turns added : {len(art_turns)} over "
          f"{len(art_titles)} distinct article(s)")
    for t in art_titles:
        print(f"    - {t}")
    print(f"corpus growth       : {grow:.3f}x  "
          f"(+{len(after) - len(before)} chars)")
    print("=" * 64)

    # Gate: each gold phrase present AFTER and increased by Fix D (after >
    # before), and the corpus stays bounded.
    def _phrase_ok(p: str) -> bool:
        return _count(after, p) > 0 and _count(after, p) > _count(before, p)

    phrase_ok = all(_phrase_ok(p) for p in GOLD_PHRASES)
    bounded_ok = grow < 1.5
    ok = phrase_ok and bounded_ok
    for p in GOLD_PHRASES:
        print(f"GATE: {p!r}  {_count(before, p)} -> {_count(after, p)} "
              f"(need present & increased) -> "
              f"{'PASS' if _phrase_ok(p) else 'FAIL'}")
    print(f"GATE: growth {grow:.3f}x < 1.5x -> {'PASS' if bounded_ok else 'FAIL'}")
    print(f"\nCHECK (Fix D): {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
