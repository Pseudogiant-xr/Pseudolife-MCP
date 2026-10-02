"""The suite finds the dev Postgres password where compose does, and a
reachable server that rejects it errors the PG-backed tests instead of
skipping them (2026-09-20: ~1000 silent skips after the 09-14 rotation)."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from tests import helpers, pg_defaults, pg_fixtures  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# -- password resolution ------------------------------------------------------

def test_env_file_password_reads_the_last_assignment_and_tolerates_crlf(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"# comment\r\nPOSTGRES_PASSWORD=first\r\nOTHER=x\r\n"
        b"POSTGRES_PASSWORD='second'\r\n")
    assert pg_defaults.env_file_password(env_file) == "second"


def test_env_file_password_is_none_when_missing_or_unset(tmp_path):
    assert pg_defaults.env_file_password(tmp_path / "absent") is None
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=\n", encoding="utf-8")
    assert pg_defaults.env_file_password(env_file) is None


@pytest.mark.parametrize(("assignment", "expected"), [
    ("POSTGRES_PASSWORD=plain-value # operator note", "plain-value"),
    ("POSTGRES_PASSWORD='single # and $ stay literal' # note",
     "single # and $ stay literal"),
    ('POSTGRES_PASSWORD="double # stays literal" # note',
     "double # stays literal"),
])
def test_env_file_password_follows_compose_comments_and_quotes(
        tmp_path, assignment, expected):
    env_file = tmp_path / ".env"
    env_file.write_text(assignment + "\n", encoding="utf-8")
    assert pg_defaults.env_file_password(env_file) == expected


@pytest.mark.parametrize("assignment", [
    "POSTGRES_PASSWORD=$UNSUPPORTED_SECRET_VALUE",
    'POSTGRES_PASSWORD="${UNSUPPORTED_SECRET_VALUE}"',
])
def test_env_file_password_rejects_unsupported_compose_expansion_without_value(
        tmp_path, assignment):
    env_file = tmp_path / ".env"
    env_file.write_text(assignment + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        pg_defaults.env_file_password(env_file)
    assert "Compose variable expansion" in str(exc.value)
    assert "UNSUPPORTED_SECRET_VALUE" not in str(exc.value)


def test_default_password_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=from-env-file\n", encoding="utf-8")
    assert pg_defaults.default_password({}, env_file) == "from-env-file"
    assert pg_defaults.default_password(
        {"PSEUDOLIFE_TEST_PG_PASSWORD": "explicit"}, env_file) == "explicit"
    assert pg_defaults.default_password({}, tmp_path / "absent") == "pseudolife"


@pytest.mark.parametrize("prefix", ['"', '$'])
def test_invalid_env_file_reports_hide_values_with_locals(tmp_path, prefix):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=" + prefix + SECRET + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        pg_defaults.env_file_password(env_file)
    assert SECRET not in _rendered(failure, showlocals=True)


def test_env_parser_traceback_redaction_is_load_bearing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=$" + SECRET + "\n", encoding="utf-8")
    monkeypatch.setattr(pg_defaults, "env_file_password", pg_defaults._read_env_file_password)
    with pytest.raises(ValueError) as failure:
        pg_defaults.env_file_password(env_file)
    assert SECRET in _rendered(failure, showlocals=True)


def test_default_admin_url_embeds_the_resolved_password(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=s3cret\n", encoding="utf-8")
    assert pg_defaults.default_admin_url({}, env_file) == (
        f"postgresql://pseudolife:s3cret@{pg_defaults.DEV_HOST_PORT}/postgres")


def test_the_dev_server_address_can_be_moved_off_5433():
    # A machine whose 5433 is a live bank's server (the homelab box) points
    # every default path at its test server instead.
    import subprocess
    import sys

    code = "from tests import pg_defaults as d; print(d.DEV_HOST_PORT)"
    root = str(pg_defaults.ENV_FILE.parent.parent)
    env = {k: v for k, v in os.environ.items() if k != "PSEUDOLIFE_TEST_PG_HOST_PORT"}
    plain = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                           capture_output=True, text=True, check=True)
    assert plain.stdout.strip() == "127.0.0.1:5433"
    moved = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                           text=True, check=True,
                           env={**env, "PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:5434"})
    assert moved.stdout.strip() == "127.0.0.1:5434"
    bad = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                         text=True, env={**env, "PSEUDOLIFE_TEST_PG_HOST_PORT": "5434"})
    assert bad.returncode != 0 and "PSEUDOLIFE_TEST_PG_HOST_PORT" in bad.stderr


def test_default_admin_url_percent_encodes_a_generated_password():
    """`@` would re-split the authority; `%` `/` `:` `#` `?` are misparsed."""
    url = pg_defaults.default_admin_url({"PSEUDOLIFE_TEST_PG_PASSWORD": "p@ss/w:rd#1%?"})
    assert url == (f"postgresql://pseudolife:p%40ss%2Fw%3Ard%231%25%3F@"
                   f"{pg_defaults.DEV_HOST_PORT}/postgres")
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(url)["password"] == "p@ss/w:rd#1%?"


def test_fixture_default_admin_follows_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=rotated\n", encoding="utf-8")
    monkeypatch.setattr(pg_defaults, "ENV_FILE", env_file)
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_TEST_PG_PASSWORD", raising=False)
    assert pg_fixtures._admin_url() == (
        f"postgresql://pseudolife:rotated@{pg_defaults.DEV_HOST_PORT}/postgres")
    assert pg_fixtures.resolve_test_db_url.__doc__ or True  # exists
    # An explicit override is still returned verbatim.
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", "postgresql://u:p@h:1/db")
    parsed = psycopg.conninfo.conninfo_to_dict(pg_fixtures._admin_url())
    assert parsed["host"] == "h" and parsed["port"] == "1"
    assert parsed["user"] == "u" and parsed["password"] == "p"
    assert parsed["dbname"] == "postgres"


def test_bench_admin_resolution_prefers_explicit_then_test_uri_and_keyword_dsn():
    explicit = "host=bench.invalid port=6001 user=bench password=explicit dbname=postgres"
    test_url = (
        "postgresql://test-user:test-pass@test.invalid:6002/test_db"
        "?sslmode=require&application_name=fixture")
    assert pg_defaults.bench_admin_url({
        "PSEUDOLIFE_BENCH_ADMIN_URL": explicit,
        "PSEUDOLIFE_TEST_DATABASE_URL": test_url,
    }) == explicit

    from_uri = psycopg.conninfo.conninfo_to_dict(pg_defaults.bench_admin_url({
        "PSEUDOLIFE_TEST_DATABASE_URL": test_url,
    }))
    assert from_uri == {
        "user": "test-user", "password": "test-pass", "dbname": "postgres",
        "host": "test.invalid", "port": "6002", "application_name": "fixture",
        "sslmode": "require",
    }

    keyword = (
        "host=keyword.invalid port=6003 user=kw password=kw-pass "
        "dbname=target sslmode=verify-full connect_timeout=9")
    from_keyword = psycopg.conninfo.conninfo_to_dict(
        pg_defaults.bench_admin_url({"PSEUDOLIFE_TEST_DATABASE_URL": keyword}))
    assert from_keyword == {
        "user": "kw", "password": "kw-pass", "dbname": "postgres",
        "host": "keyword.invalid", "port": "6003", "connect_timeout": "9",
        "sslmode": "verify-full",
    }


def test_bench_admin_fallback_keeps_explicit_test_password(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=file-value\n", encoding="utf-8")
    resolved = pg_defaults.bench_admin_url(
        {"PSEUDOLIFE_TEST_PG_PASSWORD": "explicit-value"}, env_file)
    parsed = psycopg.conninfo.conninfo_to_dict(resolved)
    assert parsed["password"] == "explicit-value"


@pytest.mark.parametrize("override", [
    ("postgresql://worker:worker-pass@worker.invalid:6004/base_db"
     "?sslmode=require&application_name=xdist"),
    ("host=worker.invalid port=6004 user=worker password=worker-pass "
     "dbname=base_db sslmode=require application_name=xdist"),
])
def test_resolve_test_db_url_preserves_options_and_applies_xdist_suffix(
        monkeypatch, override):
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", override)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    parsed = psycopg.conninfo.conninfo_to_dict(
        pg_fixtures.resolve_test_db_url())
    assert parsed == {
        "user": "worker", "password": "worker-pass", "dbname": "base_db_gw2",
        "host": "worker.invalid", "port": "6004",
        "application_name": "xdist", "sslmode": "require",
    }


# -- auth failure vs no server ------------------------------------------------

AUTH_MSG = ('connection failed: connection to server at "127.0.0.1", port 5433 '
            'failed: FATAL:  password authentication failed for user "pseudolife"')
REFUSED_MSG = ('connection failed: connection to server at "127.0.0.1", port 5433 '
               'failed: Connection refused')


def test_is_auth_failure_classifies_psycopg_messages():
    assert pg_defaults.is_auth_failure(psycopg.OperationalError(AUTH_MSG))
    assert pg_defaults.is_auth_failure(psycopg.errors.InvalidPassword("x"))
    # SQLSTATE 28000 siblings: unknown role, no pg_hba entry — the server is
    # there and will not have you; skipping would reopen the silent-skip trap.
    assert pg_defaults.is_auth_failure(psycopg.errors.InvalidAuthorizationSpecification("x"))
    assert pg_defaults.is_auth_failure(psycopg.OperationalError(
        'connection failed: FATAL:  role "pseudolife" does not exist'))
    assert pg_defaults.is_auth_failure(psycopg.OperationalError(
        'connection failed: FATAL:  no pg_hba.conf entry for host "10.0.0.5"'))
    # Not auth: refused, timed out, or a missing DATABASE (server accepted us).
    assert not pg_defaults.is_auth_failure(psycopg.OperationalError(REFUSED_MSG))
    assert not pg_defaults.is_auth_failure(TimeoutError("timed out"))
    assert not pg_defaults.is_auth_failure(psycopg.OperationalError(
        'connection failed: FATAL:  database "pseudolife_memory_test_1" does not exist'))


# -- Windows loopback port errors on connect ---------------------------------
# libpq renders a Windows socket error as "<text> (0x%08X/%d)". Two of them
# name this host's port table, not the server: WSAEADDRINUSE, which a
# loopback connect() returns when the local port it picked still has a
# TIME_WAIT entry to the same server, and WSAENOBUFS, when the ephemeral
# range is exhausted. WSAEADDRINUSE reached pg_conn setup and PostgresStorage
# in full runs on 2026-09-25 (see tests/pg_defaults.py); WSAENOBUFS is its
# exhaustion sibling, covered by reasoning rather than a sighting.
ADDR_IN_USE_MSG = ('connection failed: connection to server at "127.0.0.1", '
                   'port 5433 failed: Address already in use '
                   '(0x00002740/10048)')
NO_BUFFERS_MSG = ('connection failed: connection to server at "127.0.0.1", '
                  'port 5433 failed: No buffer space available '
                  '(0x00002747/10055)')


def test_local_port_exhaustion_is_told_apart_from_server_failures():
    assert pg_defaults.is_local_port_exhaustion(
        psycopg.OperationalError(ADDR_IN_USE_MSG))
    assert pg_defaults.is_local_port_exhaustion(
        psycopg.OperationalError(NO_BUFFERS_MSG))
    for other in (psycopg.OperationalError(REFUSED_MSG),
                  psycopg.OperationalError(AUTH_MSG),
                  psycopg.errors.InvalidPassword("x (0x00002740/10048)"),
                  TimeoutError("timed out")):
        assert not pg_defaults.is_local_port_exhaustion(other), other


def _flaky_connect(failures: int, message: str):
    calls = {"n": 0}

    def connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise psycopg.OperationalError(message)
        return ("connected", args, kwargs)
    return connect, calls


def test_connect_retries_local_port_exhaustion_then_succeeds():
    connect, calls = _flaky_connect(2, ADDR_IN_USE_MSG)
    sleeps: list[float] = []
    wrapped = pg_defaults.retry_local_port_exhaustion(connect,
                                                      sleep=sleeps.append)
    assert wrapped("dsn", autocommit=True) == (
        "connected", ("dsn",), {"autocommit": True})
    assert calls["n"] == 3
    assert len(sleeps) == 2 and sum(sleeps) < 1.0


def test_connect_gives_up_after_its_attempts_with_the_real_error():
    connect, calls = _flaky_connect(99, NO_BUFFERS_MSG)
    wrapped = pg_defaults.retry_local_port_exhaustion(
        connect, attempts=3, sleep=lambda s: None)
    with pytest.raises(psycopg.OperationalError, match="10055"):
        wrapped("dsn")
    assert calls["n"] == 3


def test_connect_does_not_retry_any_other_failure():
    """A refused, timed-out or rejected connect is an answer about the
    server; retrying it would only slow the skip or the error down."""
    for message in (REFUSED_MSG, AUTH_MSG):
        connect, calls = _flaky_connect(99, message)
        wrapped = pg_defaults.retry_local_port_exhaustion(
            connect, sleep=lambda s: pytest.fail("must not retry"))
        with pytest.raises(psycopg.OperationalError):
            wrapped("dsn")
        assert calls["n"] == 1


def test_conftest_retries_local_port_exhaustion_on_every_connect():
    """Installed once, at conftest import, on psycopg.connect itself: the
    suite's fixtures and the storage code under test all connect through
    that attribute, so a single wrapper covers both (tests that patch
    psycopg.connect put this wrapper back when they finish)."""
    assert getattr(psycopg.connect, "retries_local_port_exhaustion", False)


def _fake_connect(exc):
    calls = {"n": 0}

    def connect(*args, **kwargs):
        calls["n"] += 1
        raise exc
    return connect, calls


def test_ensure_test_db_errors_on_auth_failure_and_memoizes(monkeypatch):
    connect, calls = _fake_connect(psycopg.OperationalError(AUTH_MSG))
    monkeypatch.setattr(pg_fixtures.psycopg, "connect", connect)
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL",
                       "postgresql://u:p@auth-fail.invalid:1/db_auth")
    with pytest.raises(pg_defaults.PostgresAuthError) as exc:
        pg_fixtures.ensure_test_db()
    assert "rejected the credentials" in str(exc.value)
    assert "PSEUDOLIFE_TEST_PG_PASSWORD" in str(exc.value)
    with pytest.raises(pg_defaults.PostgresAuthError):
        pg_fixtures.ensure_test_db()
    assert calls["n"] == 1  # memoized: one probe per target, not one per test


def test_ensure_test_db_keeps_the_skip_path_for_an_absent_server(monkeypatch):
    connect, _ = _fake_connect(psycopg.OperationalError(REFUSED_MSG))
    monkeypatch.setattr(pg_fixtures.psycopg, "connect", connect)
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL",
                       "postgresql://u:p@refused.invalid:1/db_refused")
    with pytest.raises(RuntimeError) as exc:
        pg_fixtures.ensure_test_db()
    assert not isinstance(exc.value, pg_defaults.PostgresAuthError)
    assert "no test Postgres reachable" in str(exc.value)


def test_ensure_test_db_errors_on_reachable_setup_failure(monkeypatch):
    class Connected:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            raise psycopg.errors.InsufficientPrivilege(
                "permission denied to create database")

    monkeypatch.setattr(pg_fixtures.psycopg, "connect",
                        lambda *args, **kwargs: Connected())
    monkeypatch.setenv(
        "PSEUDOLIFE_TEST_DATABASE_URL",
        "postgresql://u:p@setup-fail.invalid:1/db_setup_failure",
    )
    with pytest.raises(pg_defaults.PostgresSetupError) as first:
        pg_fixtures.ensure_test_db()
    with pytest.raises(pg_defaults.PostgresSetupError) as cached:
        pg_fixtures.ensure_test_db()
    with pytest.raises(pg_defaults.PostgresSetupError):
        pg_fixtures._skip_or_raise(first.value)
    assert first.value is not cached.value


def test_pg_url_outcome_skips_only_for_an_absent_server(monkeypatch):
    """The fixture's branch, isolated: auth failure propagates (ERROR),
    anything else becomes a skip."""
    monkeypatch.delenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", raising=False)
    with pytest.raises(pg_defaults.PostgresAuthError):
        pg_fixtures._skip_or_raise(pg_defaults.PostgresAuthError("bad password"))
    with pytest.raises(pytest.skip.Exception):
        pg_fixtures._skip_or_raise(
            pg_defaults.PostgresUnavailableError("no test Postgres reachable"))


def test_helpers_pg_reachable_raises_on_auth_failure_and_false_when_absent(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", raising=False)
    connect, _ = _fake_connect(psycopg.OperationalError(AUTH_MSG))
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(pg_defaults.PostgresAuthError):
        helpers.pg_reachable("postgresql://u:p@h:1/db")
    connect, _ = _fake_connect(psycopg.OperationalError(REFUSED_MSG))
    monkeypatch.setattr(psycopg, "connect", connect)
    assert helpers.pg_reachable("postgresql://u:p@h:1/db") is False

    connect, _ = _fake_connect(
        psycopg.errors.InvalidCatalogName("database does not exist"))
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(pg_defaults.PostgresSetupError):
        helpers.pg_reachable("postgresql://u:p@h:1/db")


def test_required_pg_fixture_errors_instead_of_skipping(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "1")
    try:
        with pytest.raises(pg_defaults.PostgresUnavailableError):
            pg_fixtures._skip_or_raise(
                pg_defaults.PostgresUnavailableError("no test Postgres reachable"))
    except pytest.skip.Exception:
        pytest.fail("Required PostgreSQL must error rather than skip")


def test_required_pg_reachability_errors_without_exposing_credentials(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "1")
    connect, _ = _fake_connect(psycopg.OperationalError(REFUSED_MSG))
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(pg_defaults.PostgresUnavailableError) as exc:
        helpers.pg_reachable("postgresql://u:private-test-value@h:1/db")
    assert "required" in str(exc.value)
    assert "private-test-value" not in str(exc.value)


def test_required_pg_reachable_server_still_runs(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "1")

    class Connected:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connected())
    assert helpers.pg_reachable("postgresql://u:p@h:1/db") is True


@pytest.mark.parametrize("required", [False, True])
def test_session_pg_preflight_is_required_only_when_requested(monkeypatch, required):
    from tests import conftest

    monkeypatch.setenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "1" if required else "0")
    calls = []
    monkeypatch.setattr(pg_fixtures, "ensure_test_db", lambda: calls.append("ensure"))
    monkeypatch.setattr(pg_fixtures, "resolve_test_db_url", lambda: "fixture-url")
    monkeypatch.setattr(helpers, "pg_reachable", lambda url: calls.append(url) or True)
    conftest.pytest_sessionstart(None)
    assert calls == (["ensure", "fixture-url"] if required else [])


def test_required_pg_session_cannot_start_without_the_database(monkeypatch):
    from tests import conftest

    monkeypatch.setenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "1")

    def unavailable():
        raise pg_defaults.PostgresUnavailableError("no test Postgres reachable")

    monkeypatch.setattr(pg_fixtures, "ensure_test_db", unavailable)
    with pytest.raises(pytest.UsageError, match="no test Postgres reachable"):
        conftest.pytest_sessionstart(None)


@pytest.mark.parametrize("failure", [
    psycopg.errors.InsufficientPrivilege("connection refused by policy"),
    psycopg.OperationalError("host a: connection refused; host b: FATAL: database x does not exist"),
])
def test_server_response_defeats_transport_skip_markers(monkeypatch, failure):
    connect, _ = _fake_connect(failure)
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", "postgresql://u:p@mixed.invalid:1/db")
    pg_fixtures._ensure_state.clear()
    with pytest.raises(pg_defaults.PostgresSetupError):
        pg_fixtures.ensure_test_db()
    with pytest.raises(pg_defaults.PostgresSetupError):
        helpers.pg_reachable("postgresql://u:p@mixed.invalid:1/db")


# -- the password never reaches the test report --------------------------------

SECRET = "s3cr3t-p4ssw0rd-never-shown"


def test_redacted_url_hides_the_password_in_repr_but_not_in_value():
    url = pg_defaults.RedactedUrl(f"postgresql://pseudolife:{SECRET}@127.0.0.1:5433/postgres")
    assert SECRET in str(url) and SECRET in url  # psycopg still gets the real value
    assert SECRET not in repr(url)
    assert repr(url) == "'postgresql://pseudolife:***@127.0.0.1:5433/postgres'"
    assert url.host == "127.0.0.1:5433/postgres"
    # A keyword DSN has no '@' to split on: withhold it entirely (the safe
    # direction) rather than echo it.
    kw = pg_defaults.RedactedUrl(f"host=h password={SECRET} dbname=d")
    assert SECRET not in repr(kw) and SECRET not in kw.host


def test_pg_url_and_resolve_test_db_url_hand_tests_a_redacting_string(monkeypatch):
    """Dozens of tests take pg_url as a parameter; pytest prints parameters
    in every failure report, so the fixture value itself must redact."""
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL",
                       f"postgresql://pseudolife:{SECRET}@override.invalid:1/db_x")
    got = pg_fixtures.resolve_test_db_url()
    assert isinstance(got, pg_defaults.RedactedUrl)
    assert SECRET in got and SECRET not in repr(got)


@pytest.mark.parametrize("dsn", [
    "postgresql://example@127.0.0.1/db?password=query-secret",
    "host=127.0.0.1 dbname=db password=prefix@query-secret",
])
def test_redacted_conninfo_hides_query_and_keyword_passwords(dsn):
    safe = pg_defaults.RedactedUrl(dsn)
    assert "query-secret" not in repr(safe)
    assert "query-secret" not in safe.host


def test_conninfo_database_replacement_overrides_query_database():
    from psycopg.conninfo import conninfo_to_dict

    dsn = "postgresql://example:synthetic@127.0.0.1/old?dbname=override&sslmode=require"
    actual = conninfo_to_dict(pg_defaults.conninfo_with_dbname(dsn, "postgres"))
    assert actual["dbname"] == "postgres"
    assert actual["sslmode"] == "require"


def _rendered(excinfo, *, showlocals: bool = False) -> str:
    """What pytest prints for this exception. ``funcargs=True`` is what the
    real report path passes (``_pytest.nodes.Node._repr_failure_py``) and
    is the channel the password leaked through: without it, frame
    arguments are not rendered and this helper would prove nothing."""
    return str(excinfo.getrepr(
        style="long", showlocals=showlocals, funcargs=True, chain=True))


def _reachable_report(monkeypatch) -> str:
    def connect(conninfo, **kwargs):  # psycopg's frame shows conninfo verbatim
        raise psycopg.OperationalError(AUTH_MSG)
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(pg_defaults.PostgresAuthError) as excinfo:
        helpers.pg_reachable(f"postgresql://pseudolife:{SECRET}@127.0.0.1:5433/postgres")
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    return _rendered(excinfo)


def test_pg_reachable_auth_error_report_carries_no_password(monkeypatch):
    report = _reachable_report(monkeypatch)
    assert SECRET not in report
    assert "rejected the credentials" in report
    assert "url = 'postgresql://pseudolife:***@" in report  # the arg IS rendered, redacted


def test_the_leak_guard_is_load_bearing(monkeypatch):
    """Disable the redaction and the same renderer MUST show the password —
    otherwise the guard above is decoration (review finding, 2026-09-20)."""
    class _Unredacted(str):  # same interface, no redacting repr
        @property
        def host(self):
            return self.rpartition("@")[2]

    monkeypatch.setattr(pg_defaults, "RedactedUrl", _Unredacted)
    report = _reachable_report(monkeypatch)
    assert SECRET in report


def test_ensure_test_db_auth_error_report_carries_no_password(monkeypatch):
    def connect(conninfo, **kwargs):
        raise psycopg.OperationalError(AUTH_MSG)
    monkeypatch.setattr(pg_fixtures.psycopg, "connect", connect)
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL",
                       f"postgresql://pseudolife:{SECRET}@leak-check.invalid:1/db_leak")
    with pytest.raises(pg_defaults.PostgresAuthError) as first:
        pg_fixtures.ensure_test_db()
    with pytest.raises(pg_defaults.PostgresAuthError) as second:
        pg_fixtures.ensure_test_db()
    for excinfo in (first, second):
        report = _rendered(excinfo, showlocals=True)
        assert SECRET not in report
        assert "leak-check.invalid" in report
    matching_cache_keys = [
        key for key in pg_fixtures._ensure_state
        if "leak-check.invalid" in str(key[0])
    ]
    assert matching_cache_keys
    assert SECRET not in repr(matching_cache_keys)
    # Memoized hits raise a fresh object, so tracebacks do not accumulate.
    assert first.value is not second.value
    assert len(second.traceback) <= len(first.traceback)


def test_ensure_test_db_showlocals_guard_is_load_bearing(monkeypatch):
    class _Unredacted(str):
        @property
        def host(self):
            return self.rpartition("@")[2]

    def connect(conninfo, **kwargs):
        raise psycopg.OperationalError(AUTH_MSG)

    monkeypatch.setattr(pg_fixtures, "RedactedUrl", _Unredacted)
    monkeypatch.setattr(pg_defaults, "RedactedUrl", _Unredacted)
    monkeypatch.setattr(pg_fixtures.psycopg, "connect", connect)
    monkeypatch.setenv(
        "PSEUDOLIFE_TEST_DATABASE_URL",
        f"postgresql://pseudolife:{SECRET}@load-bearing.invalid:1/db_leak",
    )
    with pytest.raises(pg_defaults.PostgresAuthError) as excinfo:
        pg_fixtures.ensure_test_db()
    assert SECRET in _rendered(excinfo, showlocals=True)


# -- no more scattered literals ----------------------------------------------

def test_no_test_module_hard_codes_the_dev_password_url():
    """Every dev-container URL in tests/ goes through pg_defaults, so a
    password rotation cannot silently turn one file's tests into skips."""
    # Assembled so this file's own source does not match the pattern.
    role = "pseudolife"
    literal = re.compile(rf"{role}:{role}@|password={role}\b")
    offenders = sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("*.py")
        if p.name != "pg_defaults.py" and literal.search(p.read_text(encoding="utf-8")))
    assert offenders == [], offenders


