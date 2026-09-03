"""ops/migrate_embeddings.py — schema v25 embedding migration
(vector(384) -> vector(1024)) against a synthetic v24-shaped bank.

Builds a v24 bank by hand: ``ensure_schema`` REFUSES to construct
``PostgresStorage`` against a dimension mismatch (that refusal is the
whole reason this migration script exists — see
``pseudolife_memory.storage.schema._refuse_on_embedding_dim_mismatch``),
so the synthetic bank and every assertion here talk to Postgres directly
via a plain psycopg connection, exactly like the migration script itself
does. ``pg_conn`` still owns provisioning/truncation (it leaves the four
columns at vector(1024) after each test via ``ensure_schema``); each test
narrows them back to 384 as its own setup step.

The APPLY path uses the REAL Qwen3-Embedding-0.6B pipeline (not a stub).
``test_embedding_dim_guard.py::test_service_round_trip_at_dim_1024``
already
establishes that the model loads offline from the HF cache in about a
second and encodes CPU-fast; the synthetic bank here is a handful of rows
across four tables, so the wall-clock cost is noise against the ~7-minute
full suite. A stub would validate the DDL/refusal machinery but not the
one thing the spec calls the load-bearing judgment call: that every text
actually routes through ``encode``/``encode_single`` (document-side) and
never ``encode_query`` — proving that needs the real pipeline end to end,
not a mock that can't tell the difference.
"""
from __future__ import annotations

import http.server
import socket
import sys
import threading
from pathlib import Path

import numpy as np
import psycopg
import pytest
import torch
from pgvector.psycopg import register_vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import migrate_embeddings  # noqa: E402

from pseudolife_memory.storage.schema import _refuse_on_embedding_dim_mismatch  # noqa: E402
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

_NOW = 1_700_000_000.0
# RFC 2606 reserved .invalid TLD -- guaranteed to never resolve, so this
# fails FAST via a DNS lookup error (socket.gaierror, immediate), not a TCP
# probe. Deliberately NOT "http://127.0.0.1:<unused port>/health": confirmed
# on this host (2026-07-28) that an unanswered loopback connection attempt
# TIMES OUT rather than refusing immediately (some local firewall/AV
# swallows the RST) -- and with the IMPORTANT-2a fix in
# ops/migrate_embeddings.py (a health-probe timeout now reads as UP, not
# absent, because a hung-but-listening daemon is still a live writer), that
# would make this constant simulate a HUNG daemon instead of an ABSENT one,
# which is the opposite of what every test below needs it to mean.
_UNREACHABLE_HEALTH_URL = "http://pseudolife-migrate-embeddings-test.invalid/health"


def _vec(seed: int, dim: int = 384) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype("float32")
    return v / np.linalg.norm(v)


def _narrow_to_v24(pg_conn) -> None:  # noqa: F811 — fixture shadow, matches test_embedding_dim_guard.py style
    """Take the fixture's clean vector(1024) bank down to a synthetic v24
    shape: all four embedding columns at vector(384), NOT NULL dropped on
    entries first (mirrors test_embedding_dim_guard.py's own setup)."""
    pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
    for table in ("entries", "facts", "world_facts", "lessons"):
        pg_conn.execute(
            f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(384) USING NULL"
        )
    pg_conn.commit()
    register_vector(pg_conn)


