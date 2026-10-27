"""Record the Python constants the Rust gate embeds, as goldens.

``goldens/routes.json`` is the Console route table (``ConsoleRoutes.table``),
so 404 versus 405 match; ``goldens/checkin.txt`` is
``coordination.CHECKIN_TEXT``, the board check-in
``/api/hook/coordination-start`` serves; ``goldens/memory-loop-block.txt``
is ``session_hook.MEMORY_LOOP_BLOCK``, the policy text
``/api/hook/memory-policy`` serves; ``goldens/startup-memory-core.txt`` and
``goldens/onboarding-block.txt`` are ``session_hook.STARTUP_MEMORY_CORE``
and ``ONBOARDING_BLOCK``, which ``/api/hook/session-start`` serves.
``--check`` exits 1 when any has moved in Python.

usage: python record_routes.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TARGET = HERE / "goldens" / "routes.json"
CHECKIN = HERE / "goldens" / "checkin.txt"
MEMORY_LOOP = HERE / "goldens" / "memory-loop-block.txt"
STARTUP_CORE = HERE / "goldens" / "startup-memory-core.txt"
ONBOARDING = HERE / "goldens" / "onboarding-block.txt"


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
    routes = json.dumps(table(), indent=1) + "\n"
    from pseudolife_memory.coordination import CHECKIN_TEXT
    from pseudolife_memory.web.session_hook import (
        MEMORY_LOOP_BLOCK, ONBOARDING_BLOCK, STARTUP_MEMORY_CORE)
    files = {TARGET: routes, CHECKIN: CHECKIN_TEXT, MEMORY_LOOP: MEMORY_LOOP_BLOCK,
             STARTUP_CORE: STARTUP_MEMORY_CORE, ONBOARDING: ONBOARDING_BLOCK}
    if "--check" in sys.argv:
        for path, text in files.items():
            have = path.read_bytes().decode("utf-8").replace("\r\n", "\n") if path.exists() else ""
            if have != text:
                print(f"{path} is stale; rerun record_routes.py", file=sys.stderr)
                return 1
        print("routes.json, checkin.txt and the session hook texts match the Python source")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_bytes(text.encode("utf-8"))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
