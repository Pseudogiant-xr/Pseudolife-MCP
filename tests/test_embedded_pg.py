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

import re
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


_ROLES: dict[Path, str] = {}  # pgdata -> the role's password or stored verifier
_SERVERS: dict[int, Path] = {}  # port -> the pgdata of the server on it
# port -> the pg_hba.conf text a POSIX server loaded (absent: the file is in
# force at once, as on Windows). A reload loads the file.
_LOADED: dict[int, str] = {}


@pytest.fixture(autouse=True)
def _reset_fake_servers():
    for state in (_ROLES, _SERVERS, _LOADED):
        state.clear()
    yield
    for state in (_ROLES, _SERVERS, _LOADED):
        state.clear()


def _takes(stored: str, password: str) -> bool:
    if stored.startswith("SCRAM-SHA-256$"):
        import base64
        salt = base64.b64decode(stored.split("$")[1].split(":")[1])
        return _VERIFIER(password, salt=salt) == stored
    return stored == password


def _connect_ok(dsn: str) -> bool:
    """A fake server's answer, with the bank's pg_hba.conf in force at once
    (as on Windows): trust takes any password but fails a SCRAM requirement
    (no authentication happens); a password method fails it too (libpq
    refuses to send the password); SCRAM checks the role's password."""
    from urllib.parse import urlsplit

    parts = urlsplit(dsn)
    pgdata = _SERVERS.get(parts.port)
    if pgdata is None:
        return False
    hba = _LOADED.get(parts.port)
    if hba is None:
        hba = (pgdata / "pg_hba.conf").read_text() if (pgdata / "pg_hba.conf").exists() else ""
    scram_required = "require_auth=scram-sha-256" in dsn
    if ep._is_marked(hba):
        return not scram_required
    if scram_required and ep._managed_methods(hba) != {"scram-sha-256"}:
        return False
    return _takes(_ROLES.get(pgdata, "postgres"), parts.password)


def _stub_pg0(monkeypatch, *, already_running: bool = False, uri: str = "",
              start_error: Exception | None = None, sql_error_on: str | None = None,
              posix: bool = False):
    """Install a fake ``pg0`` module and fake servers; returns the call log.

    A start on a fresh pgdata writes what pg0's initdb leaves behind
    (``PG_VERSION``, a password-method ``pg_hba.conf``, the role on pg0's
    default password) and serves on port 59999; every start records the
    password pg0 was given and the hba file it saw. SQL runs against the
    fake servers' authentication (``_connect_ok``) and is recorded when it
    connects; a role change takes effect (``sql_error_on`` makes a statement
    containing that text fail first)."""
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
                _ROLES[self.data_dir] = self.password
            _SERVERS[59999] = self.data_dir
            if posix:
                _LOADED[59999] = hba.read_text()
            return StubInfo()

        def info(self):
            calls.append(("info",))
            return StubInfo()

        def stop(self):
            calls.append(("stop",))
            _SERVERS.pop(59999, None)

    stub = types.SimpleNamespace(
        Pg0=StubPg0, Pg0AlreadyRunningError=StubAlreadyRunning,
    )
    monkeypatch.setitem(sys.modules, "pg0", stub)

    def run_sql(dsn, statements):
        from urllib.parse import urlsplit
        for statement in statements:
            if sql_error_on and sql_error_on in statement:
                raise RuntimeError("injected SQL failure")
        if not _connect_ok(dsn):
            raise RuntimeError("password authentication failed")
        calls.append(("sql", dsn, list(statements)))
        port = urlsplit(dsn).port
        pgdata = _SERVERS[port]
        if "SELECT pg_reload_conf()" in statements and port in _LOADED:
            _LOADED[port] = (pgdata / "pg_hba.conf").read_text()
        for statement in statements:
            match = re.search(r"PASSWORD '([^']+)'", statement)
            if match:
                stored = match.group(1)
                _ROLES[pgdata] = stored.removeprefix("verifier-of-")
        return []

    def probe(port, pgdata, passwords):
        if _SERVERS.get(port) != Path(pgdata):
            return None
        return next((p for p in passwords if _connect_ok(
            ep._dsn(p, port, "postgres", scram=p != "postgres"))), None)

    monkeypatch.setattr(ep, "_run_sql", run_sql)
    monkeypatch.setattr(ep, "_probe", probe)
    monkeypatch.setattr(ep, "_postmaster_alive", lambda pgdata: Path(pgdata) in _SERVERS.values())
    monkeypatch.setattr(ep, "_scram_verifier", lambda secret: f"verifier-of-{secret}")
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


