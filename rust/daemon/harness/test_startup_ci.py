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
    if "PL_SHADOW_REFRESH_DSN" in env:
        assert env["PL_SHADOW_REFRESH_REQUIRED"] == "1"
        assert all(f in command for f in startup_ci.SHADOW_DB_FILTERS)
        return subprocess.CompletedProcess(
            command, 0, f"test result: ok. {startup_ci.SHADOW_DB_TESTS} passed; 0 failed\n", "")
    if "PL_PGS_MUTATION_DSN" in env:
        return subprocess.CompletedProcess(command, 0, "1 passed\n", "")
    if env.get("PSEUDOLIFE_DAEMON_MUTANT") == "startup-skip-writer-guard":
        return subprocess.CompletedProcess(
            command, 101, "1 failed\n",
            'seating inherited the abandoned transaction: [("retired", 1)]\n')
    return subprocess.CompletedProcess(command, 0, "1 passed\n", "")


FIXTURE = {"PL_SHADOW_EMBED_FIXTURE": "model", "ORT_DYLIB_PATH": "runtime"}


class Disposable:
    """The two pgdisposable calls the shadow step makes, recorded."""
    PREFIX = "pl_cf_pgs_"

    def __init__(self):
        self.created, self.dropped = [], []

    def name(self, label):
        return label

    def create(self, name):
        self.created.append(name)
        return "postgresql://disposable/" + name

    def drop(self, name):
        self.dropped.append(name)


class ShadowRefresh(unittest.TestCase):
    """The shadow step fails unless every database test ran and passed."""

    def run_step(self, outcome):
        pg = Disposable()
        with patch.object(startup_ci.subprocess, "run", side_effect=outcome), \
                patch.object(startup_ci, "shadow_fixture", return_value=FIXTURE):
            result = startup_ci.shadow_refresh(Path("daemon-test"), {}, pg)
        self.assertEqual(pg.created, pg.dropped)
        return result

    def test_all_required_tests_passing_is_accepted(self):
        result = self.run_step(lambda command, **kw: subprocess.CompletedProcess(
            command, 0, f"test result: ok. {startup_ci.SHADOW_DB_TESTS} passed; 0 failed\n", ""))
        self.assertTrue(result["accepted"], result)

    def test_fewer_passes_fail(self):
        result = self.run_step(lambda command, **kw: subprocess.CompletedProcess(
            command, 0, f"test result: ok. {startup_ci.SHADOW_DB_TESTS - 1} passed; 0 failed\n", ""))
        self.assertFalse(result["accepted"], result)

    def test_a_nonzero_exit_fails(self):
        result = self.run_step(lambda command, **kw: subprocess.CompletedProcess(
            command, 101, f"test result: ok. {startup_ci.SHADOW_DB_TESTS} passed; 0 failed\n", ""))
        self.assertFalse(result["accepted"], result)

    def test_a_timeout_fails(self):
        def expire(command, **kw):
            raise subprocess.TimeoutExpired(command, kw["timeout"], output="partial\n")
        result = self.run_step(expire)
        self.assertEqual(result, {"exit_code": None, "timed_out": True, "accepted": False})


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
                        patch.object(startup_ci, "shadow_fixture", return_value=FIXTURE), \
                        tempfile.TemporaryDirectory() as tmp:
                    output = Path(tmp) / "startup.json"
                    code = startup_ci.writer_guard(Path("daemon-test"), {}, output)
                    result = json.loads(output.read_text(encoding="utf-8"))
        finally:
            sys.modules.pop("pgdisposable", None)
            if saved is not None:
                sys.modules["pgdisposable"] = saved
        self.assertEqual(code, 0)
        self.assertEqual(sorted(result), ["control", "hydration_rows", "mutant", "shadow_refresh"])
        self.assertTrue(all(value["accepted"] for value in result.values()), result)
        self.assertEqual(len(created), 4)


if __name__ == "__main__":
    unittest.main()
