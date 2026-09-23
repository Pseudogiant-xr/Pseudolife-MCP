"""Wall time, memory and thread cost of a pytest run — the test suite's own bench.

Runs ``python -m pytest <args>`` as a child and samples the whole process
tree (the pytest process plus any daemons or shells it spawns) once per
interval: private/committed memory where the platform reports it (resident
memory otherwise), thread count, and the system-wide commit charge on
Windows, where two concurrent full suites have hit the commit limit
(os error 1455). Writes one JSON artifact:

    python evals/suite_cost.py --tag master-slice -- tests/test_service.py -q

The pytest exit status is passed through, and the pass/fail/skip counts are
read from a JUnit report the harness asks pytest to write, so a red run can
never be mistaken for a measurement of a green one. A number without a
committed artifact was never really measured.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "evals" / "results"


def _commit_gb() -> float | None:
    """System-wide commit charge in GB (Windows only)."""
    if os.name != "nt":
        return None
    import ctypes

    class _Perf(ctypes.Structure):  # PERFORMANCE_INFORMATION
        _fields_ = [("cb", ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in (
            "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal",
            "PhysicalAvailable", "SystemCache", "KernelTotal", "KernelPaged",
            "KernelNonpaged", "PageSize")] + [
            ("HandleCount", ctypes.c_ulong), ("ProcessCount", ctypes.c_ulong),
            ("ThreadCount", ctypes.c_ulong)]

    perf = _Perf()
    perf.cb = ctypes.sizeof(perf)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(perf), perf.cb):
        return None
    return round(perf.CommitTotal * perf.PageSize / 2**30, 2)


def _private(proc: psutil.Process) -> int:
    info = proc.memory_info()
    return int(getattr(info, "private", info.rss))


class Sampler(threading.Thread):
    """Peak memory and threads of a process tree, sampled on an interval."""

    def __init__(self, root: psutil.Process, interval: float) -> None:
        super().__init__(daemon=True)
        self.root = root
        self.interval = interval
        self.stop = threading.Event()
        self.samples = 0
        self.peak_root_private = 0
        self.peak_children_private = 0
        self.peak_tree_private = 0
        self.peak_root_threads = 0
        self.peak_commit_gb = 0.0
        self.trace: list[tuple[float, int, int, int]] = []
        self._t0 = time.monotonic()
        self.pytest_proc: psutil.Process | None = None

    def _resolve(self) -> psutil.Process | None:
        """The process the tests run in, or None while it is not known yet.

        A Windows venv's python.exe is a launcher that re-runs the same
        command line under the base interpreter as its only child; the tests
        run in that child. Elsewhere the root is the pytest process itself,
        which is settled once it has run a few seconds with no such child.
        """
        def cmdline(p: psutil.Process) -> list[str] | None:
            try:
                return p.cmdline()[1:]
            except psutil.Error:  # a short-lived child already gone
                return None

        proc = self.root
        args = proc.cmdline()[1:]
        while True:
            same = [c for c in proc.children() if cmdline(c) == args]
            if len(same) != 1:
                break
            proc = same[0]
        if proc is self.root and time.monotonic() - self._t0 < 5.0:
            return None
        return proc

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                if self.pytest_proc is None:
                    self.pytest_proc = self._resolve()
                    if self.pytest_proc is None:
                        self.stop.wait(0.1)
                        continue
                root_private = _private(self.pytest_proc)
                threads = self.pytest_proc.num_threads()
                children = 0
                for child in self.pytest_proc.children(recursive=True):
                    try:
                        children += _private(child)
                    except psutil.Error:
                        pass
            except psutil.Error:
                break
            self.samples += 1
            self.peak_root_private = max(self.peak_root_private, root_private)
            self.peak_children_private = max(self.peak_children_private, children)
            self.peak_tree_private = max(self.peak_tree_private, root_private + children)
            self.peak_root_threads = max(self.peak_root_threads, threads)
            commit = _commit_gb()
            if commit is not None:
                self.peak_commit_gb = max(self.peak_commit_gb, commit)
            elapsed = round(time.monotonic() - self._t0, 1)
            self.trace.append((elapsed, root_private // 2**20, children // 2**20, threads))
            self.stop.wait(self.interval)


def _junit_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in ET.parse(path).getroot().iter("testsuite"):
        for key in counts:
            counts[key] += int(suite.get(key, 0))
    counts["passed"] = (counts["tests"] - counts["failures"] - counts["errors"]
                        - counts["skipped"])
    return counts


def provenance() -> dict:
    """The commit measured, and whether tracked files differed from it."""
    def git(*argv: str) -> str:
        try:
            return subprocess.run(["git", *argv], cwd=REPO, capture_output=True,
                                  text=True).stdout
        except OSError:
            return ""

    # Porcelain lines are "XY path": never strip them, the status column
    # can start with a space.
    changed = git("status", "--porcelain", "--untracked-files=no").splitlines()
    return {"git_sha": git("rev-parse", "HEAD").strip(),
            "tracked_changes": sorted(line[3:] for line in changed if line)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True, help="names the artifact")
    parser.add_argument("--note", default="", help="what differs from the commit, and why")
    parser.add_argument("--out", type=Path, help="default evals/results/suite-cost-<tag>.json")
    parser.add_argument("--interval", type=float, default=1.0, help="sampling seconds")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER,
                        help="arguments after -- go to pytest")
    args = parser.parse_args()
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    pytest_args = pytest_args or ["tests"]
    out = args.out or RESULTS / f"suite-cost-{args.tag}.json"

    junit = Path(tempfile.mkdtemp(prefix="suite-cost-")) / "junit.xml"
    cmd = [sys.executable, "-m", "pytest", *pytest_args, f"--junitxml={junit}"]
    started = time.time()
    t0 = time.monotonic()
    commit_before = _commit_gb()
    proc = subprocess.Popen(cmd, cwd=REPO)
    sampler = Sampler(psutil.Process(proc.pid), args.interval)
    sampler.start()
    status = proc.wait()
    wall = time.monotonic() - t0
    sampler.stop.set()
    sampler.join()

    import torch  # the interpreter's build decides CPU vs GPU runs

    gib = 2**30
    result = {
        "tag": args.tag,
        "note": args.note,
        "command": ["python", "-m", "pytest", *pytest_args],
        "exit_status": status,
        "counts": _junit_counts(junit),
        "wall_s": round(wall, 1),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        **provenance(),
        "peak_pytest_private_gb": round(sampler.peak_root_private / gib, 2),
        "peak_children_private_gb": round(sampler.peak_children_private / gib, 2),
        "peak_tree_private_gb": round(sampler.peak_tree_private / gib, 2),
        "peak_pytest_threads": sampler.peak_root_threads,
        "system_commit_gb": {"before": commit_before, "peak": sampler.peak_commit_gb or None},
        "samples": sampler.samples,
        "interval_s": args.interval,
        "memory_kind": "private" if os.name == "nt" else "rss",
        "host": {"platform": platform.platform(), "cpus": os.cpu_count(),
                 "python": platform.python_version(), "torch": torch.__version__,
                 "cuda_available": bool(torch.cuda.is_available())},
        "env": {name: os.environ.get(name) for name in (
            "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS", "HF_HUB_OFFLINE",
            "PSEUDOLIFE_TEST_EMBEDDER")},
        # (elapsed s, pytest private MB, children private MB, pytest threads)
        "trace": sampler.trace[:: max(1, len(sampler.trace) // 400)],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"suite-cost: exit {status}, wall {wall:.0f}s, peak pytest "
          f"{result['peak_pytest_private_gb']} GB / {sampler.peak_root_threads} "
          f"threads -> {out}", flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
