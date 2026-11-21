"""Replay complete captured rows for the shutdown and commit-loss scenarios."""
import json
from pathlib import Path

import psycopg

from rust.harness_support.bank_fixture import initialize_bank, require_fixture_database
from rust.harness_support.database_guard import dsn_database_name
from rust.harness_support.fixture_rows import checked_fixture_insert


def prepare_empty(dsn, name, *, copied):
    if not copied:
        replay(dsn, name)
        return
    require_fixture_database(dsn_database_name(dsn) or "")
    with psycopg.connect(dsn) as conn:
        initialize_bank(conn)


def replay(dsn, name):
    require_fixture_database(dsn_database_name(dsn) or "")
    path = Path(__file__).with_name("commit_loss_seed_rows.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    fixture = data["cases"][name]
    for rows in fixture["rows"].values():
        for row in rows:
            if row.get("embedding") is not None:
                row["embedding"] = data["vectors"][row["embedding"]]
    with psycopg.connect(dsn) as conn:
        initialize_bank(conn)
        inserts = [checked_fixture_insert(conn, table, rows)
                   for table, rows in fixture["rows"].items()]
        for statement, params in inserts:
            conn.execute(statement, params)
        for sequence, (position, used) in fixture["positions"].items():
            conn.execute("SELECT setval(%s::regclass, %s, %s)", (f"public.{sequence}", position, used))
