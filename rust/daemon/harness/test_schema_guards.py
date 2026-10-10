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
