"""Boot-window memory balloon regression (2026-08-04 incident).

While Postgres was in crash-recovery after machine boot (~3 minutes), every
incoming API/MCP call retried ``MemoryService._ensure_init`` from scratch.
The embedder was constructed *before* the storage connect, so each retry
loaded a fresh ~2.4 GB model (Qwen3-Embedding-0.6B fp32 on CPU) and then
failed on the connect — 12 loads in the incident window ballooned the daemon
to a 31.5 GB cgroup peak and nearly OOMed the host. The dead copies sat in
torch module reference cycles awaiting a gen-2 GC that a quiet process
rarely triggers.

Two contracts pin the fix independently:

* Storage connects FIRST: a down database costs a fast connect error, never
  a model load (``test_db_down_retries_load_no_models``).
* An embedder built by a partially-successful attempt is reused by the next
  attempt, never rebuilt (``test_embedder_reused_across_failed_attempts``).
"""
from __future__ import annotations

import pytest

from pseudolife_memory.service import MemoryService


class _Boom(Exception):
    """Stands in for psycopg.OperationalError. Deliberately not a
    RuntimeError, which ``_ensure_init`` records as a schema refusal."""


class _FakeEmbedder:
    embedding_dim = 1024


class _DownStorage:
    """PostgresStorage whose connect fails, as during crash-recovery."""

    def __init__(self, _url: str) -> None:
        raise _Boom(
            "connection failed: FATAL: the database system is starting up",
        )


class _FakeCursor:
    def fetchone(self):
        return ("public",)


class _FakeConn:
    def execute(self, _sql: str) -> _FakeCursor:
        return _FakeCursor()


class _UpStorage:
    """Minimal stand-in for a *connected* PostgresStorage: just enough for
    the parts of ``_ensure_init`` that run outside a try/except. Everything
    else (hydration, migration) is reached only inside swallowing blocks."""

    def __init__(self, _url: str) -> None:
        self.conn = _FakeConn()

    def get_meta(self, _key: str):
        return None


def _count_embedder_builds(monkeypatch) -> list:
    built: list = []
    monkeypatch.setattr(
        "pseudolife_memory.service.EmbeddingPipeline",
        lambda config: built.append(config) or _FakeEmbedder(),
    )
    return built


def test_db_down_retries_load_no_models(tmp_path, monkeypatch):
    built = _count_embedder_builds(monkeypatch)
    monkeypatch.setattr(
        "pseudolife_memory.storage.postgres.PostgresStorage", _DownStorage,
    )

    svc = MemoryService(data_dir=tmp_path, database_url="postgresql://fake/db")
    for _ in range(3):
        with pytest.raises(_Boom):
            svc._ensure_init()  # noqa: SLF001 — exercising the guarded path

    assert built == []  # storage failed before any model load
    assert svc._cms is None  # init did not complete
    assert svc._init_refusal is None  # an outage is not a schema refusal


def test_embedder_reused_across_failed_attempts(tmp_path, monkeypatch):
    built = _count_embedder_builds(monkeypatch)
    monkeypatch.setattr(
        "pseudolife_memory.storage.postgres.PostgresStorage", _UpStorage,
    )
    from pseudolife_memory.memory import graph_store as graph_store_module
    monkeypatch.setattr(
        graph_store_module, "PostgresNetworkxGraphStore",
        lambda storage: object(),
    )

    # Fail mid-init AFTER the embedder is built but BEFORE ``_cms`` is
    # assigned (the reranker constructor sits between the two), then
    # succeed on the next attempt.
    from pseudolife_memory.service import CrossEncoderReranker
    attempts: list = []
    real_reranker = CrossEncoderReranker

    def _flaky_reranker(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise _Boom("mid-init failure after the embedder was built")
        return real_reranker(**kwargs)

    monkeypatch.setattr(
        "pseudolife_memory.service.CrossEncoderReranker", _flaky_reranker,
    )

    from pseudolife_memory.storage import sync as sync_module
    monkeypatch.setattr(sync_module, "hydrate_cms", lambda cms, storage: 0)

    svc = MemoryService(data_dir=tmp_path, database_url="postgresql://fake/db")
    with pytest.raises(_Boom):
        svc._ensure_init()  # noqa: SLF001
    assert len(built) == 1

    svc._ensure_init()  # noqa: SLF001 — second attempt completes
    assert svc._cms is not None
    assert len(built) == 1  # the embedder was reused, not rebuilt