def _running(pgdata: Path, role: str, port: int = 59998, loaded: str | None = None):
    """A server already running for ``pgdata`` (its postmaster.pid), whose
    role takes ``role``; ``loaded``: the rules a POSIX server still runs."""
    pgdata.mkdir(parents=True, exist_ok=True)
    (pgdata / "postmaster.pid").write_text(f"4242\n{pgdata}\n1\n{port}\n\n")
    _SERVERS[port] = pgdata
    _ROLES[pgdata] = role
    if loaded is not None:
        _LOADED[port] = loaded


def _moved_bank(data_dir: Path, secret: str) -> Path:
    """A bank already on its secret and SCRAM."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(data_dir)
    credentials._write_token_file(data_dir / "embedded_pg.secret", secret)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256"))
    _ROLES[pgdata] = secret
    return pgdata


_CREATE_DB = [
    "SELECT 1 FROM pg_database WHERE datname = 'pseudolife_memory'",
    "CREATE DATABASE \"pseudolife_memory\"",
]


def _move(secret: str) -> list[str]:
    """The trust-marker move: the role first (as a verifier computed here,
    never the secret itself), then SCRAM, a reload, and the check that the
    server takes the secret over SCRAM."""
    return [
        f"ALTER ROLE \"postgres\" PASSWORD 'verifier-of-{secret}'",
        "SELECT pg_reload_conf()",
        "SELECT 1",
    ]


def _scram_first(secret: str) -> list[str]:
    """The move of a role still on the public password: SCRAM and a reload
    first (with the public password), then the role over SCRAM, then the
    check."""
    return [
        "SELECT pg_reload_conf()",
        f"ALTER ROLE \"postgres\" PASSWORD 'verifier-of-{secret}'",
        "SELECT 1",
    ]


def test_start_embedded_attaches_without_taking_ownership(monkeypatch, tmp_path):
    """A second process finding the server running attaches through its
    postmaster.pid and does NOT register a stop."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _moved_bank(tmp_path, "aa" * 32)
    _running(pgdata, "aa" * 32)
    dsn = ep.start_embedded(tmp_path)
    assert dsn == ep._dsn("aa" * 32, 59998, scram=True)
    assert not any(call[0] == "start" for call in calls)
    assert ep._owned == []
    assert _sql(calls) == _CREATE_DB


def test_start_embedded_owns_and_stops_what_it_started(monkeypatch, tmp_path):
    calls = _stub_pg0(monkeypatch)
    dsn = ep.start_embedded(tmp_path)
    secret = (tmp_path / "embedded_pg.secret").read_text()
    assert dsn == ep._dsn(secret, 59999, scram=True)
    assert len(ep._owned) == 1
    ep.stop_embedded()
    assert ("stop",) in calls
    assert ep._owned == []


def test_attach_or_start_returns_handle_only_when_started(monkeypatch, tmp_path):
    _stub_pg0(monkeypatch)
    _, inst = ep.attach_or_start(tmp_path / "a")
    assert inst is not None
    pgdata = _moved_bank(tmp_path / "b", "bb" * 32)
    _running(pgdata, "bb" * 32)
    dsn, inst = ep.attach_or_start(tmp_path / "b")
    assert dsn == ep._dsn("bb" * 32, 59998, scram=True) and inst is None


