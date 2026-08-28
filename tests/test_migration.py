"""Legacy v7 .pt bank → schema v8 Postgres migration round-trip."""

from __future__ import annotations

import pytest
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
    numeric output
    (``test_embedding_dim_guard.py::test_service_round_trip_at_dim_1024``
    already proves the real Qwen3 pipeline produces 1024-d vectors
    end to end)."""

    embedding_dim = 1024

    def encode_single(self, text: str) -> torch.Tensor:
        g = torch.Generator().manual_seed(abs(hash(text)) % (2**31))
        v = torch.randn(self.embedding_dim, generator=g)
        return v / v.norm()


def _build_legacy_bank(data_dir, suffix: str = "", config=None):
    """Synthesize a v7 bank: entries + episode + supersession + cortex.

    ``suffix`` distinguishes one synthetic bank from another (used by the
    source-identity test, which needs two banks whose bytes differ).

    ``config`` selects the band preset the legacy bank was written under.
    The default here is the *live* default (``flat``), which is the least
    realistic case for a v<=7 bank — every real one predates the
    2026-08-15 flat-band cutover and carries continuum band names. Tests
    that care about the band column must pass ``preset="continuum"``.

    BOTH entries and the cortex fact embed at legacy 384-d — the only
    shape that exists in the wild (every real legacy .pt bank predates
    schema v25's 1024-d/Qwen3 default for every table alike). Prior to the
    2026-07-28 review escalation, migrate_legacy only repaired the entries
    branch on a dim mismatch; the facts branch inserted verbatim and blew
    up AFTER entries had already committed, permanently losing the facts
    (the idempotency guard blocks every retry once entries is non-empty).
    This fixture pins the fix: both branches must survive."""
    cms = ContinuumMemorySystem(config or MemoryConfig())
    cms.episodes.start("legacy session" + suffix)
    cms.store("legacy fact alpha" + suffix, _emb(1), source="legacy",
              tags=["old"])
    cms.store("legacy fact beta" + suffix, _emb(2), source="legacy")
    cms.bands[0].entries and None  # entries may have promoted; that's fine
    # Mark one superseded the way the contradiction path would.
    target = next(e for b in cms.bands for e in b.entries
                  if e.text == "legacy fact alpha" + suffix)
    target.superseded_at = 123.0
    target.superseded_by_text = "legacy fact beta" + suffix
    cms.save(data_dir / "memory_state")

    cortex = CortexStore()
    cortex.write_fact(Slot("legacy-proj" + suffix, "language", "rust"),
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


# ── interrupted-import resume (#187) ────────────────────────────────────
#
# Before this, the only idempotency guard was "the entries table is
# non-empty". Each insert_entry commits its own transaction and the
# sources are renamed only at the very end, so an import that died
# part-way durably committed rows, then every later boot saw a non-empty
# entries table and returned ``storage_not_empty`` — the remainder and
# ALL cortex facts were never imported, and the service caller swallowed
# the exception to a single warning. These tests pin the progress record
# and the resume.


def _migration_state(storage):
    from pseudolife_memory.storage.migrate import MIGRATION_META_KEY

    return storage.meta_get(MIGRATION_META_KEY)


def test_interrupted_migration_records_progress_and_resumes(
        pg_conn, pg_url, tmp_path):
    """Facts branch dies after the entries loop already committed."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        real_replace_facts = storage.replace_facts

        def _boom(rows):
            raise RuntimeError("connection dropped mid-import")

        storage.replace_facts = _boom
        with pytest.raises(RuntimeError, match="connection dropped"):
            migrate_legacy(tmp_path, storage, embedder)

        # Entries committed; the sources were NOT renamed; the progress
        # record says so instead of the bank silently looking "migrated".
        assert len(storage.load_entries()) == 2
        assert (tmp_path / "memory_state" / "cms_state.pt").exists()
        assert (tmp_path / "cortex_state.pt").exists()
        state = _migration_state(storage)
        assert state["status"] == "in_progress"
        assert state["entries_done"] == 2
        assert state["source"]  # fingerprint of the interrupted sources

        # Rerun resumes rather than returning "storage not empty".
        storage.replace_facts = real_replace_facts
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True
        assert summary["resumed"] is True
        # Both entries were already present — skipped, not duplicated.
        assert summary["entries"] == 0 and summary["entries_skipped"] == 2
        assert summary["facts"] == 1
        rows = storage.load_entries()
        assert len(rows) == 2
        assert len(storage.load_facts()) == 1
        assert (tmp_path / "memory_state" / "cms_state.pt.pre-v8.bak").exists()
        assert (tmp_path / "cortex_state.pt.pre-v8.bak").exists()
        assert _migration_state(storage)["status"] == "done"
    finally:
        storage.close()


def test_interrupted_entries_loop_resumes_without_duplicates(
        pg_conn, pg_url, tmp_path):
    """Death *inside* the entries loop — the half-imported case."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        real_insert = storage.insert_entry
        calls = {"n": 0}

        def _insert_once_then_die(e):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("disk full mid-import")
            return real_insert(e)

        storage.insert_entry = _insert_once_then_die
        with pytest.raises(RuntimeError, match="disk full"):
            migrate_legacy(tmp_path, storage, embedder)
        assert len(storage.load_entries()) == 1
        assert _migration_state(storage)["status"] == "in_progress"

        storage.insert_entry = real_insert
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True and summary["resumed"] is True
        # One row was already there; exactly the remaining one was inserted.
        assert summary["entries_skipped"] == 1 and summary["entries"] == 1
        texts = [r["text"] for r in storage.load_entries()]
        assert sorted(texts) == ["legacy fact alpha", "legacy fact beta"]
        assert _migration_state(storage)["status"] == "done"
    finally:
        storage.close()


def test_real_bank_without_migration_state_is_left_alone(
        pg_conn, pg_url, tmp_path):
    """A populated bank with no progress record keeps the old refusal —
    resume must never merge a legacy .pt into somebody's live bank."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        storage.insert_entry({
            "band": "working", "text": "a real memory",
            "embedding": embedder.encode_single("a real memory"),
            "surprise": 0.0, "ts": 1.0, "access_count": 0, "source": "user",
            "tags": [], "slots": [],
        })
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is False
        assert summary["reason"] == "storage_not_empty"
        assert len(storage.load_entries()) == 1
        assert _migration_state(storage) is None
        # Sources untouched — nothing was imported, nothing was renamed.
        assert (tmp_path / "memory_state" / "cms_state.pt").exists()
    finally:
        storage.close()


