"""Lite-tier embedded Postgres provider (storage/embedded_pg.py).

The daemon resolves its storage exactly once at startup:

    explicit DSN  >  PSEUDOLIFE_MCP_STORAGE=files opt-out  >  embedded
    (pg0-embedded installed)  >  file mode

MemoryService itself is deliberately untouched — file-mode tests and the
``embedded`` CLI escape hatch must never grow a Postgres dependency just
because pg0-embedded happens to be importable.

Unit tests stub the ``pg0`` module; the roundtrip test at the bottom runs
only when pg0-embedded is actually installed and starts a real PG 18.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from pseudolife_memory.storage import embedded_pg as ep


@pytest.fixture(autouse=True)
def _reset_ownership():
    ep._owned.clear()
    yield
    ep._owned.clear()


def _stub_pg0(monkeypatch, *, already_running: bool, uri: str):
    """Install a fake ``pg0`` module; returns the recorded call log."""
    calls: list[tuple] = []

    class StubAlreadyRunning(Exception):
        pass

    class StubInfo:
        def __init__(self):
            self.uri = uri

    class StubPg0:
        def __init__(self, name=None, database=None, data_dir=None, **kw):
            calls.append(("init", name, database, data_dir))

        def start(self):
            calls.append(("start",))
            if already_running:
                raise StubAlreadyRunning("already running")
            return StubInfo()

        def info(self):
            calls.append(("info",))
            return StubInfo()

        def stop(self):
            calls.append(("stop",))

    stub = types.SimpleNamespace(
        Pg0=StubPg0, Pg0AlreadyRunningError=StubAlreadyRunning,
    )
    monkeypatch.setitem(sys.modules, "pg0", stub)
    return calls


# ----------------------------------------------------------------------
# resolve_daemon_storage — the one decision point
# ----------------------------------------------------------------------

def test_explicit_dsn_wins(tmp_path):
    env = {"PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://explicit"}
    assert ep.resolve_daemon_storage(env) == "postgres-explicit"
    # No side effects: the data-dir default is a lite-tier concern only.
    assert "PSEUDOLIFE_MCP_DATA_DIR" not in env


def test_files_optout_never_starts_embedded(monkeypatch):
    monkeypatch.setattr(ep, "available", lambda: True)
    monkeypatch.setattr(ep, "start_embedded", _boom)
    env = {ep.ENV_STORAGE: "files"}
    assert ep.resolve_daemon_storage(env) == "files"
    assert "PSEUDOLIFE_MCP_DATABASE_URL" not in env


def test_unknown_storage_value_is_refused():
    env = {ep.ENV_STORAGE: "bogus"}
    with pytest.raises(RuntimeError, match="PSEUDOLIFE_MCP_STORAGE"):
        ep.resolve_daemon_storage(env)


def test_lite_not_installed_falls_back_to_files(monkeypatch):
    monkeypatch.setattr(ep, "available", lambda: False)
    env: dict[str, str] = {}
    assert ep.resolve_daemon_storage(env) == "files"
    assert "PSEUDOLIFE_MCP_DATABASE_URL" not in env
    # File mode keeps today's cwd-relative default — no new env either.
    assert "PSEUDOLIFE_MCP_DATA_DIR" not in env


def test_embedded_engages_and_exports_dsn(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "available", lambda: True)
    monkeypatch.setattr(
        ep, "start_embedded", lambda d: "postgresql://stub/pseudolife_memory",
    )
    env = {"PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path)}
    assert ep.resolve_daemon_storage(env) == "postgres-embedded"
    assert env["PSEUDOLIFE_MCP_DATABASE_URL"] == "postgresql://stub/pseudolife_memory"
    assert env["PSEUDOLIFE_MCP_DATA_DIR"] == str(tmp_path)  # respected


def test_embedded_defaults_data_dir_to_stable_per_user_location(
    monkeypatch, tmp_path,
):
    """cwd-relative ./data is a per-launch-directory bank — unacceptable once
    real Postgres data lives under it. The lite tier defaults to a stable
    per-user dir instead (only when it actually engages)."""
    monkeypatch.setattr(ep, "available", lambda: True)
    seen: list[Path] = []

    def fake_start(data_dir):
        seen.append(data_dir)
        return "postgresql://stub"

    monkeypatch.setattr(ep, "start_embedded", fake_start)
    monkeypatch.setattr(ep, "default_lite_data_dir", lambda: tmp_path / "stable")
    env: dict[str, str] = {}
    assert ep.resolve_daemon_storage(env) == "postgres-embedded"
    assert seen == [tmp_path / "stable"]
    assert env["PSEUDOLIFE_MCP_DATA_DIR"] == str(tmp_path / "stable")


def _boom(*a, **kw):  # pragma: no cover - guard helper
    raise AssertionError("must not be called")


# ----------------------------------------------------------------------
# start_embedded — preflight, PG-major guard, attach-vs-own
# ----------------------------------------------------------------------

def test_nonascii_path_refused_on_windows(monkeypatch, tmp_path):
    """pg0's Rust runtime dies on non-ASCII paths on Windows ('stream did
    not contain valid UTF-8', verified 2026-08-14) — refuse up front with
    a remedy instead of letting it fail cryptically."""
    monkeypatch.setattr(ep, "_PLATFORM", "win32")
    bad = tmp_path / "pgdätä"
    with pytest.raises(RuntimeError, match="ASCII") as exc:
        ep._preflight_path(bad)
    assert "PSEUDOLIFE_MCP_DATA_DIR" in str(exc.value)


def test_space_in_path_is_fine_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "_PLATFORM", "win32")
    ep._preflight_path(tmp_path / "pg data dir")  # must not raise


def test_nonascii_path_allowed_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "_PLATFORM", "linux")
    ep._preflight_path(tmp_path / "pgdätä")  # must not raise


def test_pg_major_mismatch_is_refused(tmp_path):
    """A pgdata initialized under a different PG major must never be
    touched — a clear refusal beats postgres's cryptic startup failure."""
    pgdata = tmp_path / "embedded_pg"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n")
    with pytest.raises(RuntimeError, match="16"):
        ep._guard_pg_major(pgdata)
    with pytest.raises(RuntimeError, match=str(ep.EXPECTED_PG_MAJOR)):
        ep._guard_pg_major(pgdata)


