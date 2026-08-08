"""Ladder replication with a fresh candidate container per pass.

The evlora campaign (2026-08-08, `evals/results/evlora-verdict.json`
followups) found that a WARM llama.cpp candidate container corrupts
subsequent ladder passes: stale_leak 1.0 on passes 2-3 against 0.1 on
every fresh-container pass, while all campaign numbers were
fresh-container single-pass. The ladder itself is deterministic, so
replicate disagreement means the environment drifted — most plausibly the
server's default request-level prompt cache (`cache_prompt`), which
neither the extractor client nor the container CMD pins off.

This runner makes fresh-per-pass the paved road: it owns the container
lifecycle (rm -f, run, health-poll) around each `ladder_sweep --rung`
invocation, computes agreement over the deterministic metrics only, and
REFUSES to bury a disagreement in an average — a warm/drifted pass set is
reported as exactly that. `--keep-warm` inverts the behavior (container
started once, reused) for deliberate hazard characterization runs.

    PYTHONPATH=. python evals/ladder_replicate.py --rung e4b-v3 --n 3 \
        --image pseudolife-extractor:gemma4-e4b --tag rep-<date>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RESULTS_DIR = Path(__file__).resolve().parent / "results"
HAZARD_NOTE = (
    "warm-container hazard (evlora 2026-08-08): reusing a llama.cpp "
    "candidate container across ladder passes corrupted stale_leak "
    "(1.0 warm vs 0.1 fresh); this runner restarts the container per "
    "pass unless --keep-warm is set for characterization")


def docker_commands(image: str, name: str, port: int):
    rm = ["docker", "rm", "-f", name]
    run = ["docker", "run", "-d", "--name", name,
           "-p", f"127.0.0.1:{port}:{port}", image]
    return rm, run


def wait_health(port: int, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def passes_agree(results: list[dict]) -> dict:
    """Agreement over the deterministic rung metrics only. Latency and
    token counts jitter run-to-run and are excluded by design."""
    bad = [i for i, r in enumerate(results) if r.get("status") != "ok"]
    if bad:
        return {"agree": False,
                "detail": f"pass(es) {bad} not ok "
                          f"({[results[i].get('status') for i in bad]}"
                          " includes unreachable/failed)",
                "gold_values": [r.get("gold_recoverable") for r in results],
                "stale_values": [r.get("stale_leak") for r in results]}
    golds = [r["gold_recoverable"] for r in results]
    stales = [r["stale_leak"] for r in results]
    agree = len(set(golds)) == 1 and len(set(stales)) == 1
    return {"agree": agree,
            "detail": "all passes identical" if agree else
                      "passes disagree on deterministic metrics — "
                      "environment drift (warm container?); do not average",
            "gold_values": golds, "stale_values": stales}


def write_artifact(out: Path, payload: dict) -> None:
    payload = {**payload, "hazard_note": HAZARD_NOTE,
               "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"artifact -> {out}", flush=True)


def _run_pass(rung: str, tag: str) -> dict:
    """One ladder rung invocation in a subprocess; returns its result dict
    (read back from the tagged artifact ladder_sweep writes)."""
    cmd = [sys.executable, str(Path(__file__).parent / "ladder_sweep.py"),
           "--rung", rung, "--out-tag", tag]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    artifact = RESULTS_DIR / f"{rung}-{tag}.json"
    if proc.returncode != 0 or not artifact.exists():
        return {"rung": rung, "status": "failed",
                "stderr_tail": (proc.stderr or "")[-500:]}
    return json.loads(artifact.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rung", required=True)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--image", default="pseudolife-extractor:gemma4-e4b")
    ap.add_argument("--container-name",
                    default="pseudolife-mcp-extractor-bench")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--keep-warm", action="store_true",
                    help="start the container once and reuse it — hazard "
                         "characterization only, never for numbers")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rm, run = docker_commands(args.image, args.container_name, args.port)
    results = []
    for i in range(1, args.n + 1):
        if i == 1 or not args.keep_warm:
            subprocess.run(rm, capture_output=True)
            subprocess.run(run, check=True, capture_output=True)
            if not wait_health(args.port):
                print(f"pass {i}: container never became healthy — abort",
                      flush=True)
                results.append({"rung": args.rung, "status": "unreachable"})
                break
        r = _run_pass(args.rung, f"{args.tag}-r{i}")
        results.append(r)
        print(f"pass {i}: status={r.get('status')} "
              f"gold={r.get('gold_recoverable')} "
              f"stale={r.get('stale_leak')}", flush=True)
    subprocess.run(rm, capture_output=True)

    agreement = passes_agree(results)
    out = args.out or RESULTS_DIR / f"ladder-replicate-{args.tag}.json"
    write_artifact(out, {"tag": args.tag, "rung": args.rung,
                         "mode": "keep_warm" if args.keep_warm else "fresh",
                         "image": args.image, "passes": results,
                         "agreement": agreement})
    if not agreement["agree"]:
        print(f"WARNING: {agreement['detail']}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
