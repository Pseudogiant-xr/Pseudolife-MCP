"""Replay captured background fixture rows on a guarded bank template."""
from __future__ import annotations

import copy

import psycopg
from psycopg import sql

from background_seed_rows import ROWS, SEQUENCE_CALLS
from rust.harness_support.bank_fixture import assert_fixture_database, initialize_bank, require_fixture_database
from rust.harness_support.database_guard import dsn_database_name
from rust.harness_support.fixture_rows import checked_fixture_insert


def seed_bank(dsn, kind, *, auto_title=False):
    require_fixture_database(dsn_database_name(dsn) or "")
    rows = copy.deepcopy(ROWS[kind])
    if auto_title:
        title = "session - 2023-11-14 22:13"
        next(row for row in rows["episodes"] if row["title"] == "work fixture")["title"] = title
        rows["entries"][0]["episode_title"] = title
    with psycopg.connect(dsn) as conn:
        initialize_bank(conn)
        inserts = {table: checked_fixture_insert(conn, table, values) for table, values in rows.items()}
        # Allocate the captured number of IDs, including the session entry
        # deleted by the old seed. Cloned banks may already have allocated IDs.
        ids = {}
        for table, count in SEQUENCE_CALLS[kind].items():
            ids[table] = dict(enumerate(
                (row[0] for row in conn.execute(
                    "SELECT nextval(%s::regclass) FROM generate_series(1, %s)",
                    (f"public.{table}_id_seq", count))), start=1))
            for row in rows[table]:
                row["id"] = ids[table][row["id"]]
        for row in rows.get("dream_run_slots", []):
            row["run_id"] = ids["dream_runs"][row["run_id"]]
        for table, (statement, params) in inserts.items():
            if table == "meta":
                # A copied bank keeps its existing cursor, signing seed and
                # other initial state. Only the fixture's missing keys are new.
                statement += sql.SQL(" ON CONFLICT (key) DO NOTHING")
            conn.execute(statement, params)


def seed_pruning_refusal(dsn):
    require_fixture_database(dsn_database_name(dsn) or "")
    with psycopg.connect(dsn) as conn:
        assert_fixture_database(conn)
        conn.execute(
            "CREATE FUNCTION pl_w3i_prune_failure() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'fixture pruning refusal'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER pl_w3i_prune_failure BEFORE DELETE ON public.dream_runs "
            "FOR EACH STATEMENT EXECUTE FUNCTION pl_w3i_prune_failure()"
        )
