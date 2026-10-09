"""Record the Console route table (``ConsoleRoutes.table``) as a golden.

The Rust daemon embeds ``goldens/routes.json`` to answer 404 versus 405 the
way Python does; ``--check`` exits 1 when the Python table has moved.

usage: python record_routes.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TARGET = HERE / "goldens" / "routes.json"


def table() -> dict:
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.web.routes import ConsoleRoutes

    class _Unbuilt:  # registration reads no service attribute
        pass

    routes = sorted(ConsoleRoutes(_Unbuilt()).table)
    return {"source": "pseudolife_memory.web.routes.ConsoleRoutes",
            "GET": [p for m, p in routes if m == "GET"],
            "POST": [p for m, p in routes if m == "POST"]}


def main() -> int:
    want = json.dumps(table(), indent=1) + "\n"
    if "--check" in sys.argv:
        have = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if have.replace("\r\n", "\n") != want:
            print(f"{TARGET} is stale; rerun record_routes.py", file=sys.stderr)
            return 1
        print("routes.json matches ConsoleRoutes")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(want, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
