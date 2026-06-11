"""Legacy v7 .pt bank → schema v8 Postgres migration round-trip."""

from __future__ import annotations

import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.utils.config import MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


def _build_legacy_bank(data_dir):
    """Synthesize a v7 bank: entries + episode + supersession + cortex."""
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
    cortex.write_fact(Slot("legacy-proj", "language", "rust"), _emb(3),
                      confidence=0.9, support="user")
    cortex.save(data_dir / "cortex_state.pt")
    return cms


def test_migration_roundtrip(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    legacy = _build_legacy_bank(tmp_path)
    legacy_texts = {e.text for b in legacy.bands for e in b.entries}

    storage = PostgresStorage(pg_url)
    try:
        summary = migrate_legacy(tmp_path, storage)
        assert summary["migrated"] is True
        assert summary["entries"] == len(legacy_texts) == 2
        assert summary["episodes"] == 1 and summary["facts"] == 1

        rows = {r["text"]: r for r in storage.load_entries()}
        assert set(rows) == legacy_texts
        assert rows["legacy fact alpha"]["superseded_at"] == 123.0
        assert rows["legacy fact alpha"]["tags"] == ["old"]
        assert rows["legacy fact beta"]["episode_title"] == "legacy session"
        facts = storage.load_facts()
        assert len(facts) == 1 and facts[0]["value"] == "rust"
        assert facts[0]["origin"] == "user"

        # Sources renamed, originals preserved as .pre-v8.bak.
        assert not (tmp_path / "memory_state" / "cms_state.pt").exists()
        assert (tmp_path / "memory_state" / "cms_state.pt.pre-v8.bak").exists()
        assert (tmp_path / "cortex_state.pt.pre-v8.bak").exists()

        # Idempotent: second call no-ops.
        again = migrate_legacy(tmp_path, storage)
        assert again["migrated"] is False
        assert len(storage.load_entries()) == 2
    finally:
        storage.close()
