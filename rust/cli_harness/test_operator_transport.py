"""Exercise the oracle side of native transport rows with real child processes."""
import sys
import base64
import json
from pathlib import Path

import pytest

from . import core
from .rows import invite, lease_transport, pair


@pytest.mark.parametrize("case", [case for case in pair.cases()
                                 if case.id in ("remote-plain-http", "remote-plain-http-json")],
                         ids=lambda case: case.id)
def test_plain_http_warning_reaches_successful_pairing(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    obs = core.run_arm(case, target, tmp_path / "arm")
    assert obs["exit"] == 0
    output = base64.b64decode(obs["stdout"]) + base64.b64decode(obs["stderr"])
    assert b"plain HTTP" in output
    assert any(request["target"] == "/api/pair" for request in obs["requests"])


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


@pytest.fixture
def recovery_row():
    import atexit
    from importlib.util import module_from_spec
    from unittest.mock import patch

    from rust.harness_support.import_blocker import cold_harness_imports

    from .rows import coordination_recovery

    # The dispatch table may already have loaded the row. Execute its top
    # level again and keep the blocker active while exercising its callbacks.
    spec = coordination_recovery.__spec__
    row = module_from_spec(spec)
    with cold_harness_imports(), patch.dict(coordination_recovery.normalize.RULES):
        try:
            spec.loader.exec_module(row)
            yield row
        finally:
            if cleanup := getattr(row, "_cleanup", None):
                atexit.unregister(cleanup)


def _fixture_descriptors(rows):
    from contextlib import nullcontext
    from types import SimpleNamespace

    return {f'SELECT * FROM "public"."{table}" LIMIT 0': nullcontext(SimpleNamespace(
        description=[SimpleNamespace(name=key) for key in records[0]],
    )) for table, records in rows.items()}


def _sql_text(statement):
    return statement if isinstance(statement, str) else statement.as_string()


@pytest.mark.parametrize("server_name", ["fixture", "pseudolife_memory", "pseudolife_bench", "r1_other"])
def test_recovery_drop_checks_database_before_terminating(monkeypatch, recovery_row, server_name):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import psycopg
    from rust.harness_support.database_guard import ProductionDatabaseError

    row = recovery_row
    name = row.NAMES.name("r1_fixture", max_length=63)
    server_name = name if server_name == "fixture" else server_name
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    calls = []

    def execute(sql, *args):
        calls.append("catalog" if sql.startswith("SELECT 1") else
                     "reap" if "pg_terminate_backend" in sql else "drop")
        return SimpleNamespace(fetchone=lambda: (1,))

    def target_execute(sql):
        assert sql == "SELECT current_database()"
        calls.append("guard")
        return SimpleNamespace(fetchone=lambda: (server_name,))

    monkeypatch.setattr(row, "_CREATED", {name})
    monkeypatch.setattr(row, "_url", lambda _: "fixture-only")
    monkeypatch.setattr(row, "_admin", lambda: nullcontext(SimpleNamespace(execute=execute)))
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: nullcontext(SimpleNamespace(execute=target_execute)))
    if server_name != name:
        with pytest.raises((ProductionDatabaseError, ValueError), match="production bank|different database"):
            row._drop(name)
        assert calls == ["catalog", "guard"]
        assert row._CREATED == {name}
    else:
        row._drop(name)
        assert calls == ["catalog", "guard", "reap", "drop"]
        assert row._CREATED == set()


@pytest.mark.parametrize("server_name", ["pseudolife_memory", "pseudolife_bench", "r1_other"])
def test_recovery_seed_checks_database_before_writing(monkeypatch, recovery_row, server_name):
    from unittest.mock import MagicMock

    from rust.harness_support.database_guard import ProductionDatabaseError

    row = recovery_row
    name = row.NAMES.name("r1_fixture", max_length=63)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (server_name,)
    with pytest.raises((ProductionDatabaseError, ValueError), match="production bank|different database"):
        row._seed_history(connection, name, "recover")
    connection.execute.assert_called_once_with("SELECT current_database()")
    connection.transaction.assert_not_called()


