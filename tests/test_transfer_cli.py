"""Logical export/import (`pseudolife-mcp export` / `import`) — the portable
transfer layer over the bank.

pg_dump (ops/backup.* / `pseudolife-mcp backup`) stays the physical backup;
this pair moves the *knowledge* as a ZIP of JSONL tables + manifest, decoupled
from Postgres major, tier, and (additively) schema version. The tests here pin
the contract:

* a full-fidelity roundtrip through an empty bank (ids, embeddings, HLC
  stamps, JSONB, timestamptz, sequences);
* the table roster covers the whole schema — every table is explicitly
  exported or explicitly excluded, so a future table forces a decision;
* import refuses a non-empty bank, a bank other connections hold (a running
  daemon), an unknown column (export from a newer build), and an embedding
  dimension mismatch;
* build-owned/transient meta (schema_version, extension lineage markers,
  the active-session pointer) never travels.
"""

from __future__ import annotations

import json
import zipfile
from contextlib import contextmanager

import psycopg
import pytest

from pseudolife_memory.storage.schema import (
    BENCH_RESET_TABLES,
    SCHEMA_META_VERSION,
    ensure_schema,
)
from pseudolife_memory.transfer_cli import (
    _MUST_BE_EMPTY,
    EXCLUDED_TABLES,
    EXPORTED_TABLES,
    TransferError,
    perform_export,
    perform_import,
)
from tests.pg_fixtures import pg_url  # noqa: F401  (fixture)

_DIM = 1024
# Messy leading components on purpose: all-0/1 vectors round-trip exactly
# through ANY float text rendering, so they cannot detect precision loss —
# these need full float4 shortest-round-trip digits to survive.
_VEC = "[" + ",".join(
    ["0.1234567", "1e-08", "-3.4028235e+38", "42.42424242"]
    + ["0"] * (_DIM - 5) + ["1"]) + "]"


@contextmanager
def _bank(pg_url):
    """A connection to the bench bank with schema ensured, every table
    truncated, and leaked backends from other suites reaped — managed
    explicitly (NOT the pg_conn fixture) so tests can CLOSE it before
    import runs: perform_import refuses a database other connections hold,
    and the fixture connection would trip that guard.
    """
    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        conn.commit()
        ensure_schema(conn)
        _truncate_all(conn)
        yield conn
        conn.commit()


def _truncate_all(conn) -> None:
    conn.execute(
        "TRUNCATE " + ", ".join(BENCH_RESET_TABLES)
        + " RESTART IDENTITY CASCADE"
    )
    # Re-seed the one meta row the truncate wiped (mirrors pg_fixtures).
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (str(SCHEMA_META_VERSION),),
    )
    conn.commit()