def test_a_fresh_bank_gets_its_own_owner_only_secret(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    calls = _stub_pg0(monkeypatch)
    dsn, _ = ep.attach_or_start(tmp_path)
    pgdata = tmp_path / "embedded_pg"
    # Read back through the owner-only validator the token files use.
    secret, _ = credentials._open_token(tmp_path / "embedded_pg.secret")
    assert len(secret) == 64 and int(secret, 16) >= 0
    assert dsn == ep._dsn(secret, 59999, scram=True)
    # pg0 never sees the secret: its own defaults, and it creates no database.
    init = next(call for call in calls if call[0] == "init")
    assert init[2] is None and "password" not in init[4]
    assert next(call for call in calls if call[0] == "start")[1] == "postgres"
    # SCRAM first, reloaded with the public password (the secret with SCRAM
    # required is refused while the role is still on the public one); then
    # the role over SCRAM; then everything with the secret, SCRAM required.
    sql_calls = [call for call in calls if call[0] == "sql"]
    legacy = ep._dsn("postgres", 59999, "postgres")
    assert [call[1] for call in sql_calls[:2]] == [legacy, legacy]
    assert all(call[1] == ep._dsn(secret, 59999, "postgres", scram=True)
               for call in sql_calls[2:])
    assert _sql(calls) == _scram_first(secret) + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}
    assert _ROLES[pgdata] == secret


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
    assert _ROLES[pgdata] == secret
    # Comments and layout survive; only method columns change.
    text = (pgdata / "pg_hba.conf").read_text()
    assert text.splitlines()[0] == _LEGACY_HBA.splitlines()[0]
    assert "127.0.0.1/32            scram-sha-256" in text


def test_a_migrated_bank_starts_with_its_secret_and_changes_nothing(monkeypatch, tmp_path):
    _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    ep.stop_embedded() if ep._owned else None
    _SERVERS.clear()
    secret = (tmp_path / "embedded_pg.secret").read_text()
    hba = (tmp_path / "embedded_pg" / "pg_hba.conf").read_text()
    calls = _stub_pg0(monkeypatch)
    dsn, _ = ep.attach_or_start(tmp_path)
    assert dsn == ep._dsn(secret, 59999, scram=True)
    assert _sql(calls) == _CREATE_DB
    assert (tmp_path / "embedded_pg" / "pg_hba.conf").read_text() == hba
    assert (tmp_path / "embedded_pg.secret").read_text() == secret


def test_an_interrupted_migration_completes_on_the_next_start(monkeypatch, tmp_path):
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
    _running(pgdata, "postgres")
    dsn, started = ep.attach_or_start(tmp_path)
    assert started is None and dsn == ep._dsn("cd" * 32, 59998, scram=True)
    assert not any(call[0] == "start" for call in calls)
    assert _sql(calls) == _move("cd" * 32) + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}


def test_a_running_server_on_the_public_password_is_moved_on_attach(monkeypatch, tmp_path):
    """A fresh bank killed before its move, or an older release's server,
    still takes the public password: it moves now, SCRAM first."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _legacy_bank(tmp_path)
    _running(pgdata, "postgres")
    dsn, started = ep.attach_or_start(tmp_path)
    secret = (tmp_path / "embedded_pg.secret").read_text()
    assert (dsn, started) == (ep._dsn(secret, 59998, scram=True), None)
    assert _sql(calls) == _scram_first(secret) + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}


def test_a_move_killed_between_its_reload_and_its_role_change_finishes(monkeypatch, tmp_path):
    """SCRAM in force, role still on the public password (it authenticates
    over SCRAM): the next caller gives the role the secret."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "12" * 32)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256"))
    calls = _stub_pg0(monkeypatch)
    _running(pgdata, "postgres")
    dsn, _ = ep.attach_or_start(tmp_path)
    assert dsn == ep._dsn("12" * 32, 59998, scram=True)
    assert _sql(calls) == _scram_first("12" * 32) + _CREATE_DB


