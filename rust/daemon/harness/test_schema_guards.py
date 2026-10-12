"""Harness prefix checks supplement, and never replace, the oracle guard."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE))
import pgdisposable as pg
from pseudolife_memory.storage import schema


class DisposableGuards(unittest.TestCase):
    def test_registered_production_name_wins_over_the_fixture_prefix(self):
        name = pg.PREFIX + "123"
        with patch.dict(os.environ, {schema.PRODUCTION_DATABASE_ENV: name.upper() + "/"}):
            with self.assertRaises(schema.ProductionDatabaseError):
                pg._check(name)

    def test_unresolved_production_identity_refuses_every_fixture(self):
        with patch.dict(os.environ, {schema.PRODUCTION_DATABASE_ENV: "<unresolved>"}):
            with self.assertRaises(schema.ProductionDatabaseError):
                pg._check(pg.PREFIX + "123")

    def test_cleanup_refuses_another_runs_database_before_connecting(self):
        name = pg.PREFIX + "foreign_0123456789abcdef"
        with patch.object(pg, "_admin", side_effect=AssertionError("connected")):
            with self.assertRaisesRegex(ValueError, "owned"):
                pg.drop(name)

    def test_existing_only_lists_this_runs_allocated_names(self):
        own = pg.name(pg.PREFIX + "owned")
        foreign = pg.PREFIX + "foreign_0123456789abcdef"

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, *_args):
                return [(own,), (foreign,)]

        with patch.object(pg, "_admin", return_value=Connection()):
            self.assertEqual(pg.existing(), [own])

    def test_default_template_busy_retries_without_terminating_sessions(self):
        import psycopg

        name = pg.name(pg.PREFIX + "template_busy")
        statements = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, statement):
                statements.append(statement)
                if len(statements) < 3:
                    raise psycopg.errors.ObjectInUse("template1 has another session")

        with patch.object(pg, "drop"), patch.object(pg, "_admin", Connection), \
                patch.object(pg, "assert_disposable_database"), \
                patch.object(pg, "dsn", return_value=name), patch("time.sleep"):
            self.assertEqual(pg.create(name), name)
        self.assertEqual([statement.as_string() for statement in statements],
                         [f'CREATE DATABASE "{name}"'] * 3)

    def test_server_guard_precedes_destructive_sql(self):
        events = []

        class Connection:
            def execute(self, sql, *args):
                events.append(str(sql))
                return self

            def fetchone(self):
                return ("PSEUDOLIFE_MEMORY/",)

        with patch.dict(os.environ, {schema.PRODUCTION_DATABASE_ENV: "pseudolife_memory"}):
            with self.assertRaises(schema.ProductionDatabaseError):
                schema.assert_disposable_database(Connection())
        self.assertEqual(events, ["SELECT current_database()"])


if __name__ == "__main__":
    unittest.main()
