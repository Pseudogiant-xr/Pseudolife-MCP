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
#
# HARDENED (2026-06-23): an earlier tiny corpus let single-shot search hit 1.0
# (top_k returned a third of it; terminals echoed the question), so no lift was
# measurable. This version makes single-shot genuinely hard for multi-hop:
#   * multi-hop TERMINAL snippets neither name the question's subject nor echo
#     its predicate vocab (e.g. "targets jdk-21 for its bytecode", not "runs on
#     jdk-21"), so vector search anchored on the subject can't surface them;
#   * ~28 DISTRACTORS echo the question predicates ("runs on", "stores in",
#     "cluster", "hardware") about NON-graph entities, crowding the top-k;
#   * 1-hop GUARDRAILS keep subject+predicate in one snippet (single-shot SHOULD
#     find these) so a loop regression on easy lookups is visible.
# Every edge's snippet still co-mentions both endpoints (faithful to how a real
# extractor would have built the edge from text).
# ---------------------------------------------------------------------------
CORPUS: list[dict] = [
    # chain 1 (2-hop): checkout-service -> billing-engine -> jdk-21
    {"snippet": "The checkout-service is built on top of the billing-engine library.",
     "edges": [("checkout-service", "depends-on", "billing-engine")]},
    {"snippet": "The billing-engine library targets jdk-21 for its bytecode.",
     "edges": [("billing-engine", "runs-on", "jdk-21")]},
    # chain 2 (3-hop): web-portal -> gateway-proxy -> token-issuer -> vault-kms
    {"snippet": "The web-portal routes every request through the gateway-proxy.",
     "edges": [("web-portal", "uses", "gateway-proxy")]},
    {"snippet": "The gateway-proxy hands authentication off to the token-issuer.",
     "edges": [("gateway-proxy", "depends-on", "token-issuer")]},
    {"snippet": "The token-issuer keeps its signing secrets inside vault-kms.",
     "edges": [("token-issuer", "stores-data-in", "vault-kms")]},
    # chain 3 (2-hop): order-service -> commerce-suite -> nomad-cluster
    {"snippet": "order-service ships as one module of the commerce-suite.",
     "edges": [("order-service", "part-of", "commerce-suite")]},
    {"snippet": "The commerce-suite is scheduled onto the nomad-cluster.",
     "edges": [("commerce-suite", "runs-on", "nomad-cluster")]},
    # chain 4 (2-hop): mobile-app -> sync-service -> dynamo-table
    {"snippet": "The mobile-app talks to the sync-service for offline merges.",
     "edges": [("mobile-app", "uses", "sync-service")]},
    {"snippet": "The sync-service writes its merge journals to the dynamo-table.",
     "edges": [("sync-service", "stores-data-in", "dynamo-table")]},
    # chain 5 (3-hop): analytics-ui -> query-engine -> column-store -> bare-metal-7
    {"snippet": "analytics-ui is wired directly to the query-engine.",
     "edges": [("analytics-ui", "depends-on", "query-engine")]},
    {"snippet": "The query-engine leans on the column-store for large scans.",
     "edges": [("query-engine", "uses", "column-store")]},
    {"snippet": "The column-store was provisioned on bare-metal-7.",
     "edges": [("column-store", "runs-on", "bare-metal-7")]},
    # chain 6 (2-hop): notify-service -> queue-broker -> kafka-cluster
    {"snippet": "notify-service is a client of the queue-broker.",
     "edges": [("notify-service", "depends-on", "queue-broker")]},
    {"snippet": "The queue-broker is backed by the kafka-cluster.",
     "edges": [("queue-broker", "runs-on", "kafka-cluster")]},
    # 1-hop guardrail facts (subject + predicate in one snippet)
    {"snippet": "report-svc runs-on the jvm-17 runtime.",
     "edges": [("report-svc", "runs-on", "jvm-17")]},
    {"snippet": "cache-svc uses redis-7 for hot keys.",
     "edges": [("cache-svc", "uses", "redis-7")]},
    {"snippet": "search-svc stores-data-in the es-cluster index.",
     "edges": [("search-svc", "stores-data-in", "es-cluster")]},
    # hub fixture: shared-config is depended on by many heads (high degree).
    {"snippet": "The checkout-service reads its limits from shared-config.",
     "edges": [("checkout-service", "depends-on", "shared-config")]},
    {"snippet": "The order-service reads feature flags from shared-config.",
     "edges": [("order-service", "depends-on", "shared-config")]},
    {"snippet": "The web-portal loads its theme from shared-config.",
     "edges": [("web-portal", "depends-on", "shared-config")]},
    {"snippet": "The mobile-app fetches toggles from shared-config.",
     "edges": [("mobile-app", "depends-on", "shared-config")]},
    {"snippet": "The analytics-ui reads dashboards config from shared-config.",
     "edges": [("analytics-ui", "depends-on", "shared-config")]},
    {"snippet": "The notify-service reads templates from shared-config.",
     "edges": [("notify-service", "depends-on", "shared-config")]},
]