def test_a_role_that_already_takes_the_secret_is_not_altered(monkeypatch, tmp_path):
    """The SCRAM-first move confirms before it changes the role: a role an
    interrupted move already gave the secret is left as it is."""
    _stub_pg0(monkeypatch)
    pgdata = _legacy_bank(tmp_path)
    _running(pgdata, "34" * 32)
    calls = _stub_pg0(monkeypatch)
    ep._move_over_scram(59998, pgdata, "34" * 32, {"altered": False})
    assert _sql(calls) == ["SELECT pg_reload_conf()", "SELECT 1"]


def test_a_running_server_missing_its_database_gets_it_on_attach(monkeypatch, tmp_path):
    """A starter killed after the move but before CREATE DATABASE."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _moved_bank(tmp_path, "de" * 32)
    _running(pgdata, "de" * 32)
    ep.attach_or_start(tmp_path)
    assert _sql(calls) == _CREATE_DB


def test_a_failure_after_an_owned_start_stops_the_server(monkeypatch, tmp_path):
    """Nothing this call started is left running unowned (and on trust);
    the next start finishes the move."""
    calls = _stub_pg0(monkeypatch, sql_error_on="ALTER ROLE")
    pgdata = _legacy_bank(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        ep.attach_or_start(tmp_path)
    assert ("stop",) in calls
    assert ep._owned == []
    # The marker stays. A role change was attempted, so it may have given
    # the role the new secret: the secret is kept, and the next start
    # finishes with it.
    assert _methods(pgdata) == {"trust"}
    secret = (tmp_path / "embedded_pg.secret").read_text()
    calls = _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    assert (tmp_path / "embedded_pg.secret").read_text() == secret
    assert _sql(calls) == _move(secret) + _CREATE_DB


def test_a_failed_start_puts_a_marked_file_back(monkeypatch, tmp_path):
    """pg0 refuses a server it has no record of with a generic error; the
    server may be running, so its file must not be left on trust."""
    _stub_pg0(monkeypatch, start_error=RuntimeError("lock file exists"))
    pgdata = _legacy_bank(tmp_path)
    with pytest.raises(RuntimeError, match="lock file exists"):
        ep.attach_or_start(tmp_path)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA
    # No role change was attempted: the secret this call created is dropped.
    assert not (tmp_path / "embedded_pg.secret").exists()


def test_a_running_server_that_takes_no_known_password_is_refused_before_trust(
    monkeypatch, tmp_path,
):
    """On Windows a pg_hba.conf change applies to new connections at once,
    so the trust marker is never written while a server answers on the
    bank's port."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _legacy_bank(tmp_path)
    _running(pgdata, "not-a-known-password")
    with pytest.raises(RuntimeError, match="accepts neither") as refused:
        ep.attach_or_start(tmp_path)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA
    assert not any(call[0] == "start" for call in calls)
    # The refusal names the command that stops it.
    assert f"pg0.stop('{ep._instance_name(pgdata)}')" in str(refused.value)


def test_pg0_reporting_the_server_running_is_refused_with_the_stop_command(
    monkeypatch, tmp_path,
):
    _stub_pg0(monkeypatch, already_running=True)
    pgdata = _legacy_bank(tmp_path)
    with pytest.raises(RuntimeError, match="accepts neither") as refused:
        ep.attach_or_start(tmp_path)
    assert (pgdata / "pg_hba.conf").read_text() == _LEGACY_HBA
    assert f"pg0.stop('{ep._instance_name(pgdata)}')" in str(refused.value)


