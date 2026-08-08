"""Warm-container root-cause probe: does cache_prompt=false restore
fresh-pass behavior on a warm server?

Pass 1: normal client (expect 19 claims / stale 0.1 on fresh server).
Pass 2 (warm): identical, but every chat body carries cache_prompt=false
via a json-shim patch scoped to the dream module's namespace.
Pass 3 (warm): normal client again (expect drift back if cache is causal).
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals"))

import ladder_sweep as ls                                  # noqa: E402

# dream.py imports json function-locally, so the only common seam in this
# isolated probe process is stdlib json.dumps itself — filtered narrowly to
# chat-shaped bodies (top-level "messages" key), which nothing else in the
# ladder path serializes.
_REAL_DUMPS = json.dumps


def _nocache_dumps(obj, **kw):
    if isinstance(obj, dict) and "messages" in obj:
        obj = {**obj, "cache_prompt": False}
    return _REAL_DUMPS(obj, **kw)


def one_pass(label):
    out = ls.run_rung("e4b-v3")
    row = {"label": label, "gold": out.get("gold_recoverable"),
           "stale": out.get("stale_leak"),
           "claims": (out.get("consolidation") or {}).get("claims")}
    print(json.dumps(row), flush=True)
    return row


rows = [one_pass("p1-fresh-normal")]
json.dumps = _nocache_dumps
rows.append(one_pass("p2-warm-nocache"))
json.dumps = _REAL_DUMPS
rows.append(one_pass("p3-warm-normal"))

out = REPO / "evals" / "results" / "warm-cache-probe-0809.json"
out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
print(f"-> {out}", flush=True)
