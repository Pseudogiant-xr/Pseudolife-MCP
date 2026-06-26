"""Console-script dispatch — deliberately torch-free at import time.

* ``pseudolife-mcp``           — stdio shim: find/start the daemon, proxy.
* ``pseudolife-mcp serve``     — the HTTP memory daemon (deployment mode).
* ``pseudolife-mcp embedded``  — v0.1 in-process stdio server (escape hatch).
* ``pseudolife-mcp briefing``  — print the session-start briefing (for a hook).

The heavy imports (torch, sentence-transformers) happen only inside the
``serve`` / ``embedded`` branches; the shim path imports nothing heavier
than the mcp client SDK, so per-session startup stays ~instant.
"""

from __future__ import annotations

import sys


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "shim"
    if mode == "serve":
        from pseudolife_memory.daemon import run_daemon
        run_daemon()
    elif mode == "embedded":
        from pseudolife_memory.mcp_server import main as embedded_main
        embedded_main()
    elif mode == "shim":
        from pseudolife_memory.shim import run_shim
        run_shim()
    elif mode == "briefing":
        from pseudolife_memory.briefing_cli import run_briefing
        run_briefing()
    else:
        print(
            f"unknown mode {mode!r}; use: serve | embedded | (no arg = shim)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