def test_an_old_bank_with_an_existing_secret_is_still_moved(monkeypatch, tmp_path):
    """A secret file can exist before the bank moves: the password method
    alone marks the bank."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "56" * 32)
    calls = _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    assert ep._is_marked(next(call for call in calls if call[0] == "start")[2])
    assert _sql(calls) == _move("56" * 32) + _CREATE_DB


def test_the_trust_marker_never_opens_a_network_rule(monkeypatch, tmp_path):
    """A rule a user pointed at the network keeps its method while the bank
    is on the trust marker, and moves to SCRAM with the rest."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _legacy_bank(tmp_path)
    remote = "host    all             all             10.0.0.0/8              password\n"
    (pgdata / "pg_hba.conf").write_text(_LEGACY_HBA + remote)
    ep.attach_or_start(tmp_path)
    start = next(call for call in calls if call[0] == "start")
    assert remote in start[2]
    assert ep._is_marked(start[2])
    final = (pgdata / "pg_hba.conf").read_text()
    assert "10.0.0.0/8              scram-sha-256" in final
    assert ep._managed_methods(final) == {"scram-sha-256"}
    assert ep._is_loopback_rule("host all all 127.0.0.1 255.255.255.255 md5".split())
    assert not ep._is_loopback_rule("host all all 127.0.0.0/8 md5".split())
    assert not ep._is_loopback_rule("host all all samehost md5".split())


def test_the_dsn_names_the_database_the_module_creates(monkeypatch, tmp_path):
    """evals/rust_port/phase1_ci.py swaps _DB_NAME around attach_or_start."""
    calls = _stub_pg0(monkeypatch)
    monkeypatch.setattr(ep, "_DB_NAME", "plbench_phase1_bootstrap")
    dsn, _ = ep.attach_or_start(tmp_path)
    assert "/plbench_phase1_bootstrap" in dsn
    assert "CREATE DATABASE \"plbench_phase1_bootstrap\"" in _sql(calls)


def test_the_probe_offers_the_secret_only_over_scram_to_this_bank(monkeypatch, tmp_path):
    """The real probe: the secret goes with require_auth (libpq 16+), the
    public password without, and a server whose data directory is another
    pgdata is not taken for the bank."""
    import psycopg

    pgdata = tmp_path / "embedded_pg"
    pgdata.mkdir()
    seen = []
    served = {"dir": str(pgdata)}

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            assert sql == "SHOW data_directory"
            return types.SimpleNamespace(fetchone=lambda: (served["dir"],))

    def connect(dsn, connect_timeout=None):
        seen.append(dsn)
        return FakeConn()

    monkeypatch.setattr(psycopg, "connect", connect)
    secret = "78" * 32
    assert ep._probe(5999, pgdata, (secret, "postgres")) == secret
    if ep._libpq_has_require_auth():
        assert seen[0].endswith("?require_auth=scram-sha-256")
    served["dir"] = str(tmp_path / "another" / "embedded_pg")
    seen.clear()
    assert ep._probe(5999, pgdata, (secret, "postgres")) is None
    assert len(seen) == 2 and "require_auth" not in seen[1]


def test_a_lost_secret_rekeys_through_the_trust_marker(monkeypatch, tmp_path):
    """A moved bank whose secret file is gone gets a new one, set on the
    role under trust, instead of being locked for good."""
    _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    _SERVERS.clear()
    old = (tmp_path / "embedded_pg.secret").read_text()
    (tmp_path / "embedded_pg.secret").unlink()
    calls = _stub_pg0(monkeypatch)
    ep.attach_or_start(tmp_path)
    new = (tmp_path / "embedded_pg.secret").read_text()
    assert new != old
    assert ep._managed_methods(next(c for c in calls if c[0] == "start")[2]) == {"trust"}
    assert _sql(calls) == _move(new) + _CREATE_DB


def test_a_secret_file_with_an_unexpected_value_is_refused(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    _stub_pg0(monkeypatch)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "x'--")
    with pytest.raises(RuntimeError, match="unexpected value"):
        ep.attach_or_start(tmp_path)


