"""Seed a disposable bank through the Python daemon's own write path.

usage: python seed_entries.py <dsn>

Stores a few public-repo paragraphs with ``MemoryService.store`` under the
``continuum`` preset (so every row carries a continuum band name), with one
slot-bearing entry, a tagged one and one from source ``assistant``. The bank
is then served under the default ``flat`` preset, so hydration must rewrite
every band stamp. Run with ``PSEUDOLIFE_MCP_DATA_DIR`` pointing at a
disposable directory (the harness does).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

TEXTS = [
    ("Deploy only via pseudolife-mcp update: backup, rollback tag, daemon-only recreate, health, clients.", "spike-seed", None),
    ("A contender is a competing value parked against a slot instead of overwriting it.", "spike-seed", ["glossary"]),
    ("Remote clients authenticate with a bearer token in the Authorization header.", "assistant", None),
    ("A schema bump touches seven places together, including the version pin and the atlas.", "spike-seed", None),
    ("The cortex keeps one current value per entity and attribute slot; supersession keeps history.", "spike-seed", None),
    ("Never run docker compose down -v: the bank volumes are external precisely so that is survivable.", "spike-seed", ["ops"]),
]


def main() -> int:
    dsn = sys.argv[1]
    data = Path(os.environ["PSEUDOLIFE_MCP_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.yaml").write_text("memory:\n  miras:\n    preset: continuum\n", encoding="utf-8")
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=str(data), database_url=dsn)
    for text, source, tags in TEXTS:
        svc.store(text, source=source, tags=tags)
    svc.flush()
    svc._storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