def test_conftest_defaults_the_bench_admin_url_for_the_eval_backed_tests():
    """test_recall / test_memcot_bench / test_constraint_pinning and
    evals/ladder_sweep.py read PSEUDOLIFE_BENCH_ADMIN_URL; conftest seeds it
    from the same resolver when the operator has not set it (and leaves an
    operator's own value alone — then there is nothing to check here)."""
    import os

    if not os.environ.get("_PSEUDOLIFE_BENCH_ADMIN_URL_SEEDED"):
        pytest.skip("operator set PSEUDOLIFE_BENCH_ADMIN_URL; conftest did not seed it")
    # Compared as a boolean on purpose: an assertion on the strings would
    # render the live URL, password included, into the report on failure.
    seeded_matches_resolver = (
        os.environ["PSEUDOLIFE_BENCH_ADMIN_URL"] == pg_defaults.bench_admin_url())
    assert seeded_matches_resolver


# -- the full-run password preflight -------------------------------------------
#
# 2026-09-27: two sessions ran the full suite from fresh worktrees whose
# ops/.env was missing or a copy of ops/.env.example. The resolved password
# was the compose default, the dev server rejected it, and each run went to
# the end with every PG-backed test ERRORing on setup (1,424 in one run): a
# gate that gated nothing, for the machine's one full-suite slot.

