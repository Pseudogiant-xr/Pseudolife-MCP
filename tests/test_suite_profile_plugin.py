"""tests/suite_profile_plugin.py: the opt-in profiler never changes a run's
outcome, keeps a timeline the workers and the samples share, survives a run
that dies, and writes nothing secret.

Each case runs a disposable pytest session in a subprocess with the plugin
loaded, against test files written to a temporary directory, so the
repository's own conftest and fixtures stay out of it. The sampler is
pointed at a closed port, so no case reaches a real PostgreSQL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SeKrEt-pw-4471"


def _session(tmp_path: Path, source: str, *args: str, **env: str):
    tests = tmp_path / "cases"
    tests.mkdir(exist_ok=True)
    (tests / "test_cases.py").write_text(textwrap.dedent(source), encoding="utf-8")
    out = tmp_path / "profile"
    run_env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PYTEST_", "PSEUDOLIFE_"))}
    run_env.update({
        "PSEUDOLIFE_SUITE_PROFILE_DIR": str(out),
        "PSEUDOLIFE_SUITE_PROFILE_INTERVAL": "0.2",
        # A closed port: the sampler records a connection error, never data.
        "PSEUDOLIFE_TEST_DATABASE_URL":
            f"postgresql://someone:{SECRET}@127.0.0.1:9/nothing",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    run_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.suite_profile_plugin",
         "-p", "no:cacheprovider", "--rootdir", str(tests), str(tests), *args],
        cwd=ROOT, env=run_env, capture_output=True, text=True, timeout=300)
    return proc, out


def _records(out: Path, name: str = "profile-main.jsonl") -> list[dict]:
    return [json.loads(line) for line in
            (out / name).read_text(encoding="utf-8").splitlines() if line]


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


FAILING = """
import pytest

def test_passes():
    assert True

def test_fails():
    assert 1 == 2

@pytest.mark.skip(reason="not today")
def test_skipped():
    pass
"""


def test_a_failing_run_stays_failing_and_is_described(tmp_path):
    proc, out = _session(tmp_path, FAILING)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    by_id = {r["id"].split("::")[1]: r for r in _records(out) if "id" in r}
    assert by_id["test_fails"]["outcome"] == "failed"
    assert by_id["test_passes"]["outcome"] == "passed"
    assert by_id["test_skipped"]["outcome"] == "skipped"

    manifest = _manifest(out)
    assert manifest["complete"] is True
    assert manifest["exitstatus"] == 1
    assert manifest["outcomes"] == {"passed": 1, "failed": 1, "skipped": 1}
    # Where the skip was raised, not its message (which may carry a path).
    (where,) = manifest["skips"]
    assert where.startswith("test_cases.py:") and manifest["skips"][where] == 1
    assert manifest["inventory"]["count"] == 3
    assert (out / "inventory.txt").read_text(encoding="utf-8").count("::test_") == 3


def test_phases_carry_ordered_monotonic_spans(tmp_path):
    proc, out = _session(tmp_path, """
        import time
        import pytest

        @pytest.fixture
        def slow_setup():
            time.sleep(0.05)
            yield

        def test_a(slow_setup):
            time.sleep(0.05)
    """)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = [r for r in _records(out) if "id" in r]
    assert record["worker"] == "main"
    setup, call, teardown = record["setup_t"], record["call_t"], record["teardown_t"]
    assert setup[0] <= setup[1] <= call[0] <= call[1] <= teardown[0] <= teardown[1]
    assert setup[1] - setup[0] >= 0.04
    # The samples share the clock: the run's samples bracket the test.
    samples = [json.loads(line) for line in
               (out / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert samples[0]["t"] <= setup[0] and samples[-1]["t"] >= teardown[1]


def test_a_slow_fixture_teardown_is_named(tmp_path):
    proc, out = _session(tmp_path, """
        import time
        import pytest

        @pytest.fixture(scope="module")
        def slow_bank():
            yield
            time.sleep(0.6)

        def test_a(slow_bank):
            pass
    """)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = [r for r in _records(out) if "id" in r]
    names = [name for name, _ in record["slow_teardowns"]]
    assert names == ["slow_bank@module"]
    totals = json.loads((out / "fixtures-main.json").read_text(encoding="utf-8"))
    assert totals["teardown"]["slow_bank@module"]["s"] >= 0.6
    assert "slow_bank@module" in totals["setup"]


def test_nothing_secret_or_machine_specific_is_written(tmp_path):
    junit = tmp_path / "private-dir" / "junit.xml"
    unreadable = (tmp_path / "private-dir" / "x").as_posix()
    proc, out = _session(tmp_path, FAILING + f"""

def test_skips_naming_a_path():
    pytest.skip("cannot read {unreadable} at db.example.com:5432")