def test_a_stopped_bank_on_scram_rules_with_the_old_role_password_finishes(
    monkeypatch, tmp_path,
):
    """A move interrupted between its SCRAM rewrite and its role change,
    then the server stopped (a reboot): the rules alone say "moved", so the
    owned start asks the server which password the role takes."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "9a" * 32)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256"))
    calls = _stub_pg0(monkeypatch)
    _, started = ep.attach_or_start(tmp_path)
    assert started is not None
    assert _sql(calls) == _scram_first("9a" * 32) + _CREATE_DB


def test_an_owned_start_refuses_a_secret_the_server_does_not_take(monkeypatch, tmp_path):
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "bc" * 32)
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256"))
    _ROLES[pgdata] = "some-other-password"
    calls = _stub_pg0(monkeypatch)
    with pytest.raises(RuntimeError, match="Move that file aside"):
        ep.attach_or_start(tmp_path)
    assert ("stop",) in calls
    assert (tmp_path / "embedded_pg.secret").exists()


def test_a_created_secret_the_role_may_hold_is_kept_after_a_failure(monkeypatch, tmp_path):
    """Attach moved a running server's role to a just-created secret, then
    a later step failed: the server may require that secret now."""
    _stub_pg0(monkeypatch, sql_error_on="CREATE DATABASE")
    pgdata = _legacy_bank(tmp_path)
    _running(pgdata, "postgres")
    with pytest.raises(RuntimeError, match="injected"):
        ep.attach_or_start(tmp_path)
    assert (tmp_path / "embedded_pg.secret").exists()
    assert _ROLES[pgdata] == (tmp_path / "embedded_pg.secret").read_text()


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


def test_secret_dsns_require_scram_and_split_for_pgpassword():
    secret = "ef" * 32
    if ep._libpq_has_require_auth():
        assert ep._dsn(secret, 5999, scram=True).endswith("?require_auth=scram-sha-256")
    # The public old password is never sent with a SCRAM requirement.
    assert "require_auth" not in ep._dsn("postgres", 5999)
    bare, password = ep.split_password(ep._dsn(secret, 5999, scram=True))
    assert password == secret and secret not in bare
    assert bare.startswith("postgresql://postgres@127.0.0.1:5999/pseudolife_memory")
    assert ep.split_password("postgresql://u@h:1/db") == ("postgresql://u@h:1/db", None)


def test_the_secret_never_reaches_the_server_as_text(monkeypatch, tmp_path):
    """Every statement carries a verifier, and every connection that knows
    the secret requires SCRAM authentication; the rest use the public old
    password."""
    from pseudolife_memory import credentials

    calls = _stub_pg0(monkeypatch)
    monkeypatch.setattr(ep, "_scram_verifier", _VERIFIER)  # the real one
    _legacy_bank(tmp_path)
    ep.attach_or_start(tmp_path)
    secret, _ = credentials._open_token(tmp_path / "embedded_pg.secret")
    sql_calls = [call for call in calls if call[0] == "sql"]
    assert any("SCRAM-SHA-256$4096:" in s for call in sql_calls for s in call[2])
    for call in sql_calls:
        assert all(secret not in statement for statement in call[2])
        if secret in call[1] and ep._libpq_has_require_auth():
            assert call[1].endswith("?require_auth=scram-sha-256")


_VERIFIER = ep._scram_verifier


def test_the_scram_verifier_is_postgresql_s_format():
    """RFC 7677's SCRAM-SHA-256 with a fixed salt: the stored and server keys
    recomputed independently of the module's code path."""
    import base64
    import hashlib
    import hmac

    salt = bytes(range(16))
    verifier = _VERIFIER("ab" * 32, salt=salt)
    head, keys = verifier.rsplit("$", 1)
    scheme, params = head.split("$")
    iterations, salt_b64 = params.split(":")
    stored_b64, server_b64 = keys.split(":")
    assert (scheme, iterations, base64.b64decode(salt_b64)) == ("SCRAM-SHA-256", "4096", salt)
    salted = hashlib.pbkdf2_hmac("sha256", b"ab" * 32, salt, 4096)
    client = hmac.new(salted, b"Client Key", "sha256").digest()
    assert base64.b64decode(stored_b64) == hashlib.sha256(client).digest()
    assert base64.b64decode(server_b64) == hmac.new(salted, b"Server Key", "sha256").digest()
    # A fresh salt every call.
    assert _VERIFIER("ab" * 32) != _VERIFIER("ab" * 32)