# Predicate-echoing noise about NON-graph entities — crowds the top-k so the
# real multi-hop terminal (which does NOT echo the predicate) gets pushed out.
DISTRACTORS: list[str] = [
    "The reporting-dashboard runs on jdk-17 in production.",
    "Batch ETL jobs run on the spark-runtime overnight.",
    "The legacy-portal still runs on jdk-8.",
    "Our CI workers run on ephemeral linux-vms.",
    "Most microservices run on the staging-cluster during QA.",
    "Cron tasks run on the scheduler-node.",
    "The image-resizer runs on lambda-functions.",
    "Developer laptops run on macos-14.",
    "The sandbox environment runs on a single droplet.",
    "The recommendation-model runs on a gpu-node.",
    "The data-lake stores everything in parquet-files.",
    "User uploads are persisted to the blob-store.",
    "Session cookies are kept in an encrypted jar.",
    "The audit-log writes records to cold-storage.",
    "Metrics are stored in a prometheus-tsdb.",
    "Nightly backups land in a glacier-archive.",
    "Logs are shipped to an elk-stack.",
    "The marketing-site is hosted on a static-cdn.",
    "The payment-gateway integration uses the stripe-api.",
    "The analytics-pipeline runs on airflow-workers.",
    "The search-portal indexes documents hourly.",
    "Feature flags live in a config-service.",
    "The status-page polls each service every minute.",
    "The chat-widget talks to a third-party saas.",
    "The billing-dashboard renders invoices as pdfs.",
    "Our test-suite runs on github-runners.",
    "The on-call rotation is tracked in a spreadsheet.",
    "The CDN purges its cache on every deploy.",
]

