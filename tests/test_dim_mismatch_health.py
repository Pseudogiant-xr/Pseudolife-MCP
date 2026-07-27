"""Schema v25 dim-mismatch refusal must be visible at /health (2026-07-28
review, IMPORTANT 3).

The refusal (``schema.py::_refuse_on_embedding_dim_mismatch``) fires lazily
inside ``MemoryService._ensure_init`` on the first tool call — before that,
``/health`` had no way to know every memory tool was about to fail, and kept
reporting ``status: ok``. These tests pin two things independently, without
needing a real mismatched Postgres bank:

* ``MemoryService._ensure_init`` records the refusal message on
  ``self._init_refusal`` and still re-raises (a tool call must keep failing
  loudly, not silently degrade).
* ``daemon._build_health_payload`` turns a recorded refusal into
  ``status: "degraded"`` + an ``init_refusal`` field, without needing a
  running uvicorn/ASGI stack.
"""
from __future__ import annotations

import pytest


class _FakeEmbedder:
    embedding_dim = 1024


class _RefusingStorage:
    """Stands in for PostgresStorage's constructor raising schema.py's
    dim-mismatch RuntimeError (the real message names the migration
    script; only the shape matters for this test)."""

    MESSAGE = (
        "Refusing to start: entries.embedding is vector(384) but this "
        "build's schema expects vector(1024). ensure_schema is "
        "additive-only... run `python ops/migrate_embeddings.py`."
    )

    def __init__(self, _url: str) -> None:
        raise RuntimeError(self.MESSAGE)


def test_ensure_init_records_refusal_and_still_reraises(tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService

    monkeypatch.setattr(
        "pseudolife_memory.service.EmbeddingPipeline",
        lambda config: _FakeEmbedder(),
    )
    monkeypatch.setattr(
        "pseudolife_memory.storage.postgres.PostgresStorage", _RefusingStorage,
    )

    svc = MemoryService(data_dir=tmp_path, database_url="postgresql://fake/db")
    assert svc._init_refusal is None  # nothing has run yet — lazy init

    with pytest.raises(RuntimeError, match="vector.1024."):
        svc._ensure_init()  # noqa: SLF001 — exercising the guarded path directly

    assert svc._init_refusal == _RefusingStorage.MESSAGE
    assert svc._cms is None  # init did not complete


def test_health_reports_init_refusal_as_degraded():
    from pseudolife_memory.daemon import _build_health_payload

    class _Stub:
        _db_url = "postgresql://fake"
        _persist_errors = 0
        _init_refusal = _RefusingStorage.MESSAGE
        _storage = None

    payload = _build_health_payload(_Stub(), token_present=False)
    assert payload["status"] == "degraded"
    assert payload["init_refusal"] == _RefusingStorage.MESSAGE


def test_health_stays_ok_without_a_refusal():
    from pseudolife_memory.daemon import _build_health_payload

    class _Stub:
        _db_url = None
        _persist_errors = 0
        _init_refusal = None
        _storage = None

    payload = _build_health_payload(_Stub(), token_present=False)
    assert payload["status"] == "ok"
    assert "init_refusal" not in payload
