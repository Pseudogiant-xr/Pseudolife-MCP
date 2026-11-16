"""Schema-only equivalence gate, invoked by schema_cases with its Rust binary.

The unittest discovery in schema_ci also exercises the comparison's negative
controls without requiring the executable before the build step.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
import dbstate
import psycopg
from rust.harness_support.database_guard import assert_disposable_database
from rust.harness_support import schema
from rust.harness_support.schema import ensure_schema


def compare(expected, actual):
    """Compare every catalog entry and row, with no schema normalization."""
    differences = dbstate.diff(expected, actual)
    if differences:
        raise AssertionError("schema executor mismatch: " + "; ".join(differences))


def check_equivalence(binary, pg):
    """Fresh and repeated schema-only initialization on separate owned banks."""
    names = [pg.name(pg.PREFIX + f"executor_{arm}_{time.time_ns()}")
             for arm in ("driver", "rust")]
    created = []
    try:
        dsns = []
        for name in names:
            dsns.append(pg.create(name))
            created.append(name)
        previous = None
        for _ in range(2):
            with psycopg.connect(dsns[0], autocommit=True) as conn:
                ensure_schema(conn)
            with psycopg.connect(dsns[1], autocommit=True) as conn:
                assert_disposable_database(conn)
            env = dict(os.environ, PL_W1A_OPEN_DSN=dsns[1])
            for key in ("PL_W1A_TEST_DSN", "PL_SCHEMA_EXPECT_REFUSAL", "PSEUDOLIFE_DAEMON_MUTANT"):
                env.pop(key, None)
            run = subprocess.run([str(binary), "--exact",
                "storage::tests::db_ensure_schema_once_for_state_compare", "--nocapture"],
                env=env, capture_output=True, text=True, timeout=60)
            if run.returncode or "1 passed" not in run.stdout or "schema-only initialized" not in run.stdout:
                # Child diagnostics may contain connection details: retain only status.
                raise RuntimeError(f"schema-only Rust probe did not complete (exit {run.returncode})")
            states = [dbstate.dump(dsn) for dsn in dsns]
            compare(*states)
            if previous is not None:
                compare(previous, states[0])
            previous = states[0]
        # A failure after DDL must undo it, including inside an existing
        # caller transaction; the original catalog and version remain exact.
        plan = json.loads((schema.STORAGE / "schema_plan.json").read_text(encoding="utf-8"))
        plan["steps"] += [
            {"op": "sql", "sql": "CREATE TABLE executor_rollback_probe (id integer)", "parameters": []},
            {"op": "sql", "sql": "SELECT 1 / 0", "parameters": []},
        ]
        with tempfile.TemporaryDirectory(prefix="schema-executor-") as scratch:
            directory = Path(scratch)
            (directory / "schema_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (directory / "schema.sql").write_bytes((schema.STORAGE / "schema.sql").read_bytes())
            with patch.object(schema, "STORAGE", directory):
                for autocommit in (True, False):
                    with psycopg.connect(dsns[0], autocommit=autocommit) as conn:
                        if not autocommit:
                            assert_disposable_database(conn)  # begin the caller's transaction
                        try:
                            ensure_schema(conn)
                        except psycopg.errors.DivisionByZero:
                            pass
                        else:
                            raise AssertionError("schema executor swallowed a SQL failure")
                    compare(previous, dbstate.dump(dsns[0]))
        corrupted = copy.deepcopy(previous)
        table = corrupted["rows"]["public.meta"]
        index = table["columns"].index("value")
        key = table["columns"].index("key")
        next(row for row in table["rows"] if row[key] == "schema_version")[index] = -1
        try:
            compare(previous, corrupted)
        except AssertionError:
            return {"opens": 2, "catalog_and_rows_equal": True,
                    "rollback_preserves_state": True, "corrupted_state_caught": True}
        raise AssertionError("schema comparison accepted a changed schema version")
    finally:
        for name in reversed(created):
            pg.drop(name)


class ComparisonControls(unittest.TestCase):
    def test_catalog_and_schema_version_changes_are_detected(self):
        state = {"catalog": {"indexes": [["public", "meta", "meta_pkey"]]},
                 "rows": {"public.meta": {"columns": ["key", "value"],
                                          "rows": [["schema_version", 59]]}}}
        compare(state, copy.deepcopy(state))
        for key in state:
            changed = copy.deepcopy(state)
            changed[key] = {}
            with self.subTest(section=key), self.assertRaisesRegex(AssertionError, "mismatch"):
                compare(state, changed)


if __name__ == "__main__":
    unittest.main()
