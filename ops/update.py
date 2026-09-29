"""Deploy this checkout's daemon (and, with --all, the client side).

    python ops/update.py [--all] [--rollback-tag pre-x] [--no-backup] [--allow-dirty] ...

``ops/update.ps1`` and ``ops/update.sh`` call this with their flags mapped;
the logic is ``pseudolife_memory/update_cli.py``, the same code
``pseudolife-mcp update`` runs from an installed package. This script runs
it from the checkout it sits in, ahead of any installed package on
``sys.path``, with ``--checkout`` set to that checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pseudolife_memory.update_cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--checkout" not in argv:
        argv = ["--checkout", str(ROOT)] + argv
    sys.exit(main(argv))
