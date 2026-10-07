"""Move the client side with the daemon: the shim, the plugin cache, Codex hooks.

    python ops/update_clients.py [--repo PATH] [--source SPEC] [--only shim,plugin,codex] [--json]

The logic is ``pseudolife_memory/client_updates.py`` (``pseudolife-mcp
update`` runs the same code from an installed package); this script runs
it from the checkout it sits in, ahead of any installed package on
``sys.path``, so an older installed release never answers for the
checkout being installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pseudolife_memory.client_updates import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
