"""``python rust/cli_harness --row mail`` (or ``python -m cli_harness`` from rust/)."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

# On Windows, console children of a run that has no console open no window
# on the developer's desktop (tests/windowless_children.py); before the
# relaunch below, which is itself such a child.
runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests" / "windowless_children.py"))["install"]()

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "cli_harness"  # noqa: A001

from cli_harness.runner import isolated_relaunch, main  # noqa: E402

# Relaunched once without bytecode caches before any oracle code is imported.
_relaunched = isolated_relaunch()
raise SystemExit(main() if _relaunched is None else _relaunched)
