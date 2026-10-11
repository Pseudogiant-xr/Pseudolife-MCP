"""Startup CI's writer guard runs offline against a stubbed admin connection."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE))
import startup_ci


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args):
        pass


def fake_run(command, *, env, **kwargs):
    if "PL_PGS_MUTATION_DSN" in env:
        return subprocess.CompletedProcess(command, 0, "1 passed\n", "")
    if env.get("PSEUDOLIFE_DAEMON_MUTANT") == "startup-skip-writer-guard":
        return subprocess.CompletedProcess(
            command, 101, "1 failed\n",
            'seating inherited the abandoned transaction: [("retired", 1)]\n')
    return subprocess.CompletedProcess(command, 0, "1 passed\n", "")


class WriterGuard(unittest.TestCase):
    def test_every_database_it_creates_is_owned_through_hydration(self):
        saved = sys.modules.pop("pgdisposable", None)
        try:
            with patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}):
                pg = importlib.import_module("pgdisposable")
                created = []
                with patch.object(pg, "_admin", Connection), \
                        patch.object(pg, "_login", return_value=("user", "secret")), \
                        patch.object(pg, "assert_disposable_database"), \
                        patch.object(pg, "create_from_default_template",
                                     side_effect=lambda conn, statement: created.append(statement)), \
                        patch.object(startup_ci.subprocess, "run", side_effect=fake_run), \
                        tempfile.TemporaryDirectory() as tmp:
                    output = Path(tmp) / "startup.json"
                    code = startup_ci.writer_guard(Path("daemon-test"), {}, output)
                    result = json.loads(output.read_text(encoding="utf-8"))
        finally:
            sys.modules.pop("pgdisposable", None)
            if saved is not None:
                sys.modules["pgdisposable"] = saved
        self.assertEqual(code, 0)
        self.assertEqual(sorted(result), ["control", "hydration_rows", "mutant"])
        self.assertTrue(all(value["accepted"] for value in result.values()), result)
        self.assertEqual(len(created), 3)


if __name__ == "__main__":
    unittest.main()