def test_partial_migration_refuses_a_different_legacy_source(
        pg_conn, pg_url, tmp_path):
    """The recorded fingerprint keeps an unrelated .pt bank from being
    folded into the interrupted one's leftovers."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        def _boom(rows):
            raise RuntimeError("connection dropped mid-import")

        storage.replace_facts = _boom
        with pytest.raises(RuntimeError):
            migrate_legacy(tmp_path, storage, embedder)
        assert _migration_state(storage)["status"] == "in_progress"

        # Swap in a *different* legacy bank at the same paths.
        other = tmp_path / "other"
        other.mkdir()
        _build_legacy_bank(other, suffix=" two")
        for rel in ("memory_state/cms_state.pt", "cortex_state.pt"):
            (tmp_path / rel).write_bytes((other / rel).read_bytes())

        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is False
        assert summary["reason"] == "partial_migration_source_mismatch"
        assert len(storage.load_entries()) == 2  # nothing merged in
        assert (tmp_path / "memory_state" / "cms_state.pt").exists()
    finally:
        storage.close()


def test_rename_completed_before_the_status_write_is_reconciled(
        pg_conn, pg_url, tmp_path):
    """Crash in the one-``meta_set`` window between the source renames and
    ``status=done`` must not leave a permanently-degraded record."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True
        # Rewind the record to the pre-``status=done`` state the crash
        # would have left behind (renames already on disk).
        from pseudolife_memory.storage.migrate import MIGRATION_META_KEY
        state = storage.meta_get(MIGRATION_META_KEY)
        state["status"] = "in_progress"
        storage.meta_set(MIGRATION_META_KEY, state)

        again = migrate_legacy(tmp_path, storage, embedder)
        assert again["reason"] == "completed_at_rename"
        assert _migration_state(storage)["status"] == "done"
        assert len(storage.load_entries()) == 2
    finally:
        storage.close()


