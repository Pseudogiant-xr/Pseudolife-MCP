"""Transport absence cannot become a producer pass or hide a setup failure."""
from contextlib import contextmanager

import psycopg
import pytest

from evals.rust_port import test_sent_review_controls as controls


@pytest.mark.parametrize("explicit", [False, True])
def test_sent_pg_preflight_uses_the_disposable_admin(monkeypatch, explicit):
    from tests import pg_defaults
    calls = []
    default = "host=synthetic-default dbname=postgres"
    selected = "host=synthetic-owned dbname=postgres"
    monkeypatch.setattr(pg_defaults, "default_admin_url", lambda: default)
    monkeypatch.delenv("PSEUDOLIFE_BENCH_ADMIN_URL", raising=False)
    if explicit:
        monkeypatch.setenv("PSEUDOLIFE_BENCH_ADMIN_URL", selected)

    @contextmanager
    def connect(dsn, **options):
        calls.append((dsn, options))
        yield None

    monkeypatch.setattr(controls.psycopg, "connect", connect)
    controls.require_sent_postgres()
    assert calls == [(selected if explicit else default,
                      {"autocommit": True, "connect_timeout": 5})]


@pytest.mark.parametrize("message", ["Connection refused", "connection timeout expired"])
@pytest.mark.parametrize("required", [False, True])
def test_sent_pg_absence_skips_only_optional_coverage(monkeypatch, message, required):
    error = psycopg.OperationalError(message)
    monkeypatch.setenv("PSEUDOLIFE_BENCH_ADMIN_URL", "host=synthetic-owned dbname=postgres")
    monkeypatch.delenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", raising=False)
    if required:
        monkeypatch.setenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "1")

    def connect(*args, **kwargs):
        raise error

    monkeypatch.setattr(controls.psycopg, "connect", connect)
    if required:
        with pytest.raises(psycopg.OperationalError) as caught:
            controls.require_sent_postgres()
        assert caught.value is error
    else:
        with pytest.raises(pytest.skip.Exception, match="real-PG producer control not executed"):
            controls.require_sent_postgres()


@pytest.mark.parametrize("message", [
    "FATAL: password authentication failed",
    'FATAL: database "synthetic" does not exist',
    "unclassified connection failure",
])
def test_sent_pg_answering_or_unknown_failures_are_not_skips(monkeypatch, message):
    error = psycopg.OperationalError(message)
    monkeypatch.setenv("PSEUDOLIFE_BENCH_ADMIN_URL", "host=synthetic-owned dbname=postgres")
    monkeypatch.delenv("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", raising=False)

    def connect(*args, **kwargs):
        raise error

    monkeypatch.setattr(controls.psycopg, "connect", connect)
    with pytest.raises(psycopg.OperationalError) as caught:
        controls.require_sent_postgres()
    assert caught.value is error