_REAL_ALIVE = ep._postmaster_alive


@pytest.mark.parametrize("posix", [False, True])
def test_fresh_and_legacy_banks_move_with_either_rule_timing(monkeypatch, tmp_path, posix):
    """POSIX servers keep their loaded rules until a reload, Windows applies
    the file at once; both moves end in the same place."""
    calls = _stub_pg0(monkeypatch, posix=posix)
    ep.attach_or_start(tmp_path / "fresh")
    secret = (tmp_path / "fresh" / "embedded_pg.secret").read_text()
    assert _sql(calls) == _scram_first(secret) + _CREATE_DB
    _SERVERS.clear()
    _LOADED.clear()
    calls = _stub_pg0(monkeypatch, posix=posix)
    pgdata = _legacy_bank(tmp_path / "legacy")
    ep.attach_or_start(tmp_path / "legacy")
    secret = (tmp_path / "legacy" / "embedded_pg.secret").read_text()
    assert _sql(calls) == _move(secret) + _CREATE_DB
    assert _ROLES[pgdata] == secret


def test_lite_refuses_a_libpq_without_require_auth(monkeypatch, tmp_path):
    _stub_pg0(monkeypatch)
    monkeypatch.setattr(ep, "_libpq_has_require_auth", lambda: False)
    with pytest.raises(RuntimeError, match="libpq 16 or newer"):
        ep.attach_or_start(tmp_path)
    assert not (tmp_path / "embedded_pg.secret").exists()


def test_a_stale_pid_file_with_a_foreign_listener_does_not_block_the_move(
    monkeypatch, tmp_path,
):
    """After a reboot the recorded pid is gone (or another program's) and an
    unrelated server may listen on the recorded port: not a running bank."""
    import os

    calls = _stub_pg0(monkeypatch)
    monkeypatch.setattr(ep, "_postmaster_alive", _REAL_ALIVE)
    pgdata = _legacy_bank(tmp_path)
    (pgdata / "postmaster.pid").write_text(f"{os.getpid()}\n{pgdata}\n1\n5432\n")
    assert not ep._postmaster_alive(pgdata)
    _, started = ep.attach_or_start(tmp_path)
    assert started is not None
    assert any(call[0] == "start" for call in calls)


def test_postmaster_alive_needs_a_live_postgres_pid(tmp_path):
    import os

    assert not _REAL_ALIVE(tmp_path)  # no postmaster.pid
    (tmp_path / "postmaster.pid").write_text(f"{os.getpid()}\nx\n")
    assert not _REAL_ALIVE(tmp_path)  # alive, but no postgres
    (tmp_path / "postmaster.pid").write_text("not a pid\n")
    assert not _REAL_ALIVE(tmp_path)


def test_a_failed_start_stops_whatever_pg0_may_have_launched(monkeypatch, tmp_path):
    calls = _stub_pg0(monkeypatch, start_error=RuntimeError("readiness timeout"))
    _legacy_bank(tmp_path)
    with pytest.raises(RuntimeError, match="readiness timeout"):
        ep.attach_or_start(tmp_path)
    assert ("stop",) in calls


def test_a_trust_file_on_a_server_running_scram_goes_back_to_scram(monkeypatch, tmp_path):
    """POSIX: a starter killed after AlreadyRunning but before it restored
    the file leaves trust in the file while the server runs SCRAM with the
    role on the secret. The probe takes the secret over SCRAM, so only the
    file goes back; the role is not changed."""
    from pseudolife_memory import credentials

    pgdata = _legacy_bank(tmp_path)
    credentials._write_token_file(tmp_path / "embedded_pg.secret", "f0" * 32)
    scram = ep._rewrite_hba(_LEGACY_HBA, "scram-sha-256")
    (pgdata / "pg_hba.conf").write_text(ep._rewrite_hba(_LEGACY_HBA, "trust", loopback_only=True))
    calls = _stub_pg0(monkeypatch, posix=True)
    _running(pgdata, "f0" * 32, loaded=scram)
    ep.attach_or_start(tmp_path)
    assert _sql(calls) == ["SELECT pg_reload_conf()", "SELECT 1"] + _CREATE_DB
    assert _methods(pgdata) == {"scram-sha-256"}


