"""Fixture-only entry point; the filename preserves the existing seed launch.

Dream operations run in dream-contract and are checked against committed
goldens. This module no longer runs a Python dream service.
"""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from dream_fixtures import seed


def main():
    if sys.argv[1:] != ["--seed", "-"]:
        raise SystemExit("Python dream oracle retired; use dream_cursors.py golden --rust-bin PATH")
    # In this seed-only child the variable carries the fixture target, not
    # the parent's production bank. seed_template guards it before launch;
    # retain any inherited _PSEUDOLIFE_PRODUCTION_DB snapshot for our guard.
    dsn = os.environ.pop("PSEUDOLIFE_MCP_DATABASE_URL")
    seed(dsn, json.load(sys.stdin))
    print("dream template seeded", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
