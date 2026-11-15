"""Keep the console children tests start off a developer's desktop (Windows).

A console program (``pwsh``, ``powershell``, ``cmd``, ``python``) started by
a process that has no console of its own gets a new console, and so a new
visible Windows Terminal or conhost window that takes the foreground. That
is how a test run started by an agent, whose interpreter has no console,
interrupted the maintainer's desktop every few seconds (15 November 2026):
``rust/ci`` and the installer tests each spawn ``pwsh``.

:func:`install` makes every ``subprocess.Popen`` in the test process add
``CREATE_NO_WINDOW`` when the test process itself has no console: the child
then gets a console with no window, and its own children share it. A spawn
that asks for a console arrangement itself (``CREATE_NEW_CONSOLE``,
``DETACHED_PROCESS`` or ``CREATE_NO_WINDOW``) is left as it is, and so is
every spawn of a process that has a console, shown or hidden (a
developer's terminal, a CI runner), whose children share that console and
open no window anyway. Without a console a parent can send no console
control event to its children, so nothing a test did there is lost.
Elsewhere it does nothing. ``tests/conftest.py`` and ``rust/conftest.py``
call it; ``tests/helpers.py`` already started its daemons this way.
"""

from __future__ import annotations

import os
import subprocess

# Win32 process creation flags (WinBase.h); named here so the rule reads the
# same off Windows, where subprocess does not define them.
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
_CONSOLE_CHOICES = CREATE_NEW_CONSOLE | DETACHED_PROCESS | CREATE_NO_WINDOW

_INSTALLED = "_pseudolife_windowless_children"


def flags_for(creationflags: int, *, parent_has_console: bool) -> int:
    """The creation flags a test child starts with."""
    if parent_has_console or creationflags & _CONSOLE_CHOICES:
        return creationflags
    return creationflags | CREATE_NO_WINDOW


def _has_console() -> bool:
    """Whether a console is attached, with or without a window.
    GetConsoleProcessList fails (returns 0) only without one. Not
    GetConsoleWindow, which is NULL for a hidden console too: children
    there share the hidden console (no window), and a harness that sends
    them CTRL_BREAK_EVENT needs them to keep sharing it."""
    import ctypes

    processes = (ctypes.c_uint32 * 1)()
    return ctypes.windll.kernel32.GetConsoleProcessList(processes, 1) != 0


def install() -> None:
    """Wrap ``subprocess.Popen`` once for this process (Windows only)."""
    if os.name != "nt" or getattr(subprocess.Popen, _INSTALLED, False):
        return
    has_console = _has_console()
    original = subprocess.Popen.__init__

    def __init__(self, *args, creationflags=0, **kwargs):
        original(self, *args, creationflags=flags_for(creationflags, parent_has_console=has_console), **kwargs)

    subprocess.Popen.__init__ = __init__
    setattr(subprocess.Popen, _INSTALLED, True)
