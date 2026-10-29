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
    # The harness modules import each other by name; the path entry is only
    # needed while they load, and is removed again so no later test sees it.
    added = str(HARNESS) not in sys.path
    if added:
        sys.path.insert(0, str(HARNESS))
    try:
        spec = importlib.util.spec_from_file_location("rehearse_activation_under_test",
                                                      HARNESS / "rehearse_activation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(str(HARNESS))


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


def test_text_is_scrubbed_before_it_is_cut(monkeypatch):
    # A tail that starts inside a home path must not keep the account name.
    home = Path.home()
    monkeypatch.setattr(rehearse, "SCRATCH", home / "scratch-root")
    text = f"failed at {home / 'scratch-root' / 'bin' / 'x.exe'} and " + "y" * 30
    for n in range(5, len(text)):
        assert home.name.lower() not in rehearse.tail(text, n).lower()
    shouted = str(home).upper() + "/data"
    assert home.name.lower() not in str(rehearse.scrub(shouted, scratch=home / "s")).lower()


def test_a_failed_step_stops_the_run_unless_it_is_a_recorded_blocker():
    log = rehearse.Log()
    log.step("blocked path", False, fatal=False, why="known")
    assert log.steps[-1] == {"step": "blocked path", "ok": False, "why": "known"}
    with pytest.raises(rehearse.Failed, match="must hold"):
        log.step("must hold", False)


@pytest.mark.parametrize("refuses,preserves_stamp", [(True, True), (False, True), (True, False)])
def test_newer_bank_checks_finish_on_refusal_and_reject_unsafe_starts(
        monkeypatch, tmp_path, refuses, preserves_stamp):
    from types import SimpleNamespace

    state = SimpleNamespace(stamp=rehearse.SCHEMA_META_VERSION, facts={}, entries=[], ready=True)

    class Mcp:
        def call(self, tool, arguments):
            if tool == "memory_store":
                state.entries.append(arguments["text"])
            elif tool == "memory_fact_set":
                state.facts[arguments["entity"], arguments["attribute"]] = arguments["value"]
            return {"isError": False, "value": {}}

    class Bank:
        def __init__(self, *args):
            self.dsn, self.port = "fixture", 1234

        def start(self, kind, *, refused=False):
            newer = state.stamp > rehearse.SCHEMA_META_VERSION
            refusing = newer and (kind == "rust" or refuses)
            if refusing and not refused:
                raise rehearse.Failed("a correctly refusing daemon never becomes ready")
            state.ready = not refusing
            if newer and kind == "python" and not preserves_stamp:
                state.stamp = rehearse.SCHEMA_META_VERSION
            log_path = tmp_path / f"{kind}.log"
            log_path.write_text("schema is newer than this build" if refusing else "ready")
            return SimpleNamespace(log=log_path, proc=SimpleNamespace(poll=lambda: None))

        def stop(self):
            return 0

        def mcp(self):
            return Mcp()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, parameters):
            assert statement.startswith("UPDATE meta SET value")
            state.stamp = int(parameters[0])

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(rehearse, "Bank", Bank)
    monkeypatch.setattr(rehearse, "_meta", lambda dsn: {"schema_version": state.stamp})
    monkeypatch.setattr(rehearse, "_fact", lambda m, e, a: state.facts.get((e, a)))
    monkeypatch.setattr(rehearse, "_found", lambda m, q, text: text in state.entries)
    monkeypatch.setattr(rehearse.h.w1a, "call", lambda *a, **k: {"json": {"ready": state.ready}})
    clock = iter(range(0, 1000, 30))
    monkeypatch.setattr(rehearse, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    log = rehearse.Log()
    rehearse.bank_leg(tmp_path, tmp_path / "daemon", log, [], None, 1234)
    step = next(s for s in log.steps if s["step"] == "the Python daemon leaves a newer build's stamp alone")
    assert step["ok"] is (refuses and preserves_stamp)