def test_a_role_change_refused_by_a_just_applied_reload_waits_for_scram(
    monkeypatch, tmp_path,
):
    """POSIX: the secret looked untaken while trust was still in force, then
    the reload landed and the old password fails the role change: the role
    already holds the secret, which the wait confirms."""
    calls = _stub_pg0(monkeypatch, sql_error_on="ALTER ROLE")
    pgdata = _legacy_bank(tmp_path)
    _running(pgdata, "a1" * 32)
    real = ep._takes_secret
    first = {"call": True}

    def takes_secret(port, secret):
        # The first check meets trust (the reload still pending); later ones
        # meet the server as it is.
        if first["call"]:
            first["call"] = False
            return False
        return real(port, secret)

    monkeypatch.setattr(ep, "_takes_secret", takes_secret)
    ep._move_over_scram(59998, pgdata, "a1" * 32, {"altered": False})
    assert _sql(calls) == ["SELECT pg_reload_conf()", "SELECT 1"]


def test_the_scram_verifier_passes_rfc_7677_s_published_exchange():
    """RFC 7677 section 3: user "user", password "pencil", this salt and
    4096 iterations. A verifier from these inputs must check the RFC's client
    proof and produce its server signature."""
    import base64
    import hashlib
    import hmac

    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    stored_key, server_key = (base64.b64decode(k) for k in
                              _VERIFIER("pencil", salt=salt).split("$")[2].split(":"))
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = ("n=user,r=rOprNGfwEbeRWgbNEkqO,"
            f"r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
            f"c=biws,r={nonce}").encode()
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_signature = hmac.new(stored_key, auth, hashlib.sha256).digest()
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature))
    assert hashlib.sha256(client_key).digest() == stored_key
    assert base64.b64encode(hmac.new(server_key, auth, hashlib.sha256).digest()).decode() \
        == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="


def test_a_running_moved_bank_that_takes_no_password_is_refused_not_stopped(
    monkeypatch, tmp_path,
):
    """A healthy server can fail the probe (busy, starting): an unmarked
    bank is refused too, so no start runs and nothing stops the daemon's
    server."""
    calls = _stub_pg0(monkeypatch)
    pgdata = _moved_bank(tmp_path, "c4" * 32)
    _running(pgdata, "the-probe-cannot-get-in")
    with pytest.raises(RuntimeError, match="may be busy"):
        ep.attach_or_start(tmp_path)
    assert not any(call[0] in ("start", "stop") for call in calls)


def test_postmaster_alive_fails_closed_and_matches_the_start_time(monkeypatch, tmp_path):
    import psutil

    (tmp_path / "postmaster.pid").write_text("4321\nd\n1792900000\n5432\n")

    class Fake:
        name_ = "postgres.exe"
        created = 1792900000.4
        error = None

        def __init__(self, pid):
            assert pid == 4321
            if Fake.error:
                raise Fake.error

        def name(self):
            return Fake.name_

        def create_time(self):
            return Fake.created

    monkeypatch.setattr(psutil, "Process", Fake)
    assert _REAL_ALIVE(tmp_path)
    # The stamp lags the process start, by seconds on a slow start: still it.
    for created in (1792899999.5, 1792900030.0):
        Fake.created = created
        assert _REAL_ALIVE(tmp_path)
    Fake.created = 1792990000.0  # another postgres took the pid after a reboot
    assert not _REAL_ALIVE(tmp_path)
    Fake.error = psutil.AccessDenied(4321)  # alive, not inspectable
    assert _REAL_ALIVE(tmp_path)
    Fake.error = psutil.NoSuchProcess(4321)
    assert not _REAL_ALIVE(tmp_path)
    Fake.error, Fake.name_ = None, "notepad.exe"
    assert not _REAL_ALIVE(tmp_path)