@pytest.mark.parametrize("kind", ["recover", "invited", "invited-denied", "repeat-recover"])
def test_recovery_bank_setup_runs_without_frozen_imports(monkeypatch, recovery_row, tmp_path, kind):
    from contextlib import nullcontext
    from unittest.mock import MagicMock, call

    import psycopg

    row = recovery_row
    name = row.NAMES.name("r1_coordination_recovery_" + kind.replace("-", "_"), max_length=63)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    admin, target = MagicMock(), MagicMock()
    admin.execute.return_value.fetchone.return_value = None
    reply = MagicMock()
    reply.fetchone.return_value = (name,)
    rows = json.loads(row._HISTORY.read_text(encoding="utf-8"))["variants"][kind]["rows"]
    descriptors = _fixture_descriptors(rows)
    target.execute.side_effect = lambda statement, *_: descriptors.get(_sql_text(statement), reply)
    monkeypatch.setattr(row, "_admin", lambda: nullcontext(admin))
    monkeypatch.setattr(row, "_url", lambda _: "fixture-only")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: nullcontext(target))
    arm = core.Arm("rust", tmp_path, tmp_path)
    row._bank_setup(kind)(arm)
    assert arm.state["bank"] == name
    assert target.execute.call_args_list[0] == call("SELECT current_database()")
    assert target.transaction.call_count == 1
    assert target.execute.call_args_list[-1] == call(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )


def test_recovery_seed_checks_live_columns(recovery_row, monkeypatch):
    from unittest.mock import MagicMock

    row = recovery_row
    name = row.NAMES.name("r1_fixture", max_length=63)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    conn = MagicMock()
    reply = MagicMock()
    reply.fetchone.return_value = (name,)
    captured = json.loads(row._HISTORY.read_text(encoding="utf-8"))["variants"]["recover"]["rows"]
    columns = captured["coordination_agents"][0].keys() - {"credential_hash"}
    descriptor, = _fixture_descriptors({"coordination_agents": [dict.fromkeys(columns)]}).values()
    conn.execute.side_effect = [reply, None, None, descriptor, None]
    with pytest.raises(ValueError, match="public.coordination_agents .*unexpected credential_hash"):
        row._seed_history(conn, name, "recover")
    assert len(conn.execute.call_args_list) == 4


@pytest.mark.parametrize("damage,reason", [
    ("principals", "incomplete recovery history"),
    ("coordination_lease_fence", "incomplete recovery history"),
    ("board", "wrong paired owner"),
    ("old-principal", "public.principals .*missing principal, unexpected old_key"),
    ("old-board", "public.principals .*missing board, unexpected old_key"),
])
def test_recovery_seed_refuses_an_incomplete_history(monkeypatch, recovery_row, tmp_path, damage, reason):
    import json
    from unittest.mock import MagicMock

    row = recovery_row
    name = row.NAMES.name("r1_fixture", max_length=63)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    history = json.loads(row._HISTORY.read_text(encoding="utf-8"))
    variant = history["variants"]["invited"]
    descriptors = _fixture_descriptors(variant["rows"])
    if damage == "principals":
        del variant["rows"]["principals"]
    elif damage == "board":
        variant["rows"]["principals"][0]["board"] = False
    elif damage.startswith("old-"):
        principal = variant["rows"]["principals"][0]
        principal["old_key"] = principal.pop(damage.removeprefix("old-"))
    else:
        del variant["sequences"][damage]
    broken = tmp_path / "history.json"
    broken.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(row, "_HISTORY", broken)
    connection = MagicMock()
    reply = MagicMock()
    reply.fetchone.return_value = (name,)
    connection.execute.side_effect = lambda statement, *_: descriptors.get(_sql_text(statement), reply)
    with pytest.raises(ValueError, match=reason):
        row._seed_history(connection, name, "invited")
    assert not any(_sql_text(call.args[0]).startswith("INSERT ") for call in connection.execute.call_args_list)


def test_recovery_reservation_replacement_keeps_both_files(recovery_row, tmp_path):
    import os
    from unittest.mock import MagicMock

    reserved = tmp_path / "agent.json"
    reserved.touch()
    original = reserved.stat()
    blocker = MagicMock()
    blocker.execute.return_value.fetchone.return_value = (True,)
    arm = core.Arm("rust", tmp_path, tmp_path, state={"blocker": blocker})
    recovery_row._reservation_race(arm, MagicMock())
    retained = tmp_path / "original-reservation.json"
    assert os.path.samestat(retained.stat(), original)
    assert not os.path.samefile(reserved, retained)
    assert reserved.read_bytes() == retained.read_bytes() == b""
    if os.name == "nt":
        from .rows.test_login import _permissions

        assert _permissions(reserved) == {
            "owner": "<me>", "protected": True, "aces": ["(A;;FA;;;OW)"],
        }
    else:
        assert reserved.stat().st_mode & 0o777 == 0o600
    assert arm.state["race_applied"]
    blocker.close.assert_called_once()
