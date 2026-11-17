"""Turn policy probes with inert backends; native lock tests run on the VM."""
import builtins
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from rust.harness_support import database_turn
from rust.harness_support import _database_turn as turn
from rust.harness_support._database_turn_platform import Identity


def test_no_marker_does_only_one_existence_check(tmp_path, monkeypatch):
    monkeypatch.setattr(database_turn, "lock_folder", lambda: tmp_path)
    checked = []
    exists = Path.exists
    importer = builtins.__import__

    def check(path):
        checked.append(path)
        return exists(path)

    def import_without_engine(name, globals=None, locals=None, fromlist=(), level=0):
        if "_database_turn" in name or "_database_turn" in fromlist:
            raise AssertionError("the unconfigured path imported the turn engine")
        return importer(name, globals, locals, fromlist, level)

    monkeypatch.setattr(Path, "exists", check)
    monkeypatch.setattr(builtins, "__import__", import_without_engine)
    assert database_turn.ensure_turn() is None
    assert checked == [tmp_path / database_turn.MARKER]


def test_cli_bank_takes_the_turn_before_reading_the_test_login(monkeypatch):
    from rust.cli_harness.rows import _bank

    called = []
    monkeypatch.setattr(_bank, "_DATABASE_TURN_MARKER", SimpleNamespace(exists=lambda: True))
    def held(**kwargs):
        called.append("turn")
        raise RuntimeError("held by the gate")
    monkeypatch.setattr(database_turn, "require_turn", held)
    monkeypatch.setitem(sys.modules, "tests.pg_defaults", SimpleNamespace(
        default_login=lambda: called.append("login") or ("pseudolife_test", "fixture-password")))
    with pytest.raises(RuntimeError, match="held by the gate"):
        _bank._login()
    assert called == ["turn"]
    monkeypatch.setattr(_bank, "_DATABASE_TURN_MARKER", SimpleNamespace(exists=lambda: False))
    assert _bank._login() == ("pseudolife_test", "fixture-password")
    assert called == ["turn", "login"]


@pytest.fixture
def backend(tmp_path, monkeypatch):
    current = os.getpid()
    holder = current + 10000000
    identities = {current: Identity(current, "200", "test-owner", 0),
                  holder: Identity(holder, "100", "test-owner", 0)}
    calls = []
    state = SimpleNamespace(busy=False, contains=0, holder=holder, current=current,
                            identities=identities, identity_calls=calls)

    def identity(pid):
        calls.append(pid)
        return identities.get(pid)

    def try_lock(path):
        return None if state.busy else path.open("a+b")

    def contain():
        state.contains += 1

    state.identity, state.try_lock, state.contain_process_tree = identity, try_lock, contain
    leases = {}
    monkeypatch.setattr(turn, "_held", leases)
    monkeypatch.setattr(turn, "_backend", lambda: state)
    yield state
    for lease in leases.values():
        lease.close()


def holder_record(backend):
    return {"holder": "another database run", "pid": backend.holder,
            "process_started": "100", "owner": "test-owner",
            "acquired": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()}


@pytest.mark.parametrize("old", [b"not JSON", b'{"pid":999,"process_started":"1"}', b""])
def test_free_os_handle_ignores_all_old_metadata(tmp_path, backend, old):
    path = tmp_path / "turn.lock"
    path.write_bytes(old)
    lease = turn.acquire(path, holder="new run")
    try:
        record = json.loads(path.read_text())
        assert record["holder"] == "new run" and record["pid"] == os.getpid()
        assert backend.identity_calls == [os.getpid()]
    finally:
        lease.close()
    assert path.read_bytes() == b""


@pytest.mark.parametrize("data", [b"", b"not JSON", b'[]', b'{"pid":true}'])
def test_held_handle_with_unreadable_identity_refuses_without_guessing(tmp_path, backend, data):
    backend.busy = True
    path = tmp_path / "turn.lock"
    path.write_bytes(data)
    with pytest.raises(turn.TurnUnavailable, match="identity could not be verified") as error:
        turn.acquire(path, holder="arriving run")
    assert "list running harnesses" in str(error.value)
    assert path.read_bytes() == data


