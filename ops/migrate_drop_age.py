"""One-time: drop the AGE graph + extension from an existing bank.

Edges live in the relational ``edges`` table (the source of truth), so dropping
the AGE graph is zero data loss. Run while the Postgres image still has the AGE
binary (apache/age), THEN switch the image to pgvector-only. Back up first
(``ops/backup.ps1``).

``DROP EXTENSION age CASCADE`` removes the ``age`` extension, the ``ag_catalog``
schema, and any AGE graph schemas (e.g. ``pseudolife_graph``) with their label
tables. It does NOT touch the relational ``public`` bank.
"""
from __future__ import annotations

import os

import psycopg


def main() -> None:
    dsn = os.environ.get(
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION IF EXISTS age CASCADE")
        print("migrate_drop_age: dropped AGE extension + graph (if present)")


if __name__ == "__main__":
    main()
