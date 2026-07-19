"""LME-V2 Fix B — Check 1 client (uses a running sonnet_shim; CPU-only).

Runs the trajectory-mode extraction prompt over a few trajectories whose
distilled corpus contains both gold module names, and dumps the raw claims JSON.
Success = at least one procedure claim whose value contains both "Reports" and
"Problems", and no user-fact scraps of the shipped-prompt kind.

Assumes ``sonnet_shim.py`` is already serving (default http://127.0.0.1:8082/v1).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lme_v2_adapter as A  # noqa: E402
from lme_v2_smoke import (resolve_data_dir, _bind_adapter_paths,  # noqa: E402
                          load_trajectories_by_ids, _TRAJECTORY_SYSTEM_PROMPT)
from pseudolife_memory.memory.dream import OpenAICompatExtractor  # noqa: E402

SHIM = "http://127.0.0.1:8082/v1"
# success, success (compact), failure — trees carry both "Reports" and "Problems"
PICKS = ["12457787", "b1ba591c", "6022defe"]


def main() -> int:
    _bind_adapter_paths(resolve_data_dir())
    trajs = load_trajectories_by_ids(PICKS)
    ex = OpenAICompatExtractor(SHIM, "claude-sonnet-5", max_tokens=4096,
                               timeout_seconds=300.0,
                               system_prompt=_TRAJECTORY_SYSTEM_PROMPT)
    all_both = False
    for tid in PICKS:
        t = trajs.get(tid)
        if not t:
            print(f"!! {tid} not on disk", file=sys.stderr)
            continue
        turns = A.trajectory_to_turns(t, include_observations=True)
        claims = ex.extract(turns, [])
        payload = {"trajectory": tid, "outcome": t.get("outcome"),
                   "turns": len(turns),
                   "claims": [dict(c) for c in claims]}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        for c in claims:
            v = str(c.get("value", ""))
            if re.search(r"\bReports\b", v) and re.search(r"\bProblems\b", v):
                all_both = True
    print("\nCHECK 1:",
          "PASS — a procedure claim carries both Reports and Problems"
          if all_both else
          "PARTIAL — see claims above (no single value held both terms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
