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


_LEGACY_HBA = (
    "# TYPE  DATABASE        USER            ADDRESS                 METHOD\n"
    "local   all             all                                     password\n"
    "host    all             all             127.0.0.1/32            password\n"
    "host    all             all             ::1/128                 password\n"
    "host    replication     all             127.0.0.1/32            password\n"
)


def _stub_pg0(monkeypatch, *, already_running: bool, uri: str):
    """Install a fake ``pg0`` module; returns the recorded call log.

    A start on a fresh pgdata writes what pg0's initdb leaves behind
    (``PG_VERSION`` and a password-method ``pg_hba.conf``); every start
    records the password pg0 was given and the hba file it saw. SQL the
    module runs is recorded instead of executed."""
    calls: list[tuple] = []

    class StubAlreadyRunning(Exception):
        pass

    class StubInfo:
        def __init__(self):
            self.uri = uri

    class StubPg0:
        def __init__(self, name=None, database=None, data_dir=None, **kw):
            calls.append(("init", name, database, data_dir))
            self.data_dir = Path(data_dir)
            self.password = kw.get("password", "postgres")

        def start(self):
            hba = self.data_dir / "pg_hba.conf"
            calls.append(("start", self.password,
                          hba.read_text() if hba.exists() else None))
            if already_running:
                raise StubAlreadyRunning("already running")
            if not (self.data_dir / "PG_VERSION").exists():
                (self.data_dir / "PG_VERSION").write_text("18\n")
                hba.write_text(_LEGACY_HBA)
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
    monkeypatch.setattr(
        ep, "_run_sql",
        lambda dsn, statements: calls.append(("sql", dsn, list(statements))),
    )
    return calls


def _sql(calls):
    return [statement for call in calls if call[0] == "sql" for statement in call[2]]


def _legacy_bank(data_dir: Path) -> Path:
    """A bank an older release created: pg0's initdb, no secret file."""
    pgdata = data_dir / "embedded_pg"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("18\n")
    (pgdata / "pg_hba.conf").write_text(_LEGACY_HBA)
    return pgdata


def _methods(pgdata: Path) -> set[str]:
    return ep._hba_methods((pgdata / "pg_hba.conf").read_text())


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


# ----------------------------------------------------------------------
# The per-cluster secret (replaces the fixed postgres/postgres password)
# ----------------------------------------------------------------------

_URI = "postgresql://postgres:x@127.0.0.1:59999/pseudolife_memory"


