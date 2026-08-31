"""The embedder-free storage-connect seam (``_ensure_postgres_storage``).

Pins the seam's two contracts, without needing a real Postgres bank:

* the connection is REUSED — a second call (or a mid-init retry) returns
  the same object instead of rebuilding it (2026-08-04 boot-balloon
  lineage: retries must stay cheap);
* the ``public``-search-path shadow-schema invariant runs on every
  connect, and a connect whose invariant check FAILS leaves no reusable
  connection behind — otherwise the retry would skip the check via the
  reuse guard and serve against the exact configuration the guard
  refuses.
"""
from __future__ import annotations

import pytest


class _FakeStorage:
    instances: list["_FakeStorage"] = []

    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        _FakeStorage.instances.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService

    _FakeStorage.instances = []
    monkeypatch.setattr(
        "pseudolife_memory.storage.postgres.PostgresStorage", _FakeStorage)
    service = MemoryService(
        data_dir=tmp_path, database_url="postgresql://fake/db")
    return service


def test_connect_is_reused_not_rebuilt(svc, monkeypatch):
    monkeypatch.setattr(svc, "_assert_public_search_path", lambda: None)

    first = svc._ensure_postgres_storage()  # noqa: SLF001
    second = svc._ensure_postgres_storage()  # noqa: SLF001

    assert first is second
    assert len(_FakeStorage.instances) == 1


def test_failed_search_path_invariant_leaves_no_reusable_connection(
        svc, monkeypatch):
    calls = {"n": 0}

    def failing_assert():
        calls["n"] += 1
        raise RuntimeError("search_path shadows the real bank")

    monkeypatch.setattr(svc, "_assert_public_search_path", failing_assert)

    with pytest.raises(RuntimeError, match="shadows"):
        svc._ensure_postgres_storage()  # noqa: SLF001

    # The failed connect must not satisfy the reuse guard: the bad
    # connection is closed and dropped, so the next attempt reconnects
    # and re-runs the invariant instead of silently serving.
    assert svc._storage is None  # noqa: SLF001
    assert _FakeStorage.instances[0].closed is True

    monkeypatch.setattr(svc, "_assert_public_search_path", lambda: None)
    retried = svc._ensure_postgres_storage()  # noqa: SLF001

    assert calls["n"] == 1
    assert retried is _FakeStorage.instances[1]
    assert len(_FakeStorage.instances) == 2
