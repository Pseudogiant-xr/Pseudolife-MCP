"""Compare current stdio shim transports on the same disposable MCP workload.

This bounded CPU fixture comparison measures the whole shim process tree.
It is not a daemon benchmark or evidence about retrieval performance.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_shim_transport_recovery import _Fixture, _proxy_client, _replace_token, OLD_TOKEN


def response_hash(result):
    """Compare all parsed MCP result fields, including error and metadata."""
    encoded = json.dumps(result.model_dump(mode="json", by_alias=True),
                         sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ProcessSamples:
    """Sample only children spawned by this benchmark driver, without argv."""
    def __init__(self):
        self.parent = psutil.Process()
        self.existing = {(p.pid, p.create_time()) for p in self.parent.children(recursive=True)}
        self.owned = set()
        self.rows = []
        self.phase = "startup"
        self.done = Event()
        self.thread = Thread(target=self.run, daemon=True)

    def run(self):
        while not self.done.is_set():
            total, count = 0, 0
            for process in self.parent.children(recursive=True):
                try:
                    identity = (process.pid, process.create_time())
                    if identity in self.existing:
                        continue
                    self.owned.add(identity)
                    total += process.memory_info().rss
                    count += 1
                except psutil.Error:
                    continue
            self.rows.append((self.phase, total, count))
            # Measurement resolution: sample every 10 ms; short peaks between
            # samples can be missed, so the artifact calls these sampled RSS.
            self.done.wait(0.01)

    def finish(self):
        self.done.set()
        self.thread.join(timeout=2)
        alive = []
        for pid, created in self.owned:
            try:
                process = psutil.Process(pid)
                if process.create_time() == created and process.is_running():
                    alive.append(process)
            except psutil.Error:
                pass
        clean = not alive
        for process in alive:
            try:
                process.kill()
                process.wait(timeout=5)
            except psutil.Error:
                pass
        return {"owned_cleanup_before_emergency_stop": clean,
                "sample_count": len(self.rows),
                "max_process_count": max((row[2] for row in self.rows), default=0),
                "sampled_total_rss_peak_bytes": max((row[1] for row in self.rows), default=0),
                "sampled_total_rss_mean_bytes": statistics.mean(row[1] for row in self.rows),
                "sampled_phase_peak_bytes": {
                    phase: max(row[1] for row in self.rows if row[0] == phase)
                    for phase in sorted({row[0] for row in self.rows})}}


async def workload(fixture, directory, calls, concurrency, samples):
    token_file = directory / "token"
    _replace_token(token_file, OLD_TOKEN)
    started = time.perf_counter()
    async with _proxy_client(fixture, token_file, directory / "stderr.log",
                             operation_timeout=10) as client:
        result = {"startup_initialize_seconds": time.perf_counter() - started}
        # The SDK caches its initialized result; this adds no wire request.
        initialized = await client.initialize()
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {"read", "write", "bulk"}
        responses = {"initialize": response_hash(initialized),
                     "list_tools": response_hash(listed), "ordinary": []}
        samples.phase = "ordinary"
        durations = []
        for index in range(calls):
            before = time.perf_counter()
            returned = await client.call_tool("read", {"label": f"fixture-{index}"})
            duration = time.perf_counter() - before
            assert json.loads(returned.content[0].text)["label"] == f"fixture-{index}"
            durations.append(duration)
            responses["ordinary"].append(response_hash(returned))
        result["ordinary_call_seconds"] = durations
        samples.phase = "bulk"
        before = time.perf_counter()
        bulk = await client.call_tool("bulk", {"bytes": 1_400_000})
        result["bulk_call_seconds"] = time.perf_counter() - before
        assert bulk.content[0].text == "b" * 1_400_000
        responses["bulk"] = response_hash(bulk)
        samples.phase = "concurrent"
        before = time.perf_counter()
        burst = await asyncio.gather(*[client.call_tool("read", {"label": f"burst-{i}"})
                                       for i in range(concurrency)])
        result["concurrent_burst_seconds"] = time.perf_counter() - before
        assert [json.loads(item.content[0].text)["label"] for item in burst] == [
            f"burst-{i}" for i in range(concurrency)]
        responses["concurrent"] = [response_hash(item) for item in burst]
        result["response_sha256"] = responses
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-binary", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--calls", type=int, default=10)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--build-profile", choices=("debug", "release", "unspecified"),
                        default="unspecified")
    args = parser.parse_args()
    if not args.rust_binary.is_file() or min(args.calls, args.replicates, args.concurrency) < 1:
        parser.error("an existing Rust binary and positive workload sizes are required")
    binary = args.rust_binary.resolve()
    # Exclusive creation refuses overwriting a previous tagged artifact.
    with args.out.open("x", encoding="utf-8") as output:
        sources = ["pseudolife_memory/shim.py", "pseudolife_memory/rust_transport.py",
                   "rust/http-transport/Cargo.toml", "rust/http-transport/Cargo.lock",
                   "rust/http-transport/src/main.rs", "tests/test_shim_transport_recovery.py",
                   "evals/bench_rust_transport.py"]
        artifact = {"format_version": 1, "status": "running",
                    "measured_at_utc": datetime.now(timezone.utc).isoformat(),
                    "source_head": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    "source_staged_tree": subprocess.check_output(
                        ["git", "write-tree"], cwd=ROOT, text=True).strip(),
                    "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                      for name in sources},
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "build_profile": args.build_profile,
                    "rust_toolchain": {name: subprocess.check_output(
                        [name, "--version"], text=True).strip() for name in ("cargo", "rustc")},
                    "dependencies": {name: version(name) for name in ("mcp", "httpx2", "anyio", "psutil")},
                    "platform": {"system": platform.system(), "release": platform.release(),
                                 "architecture": platform.machine(), "python": platform.python_version()},
                    "workload": {"calls": args.calls, "replicates": args.replicates,
                                 "concurrency": args.concurrency, "bulk_bytes": 1_400_000,
                                 "warmup_calls": 0, "order": "alternating paired ABBA"},
                    "functional_equivalence": {"status": "unverified", "completed_pairs": 0,
                        "scope": "complete parsed MCP initialize, list and call results"},
                    "rss_scope": "sum of Python stdio shim and all its child processes; driver and HTTP fixture excluded",
                    "scope": "local disposable HTTP fixture; startup remains Python/SDK; no daemon or live bank",
                    "rows": []}
        def persist():
            output.seek(0)
            json.dump(artifact, output, indent=2)
            output.write("\n")
            output.truncate()
            output.flush()
        persist()
        saved = os.environ.copy()
        try:
            for name in list(os.environ):
                if name.startswith("PSEUDOLIFE_") or name.upper() in {
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}:
                    del os.environ[name]
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
            os.environ["NO_PROXY"] = "127.0.0.1,localhost"
            os.environ["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:1"
            for replicate in range(args.replicates):
                order = ("python", "rust") if replicate % 2 == 0 else ("rust", "python")
                for backend in order:
                    if backend == "rust":
                        os.environ["PSEUDOLIFE_MCP_RUST_HTTP"] = str(binary)
                    else:
                        os.environ.pop("PSEUDOLIFE_MCP_RUST_HTTP", None)
                    fixture, samples = _Fixture(), ProcessSamples()
                    fixture.start()
                    samples.thread.start()
                    row = {"backend": backend, "replicate": replicate}
                    try:
                        with tempfile.TemporaryDirectory(prefix="pseudolife-http-bench-") as temporary:
                            directory = Path(temporary)
                            os.environ["CODEX_HOME"] = str(directory / "client-home")
                            row.update(asyncio.run(asyncio.wait_for(workload(
                                fixture, directory, args.calls, args.concurrency, samples), timeout=60)))
                            row["status"] = "passed"
                    finally:
                        fixture.stop()
                        row.update(samples.finish())
                        artifact["rows"].append(row)
                        persist()
                    if not row["owned_cleanup_before_emergency_stop"]:
                        raise RuntimeError("owned process cleanup failed")
                python_row, rust_row = sorted(artifact["rows"][-2:], key=lambda row: row["backend"])
                if python_row["response_sha256"] != rust_row["response_sha256"]:
                    artifact["functional_equivalence"]["status"] = "failed"
                    raise AssertionError("complete MCP results differ between transports")
                artifact["functional_equivalence"]["completed_pairs"] += 1
                persist()
            artifact["functional_equivalence"]["status"] = "passed"
            artifact["status"] = "passed"
        except BaseException as error:
            artifact["status"] = "failed"
            artifact["error_type"] = type(error).__name__
        finally:
            os.environ.clear()
            os.environ.update(saved)
            persist()
    print(f"comparison status: {artifact['status']}; rows: {len(artifact['rows'])}")
    return 0 if artifact["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
