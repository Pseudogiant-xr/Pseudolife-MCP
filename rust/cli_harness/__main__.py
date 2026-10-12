"""``python rust/cli_harness --row mail`` (or ``python -m cli_harness`` from rust/)."""

from __future__ import annotations

import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "cli_harness"  # noqa: A001

from cli_harness.runner import isolated_relaunch, main  # noqa: E402

# Relaunched once without bytecode caches before any oracle code is imported.
_relaunched = isolated_relaunch()
raise SystemExit(main() if _relaunched is None else _relaunched)
