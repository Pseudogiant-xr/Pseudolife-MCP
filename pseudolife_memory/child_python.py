"""The command line that re-runs one of this package's modules in a child
Python process.

``python -m`` puts the working directory first on ``sys.path``. A child
started that way from inside a checkout of this repository imports the
checkout's ``pseudolife_memory``, not the package its parent ran: an older
checkout then refuses a mode the parent offered (``conductor-watch --detach``
failed with "unknown mode", 2026-11-03), or the child silently runs other
code. ``-P`` (Python 3.11+) leaves the working directory off the child's
path and changes nothing else: the environment, the working directory
itself and the module's ``sys.argv`` stay as ``-m`` gives them. ``-I``
would also drop the ``PSEUDOLIFE_*`` environment, and a neutral working
directory would change what relative paths mean to the child, and a
temporary one cannot outlive a detached child.

Two cases keep plain ``-m``: Python 3.10, which has no ``-P`` (it says so on
stderr, once, when the working directory holds a ``pseudolife_memory``), and
a parent that imported ``pseudolife_memory`` from the working directory
itself (a checkout run with no install), where the working directory is the
parent's own code and leaving it off would leave the child nothing to import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLI = "pseudolife_memory.cli"

# Whether this process has named a package that 3.10 lets shadow its children.
_shadow_named = False


def module_argv(module: str, *args: str, saved: bool = False) -> list[str]:
    """``[python, -m, module, *args]`` for a child that inherits this
    process's working directory: it imports ``pseudolife_memory`` from its
    import path (an installed runtime's site-packages, an editable install's
    checkout), never from that working directory unless this process's own
    package came from there.

    ``saved``: the command is stored and run later from another working
    directory (a scheduled task, a service), so the 3.10 notice, which names
    this one, stays unsaid."""
    if not _imported_from_working_directory():
        if sys.version_info >= (3, 11):
            return [sys.executable, "-P", "-m", module, *args]
        if not saved:
            _name_a_shadowing_package()
    return [sys.executable, "-m", module, *args]


def _name_a_shadowing_package() -> None:
    """On Python 3.10, say once when the working directory holds a
    ``pseudolife_memory`` the child will import instead of this one."""
    global _shadow_named
    try:
        shadow = Path.cwd() / "pseudolife_memory"
        present = shadow.is_dir()
    except OSError:
        return
    if not present or _shadow_named:
        return
    _shadow_named = True
    print(f"pseudolife-mcp: Python 3.10 cannot keep {shadow} off a child process's import path "
          "(that needs Python 3.11's -P), so the child may run that copy instead of this one; "
          "run it from another directory.", file=sys.stderr, flush=True)


def _imported_from_working_directory() -> bool:
    import pseudolife_memory
    root = Path(pseudolife_memory.__file__).resolve().parent.parent
    try:
        here = Path.cwd().resolve()
    except OSError:     # the working directory is gone: nothing there to import
        return False
    return os.path.normcase(str(root)) == os.path.normcase(str(here))


def cli_argv(*args: str, saved: bool = False) -> list[str]:
    """``pseudolife-mcp <args>`` run by this interpreter and this package."""
    return module_argv(CLI, *args, saved=saved)
