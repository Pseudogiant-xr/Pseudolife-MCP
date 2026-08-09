"""Production-sidecar cache_prompt latency probe (deferred from PR #123).

The warm-cache root-cause work pinned that llama-server's default prompt
cache changes model output once populated; the ladder pins it OFF, but the
PRODUCTION sidecar still runs with it on — so live extraction output can
depend on cache state. Pinning it off in the daemon trades prefill latency
for determinism. This probe measures that trade on the LIVE sidecar so the
decision is a number, not a guess.

Protocol: alternating requests (cache-default vs ``cache_prompt: false``),
each a realistic dream-extraction shape — the shipped system prompt as the
shared prefix (what the cache would actually reuse across batches) plus a
distinct numbered-notes batch per request. ``max_tokens`` is small so the
timing signal is dominated by prefill, which is where the two configs
differ. Timing is curl's ``time_total`` inside the container (no host
overhead asymmetry). Unjudged output — determinism itself was already
established by ``warm-cache-probe-0809``.

Perturbs nothing durable: the sidecar is stateless between requests apart
from the very cache being measured.

    PYTHONPATH=. python evals/sidecar_cache_probe.py --tag sidecar-cache-<date>
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "ku_op_prompt_v5.txt"
CONTAINER = "pseudolife-mcp-extractor"
URL = "http://localhost:8081/v1/chat/completions"

_TOPICS = [
    ("build-farm", "runner count", "14", "16"),
    ("edge-proxy", "cert serial", "5c31f2", "9d44a0"),
    ("billing-api", "version", "3.8.1", "3.9.0"),
    ("metrics-db", "retention days", "45", "60"),
    ("auth-gateway", "replica count", "6", "8"),
    ("search-cluster", "shard count", "24", "32"),
    ("cache-tier", "engine", "keydb", "dragonfly"),
    ("ingest-queue", "max lag seconds", "120", "90"),
]


def batch_text(i: int) -> str:
    """A distinct 6-note numbered batch per request index."""
    lines = []
    for n in range(6):
        ent, attr, v1, v2 = _TOPICS[(i * 6 + n) % len(_TOPICS)]
        val = v1 if (i + n) % 2 == 0 else v2
        lines.append(f"[{n}] probe-{i}: the {ent} {attr} is now {val}")
    return "\n".join(lines)


def one_request(system: str, user: str, cache_prompt: bool | None) -> dict:
    body = {"model": "extractor",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 32}
    if cache_prompt is not None:
        body["cache_prompt"] = cache_prompt
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "curl", "-s",
         "-w", "\n%{time_total}", "-X", "POST",
         "-H", "content-type: application/json",
         "--data-binary", "@-", URL],
        input=json.dumps(body).encode(), capture_output=True, timeout=600)
    out = proc.stdout.decode(errors="replace").rstrip().rsplit("\n", 1)
    return {"seconds": float(out[-1]), "ok": proc.returncode == 0
            and '"choices"' in out[0]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", type=int, default=4,
                    help="alternating (default, nocache) request pairs")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    system = PROMPT_FILE.read_text(encoding="utf-8")
    # Warmup: populate the cache with the shared prefix so the DEFAULT arm
    # measures the steady warm state production actually runs in.
    one_request(system, batch_text(0), cache_prompt=None)

    default_s, nocache_s = [], []
    for i in range(1, args.pairs + 1):
        d = one_request(system, batch_text(2 * i), cache_prompt=None)
        n = one_request(system, batch_text(2 * i + 1), cache_prompt=False)
        assert d["ok"] and n["ok"], (d, n)
        default_s.append(d["seconds"])
        nocache_s.append(n["seconds"])
        print(f"pair {i}: default {d['seconds']:.2f}s "
              f"| nocache {n['seconds']:.2f}s", flush=True)

    payload = {
        "container": CONTAINER, "tag": args.tag,
        "prompt_chars": len(system), "pairs": args.pairs,
        "default_seconds": default_s,
        "nocache_seconds": nocache_s,
        "default_mean": round(statistics.mean(default_s), 2),
        "nocache_mean": round(statistics.mean(nocache_s), 2),
        "nocache_penalty_seconds": round(
            statistics.mean(nocache_s) - statistics.mean(default_s), 2),
        "note": ("penalty = prefill of the shared system prompt per call; "
                 "the dream is a background sweep (600s interval) so "
                 "latency tolerance is high — weigh against the measured "
                 "warm-cache output nondeterminism (warm-cache-probe-0809)"),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = args.out or RESULTS_DIR / f"sidecar-cache-latency-{args.tag}.json"
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps({k: payload[k] for k in
                      ("default_mean", "nocache_mean",
                       "nocache_penalty_seconds")}), flush=True)
    print(f"artifact -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
