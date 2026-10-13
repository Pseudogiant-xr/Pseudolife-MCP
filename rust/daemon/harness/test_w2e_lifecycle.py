"""Numeric comparison and run ownership without starting real daemons."""
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


@pytest.mark.parametrize("python_embedding,rust_embedding", [
    ([1.0, 0.0], [1.0]), ([1.0], [1.0, 0.0]),
    ([], [1.0]), ([1.0], []), (None, [1.0]), ([1.0], None),
])
@pytest.mark.parametrize("calibrate", [False, True])
def test_embedding_shape_mismatch_is_a_difference(harness, python_embedding, rust_embedding,
                                                 calibrate):
    tolerance = {} if calibrate else json.loads(
        harness.TOLERANCE_FILE.read_text(encoding="utf-8"))
    stats = {}
    differences = harness.compare_numeric(
        {1: {"embedding": python_embedding, "surprise": None}},
        {1: {"embedding": rust_embedding, "surprise": None}}, tolerance, stats)
    assert len(differences) == 1 and "entries[1].embedding" in differences[0]
    assert not stats  # No tolerance statistics from incompatible shapes.


def test_tokenized_embeddings_still_report_dimension_mismatch(harness):
    def state(embedding):
        return {"rows": {"public.entries": {
            "columns": ["id", "embedding", "surprise"],
            "rows": [[1, embedding, None]],
        }}}

    python, rust = state("[1.0,0.0]"), state("[1.0]")
    a, b = harness.extract_numeric(python), harness.extract_numeric(rust)
    assert python == rust  # The separate numeric comparison owns this check.
    assert "length" in harness.compare_numeric(a, b, {}, {})[0]


@pytest.mark.parametrize("driver", ["w2e", "w2e_shutdown"])
def test_overlapping_invocations_preserve_each_others_homes(harness, monkeypatch, tmp_path, driver):
    module = importlib.import_module(driver)
    arguments = [driver, *(["live", "--only", "store-defaults"] if driver == "w2e" else []),
                 "--rust-bin", "candidate", "--out", str(tmp_path / "result.json")]
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(harness.daemons, "scratch_root", lambda: tmp_path)
    roots = []

    def overlap(*args, **kwargs):
        root = args[2]
        roots.append(root)
        marker = harness.daemons.make_home(root, "same-scenario", None) / f"run-{len(roots)}"
        marker.write_text("owned evidence", encoding="utf-8")
        if len(roots) == 1:
            with pytest.raises(FixtureFailure, match="after home creation"):
                module.main()
            assert marker.read_text(encoding="utf-8") == "owned evidence"
        raise FixtureFailure("after home creation")

    monkeypatch.setattr(harness, "run_scenario" if driver == "w2e" else "prepare_template", overlap)
    with pytest.raises(FixtureFailure, match="after home creation"):
        module.main()
    assert len(set(roots)) == 2
    assert all(root.parent == tmp_path for root in roots)
    assert (roots[1] / "same-scenario" / "run-2").read_text(encoding="utf-8") == "owned evidence"


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
