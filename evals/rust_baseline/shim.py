"""Time fresh Python CLI processes through stdio initialization, using a fixture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

from .common import ROOT, child_environment, controls, distribution, lease_gate, memory_tree, provenance, write_result
from .transport import Stdio, TOKEN, shim_fixture
from evals.rust_port.processes import owned_process
from evals.rust_port.provenance import runtime_probe_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT, help="Python source checkout")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--board-checked-at", help="UTC time of lead's explicit board/peer resource-free verification")
    parser.add_argument("--offline-resource-checked-at",
                        help="UTC time of independent local/WSL lock and peer clearance when board unavailable")
    args = parser.parse_args()
    if min(args.repeats, args.samples) < 1:
        parser.error("positive repeats and samples required")
    resource = None
    if args.smoke:
        args.repeats = args.samples = 1
    else:
        resource = lease_gate(args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at)
    blocks, runs = [], []
    for repeat in range(args.repeats):
        block = []
        for sample in range(args.samples):
            with tempfile.TemporaryDirectory(prefix="plbench_shim_") as private:
                with shim_fixture() as (url, observed):
                    env = child_environment(private)
                    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": url, "PSEUDOLIFE_MCP_TOKEN": TOKEN})
                    with owned_process(runtime_probe_command(args.source_root.resolve()), env=env,
                                       cwd=args.source_root.resolve()) as probe:
                        runtime_output, _ = probe.communicate(timeout=15)
                        if probe.returncode:
                            raise RuntimeError("shim runtime probe failed")
                        runtime = json.loads(runtime_output)
                    started = time.perf_counter()
                    client = Stdio([sys.executable, "-m", "pseudolife_memory.cli"], env, args.source_root.resolve())
                    try:
                        initialized = client.initialize()
                        elapsed = (time.perf_counter() - started) * 1000
                        client.request("tools/list")
                        memory = memory_tree(client.process.pid)
                    finally:
                        cleanup = client.close()
                    if not cleanup["cleanup_confirmed"] or cleanup["exit_code"] != 0 or cleanup["forced"]:
                        raise RuntimeError("shim did not exit cleanly")
                    if not observed["authorized"] or observed["initialize_requests"] < 1:
                        raise RuntimeError("shim fixture was not exercised")
                    block.append(elapsed)
                    runs.append({"repeat": repeat, "sample": sample, "startup_ms": elapsed,
                                 "idle_process_tree": memory, "protocol": initialized["protocolVersion"],
                                 "actual_child_runtime": runtime,
                                 "runtime_observation": "separate untimed child with identical interpreter, environment and source root",
                                 "fixture_auth_match": observed["authorized"], "cleanup": cleanup})
        blocks.append(block)
    result = {"schema": 1, "status": "contaminated-plumbing-smoke" if args.smoke else "quiet-fixture-baseline",
              "provenance": provenance(source_root=args.source_root), "resource_check": resource, "runs": runs,
              "startup_ms": distribution([v for b in blocks for v in b]), "control": controls(blocks),
              "rss_root": "shim launcher and recursive descendants, including redirected runtime",
              "bank_size": 0, "limitations": [
                  "Fresh process startup through real CLI/discovery/auth/proxy to stdio initialize; warm OS filesystem cache.",
                  "Upstream is an empty loopback fixture; no daemon spawn, PG, embedding or default coordination attachment.",
                  "Coordination is explicitly disabled; this configuration must match any Rust comparison.",
                  "Smoke proves process launch, response, artifact and cleanup only; no baseline claim." if args.smoke
                  else "A quiet resource lease is required; external desktop activity is not controlled."]}
    write_result(args.out, result)


if __name__ == "__main__":
    main()