def test_start_embedded_attaches_without_taking_ownership(
    monkeypatch, tmp_path,
):
    """Second process finding the instance running must attach via info()
    and NOT register a stop — it doesn't own the server's lifecycle."""
    uri = "postgresql://postgres:pw@127.0.0.1:59999/pseudolife_memory"
    calls = _stub_pg0(monkeypatch, already_running=True, uri=uri)
    dsn = ep.start_embedded(tmp_path)
    assert dsn == uri
    assert ("info",) in calls
    assert ep._owned == []


def test_start_embedded_owns_and_stops_what_it_started(
    monkeypatch, tmp_path,
):
    uri = "postgresql://postgres:pw@127.0.0.1:59999/pseudolife_memory"
    calls = _stub_pg0(monkeypatch, already_running=False, uri=uri)
    dsn = ep.start_embedded(tmp_path)
    assert dsn == uri
    assert len(ep._owned) == 1
    ep.stop_embedded()
    assert ("stop",) in calls
    assert ep._owned == []


def test_attach_or_start_returns_handle_only_when_started(
    monkeypatch, tmp_path,
):
    uri = "postgresql://postgres:pw@127.0.0.1:59999/pseudolife_memory"
    _stub_pg0(monkeypatch, already_running=False, uri=uri)
    dsn, inst = ep.attach_or_start(tmp_path / "a")
    assert dsn == uri and inst is not None
    _stub_pg0(monkeypatch, already_running=True, uri=uri)
    dsn, inst = ep.attach_or_start(tmp_path / "b")
    assert dsn == uri and inst is None


def test_start_lock_is_mutually_exclusive(tmp_path):
    """Two concurrent starters must serialize on the pgdata lock — the
    guard against racing initdb on a fresh bank."""
    import threading
    import time

    pgdata = tmp_path / "embedded_pg"
    pgdata.mkdir()
    inside: list[int] = []
    overlap: list[bool] = []

    def worker():
        with ep._start_lock(pgdata):
            inside.append(1)
            overlap.append(len(inside) > 1)
            time.sleep(0.3)
            inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(overlap), "two processes were inside the start lock at once"


def test_instance_name_is_stable_and_path_scoped(tmp_path):
    a = ep._instance_name(tmp_path / "bank_a")
    b = ep._instance_name(tmp_path / "bank_b")
    assert a != b
    assert a == ep._instance_name(tmp_path / "bank_a")
    assert a.startswith("pseudolife-")


# ----------------------------------------------------------------------
# default_lite_data_dir — stable per-user location per platform
# ----------------------------------------------------------------------

def test_default_data_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "_PLATFORM", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ep.default_lite_data_dir() == tmp_path / "pseudolife-mcp"


def test_default_data_dir_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "_PLATFORM", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert ep.default_lite_data_dir() == tmp_path / "pseudolife-mcp"


# ----------------------------------------------------------------------
# Integration — real pg0, real PG 18, our real schema
# ----------------------------------------------------------------------

@pytest.mark.skipif(not ep.available(), reason="pg0-embedded not installed")
def test_embedded_roundtrip_real_schema(tmp_path):
    """Full lite-tier roundtrip: start embedded PG under a path with a
    space, run the real ensure_schema through PostgresStorage (CREATE
    EXTENSION vector included), stop cleanly, data dir retained."""
    import pg0 as _pg0

    data_dir = tmp_path / "lite bank"
    dsn = ep.start_embedded(data_dir)
    try:
        from pseudolife_memory.storage.postgres import PostgresStorage
        from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

        storage = PostgresStorage(dsn)
        assert int(storage.get_meta("schema_version")) >= SCHEMA_META_VERSION
        ext = storage.conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'",
        ).fetchone()
        assert ext is not None
        storage.close()
    finally:
        name = ep._instance_name(data_dir / "embedded_pg")
        ep.stop_embedded()
        # stop must retain the data dir — only the test's own drop below
        # removes it.
        retained = (data_dir / "embedded_pg" / "PG_VERSION").exists()
        # Test hygiene only: drop the throwaway registry entry. Shipped
        # code never calls drop() — that invariant is greppable.
        _pg0.drop(name)
    assert retained