def _example_env(tmp_path: Path) -> Path:
    example = tmp_path / ".env.example"
    example.write_bytes((ROOT / "ops" / ".env.example").read_bytes())
    return example


def test_password_source_names_a_missing_or_example_env_file_without_the_value(tmp_path):
    example = _example_env(tmp_path)
    env_file = tmp_path / ".env"
    describe = pg_defaults.describe_pg_login_source

    assert "ops/.env is missing" in describe({}, env_file, example)
    env_file.write_bytes(example.read_bytes())
    assert "copy of ops/.env.example" in describe({}, env_file, example)
    env_file.write_text("POSTGRES_USER=pseudolife\n", encoding="utf-8")
    assert "sets no POSTGRES_PASSWORD" in describe({}, env_file, example)
    env_file.write_text(f"POSTGRES_PASSWORD={pg_defaults.EXAMPLE_PASSWORD}\n",
                        encoding="utf-8")
    assert "example POSTGRES_PASSWORD" in describe({}, env_file, example)
    env_file.write_text("POSTGRES_PASSWORD=" + SECRET + "\n", encoding="utf-8")
    described = describe({}, env_file, example)
    assert described == "POSTGRES_PASSWORD from ops/.env"
    assert SECRET not in described
    # The explicit override wins whatever the file says, and is named, not shown.
    described = describe({"PSEUDOLIFE_TEST_PG_PASSWORD": SECRET}, tmp_path / "absent",
                         example)
    assert described == "PSEUDOLIFE_TEST_PG_PASSWORD"
    assert SECRET not in described
    # Compose expansion is refused by the parser; the description carries the
    # parser's value-free diagnosis rather than raising out of pytest_configure.
    env_file.write_text("POSTGRES_PASSWORD=$UNSUPPORTED_SECRET_VALUE\n", encoding="utf-8")
    described = describe({}, env_file, example)
    assert "POSTGRES_PASSWORD cannot be parsed" in described
    assert "Compose variable expansion" in described
    assert "UNSUPPORTED_SECRET_VALUE" not in described


