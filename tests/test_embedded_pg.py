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


def _stub_pg0(monkeypatch, *, already_running: bool = False, uri: str = "",
              start_error: Exception | None = None, sql_error_on: str | None = None):
    """Install a fake ``pg0`` module; returns the recorded call log.

    A start on a fresh pgdata writes what pg0's initdb leaves behind
    (``PG_VERSION`` and a password-method ``pg_hba.conf``); every start
    records the password pg0 was given and the hba file it saw. SQL the
    module runs is recorded instead of executed (``sql_error_on`` makes a
    statement containing that text fail), and no server is ever probed
    unless a test installs its own ``_probe``."""
    calls: list[tuple] = []

    class StubAlreadyRunning(Exception):
        pass

    class StubInfo:
        def __init__(self):
            self.uri = uri or None
            self.port = 59999

    class StubPg0:
        def __init__(self, name=None, database=None, data_dir=None, **kw):
            calls.append(("init", name, database, data_dir, kw))
            self.data_dir = Path(data_dir)
            self.password = kw.get("password", "postgres")

        def start(self):
            hba = self.data_dir / "pg_hba.conf"
            calls.append(("start", self.password,
                          hba.read_text() if hba.exists() else None))
            if already_running:
                raise StubAlreadyRunning("already running")
            if start_error is not None:
                raise start_error
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

    def run_sql(dsn, statements):
        for statement in statements:
            if sql_error_on and sql_error_on in statement:
                raise RuntimeError("injected SQL failure")
        calls.append(("sql", dsn, list(statements)))
        return []

    monkeypatch.setattr(ep, "_run_sql", run_sql)
    monkeypatch.setattr(ep, "_probe", lambda port, pgdata, passwords: None)
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
    return ep._managed_methods((pgdata / "pg_hba.conf").read_text())


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


def _running(monkeypatch, pgdata: Path, accepts: str | None, port: int = 59998):
    """A server already running for ``pgdata`` (its postmaster.pid), which
    accepts the password ``accepts`` (or neither candidate when None)."""
    pgdata.mkdir(parents=True, exist_ok=True)
    (pgdata / "postmaster.pid").write_text(f"4242\n{pgdata}\n1\n{port}\n\n")

    def probe(port_, pgdata_, passwords):
        assert port_ == port and Path(pgdata_) == pgdata
        return next((ep._dsn(p, port_) for p in passwords if p == accepts), None)

    monkeypatch.setattr(ep, "_probe", probe)


def test_start_embedded_attaches_without_taking_ownership(
    monkeypatch, tmp_path,
):
    """A second process finding the server running attaches through its
    postmaster.pid and does NOT register a stop — it doesn't own the
    server's lifecycle."""
    calls = _stub_pg0(monkeypatch)
    pgdata = tmp_path / "embedded_pg"
    secret, _ = ep._cluster_secret(pgdata)
    (pgdata.mkdir(parents=True, exist_ok=True))
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256"))
    _running(monkeypatch, pgdata, accepts=secret)
    dsn = ep.start_embedded(tmp_path)
    assert dsn == ep._dsn(secret, 59998)
    assert not any(call[0] == "start" for call in calls)
    assert ep._owned == []
    # A SCRAM file a dead starter may not have reloaded yet is reloaded.
    assert _sql(calls) == ["SELECT pg_reload_conf()"]


def test_start_embedded_owns_and_stops_what_it_started(
    monkeypatch, tmp_path,
):
    calls = _stub_pg0(monkeypatch)
    dsn = ep.start_embedded(tmp_path)
    secret = (tmp_path / "embedded_pg.secret").read_text()
    assert dsn == ep._dsn(secret, 59999)
    assert len(ep._owned) == 1
    ep.stop_embedded()
    assert ("stop",) in calls
    assert ep._owned == []


def test_attach_or_start_returns_handle_only_when_started(
    monkeypatch, tmp_path,
):
    _stub_pg0(monkeypatch)
    dsn, inst = ep.attach_or_start(tmp_path / "a")
    assert inst is not None
    _stub_pg0(monkeypatch)
    pgdata = tmp_path / "b" / "embedded_pg"
    secret, _ = ep._cluster_secret(pgdata)
    pgdata.mkdir(parents=True, exist_ok=True)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256"))
    _running(monkeypatch, pgdata, accepts=secret)
    dsn, inst = ep.attach_or_start(tmp_path / "b")
    assert dsn == ep._dsn(secret, 59998) and inst is None


