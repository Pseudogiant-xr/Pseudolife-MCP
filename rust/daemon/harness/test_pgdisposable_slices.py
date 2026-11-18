"""Each harness admits its own slice's disposable names whatever imported
the shared helper first, and no other slice's."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
# Harness modules a pytest process can import together, with the slice each
# admits when the launcher sets none.
SLICES = {"run": "w1a", "board.run": "w2f", "graph_store": "w3h", "w2e": "w2e",
          "w2d_search": "w2d", "w2d_cortex_check": "w2d", "mcp_run": "w2g",
          "principals": "prn", "dream_cursors": "w3d", "schema_cases": "pgs",
          "clock_retry_cases": "pgs", "startup_cases": "pgs", "writer_session_cases": "pgs"}
PROBE = '''
import importlib, json, os, sys
order = json.loads(sys.argv[1])
modules = {m: importlib.import_module(m) for m in order}
import pgdisposable
seen = {}
for m, mod in modules.items():
    pg = mod.pg
    admitted = {}
    for other in sorted(set(json.loads(sys.argv[2]).values())):
        try:
            pg.name(f"pl_cf_{other}_probe")
            admitted[other] = True
        except ValueError:
            admitted[other] = False
    seen[m] = {"slice": pg.SLICE, "prefix": pg.PREFIX, "admitted": admitted}
print(json.dumps({"seen": seen, "env": os.environ.get("PL_HARNESS_SLICE"),
                  "default": [pgdisposable.SLICE, pgdisposable.PREFIX]}))
'''


def probe(order: list[str]) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PL_HARNESS_SLICE"}
    result = subprocess.run([sys.executable, "-c", PROBE, json.dumps(order), json.dumps(SLICES)],
                            cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8",
                            timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_every_harness_keeps_its_slice_in_any_import_order(order):
    names = list(SLICES) if order == "forward" else list(reversed(SLICES))
    out = probe(names)
    for module, own in SLICES.items():
        seen = out["seen"][module]
        assert (seen["slice"], seen["prefix"]) == (own, f"pl_cf_{own}_"), (module, seen)
        assert seen["admitted"] == {s: s == own for s in seen["admitted"]}, (module, seen)
    # Importing a harness configures only that harness: the launcher's slice
    # (none here) is what the shared helper's own functions still follow.
    assert out["env"] is None
    assert out["default"] == ["w1a", "pl_cf_w1a_"]


def test_the_launcher_slice_is_read_when_used():
    probe_code = '''
import os
import pgdisposable as pg
os.environ["PL_HARNESS_SLICE"] = "w2f"
assert (pg.SLICE, pg.PREFIX) == ("w2f", "pl_cf_w2f_"), pg.PREFIX
assert pg.name("pl_cf_w2f_probe").startswith("pl_cf_w2f_probe")
for name in ("pl_cf_w1a_probe", "pl_cf_w2f_", "pl_cf_w2f_A", "pl_cf_w2fx_probe"):
    try:
        pg.name(name)
    except ValueError:
        continue
    raise AssertionError(f"admitted {name!r}")
os.environ["PL_HARNESS_SLICE"] = "nope"
try:
    pg.name("pl_cf_nope_probe")
except SystemExit as exc:
    assert "PL_HARNESS_SLICE='nope'" in str(exc), exc
else:
    raise AssertionError("an unknown slice was admitted")
'''
    env = {k: v for k, v in os.environ.items() if k != "PL_HARNESS_SLICE"}
    result = subprocess.run([sys.executable, "-c", probe_code], cwd=HERE, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr



def _gate():
    """The harness's database gate, imported in this process (the tests above
    import harness modules in subprocesses)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import database_gate
    return database_gate

def test_an_unavailable_database_skips_by_default(monkeypatch):
    import pytest as _pytest
    gate = _gate()
    monkeypatch.delenv(gate.REQUIRE_DATABASES_ENV, raising=False)
    with _pytest.raises(_pytest.skip.Exception, match="no disposable-bank login"):
        gate.unavailable("no disposable-bank login here", "create one")


def test_an_unavailable_database_fails_when_databases_are_required(monkeypatch):
    """The batch's local harness run (--require-databases) must not pass a
    database case it never ran: it fails, naming the cause and the fix."""
    import pytest as _pytest
    gate = _gate()
    monkeypatch.setenv(gate.REQUIRE_DATABASES_ENV, "1")
    with _pytest.raises(_pytest.fail.Exception) as failed:
        gate.unavailable("no Linux Docker engine answers here", "start Docker and rerun")
    text = str(failed.value)
    assert "no Linux Docker engine answers here" in text and "start Docker and rerun" in text
    assert "--require-databases" in text