def test_resume_survives_a_half_completed_rename(pg_conn, pg_url, tmp_path):
    """The renames at the end are per-file, so a crash between them leaves
    one source renamed. That must still read as *this* interrupted import,
    not as a foreign bank."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        real_replace_facts = storage.replace_facts
        storage.replace_facts = lambda rows: (_ for _ in ()).throw(
            RuntimeError("dropped"))
        with pytest.raises(RuntimeError):
            migrate_legacy(tmp_path, storage, embedder)

        # Only the entries source made it through the rename loop.
        cms = tmp_path / "memory_state" / "cms_state.pt"
        cms.rename(cms.with_name(cms.name + ".pre-v8.bak"))

        storage.replace_facts = real_replace_facts
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True and summary["resumed"] is True
        assert summary["facts"] == 1 and summary["entries"] == 0
        assert len(storage.load_entries()) == 2
        assert (tmp_path / "cortex_state.pt.pre-v8.bak").exists()
        assert _migration_state(storage)["status"] == "done"
    finally:
        storage.close()


def test_resume_after_hydrate_rewrote_band_stamps_does_not_duplicate(
        pg_conn, pg_url, tmp_path):
    """The real boot sequence runs hydrate_cms right after migrate_legacy,
    and hydrate REWRITES the band column of every row whose band is not in
    the live preset (sync.hydrate_cms's stale-stamp reconcile). A legacy
    v<=7 bank carries continuum band names while the live default preset is
    the single ``flat`` band, so boot 1's imported rows come back with
    band='flat'. A skip identity that included ``band`` would therefore
    match nothing on boot 2 and re-insert the whole imported prefix."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.sync import hydrate_cms
    from pseudolife_memory.utils.config import MIRASConfig

    legacy_cfg = MemoryConfig(miras=MIRASConfig(preset="continuum"))
    _build_legacy_bank(tmp_path, config=legacy_cfg)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        real_replace_facts = storage.replace_facts
        storage.replace_facts = lambda rows: (_ for _ in ()).throw(
            RuntimeError("dropped mid-import"))
        with pytest.raises(RuntimeError):
            migrate_legacy(tmp_path, storage, embedder)
        imported = storage.load_entries()
        assert len(imported) == 2
        # The legacy bank really did use continuum band names.
        assert {r["band"] for r in imported} != {"flat"}

        # ...and the rest of boot 1 runs, rewriting those stamps to the
        # live preset's single band.
        hydrate_cms(ContinuumMemorySystem(MemoryConfig()), storage)
        assert {r["band"] for r in storage.load_entries()} == {"flat"}

        # Boot 2 resumes. The prefix must be recognised despite the rewrite.
        storage.replace_facts = real_replace_facts
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True and summary["resumed"] is True
        assert summary["entries_skipped"] == 2 and summary["entries"] == 0
        assert len(storage.load_entries()) == 2
    finally:
        storage.close()


def _write_live_fact(storage, embedder, entity, attribute, value):
    """Commit one fact the way the service's per-write path does — a
    per-slot replace, not a snapshot rewrite."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.storage.sync import _record_to_row

    cortex = CortexStore()
    cortex.write_fact(Slot(entity, attribute, value),
                      embedder.encode_single(f"{entity} {attribute} {value}"),
                      confidence=0.9, support="user")
    rows = [_record_to_row(r) for r in cortex.records]
    slots = {(r["entity_norm"], r["attribute_norm"]) for r in rows}
    storage.replace_slot_facts(slots, rows)


def test_resume_merges_facts_instead_of_replacing_live_writes(
        pg_conn, pg_url, tmp_path):
    """The daemon deliberately keeps serving between the failed import and
    the resume, so anything written into the cortex during that degraded
    window must survive the resume. A snapshot ``replace_facts`` would
    DELETE it, and resetting the dream cursor backwards would force
    re-extraction of everything since."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        real_replace_facts = storage.replace_facts
        storage.replace_facts = lambda rows: (_ for _ in ()).throw(
            RuntimeError("dropped mid-import"))
        with pytest.raises(RuntimeError):
            migrate_legacy(tmp_path, storage, embedder)
        assert storage.load_facts() == []

        # The degraded window: a real write lands, and a dream advances the
        # cursor well past the legacy bank's.
        _write_live_fact(storage, embedder, "live-proj", "language", "python")
        storage.meta_set("cortex_dream_cursor", 9_999.0)
        storage.meta_set("cortex_supersession_log", [{"slot": "live"}])

        storage.replace_facts = real_replace_facts
        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is True and summary["resumed"] is True

        by_slot = {(f["entity"], f["attribute"]): f for f in storage.load_facts()}
        # The live write survived...
        assert by_slot[("live-proj", "language")]["value"] == "python"
        # ...and the legacy fact landed in its own, empty slot.
        assert by_slot[("legacy-proj", "language")]["value"] == "rust"
        # The cursor only ever moves forward.
        assert float(storage.meta_get("cortex_dream_cursor")) == 9_999.0
        # The live supersession log was not clobbered by the legacy one.
        assert storage.meta_get("cortex_supersession_log") == [{"slot": "live"}]
    finally:
        storage.close()


def test_resume_refuses_when_a_recorded_source_vanished(
        pg_conn, pg_url, tmp_path):
    """Matching only the sources still ON DISK would let a recorded source
    that simply disappeared pass the check — and the resume would then mark
    a permanently short bank ``done``."""
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage

    _build_legacy_bank(tmp_path)
    storage = PostgresStorage(pg_url)
    embedder = _FakeEmbedder()
    try:
        storage.replace_facts = lambda rows: (_ for _ in ()).throw(
            RuntimeError("dropped mid-import"))
        with pytest.raises(RuntimeError):
            migrate_legacy(tmp_path, storage, embedder)
        state = _migration_state(storage)
        assert set(state["source"]) == {"cms", "cortex"}

        # The cortex source is deleted rather than renamed — its facts were
        # never imported, so this bank can never be completed.
        (tmp_path / "cortex_state.pt").unlink()

        summary = migrate_legacy(tmp_path, storage, embedder)
        assert summary["migrated"] is False
        assert summary["reason"] == "partial_migration_source_mismatch"
        assert _migration_state(storage)["status"] == "in_progress"
    finally:
        storage.close()
