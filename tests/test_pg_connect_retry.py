"""The daemon's own Postgres connects retry a local-port failure, and only that.

On Windows a loopback connect() can fail with WSAEADDRINUSE (10048) when the
local port it picked still has a TIME_WAIT entry to the same server, or with
WSAENOBUFS (10055) when the ephemeral range is exhausted. Neither says
anything about the server, and a second connect picks another port. Every
other failure (refused, timeout, any answer from the server) must surface on
the first call. No server is needed: ``psycopg.connect`` is stubbed.
"""
from __future__ import annotations

import logging

import psycopg
import pytest

from pseudolife_memory.storage import postgres as pg
from pseudolife_memory.storage.coordination import CoordinationConnection
from pseudolife_memory.storage.postgres import PostgresStorage

PASSWORD = "s3cret-pw-do-not-log"
DSN = f"postgresql://pseudolife:{PASSWORD}@127.0.0.1:5433/pseudolife_bank"

ADDR_IN_USE = psycopg.OperationalError(
    'connection failed: connection to server at "127.0.0.1", port 5433 '
    "failed: Address already in use (0x00002740/10048)\n"
    "\tIs the server running on that host and accepting TCP/IP connections?")
NO_BUFS = psycopg.OperationalError(
    'connection failed: connection to server at "127.0.0.1", port 5433 '
    "failed: No buffer space available (0x00002747/10055)\n"
    "\tIs the server running on that host and accepting TCP/IP connections?")
REFUSED = psycopg.OperationalError(
    'connection failed: connection to server at "127.0.0.1", port 5433 '
    "failed: Connection refused (0x0000274D/10061)\n"
    "\tIs the server running on that host and accepting TCP/IP connections?")
TIMEOUT = psycopg.OperationalError("connection failed: timeout expired")
BAD_PASSWORD = psycopg.errors.InvalidPassword(
    'connection failed: FATAL:  password authentication failed for user '
    '"pseudolife"')
# What a real connect-time rejection from the server looks like: psycopg
# builds it from libpq's message alone, so it carries no SQLSTATE and only
# its text keeps it from being retried.
AUTH_REJECTED = psycopg.OperationalError(
    'connection failed: connection to server at "127.0.0.1", port 5433 '
    'failed: FATAL:  password authentication failed for user "pseudolife"')
TOO_MANY = psycopg.OperationalError(
    'connection failed: connection to server at "127.0.0.1", port 5433 '
    "failed: FATAL:  sorry, too many clients already")


class FakeConnection:
    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedConnect:
    """Stands in for ``psycopg.connect``: raises the scripted errors in
    order, then returns a fake connection; records every call's arguments."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        return FakeConnection()


@pytest.fixture
def sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(pg.time, "sleep", slept.append)
    return slept


def _storage() -> PostgresStorage:
    storage = PostgresStorage.__new__(PostgresStorage)
    storage.dsn = DSN
    storage._lease_lost = None
    storage.resident_invalidated = None
    return storage


def _coordination() -> CoordinationConnection:
    coordination = CoordinationConnection.__new__(CoordinationConnection)
    coordination.dsn = DSN
    coordination._in_txn = False
    return coordination


SITES = {
    "storage_connect": lambda: _storage()._connect(),
    "storage_ping": lambda: _storage().ping(),
    "coordination_connect": lambda: _coordination()._connect(),
}


@pytest.mark.parametrize("site", sorted(SITES))
def test_site_succeeds_after_two_local_port_failures(site, monkeypatch, sleeps):
    connect = ScriptedConnect([ADDR_IN_USE, NO_BUFS])
    monkeypatch.setattr(psycopg, "connect", connect)

    result = SITES[site]()

    assert result
    assert len(connect.calls) == 3
    assert connect.calls[0][0] == (DSN,)
    assert all(call == connect.calls[0] for call in connect.calls)
    assert len(sleeps) == 2


def test_connect_arguments_are_unchanged_at_each_site(monkeypatch, sleeps):
    connect = ScriptedConnect([])
    monkeypatch.setattr(psycopg, "connect", connect)
    for site in sorted(SITES):
        SITES[site]()
    kwargs = [call[1] for call in connect.calls]
    assert kwargs == [
        {"connect_timeout": 10, "autocommit": True},
        {"connect_timeout": 10, "autocommit": True,
         "fallback_application_name": pg._application_name()},
        {"connect_timeout": 2},
    ]


@pytest.mark.parametrize("site", sorted(SITES))
def test_persistent_local_port_failure_raises_after_bounded_attempts(
        site, monkeypatch, sleeps):
    errors = [psycopg.OperationalError(str(ADDR_IN_USE))
              for _ in range(pg._CONNECT_ATTEMPTS + 5)]
    last = errors[pg._CONNECT_ATTEMPTS - 1]
    connect = ScriptedConnect(errors)
    monkeypatch.setattr(psycopg, "connect", connect)

    with pytest.raises(psycopg.OperationalError) as raised:
        SITES[site]()

    assert raised.value is last
    assert len(connect.calls) == pg._CONNECT_ATTEMPTS
    assert len(sleeps) == pg._CONNECT_ATTEMPTS - 1


def test_backoff_is_short_exponential_and_jittered(monkeypatch, sleeps):
    connect = ScriptedConnect([ADDR_IN_USE] * (pg._CONNECT_ATTEMPTS - 1))
    monkeypatch.setattr(psycopg, "connect", connect)

    pg.connect_retrying_local_ports(DSN)

    assert len(sleeps) == pg._CONNECT_ATTEMPTS - 1
    for attempt, slept in enumerate(sleeps):
        nominal = pg._CONNECT_BACKOFF_SECONDS * 2 ** attempt
        assert 0.5 * nominal <= slept <= 1.5 * nominal
    assert sum(sleeps) < 1.0


@pytest.mark.parametrize("error", [
    REFUSED,
    TIMEOUT,
    BAD_PASSWORD,
    AUTH_REJECTED,
    TOO_MANY,
    # An answer from the server is never retried, whatever its text says.
    psycopg.errors.InvalidPassword("odd text (0x00002740/10048)"),
    # Only a connect failure qualifies, not a bad conninfo.
    psycopg.ProgrammingError("invalid dsn (0x00002740/10048)"),
], ids=["refused", "timeout", "invalid-password", "auth-rejected",
        "too-many-clients", "sqlstate-with-code", "not-operational"])
@pytest.mark.parametrize("site", sorted(SITES))
def test_other_failures_raise_on_the_first_call(site, error, monkeypatch,
                                               sleeps):
    connect = ScriptedConnect([error, ADDR_IN_USE])
    monkeypatch.setattr(psycopg, "connect", connect)

    with pytest.raises(type(error)) as raised:
        SITES[site]()

    assert raised.value is error
    assert len(connect.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("site", sorted(SITES))
def test_each_retry_logs_one_warning_with_the_code_only(site, monkeypatch,
                                                        sleeps, caplog):
    connect = ScriptedConnect([ADDR_IN_USE, NO_BUFS])
    monkeypatch.setattr(psycopg, "connect", connect)
    caplog.set_level(logging.WARNING, logger=pg.logger.name)

    SITES[site]()

    warnings = [r for r in caplog.records
                if r.name == pg.logger.name and r.levelno == logging.WARNING]
    assert len(warnings) == 2
    first, second = (r.getMessage() for r in warnings)
    assert "10048" in first and "1 of" in first
    assert "10055" in second and "2 of" in second
    for text in (first, second):
        assert PASSWORD not in text
        assert "127.0.0.1" not in text
        assert "Is the server running" not in text
