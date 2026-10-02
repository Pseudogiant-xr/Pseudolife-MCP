"""The daemon installs the stored-principal snapshot at startup (spec
2026-10-02, "The daemon's view: an in-memory snapshot"): only with
Postgres, shadowed by the environment's principals, refreshed by its own
thread from the daemon's DSN, never holding the service."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pseudolife_memory import daemon, principal_store
from pseudolife_memory.principals import installed_store, install_store


@pytest.fixture(autouse=True)
def _restore_store():
    previous = installed_store()
    yield
    install_store(previous)


@pytest.fixture
def started(monkeypatch):
    calls = []

    def start(self):
        calls.append(self)
        return None

    monkeypatch.setattr(principal_store.PrincipalRefresher, "start", start)
    return calls


def test_a_postgres_daemon_installs_a_shadowed_snapshot_and_starts_its_refresh(started):
    svc = SimpleNamespace(_db_url="postgresql://fixture@127.0.0.1:5433/fixture")
    snapshot = daemon.start_stored_principals(svc, {"tok": "desk"}, auth_configured=True)
    assert installed_store() is snapshot
    assert len(started) == 1
    refresher = started[0]
    assert refresher._dsn == svc._db_url and refresher._snapshot is snapshot
    # The refresher is handed the DSN, never the service or its locks.
    assert not any(value is svc for value in vars(refresher).values())
    snapshot.refresh(lambda: ([principal_store.StoredPrincipal("desk", "a" * 64, None, True, False),
                               principal_store.StoredPrincipal("laptop", "b" * 64, None, True, False)],
                              None))
    assert snapshot.shadowed_rows == ["desk"] and snapshot.has("laptop")


def test_a_daemon_without_postgres_has_no_store(started):
    install_store(None)
    assert daemon.start_stored_principals(SimpleNamespace(_db_url=None), {}, auth_configured=True) is None
    assert installed_store() is None and started == []
