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


from dataclasses import dataclass, field  # noqa: E402
from typing import Protocol  # noqa: E402


@dataclass
class LoopState:
    entities: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    iterations: int = 0
    queries_issued: int = 0
    latency_ms: float = 0.0
    low_confidence: bool = False
    top_score: float = 0.0


def assembled_context(state: LoopState) -> list[str]:
    """Everything the arm 'read' — scored for gold presence + token cost."""
    return list(state.texts) + list(state.facts) + sorted(state.entities)


class Controller(Protocol):
    def seed_queries(self, question: str) -> list[str]: ...
    def expand(self, question: str, newly: list[str]) -> tuple[list[str], bool]: ...


class MechanicalController:
    """Deterministic controller: re-query with each newly discovered entity;
    stop when an iteration discovers nothing new. The LLM seam is a future
    subclass implementing the same two methods over a served model."""

    def seed_queries(self, question: str) -> list[str]:
        return [question]

    def expand(self, question: str, newly: list[str]) -> tuple[list[str], bool]:
        if not newly:
            return [], True
        return [f"{question} {name}" for name in newly], False


import time  # noqa: E402


def run_loop(svc, question: str, controller: Controller, *, use_graph: bool,
             known_entities: set[str], hop_cap: int = 3,
             top_k: int = 5) -> LoopState:
    """Iterative search(+graph) loop. Depth=1 graph expansion per iteration so
    N hops costs N iterations (honest iteration cost)."""
    state = LoopState()
    # Seed entities from the question itself (the LLM would read them; we spot).
    seeds = spot_entities(question, known_entities)
    state.entities.update(seeds)
    pending = list(seeds)              # entities awaiting graph expansion
    queries = controller.seed_queries(question)
    t0 = time.perf_counter()
    while True:
        state.iterations += 1
        newly: list[str] = []
        # 1) search step
        for q in queries:
            state.queries_issued += 1
            res = svc.search(q, top_k=top_k)
            if state.iterations == 1 and q == question:
                state.low_confidence = bool(res.get("low_confidence"))
                entries0 = res.get("entries", [])
                state.top_score = float(entries0[0]["score"]) if entries0 else 0.0
            for e in res.get("entries", []):
                txt = e.get("text", "")
                if txt and txt not in state.texts:
                    state.texts.append(txt)
                    for nm in spot_entities(txt, known_entities):
                        if nm not in state.entities:
                            state.entities.add(nm)
                            newly.append(nm)
        # 2) graph expansion step (arm A only): expand entities found so far
        next_pending: list[str] = []
        if use_graph:
            for nm in pending:
                nb = svc.graph_neighborhood(nm, depth=1)
                if not nb.get("found"):
                    continue
                for node in nb.get("nodes", []):
                    en = node.get("entity", "")
                    if en and en not in state.entities:
                        state.entities.add(en)
                        newly.append(en)
                        next_pending.append(en)
                    for f in node.get("facts", []):
                        fs = f"{f.get('attribute')}={f.get('value')}"
                        if fs not in state.facts:
                            state.facts.append(fs)
            # Also queue newly discovered entities from search for graph expansion
            for nm in newly:
                if nm not in next_pending and nm not in pending:
                    next_pending.append(nm)
        # 3) controller decides continuation
        queries, stop = controller.expand(question, newly)
        pending = next_pending
        if stop or not queries or state.iterations >= hop_cap:
            break
    state.latency_ms = (time.perf_counter() - t0) * 1000
    return state
