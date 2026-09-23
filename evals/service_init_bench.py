"""Cost of starting a MemoryService — time, threads and memory per start.

Starts N file-mode services one after another, each on a fresh data
directory, with the embedding model stubbed out so only the service's own
start-up is measured, and keeps every service alive the way the test suite
leaves dropped services in garbage cycles. Anything a start leaks for the
life of the process (the 2026-09-23 ChromaDB client cache: ~19 threads and
~3 MB per start) shows up in the thread count and private memory at the end.

    python evals/service_init_bench.py --tag lazy-reference-bank --n 50

Writes a JSON artifact by default; a number without a committed artifact was
never really measured.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "evals" / "results"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals"))

from suite_cost import provenance  # noqa: E402


class _Embedding:
    """Stands in for EmbeddingPipeline: start-up reads only the dimension."""

    embedding_dim = 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True, help="names the artifact")
    parser.add_argument("--note", default="", help="what differs from the commit, and why")
    parser.add_argument("--n", type=int, default=50, help="service starts")
    parser.add_argument("--out", type=Path,
                        help="default evals/results/service-init-<tag>.json")
    args = parser.parse_args()
    out = args.out or RESULTS / f"service-init-{args.tag}.json"

    os.environ.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)  # file mode
    import pseudolife_memory.service as service_module

    service_module.EmbeddingPipeline = lambda config: _Embedding()
    proc = psutil.Process()
    root = Path(tempfile.mkdtemp(prefix="service-init-bench-"))
    threads_before = proc.num_threads()
    alive, seconds = [], []
    for i in range(args.n):
        started = time.perf_counter()
        svc = service_module.MemoryService(data_dir=root / f"s{i}")
        svc._ensure_init()  # noqa: SLF001 — the start-up under test
        seconds.append(time.perf_counter() - started)
        alive.append(svc)
    info = proc.memory_info()
    later = seconds[1:]  # the first start also pays one-off imports
    result = {
        "tag": args.tag,
        "note": args.note,
        "n": args.n,
        **provenance(),
        "first_start_s": round(seconds[0], 4),
        "mean_start_s": round(sum(later) / len(later), 4),
        "threads_before": threads_before,
        "threads_after": proc.num_threads(),
        "private_mb_after": round(getattr(info, "private", info.rss) / 2**20),
        "memory_kind": "private" if os.name == "nt" else "rss",
        "host": {"platform": platform.platform(), "cpus": os.cpu_count(),
                 "python": platform.python_version()},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