def test_the_example_password_pin_matches_the_example_file():
    text = (ROOT / "ops" / ".env.example").read_text(encoding="utf-8")
    assert f"#POSTGRES_PASSWORD={pg_defaults.EXAMPLE_PASSWORD}" in text


def test_probe_classifies_the_dev_server_answer(monkeypatch):
    def connect_raising(exc):
        def connect(*args, **kwargs):
            raise exc
        return connect

    monkeypatch.setattr(psycopg, "connect", connect_raising(
        psycopg.OperationalError("FATAL:  password authentication failed for user")))
    assert pg_defaults.probe_dev_server({}, Path("absent")) == "auth"
    monkeypatch.setattr(psycopg, "connect", connect_raising(
        ConnectionRefusedError("connection refused")))
    assert pg_defaults.probe_dev_server({}, Path("absent")) == "absent"
    monkeypatch.setattr(psycopg, "connect", connect_raising(
        psycopg.OperationalError("FATAL:  too many connections")))
    assert pg_defaults.probe_dev_server({}, Path("absent")) == "other"

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Conn())
    assert pg_defaults.probe_dev_server({}, Path("absent")) == "ok"


@pytest.mark.parametrize("answer", ["ok", "absent", "other"])
def test_preflight_lets_a_full_run_through_unless_the_server_rejects_it(tmp_path, answer):
    out = io.StringIO()
    assert pg_defaults.full_run_password_preflight(
        "full", {}, tmp_path / "absent", probe=lambda env, env_file, **kwargs: answer,
        out=out) is None
    assert out.getvalue() == ""