""", f"--junitxml={junit}")

    assert proc.returncode == 1
    for path in out.iterdir():
        text = path.read_text(encoding="utf-8")
        assert SECRET not in text, path.name
        assert "private-dir" not in text, path.name
        assert "db.example.com" not in text, path.name
        assert str(Path.home()) not in text, path.name
    manifest = _manifest(out)
    assert "--junitxml=<abs>/junit.xml" in manifest["args"]
    # The unreachable server is recorded by error class only.
    samples = [json.loads(line) for line in
               (out / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    errors = {s["pg"]["error"] for s in samples if "pg" in s}
    assert errors and all(e.isidentifier() for e in errors)


def test_a_run_that_dies_leaves_what_it_had_written(tmp_path):
    proc, out = _session(tmp_path, """
        import os

        def test_1_first():
            pass

        def test_2_dies():
            os._exit(3)
    """)

    assert proc.returncode == 3
    lines = _records(out)
    assert [r["id"].split("::")[1] for r in lines if "id" in r] == ["test_1_first"]
    # The test it died in is named by its start line.
    assert lines[-1]["start"].endswith("::test_2_dies")
    manifest = _manifest(out)
    assert manifest["complete"] is False
    assert "exitstatus" not in manifest
    # The inventory was written when collection finished.
    assert manifest["inventory"]["count"] == 2
    assert (out / "inventory.txt").read_text(encoding="utf-8").count("::test_") == 2


@pytest.mark.skipif(not __import__("importlib.util").util.find_spec("xdist"),
                    reason="pytest-xdist is not installed")
def test_xdist_workers_write_their_own_timelines(tmp_path):
    proc, out = _session(tmp_path, FAILING, "-n", "2", "--dist", "loadfile")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    worker_files = sorted(p.name for p in out.glob("profile-gw*.jsonl"))
    assert worker_files
    records = [r for name in worker_files for r in _records(out, name) if "id" in r]
    assert {r["worker"] for r in records} <= {"gw0", "gw1"}
    assert sorted(r["outcome"] for r in records) == ["failed", "passed", "skipped"]
    assert all("call_t" in r or r["outcome"] == "skipped" for r in records)
    manifest = _manifest(out)
    assert manifest["workers_requested"] == 2 and manifest["dist"] == "loadfile"
    assert manifest["inventory"]["count"] == 3
    assert manifest["outcomes"] == {"passed": 1, "failed": 1, "skipped": 1}
    assert set(manifest["workers_seen"]) <= {"gw0", "gw1"}


@pytest.mark.parametrize("query, kind", [
    ("CREATE DATABASE x", "db_create"),
    ("  drop database if exists x", "db_drop"),
    # Lower case: the reset guard's tripwire reads an upper-case "TRUNCATE "
    # literal as a reset site (tests/test_disposable_database_guard.py).
    ("truncate a, b restart identity", "truncate"),
    (b"\ttruncate a", "truncate"),
    ("SELECT pg_terminate_backend(4242)", "terminate"),
    ("CHECKPOINT", "checkpoint"),
    ("SELECT 1", None),
    ("INSERT INTO meta VALUES (1)", None),
])
def test_statement_kinds_are_told_apart_by_their_start(query, kind):
    from tests import suite_profile_plugin as plugin

    assert plugin._sql_kind(query) == kind


def test_composed_statements_are_classified_without_a_connection():
    psycopg = pytest.importorskip("psycopg")
    from tests import suite_profile_plugin as plugin

    query = psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier("x"))
    assert plugin._sql_kind(query, None) == "db_drop"


@pytest.mark.parametrize("arg, shown", [
    ("tests/", "tests/"),
    ("-q", "-q"),
    ("--junitxml=/home/someone/ci/junit.xml", "--junitxml=<abs>/junit.xml"),
    ("D:\\work\\x\\tests", "<abs>/tests"),
    ("cache_dir=/srv/ci/cache", "cache_dir=<abs>/cache"),
    ("..\\..\\Users\\someone\\repo\\tests", "<abs>/tests"),
    ("../home/someone/repo", "<abs>/repo"),
    ("--dsn=postgresql://u:pw@db.example.com:5432/x", "--dsn=<url>"),
    ("tests/test_a.py::test_b[x y]", "tests/test_a.py::test_b[x y]"),
    ("--junitxml=D:\\Work Files\\Jane Doe\\j.xml", "--junitxml=<abs>/j.xml"),
    ("/home/jane doe/x/junit.xml", "<abs>/junit.xml"),
    ("--base=https://x.example.com/a?password=secret", "--base=<url>"),
    ("host=db.example.com password=pw", "host=<url>"),
    ("u:pw@db.example.com:5432", "<url>"),
    ("-k", "-k"),
    ("a or b", "a or b"),
    ("--deselect=tests/x.py::t", "--deselect=tests/x.py::t"),
    ("tests/x.py::test[a/b]", "tests/x.py::test[a/b]"),
    ("--rootdir=~/repo/", "--rootdir=<abs>/repo"),
    ("-k", "-k"),
    ("not slow", "not slow"),
])
def test_arguments_keep_only_a_paths_last_component(arg, shown):
    from tests import suite_profile_plugin as plugin

    assert plugin._scrub(arg) == shown


@pytest.mark.skipif(not __import__("importlib.util").util.find_spec("xdist"),
                    reason="pytest-xdist is not installed")
def test_a_worker_crash_is_counted_and_its_test_named(tmp_path):
    proc, out = _session(tmp_path, """
        import os

        def test_dies():
            os._exit(3)
    """, "-n", "1")

    assert proc.returncode != 0
    manifest = _manifest(out)
    assert manifest["outcomes"].get("crashed") == 1
    assert "main" not in manifest["workers_seen"]
    starts = [json.loads(line) for p in out.glob("profile-gw*.jsonl")
              for line in p.read_text(encoding="utf-8").splitlines() if line]
    assert [s["start"].split("::")[1] for s in starts if "start" in s] == ["test_dies"]


def test_a_failed_profile_write_never_fails_the_run(tmp_path):
    out = tmp_path / "profile"
    (out / "manifest.json").mkdir(parents=True)  # os.replace onto it fails
    proc, _ = _session(tmp_path, "def test_ok():\n    pass\n")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INTERNALERROR" not in proc.stdout + proc.stderr


def test_an_earlier_runs_files_are_cleared(tmp_path):
    out = tmp_path / "profile"
    out.mkdir()
    (out / "profile-gw7.jsonl").write_text('{"id": "old::t"}\n', encoding="utf-8")
    (out / "summary.json").write_text("{}", encoding="utf-8")
    (out / "notes.txt").write_text("kept", encoding="utf-8")

    proc, _ = _session(tmp_path, "def test_ok():\n    pass\n")

    assert proc.returncode == 0
    assert not (out / "profile-gw7.jsonl").exists()
    assert not (out / "summary.json").exists()
    assert (out / "notes.txt").read_text(encoding="utf-8") == "kept"


def test_statements_are_credited_to_the_phase_they_ran_in():
    from tests import suite_profile_plugin as plugin

    class Cursor:
        def execute(self, query, *args, **kwargs):
            return "ran"

    plugin._patch_execute(Cursor)
    plugin._counters.clear()
    try:
        plugin._current_phase[0] = "teardown"
        assert Cursor().execute("DROP DATABASE x") == "ran"
        plugin._current_phase[0] = "setup"
        Cursor().execute("CREATE DATABASE x")
        Cursor().execute("SELECT 1")
        assert plugin._counters["db_drop_n"] == 1
        assert plugin._counters["db_create_n"] == 1
        assert "teardown_db_drop_s" in plugin._counters
        assert "setup_db_create_s" in plugin._counters
        assert not any(k.startswith("setup_db_drop") for k in plugin._counters)
    finally:
        plugin._counters.clear()
        plugin._current_phase[0] = "none"


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *args):
        return _Rows(self.rows)


def _sampler(tmp_path, rows):
    from tests import suite_profile_plugin as plugin

    run = plugin._Run.__new__(plugin._Run)
    run.data = {}
    sampler = plugin._Sampler(tmp_path, run, 1.0, None)
    sampler.conn = _Conn(rows)
    return sampler


def test_sessions_on_a_production_bank_are_never_counted(tmp_path, monkeypatch):
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    sampler = _sampler(tmp_path, [
        ("client backend", "active", "IPC", "CheckpointStart", "pseudolife_memory_test_9"),
        ("client backend", "active", "Lock", "relation", "pseudolife_memory"),
        ("checkpointer", "", "IO", "DataFileSync", None),
    ])
    try:
        assert sampler._sessions() == [
            ["checkpointer", "", "IO", "DataFileSync", 1],
            ["client backend", "active", "IPC", "CheckpointStart", 1]]
    finally:
        sampler.out.close()


def test_no_session_is_counted_when_the_bank_cannot_be_identified(tmp_path, monkeypatch):
    monkeypatch.setenv("_PSEUDOLIFE_PRODUCTION_DB", "<unresolved>")
    sampler = _sampler(tmp_path, [
        ("client backend", "active", "IPC", "CheckpointStart", "pseudolife_memory_test_9"),
    ])
    try:
        assert sampler._sessions() is None
    finally:
        sampler.out.close()


def test_a_sampler_that_cannot_start_never_stops_the_run(tmp_path):
    out = tmp_path / "profile"
    (out / "samples.jsonl").mkdir(parents=True)  # opening it fails
    proc, _ = _session(tmp_path, "def test_ok():\n    pass\n")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INTERNALERROR" not in proc.stdout + proc.stderr
    assert _manifest(out)["complete"] is True


def test_a_profile_that_cannot_be_written_never_stops_the_run(tmp_path):
    out = tmp_path / "profile"
    (out / "profile-main.jsonl").mkdir(parents=True)  # opening it fails
    proc, _ = _session(tmp_path, "def test_ok():\n    pass\n")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INTERNALERROR" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("argv, head", [
    (["python3", "-m", "pytest", "-q"], "python3:pytest"),
    (["/usr/bin/python3.11", "-m", "pip"], "python3.11:pip"),
    (["git", "commit", "-m", "private message"], "git"),
    (["pwsh", "-File", "x.ps1"], "pwsh"),
])
def test_a_spawn_is_named_by_its_program_never_its_arguments(argv, head):
    from tests import suite_profile_plugin as plugin

    assert plugin._spawn_head(argv) == head
