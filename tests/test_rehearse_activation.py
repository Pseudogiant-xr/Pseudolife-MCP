"""rust/daemon/harness/rehearse_activation.py: the guards a real run rests on.

The rehearsal itself starts daemons on the bench PostgreSQL and is run on
demand (see its docstring); these cases pin the parts that decide what it
may touch and what it counts as proof, without starting anything.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "rust" / "daemon" / "harness"


def _load():
    if str(HARNESS) not in sys.path:
        sys.path.insert(0, str(HARNESS))
    spec = importlib.util.spec_from_file_location("rehearse_activation_under_test",
                                                  HARNESS / "rehearse_activation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rehearse = _load()
OWNED = ["pl_cf_w2e_rehearsal_0123456789abcdef"]


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1:5433/pl_cf_w2e_rehearsal_0123456789abcdef",
    "host=127.0.0.1 port=5433 dbname=pl_cf_w2e_rehearsal_0123456789abcdef user=u",
])
def test_a_daemon_starts_only_on_the_bank_this_run_created(dsn):
    assert rehearse.disposable_dsn(dsn, OWNED) == dsn


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1:5432/pseudolife",            # a real bank's name
    "postgresql://u:p@127.0.0.1:5433/pl_cf_w2e_other_feed",  # disposable, not ours
    "postgresql://u:p@127.0.0.1:5433/",                       # no database named
])
def test_any_other_database_is_refused_before_a_daemon_starts(dsn):
    with pytest.raises(rehearse.Failed, match="not a disposable bank"):
        rehearse.disposable_dsn(dsn, OWNED)


def test_an_owned_name_without_the_disposable_prefix_is_still_refused():
    with pytest.raises(rehearse.Failed):
        rehearse.disposable_dsn("postgresql://u:p@h/pseudolife", ["pseudolife"])


@pytest.mark.parametrize("output, want", [
    ("pseudolife-mcp 0.19.0\nruntime C:\\r\\runtimes\\000002 (source commit abc)\n",
     "C:\\r\\runtimes\\000002"),
    ("pseudolife-mcp 0.19.0\nruntime /r/runtimes/000001 (source rehearsal)\n",
     "/r/runtimes/000001"),
    ("pseudolife-mcp 0.19.0\n", None),        # ran outside a runtime: no proof
])
def test_the_selected_runtime_comes_from_the_shims_own_version_line(output, want):
    # Both shims print the same first line by design; only the runtime line
    # says which runtime's console answered.
    assert rehearse.selected_runtime(output) == want


def test_evidence_names_no_machine_path():
    # The evidence is committed to a public repo: scratch, checkout and home
    # paths become placeholders, in either slash style and inside text.
    home = Path.home()
    evidence = {"launcher": str(home / "AppData" / "x" / "bin" / "pseudolife-mcp.exe"),
                "stdout": f"runtime {home / 'r' / '000002'} (source commit abc)",
                "nested": [{"log": str(ROOT / "rust").replace("\\", "/")}]}
    text = str(rehearse.scrub(evidence, scratch=home / "AppData" / "x"))
    assert str(home) not in text and str(home).replace("\\", "/") not in text
    assert str(ROOT) not in text and home.name not in text
    assert "<scratch>" in text and "<repo>" in text and "<home>" in text


def test_a_failed_step_stops_the_run_unless_it_is_a_recorded_blocker():
    log = rehearse.Log()
    log.step("blocked path", False, fatal=False, why="known")
    assert log.steps[-1] == {"step": "blocked path", "ok": False, "why": "known"}
    with pytest.raises(rehearse.Failed, match="must hold"):
        log.step("must hold", False)
