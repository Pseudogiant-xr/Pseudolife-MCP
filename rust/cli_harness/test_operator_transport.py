"""Exercise the oracle side of native transport rows with real child processes."""
import sys
from pathlib import Path

import pytest

from . import core
from .rows import invite, lease_transport, pair


@pytest.mark.parametrize("case", invite._transport_cases(), ids=lambda case: case.id)
def test_invite_transport_oracle_arm(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    core.run_arm(case, target, tmp_path / "arm")


@pytest.mark.parametrize("case", [case for case in lease_transport.cases()
                                 if case.id.startswith("operator-container")], ids=lambda case: case.id)
def test_lease_transport_oracle_arm(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    core.run_arm(case, target, tmp_path / "arm")


@pytest.mark.parametrize("case", [case for case in pair.cases()
                                 if case.id in ("https-verified", "https-untrusted")], ids=lambda case: case.id)
def test_pair_https_oracle_arm(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    obs = core.run_arm(case, target, tmp_path / "arm")
    assert obs["exit"] == (0 if case.id == "https-verified" else 4)
    assert bool(obs.get("requests")) == (case.id == "https-verified")


@pytest.mark.parametrize("refused", [False, True])
def test_recovery_drop_checks_database_before_terminating(monkeypatch, refused):
    import contextlib
    from types import SimpleNamespace
    import psycopg
    from pseudolife_memory.storage import schema
    from cli_harness.rows import coordination_recovery as row

    calls = []
    target = object()

    def execute(sql, *args):
        calls.append("catalog" if sql.startswith("SELECT 1") else
                     "reap" if "pg_terminate_backend" in sql else "drop")
        return SimpleNamespace(fetchone=lambda: (1,))

    def guard(conn):
        assert conn is target
        calls.append("guard")
        if refused:
            raise RuntimeError("fixture database refused")

    monkeypatch.setattr(row, "NAMES", SimpleNamespace(require_owned=lambda name: None))
    monkeypatch.setattr(row, "_CREATED", {"r1_fixture"})
    monkeypatch.setattr(row, "_url", lambda name: "fixture-only")
    monkeypatch.setattr(row, "_admin", lambda: contextlib.nullcontext(SimpleNamespace(execute=execute)))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: contextlib.nullcontext(target))
    monkeypatch.setattr(schema, "assert_disposable_database", guard)
    if refused:
        with pytest.raises(RuntimeError, match="fixture database refused"):
            row._drop("r1_fixture")
        assert calls == ["catalog", "guard"]
        assert row._CREATED == {"r1_fixture"}
    else:
        row._drop("r1_fixture")
        assert calls == ["catalog", "guard", "reap", "drop"]
        assert row._CREATED == set()
