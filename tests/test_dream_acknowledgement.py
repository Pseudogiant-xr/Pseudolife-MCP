"""Durable, exact acknowledgement of dream input batches."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401


def _entries(service):
    service._ensure_init()  # noqa: SLF001 - white-box persistence contract
    return [
        entry
        for band in service._cms.bands  # noqa: SLF001
        for entry in band.entries
    ]


def test_signed_commit_token_rejects_tampering_and_foreign_generation() -> None:
    from pseudolife_memory.dream_token import issue_commit_token, verify_commit_token

    token = issue_commit_token(
        secret="11" * 32,
        backend="file",
        generation="generation-a",
        entry_ids=["a" * 32, "b" * 32],
        display_timestamp=42.5,
    )
    payload = verify_commit_token(
        token,
        secret="11" * 32,
        backend="file",
        generation="generation-a",
    )
    assert payload.entry_ids == ("a" * 32, "b" * 32)
    assert payload.display_timestamp == 42.5

    with pytest.raises(ValueError, match="invalid_dream_commit_token"):
        verify_commit_token(
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            secret="11" * 32,
            backend="file",
            generation="generation-a",
        )
    with pytest.raises(ValueError, match="invalid_dream_commit_token"):
        verify_commit_token(
            token,
            secret="11" * 32,
            backend="file",
            generation="generation-b",
        )


def test_commit_token_rejects_malformed_and_oversized_inputs() -> None:
    from pseudolife_memory.dream_token import (
        MAX_ENTRY_IDS,
        MAX_TOKEN_CHARS,
        issue_commit_token,
        verify_commit_token,
    )

    for malformed in (None, 12.5, "x" * (MAX_TOKEN_CHARS + 1)):
        with pytest.raises(ValueError, match="invalid_dream_commit_token"):
            verify_commit_token(
                malformed,
                secret="11" * 32,
                backend="file",
                generation="generation-a",
            )
    with pytest.raises(ValueError, match="invalid_dream_commit_payload"):
        issue_commit_token(
            secret="11" * 32,
            backend="file",
            generation="generation-a",
            entry_ids=["bad-id"],
            display_timestamp=-100.0,
        )
    with pytest.raises(ValueError, match="invalid_dream_commit_payload"):
        issue_commit_token(
            secret="11" * 32,
            backend="postgres",
            generation="generation-a",
            entry_ids=list(range(1, MAX_ENTRY_IDS + 2)),
            display_timestamp=-100.0,
        )


def test_commit_token_accepts_finite_pre_epoch_display_timestamp() -> None:
    from pseudolife_memory.dream_token import issue_commit_token, verify_commit_token

    token = issue_commit_token(
        secret="11" * 32,
        backend="file",
        generation="generation-a",
        entry_ids=["a" * 32],
        display_timestamp=-100.0,
    )
    assert verify_commit_token(
        token,
        secret="11" * 32,
        backend="file",
        generation="generation-a",
    ).display_timestamp == -100.0


def test_file_acknowledges_exact_tied_and_backdated_entries(pristine_service) -> None:
    svc = pristine_service
    svc.store("alpha setting is enabled", source="notes")
    svc.store("beta setting is enabled", source="notes")
    svc.store("gamma setting is enabled", source="notes")
    entries = _entries(svc)
    entries[0].timestamp = 100.0
    entries[1].timestamp = 100.0
    entries[2].timestamp = 50.0

    first = svc.dream_pull(limit=2)
    assert first["count"] == 2
    assert "commit_token" in first
    acknowledged_ids = {e["dream_id"] for e in first["entries"]}
    result = svc.dream_commit(first["commit_token"])
    assert result["acknowledged"] == 2

    remaining = svc.dream_pull(limit=10)
    assert remaining["count"] == 1
    assert remaining["entries"][0]["dream_id"] not in acknowledged_ids
    assert remaining["entries"][0]["timestamp"] == 100.0


def test_file_ack_save_failure_leaves_entries_pending(pristine_service, monkeypatch) -> None:
    svc = pristine_service
    svc.store("durability probe is active", source="notes")
    pulled = svc.dream_pull(limit=10)
    before_cursor = pulled["cursor"]

    def fail_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(svc._cms, "save", fail_save)  # noqa: SLF001
    result = svc.dream_commit(pulled["commit_token"])
    assert result["error"] == "dream_ack_persist_failed"
    assert svc._cortex.dream_cursor == before_cursor  # noqa: SLF001
    assert all(entry.dream_state == "pending" for entry in _entries(svc))


def test_automatic_dream_reports_ack_failure_and_retries_batch(
        pristine_service, monkeypatch) -> None:
    svc = pristine_service
    svc.store("automatic acknowledgement probe", source="notes")

    class EmptyExtractor:
        def extract(self, texts, vocab=None, known_facts=None):
            return []

    monkeypatch.setattr(
        svc._cms, "save",  # noqa: SLF001
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = svc.dream_run(EmptyExtractor())
    assert result["error"] == "dream_ack_persist_failed"
    assert result["acknowledgement_failed"] is True
    assert result["claims"] == 0
    assert all(entry.dream_state == "pending" for entry in _entries(svc))


def test_empty_pull_omits_commit_token(pristine_service) -> None:
    pulled = pristine_service.dream_pull()
    assert pulled["entries"] == []
    assert "commit_token" not in pulled


def test_file_restart_uses_cms_display_cursor_over_stale_cortex(tmp_path: Path) -> None:
    from pseudolife_memory.service import MemoryService

    original = MemoryService(data_dir=tmp_path)
    original.store("restart cursor probe", source="notes")
    pulled = original.dream_pull()
    committed = original.dream_commit(pulled["commit_token"])
    assert committed["dream_cursor"] > 0.0

    reloaded = MemoryService(data_dir=tmp_path)
    after = reloaded.dream_pull()
    assert after["cursor"] == committed["dream_cursor"]
    assert after["entries"] == []


@pytest.mark.parametrize(
    "corruption", ["secret", "state", "duplicate-id", "missing-id", "display"])
def test_file_checkpoint_corruption_disables_only_dream(
        tmp_path: Path, corruption: str) -> None:
    import torch

    from pseudolife_memory.service import MemoryService

    original = MemoryService(data_dir=tmp_path)
    original.store("checkpoint validation probe", source="notes")
    original.save()
    cms_path = tmp_path / "memory_state" / "cms_state.pt"
    state = torch.load(cms_path, weights_only=True)
    entries = next(iter(state["bands"].values()))["entries"]
    if corruption == "secret":
        state["dream_ack_secret"] = "not-a-secret"
    elif corruption == "state":
        entries[0]["dream_state"] = "impossible"
    elif corruption == "display":
        state["dream_display_cursor"] = "not-a-number"
    elif corruption == "missing-id":
        entries[0].pop("dream_id")
    else:
        entries.append(dict(entries[0]))
    torch.save(state, cms_path)

    reloaded = MemoryService(data_dir=tmp_path)
    pulled = reloaded.dream_pull()
    assert pulled["error"] == "invalid_dream_ack_state"
    assert reloaded.stats()["total_memories"] >= 1


def test_postgres_ack_failure_marks_automatic_run_failed(
        pg_service, monkeypatch) -> None:
    svc = pg_service
    svc.store("ack-journal relay port is 8123", source="notes")

    class StubExtractor:
        def extract(self, texts, vocab=None, known_facts=None):
            return [{"entity": "ack-journal relay", "attribute": "port",
                     "value": "8123", "confidence": 0.9,
                     "origin": "agent", "source": 0}]

    def fail_ack(*args, **kwargs):
        raise OSError("connection outcome unknown")

    monkeypatch.setattr(svc._storage, "acknowledge_dream_entries", fail_ack)
    result = svc.dream_run(StubExtractor())
    assert result["error"] == "dream_ack_persist_failed"
    assert result["acknowledgement_failed"] is True
    assert svc.dream_runs(limit=1)["runs"][0]["status"] == "failed"
    assert svc._storage.conn.execute(  # noqa: SLF001
        "SELECT dream_state FROM entries WHERE text LIKE 'ack-journal%'"
    ).fetchone()[0] == "pending"


def _resident(service, text: str):
    return next(entry for band in service._cms.bands  # noqa: SLF001
                for entry in band.entries if entry.text == text)


def test_pending_order_survives_a_resident_without_a_row_id(pg_service) -> None:
    """``cms.store`` seats the entry in the band BEFORE its write-through
    insert, so a failed insert leaves a resident holding ``db_id=None``.
    The pull order must stay type-homogeneous through that — a tie on
    timestamp used to compare an int against a str and raise."""
    svc = pg_service
    svc.store("tied alpha is enabled", source="notes")
    svc.store("tied beta is enabled", source="notes")
    for entry in _entries(svc):
        entry.timestamp = 100.0
    _resident(svc, "tied alpha is enabled").db_id = None

    first = [entry.text for entry in svc._pending_dream_entries()]  # noqa: SLF001
    assert first == [entry.text for entry in svc._pending_dream_entries()]  # noqa: SLF001
    assert len(first) == 2
    assert svc.dream_status()["backlog"] == 2


def test_pull_reflushes_a_resident_that_never_reached_storage(pg_service) -> None:
    svc = pg_service
    svc.store("healed probe is enabled", source="notes")
    svc.store("intact probe is enabled", source="notes")
    orphan = _resident(svc, "healed probe is enabled")
    svc._storage.conn.execute(  # noqa: SLF001
        "DELETE FROM entries WHERE id = %s", (orphan.db_id,))
    orphan.db_id = None

    pulled = svc.dream_pull(limit=10)

    assert "error" not in pulled
    assert pulled["count"] == 2
    assert orphan.db_id is not None
    assert pulled.get("skipped_unpersisted", 0) == 0
    assert svc.dream_commit(pulled["commit_token"])["newly_acknowledged"] == 2


def test_pull_skips_an_unpersistable_resident_instead_of_stalling(
        pg_service, monkeypatch) -> None:
    svc = pg_service
    svc.store("intact probe is enabled", source="notes")
    svc.store("doomed probe is enabled", source="notes")
    doomed = _resident(svc, "doomed probe is enabled")
    svc._storage.conn.execute(  # noqa: SLF001
        "DELETE FROM entries WHERE id = %s", (doomed.db_id,))
    doomed.db_id = None
    monkeypatch.setattr(
        svc._storage, "insert_entry",  # noqa: SLF001
        lambda *a, **k: (_ for _ in ()).throw(OSError("insert failed")))

    pulled = svc.dream_pull(limit=10)

    assert "error" not in pulled
    assert pulled["count"] == 1
    assert pulled["skipped_unpersisted"] == 1
    assert pulled["entries"][0]["text"] == "intact probe is enabled"
    assert svc.dream_status()["backlog"] == 2
    assert svc.dream_commit(pulled["commit_token"])["newly_acknowledged"] == 1


def test_postgres_commit_reports_a_vanished_entry_and_keeps_the_rest(
        pg_service) -> None:
    svc = pg_service
    svc.store("survivor probe is enabled", source="notes")
    svc.store("vanished probe is enabled", source="notes")
    pulled = svc.dream_pull(limit=10)
    vanished = _resident(svc, "vanished probe is enabled")
    svc._storage.conn.execute(  # noqa: SLF001
        "DELETE FROM entries WHERE id = %s", (vanished.db_id,))

    result = svc.dream_commit(pulled["commit_token"])

    assert "error" not in result
    assert result["acknowledged"] == 1
    assert result["newly_acknowledged"] == 1
    assert result["missing"] == 1
    assert svc.dream_status()["backlog"] == 0


def test_file_commit_reports_a_forgotten_entry_and_keeps_the_rest(
        pristine_service) -> None:
    svc = pristine_service
    svc.store("survivor note is enabled", source="notes")
    svc.store("forgotten note is enabled", source="notes")
    pulled = svc.dream_pull(limit=10)
    forgotten = _resident(svc, "forgotten note is enabled")
    for band in svc._cms.bands:  # noqa: SLF001
        if forgotten in band.entries:
            band.entries.remove(forgotten)

    result = svc.dream_commit(pulled["commit_token"])

    assert "error" not in result
    assert result["acknowledged"] == 1
    assert result["missing"] == 1
    assert svc.dream_pull(limit=10)["count"] == 0


def test_transient_tracking_failure_is_retried_by_the_next_dream_call(
        pg_conn, pg_url, tmp_path: Path, monkeypatch) -> None:
    """A dream-tracking failure used to latch until the daemon restarted.
    A storage blip must clear itself on the next dream call."""
    import psycopg

    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage.postgres import PostgresStorage

    real = PostgresStorage.initialize_dream_tracking
    calls: list[int] = []

    def flaky(self, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.OperationalError("connection lost")
        return real(self, **kwargs)

    monkeypatch.setattr(PostgresStorage, "initialize_dream_tracking", flaky)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    try:
        svc._ensure_init()  # noqa: SLF001
        assert svc._dream_tracking_error is not None  # noqa: SLF001

        status = svc.dream_status()

        assert "error" not in status
        assert svc._dream_tracking_error is None  # noqa: SLF001
        assert len(calls) == 2
        svc.store("post-recovery probe is enabled", source="notes")
        assert svc.dream_pull(limit=10)["count"] == 1
        assert len(calls) == 2
    finally:
        svc._storage.close()  # noqa: SLF001


def test_corrupt_bank_secret_is_not_retried_on_every_dream_call(
        pg_conn, pg_url, tmp_path: Path, monkeypatch) -> None:
    """The companion guard to the retry: a corrupt secret is the bank's own
    data, and re-running the same pass over the same bytes fails the same
    way. It stays latched so the failure is reported, not re-attempted on
    every call."""
    import json

    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage.postgres import PostgresStorage

    pg_conn.execute(
        "INSERT INTO meta (key, value) VALUES "
        "('dream_ack_secret_v1', %s::jsonb)", (json.dumps("not-a-secret"),))
    pg_conn.commit()
    real = PostgresStorage.initialize_dream_tracking
    calls: list[int] = []

    def counted(self, **kwargs):
        calls.append(1)
        return real(self, **kwargs)

    monkeypatch.setattr(PostgresStorage, "initialize_dream_tracking", counted)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    try:
        svc._ensure_init()  # noqa: SLF001
        assert len(calls) == 1

        assert svc.dream_pull()["error"] == "dream_ack_initialization_failed"
        assert svc.dream_status()["error"] == "dream_ack_initialization_failed"
        assert len(calls) == 1
    finally:
        svc._storage.close()  # noqa: SLF001


def test_invalid_legacy_cursor_blocks_only_dream_initialization(
        tmp_path: Path, monkeypatch) -> None:
    import torch

    from pseudolife_memory.service import MemoryService

    original = MemoryService(data_dir=tmp_path)
    original.store("legacy entry remains readable", source="notes")
    original._cortex.dream_cursor = math.inf  # noqa: SLF001
    original.save()
    cms_path = tmp_path / "memory_state" / "cms_state.pt"
    state = torch.load(cms_path, weights_only=True)
    state["schema_version"] = 6
    state.pop("dream_ack_secret", None)
    state.pop("dream_display_cursor", None)
    for band in state["bands"].values():
        for entry in band["entries"]:
            entry.pop("dream_state", None)
            entry.pop("dream_id", None)
    torch.save(state, cms_path)

    reloaded = MemoryService(data_dir=tmp_path)
    assert reloaded.search("legacy entry", top_k=3)["count"] >= 1
    pulled = reloaded.dream_pull()
    assert pulled["error"] == "invalid_legacy_dream_cursor"
