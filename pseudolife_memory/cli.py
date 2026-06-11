"""Console-script dispatch — deliberately torch-free at import time.

* ``pseudolife-mcp``           — stdio shim: find/start the daemon, proxy.
* ``pseudolife-mcp serve``     — the HTTP memory daemon (deployment mode).
* ``pseudolife-mcp embedded``  — v0.1 in-process stdio server (escape hatch).
* ``pseudolife-mcp age-sync``  — rebuild the AGE graph mirror from the tables.

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
    elif mode == "age-sync":
        import os
        from pseudolife_memory.storage.age import AgeGraph
        from pseudolife_memory.storage.postgres import PostgresStorage
        url = os.environ.get(
            "PSEUDOLIFE_MCP_DATABASE_URL",
            "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
        )
        storage = PostgresStorage(url)
        if not storage.capabilities.get("age_available"):
            print("age-sync: AGE extension unavailable on this Postgres "
                  "(use the ops/Dockerfile.pg compose image).", file=sys.stderr)
            sys.exit(1)
        summary = AgeGraph(storage.conn).resync(storage)
        print(f"age-sync: mirrored {summary['entities']} entities, "
              f"{summary['edges']} edges")
    else:
        print(
            f"unknown mode {mode!r}; use: serve | embedded | age-sync | "
            f"(no arg = shim)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
