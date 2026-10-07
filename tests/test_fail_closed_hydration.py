"""Fail-closed hydration: never serve or write from a half-loaded bank.

The daemon's canonical stores are resident copies written back per slot:
``sync_cortex_slots`` sends Postgres only the resident rows of each dirty
slot, and Postgres DELETEs every row of that slot before inserting them.
Until 2026-09-23, a failed ``hydrate_cortex`` / ``hydrate_world_cortex`` /
``hydrate_lessons`` at startup was logged and swallowed, and the daemon
carried on with that store EMPTY — so the next write to a pre-existing slot
replaced the slot's whole durable history with one new row, and an explicit
save rewrote the entire table from the empty copy (fresh-eyes review, Tier A
item 8; demonstrated then on fake storage only). A failed ``hydrate_cms``
was worse in a different way: it left the half-built CMS assigned, and
``_ensure_init`` never ran again.

Now any failure while building the resident stores drops all of them and
raises, retries back off, and ``/health`` reports the refusal. These probes
run against the per-run test database.
"""
from __future__ import annotations

import psycopg
import pytest

from pseudolife_memory.daemon import _build_health_payload
from pseudolife_memory.service import MemoryService
from pseudolife_memory.storage.postgres import PostgresStorage
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

_WORLD = ("postgres", "latest major")
_URL = "https://example.com/postgres-release"


def _rows(conn, table: str, entity: str, attribute: str) -> list[tuple]:
    rows = conn.execute(
        f"SELECT value, status FROM {table} "  # noqa: S608 — fixed table names
        "WHERE entity = %s AND attribute = %s ORDER BY value",
        (entity, attribute),
    ).fetchall()
    # End the read's implicit transaction: its ACCESS SHARE lock would
    # otherwise block the next storage's ensure_schema DDL on this table.
    conn.commit()
    return rows


@pytest.fixture()
def seeded(pg_conn, pg_url, tmp_path):
    """A bank whose (svc, port) slot carries history (8080 superseded by
    8081) and which holds one world fact, written by a daemon that has
    since stopped (its storage closed, so the writer lease is free)."""
    seed = MemoryService(data_dir=tmp_path / "seed", database_url=pg_url)
    seed.cortex_write("svc", "port", "8080")
    seed.cortex_write("svc", "port", "8081")
    seed.world_write(*_WORLD, "18", source_url=_URL, source_quote="18 is out")
    seed._storage.close()  # noqa: SLF001 — the stopped daemon's exit
    facts = _rows(pg_conn, "facts", "svc", "port")
    world = _rows(pg_conn, "world_facts", *_WORLD)
    assert [v for v, _ in facts] == ["8080", "8081"]
    assert len(world) == 1
    return {"facts": facts, "world": world}


def _fail_once(monkeypatch, loader: str) -> list[str]:
    """Make ``PostgresStorage.<loader>`` raise on its first call only."""
    real = getattr(PostgresStorage, loader)
    calls: list[str] = []

    def flaky(self, *args, **kwargs):
        calls.append(loader)
        if len(calls) == 1:
            raise psycopg.OperationalError(f"simulated {loader} failure")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PostgresStorage, loader, flaky)
    return calls


