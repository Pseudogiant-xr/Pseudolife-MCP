"""CLI path resolution and run-owned cleanup before any real daemon starts."""
import argparse
import importlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import pgdisposable as pg
    from rust.cli_harness.run_names import RunNames
    # The executable harness changes its name admission on import; restore
    # the shared module afterwards for the other harness test files.
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", pg.DISPOSABLE_NAME)
    h = importlib.import_module("w2e")
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", re.compile(r"pl_cf_w2e_[a-z0-9_]{1,40}"))
    monkeypatch.setattr(pg, "NAMES", RunNames())
    return h


class FixtureFailure(RuntimeError):
    pass


@pytest.mark.parametrize("driver", ["w2e", "w2e_shutdown"])
def test_relative_binary_is_resolved_before_provisioning(harness, monkeypatch, tmp_path, driver):
    module = importlib.import_module(driver)
    monkeypatch.chdir(tmp_path)
    arguments = [driver, *(["live"] if driver == "w2e" else []),
                 "--rust-bin", "candidate", "--out", "result.json"]
    monkeypatch.setattr(sys, "argv", arguments)
    seen = []
    parse = argparse.ArgumentParser.parse_args

    def capture(parser, *args, **kwargs):
        value = parse(parser, *args, **kwargs)
        seen.append(value)
        return value

    def stop(*args, **kwargs):
        raise FixtureFailure("before provisioning")

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    monkeypatch.setattr(harness.daemons, "scratch_root", lambda: tmp_path)
    monkeypatch.setattr(harness, "run_scenario", stop)
    monkeypatch.setattr(harness, "prepare_template", stop)
    with pytest.raises(FixtureFailure, match="before provisioning"):
        module.main()
    assert seen[0].rust_bin == tmp_path / "candidate"


@pytest.mark.parametrize("mode,stage", [
    ("live", "template-create"), ("live", "seed-start"),
    ("live", "clone-create"), ("live", "arm-start"),
    ("golden", "snapshot"), ("golden", "arm-start"),
])
def test_failure_cleans_only_this_scenarios_databases(harness, monkeypatch, tmp_path, mode, stage):
    h = harness
    prior = h.pg.name("pl_cf_w2e_prior")
    present, dropped, daemons = {prior}, [], []
    creates, starts = 0, 0
    monkeypatch.setitem(h.SCENARIOS, "cleanup", {"config": None, "seed": [], "ops": []})
    monkeypatch.setattr(h, "GOLDENS", tmp_path)
    (tmp_path / "cleanup.json").write_text(json.dumps({"steps": []}), encoding="utf-8")

    def create(name, **kwargs):
        nonlocal creates
        creates += 1
        present.add(name)
        if (stage == "template-create" and creates == 1
                or stage == "clone-create" and creates == 3):
            raise FixtureFailure(stage)
        return name

    def drop(name):
        h.pg.NAMES.require_owned(name)
        dropped.append(name)
        present.discard(name)

    def dump(dsn):
        if stage == "snapshot":
            raise FixtureFailure(stage)
        return {}

    class Daemon:
        port = 1
        started = stopped = False

        def __init__(self, *args, **kwargs):
            daemons.append(self)

        def start(self, timeout):
            nonlocal starts
            starts += 1
            self.started = True
            if (stage == "seed-start" or stage == "arm-start"
                    and starts == (2 if mode == "live" else 1)):
                raise FixtureFailure(stage)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(h.pg, "create", create)
    monkeypatch.setattr(h.pg, "drop", drop)
    monkeypatch.setattr(h.dbstate, "dump", dump)
    monkeypatch.setattr(h.daemons, "python_daemon", Daemon)
    monkeypatch.setattr(h.daemons, "rust_daemon", Daemon)
    monkeypatch.setattr(h.w1a, "wait_settled", lambda *args, **kwargs: None)
    monkeypatch.setattr(h, "Mcp", lambda port: SimpleNamespace(init=lambda: None))
    with pytest.raises(FixtureFailure, match=stage):
        h.run_scenario("cleanup", tmp_path / "candidate", tmp_path, mode, False, {})
    assert present == {prior}
    assert prior not in dropped
    assert all(d.stopped for d in daemons if d.started)
