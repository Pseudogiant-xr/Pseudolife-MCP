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


# ── File-mode hydration guard (2026-08-29 incident) ──────────────────────────
#
# The schema.py refusal above only protects Postgres banks (pgvector column
# dimensions). A v0.1 FILE-mode bank hydrates its .pt entries unchecked, so a
# daemon booted against a bank embedded under an older model (the retired
# 384-d MiniLM-era bank the shim-spawned fallback served in the incident)
# crashed on every search/store with a bare torch shape error —
# "size mismatch, mat (12x384), vec (1024)" — instead of a diagnosis.


def _save_file_bank(data_dir, dim: int) -> None:
    """A minimal v0.1 file-mode bank whose one entry is embedded at ``dim``."""
    import torch

    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.titans_memory import MemoryEntry
    from pseudolife_memory.utils.config import MemoryConfig

    cfg = MemoryConfig()
    cfg.embedding_dim = dim
    cms = ContinuumMemorySystem(cfg)
    entry = MemoryEntry(
        text="pre-cutover era entry",
        embedding=torch.zeros(dim),
        bank=cms.bands[0].name,
    )
    cms.bands[0].entries.append(entry)
    cms.bands[0]._dirty = True  # noqa: SLF001 — direct append, mark for save
    save_dir = data_dir / "memory_state"
    save_dir.mkdir(parents=True, exist_ok=True)
    cms.save(save_dir)


def test_file_mode_boot_refuses_a_stale_dim_bank(tmp_path, monkeypatch):
    """A hydrated entry whose embedding dim doesn't match the live embedder
    must refuse at boot — naming the bank location and both dimensions — not
    crash cryptically on every later query."""
    from pseudolife_memory.service import MemoryService

    _save_file_bank(tmp_path, dim=384)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "pseudolife_memory.service.EmbeddingPipeline",
        lambda config: _FakeEmbedder(),
    )

    svc = MemoryService(data_dir=tmp_path)  # no DSN → v0.1 file mode
    with pytest.raises(RuntimeError) as exc:
        svc._ensure_init()  # noqa: SLF001 — exercising the guarded path
    msg = str(exc.value)
    assert "384" in msg and "1024" in msg
    # Names the bank, so a shadow bank (a daemon serving the WRONG data dir)
    # is identifiable from the error alone.
    assert str(tmp_path) in msg
    # Surfaces at /health like the Postgres refusal does.
    assert svc._init_refusal == msg
    # A retry must re-run init and refuse again — never skip past the
    # ``_cms is not None`` sentinel and serve the mismatched band.
    assert svc._cms is None


def test_file_mode_boot_accepts_a_matching_dim_bank(tmp_path, monkeypatch):
    """The guard is a mismatch guard, not a file-mode ban: a bank embedded
    at the live dimension boots exactly as before."""
    from pseudolife_memory.service import MemoryService

    _save_file_bank(tmp_path, dim=1024)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "pseudolife_memory.service.EmbeddingPipeline",
        lambda config: _FakeEmbedder(),
    )

    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()  # noqa: SLF001
    assert svc._cms is not None
    assert svc._init_refusal is None
    # The saved entry actually hydrated — this test cannot pass vacuously
    # on a load path that silently skipped the bank.
    assert sum(len(b.entries) for b in svc._cms.bands) == 1