def test_the_option_turns_database_skips_into_failures(monkeypatch):
    """rust/conftest.py's --require-databases sets what unavailable() reads."""
    import importlib.util
    from types import SimpleNamespace
    gate = _gate()
    # Registered with monkeypatch first (set, then removed), so teardown takes
    # away what pytest_configure sets below: left set, it turned every later
    # database skip in this process into a failure (review finding on #1415).
    monkeypatch.setenv(gate.REQUIRE_DATABASES_ENV, "0")
    monkeypatch.delenv(gate.REQUIRE_DATABASES_ENV)
    spec = importlib.util.spec_from_file_location(
        "rust_conftest", Path(__file__).resolve().parents[2] / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    def config(required):
        # Model both option types on a turn-enabled machine. The enclosing
        # pytest session owns its turn cleanup; this direct hook call must
        # not release that shared session lease during the unit test.
        return SimpleNamespace(
            getoption=lambda name: required if name == "--require-databases" else 0,
            add_cleanup=lambda callback: None)

    conftest.pytest_configure(config(False))
    assert gate.REQUIRE_DATABASES_ENV not in __import__("os").environ
    conftest.pytest_configure(config(True))
    assert __import__("os").environ[gate.REQUIRE_DATABASES_ENV] == "1"



def test_the_option_test_leaves_no_strict_mode_behind():
    """Run in CI's order, without the option and without a login, the
    database cases after the option test still skip."""
    here = Path(__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if k != _gate().REQUIRE_DATABASES_ENV}
    env["PSEUDOLIFE_TEST_PG_LOGIN_FILE"] = str(here / "no-such-login.env")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{here / 'test_pgdisposable_slices.py'}::test_the_option_turns_database_skips_into_failures",
         str(here / "mirror" / "test_base_delta.py")],
        cwd=here.parents[2], env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    # Every database case in the module skips; counted, not pinned, so a
    # case added there does not break this one.
    skipped = re.search(r"(\d+) skipped", run.stdout.splitlines()[-1])
    assert skipped and int(skipped.group(1)) >= 2, run.stdout[-2000:]


# The login fixtures that skipped with a bare pytest.skip (16 November 2026):
# under --require-databases a machine without the login file skipped them
# silently, so moving the batch's harness run to another machine could pass
# without them. Every one goes through database_gate now.
_LOGIN_FIXTURE_CASES = (
    "board/test_base_delta.py::test_an_untouched_copy_dumps_no_board_rows",
    "board/test_maintainer_rollback.py::test_the_rollback_snapshot_sees_any_change_and_ignores_row_order",
    "test_w2e_judge_settle.py::test_a_foreign_unjudged_proposal_does_not_hold_the_settle",
    "test_w2e_own_decisions.py::test_foreign_decisions_change_no_scenario_check",
    "test_w2e_real_bank_expects.py::test_signal_retention_counts_only_the_fixture_signal",
    "test_w2d_seed_base.py::test_the_stats_seed_lands_on_a_bank_that_has_a_graph",
)


def test_no_harness_test_skips_a_missing_login_past_the_gate():
    here = Path(__file__).resolve().parent
    bare = re.compile(r"pytest\.skip\(\s*[\"'][^\"']*disposable-bank login")
    offenders = [str(p.relative_to(here)) for p in sorted(here.rglob("test_*.py"))
                 if bare.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_a_missing_login_fails_the_login_fixtures_when_databases_are_required(tmp_path):
    import xml.etree.ElementTree as ET

    here = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PSEUDOLIFE_TEST_PG_LOGIN_FILE"] = str(here / "no-such-login.env")
    report = tmp_path / "login-fixtures.xml"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--require-databases",
         f"--junitxml={report}",
         *[str(here / name) for name in _LOGIN_FIXTURE_CASES]],
        cwd=here.parents[2], env=env, capture_output=True, text=True, timeout=300)
    last = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
    # One case per fixture: unrelated tests in these modules may legitimately
    # skip optional packages (the hosted harness does not install Torch).
    # Each selected case must fail setup for its missing login, never skip.
    assert run.returncode == 1 and f"{len(_LOGIN_FIXTURE_CASES)} errors" in last, run.stdout[-2000:]
    assert "skipped" not in last, run.stdout[-2000:]
    cases = ET.parse(report).findall(".//testcase")
    assert {case.get("name") for case in cases} == {case.split("::")[1] for case in _LOGIN_FIXTURE_CASES}
    for case in cases:
        error = case.find("error")
        assert error is not None and case.find("skipped") is None
        assert "no disposable-bank login here" in error.get("message", "")
        assert "This run requires databases" in error.get("message", "")


def test_without_the_option_a_missing_login_still_skips_the_login_fixtures():
    # Hosted CI's case: no login and no --require-databases, so they skip.
    here = Path(__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if k != _gate().REQUIRE_DATABASES_ENV}
    env["PSEUDOLIFE_TEST_PG_LOGIN_FILE"] = str(here / "no-such-login.env")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *[str(here / name) for name in _LOGIN_FIXTURE_CASES]],
        cwd=here.parents[2], env=env, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:]
    assert f"{len(_LOGIN_FIXTURE_CASES)} skipped" in run.stdout.strip().splitlines()[-1], run.stdout[-2000:]
