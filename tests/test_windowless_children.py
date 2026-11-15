"""tests/windowless_children.py: test children open no console window on a
developer's desktop, and a spawn that chose its console keeps its choice."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import windowless_children as wc

ROOT = Path(__file__).resolve().parents[1]
CREATE_NEW_PROCESS_GROUP = 0x00000200


@pytest.mark.parametrize(("given", "expected"), [
    (0, wc.CREATE_NO_WINDOW),
    # A harness that sends CTRL_BREAK_EVENT asks for its own group, not a console.
    (CREATE_NEW_PROCESS_GROUP, CREATE_NEW_PROCESS_GROUP | wc.CREATE_NO_WINDOW),
])
def test_a_console_child_of_a_console_less_run_gets_no_window(given, expected):
    assert wc.flags_for(given, parent_has_console=False) == expected


@pytest.mark.parametrize("chosen", [
    # The terminal tests drive a console of their own (CREATE_NEW_CONSOLE).
    wc.CREATE_NEW_CONSOLE,
    wc.DETACHED_PROCESS,
    wc.CREATE_NO_WINDOW,
    wc.CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP,
])
def test_a_spawn_that_chose_its_console_keeps_it(chosen):
    assert wc.flags_for(chosen, parent_has_console=False) == chosen


@pytest.mark.parametrize("given", [0, CREATE_NEW_PROCESS_GROUP, wc.CREATE_NEW_CONSOLE])
def test_a_run_with_a_console_changes_nothing(given):
    # A developer's terminal or a CI runner: children share that console
    # and open no window, so the flags stay exactly as the test gave them.
    assert wc.flags_for(given, parent_has_console=True) == given


def _fake_subprocess():
    class Popen:
        def __init__(self, args, *, creationflags=0, **kwargs):
            self.args, self.creationflags, self.kwargs = args, creationflags, kwargs

    return SimpleNamespace(Popen=Popen)


@pytest.mark.parametrize("has_console", [False, True])
def test_install_wraps_popen_once_on_windows(monkeypatch, has_console):
    fake = _fake_subprocess()
    monkeypatch.setattr(wc, "subprocess", fake)
    monkeypatch.setattr(wc.os, "name", "nt")
    monkeypatch.setattr(wc, "_has_console", lambda: has_console)
    wc.install()
    wc.install()   # idempotent: tests/ and rust/ both call it
    child = fake.Popen(["pwsh", "-NoProfile"], stdout=None)
    assert child.creationflags == (0 if has_console else wc.CREATE_NO_WINDOW)
    assert child.args == ["pwsh", "-NoProfile"] and child.kwargs == {"stdout": None}
    chosen = fake.Popen(["cmd"], creationflags=wc.CREATE_NEW_CONSOLE)
    assert chosen.creationflags == wc.CREATE_NEW_CONSOLE


def test_install_does_nothing_off_windows(monkeypatch):
    fake = _fake_subprocess()
    original = fake.Popen.__init__
    monkeypatch.setattr(wc, "subprocess", fake)
    monkeypatch.setattr(wc.os, "name", "posix")
    wc.install()
    assert fake.Popen.__init__ is original


def test_the_suite_conftest_installs_it():
    # rust/conftest.py installs it too: rust/ci/test_windowless_conftest.py
    # checks that, beside the file it reads.
    assert "windowless_children.install()" in (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
