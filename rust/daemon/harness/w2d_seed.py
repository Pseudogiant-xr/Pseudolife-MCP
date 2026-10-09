"""Seed a disposable bank for the W2-D search harness, through Python's own
write paths only (``MemoryService.store`` / ``supersede`` / ``cortex_write``
/ ``set_add`` and ``PostgresStorage.add_chronicle_event``).

usage: python w2d_seed.py <dsn> [paragraphs]

The corpus is paragraphs of this repository's public docs, plus crafted
entries for every search channel: an assistant-sourced note, tagged notes,
slot-bearing statements, a superseded entry and its correction, chronicle
events (dated and undated) and cortex facts (scalar, set, assistant-origin,
constraint). Run with ``PSEUDOLIFE_MCP_DATA_DIR`` pointing at a disposable
directory.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

DOCS = ["docs/guide/configuration.md", "docs/guide/dreaming.md", "README.md",
        "docs/guide/agent-isolation.md", "CONTRIBUTING.md"]

CRAFTED = [
    ("Remote clients authenticate with a bearer token in the Authorization header.", "assistant", None),
    ("Never run docker compose down -v: the bank volumes are external precisely so that is survivable.", "ops-notes", ["ops"]),
    ("A contender is a competing value parked against a slot instead of overwriting it.", "glossary", ["glossary"]),
    ("My cat is named Jacque and she is a Ragdoll.", "conversation", None),
    ("The staging database host is pg-staging-2 since the migration.", "conversation", ["infra"]),
    ("We deployed release 0.15.0 to the homelab box on Tuesday.", "conversation", ["deploy"]),
    ("First we backed up the bank, then we tagged the rollback image, then we recreated the daemon.", "conversation", ["deploy"]),
    ("Later the clients were updated after the daemon came back healthy.", "conversation", ["deploy"]),
    ("process_chunk_v2 raised a KeyError when the cursor was empty.", "tool_result", ["bug"]),
    ("The error code PLX-4471 means the writer lease is held by another daemon.", "tool_result", ["bug"]),
]
SUPERSEDED = ("The extractor endpoint is http://10.0.0.5:1234/v1 on the old GPU box.",
              "The extractor endpoint moved to http://10.0.0.7:1234/v1 on the new GPU box.")
EVENTS = [
    ("2026-08-08", None, "maintainer", "deployed release 0.14.0 to the homelab box"),
    ("2026-08-12", None, "maintainer", "rolled back the release after a failed health check"),
    ("2026-09-01", None, "agent", "migrated the bank to schema 50"),
    (None, "last spring", "maintainer", "first installed the memory daemon on the homelab box"),
    ("2026-09-20", None, "agent", "deployed the reranker margin gate to the daemon"),
]
FACTS = [
    ("homelab box", "os", "Debian 13", 0.9, "auto"),
    ("staging database", "host", "pg-staging-2", 0.8, "auto"),
    ("daemon", "port", "8765", 0.9, "constraint"),
    ("Jacque", "type", "cat", 0.9, "auto"),
]
SET_MEMBERS = [("homelab box", "services", m) for m in ("postgres", "the memory daemon", "a GPU judge")]


def paragraphs(limit: int) -> list[str]:
    out: list[str] = []
    for rel in DOCS:
        path = REPO / rel
        if not path.exists():
            continue
        for para in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            text = " ".join(para.split())
            if 80 <= len(text) <= 900 and not text.startswith(("|", "```", "<")):
                out.append(text)
    return out[:limit]


def main() -> int:
    dsn = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    data = Path(os.environ["PSEUDOLIFE_MCP_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=str(data), database_url=dsn)
    stored = 0
    for text in paragraphs(limit):
        stored += bool(svc.store(text, source="docs").get("stored"))
    for text, source, tags in CRAFTED:
        stored += bool(svc.store(text, source=source, tags=tags).get("stored"))
    svc.store(SUPERSEDED[0], source="conversation")
    svc.supersede(old_text=SUPERSEDED[0], new_text=SUPERSEDED[1])
    for entity, attribute, value, conf, tol in FACTS:
        svc.cortex_write(entity, attribute, value, confidence=conf, distortion_tolerance=tol)
    for entity, attribute, member in SET_MEMBERS:
        svc.set_add(entity, attribute, member)
    svc.cortex_write("homelab box", "gpu", "RTX 4090", confidence=0.6)
    import time
    from pseudolife_memory.memory.cortex import _norm_key
    for date, phrase, actor, desc in EVENTS:
        svc._storage.add_chronicle_event({
            "occurred_at": f"{date}T00:00:00+00:00" if date else None,
            "occurred_phrase": phrase, "recorded_at": time.time(),
            "actor": actor, "actor_norm": _norm_key(actor),
            "description": desc, "description_norm": _norm_key(desc)})
    svc.flush()
    svc._storage.close()
    print(f"stored {stored} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