def _seed_bank(conn) -> None:
    """One row (or a small chain) in every exported table, exercising the
    tricky column types: vector, JSONB, timestamptz, HLC stamps, NULLs."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO episodes (id, title, hint, started_at, ended_at, "
        "closed_by_new_start, session_key, parent_id) "
        "VALUES ('ep-1', 'seed episode', NULL, 100.0, 200.0, FALSE, "
        "'sess-key-1', NULL)"
    )
    cur.execute(
        "INSERT INTO entries (band, text, embedding, surprise, ts, "
        "access_count, source, episode_id, episode_title, tags, slots, "
        "reinforcements, explicit_reinforcements) "
        "VALUES ('flat', 'seed entry one', %s::vector, 0.5, 150.0, 3, "
        "'seed-src', 'ep-1', 'seed episode', '[\"tag-a\"]'::jsonb, "
        "'[]'::jsonb, 2, 1)",
        (_VEC,),
    )
    # U+2028/U+0085/U+2029 are line breaks to str.split("\n") but NOT to
    # JSON or to a "\n"-delimited JSONL reader — text pasted from web pages
    # carries them routinely, and one such entry must not shatter a record.
    cur.execute(
        "INSERT INTO entries (band, text, embedding, ts, superseded_at, "
        "superseded_by_text) "
        "VALUES ('flat', %s, %s::vector, 160.0, 170.0, "
        "'replaced by entry one')",
        ("seed entry two pasted\u2028line\u0085sep\u2029end", _VEC),
    )
    cur.execute(
        "INSERT INTO entities (canonical, display, etype, created_at) "
        "VALUES ('widget', 'Widget', 'artifact', 100.0), "
        "('gadget', 'Gadget', NULL, 101.0)"
    )
    cur.execute(
        "INSERT INTO entity_aliases (alias, entity_id) VALUES ('the widget', 1)"
    )
    # An inverse pair exercises the self-referential inverse_of FK, which
    # import must satisfy regardless of the order rows appear in the file.
    cur.execute(
        "INSERT INTO relations (name, description, transitive, builtin, "
        "created_at) VALUES "
        "('uses', 'src makes use of dst', FALSE, TRUE, 90.0), "
        "('hosts', 'src is the host/platform for dst', FALSE, TRUE, 90.0)"
    )
    cur.execute(
        "INSERT INTO relations (name, description, transitive, inverse_of, "
        "builtin, created_at) VALUES "
        "('runs-on', 'src executes on host/platform dst', FALSE, 'hosts', "
        "TRUE, 90.0)"
    )
    cur.execute(
        "INSERT INTO edges (src_id, relation, dst_id, confidence, origin, "
        "asserted_at, tx_time, valid_time, hlc_phys, hlc_logical, writer_id, "
        "session_id, version) "
        "VALUES (1, 'uses', 2, 0.9, 'agent', 110.0, 110.0, 110.0, "
        "1750000000000, 4, 'seed-writer', 'sess-1', 1)"
    )
    cur.execute(
        "INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, "
        "similarity, rationale, source, created_at, status) "
        "VALUES (2, 'uses', 1, 0.6, 0.8, 'looks related', 'deep-dream', "
        "120.0, 'pending')"
    )
    cur.execute(
        "INSERT INTO entity_proposals (kind, entity_id, into_id, score, "
        "reason, status, created_at, judge_verdict, judge_confidence) "
        "VALUES ('merge', 2, 1, 0.7, 'same thing?', 'pending', 121.0, "
        "'reject', 0.55)"
    )
    cur.execute(
        "INSERT INTO entity_kinds (entity_norm, kind, origin, confidence, "
        "decided_at) VALUES ('widget', 'artifact', 'dream', 0.8, 122.0)"
    )
    cur.execute(
        "INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) "
        "VALUES ('gadget', 'widget', 123.0)"
    )
    # A superseded->current scalar chain, one member row, one contested row.
    cur.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, origin, support, provenance, asserted_at, "
        "last_confirmed, superseded_by_value, superseded_at, embedding, "
        "freshness_class, kind, tx_time, valid_time, hlc_phys, hlc_logical, "
        "writer_id, version) "
        "VALUES ('widget', 'color', 'widget', 'color', 'red', 'superseded', "
        "0.9, 'user', '[]'::jsonb, '[1]'::jsonb, 100.0, 100.0, 'blue', "
        "130.0, %s::vector, 'evergreen', 'scalar', 100.0, 100.0, "
        "1750000000001, 0, 'seed-writer', 1)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, origin, support, provenance, asserted_at, "
        "last_confirmed, supersedes_value, embedding, freshness_class, kind, "
        "stance, tx_time, valid_time, hlc_phys, hlc_logical, writer_id, "
        "version) "
        "VALUES ('widget', 'color', 'widget', 'color', 'blue', 'current', "
        "0.95, 'user', '[]'::jsonb, '[2]'::jsonb, 130.0, 130.0, 'red', "
        "%s::vector, 'evergreen', 'scalar', 'probably', 130.0, 130.0, "
        "1750000000002, 0, 'seed-writer', 2)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, support, provenance, asserted_at, "
        "last_confirmed, kind, value_norm, tx_time, valid_time, writer_id, "
        "version) "
        "VALUES ('widget', 'owners', 'widget', 'owners', 'alice', 'current', "
        "0.9, '[]'::jsonb, '[]'::jsonb, 131.0, 131.0, 'member', 'alice', "
        "131.0, 131.0, 'seed-writer', 1)"
    )
    cur.execute(
        "INSERT INTO world_facts (entity, attribute, entity_norm, "
        "attribute_norm, value, status, confidence, origin, support, "
        "provenance, asserted_at, last_confirmed, embedding, source_url, "
        "source_quote, retrieved_at, freshness_class, content_hash, "
        "tx_time, valid_time, writer_id, version) "
        "VALUES ('pgvector', 'latest-version', 'pgvector', 'latest-version', "
        "'0.8.6', 'current', 0.9, 'source', '[]'::jsonb, '[]'::jsonb, 140.0, "
        "140.0, %s::vector, 'https://example.com/rel', 'v0.8.6 released', "
        "140.0, 'volatile', 'abc123', 140.0, 140.0, 'seed-writer', 1)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, "
        "value, about, polarity, outcome, status, confidence, support, "
        "provenance, asserted_at, last_confirmed, embedding, tx_time, "
        "valid_time, writer_id, version) "
        "VALUES ('deploys', 'approach', 'deploys', 'approach', "
        "'verify live after deploy', 'ops/update.ps1', '+', 'success', "
        "'current', 0.8, '[]'::jsonb, '[]'::jsonb, 145.0, 145.0, %s::vector, "
        "145.0, 145.0, 'seed-writer', 1)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO outcome_signals (task, outcome, about, detail, "
        "episode_id, created_at, consumed_at) "
        "VALUES ('seed task', 'success', 'seeding', 'went fine', 'ep-1', "
        "150.0, NULL)"
    )
    cur.execute(
        "INSERT INTO communities (id, label, size, cohesion, computed_at) "
        "VALUES (7, 'widgets', 2, 0.5, 151.0)"
    )
    cur.execute(
        "INSERT INTO entity_communities (entity_id, community_id, "
        "computed_at) VALUES (1, 7, 151.0)"
    )
    cur.execute(
        "INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, "
        "created_at) VALUES ('widget', 'color', 1, 152.0)"
    )
    cur.execute(
        "INSERT INTO entity_sources (entity_id, source, count, origin, "
        "updated_at) VALUES (1, 'seed-src', 3, 'derived', 153.0)"
    )
    cur.execute(
        "INSERT INTO merge_decisions (proposal_id, entity_display, "
        "into_display, status, score, reason, decided_by, decided_at) "
        "VALUES (1, 'Gadget', 'Widget', 'rejected', 0.7, 'distinct', "
        "'user', 154.0)"
    )
    cur.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm, "
        "episode, src_entry_id, hlc_phys, hlc_logical, writer_id) "
        "VALUES ('2026-08-30T12:00:00+00:00'::timestamptz, NULL, 155.0, "
        "'Widget', 'widget', 'shipped', 'shipped', 'ep-1', 1, "
        "1750000000003, 0, 'seed-writer')"
    )
    cur.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm) "
        "VALUES (NULL, 'last spring', 156.0, 'Gadget', 'gadget', "
        "'was painted', 'was painted')"
    )
    cur.execute(
        "INSERT INTO meta (key, value) VALUES "
        "('cortex_dream_cursor', '160.5'::jsonb), "
        "('active_session_pointer', '{\"session\": \"sess-1\"}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    conn.commit()


def _dump_all(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table in EXPORTED_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table}")
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur]
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
        out[table] = rows
    return out


def _rewrite_zip(src, dst, mutate) -> None:
    """Copy a zip, passing {name: bytes} through ``mutate`` first."""
    with zipfile.ZipFile(src) as zf:
        blobs = {n: zf.read(n) for n in zf.namelist()}
    mutate(blobs)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in blobs.items():
            zf.writestr(name, blob)


def test_table_roster_covers_the_whole_schema():
    """Every schema table is explicitly exported or explicitly excluded —
    adding a table without classifying it fails here, the same forcing
    function BENCH_RESET_TABLES applies to the bench reset."""
    exported, excluded = set(EXPORTED_TABLES), set(EXCLUDED_TABLES)
    assert exported & excluded == set()
    assert exported | excluded == set(BENCH_RESET_TABLES)
    # The import freshness guard covers every exported table by
    # construction, minus only what a daemon-initialized bank legitimately
    # holds (schema_version in meta, the builtin relation vocabulary).
    assert set(_MUST_BE_EMPTY) == exported - {"meta", "relations"}


def test_export_import_roundtrip_preserves_every_table(pg_url, tmp_path):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
        before = _dump_all(conn)
        cur = conn.execute("SELECT MAX(id) FROM entries")
        max_entry_id = cur.fetchone()[0]

    out = tmp_path / "bank.zip"
    result = perform_export(pg_url, out)
    assert result["path"] == out
    assert result["counts"]["entries"] == 2
    assert result["counts"]["facts"] == 3

    with _bank(pg_url):
        pass  # truncate back to empty, then release the connection

    perform_import(pg_url, out)

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        after = _dump_all(conn)
        # schema_version belongs to the TARGET build, and the transient
        # active-session pointer must not travel.
        cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        assert cur.fetchone()[0] == SCHEMA_META_VERSION
        cur = conn.execute(
            "SELECT count(*) FROM meta WHERE key = 'active_session_pointer'")
        assert cur.fetchone()[0] == 0
        cur = conn.execute(
            "SELECT value FROM meta WHERE key = 'cortex_dream_cursor'")
        assert cur.fetchone()[0] == 160.5
        # Sequences moved past the imported ids: a fresh insert extends the
        # bank instead of colliding with an imported row.
        cur = conn.execute(
            "INSERT INTO entries (band, text, embedding, ts) "
            "VALUES ('flat', 'post-import entry', %s::vector, 999.0) "
            "RETURNING id",
            (_VEC,),
        )
        assert cur.fetchone()[0] == max_entry_id + 1
        conn.rollback()

    before.pop("meta"), after.pop("meta")  # compared key-by-key above
    for table in EXPORTED_TABLES:
        if table == "meta":
            continue
        assert after[table] == before[table], f"{table} did not roundtrip"


def test_export_skips_transient_meta_and_telemetry(pg_url, tmp_path):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
        conn.execute(
            "INSERT INTO retrieval_events (query_text, origin, created_at) "
            "VALUES ('who owns widget', 'search', 170.0)"
        )
        conn.execute(
            "INSERT INTO dream_runs (started_at, cursor_before, status) "
            "VALUES (171.0, 0.0, 'running')"
        )
        # An extension schema marker (the `*_schema_version` convention) is
        # build-owned like schema_version itself and must not travel.
        conn.execute(
            "INSERT INTO meta (key, value) VALUES "
            "('sampleext_schema_version', '\"v34-sampleext\"'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        conn.commit()

    out = tmp_path / "bank.zip"
    perform_export(pg_url, out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert names == {f"{t}.jsonl" for t in EXPORTED_TABLES} | {
            "manifest.json"}
        meta_keys = {
            json.loads(line)["key"]
            for line in zf.read("meta.jsonl").decode().split("\n") if line
        }
        assert "schema_version" not in meta_keys
        assert "sampleext_schema_version" not in meta_keys
        assert "active_session_pointer" not in meta_keys
        assert "cortex_dream_cursor" in meta_keys
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert manifest["schema_version"] == SCHEMA_META_VERSION
        assert manifest["embedding_dim"] == _DIM
        assert manifest["counts"]["entries"] == 2
        assert set(manifest["excluded_tables"]) == set(EXCLUDED_TABLES)


def test_import_skips_extension_markers_injected_into_the_archive(pg_url, tmp_path):
    """The import-side guard is load-bearing on its own: an archive carrying
    a build-owned extension marker (hand-edited, or exported by a build that
    predates the suffix rule) must not plant it in the target bank."""
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    perform_export(pg_url, out)

    hacked = tmp_path / "hacked.zip"

    def add_marker(blobs):
        row = json.dumps(
            {"key": "sampleext_schema_version", "value": "v34-sampleext"})
        blobs["meta.jsonl"] = blobs["meta.jsonl"] + (row + "\n").encode()

    _rewrite_zip(out, hacked, add_marker)

    with _bank(pg_url):
        pass  # truncate back to empty, then release the connection

    perform_import(pg_url, hacked)

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        cur = conn.execute(
            "SELECT count(*) FROM meta WHERE key = 'sampleext_schema_version'")
        assert cur.fetchone()[0] == 0


def test_import_refuses_a_nonempty_bank(pg_url, tmp_path):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    perform_export(pg_url, out)
    # Bank still holds the seeded rows: import must refuse, not merge.
    with pytest.raises(TransferError, match="not empty"):
        perform_import(pg_url, out)


def test_import_refuses_while_other_connections_hold_the_bank(
    pg_url, tmp_path,
):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    perform_export(pg_url, out)
    with _bank(pg_url) as holder:  # empty bank again, connection held open
        with pytest.raises(TransferError, match="connection"):
            perform_import(pg_url, out)
        # --force acknowledges the connection (e.g. an inert psql session).
        result = perform_import(pg_url, out, force=True)
        assert result["counts"]["entries"] == 2
        holder.rollback()


def test_import_refuses_columns_the_target_does_not_know(pg_url, tmp_path):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    tampered = tmp_path / "newer.zip"
    perform_export(pg_url, out)

    def add_bogus_column(blobs):
        lines = blobs["entries.jsonl"].decode().split("\n")
        rec = json.loads(lines[0])
        rec["from_the_future"] = 1
        lines[0] = json.dumps(rec)
        blobs["entries.jsonl"] = "\n".join(lines).encode()

    _rewrite_zip(out, tampered, add_bogus_column)
    with _bank(pg_url):
        pass
    with pytest.raises(TransferError, match="from_the_future"):
        perform_import(pg_url, tampered)

    # meta rows are imported by a dedicated path that must apply the same
    # refusal — a newer build's extra meta column must not silently drop.
    tampered_meta = tmp_path / "newer-meta.zip"

    def add_bogus_meta_column(blobs):
        lines = blobs["meta.jsonl"].decode().split("\n")
        rec = json.loads(lines[0])
        rec["meta_extra"] = 1
        lines[0] = json.dumps(rec)
        blobs["meta.jsonl"] = "\n".join(lines).encode()

    _rewrite_zip(out, tampered_meta, add_bogus_meta_column)
    with pytest.raises(TransferError, match="meta_extra"):
        perform_import(pg_url, tampered_meta)


def test_import_satisfies_the_relations_inverse_fk_in_any_file_order(
    pg_url, tmp_path,
):
    """relations.inverse_of is a self-referential, non-deferrable FK, and
    nothing guarantees a referenced inverse appears first in the export
    (heap order moves under UPDATEs) — import must load the pair whichever
    way round the file has it."""
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    reversed_zip = tmp_path / "reversed.zip"
    perform_export(pg_url, out)

    def reverse_relations(blobs):
        lines = [
            line for line in blobs["relations.jsonl"].decode().split("\n")
            if line
        ]
        assert any(json.loads(l).get("inverse_of") for l in lines)
        blobs["relations.jsonl"] = "\n".join(reversed(lines)).encode()

    _rewrite_zip(out, reversed_zip, reverse_relations)
    with _bank(pg_url):
        pass
    perform_import(pg_url, reversed_zip)
    with psycopg.connect(pg_url) as conn:
        cur = conn.execute(
            "SELECT inverse_of FROM relations WHERE name = 'runs-on'")
        assert cur.fetchone()[0] == "hosts"


def test_import_applies_defaults_for_columns_the_export_lacks(
    pg_url, tmp_path,
):
    """An export from an OLDER build (fewer columns) loads into a newer
    schema: missing columns take their DDL defaults."""
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    aged = tmp_path / "older.zip"
    perform_export(pg_url, out)

    def drop_columns(blobs):
        lines = []
        for line in blobs["entries.jsonl"].decode().split("\n"):
            if not line:
                continue
            rec = json.loads(line)
            rec.pop("explicit_reinforcements", None)
            lines.append(json.dumps(rec))
        blobs["entries.jsonl"] = "\n".join(lines).encode()

    _rewrite_zip(out, aged, drop_columns)
    with _bank(pg_url):
        pass
    perform_import(pg_url, aged)
    with psycopg.connect(pg_url) as conn:
        cur = conn.execute(
            "SELECT explicit_reinforcements FROM entries ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == [0, 0]


def test_import_refuses_embedding_dim_mismatch(pg_url, tmp_path):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    tampered = tmp_path / "otherdim.zip"
    perform_export(pg_url, out)

    def shrink_dim(blobs):
        manifest = json.loads(blobs["manifest.json"])
        manifest["embedding_dim"] = 512
        blobs["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_zip(out, tampered, shrink_dim)
    with _bank(pg_url):
        pass
    with pytest.raises(TransferError, match="512"):
        perform_import(pg_url, tampered)


def test_import_refuses_a_foreign_format_version(pg_url, tmp_path):
    with _bank(pg_url) as conn:
        _seed_bank(conn)
    out = tmp_path / "bank.zip"
    tampered = tmp_path / "format9.zip"
    perform_export(pg_url, out)

    def bump_format(blobs):
        manifest = json.loads(blobs["manifest.json"])
        manifest["format_version"] = 9
        blobs["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_zip(out, tampered, bump_format)
    with _bank(pg_url):
        pass
    with pytest.raises(TransferError, match="format"):
        perform_import(pg_url, tampered)


def test_cli_dispatches_export_and_import(monkeypatch):
    from pseudolife_memory import cli

    assert "export" in cli._USAGE and "import" in cli._USAGE
    calls = []
    import pseudolife_memory.transfer_cli as transfer_cli

    monkeypatch.setattr(
        transfer_cli, "run_transfer", lambda mode: calls.append(mode))
    for mode in ("export", "import"):
        monkeypatch.setattr("sys.argv", ["pseudolife-mcp", mode])
        cli.main()
    assert calls == ["export", "import"]
