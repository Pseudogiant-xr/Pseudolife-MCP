"""Fixture failures must fail the MCP runner without sharing or abandoning homes."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import mcp_run


@pytest.fixture
def runner(tmp_path, monkeypatch):
    homes, stopped, dropped, binaries = [], [], [], []
    fault = {"stop": False, "drop": False, "start": False}

    class Daemon:
        def __init__(self, side, home):
            self.side = side
            homes.append(home)

        def start(self, **kwargs):
            if fault["start"] and self.side == "rs":
                raise RuntimeError("startup failed after launching")
            return self

        def stop(self):
            stopped.append(self.side)
            if fault["stop"] and self.side == "rs":
                raise RuntimeError("shutdown failed")

    def rust(binary, home, *args):
        binaries.append(binary)
        return Daemon("rs", home)

    def drop(name):
        dropped.append(name)
        if fault["drop"]:
            raise RuntimeError("drop failed")

    def compare(command):
        Path(command[5]).write_text(json.dumps({"summary": {"responses": 10, "cases": 2,
            "diffs": 0, "unserved_checked": []}, "diffs": []}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mcp_run.daemons, "scratch_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_run.daemons, "python_daemon", lambda home, *args: Daemon("py", home))
    monkeypatch.setattr(mcp_run.daemons, "rust_daemon", rust)
    monkeypatch.setattr(mcp_run.pg, "name", lambda name: name)
    monkeypatch.setattr(mcp_run.pg, "create", lambda name: "postgresql://unused/fixture")
    monkeypatch.setattr(mcp_run.pg, "drop", drop)
    monkeypatch.setattr(mcp_run.subprocess, "run", compare)
    monkeypatch.setattr(sys, "argv", ["mcp_run.py", "relative-daemon", str(tmp_path / "out"), "--config2"])
    return SimpleNamespace(homes=homes, stopped=stopped, dropped=dropped, binaries=binaries, fault=fault)


def test_invocations_keep_distinct_homes(runner):
    assert mcp_run.main() == 0
    marker = runner.homes[0] / "owned"
    marker.write_text("first invocation", encoding="utf-8")
    assert mcp_run.main() == 0
    assert marker.exists()
    assert runner.homes[0] != runner.homes[2]


def test_relative_binary_is_resolved_before_daemon_cwd_changes(runner):
    assert mcp_run.main() == 0
    assert runner.binaries[0].is_absolute()


@pytest.mark.parametrize("failure", ["stop", "drop", "start"])
def test_infrastructure_failure_is_distinct_and_cleanup_continues(runner, failure):
    runner.fault[failure] = True
    try:
        code = mcp_run.main()
    except RuntimeError:
        code = 1
    assert code == 2
    assert set(runner.stopped) == {"py", "rs"}
    assert len(runner.dropped) == 2


def test_failed_comparison_cannot_reuse_an_old_report(runner, monkeypatch):
    output = Path(sys.argv[2])
    output.mkdir()
    (output / "compare.json").write_text(json.dumps({"summary": {"responses": 10, "cases": 2,
        "diffs": 1, "unserved_checked": []}, "diffs": ["config_lists[0]: old difference"]}), encoding="utf-8")
    monkeypatch.setattr(mcp_run.subprocess, "run", lambda command: SimpleNamespace(returncode=1))
    assert mcp_run.main() == 2
