"""The allocator probe harness must not turn a failed burst into evidence.

Review of PR #347 (2026-09-24): the threads4 workload joined raw threads,
and a worker that raised (MemoryError, say) only reached
``threading.excepthook``; the burst then measured and reported as if all
four encodes had run, so a short-handed burst could read as lower memory
or better latency. The driver also accepted a result line without checking
the container's exit status.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))
import allocator_trim_probe as probe  # noqa: E402


def test_a_worker_error_fails_the_burst() -> None:
    done: list[int] = []

    def work(k: int) -> None:
        if k == 2:
            raise MemoryError("worker 2 ran out of memory")
        done.append(k)

    before = threading.active_count()
    with pytest.raises(MemoryError, match="worker 2"):
        probe._run_threads(work, [(k,) for k in range(4)])
    # Every worker was still started and joined: the lifecycle measured is
    # unchanged, only the error now surfaces.
    assert sorted(done) == [0, 1, 3]
    assert threading.active_count() == before


def test_a_clean_burst_returns() -> None:
    done: list[int] = []
    probe._run_threads(done.append, [(k,) for k in range(4)])
    assert sorted(done) == [0, 1, 2, 3]


class _Proc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout.encode()
        self.stderr = b"Traceback (most recent call last): ...\n"


@pytest.mark.parametrize("returncode", [1, -9])
def test_a_failed_container_is_not_a_result(monkeypatch, returncode: int) -> None:
    """Even a well-formed result line does not stand if the process failed."""
    line = probe.RESULT_MARK + json.dumps({"steps": []})
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Proc(returncode, line + "\n"))
    with pytest.raises(RuntimeError, match=f"exit {returncode}"):
        probe._run_one("img", "src", "fp32", "threads4", "ctrl", None)


def test_a_clean_container_returns_its_result(monkeypatch) -> None:
    line = probe.RESULT_MARK + json.dumps({"steps": [], "ok": True})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(0, line + "\n"))
    assert probe._run_one("img", "src", "fp32", "threads4", "ctrl", None)["ok"]


RESULTS = REPO / "evals" / "results"
STDERR_CHECK = RESULTS / "allocator-trim-stderr-check-20260924.json"


@pytest.mark.parametrize("artifact", [
    "allocator-trim-probe-20260923.json",
    "allocator-trim-latency-20260923.json",
    "allocator-trim-pool-20260923.json",
])
def test_no_committed_run_logged_a_failed_worker(artifact: str) -> None:
    """The artifacts predate the fix; their runs' captured stderr, summarised
    in the stderr check, shows no worker failure and every burst logged."""
    check = json.loads(STDERR_CHECK.read_text(encoding="utf-8"))
    runs = json.loads((RESULTS / artifact).read_text(encoding="utf-8"))["runs"]
    entries = check["artifacts"][artifact]
    assert [(e["dtype"], e["workload"], e["arm"], e["replicate"]) for e in entries] \
        == [(r["dtype"], r["workload"], r["arm"], r["replicate"]) for r in runs]
    for entry, run in zip(entries, runs):
        assert not any(entry[k] for k in check["patterns"]), entry
        result = run["result"]
        bursts = result["pairs"] if run["workload"] == "pairs" else result["steps"]
        assert entry["bursts_logged"] == len(bursts), entry


def test_the_probe_starts_no_raw_threads() -> None:
    """Workers go through _run_threads (errors surface) or a pool whose
    map() re-raises; a bare Thread in the probe would swallow them again."""
    tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"))
    inner = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "_inner")
    calls = {ast.unparse(node.func) for node in ast.walk(inner)
             if isinstance(node, ast.Call)}
    assert "threading.Thread" not in calls
    assert "_run_threads" in calls
