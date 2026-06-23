#!/usr/bin/env python
"""MemCoT-style iterative retrieval loop — measurement harness (dev-only).

Measures whether an iterative search->graph-expand->re-query loop lifts
multi-hop recall over single-shot search. Three arms: baseline (single
search), loop-no-graph, loop+graph. Deterministic seeded edges isolate the
retrieval loop from extraction. See
docs/specs/2026-06-23-memcot-retrieval-loop-design.md.

Isolation: dedicated pseudolife_memory_bench DB, CPU only, no served LLM,
live bank untouched.
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder_sweep import approx_tokens, value_present  # noqa: E402

# ---------------------------------------------------------------------------
# Corpus — each fact appears twice: as an ingested snippet AND a seeded edge.
# Questions span 1/2/3 hops; gold = terminal entity name.
# ---------------------------------------------------------------------------
CORPUS: list[dict] = [
    # chain 1 (2-hop): checkout-svc -> billing-lib -> jvm-21
    {"snippet": "The checkout-svc depends-on the billing-lib module.",
     "edges": [("checkout-svc", "depends-on", "billing-lib")]},
    {"snippet": "Internally, billing-lib runs-on the jvm-21 runtime.",
     "edges": [("billing-lib", "runs-on", "jvm-21")]},
    # chain 2 (3-hop): web-frontend -> api-gateway -> auth-svc -> session-store
    {"snippet": "The web-frontend uses the api-gateway for all calls.",
     "edges": [("web-frontend", "uses", "api-gateway")]},
    {"snippet": "Our api-gateway depends-on the auth-svc for tokens.",
     "edges": [("api-gateway", "depends-on", "auth-svc")]},
    {"snippet": "The auth-svc stores-data-in the session-store backend.",
     "edges": [("auth-svc", "stores-data-in", "session-store")]},
    # chain 3 (2-hop): order-svc -> commerce-platform -> k8s-prod
    {"snippet": "order-svc is part-of the commerce-platform.",
     "edges": [("order-svc", "part-of", "commerce-platform")]},
    {"snippet": "The commerce-platform runs-on the k8s-prod cluster.",
     "edges": [("commerce-platform", "runs-on", "k8s-prod")]},
    # 1-hop guardrail facts
    {"snippet": "report-svc runs-on the jvm-17 runtime.",
     "edges": [("report-svc", "runs-on", "jvm-17")]},
    {"snippet": "cache-svc uses redis-7 for hot keys.",
     "edges": [("cache-svc", "uses", "redis-7")]},
    {"snippet": "search-svc stores-data-in the es-cluster index.",
     "edges": [("search-svc", "stores-data-in", "es-cluster")]},
]

DISTRACTORS: list[str] = [
    "The internal wiki lives at wiki.corp.local.",
    "Daily standups are at 9:30am in the main channel.",
    "The frontend bundle is about 2MB after tree-shaking.",
    "Release notes ship to the changelog every Friday.",
    "The staging autoscaler kicks in above 70% CPU.",
]

QUESTIONS: list[dict] = [
    # 1-hop
    {"question": "What runtime does report-svc run on?", "gold": "jvm-17", "hops": 1},
    {"question": "What does cache-svc use for hot keys?", "gold": "redis-7", "hops": 1},
    {"question": "Where does search-svc store its data?", "gold": "es-cluster", "hops": 1},
    # 2-hop
    {"question": "What runtime does checkout-svc run on?", "gold": "jvm-21", "hops": 2},
    {"question": "What cluster does order-svc run on?", "gold": "k8s-prod", "hops": 2},
    # 3-hop
    {"question": "Where does the web-frontend ultimately store data?",
     "gold": "session-store", "hops": 3},
]

KNOWN_ENTITIES: set[str] = {
    e for rec in CORPUS for (s, _r, d) in rec["edges"] for e in (s, d)
}


def spot_entities(text: str, known: set[str]) -> list[str]:
    """Known entity names present in ``text`` (word-boundary match).

    Mechanical stand-in for NER: the LLM controller would name entities by
    reading; the mechanical controller matches against the known vocabulary.
    """
    return [name for name in known if value_present(text, name)]