# ----------------------------------------------------------------------
# The per-cluster secret (replaces the fixed postgres/postgres password)
# ----------------------------------------------------------------------

_CREATE_DB = [
    "SELECT 1 FROM pg_database WHERE datname = 'pseudolife_memory'",
    "CREATE DATABASE \"pseudolife_memory\"",
]


def _move(secret: str) -> list[str]:
    return [
        "SET password_encryption = 'scram-sha-256'",
        f"ALTER ROLE \"postgres\" PASSWORD '{secret}'",
        "SELECT pg_reload_conf()",
    ]


def test_a_fresh_bank_gets_its_own_owner_only_secret(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    calls = _stub_pg0(monkeypatch)
    dsn, _ = ep.attach_or_start(tmp_path)
    pgdata = tmp_path / "embedded_pg"
    # Read back through the owner-only validator the token files use.
    secret, _ = credentials._open_token(tmp_path / "embedded_pg.secret")
    assert len(secret) == 64 and int(secret, 16) >= 0
    assert dsn == ep._dsn(secret, 59999)
    # pg0 never sees the secret: its own defaults, and it creates no database.
    init = next(call for call in calls if call[0] == "init")
    assert init[2] is None and "password" not in init[4]
    start = next(call for call in calls if call[0] == "start")
    assert start[1] == "postgres"
    # The role moves from pg0's initdb password to the secret, over the old
    # password; the reload and the database use the secret.
    sql_calls = [call for call in calls if call[0] == "sql"]
    assert sql_calls[0][1] == ep._dsn("postgres", 59999, "postgres")
    assert all(call[1] == ep._dsn(secret, 59999, "postgres") for call in sql_calls[1:])
    assert _sql(calls) == _move(secret) + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}


def test_a_legacy_bank_migrates_to_its_secret(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    calls = _stub_pg0(monkeypatch)
    pgdata = _legacy_bank(tmp_path)
    ep.attach_or_start(tmp_path)
    secret, _ = credentials._open_token(tmp_path / "embedded_pg.secret")
    start = next(call for call in calls if call[0] == "start")
    # The server starts with loopback trust (the progress marker).
    assert ep._managed_methods(start[2]) == {"trust"}
    assert _sql(calls) == _move(secret) + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}
    # Comments and layout survive; only method columns change.
    text = (pgdata / "pg_hba.conf").read_text()
    assert text.splitlines()[0] == _LEGACY_HBA.splitlines()[0]
    assert "127.0.0.1/32            scram-sha-256" in text


def test_a_migrated_bank_starts_with_its_secret_and_changes_nothing(
    monkeypatch, tmp_path,
):
    _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    secret = (tmp_path / "embedded_pg.secret").read_text()
    hba = (tmp_path / "embedded_pg" / "pg_hba.conf").read_text()
    calls = _stub_pg0(monkeypatch)
    dsn, _ = ep.attach_or_start(tmp_path)
    assert dsn == ep._dsn(secret, 59999)
    assert _sql(calls) == _CREATE_DB
    assert (tmp_path / "embedded_pg" / "pg_hba.conf").read_text() == hba
    assert (tmp_path / "embedded_pg.secret").read_text() == secret


def test_an_interrupted_migration_completes_on_the_next_start(
    monkeypatch, tmp_path,
):
    """A crash after the trust marker (before or after ALTER ROLE) with the
    server stopped leaves trust in pg_hba.conf; the next start finishes."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "ab" * 32)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "trust"))
    calls = _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    assert _sql(calls) == _move("ab" * 32) + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}


def test_an_interrupted_migration_on_a_running_server_is_finished_on_attach(
    monkeypatch, tmp_path,
):
    """A starter killed after the server came up leaves it running on trust
    with no owner. The next caller holds the start lock, so the marker is
    this module's own unfinished move: it finishes it, without starting."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "cd" * 32)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "trust"))
    calls = _stub_pg0(monkeypatch)
    _running(monkeypatch, pgdata, accepts="cd" * 32)  # trust takes anything
    dsn, started = ep.attach_or_start(tmp_path)
    assert started is None and dsn == ep._dsn("cd" * 32, 59998)
    assert not any(call[0] == "start" for call in calls)
    assert _sql(calls) == _move("cd" * 32)
    assert _methods(pgdata) == {"scram-sha-256"}


