"""Schema-only equivalence gate, invoked by schema_cases with its Rust binary.

The unittest discovery in schema_ci also exercises the comparison's negative
controls without requiring the executable before the build step.
"""
import copy
import hashlib
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

PLAN_SHA256 = "21fe09e91cf0494b6d7dd667cd27fa04211defd21efc8ef65da9cff0323ba882"


def plan_fingerprint():
    payload = json.dumps(schema.load_plan(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare(expected, actual):
    """Compare every catalog entry and row, with no schema normalization."""
    differences = dbstate.diff(expected, actual)
    if differences:
        raise AssertionError("schema executor mismatch: " + "; ".join(differences))


def check_equivalence(binary, pg):
    """Linux gate: the schema plan and PostgreSQL semantics are OS-independent."""
    if sys.platform != "linux":
        reason = ("Linux-only schema equivalence: committed plan and PostgreSQL semantics are "
                  "client-OS independent; Windows exercises the executor through _bank and "
                  "the non-spawning parsed-plan hash test")
        print("schema executor equivalence: SKIP: " + reason, flush=True)
        return {"skipped": reason}
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
    def test_committed_plan_and_sql_fingerprint(self):
        # Pin the actual executor input, including every operation, branch,
        # predicate and SQL byte after its universal-newline decoding.
        self.assertEqual(plan_fingerprint(), PLAN_SHA256)

    def test_crlf_and_unicode_space_paths_read_the_same_plan(self):
        with tempfile.TemporaryDirectory(prefix="schema-plan-") as scratch:
            directory = Path(scratch) / "schema fixtures \u00e9"
            directory.mkdir()
            for name in ("schema_plan.json", "schema.sql"):
                data = (schema.STORAGE / name).read_bytes().replace(b"\r\n", b"\n")
                (directory / name).write_bytes(data.replace(b"\n", b"\r\n"))
            with patch.object(schema, "STORAGE", directory):
                self.assertEqual(plan_fingerprint(), PLAN_SHA256)
                plan = json.loads((directory / "schema_plan.json").read_text(encoding="utf-8"))
                plan["steps"].append({"op": "unknown_future_operation"})
                (directory / "schema_plan.json").write_text(json.dumps(plan), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unknown schema operation"):
                    plan_fingerprint()

    def test_windows_equivalence_skips_before_spawning_or_connecting(self):
        with patch.object(sys, "platform", "win32"), patch("sys.stdout"), \
                patch.object(subprocess, "run", side_effect=AssertionError("spawned")):
            result = check_equivalence(None, None)
        self.assertIn("Linux-only", result["skipped"])
        self.assertIn("_bank", result["skipped"])

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


class RefusalFixtureControls(unittest.TestCase):
    def test_refusal_seed_rejects_non_disposable_server_name_before_writes(self):
        from unittest.mock import MagicMock

        import schema_cases
        from rust.harness_support.database_guard import PRODUCTION_DATABASE_ENV

        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = ("ordinary_bank",)
        with patch.object(schema_cases.pg, "create", return_value="fixture-dsn"), \
                patch.object(schema_cases.pg, "drop") as drop, \
                patch.object(schema_cases.psycopg, "connect", return_value=connection), \
                patch.dict(sys.modules, {"pseudolife_memory.storage.postgres": None}), \
                patch.dict(os.environ, {PRODUCTION_DATABASE_ENV: "configured_bank"}), \
                self.assertRaisesRegex(ValueError, "bank fixtures require a disposable harness database"):
            schema_cases.refusal_cell(Path("unused"), 1, "99")
        connection.execute.assert_called_once_with("SELECT current_database()")
        connection.transaction.assert_not_called()
        drop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