QUESTIONS: list[dict] = [
    # 1-hop guardrails (single-shot SHOULD answer these)
    {"question": "What runtime does report-svc run on?", "gold": "jvm-17", "hops": 1},
    {"question": "What does cache-svc use for hot keys?", "gold": "redis-7", "hops": 1},
    {"question": "Where does search-svc store its data?", "gold": "es-cluster", "hops": 1},
    # 2-hop
    {"question": "What does the checkout-service run on?", "gold": "jdk-21", "hops": 2},
    {"question": "Which cluster hosts the order-service?", "gold": "nomad-cluster", "hops": 2},
    {"question": "Where does the mobile-app keep its data?", "gold": "dynamo-table", "hops": 2},
    {"question": "What does notify-service run on?", "gold": "kafka-cluster", "hops": 2},
    # 3-hop
    {"question": "Where does the web-portal's data ultimately get persisted?",
     "gold": "vault-kms", "hops": 3},
    {"question": "What hardware does the analytics-ui ultimately run on?",
     "gold": "bare-metal-7", "hops": 3},
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


def assembled_from_recall(result: dict) -> list[str]:
    """Flatten service.recall output into the scorer's context list:
    texts + per-entity facts + entity names (mirrors assembled_context)."""
    out = list(result.get("texts", []))
    for ent in result.get("entities", []):
        for f in ent.get("facts", []):
            out.append(f"{f.get('attribute')}={f.get('value')}")
        out.append(ent.get("entity", ""))
    return [s for s in out if s]


def recall_record(result: dict, gold: str, hops: int) -> dict:
    ctx = assembled_from_recall(result)
    return {
        "hops": hops,
        "recovered": any(value_present(s, gold) for s in ctx),
        "iterations": result.get("iterations", 0),
        "tokens": sum(approx_tokens(s) for s in ctx),
        "entities": len(result.get("entities", [])),
        "latency_ms": 0.0,
    }


def run_baseline(svc, question: str, *, top_k: int = 5) -> LoopState:
    """Single-shot search — the control arm."""
    state = LoopState(iterations=1, queries_issued=1)
    t0 = time.perf_counter()
    res = svc.search(question, top_k=top_k)
    state.low_confidence = bool(res.get("low_confidence"))
    entries = res.get("entries", [])
    state.top_score = float(entries[0]["score"]) if entries else 0.0
    for e in entries:
        txt = e.get("text", "")
        if txt:
            state.texts.append(txt)
    state.latency_ms = (time.perf_counter() - t0) * 1000
    return state


def gold_recovered(state: LoopState, gold: str) -> bool:
    return any(value_present(s, gold) for s in assembled_context(state))


def tokens_read(state: LoopState) -> int:
    return sum(approx_tokens(s) for s in assembled_context(state))


def would_gate(state: LoopState, thin: float = 0.5) -> bool:
    """Whether a shipped gate WOULD enter the loop (reported, not enforced)."""
    return bool(state.low_confidence) or state.top_score < thin


def _means(recs: list[dict]) -> dict:
    n = len(recs)
    if n == 0:
        return {"n": 0, "recall": 0.0, "mean_iterations": 0.0,
                "mean_tokens": 0.0, "mean_entities": 0.0, "mean_latency_ms": 0.0}
    return {
        "n": n,
        "recall": round(sum(1 for r in recs if r["recovered"]) / n, 3),
        "mean_iterations": round(sum(r["iterations"] for r in recs) / n, 2),
        "mean_tokens": round(sum(r["tokens"] for r in recs) / n, 1),
        "mean_entities": round(sum(r.get("entities", 0) for r in recs) / n, 2),
        "mean_latency_ms": round(sum(r["latency_ms"] for r in recs) / n, 1),
    }


def aggregate(records: list[dict]) -> dict:
    by_hops = {}
    for h in sorted({r["hops"] for r in records}):
        by_hops[h] = _means([r for r in records if r["hops"] == h])
    return {"overall": _means(records), "by_hops": by_hops}


# ---------------------------------------------------------------------------
# Real-service layer: seed, run, report, main
# ---------------------------------------------------------------------------
import json  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def seed_bench(svc) -> None:
    """Ingest snippets + distractors as memories AND seed the graph edges."""
    for rec in CORPUS:
        svc.store(rec["snippet"], source="bench")
    for d in DISTRACTORS:
        svc.store(d, source="bench")
    for rec in CORPUS:
        for (src, rel, dst) in rec["edges"]:
            out = svc.graph_relate(src, rel, dst, origin="bench")
            if out.get("error"):
                raise RuntimeError(f"seed edge failed: {src} {rel} {dst}: {out}")


def run_all(svc, *, top_k: int = 5, hop_cap: int = 3) -> dict:
    base_recs, nogate_recs, gate_recs = [], [], []
    gate_fire = 0
    cfg = svc.config.memory.recall
    _saved = (cfg.hub_floor, cfg.hub_gate)  # run_all mutates the live config; restore below
    cfg.hub_floor = 3          # shared-config (deg 6) is a hub; chain heads are not
    for q in QUESTIONS:
        question, gold, hops = q["question"], q["gold"], q["hops"]
        base = run_baseline(svc, question, top_k=top_k)
        if would_gate(base):
            gate_fire += 1
        base_recs.append({"hops": hops, "recovered": gold_recovered(base, gold),
                          "iterations": base.iterations, "tokens": tokens_read(base),
                          "entities": 0, "latency_ms": base.latency_ms})
        cfg.hub_gate = False
        nogate_recs.append(recall_record(
            svc.recall(question, hops=hop_cap, top_k=top_k), gold, hops))
        cfg.hub_gate = True
        gate_recs.append(recall_record(
            svc.recall(question, hops=hop_cap, top_k=top_k), gold, hops))
    base_agg = aggregate(base_recs)
    nogate_agg = aggregate(nogate_recs)
    gate_agg = aggregate(gate_recs)
    cfg.hub_floor, cfg.hub_gate = _saved  # restore the live config we mutated
    return {
        "baseline": base_agg, "recall_nogate": nogate_agg, "recall_gate": gate_agg,
        "gate_would_fire": gate_fire, "questions": len(QUESTIONS),
        "recall_delta": round(
            gate_agg["overall"]["recall"] - nogate_agg["overall"]["recall"], 3),
        "tokens_saved": round(
            nogate_agg["overall"]["mean_tokens"] - gate_agg["overall"]["mean_tokens"], 1),
        "entities_saved": round(
            nogate_agg["overall"]["mean_entities"] - gate_agg["overall"]["mean_entities"], 2),
    }


def report(results: dict) -> None:
    arms = [("baseline", "single-shot search"),
            ("recall_nogate", "recall, gate off"),
            ("recall_gate", "recall, gate on")]
    hdr = f"{'arm':<24}{'recall':>8}{'iters':>7}{'tok/q':>8}{'ents/q':>8}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for key, label in arms:
        o = results[key]["overall"]
        print(f"{label:<24}{o['recall']:>8}{o['mean_iterations']:>7}"
              f"{o['mean_tokens']:>8}{o['mean_entities']:>8}")
    print("\nby hop-class (recall):")
    print(f"{'arm':<24}{'1-hop':>8}{'2-hop':>8}{'3-hop':>8}")
    for key, label in arms:
        bh = results[key]["by_hops"]
        cells = "".join(f"{bh.get(h, {}).get('recall', '—'):>8}" for h in (1, 2, 3))
        print(f"{label:<24}{cells}")
    print(f"\nrecall_delta (gate - nogate): {results['recall_delta']}  "
          f"(must be 0.0 — no regression)")
    print(f"tokens_saved:   {results['tokens_saved']}")
    print(f"entities_saved: {results['entities_saved']}")
    print(f"gate would fire on {results['gate_would_fire']}/{results['questions']} "
          f"questions")


def main() -> int:
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true", help="run all three arms")
    ap.add_argument("--show-corpus", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--hop-cap", type=int, default=3)
    args = ap.parse_args()

    if args.show_corpus:
        for q in QUESTIONS:
            print(f"  [{q['hops']}-hop] {q['question']}  -> {q['gold']}")
        return 0
    if args.run:
        import tempfile
        from ladder_sweep import build_service
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="plmemcot_",
                                         ignore_cleanup_errors=True) as td:
            svc = build_service(Path(td))
            seed_bench(svc)
            results = run_all(svc, top_k=args.top_k, hop_cap=args.hop_cap)
        (RESULTS_DIR / "memcot.json").write_text(json.dumps(results, indent=2))
        report(results)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
