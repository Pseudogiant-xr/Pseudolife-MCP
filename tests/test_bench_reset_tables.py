"""The bench/test reset must truncate every table the schema declares.

`TRUNCATE ... CASCADE` reaches only tables with a foreign key into the
truncated set, so an FK-free table left off the list survives the reset and
carries rows from one bench question into the next. That has now cost two
runs: `chronicle_events` (2026-08-04, events accumulated across all 266
questions of the ev-weak run — its serving-side verdict was invalidated)
and, found by the 2026-08-25 audit, `entity_kinds` / `retrieval_events` /
`outcome_signals` / `dismissed_pairs` / `merge_decisions` / `communities`
in `evals/ladder_sweep.py` (#181). A leaked `entity_kinds` row flips a
later question's `freshness_class`, which changes what `stale_policy`
serves.

Both times the fix taught exactly one of the two hand-maintained lists.
There is now one list — `schema.BENCH_RESET_TABLES` — and this parses the
DDL to prove it is complete, so the next schema bump cannot reopen the gap
by forgetting a file.
"""
from __future__ import annotations

import inspect
import re

from pseudolife_memory.storage import schema

# `IF NOT EXISTS` is required in the pattern on purpose: without it the
# regex also matches the prose "before the CREATE TABLE calls" comment.
_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)")


def _declared_tables() -> set[str]:
    """Every table name the schema module creates, DDL string and the
    additive-migration `cur.execute` tail alike — read from the source, so
    a table added anywhere in the file counts."""
    return set(_CREATE_RE.findall(inspect.getsource(schema)))


def test_every_declared_table_is_in_the_reset_list():
    missing = sorted(_declared_tables() - set(schema.BENCH_RESET_TABLES))
    assert not missing, (
        "these tables are declared by schema.py but absent from "
        "BENCH_RESET_TABLES, so a bench reset leaves their rows behind:\n  "
        + "\n  ".join(missing))


def test_reset_list_names_no_table_the_schema_does_not_declare():
    """The other direction: a renamed or dropped table must not linger in
    the list, or every TRUNCATE fails with `relation does not exist`."""
    extra = sorted(set(schema.BENCH_RESET_TABLES) - _declared_tables())
    assert not extra, (
        "BENCH_RESET_TABLES names tables schema.py does not create:\n  "
        + "\n  ".join(extra))


def test_the_previously_leaking_tables_are_covered():
    """Named explicitly so a future edit that trims the list by FK
    reasoning has to argue with the two incidents by name."""
    for table in ("chronicle_events", "entity_kinds", "retrieval_events",
                  "outcome_signals", "dismissed_pairs", "merge_decisions",
                  "communities"):
        assert table in schema.BENCH_RESET_TABLES, table


def test_both_reset_sites_consume_the_shared_list():
    """The point of the constant is that no second list exists."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import ladder_sweep

    from tests import pg_fixtures

    assert ladder_sweep._ALL_TABLES is schema.BENCH_RESET_TABLES
    assert pg_fixtures._ALL_TABLES is schema.BENCH_RESET_TABLES