def test_preflight_refuses_a_full_run_the_server_rejects_and_names_the_fix(tmp_path):
    example = _example_env(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_bytes(example.read_bytes())
    probed: list[tuple] = []

    def probe(env, path, **kwargs):
        probed.append((env, path))
        return "auth"

    out = io.StringIO()
    refusal = pg_defaults.full_run_password_preflight(
        "full", {}, env_file, example_file=example, probe=probe, out=out)
    assert refusal is not None
    assert refusal.startswith("refusing the full suite")
    assert "copy of ops/.env.example" in refusal
    assert "copy ops/.env from the main checkout" in refusal
    assert "PSEUDOLIFE_TEST_PG_PASSWORD" in refusal
    assert out.getvalue() == ""  # the refusal is raised by conftest, not printed
    assert probed == [({}, env_file)]


def test_preflight_gives_a_targeted_run_one_line_and_lets_it_run(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=" + SECRET + "\n", encoding="utf-8")
    out = io.StringIO()
    assert pg_defaults.full_run_password_preflight(
        "targeted", {}, env_file, probe=lambda env, path, **kwargs: "auth",
        out=out) is None
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, lines
    assert "POSTGRES_PASSWORD from ops/.env" in lines[0]
    assert "PSEUDOLIFE_TEST_PG_PASSWORD" in lines[0]
    assert "refusing" not in lines[0]
    assert SECRET not in lines[0]


@pytest.mark.parametrize(("kind", "env"), [
    ("worker", {}),
    ("off", {}),
    # An explicit test DSN is used verbatim (CI's form): ops/.env is never
    # read for it, so there is no password to check.
    ("full", {"PSEUDOLIFE_TEST_DATABASE_URL": "dbname=pseudolife_memory_test host=x"}),
    ("targeted", {"PSEUDOLIFE_TEST_DATABASE_URL": "dbname=pseudolife_memory_test host=x"}),
])
def test_preflight_never_probes_outside_a_gated_local_run(tmp_path, kind, env):
    def probe(*args):
        raise AssertionError("probed")

    out = io.StringIO()
    assert pg_defaults.full_run_password_preflight(
        kind, env, tmp_path / "absent", probe=probe, out=out) is None
    assert out.getvalue() == ""


def test_probe_never_raises_on_an_env_file_the_parser_refuses(tmp_path, monkeypatch):
    # Reached with PSEUDOLIFE_BENCH_ADMIN_URL exported, when conftest's own
    # import-time resolve is skipped: a raise here would escape
    # pytest_configure as an INTERNALERROR whose --fulltrace frames list
    # os.environ.
    def connect(*args, **kwargs):
        raise AssertionError("no connect is attempted without a URL")

    monkeypatch.setattr(psycopg, "connect", connect)
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=$UNSUPPORTED_SECRET_VALUE\n", encoding="utf-8")
    assert pg_defaults.probe_dev_server({}, env_file) == "other"


def test_a_quick_probe_finds_no_listener_without_waiting_on_libpq(monkeypatch):
    # A targeted run on a machine without the dev server must not pay
    # libpq's connect timeout (3.09 s measured to a closed loopback port on
    # the maintainer's Windows host, 2026-09-28) on every invocation.
    import socket

    def connect(*args, **kwargs):
        raise AssertionError("libpq is not tried when nothing listens")

    def create_connection(address, timeout=None):
        assert timeout is not None and timeout <= 0.5
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    assert pg_defaults.probe_dev_server({}, Path("absent"), quick=True) == "absent"


def test_a_targeted_run_probes_quickly_and_a_full_run_does_not(tmp_path):
    calls: list[dict] = []

    def probe(env, path, **kwargs):
        calls.append(kwargs)
        return "absent"

    pg_defaults.full_run_password_preflight("targeted", {}, tmp_path / "absent",
                                            probe=probe, out=io.StringIO())
    pg_defaults.full_run_password_preflight("full", {}, tmp_path / "absent",
                                            probe=probe, out=io.StringIO())
    assert calls == [{"quick": True}, {"quick": False}]


def test_a_rejected_override_is_named_as_the_thing_to_fix(tmp_path):
    refusal = pg_defaults.full_run_password_preflight(
        "full", {"PSEUDOLIFE_TEST_PG_PASSWORD": SECRET}, tmp_path / "absent",
        probe=lambda env, path, **kwargs: "auth", out=io.StringIO())
    assert "correct or unset PSEUDOLIFE_TEST_PG_PASSWORD" in refusal
    assert "copy ops/.env" not in refusal  # the override wins over the file
    assert SECRET not in refusal


# -- a dispatched run refuses the live bank's server by identity ----------------
#
# On the maintainer's homelab box the live bank's Postgres answers on host port
# 5433 and also on its container's Docker-network address (container port
# 5432). The launchers refuse a PSEUDOLIFE_TEST_PG_HOST_PORT on 5433, but every
# connection the suite makes comes from three settings (that one, an explicit
# PSEUDOLIFE_TEST_DATABASE_URL and PSEUDOLIFE_BENCH_ADMIN_URL), and a network
# address in any of them passed. So a dispatched run asks each server it would
# use which databases it holds, and refuses one that holds a production bank.

_DISPATCHED = {"PSEUDOLIFE_SUITE_DISPATCHED": "1", "PSEUDOLIFE_TEST_PG_PASSWORD": "s3cret"}
_BRIDGE_URL = "postgresql://pseudolife:s3cret@10.0.0.7:5432/pseudolife_memory_test_1"
_BRIDGE_ADMIN = "postgresql://pseudolife:s3cret@10.0.0.7:5432/postgres"


class _CatalogServer:
    """A fake psycopg.connect: each host:port answers with its database list,
    or raises; a ``host:port/dbname`` key answers for that database alone.
    Records every address it was asked for."""

    def __init__(self, servers: dict[str, object]):
        self.servers = servers
        self.asked: list[str] = []
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(str(url))
        address = f"{parts['host']}:{parts['port']}"
        self.asked.append(address)
        self.urls.append(str(url))
        answer = self.servers.get(f"{address}/{parts.get('dbname')}", self.servers.get(
            address, ["postgres", "template0", "template1"]))
        if isinstance(answer, BaseException):
            raise answer
        names = answer

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *args):
                assert "pg_database" in sql
                return type("_Cur", (), {"fetchall": lambda _s: [(n,) for n in names]})()

        return _Conn()


def test_an_undispatched_run_never_asks_the_servers():
    server = _CatalogServer({})
    assert pg_defaults.dispatched_live_bank_refusal(
        {"PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}, Path("absent"), connect=server) is None
    assert server.asked == []


def test_a_dispatched_run_asks_every_distinct_server_and_passes_test_servers():
    server = _CatalogServer({})
    # The bench URL names a database that need not exist yet; it is asked
    # through ``postgres``, as its consumers connect.
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL":
           "postgresql://pseudolife:s3cret@10.0.0.8:5434/fixed",
           "PSEUDOLIFE_BENCH_ADMIN_URL": "postgresql://pseudolife:s3cret@10.0.0.9:5434/notyet"}
    assert pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server) is None
    assert sorted(server.asked) == sorted(
        [pg_defaults.DEV_HOST_PORT, "10.0.0.8:5434", "10.0.0.9:5434"])
    # The test URL through its own database, the others through postgres.
    assert sorted(url.rpartition("/")[2] for url in server.urls) == [
        "fixed", "postgres", "postgres"]


def test_a_dispatched_run_asks_a_server_named_twice_once():
    # Without a bench URL, the bench admin URL is the test URL's server.
    server = _CatalogServer({})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL":
           "postgresql://pseudolife:s3cret@10.0.0.8:5434/fixed"}
    assert pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server) is None
    assert sorted(server.asked) == sorted([pg_defaults.DEV_HOST_PORT, "10.0.0.8:5434"])


