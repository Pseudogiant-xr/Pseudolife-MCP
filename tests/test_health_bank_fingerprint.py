"""``/health`` names the bank it serves: ``bank`` is the first 16 hex
characters of the SHA-256 of the coordination bank id (meta key
``coordination_bank_id``), or null when that is unknown.

``version`` and ``schema`` say what the daemon runs, not which bank it
holds; ``pseudolife-mcp expose`` compares this fingerprint through the
tailnet address, and later commands use it to tell two daemons' banks
apart. The probe is polled every 15 s by the Docker healthcheck, so the
read is lazy and cached: it never starts storage, never takes the service
lock, reads the meta row on its own short-lived connection only after
storage has started, and stops reading once the id is found.
"""

from __future__ import annotations

import hashlib
import uuid

from psycopg.types.json import Jsonb

from pseudolife_memory.daemon import _build_health_payload
from pseudolife_memory.storage.coordination import BANK_ID_META_KEY
from pseudolife_memory.storage.postgres import PostgresStorage
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


class _Svc:
    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None

    def __init__(self, storage=None):
        self._storage = storage


def _fingerprint(bank_id: str) -> str:
    return hashlib.sha256(bank_id.encode("utf-8")).hexdigest()[:16]


def test_a_cold_daemon_reports_null_and_leaves_storage_unstarted(pg_url, tmp_path):
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    payload = _build_health_payload(svc, token_present=True)
    assert "bank" in payload and payload["bank"] is None
    assert svc._storage is None  # noqa: SLF001 — /health must not start it


def test_a_file_mode_service_reports_null():
    assert _build_health_payload(_Svc(), token_present=False)["bank"] is None


def test_a_started_bank_reports_its_fingerprint(pg_conn, pg_url):
    bank_id = str(uuid.uuid4())
    pg_conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s)",
                    (BANK_ID_META_KEY, Jsonb(bank_id)))
    pg_conn.commit()
    storage = PostgresStorage(pg_url)
    try:
        payload = _build_health_payload(_Svc(storage), token_present=True)
        assert payload["bank"] == _fingerprint(bank_id)
        assert len(payload["bank"]) == 16
        assert payload["status"] == "ok"
    finally:
        storage.close()


def test_the_fingerprint_is_cached_once_found(pg_conn, pg_url, monkeypatch):
    bank_id = str(uuid.uuid4())
    pg_conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s)",
                    (BANK_ID_META_KEY, Jsonb(bank_id)))
    pg_conn.commit()
    storage = PostgresStorage(pg_url)
    try:
        svc = _Svc(storage)
        assert _build_health_payload(svc, token_present=True)["bank"] == _fingerprint(bank_id)
        # Found once, never read again: a later read would see the row gone.
        pg_conn.execute("DELETE FROM meta WHERE key=%s", (BANK_ID_META_KEY,))
        pg_conn.commit()
        import pseudolife_memory.storage.postgres as postgres

        now = postgres.time.monotonic()
        monkeypatch.setattr(postgres.time, "monotonic",
                            lambda: now + postgres.BANK_ID_RETRY_SECONDS + 1)
        assert _build_health_payload(svc, token_present=True)["bank"] == _fingerprint(bank_id)
    finally:
        storage.close()


def test_a_bank_without_an_id_yet_is_null_then_found_after_the_retry(
        pg_conn, pg_url, monkeypatch):
    import pseudolife_memory.storage.postgres as postgres

    storage = PostgresStorage(pg_url)
    try:
        svc = _Svc(storage)
        assert _build_health_payload(svc, token_present=True)["bank"] is None
        bank_id = str(uuid.uuid4())
        pg_conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s)",
                        (BANK_ID_META_KEY, Jsonb(bank_id)))
        pg_conn.commit()
        # Inside the retry window nothing is read again...
        assert _build_health_payload(svc, token_present=True)["bank"] is None
        # ...and after it the id the board created since is found.
        now = postgres.time.monotonic()
        monkeypatch.setattr(postgres.time, "monotonic",
                            lambda: now + postgres.BANK_ID_RETRY_SECONDS + 1)
        assert _build_health_payload(svc, token_present=True)["bank"] == _fingerprint(bank_id)
    finally:
        storage.close()


def test_a_failing_read_never_raises():
    class _Broken:
        def cached_bank_id(self):
            raise RuntimeError("database gone")

    payload = _build_health_payload(_Svc(_Broken()), token_present=True)
    assert payload["bank"] is None
    assert payload["status"] == "ok"


def test_a_non_string_answer_is_not_a_fingerprint():
    class _Odd:
        def cached_bank_id(self):
            return object()

    assert _build_health_payload(_Svc(_Odd()), token_present=True)["bank"] is None
