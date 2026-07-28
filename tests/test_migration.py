"""Legacy v7 .pt bank → schema v8 Postgres migration round-trip."""

from __future__ import annotations

import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.utils.config import MemoryConfig


def _emb(seed: int, dim: int = 384) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


class _FakeEmbedder:
    """Duck-types the two ``EmbeddingPipeline`` members ``migrate_legacy``
    calls (``embedding_dim`` / ``encode_single``) — real-model-free so this
    test exercises migrate.py's re-embed DECISION, not a real model's
    numeric output (``test_schema_v25.py::test_service_round_trip_at_dim_1024``
    already proves the real Qwen3 pipeline produces 1024-d vectors
    end to end)."""

    embedding_dim = 1024

    def encode_single(self, text: str) -> torch.Tensor:
        g = torch.Generator().manual_seed(abs(hash(text)) % (2**31))
        v = torch.randn(self.embedding_dim, generator=g)
        return v / v.norm()


def _build_legacy_bank(data_dir):
    """Synthesize a v7 bank: entries + episode + supersession + cortex.

    BOTH entries and the cortex fact embed at legacy 384-d — the only
    shape that exists in the wild (every real legacy .pt bank predates
    schema v25's 1024-d/Qwen3 default for every table alike). Prior to the
    2026-07-28 review escalation, migrate_legacy only repaired the entries
    branch on a dim mismatch; the facts branch inserted verbatim and blew
    up AFTER entries had already committed, permanently losing the facts
    (the idempotency guard blocks every retry once entries is non-empty).
    This fixture pins the fix: both branches must survive."""
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.episodes.start("legacy session")
    cms.store("legacy fact alpha", _emb(1), source="legacy", tags=["old"])
    cms.store("legacy fact beta", _emb(2), source="legacy")
    cms.bands[0].entries and None  # entries may have promoted; that's fine
    # Mark one superseded the way the contradiction path would.
    target = next(e for b in cms.bands for e in b.entries
                  if e.text == "legacy fact alpha")
    target.superseded_at = 123.0
    target.superseded_by_text = "legacy fact beta"
    cms.save(data_dir / "memory_state")

    cortex = CortexStore()
    cortex.write_fact(Slot("legacy-proj", "language", "rust"),
                      _emb(3, dim=384), confidence=0.9, support="user")
    cortex.save(data_dir / "cortex_state.pt")
    return cms


def test_migration_roundtrip(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    legacy = _build_legacy_bank(tmp_path)
    legacy_texts = {e.text for b in legacy.bands for e in b.entries}

    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True
        assert summary["entries"] == len(legacy_texts) == 2
        assert summary["episodes"] == 1 and summary["facts"] == 1
        # Both entries were stored at legacy 384-d and had to be re-embedded
        # to fit the live vector(1024) column — the exact bug this test
        # pins (a real bank in the wild is always 384-d, never 1024-d).
        assert summary["reembedded"] == 2
        # The cortex fact was stored at legacy 384-d — same bug class as
        # entries, now fixed. Pins the exact count (not just "> 0") so a
        # regression that silently stops re-embedding facts fails loudly.
        assert summary["reembedded_facts"] == 1

        rows = {r["text"]: r for r in storage.load_entries()}
        assert set(rows) == legacy_texts
        assert rows["legacy fact alpha"]["superseded_at"] == 123.0
        assert rows["legacy fact alpha"]["tags"] == ["old"]
        assert rows["legacy fact beta"]["episode_title"] == "legacy session"
        # The re-embedded vectors actually landed at the live dimension --
        # proves the row fit the vector(1024) column, not just that no
        # exception was raised.
        assert all(len(r["embedding"]) == 1024 for r in rows.values())
        facts = storage.load_facts()
        assert len(facts) == 1 and facts[0]["value"] == "rust"
        assert facts[0]["origin"] == "user"
        # The fact actually landed at the live dimension too — the whole
        # point of the fix (a verbatim 384-d insert would have raised
        # inside replace_facts, after entries already committed).
        assert len(facts[0]["embedding"]) == 1024

        # Sources renamed, originals preserved as .pre-v8.bak.
        assert not (tmp_path / "memory_state" / "cms_state.pt").exists()
        assert (tmp_path / "memory_state" / "cms_state.pt.pre-v8.bak").exists()
        assert (tmp_path / "cortex_state.pt.pre-v8.bak").exists()

        # Idempotent: second call no-ops.
        again = migrate_legacy(tmp_path, storage, embedder)
        assert again["migrated"] is False
        assert len(storage.load_entries()) == 2
    finally:
        storage.close()