@pytest.mark.parametrize("setting", ["PSEUDOLIFE_TEST_DATABASE_URL", "PSEUDOLIFE_BENCH_ADMIN_URL"])
def test_a_dispatched_run_refuses_the_live_bank_on_its_network_address(setting):
    # Port 5432, not 5433: the launchers' port check lets this through.
    server = _CatalogServer({"10.0.0.7:5432": ["postgres", "pseudolife_memory", "template1"]})
    env = {**_DISPATCHED, setting: _BRIDGE_URL if setting.endswith("DATABASE_URL") else _BRIDGE_ADMIN}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None
    assert "10.0.0.7:5432" in refusal and "pseudolife_memory" in refusal
    assert setting in refusal
    assert "s3cret" not in refusal


def test_a_dispatched_run_refuses_the_live_bank_on_the_default_address():
    server = _CatalogServer({pg_defaults.DEV_HOST_PORT: ["postgres", "pseudolife_memory"]})
    refusal = pg_defaults.dispatched_live_bank_refusal(_DISPATCHED, Path("absent"), connect=server)
    assert refusal is not None and pg_defaults.DEV_HOST_PORT in refusal


def test_a_dispatched_run_refuses_the_bank_the_daemon_dsn_named(monkeypatch):
    # conftest records the exported daemon DSN's database as a production bank.
    from pseudolife_memory.storage.schema import PRODUCTION_DATABASE_ENV

    monkeypatch.setenv(PRODUCTION_DATABASE_ENV, "renamed_bank")
    server = _CatalogServer({"10.0.0.7:5432": ["postgres", "renamed_bank"]})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None and "renamed_bank" in refusal


