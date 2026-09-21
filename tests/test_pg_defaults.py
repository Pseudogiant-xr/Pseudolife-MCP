"""The suite finds the dev Postgres password where compose does, and a
reachable server that rejects it errors the PG-backed tests instead of
skipping them (2026-09-20: ~1000 silent skips after the 09-14 rotation)."""
from __future__ import annotations

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
        "postgresql://pseudolife:s3cret@127.0.0.1:5433/postgres")


def test_default_admin_url_percent_encodes_a_generated_password():
    """`@` would re-split the authority; `%` `/` `:` `#` `?` are misparsed."""
    url = pg_defaults.default_admin_url({"PSEUDOLIFE_TEST_PG_PASSWORD": "p@ss/w:rd#1%?"})
    assert url == "postgresql://pseudolife:p%40ss%2Fw%3Ard%231%25%3F@127.0.0.1:5433/postgres"
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(url)["password"] == "p@ss/w:rd#1%?"


def test_fixture_default_admin_follows_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=rotated\n", encoding="utf-8")
    monkeypatch.setattr(pg_defaults, "ENV_FILE", env_file)
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_TEST_PG_PASSWORD", raising=False)
    assert pg_fixtures._admin_url() == "postgresql://pseudolife:rotated@127.0.0.1:5433/postgres"
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
