#!/usr/bin/env python
"""Relation-extraction benchmark (dev-only).

Scores the dream graph-from-text path: runs each extractor rung's
``extract_relations`` over a hand-labeled corpus and reports edge precision/
recall/F1 plus four defect-aligned diagnostics (naming consistency, type-
violation rate, related-to share, over-extraction). Unlike ladder_sweep this
needs NO database and NO embedder — extract_relations is a pure model call.

The opus-4.8 ceiling rung is produced in-session by subagents (see README):
  1. ``--emit-prompts`` writes results/relations_corpus_prompts.json
  2. dispatch subagents to extract over those prompts (the user's Claude usage)
  3. collect into results/relations-opus-4.8.json
  4. ``--report`` scores it as the absolute ceiling.
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.graph import norm_name
from pseudolife_memory.storage.postgres import _BUILTIN_RELATIONS

# Relation vocab handed to the model — same as service._dream_extract_relations
# (builtins minus the lesson-only prefers/avoids).
RELATION_REGISTRY = [(n, d) for (n, d, *_rest) in _BUILTIN_RELATIONS
                     if n not in ("prefers", "avoids")]

# canonical name -> {type, aliases}. Types: runtime/host, service/process,
# tool, file, datastore, component, concept, person.
ENTITIES: dict[str, dict] = {
    "pseudolife-daemon":   {"type": "service",   "aliases": ["the daemon", "pseudolife daemon"]},
    "postgres":            {"type": "datastore", "aliases": ["the postgres db", "pg", "postgres 16"]},
    "docker-desktop":      {"type": "runtime",   "aliases": ["docker"]},
    "windows 11":          {"type": "runtime",   "aliases": ["the windows host"]},
    "cortex console":      {"type": "component", "aliases": ["the web console", "the console ui"]},
    "chromadb":            {"type": "datastore", "aliases": ["the reference bank"]},
    "gemma 4 e2b sidecar": {"type": "service",   "aliases": ["the extractor sidecar", "gemma sidecar"]},
    "memory_recall":       {"type": "tool",      "aliases": ["the recall tool"]},
    "networkx":            {"type": "tool",      "aliases": ["the networkx read-model"]},
    "config.yaml":         {"type": "file",      "aliases": ["the config file"]},
    "schema":              {"type": "concept",   "aliases": []},
    "the user":            {"type": "person",    "aliases": ["i", "me"]},
}

# Type constraints for STRUCTURAL relations only. depends-on/uses/configures/
# related-to are intentionally absent (any->any; never a type violation).
RELATION_CONSTRAINTS: dict[str, tuple[set, set]] = {
    "runs-on":        ({"service", "process", "component", "tool", "file"}, {"runtime", "host"}),
    "hosts":          ({"runtime", "host"}, {"service", "process", "component"}),
    "stores-data-in": ({"service", "process", "tool"}, {"datastore", "file"}),
    "part-of":        ({"component", "service", "file", "datastore"}, {"component", "service"}),
}

# Each note: source text + gold closed-vocab edges (possibly empty).
CORPUS: list[dict] = [
    # --- Class 1: clean structural ---
    {"text": "The daemon runs in Docker and persists everything to Postgres.",
     "edges": [("pseudolife-daemon", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "Cortex Console is part of the daemon and uses NetworkX for graph queries.",
     "edges": [("cortex console", "part-of", "pseudolife-daemon"),
               ("cortex console", "uses", "networkx")]},
    {"text": "The extractor sidecar runs on Docker, and the daemon depends on it for consolidation.",
     "edges": [("gemma 4 e2b sidecar", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "depends-on", "gemma 4 e2b sidecar")]},
    {"text": "ChromaDB is part of the daemon's reference bank.",
     "edges": [("chromadb", "part-of", "pseudolife-daemon")]},
    {"text": "memory_recall uses NetworkX to walk the graph.",
     "edges": [("memory_recall", "uses", "networkx")]},
    {"text": "config.yaml configures the daemon.",
     "edges": [("config.yaml", "configures", "pseudolife-daemon")]},
    {"text": "The daemon runs on the Windows 11 host.",
     "edges": [("pseudolife-daemon", "runs-on", "windows 11")]},
    # --- Class 2: canonicalization probes (same entity, varied surface forms) ---
    {"text": "The gemma sidecar runs on Docker too.",
     "edges": [("gemma 4 e2b sidecar", "runs-on", "docker-desktop")]},
    {"text": "The daemon writes the entities table to pg.",
     "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "The console ui is part of the daemon.",
     "edges": [("cortex console", "part-of", "pseudolife-daemon")]},
    # --- Class 3: null notes (no entity-to-entity relationship) ---
    {"text": "Honestly the new dashboard looks way cleaner than the old one.", "edges": []},
    {"text": "I spent way too long debugging this yesterday.", "edges": []},
    {"text": "We should probably write more tests at some point.", "edges": []},
    # --- Class 4: type traps ---
    {"text": "The migration touched schema v11 over the weekend.", "edges": []},
    {"text": "The user is on Windows 11.", "edges": []},
    {"text": "The daemon's data ends up in the nightly backup folder.", "edges": []},
]


def alias_index(entities: dict[str, dict]) -> dict[str, str]:
    """norm_name(surface) -> canonical, over canonical names + their aliases."""
    idx: dict[str, str] = {}
    for canon, meta in entities.items():
        for surface in [canon, *meta.get("aliases", [])]:
            idx[norm_name(surface)] = canon
    return idx


def resolve(name: str, idx: dict[str, str]) -> str | None:
    return idx.get(norm_name(name))