def test_a_dispatched_run_skips_servers_that_cannot_be_used_at_all():
    # Nothing answering, or a server refusing the login: the suite cannot
    # write there either, and its own fixtures report it.
    server = _CatalogServer({
        pg_defaults.DEV_HOST_PORT: ConnectionRefusedError("connection refused"),
        "10.0.0.7:5432": psycopg.OperationalError(
            "FATAL:  password authentication failed for user")})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    assert pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server) is None


@pytest.mark.parametrize("failure", [
    psycopg.OperationalError("FATAL:  too many connections for role"),
    # A slow server is still a server: later connects wait longer.
    psycopg.OperationalError("connection failed: timeout expired"),
    TimeoutError("timed out"),
])
def test_a_dispatched_run_refuses_a_server_it_could_not_check(failure):
    server = _CatalogServer({"10.0.0.7:5432": failure})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None and "10.0.0.7:5432" in refusal
    assert "s3cret" not in refusal


def test_conftest_stops_a_dispatched_run_it_could_not_clear():
    # The wiring, with no server reached: the default address is a closed
    # port and the explicit URL is rejected by libpq before any network I/O.
    import subprocess
    import sys

    root = str(pg_defaults.ENV_FILE.parent.parent)
    url = "postgresql://pseudolife:s3cret@127.0.0.1:9/fixed?sslmode=bogus"
    # PYTEST_*: under CI's xdist the child would inherit a worker id and
    # skip the check. PSEUDOLIFE_BENCH_DB: a pinned name keeps the exit-time
    # bench drop off any real database.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PSEUDOLIFE_TEST_", "PSEUDOLIFE_BENCH_", "_PSEUDOLIFE_BENCH_",
                                "PYTEST_"))}
    env.update(PSEUDOLIFE_SUITE_DISPATCHED="1", PSEUDOLIFE_TEST_PG_HOST_PORT="127.0.0.1:9",
               PSEUDOLIFE_TEST_DATABASE_URL=url, PSEUDOLIFE_SUITE_LOCK="off",
               PSEUDOLIFE_BENCH_DB="pseudolife_memory_bench_wiring_test")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider",
         "tests/test_pg_defaults.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 4, run.stdout[-2000:] + run.stderr[-2000:]
    assert "refusing a dispatched run" in run.stderr + run.stdout
    assert "s3cret" not in run.stderr + run.stdout


def test_a_dispatched_run_refuses_a_url_naming_several_hosts():
    # libpq would clear the first host to answer and might use another later.
    server = _CatalogServer({})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL":
           "postgresql://pseudolife:s3cret@10.0.0.8:5434,10.0.0.7:5432/fixed"}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None and "several hosts" in refusal
    assert "s3cret" not in refusal