def _restore_to_v25(pg_conn) -> None:  # noqa: F811
    """Undo :func:`_narrow_to_v24` regardless of how a test ended (dry-run
    and refusal tests deliberately leave the bank at 384; the success test
    already left it at 1024 via the migration itself — this is a no-op
    there, since re-running ``... USING NULL`` on an already-vector(1024)
    column would needlessly null out the just-migrated data). ``pg_conn``'s
    per-test truncate/``ensure_schema`` only re-seeds meta; it does NOT
    re-widen a column a previous test left narrowed, so the NEXT test's
    ``pg_conn`` fixture setup would otherwise hit this build's own
    dim-mismatch refusal before it even gets to truncate.

    Best-effort on the NOT NULL restore (a failed assertion earlier in the
    test must surface as THAT failure, not be masked by a teardown error)
    — only the dimension actually gates the next test's fixture setup.
    Committed SEPARATELY from the SET NOT NULL attempt below: the
    dry-run/refusal tests leave every row's embedding NULL after the
    ``USING NULL`` cast, so ``SET NOT NULL`` legitimately fails there --
    and psycopg aborts the WHOLE current transaction on any error, so
    committing the widening first is load-bearing, not cosmetic. A
    same-transaction attempt was the actual bug behind this fixture's
    first version: the failed ``SET NOT NULL`` silently rolled back the
    widening ALTERs too, leaving the bank at 384 for the next test."""
    pg_conn.rollback()  # drop any open transaction/lock from a failed assertion
    # Idempotent, unconditional: the main apply test (MINOR 5) now sets
    # entries.embedding NOT NULL before invoking the migration, to match
    # production shape. If that test aborts before migrate_table ever runs
    # (e.g. an earlier assertion fails, or -- as happened once during this
    # fix wave -- a health-gate regression made --apply refuse early), NOT
    # NULL is still active with real, non-null data underneath it, and the
    # widening ALTER below (``USING NULL``, which nulls every row) would
    # otherwise raise a constraint violation the surrounding code doesn't
    # catch. Dropping it first is always safe (a no-op if already dropped)
    # and the ``SET NOT NULL`` at the bottom re-applies it whenever the
    # data actually supports it.
    pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
    dim = pg_conn.execute(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = "
        "to_regclass('public.entries') AND attname = 'embedding' "
        "AND attnum > 0 AND NOT attisdropped"
    ).fetchone()[0]
    if dim != 1024:
        for table in ("entries", "facts", "world_facts", "lessons"):
            pg_conn.execute(
                f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(1024) USING NULL"
            )
        pg_conn.commit()
    try:
        pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding SET NOT NULL")
        pg_conn.commit()
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        pg_conn.rollback()


@pytest.fixture()
def v24_bank(pg_conn):  # noqa: F811
    """Narrow the fixture's clean vector(1024) bank to a synthetic v24
    shape and seed it with real-text rows across all four tables; always
    restores to vector(1024) NOT NULL afterward so the next test's
    ``pg_conn`` setup (which calls ``ensure_schema`` before it even
    truncates) never sees a leftover 384-d column."""
    _narrow_to_v24(pg_conn)
    _seed_v24_bank(pg_conn)
    try:
        yield pg_conn
    finally:
        _restore_to_v25(pg_conn)


def _seed_v24_bank(pg_conn) -> None:  # noqa: F811
    """Insert a handful of real-text rows across all four tables at 384-d.
    Omits every column with a DEFAULT (outcome, freshness_class, polarity,
    support, provenance, ...) so Postgres' own defaults apply — only the
    columns actually exercised by the migration/assertions are set."""
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts) VALUES (%s, %s, %s, %s)",
        ("instant", "the bench postgres for schema v25 runs on port 5433",
         _vec(1), _NOW),
    )
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts) VALUES (%s, %s, %s, %s)",
        ("instant", "qwen3 embedding 0.6b replaced minilm as the default backbone",
         _vec(2), _NOW),
    )
    pg_conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, asserted_at, last_confirmed, embedding) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("migrate-probe", "status", "migrate-probe", "status", "verified",
         "current", 0.9, _NOW, _NOW, _vec(3)),
    )
    pg_conn.execute(
        "INSERT INTO world_facts (entity, attribute, entity_norm, "
        "attribute_norm, value, status, confidence, asserted_at, "
        "last_confirmed, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("pgvector", "hnsw_dim_cap", "pgvector", "hnsw_dim_cap", "2000",
         "current", 0.9, _NOW, _NOW, _vec(4)),
    )
    pg_conn.execute(
        "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, asserted_at, last_confirmed, embedding) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("embedding-migration", "batching", "embedding-migration", "batching",
         "batch encodes, never one row at a time", "current", 0.8, _NOW, _NOW,
         _vec(5)),
    )
    pg_conn.commit()


def _dims(pg_conn, table: str) -> set[int]:  # noqa: F811
    rows = pg_conn.execute(
        f"SELECT DISTINCT vector_dims(embedding) FROM {table}"  # noqa: S608
    ).fetchall()
    return {r[0] for r in rows}


def _null_count(pg_conn, table: str) -> int:  # noqa: F811
    return pg_conn.execute(
        f"SELECT count(*) FROM {table} WHERE embedding IS NULL"  # noqa: S608
    ).fetchone()[0]