def test_live_unrelated_holder_is_named_and_environment_flag_cannot_bypass_it(tmp_path, backend, monkeypatch):
    backend.busy = True
    path = tmp_path / "turn.lock"
    path.write_text(json.dumps(holder_record(backend)))
    monkeypatch.setenv("PSEUDOLIFE_DATABASE_TURN_HELD", "1")
    with pytest.raises(turn.TurnUnavailable, match="another database run") as error:
        turn.acquire(path, holder="arriving run")
    assert f"PID {backend.holder}" in str(error.value) and "held for" in str(error.value)
    message = str(error.value)
    assert "bounded wait" in message and "-WaitSeconds <seconds> -- <command>" in message
    assert "ops/dev-database-lock.ps1" in message and "repository root" in message
    assert "--database-turn-wait-seconds" not in message


@pytest.mark.parametrize("edit", ["none", "reused", "wrong-owner", "dead", "new-parent"])
def test_only_verified_live_ancestor_grants_reuse(tmp_path, backend, edit):
    backend.busy = True
    path = tmp_path / "turn.lock"
    record = holder_record(backend)
    path.write_text(json.dumps(record))
    backend.identities[backend.current] = Identity(backend.current, "200", "test-owner", backend.holder)
    if edit == "reused":
        record["process_started"] = "99"
        path.write_text(json.dumps(record))
    elif edit == "wrong-owner":
        backend.identities[backend.current] = Identity(backend.current, "200", "different-owner", backend.holder)
    elif edit == "dead":
        del backend.identities[backend.holder]
    elif edit == "new-parent":
        backend.identities[backend.holder] = Identity(backend.holder, "300", "test-owner", 0)
        record["process_started"] = "300"
        path.write_text(json.dumps(record))
    if edit != "none":
        with pytest.raises(turn.TurnUnavailable):
            turn.acquire(path, holder="child")
    else:
        lease = turn.acquire(path, holder="child", contain=True)
        assert lease.delegated and lease.stream is None and backend.contains == 1
        assert lease.contained
        lease.close()
        assert json.loads(path.read_text()) == record


def test_same_creation_tick_does_not_reject_a_legitimate_parent(tmp_path, backend):
    backend.busy = True
    path = tmp_path / "turn.lock"
    path.write_text(json.dumps(holder_record(backend)))
    backend.identities[backend.current] = Identity(backend.current, "100", "test-owner", backend.holder)
    lease = turn.acquire(path, holder="child")
    assert lease.delegated and not lease.contained and backend.contains == 0


def test_repeated_owned_call_reuses_handle_and_contains_only_on_explicit_request(tmp_path, backend):
    path = tmp_path / "turn.lock"
    lease = turn.acquire(path, holder="first")
    assert backend.contains == 0
    assert turn.acquire(path, holder="again", contain=True) is lease
    assert turn.acquire(path, holder="again", contain=True) is lease
    assert backend.contains == 1


def test_forked_copy_of_lease_cannot_erase_parent_record(tmp_path):
    path = tmp_path / "turn.lock"
    path.write_bytes(b"parent record")
    lease = turn.Lease(path.open("r+b"), pid=os.getpid() + 10000000)
    lease.close()
    assert path.read_bytes() == b"parent record"


def test_bounded_wait_retries_os_handle_and_reports_holder(tmp_path, backend, monkeypatch, capsys):
    backend.busy = True
    path = tmp_path / "turn.lock"
    path.write_text(json.dumps(holder_record(backend)))
    monkeypatch.setattr(turn.time, "sleep", lambda seconds: setattr(backend, "busy", False))
    lease = turn.acquire(path, holder="arriving run", wait_seconds=1)
    assert not lease.delegated
    assert "another database run" in capsys.readouterr().err
