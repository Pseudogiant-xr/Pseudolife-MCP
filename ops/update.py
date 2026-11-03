"""Deploy this checkout's daemon (and, with --all, the client side).

    python ops/update.py [--engine python|rust] [--shadow] [--all] [--rollback-tag pre-x] ...

``ops/update.ps1`` and ``ops/update.sh`` call this with their flags mapped;
the logic is ``pseudolife_memory/update_cli.py``, the same code
``pseudolife-mcp update`` runs from an installed package. This script runs
it from the checkout it sits in, ahead of any installed package on
``sys.path``, with ``--checkout`` set to that checkout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Explicit native opt-in is handled before importing the Python oracle.
# This launcher is for callers already invoking Python; the shell wrappers
# call the native binary directly on Python-free hosts.
if __name__ == "__main__" and os.environ.get("PSEUDOLIFE_NATIVE_UPDATE") == "1":
    native = os.environ.get("PSEUDOLIFE_NATIVE_CLI") or os.environ.get("PSEUDOLIFE_SHIM_LAUNCHER")
    if not native:
        base = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") if os.name == "nt"
                else Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"))
        native = str(base / "pseudolife-mcp/bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"))
    if not Path(native).is_file():
        print("native update CLI is missing; install the native runtime or set PSEUDOLIFE_NATIVE_CLI to its binary; nothing was changed", file=sys.stderr)
        sys.exit(2)
    argv = sys.argv[1:]
    if "--checkout" not in argv:
        argv = ["--checkout", str(ROOT)] + argv
    try:
        sys.exit(subprocess.run([native, "update", *argv], check=False).returncode)
    except OSError:
        print("cannot start the native update CLI; check PSEUDOLIFE_NATIVE_CLI; nothing was changed", file=sys.stderr)
        sys.exit(2)

sys.path.insert(0, str(ROOT))

from pseudolife_memory.update_cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--checkout" not in argv:
        argv = ["--checkout", str(ROOT)] + argv
    sys.exit(main(argv))
