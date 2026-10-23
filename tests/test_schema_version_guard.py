"""``ensure_schema`` refuses a bank stamped by a newer build, and leaves
the stamp alone.

Its trailing ``schema_version`` upsert used to rewrite any stamp to this
build's version, so starting an older daemon against a bank a newer
release had upgraded stamped it down with no warning: a rollback crossed
a schema boundary silently (found by #896's activation rehearsal, where a
bank stamped 57 came back as 56). It now refuses before any DDL runs, as
the Rust daemon does, with the same message, and a stamp that is not a
positive integer is refused the same way. A missing table or row, an
older stamp and the current one still open.

Refusal cases on the bench database run inside the fixture connection's
own uncommitted transaction (``ensure_schema``'s ``conn.transaction()``
becomes a savepoint there), so they never leave the shared test database
stamped by a "newer build". The one test that goes through
``PostgresStorage``, the daemon's entry point, must commit the stamp and
puts it back in ``finally``: only a killed process can leave it, and the
next ``pg_conn`` setup then refuses with this message, naming the cause.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pseudolife_memory.storage.schema import (
    SCHEMA_META_VERSION,
    _refuse_on_future_or_malformed_version,
    _schema_version_refusal,
    ensure_schema,
)
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

NEWER = SCHEMA_META_VERSION + 1
MALFORMED = ['"55"', "null", "true", "1.5", "0", "-1", '{"v": 56}', str(2**63)]
# A table ensure_schema creates on every start: absent after a refused call
# proves the refusal came before any DDL.
_PROBE_TABLE = "store_decisions"


def _stamp(conn):
    return conn.execute(
        "SELECT value::text FROM meta WHERE key = 'schema_version'").fetchone()[0]


def _set_stamp(conn, value: str) -> None:
    conn.execute("UPDATE meta SET value = %s::jsonb WHERE key = 'schema_version'",
                 (value,))


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()[0] is not None


@pytest.mark.parametrize("stamp", [str(NEWER), *MALFORMED])
def test_a_refused_stamp_is_left_as_it_is_and_no_ddl_runs(pg_conn, stamp):
    try:
        _set_stamp(pg_conn, stamp)
        pg_conn.execute(f"DROP TABLE {_PROBE_TABLE}")
        before = _stamp(pg_conn)
        with pytest.raises(RuntimeError, match="Refusing to start: bank schema"):
            ensure_schema(pg_conn)
        assert _stamp(pg_conn) == before
        assert not _table_exists(pg_conn, _PROBE_TABLE)
    finally:
        pg_conn.rollback()
    assert _stamp(pg_conn) == str(SCHEMA_META_VERSION)
    assert _table_exists(pg_conn, _PROBE_TABLE)


def test_a_newer_build_is_named_before_a_dimension_it_moved(pg_conn):
    """A newer build may also have moved the embedding dimension: the
    refusal names the newer build, the true cause, not the dimension
    migration script, which does not apply to a newer bank."""
    try:
        _set_stamp(pg_conn, str(NEWER))
        pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
        pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding TYPE vector(384) USING NULL")
        with pytest.raises(RuntimeError) as raised:
            ensure_schema(pg_conn)
        assert "is newer than this build" in str(raised.value)
        assert "migrate_embeddings" not in str(raised.value)
    finally:
        pg_conn.rollback()


def test_the_daemon_entry_point_refuses_a_newer_bank_and_names_the_fix(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    _set_stamp(pg_conn, str(NEWER))
    pg_conn.commit()
    try:
        with pytest.raises(RuntimeError) as raised:
            PostgresStorage(pg_url).close()
        message = str(raised.value)
        # The Rust daemon's wording, which the parity and rehearsal
        # harnesses match on, with the true cause and both ways out.
        assert message.startswith(
            f"Refusing to start: bank schema version {NEWER} is newer than "
            f"this build's schema version {SCHEMA_META_VERSION}.")
        assert "pseudolife-mcp update" in message
        assert "ops/restore.ps1 -Apply -BackupFile <dump>" in message
        assert "ops/restore.sh --apply --backup-file <dump>" in message
        assert _stamp(pg_conn) == str(NEWER)
    finally:
        pg_conn.rollback()
        _set_stamp(pg_conn, str(SCHEMA_META_VERSION))
        pg_conn.commit()


@pytest.mark.parametrize("stamp", ["1", "8", str(SCHEMA_META_VERSION - 1),
                                   str(SCHEMA_META_VERSION)])
def test_an_older_or_current_stamp_still_opens_and_is_upgraded(pg_conn, stamp):
    """The near miss: every bank this build may serve keeps opening."""
    try:
        _set_stamp(pg_conn, stamp)
        ensure_schema(pg_conn)
        assert _stamp(pg_conn) == str(SCHEMA_META_VERSION)
    finally:
        pg_conn.rollback()


def test_an_unstamped_bank_still_opens_and_is_stamped(pg_conn):
    try:
        pg_conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        ensure_schema(pg_conn)
        assert _stamp(pg_conn) == str(SCHEMA_META_VERSION)
    finally:
        pg_conn.rollback()


class _Cursor:
    """``to_regclass('public.meta')`` and the stamp read, scripted."""

    def __init__(self, *rows):
        self.rows = list(rows)

    def execute(self, *_args, **_kwargs) -> None:
        pass

    def fetchone(self):
        return self.rows.pop(0)


@pytest.mark.parametrize("rows", [[(None,)], [("meta",), None]])
def test_a_fresh_install_or_a_missing_row_has_nothing_to_refuse(rows):
    _refuse_on_future_or_malformed_version(_Cursor(*rows))


def test_the_refusal_decides_on_the_stamp_alone():
    assert _schema_version_refusal("1") is None
    assert _schema_version_refusal(str(SCHEMA_META_VERSION)) is None
    assert "is newer than this build" in _schema_version_refusal(str(NEWER))
    for stamp in MALFORMED + ["", "not json"]:
        assert "must be a positive integer" in _schema_version_refusal(stamp), stamp


_RUST = Path(__file__).resolve().parents[1] / "rust" / "daemon" / "src" / "storage" / "schema.rs"


def test_the_rust_daemon_refuses_with_the_same_text():
    """schema.rs's ``version_message_matches_python`` test holds both
    messages for the stamps 999999 and 0 (string continuations joined, the
    build's version as ``{SCHEMA_META_VERSION}``), so a wording change on
    either side fails one of the two tests."""
    rust = re.sub(r"\\\n\s*", "", _RUST.read_text(encoding="utf-8"))
    newer = _schema_version_refusal("999999").replace(
        f"version {SCHEMA_META_VERSION}.", "version {SCHEMA_META_VERSION}.")
    assert newer in rust
    assert _schema_version_refusal("0") in rust