def test_a_fresh_bank_gets_its_own_owner_only_secret(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    calls = _stub_pg0(monkeypatch, already_running=False, uri=_URI)
    ep.attach_or_start(tmp_path)
    pgdata = tmp_path / "embedded_pg"
    secret_path = tmp_path / "embedded_pg.secret"
    # Read back through the owner-only validator the token files use.
    secret, _ = credentials._open_token(secret_path)
    assert len(secret) == 64 and int(secret, 16) >= 0
    assert secret != "postgres"
    start = next(call for call in calls if call[0] == "start")
    assert start[1] == secret  # initdb and pg0's record get the secret
    # The role already has the secret; only the method moves to SCRAM.
    assert _sql(calls) == ["SELECT pg_reload_conf()"]
    assert _methods(pgdata) == {"scram-sha-256"}


def test_a_legacy_bank_migrates_to_its_secret(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    calls = _stub_pg0(monkeypatch, already_running=False, uri=_URI)
    pgdata = _legacy_bank(tmp_path)
    ep.attach_or_start(tmp_path)
    secret, _ = credentials._open_token(tmp_path / "embedded_pg.secret")
    start = next(call for call in calls if call[0] == "start")
    # The server starts with loopback trust (the progress marker), so pg0's
    # own check passes whatever the role's password is at that moment.
    assert ep._hba_methods(start[2]) == {"trust"}
    assert _sql(calls) == [
        "SET password_encryption = 'scram-sha-256'",
        f"ALTER ROLE \"postgres\" PASSWORD '{secret}'",
        "SELECT pg_reload_conf()",
    ]
    assert _methods(pgdata) == {"scram-sha-256"}
    # Comments and layout survive; only method columns change.
    text = (pgdata / "pg_hba.conf").read_text()
    assert text.splitlines()[0] == _LEGACY_HBA.splitlines()[0]
    assert "127.0.0.1/32            scram-sha-256" in text


def test_a_migrated_bank_starts_with_its_secret_and_changes_nothing(
    monkeypatch, tmp_path,
):
    calls = _stub_pg0(monkeypatch, already_running=False, uri=_URI)
    ep.attach_or_start(tmp_path)
    secret = (tmp_path / "embedded_pg.secret").read_text()
    hba = (tmp_path / "embedded_pg" / "pg_hba.conf").read_text()
    calls = _stub_pg0(monkeypatch, already_running=False, uri=_URI)
    ep.attach_or_start(tmp_path)
    start = next(call for call in calls if call[0] == "start")
    assert start[1] == secret
    assert _sql(calls) == []
    assert (tmp_path / "embedded_pg" / "pg_hba.conf").read_text() == hba
    assert (tmp_path / "embedded_pg.secret").read_text() == secret


def test_an_interrupted_migration_completes_on_the_next_start(
    monkeypatch, tmp_path,
):
    """A crash after the trust marker (before or after ALTER ROLE) leaves
    trust in pg_hba.conf; the next start redoes the idempotent steps."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "ab" * 32)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "trust"))
    calls = _stub_pg0(monkeypatch, already_running=False, uri=_URI)
    ep.attach_or_start(tmp_path)
    assert f"ALTER ROLE \"postgres\" PASSWORD '{'ab' * 32}'" in _sql(calls)
    assert _methods(pgdata) == {"scram-sha-256"}


def test_attaching_to_a_running_legacy_server_leaves_its_hba_as_found(
    monkeypatch, tmp_path,
):
    """Only a server this process started is migrated; a running server's
    pg_hba.conf goes back to what it was, so a reload cannot open it."""
    calls = _stub_pg0(monkeypatch, already_running=True, uri=_URI)
    pgdata = _legacy_bank(tmp_path)
    dsn, started = ep.attach_or_start(tmp_path)
    assert (dsn, started) == (_URI, None)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA
    assert _sql(calls) == []


def test_hba_rewrite_changes_only_password_style_methods():
    text = (
        "# host all all 0.0.0.0/0 trust   (a comment, untouched)\n"
        "\n"
        "local   all   all                       password\n"
        "host    all   all   127.0.0.1/32        md5\n"
        "host    all   all   10.0.0.0 255.0.0.0  scram-sha-256\n"
        "host    all   all   0.0.0.0/0           reject\n"
    )
    out = ep._rewrite_hba(text, "trust")
    assert out.splitlines()[0] == text.splitlines()[0]
    assert out.splitlines()[2] == "local   all   all                       trust"
    assert out.splitlines()[3] == "host    all   all   127.0.0.1/32        trust"
    assert out.splitlines()[4] == "host    all   all   10.0.0.0 255.0.0.0  trust"
    assert out.splitlines()[5].endswith("reject")
    assert ep._hba_methods(out) == {"trust", "reject"}


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


@pytest.mark.skipif(not ep.available(), reason="pg0-embedded not installed")
def test_embedded_legacy_bank_migrates_real(tmp_path):
    """A bank an older release made with postgres/postgres moves to its own
    secret: the old password stops working, the returned DSN works, and a
    restart still opens it."""
    import psycopg
    import pg0 as _pg0

    data_dir = tmp_path / "legacy bank"
    pgdata = data_dir / "embedded_pg"
    pgdata.mkdir(parents=True)
    name = ep._instance_name(pgdata)
    legacy = _pg0.Pg0(name=name, database="pseudolife_memory", data_dir=str(pgdata))
    old_uri = legacy.start().uri
    legacy.stop()
    try:
        dsn = ep.start_embedded(data_dir)
        secret = (data_dir / "embedded_pg.secret").read_text()
        assert f":{secret}@" in dsn
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(old_uri, connect_timeout=10).close()
        assert ep._hba_methods((pgdata / "pg_hba.conf").read_text()) == {
            "scram-sha-256"}
        ep.stop_embedded()
        dsn_again = ep.start_embedded(data_dir)
        assert f":{secret}@" in dsn_again
        with psycopg.connect(dsn_again) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        ep.stop_embedded()
        _pg0.drop(name)
