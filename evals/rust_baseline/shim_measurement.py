"""First-frame observation and paired-cell summaries for the fixture lane."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

from .common import controls, distribution, memory_tree, percentile
from .transport import Stdio

ORACLE_HEAD = "0b015f9279a778f996e71ee78510695e5fee7196"


def artifact_identity(path, expected=None):
    path = Path(path)
    identity = {"basename": path.name, "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if expected is not None and identity["sha256"] != expected:
        raise RuntimeError("executable identity differs from supplied frozen hash")
    return identity


def require_pinned_source(root):
    paths = ("pseudolife_memory", "pyproject.toml")
    changed = subprocess.run(["git", "diff", "--quiet", ORACLE_HEAD, "--", *paths],
                             cwd=root, capture_output=True, timeout=10)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", *paths],
                               cwd=root, capture_output=True, timeout=10)
    if changed.returncode != 0 or untracked.returncode != 0 or untracked.stdout.strip():
        raise RuntimeError("pinned Python oracle source is required")
    return {"oracle_head": ORACLE_HEAD, "production_source_matches_pin": True}


def candidate_source_identity(root):
    root = Path(root).resolve()
    paths = sorted(path for path in (root / "rust").rglob("*") if path.is_file()
                   and "target" not in path.relative_to(root).parts
                   and (path.suffix in (".rs", ".toml") or path.name == "Cargo.lock"))
    if not paths:
        raise RuntimeError("candidate Rust source is unavailable")
    return {"source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                                   text=True).strip(),
            "files_sha256": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in paths}}


class FrameStdio(Stdio):
    def __init__(self, *args):
        self.first_frame_at = None
        super().__init__(*args)

    def _read(self):
        try:
            for line in self.process.stdout:
                received_at = time.perf_counter()
                message = json.loads(line)
                if self.first_frame_at is None:
                    self.first_frame_at = received_at
                self.messages.put(message)
        except ValueError:
            if not self._closing:
                raise
        finally:
            self.messages.put(None)


class PeakRss:
    """Observed maximum simultaneous tree RSS, not a sum of per-process peaks."""
    def __init__(self, pid, interval=0.005):
        self.pid, self.interval = pid, interval
        self.peak = self.samples = 0
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        import psutil
        try:
            while not self.stop.is_set():
                try:
                    value = memory_tree(self.pid)["rss_bytes"]
                except psutil.NoSuchProcess:
                    break
                self.peak = max(self.peak, value)
                self.samples += 1
                self.stop.wait(self.interval)
        except Exception:
            self.error = True

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive() or self.error or not self.samples:
            raise RuntimeError("peak RSS sampler did not complete")


def metric_cells(runs):
    cells = {}
    for metric in ("startup_ms", "first_frame_ms", "peak_rss_bytes", "executable_bytes"):
        blocks = [[row[metric] for row in runs if row["repeat"] == repeat]
                  for repeat in sorted({row["repeat"] for row in runs})]
        cells[metric] = {"distribution": distribution([value for block in blocks for value in block]),
                         "median_control": controls(blocks)}
        for quantile in (50, 95):
            values = [percentile(block, quantile) for block in blocks]
            cells[metric][f"p{quantile}"] = {
                "value": percentile([value for block in blocks for value in block], quantile),
                "repeat_values": values,
                "noise_floor_abs": max(values) - min(values) if len(values) >= 2 else None,
                "method": "max minus min of identical-input repeat block quantiles"}
    return cells


def _clean(cleanup):
    if not cleanup["cleanup_confirmed"] or cleanup["exit_code"] != 0 or cleanup["forced"]:
        raise RuntimeError("shim did not exit cleanly")


def measure_pair(args, resource):
    import sys
    import tempfile
    from .common import child_environment, lease_gate, provenance
    from .transport import TOKEN, shim_fixture
    from evals.rust_port.processes import owned_process
    from evals.rust_port.provenance import runtime_probe_command

    root = args.source_root.resolve()
    pin = require_pinned_source(root)
    binary = args.candidate.resolve()
    rust_identity = artifact_identity(binary, args.candidate_sha256)
    source_identity = candidate_source_identity(args.candidate_root)
    python_identity = artifact_identity(sys.executable)
    runs = {"python": [], "rust": []}
    resource_checks = []
    for repeat in range(args.repeats):
        resource_checks.append(resource if repeat == 0 or args.smoke else
                               lease_gate(args.board_checked_at,
                                          offline_resource_checked_at=args.offline_resource_checked_at))
        for sample in range(args.samples):
            # Alternate the first arm, without simultaneous processes or hosts.
            order = ("python", "rust") if (repeat * args.samples + sample) % 2 == 0 else ("rust", "python")
            for arm in order:
                command = [sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python" else [str(binary)]
                with tempfile.TemporaryDirectory(prefix="plbench_shim_") as private:
                    env = child_environment(private)
                    runtime = rust_identity
                    if arm == "python":
                        with owned_process(runtime_probe_command(root), env=env, cwd=root) as probe:
                            output, _ = probe.communicate(timeout=15)
                            if probe.returncode:
                                raise RuntimeError("shim runtime probe failed")
                            runtime = json.loads(output)
                        if not runtime["source_origin_matches_selected_root"] or runtime["package_runtime_version"] != "0.16.0" \
                                or runtime["distribution_versions"]["pseudolife-mcp"] != "0.16.0":
                            raise RuntimeError("pinned oracle runtime/version does not match")
                    else:
                        artifact_identity(binary, rust_identity["sha256"])
                    with shim_fixture() as (url, observed):
                        env.update({"PSEUDOLIFE_MCP_DAEMON_URL": url, "PSEUDOLIFE_MCP_TOKEN": TOKEN})
                        # Preserve r5's boundary and sequence. No RSS polling thread
                        # runs in this timed process; peak RSS uses a separate launch.
                        started = time.perf_counter()
                        client = FrameStdio(command, env, root)
                        try:
                            initialized = client.initialize()
                            elapsed = (time.perf_counter() - started) * 1000
                            first_frame = (client.first_frame_at - started) * 1000
                            client.request("tools/list")
                            idle = memory_tree(client.process.pid)
                        finally:
                            cleanup = client.close()
                        _clean(cleanup)
                        client = Stdio(command, env, root)
                        with PeakRss(client.process.pid) as peak:
                            try:
                                peak_initialized = client.initialize()
                                client.request("tools/list")
                            finally:
                                peak_cleanup = client.close()
                        _clean(peak_cleanup)
                        if not observed["authorized"] or observed["initialize_requests"] < 2:
                            raise RuntimeError("shim fixture was not exercised by both launches")
                    if arm == "rust":
                        artifact_identity(binary, rust_identity["sha256"])
                    runs[arm].append({"repeat": repeat, "sample": sample, "arm_order": list(order),
                        "startup_ms": elapsed, "first_frame_ms": first_frame,
                        "peak_rss_bytes": peak.peak, "peak_rss_samples": peak.samples,
                        "peak_rss_poll_interval_s": peak.interval,
                        "idle_process_tree": idle, "protocol": initialized["protocolVersion"],
                        "peak_protocol": peak_initialized["protocolVersion"],
                        "executable_bytes": python_identity["bytes"] if arm == "python" else rust_identity["bytes"],
                        "actual_child_runtime": runtime, "fixture_auth_match": observed["authorized"],
                        "cleanup": cleanup, "peak_cleanup": peak_cleanup})
    require_pinned_source(root)
    if candidate_source_identity(args.candidate_root) != source_identity:
        raise RuntimeError("candidate source changed during measurement; receipt not finalizable")
    artifact_identity(binary, rust_identity["sha256"])
    historical = Path(__file__).resolve().parents[1] / "results/rust-rewrite-baseline-shim-20261003-r5.json"
    return {"schema": 2, "status": "contaminated-plumbing-smoke" if args.smoke else
            "quiet-fixture-pair-" + args.measurement_status, "bank_size": 0,
            "provenance": provenance(source_root=root), "python_oracle": pin,
            "candidate_source": source_identity, "candidate_executable": rust_identity,
            "python_executable": python_identity, "resource_check": resource,
            "repeat_resource_checks": resource_checks,
            "repeats": args.repeats, "samples_per_repeat": args.samples, "runs": runs,
            "metrics": {arm: metric_cells(rows) for arm, rows in runs.items()},
            "historical_method": {"artifact": historical.name,
                                  "sha256": hashlib.sha256(historical.read_bytes()).hexdigest(),
                                  "retained_metric": "startup_ms",
                                  "boundary": "before owned CLI spawn through initialize return and initialized notification"},
            "limitations": [
                "Fresh public CLI; warm OS filesystem cache; empty loopback HTTP fixture; no PG, models or daemon spawn.",
                "Coordination and Codex doorbell disabled; no live-bank access; private state removed after each cell.",
                "first_frame_ms ends at reader observation of the first complete stdout line, before JSON parsing.",
                "peak_rss_bytes is a sampled lower bound on simultaneous process-tree RSS, including descendants.",
                "Peak RSS has a separate untimed fresh process, sampled after ownership setup until EOF/exit; short peaks can be missed.",
                "executable_bytes is the selected launcher file only; Python runtime/dependency size is excluded.",
                "r5 contains initialize-return timing and idle RSS, not first-frame timing or peak RSS; those metrics have no historical match.",
                "Noise floors are descriptive repeat-block ranges, not confidence intervals; external desktop activity uncontrolled.",
                "Smoke proves plumbing and cleanup only; final measurements require the lead-frozen executable." if args.smoke else
                "Preliminary measurements are invalidated by later candidate changes." if args.measurement_status == "preliminary" else
                "Final status records the caller's frozen-binary signal; executable/source identities are checked throughout."]}