def _attempt(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the outcome is the assertion
        return exc
    return None


@pytest.mark.parametrize(
    "loader", ["load_facts", "load_world_facts", "load_lessons", "load_entries"])
def test_failed_hydration_refuses_writes_and_keeps_durable_history(
        seeded, pg_conn, pg_url, tmp_path, monkeypatch, loader):
    calls = _fail_once(monkeypatch, loader)
    svc = MemoryService(data_dir=tmp_path / "daemon", database_url=pg_url)

    cortex_err = _attempt(svc.cortex_write, "svc", "port", "9090")
    world_err = _attempt(svc.world_write, *_WORLD, "19",
                         source_url=_URL, source_quote="19 is out")

    # The durable history of both pre-existing slots survives the failed
    # boot: nothing was written from a half-loaded store.
    assert _rows(pg_conn, "facts", "svc", "port") == seeded["facts"]
    assert _rows(pg_conn, "world_facts", *_WORLD) == seeded["world"]
    # No half-built store is left behind for a later call (or the autosave
    # loop) to serve or write from...
    assert (svc._cms, svc._cortex, svc._world, svc._lessons) == (  # noqa: SLF001
        None, None, None, None)
    assert svc.autosave_if_changed() is None
    # ...and both writes were refused as not-ready, not left to crash.
    assert isinstance(cortex_err, RuntimeError), cortex_err
    assert isinstance(world_err, RuntimeError), world_err
    assert "not ready" in str(world_err)
    assert calls == [loader]  # one hydration attempt: the retry backed off
    # Retryable, so reported as not-ready, never as the permanent refusal.
    assert "hydration failed" in svc._not_ready  # noqa: SLF001
    assert svc._init_refusal is None  # noqa: SLF001

    # Once the backoff expires the next call hydrates in full, and the
    # write supersedes the slot with its history intact.
    svc._init_retry_at = 0.0  # noqa: SLF001
    svc.cortex_write("svc", "port", "9090")
    after = dict(_rows(pg_conn, "facts", "svc", "port"))
    assert set(after) == {"8080", "8081", "9090"}
    assert after["9090"] == "current"
    assert (svc._not_ready, svc._init_refusal) == (None, None)  # noqa: SLF001


def test_not_ready_is_reported_on_health_until_a_retry_succeeds(
        seeded, pg_conn, pg_url, tmp_path, monkeypatch):
    _fail_once(monkeypatch, "load_facts")
    svc = MemoryService(data_dir=tmp_path / "daemon", database_url=pg_url)

    with pytest.raises(RuntimeError, match="cortex hydration failed"):
        svc._ensure_init()  # noqa: SLF001
    payload = _build_health_payload(svc, token_present=False)
    assert payload["status"] == "degraded"
    assert "cortex hydration failed" in payload["not_ready"]
    assert "simulated load_facts failure" in payload["not_ready"]
    # Not the permanent refusal: the shim exits on init_refusal, and a
    # client that starts during a retry window must still attach.
    assert "init_refusal" not in payload
    from pseudolife_memory import shim
    shim._accept_health("http://127.0.0.1:1", payload)  # attaches, no exit

    # Inside the backoff window a call is refused without touching storage.
    with pytest.raises(RuntimeError, match="not ready.*retry"):
        svc.search("anything")

    svc._init_retry_at = 0.0  # noqa: SLF001
    svc._ensure_init()  # noqa: SLF001
    payload = _build_health_payload(svc, token_present=False)
    assert payload["status"] == "ok"
    assert "not_ready" not in payload and "init_refusal" not in payload


def test_warmup_keeps_retrying_a_failed_store_build(
        seeded, pg_url, tmp_path, monkeypatch):
    """A daemon nobody is calling still recovers: warmup retries a failed
    store build once each backoff window expires, rather than leaving the
    bank refused until a caller (or the 5-minute reaper) happens by."""
    import pseudolife_memory.service as service_module

    monkeypatch.setattr(service_module, "INIT_RETRY_BASE_SECONDS", 0.05)
    calls = _fail_once(monkeypatch, "load_facts")
    svc = MemoryService(data_dir=tmp_path / "daemon", database_url=pg_url)

    svc.warmup()

    assert calls == ["load_facts", "load_facts"]
    assert svc._cms is not None  # noqa: SLF001
    assert svc._not_ready is None  # noqa: SLF001


def test_warmup_stops_retrying_once_the_backoff_is_at_its_cap(
        seeded, pg_url, tmp_path, monkeypatch):
    """A failure that never clears (say, an unreadable row) must not have
    warmup re-hydrate the whole bank every minute forever. Warmup gives up
    once the backoff reaches its cap; callers and the session reaper keep
    retrying at their own pace."""
    import threading

    import pseudolife_memory.service as service_module

    monkeypatch.setattr(service_module, "INIT_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(service_module, "INIT_RETRY_MAX_SECONDS", 0.08)
    attempts: list[int] = []

    def always_failing(self, *args, **kwargs):
        attempts.append(1)
        raise psycopg.OperationalError("simulated unreadable row")

    monkeypatch.setattr(PostgresStorage, "load_lessons", always_failing)
    svc = MemoryService(data_dir=tmp_path / "daemon", database_url=pg_url)

    warmup = threading.Thread(target=svc.warmup, daemon=True)
    warmup.start()
    warmup.join(timeout=30)

    assert not warmup.is_alive(), "warmup kept retrying a permanent failure"
    # Backoffs 0.01, 0.02, 0.04, then 0.08 = the cap: four attempts.
    assert len(attempts) == 4
    assert svc._not_ready is not None  # noqa: SLF001 — still reported


def test_abandoned_attempt_is_collected_before_the_next_one(
        pg_conn, pg_url, tmp_path, monkeypatch):
    """The half-built entry store must be gone before a retry builds a new
    one. Collecting inside the failed attempt cannot free it: the exception
    still in flight holds the ``hydrate_cms`` frame, and with it the store."""
    import weakref

    from pseudolife_memory.storage import sync as sync_module

    real = sync_module.hydrate_cms
    first: list = []
    alive_at_retry: list = []

    def flaky(cms, storage):
        if not first:
            first.append(weakref.ref(cms))
            raise psycopg.OperationalError("simulated load failure")
        alive_at_retry.append(first[0]() is not None)
        return real(cms, storage)

    monkeypatch.setattr(sync_module, "hydrate_cms", flaky)
    svc = MemoryService(data_dir=tmp_path / "daemon", database_url=pg_url)
    with pytest.raises(RuntimeError, match="entry hydration failed"):
        svc._ensure_init()  # noqa: SLF001
    svc._init_retry_at = 0.0  # noqa: SLF001
    svc._ensure_init()  # noqa: SLF001

    assert alive_at_retry == [False]


def test_backoff_grows_between_consecutive_failures_and_resets_on_success(
        seeded, pg_url, tmp_path, monkeypatch):
    real = PostgresStorage.load_lessons
    failing = {"on": True}

    def flaky(self, *args, **kwargs):
        if failing["on"]:
            raise psycopg.OperationalError("simulated persistent failure")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PostgresStorage, "load_lessons", flaky)
    svc = MemoryService(data_dir=tmp_path / "daemon", database_url=pg_url)

    delays = []
    for _ in range(4):
        with pytest.raises(RuntimeError, match="lesson hydration failed"):
            svc._ensure_init()  # noqa: SLF001
        delays.append(svc._init_backoff_s)  # noqa: SLF001
        svc._init_retry_at = 0.0  # noqa: SLF001 — let the window expire
    assert delays == sorted(delays) and delays[0] < delays[-1]

    failing["on"] = False
    svc._ensure_init()  # noqa: SLF001
    assert svc._cms is not None  # noqa: SLF001
    assert (svc._init_retry_at, svc._init_backoff_s) == (0.0, 0.0)  # noqa: SLF001