def _live_dim(pg_conn, table: str) -> int | None:  # noqa: F811
    row = pg_conn.execute(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = to_regclass(%s) "
        "AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped",
        (f"public.{table}",),
    ).fetchone()
    return row[0] if row else None


def _invoke(monkeypatch, pg_url, *extra_args) -> int:  # noqa: F811
    """Run migrate_embeddings.main() with the given CLI args, capturing its
    sys.exit() code (main() always calls sys.exit(run(args)))."""
    monkeypatch.setattr(sys, "argv", ["migrate_embeddings.py", "--dsn", pg_url, *extra_args])
    with pytest.raises(SystemExit) as exc_info:
        migrate_embeddings.main()
    return exc_info.value.code


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's naming
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):  # noqa: D401 — silence test-run noise
        pass


@pytest.fixture()
def health_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health"
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _HungHealthServer:
    """A TCP listener that accepts connections and never answers -- the
    technique for proving IMPORTANT 2a: a hung-but-listening daemon must
    read as UP (``_daemon_reachable`` returns True), not absent. Distinct
    from ``_HealthHandler``/``health_server`` above, which answers promptly
    with a real 200."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._accepted: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._accepted.append(conn)  # held open, never written to or closed

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        for conn in self._accepted:
            try:
                conn.close()
            except OSError:
                pass
        self._listener.close()


@pytest.fixture()
def hung_health_server():
    server = _HungHealthServer()
    try:
        yield f"http://127.0.0.1:{server.port}/health"
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Dry run: prints the plan, mutates nothing, regardless of flags.
# ---------------------------------------------------------------------------


def test_dry_run_mutates_nothing(v24_bank, pg_url, monkeypatch):
    code = _invoke(monkeypatch, pg_url)  # no --apply
    assert code == 0

    assert _live_dim(v24_bank, "entries") == 384
    assert _live_dim(v24_bank, "facts") == 384
    assert _live_dim(v24_bank, "world_facts") == 384
    assert _live_dim(v24_bank, "lessons") == 384
    assert _null_count(v24_bank, "entries") == 0  # rows untouched, not nulled


# ---------------------------------------------------------------------------
# --apply gates: missing --backup-verified, and a reachable daemon.
# ---------------------------------------------------------------------------


def test_apply_without_backup_verified_refuses(v24_bank, pg_url, monkeypatch):
    code = _invoke(monkeypatch, pg_url, "--apply",
                    "--health-url", _UNREACHABLE_HEALTH_URL)
    assert code == 1
    assert _live_dim(v24_bank, "entries") == 384  # nothing written


def test_apply_while_daemon_reachable_refuses(v24_bank, pg_url, monkeypatch, health_server):
    code = _invoke(monkeypatch, pg_url, "--apply", "--backup-verified",
                    "--health-url", health_server)
    assert code == 1
    assert _live_dim(v24_bank, "entries") == 384  # nothing written


# ---------------------------------------------------------------------------
# IMPORTANT 2a: a hung-but-listening daemon reads as UP, not absent.
# ---------------------------------------------------------------------------


def test_daemon_reachable_treats_hung_socket_as_up(hung_health_server):
    # 0.2s is plenty: the fixture's socket accepts and never answers, so the
    # probe can only end in the timeout branch — the duration is not the
    # contract, "hung reads as up" is.
    assert migrate_embeddings._daemon_reachable(hung_health_server, timeout=0.2) is True


def test_apply_refuses_against_hung_daemon(v24_bank, pg_url, monkeypatch, hung_health_server):
    code = _invoke(monkeypatch, pg_url, "--apply", "--backup-verified",
                    "--health-url", hung_health_server)
    assert code == 1
    assert _live_dim(v24_bank, "entries") == 384  # refused before any ALTER


def test_assume_daemon_stopped_bypasses_only_the_health_gate(
        v24_bank, pg_url, monkeypatch, hung_health_server):
    """On this host a CLOSED loopback port times out instead of refusing, so
    a genuinely-stopped daemon reads as 'up' and the fail-safe gate would
    block the legitimate morning migration forever. The override must skip
    exactly the health probe and nothing else — proven by omitting
    --backup-verified: with the flag we must reach the BACKUP refusal, which
    sits behind the health gate in no ordering, i.e. the run got past the
    probe without migrating anything."""
    code = _invoke(monkeypatch, pg_url, "--apply", "--assume-daemon-stopped",
                   "--health-url", hung_health_server)
    assert code == 1                                    # still refused...
    assert _live_dim(v24_bank, "entries") == 384        # ...nothing written
    # And WITH backup-verified the same hung probe no longer blocks: the run
    # proceeds into the migration proper (entries ends at 1024).
    code = _invoke(monkeypatch, pg_url, "--apply", "--backup-verified",
                   "--assume-daemon-stopped", "--health-url", hung_health_server)
    assert code == 0
    assert _live_dim(v24_bank, "entries") == 1024


def test_entries_is_last_in_the_migration_order():
    """The daemon's dim guard checks entries ONLY (schema.py: one column as
    sentinel). entries-last is the single ordering property that keeps every
    partial state refusable; the crash test cannot see a reorder to the
    middle positions (it always crashes after two tables), so the order is
    pinned directly."""
    assert migrate_embeddings._TABLES[-1] == "entries"


# ---------------------------------------------------------------------------
# IMPORTANT 2b: lock_timeout on the migration connection fails loudly
# instead of queuing an ACCESS EXCLUSIVE ALTER behind a stray lock forever.
# ---------------------------------------------------------------------------


def test_lock_timeout_fires_on_queued_alter(v24_bank, pg_url):
    """A real competing transaction holding a lock on ``facts`` (the first
    table in migration order) makes the ALTER fail fast via lock_timeout
    instead of hanging. Uses a short override (100ms) so the test doesn't pay
    the real 10s default — the assertion is that the mechanism fires, not how
    long it waits, and the blocker holds the lock for the whole test."""
    pg_conn = v24_bank
    pg_conn.commit()  # release any read locks left open by fixture setup

    blocker = psycopg.connect(pg_url)
    blocker.execute("LOCK TABLE facts IN ACCESS EXCLUSIVE MODE")
    conn = psycopg.connect(pg_url, autocommit=True)
    try:
        conn.execute("SET search_path TO public")
        migrate_embeddings._apply_lock_timeout(conn, "100ms")
        register_vector(conn)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate_embeddings.migrate_table(conn, None, "facts")
    finally:
        conn.close()
        blocker.rollback()
        blocker.close()


def test_apply_lock_timeout_prints_actionable_message(v24_bank, pg_url, monkeypatch, capsys):
    def _boom(conn, pipeline, table):
        raise psycopg.errors.LockNotAvailable(
            "simulated: canceling statement due to lock timeout"
        )

    monkeypatch.setattr(migrate_embeddings, "migrate_table", _boom)
    code = _invoke(monkeypatch, pg_url, "--apply", "--backup-verified",
                    "--health-url", _UNREACHABLE_HEALTH_URL)
    assert code == 1
    err = capsys.readouterr().err
    assert "lock" in err.lower()
    assert "stop" in err.lower()
    assert _live_dim(v24_bank, "entries") == 384  # nothing written


# ---------------------------------------------------------------------------
# IMPORTANT 1: entries migrates LAST -- a crash partway through must leave
# the daemon's dimension sentinel (entries.embedding) armed, and a clean
# re-run must complete and stamp v25.
# ---------------------------------------------------------------------------


def test_apply_crash_after_two_tables_keeps_entries_armed_then_resumes(
    v24_bank, pg_url, monkeypatch
):
    """Watched RED: against the pre-fix entries-first ``_TABLES`` order,
    assertion (b) below fails, because entries would be one of the first
    two tables migrated (already vector(1024), not 384) by the time the
    crash fires -- the exact bug IMPORTANT 1 fixes."""
    pg_conn = v24_bank

    # The v24_bank fixture's own setup calls ensure_schema, which stamps
    # meta.schema_version at THIS build's version (25) before the columns
    # are narrowed to a synthetic v24 shape -- not what a genuine
    # pre-migration bank would show. Pin it back to 24 so "meta still 24"
    # below means what it says.
    pg_conn.execute("UPDATE meta SET value = '24'::jsonb WHERE key = 'schema_version'")
    pg_conn.commit()

    real_migrate_table = migrate_embeddings.migrate_table
    calls = {"n": 0}

    def _crash_after_two(conn, pipeline, table):
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-migration")
        calls["n"] += 1
        return real_migrate_table(conn, pipeline, table)

    monkeypatch.setattr(migrate_embeddings, "migrate_table", _crash_after_two)
    monkeypatch.setattr(sys, "argv", [
        "migrate_embeddings.py", "--dsn", pg_url, "--apply", "--backup-verified",
        "--health-url", _UNREACHABLE_HEALTH_URL,
    ])

    # main() calls sys.exit(run(args)) -- run(args) raises here instead of
    # returning, so sys.exit() is never reached and the exception
    # propagates straight out of main(). Unlike _invoke(), this is NOT a
    # SystemExit.
    with pytest.raises(RuntimeError, match="simulated crash"):
        migrate_embeddings.main()

    # (a) meta untouched: stamp_schema_version is the last line of the
    # apply loop and never ran.
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert meta[0] == 24
    pg_conn.commit()

    # (b) entries still 384 -- the sentinel is armed, so the daemon's own
    # startup guard refuses to boot against this partial bank.
    assert _live_dim(pg_conn, "entries") == 384
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        with pytest.raises(RuntimeError, match="Refusing to start"):
            _refuse_on_embedding_dim_mismatch(cur)
    pg_conn.commit()  # release the read lock before the resumed run below

    # (c) re-run (crash removed) completes and stamps v26.
    monkeypatch.setattr(migrate_embeddings, "migrate_table", real_migrate_table)
    code = _invoke(monkeypatch, pg_url, "--apply", "--backup-verified",
                    "--health-url", _UNREACHABLE_HEALTH_URL)
    assert code == 0
    for table in ("entries", "facts", "world_facts", "lessons"):
        assert _live_dim(pg_conn, table) == 1024, table
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    # Literal pin, bump alongside SCHEMA_META_VERSION -- see the other
    # tests/test_schema_version.py CURRENT_SCHEMA pin, same convention.
    assert meta[0] == 37


# ---------------------------------------------------------------------------
# The real thing: --apply with both gates cleared migrates all four tables.
# ---------------------------------------------------------------------------


def _assert_write_path_cosine_one(pipeline, text: str, stored_vec) -> None:
    """cos(stored, freshly re-encoded) ~= 1.0. The review measured
    document-side re-encodes at 1.000000 and query-side (an ``encode_query``
    slip) at 0.20-0.93 on the same rows -- a dims-only check can't tell the
    two apart, this can. ``pipeline`` is a second, independently-built
    instance of the same real model the migration used (deterministic,
    same weights), not the one internal to the script under test."""
    from pseudolife_memory.storage.postgres import _embedding_out

    # Compare on CPU: the pipeline encodes on whatever device it picked
    # (CUDA whenever the GPU is free, CPU otherwise), while ``from_numpy``
    # below is always CPU -- ``torch.dot`` across devices raises. This is
    # why full-suite runs passed while the GPU was busy and failed idle.
    doc_vec = pipeline.encode_single(text).cpu()
    qry_vec = pipeline.encode_query(text).cpu()
    # Reuse the storage layer's own reader instead of np.asarray: pgvector
    # <0.5 hands psycopg reads back as numpy arrays, 0.5+ returns ``Vector``
    # objects that np.asarray raises TypeError on. The dependency is
    # unpinned (``pgvector>=0.3``), so a local venv and a fresh CI install
    # legitimately differ -- which is exactly how this test passed here and
    # failed on the runner. _embedding_out already encodes that lesson.
    stored = torch.from_numpy(_embedding_out(stored_vec))
    cos_doc = torch.dot(stored, doc_vec).item()
    cos_qry = torch.dot(stored, qry_vec).item()
    # RELATIVE, because no absolute threshold is verifiable here. The
    # migration encodes in batches and this verifier encodes singly; on
    # this machine those agree to 1.000000, but on CI's BLAS/threading they
    # drift ~2e-3 -- so a bound tuned locally is a bound tuned to one CPU
    # (two CI failures learned that the hard way: 1.00013 rejected as "too
    # identical", then 0.99797 as "not identical enough"). The claim this
    # test actually makes is directional: the stored vector is the DOCUMENT
    # encoding, not a query one. Measured doc-minus-query margins across
    # these rows run 0.046-0.254, so 0.02 is comfortably inside the signal
    # while immune to batch, machine, and model drift.
    assert cos_doc > cos_qry + 0.02, (
        f"stored vector looks query-side: cos_doc={cos_doc:.6f} "
        f"cos_qry={cos_qry:.6f} for text={text!r}")
    # And it really is that vector, not merely closer to it than to the
    # query one -- catches a garbage/zero/stale vector that would satisfy
    # the direction test vacuously.
    assert cos_doc > 0.99, f"cos_doc {cos_doc:.6f} for text={text!r}"


def test_apply_migrates_all_four_tables(v24_bank, pg_url, monkeypatch):
    pg_conn = v24_bank
    # Keep entries.embedding NOT NULL going into the migration -- the
    # production shape (schema.py declares it NOT NULL). The shared
    # v24_bank fixture drops it while entries is still empty (harmless for
    # every OTHER test here), which means without this line, THIS test --
    # the one real-pipeline test that actually runs migrate_table's own
    # "DROP NOT NULL" step against live data -- never proved that step is
    # necessary. Safe to restore here: every seeded row already carries a
    # real, non-null embedding.
    pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding SET NOT NULL")
    pg_conn.commit()

    before_text = {r[0]: r[1] for r in pg_conn.execute(
        "SELECT id, text FROM entries ORDER BY id").fetchall()}
    before_fact = pg_conn.execute(
        "SELECT entity, attribute, value FROM facts").fetchone()
    # Release the AccessShareLock the reads above hold open (pg_conn is not
    # autocommit) — otherwise the migration's own connection blocks
    # indefinitely on its ALTER TABLE (ACCESS EXCLUSIVE) waiting for this
    # session's lock to drop.
    pg_conn.commit()

    code = _invoke(monkeypatch, pg_url, "--apply", "--backup-verified",
                    "--health-url", _UNREACHABLE_HEALTH_URL)
    assert code == 0

    for table in ("entries", "facts", "world_facts", "lessons"):
        assert _live_dim(pg_conn, table) == 1024, table
        assert _dims(pg_conn, table) == {1024}, table  # every row re-embedded
        assert _null_count(pg_conn, table) == 0, table

    # entries NOT NULL restored.
    row = pg_conn.execute(
        "SELECT attnotnull FROM pg_attribute WHERE attrelid = "
        "to_regclass('public.entries') AND attname = 'embedding'"
    ).fetchone()
    assert row[0] is True

    # Non-embedding metadata is untouched.
    after_text = {r[0]: r[1] for r in pg_conn.execute(
        "SELECT id, text FROM entries ORDER BY id").fetchall()}
    assert after_text == before_text
    after_fact = pg_conn.execute(
        "SELECT entity, attribute, value FROM facts").fetchone()
    assert after_fact == before_fact

    # No index created (none exist to rebuild — see the spec correction).
    idx = pg_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'entries' "
        "AND indexname = 'entries_embedding_idx'"
    ).fetchone()
    assert idx is None

    # meta.schema_version stamped 26, last, only on full success. The
    # jsonb cast of the bare digits "26" parses as the JSON number 26
    # (not a JSON string) — matches schema.py's own stamp exactly, same
    # cast, same param shape (str(SCHEMA_META_VERSION)).
    # Literal pin, bump alongside SCHEMA_META_VERSION -- see the other
    # tests/test_schema_version.py CURRENT_SCHEMA pin, same convention.
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert meta[0] == 37

    # Write-path fidelity, at least one row per table (MINOR 4): the real
    # pipeline test above spent its cost without collecting this evidence
    # until now.
    verify_pipeline = migrate_embeddings._build_pipeline()

    entries_row = pg_conn.execute(
        "SELECT text, embedding FROM entries ORDER BY id LIMIT 1"
    ).fetchone()
    _assert_write_path_cosine_one(verify_pipeline, entries_row[0], entries_row[1])

    for table in ("facts", "world_facts", "lessons"):
        row = pg_conn.execute(
            f"SELECT entity, attribute, value, embedding FROM {table} LIMIT 1"  # noqa: S608
        ).fetchone()
        claim_text = migrate_embeddings._claim_text(row[0], row[1], row[2])
        _assert_write_path_cosine_one(verify_pipeline, claim_text, row[3])