def test_attaching_to_an_older_release_running_server_leaves_it_alone(
    monkeypatch, tmp_path,
):
    """A server an older release started still has the fixed password and
    the password method: attach with it, change nothing; it moves at its
    next own start."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _legacy_bank(tmp_path)
    _running(monkeypatch, pgdata, accepts="postgres")
    dsn, started = ep.attach_or_start(tmp_path)
    assert (dsn, started) == (ep._dsn("postgres", 59998), None)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA
    assert _sql(calls) == []


def test_a_failure_after_an_owned_start_stops_the_server(monkeypatch, tmp_path):
    """Nothing this call started is left running unowned (and on trust);
    the next start finishes the move."""
    calls = _stub_pg0(monkeypatch, sql_error_on="ALTER ROLE")
    pgdata = _legacy_bank(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        ep.attach_or_start(tmp_path)
    assert ("stop",) in calls
    assert ep._owned == []
    # The marker stays, and the unused new secret is dropped.
    assert _methods(pgdata) == {"trust"}
    assert not (tmp_path / "embedded_pg.secret").exists()
    calls = _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    secret = (tmp_path / "embedded_pg.secret").read_text()
    assert _sql(calls) == _move(secret) + _CREATE_DB


def test_a_failed_start_puts_a_marked_file_back(monkeypatch, tmp_path):
    """pg0 refuses a server it has no record of with a generic error; the
    server may be running, so its file must not be left on trust."""
    _stub_pg0(monkeypatch, start_error=RuntimeError("lock file exists"))
    pgdata = _legacy_bank(tmp_path)
    with pytest.raises(RuntimeError, match="lock file exists"):
        ep.attach_or_start(tmp_path)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA


def test_a_running_server_that_takes_no_known_password_is_refused(
    monkeypatch, tmp_path,
):
    _stub_pg0(monkeypatch, already_running=True)
    pgdata = _legacy_bank(tmp_path)
    _running(monkeypatch, pgdata, accepts=None)
    with pytest.raises(RuntimeError, match="accepts neither"):
        ep.attach_or_start(tmp_path)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA


def test_a_lost_secret_rekeys_through_the_trust_marker(monkeypatch, tmp_path):
    """A migrated bank whose secret file is gone gets a new one, set on the
    role under trust, instead of being locked for good."""
    _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    old = (tmp_path / "embedded_pg.secret").read_text()
    (tmp_path / "embedded_pg.secret").unlink()
    calls = _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    new = (tmp_path / "embedded_pg.secret").read_text()
    assert new != old
    start = next(call for call in calls if call[0] == "start")
    assert ep._managed_methods(start[2]) == {"trust"}
    assert _sql(calls) == _move(new) + _CREATE_DB


def test_a_secret_file_with_an_unexpected_value_is_refused(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    _stub_pg0(monkeypatch)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "x'--")
    with pytest.raises(RuntimeError, match="unexpected value"):
        ep.attach_or_start(tmp_path)


def test_hba_rewrite_changes_only_managed_methods_on_rule_lines():
    text = (
        "# host all all 0.0.0.0/0 trust   (a comment, untouched)\n"
        "\n"
        "local   all   all                       password\n"
        "host    all   all   127.0.0.1/32        md5\n"
        "host    all   all   10.0.0.0 255.0.0.0  scram-sha-256\r\n"
        "host    all   all   0.0.0.0/0           reject\n"
        "include_dir conf.d\n"
        "hostssl all   all   ::1/128             cert clientcert=verify-full\n"
    )
    out = ep._rewrite_hba(text, "trust")
    lines = out.split("\n")
    assert lines[0] == text.split("\n")[0]
    assert lines[2] == "local   all   all                       trust"
    assert lines[3] == "host    all   all   127.0.0.1/32        trust"
    assert lines[4] == "host    all   all   10.0.0.0 255.0.0.0  trust\r"
    assert lines[5:8] == text.split("\n")[5:8]
    assert ep._managed_methods(out) == {"trust"}
    # Unmanaged rules do not make a SCRAM bank look unmigrated.
    scram = ep._rewrite_hba(text, "scram-sha-256")
    assert ep._managed_methods(scram) == {"scram-sha-256"}
    assert ep._managed_methods("# only comments\n") == set()


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
        assert ep._managed_methods((pgdata / "pg_hba.conf").read_text()) == {
            "scram-sha-256"}
        ep.stop_embedded()
        dsn_again = ep.start_embedded(data_dir)
        assert f":{secret}@" in dsn_again
        with psycopg.connect(dsn_again) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        ep.stop_embedded()
        _pg0.drop(name)
