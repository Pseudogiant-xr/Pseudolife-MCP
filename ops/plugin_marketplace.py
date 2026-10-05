"""Move the Claude Code plugin marketplace off the github source to HTTPS.

    python ops/plugin_marketplace.py

The installers up to 0.16.1 added the marketplace by its owner/repo
shorthand, which Claude Code records as a github source and refreshes over
SSH; on a host with no GitHub SSH key the plugin stops updating. The
installers run this before their plugin step; ``pseudolife-mcp update``
does the same inside its plugin step. The logic is
``pseudolife_memory.client_updates.migrate_marketplace``, run from the
checkout this script sits in. Prints one line (nothing when no marketplace
is recorded); exits 1 only when the move failed, leaving nothing changed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pseudolife_memory.client_updates import marketplace_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(marketplace_main())