def test_a_dispatched_run_refuses_a_url_that_does_not_parse():
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": "postgresql://s3cret@[broken",
           "PSEUDOLIFE_BENCH_ADMIN_URL": "postgresql://pseudolife:s3cret@10.0.0.9:5434/postgres"}
    refusal = pg_defaults.dispatched_live_bank_refusal(
        env, Path("absent"), connect=_CatalogServer({}))
    assert refusal is not None and "does not parse" in refusal
    assert "s3cret" not in refusal


# -- hardening after #532's review (2026-10-03) ----------------------------------

def test_a_dispatched_run_asks_through_the_test_urls_own_database():
    # pg_hba may let the role into its test database and not into postgres;
    # the tests and daemons connect to the test database, so ask there.
    server = _CatalogServer({
        "10.0.0.7:5432/postgres": psycopg.OperationalError(
            'FATAL:  no pg_hba.conf entry for host "10.0.0.2", database "postgres"'),
        "10.0.0.7:5432": ["postgres", "pseudolife_memory", "pseudolife_memory_test_1"]})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None and "holds the production bank" in refusal


def test_a_dispatched_run_falls_back_to_postgres_when_the_test_database_is_new():
    # The explicit URL's database need not exist yet (pg_url provisions it).
    server = _CatalogServer({
        "10.0.0.7:5432/pseudolife_memory_test_1": psycopg.OperationalError(
            'FATAL:  database "pseudolife_memory_test_1" does not exist'),
        "10.0.0.7:5432": ["postgres", "pseudolife_memory"]})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None and "holds the production bank" in refusal
    assert server.urls[-1].endswith("/postgres")


def test_a_dispatched_run_asks_an_absent_server_only_once():
    # Nothing answered at all: another database on it cannot answer either,
    # and each try may cost a connect timeout.
    server = _CatalogServer({"10.0.0.7:5432": ConnectionRefusedError("connection refused")})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    assert pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server) is None
    assert server.asked.count("10.0.0.7:5432") == 1


def test_a_dispatched_run_refuses_a_server_with_no_database_to_ask():
    # Neither the test database nor postgres exists: nothing was cleared.
    missing = psycopg.OperationalError('FATAL:  database "x" does not exist')
    server = _CatalogServer({"10.0.0.7:5432": missing})
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": _BRIDGE_URL}
    refusal = pg_defaults.dispatched_live_bank_refusal(env, Path("absent"), connect=server)
    assert refusal is not None and "could not check" in refusal
    server = _CatalogServer({pg_defaults.DEV_HOST_PORT: missing})
    refusal = pg_defaults.dispatched_live_bank_refusal(_DISPATCHED, Path("absent"), connect=server)
    assert refusal is not None and "could not check" in refusal


@pytest.mark.parametrize(("url", "extra", "expected"), [
    ("postgresql://pseudolife:s3cret@10.0.0.8:5434/fixed?service=suite", {},
     "PSEUDOLIFE_TEST_DATABASE_URL"),
    ("dbname=fixed user=pseudolife password=s3cret", {"PGSERVICE": "suite"}, "PGSERVICE"),
    ("dbname=fixed user=pseudolife password=s3cret", {"PGHOST": "10.0.0.8,10.0.0.7"},
     "PGHOST"),
    ("dbname=fixed user=pseudolife password=s3cret port=5434",
     {"PGHOSTADDR": "10.0.0.8,10.0.0.7"}, "PGHOSTADDR"),
])
def test_a_dispatched_run_refuses_hosts_it_cannot_see_in_the_url(url, extra, expected):
    # libpq reads a service file, or a host list from PGHOST, only at connect
    # time: the URL alone does not show which servers it may reach. The
    # refusal names the setting that has to change.
    env = {**_DISPATCHED, "PSEUDOLIFE_TEST_DATABASE_URL": url, **extra}
    refusal = pg_defaults.dispatched_live_bank_refusal(
        env, Path("absent"), connect=_CatalogServer({}))
    assert refusal is not None
    assert ("service file" if "SERVICE" in expected or "service=" in url
            else "several hosts") in refusal
    assert expected in refusal
    assert "s3cret" not in refusal


def test_conftest_checks_again_once_it_holds_the_suite_lock(tmp_path):
    # A server that refused the first check (a container restarting) may be
    # up by the time a queued run gets the lock. A refusal there still
    # releases the lock, without timing the next run's expected end.
    import subprocess
    import sys

    marker = tmp_path / "released"
    plugin = [
        'from tests import pg_defaults, suite_lock',
        "state = {'locked': False}",
        'def take(*a, **k):',
        "    state['locked'] = True",
        "    return 'held'",
        'suite_lock.take_for_session = take',
        'def release(held, record):',
        f'    open({str(marker)!r}, "w").write(f"{{held}} {{record}}")',
        'suite_lock.release = release',
        'def refusal(*a, **k):',
        "    return 'refusing a dispatched run: late' if state['locked'] else None",
        'pg_defaults.dispatched_live_bank_refusal = refusal',
    ]
    (tmp_path / "late_live_bank.py").write_text(
        "".join(f"{line}{chr(10)}" for line in plugin), encoding="utf-8")
    root = str(pg_defaults.ENV_FILE.parent.parent)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PSEUDOLIFE_TEST_", "PSEUDOLIFE_BENCH_", "_PSEUDOLIFE_BENCH_",
                                "PYTEST_"))}
    env.update(PSEUDOLIFE_SUITE_DISPATCHED="1", PSEUDOLIFE_TEST_PG_HOST_PORT="127.0.0.1:9",
               PSEUDOLIFE_SUITE_LOCK="off",
               PSEUDOLIFE_BENCH_DB="pseudolife_memory_bench_wiring_test",
               PYTHONPATH=os.pathsep.join([root, str(tmp_path)]))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider",
         "-p", "late_live_bank", "tests/test_pg_defaults.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 4, run.stdout[-2000:] + run.stderr[-2000:]
    assert "refusing a dispatched run: late" in run.stderr + run.stdout
    assert marker.read_text(encoding="utf-8") == "held False"
