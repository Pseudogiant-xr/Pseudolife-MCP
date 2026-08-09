"""Consolidation-quarantine gate 2: common-path non-inferiority (ladder).

Preregistration: docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md

Runs the e4b rung twice against one fresh extractor container — quarantine
OFF, then ON with EMPTY ``trusted_sources`` (the paranoid configuration) —
and asserts ``gold_recoverable`` / ``stale_leak`` and the write tally are
identical, with the parked count reported honestly.

Structural note, stated up front rather than discovered later: the ladder
ingests every turn with ``source="bench"``, which maps to NO origin tier
(``_origin_from_source``), so the low-trust predicate cannot fire on this
bank by construction — even the paranoid arm parks nothing here. What this
run therefore verifies is that the restructured dream claim loop is
metric-identical on the real GPU path (the regression the CPU tests cannot
see); the predicate's firing behavior is pinned by
``tests/test_dream_quarantine.py``. The prereg's "trusted_sources includes
the bench source" arm is subsumed: trusting a source that is never
low-trust is the same code path.

Determinism: the extractor client pins ``cache_prompt: false`` (the
2026-08-09 warm-container fix), so two passes against one warm container
are byte-comparable.

    PYTHONPATH=. python evals/quarantine_gate.py --tag qgate-<date>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PREREG = "docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md"


def consolidate_with_counters(svc, extractor) -> tuple[dict, dict]:
    """Like ladder_sweep.consolidate but also sums the quarantine counters."""
    tally = {"pulled": 0, "claims": 0, "inserted": 0, "superseded": 0,
             "literal_flagged": 0, "literal_dropped": 0}
    qt = {"quarantine_parked": 0, "quarantine_held": 0,
          "quarantine_promoted": 0}
    while True:
        res = svc.dream_run(extractor, limit=100)
        for k in tally:
            tally[k] += int(res.get(k, 0))
        for k in qt:
            qt[k] += int(res.get(k, 0))
        if not res.get("pulled"):
            break
    return tally, qt


def run_arm_with_counters(rung: str, quarantine: bool) -> dict:
    import ladder_sweep as ls

    with tempfile.TemporaryDirectory(prefix="plqgate_",
                                     ignore_cleanup_errors=True) as td:
        svc = ls.build_service(Path(td))
        if quarantine:
            svc.config.memory.dream.quarantine_low_trust = True
            svc.config.memory.dream.trusted_sources = []
        ls.ingest(svc)
        extractor = ls.make_extractor(ls.RUNGS[rung])
        t0 = time.perf_counter()
        tally, qt = consolidate_with_counters(svc, extractor)
        elapsed = time.perf_counter() - t0
        metrics = ls.measure_cortex(svc)
        return {"quarantine": quarantine,
                "consolidate_seconds": round(elapsed, 1),
                "tally": tally, **qt,
                "gold_recoverable": metrics["gold_recoverable"],
                "stale_leak": metrics["stale_leak"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rung", default="e4b-v3")
    ap.add_argument("--image", default="pseudolife-extractor:gemma4-e4b")
    ap.add_argument("--container", default="pseudolife-quarantine-gate")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    import ladder_replicate as lr
    import ladder_sweep as ls

    # The port is DERIVED from the rung's base_url, never a flag: a
    # mismatched publish would silently bench whatever else answers on
    # the rung's port ("something answered the probe" is not proof).
    port = int(ls.RUNGS[args.rung]["base_url"].rsplit(":", 1)[-1]
               .split("/", 1)[0])

    rm, run = lr.docker_commands(args.image, args.container, port)
    subprocess.run(rm, capture_output=True, check=False)
    subprocess.run(run, capture_output=True, check=True)
    try:
        if not lr.wait_health(port):
            print("extractor container never became healthy", flush=True)
            return 1
        off = run_arm_with_counters(args.rung, quarantine=False)
        on = run_arm_with_counters(args.rung, quarantine=True)
    finally:
        subprocess.run(rm, capture_output=True, check=False)

    identical = (off["gold_recoverable"] == on["gold_recoverable"]
                 and off["stale_leak"] == on["stale_leak"]
                 and off["tally"] == on["tally"])
    payload = {
        "preregistration": PREREG,
        "tag": args.tag, "rung": args.rung, "image": args.image,
        "arms": {"quarantine_off": off, "quarantine_on_paranoid": on},
        "gate2_non_inferiority": {
            "pass": identical and on["quarantine_parked"] == 0,
            "metrics_identical": identical,
            "paranoid_parked": on["quarantine_parked"],
            "structural_note": (
                "bench entries carry source='bench' (no origin tier), so "
                "the predicate cannot fire on this bank — firing behavior "
                "is pinned by tests/test_dream_quarantine.py; this run "
                "verifies the restructured claim loop is metric-identical "
                "on the real path"),
        },
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = args.out or RESULTS_DIR / f"quarantine-gate-{args.tag}.json"
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps(payload["gate2_non_inferiority"]), flush=True)
    print(f"artifact -> {out}", flush=True)
    return 0 if payload["gate2_non_inferiority"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
