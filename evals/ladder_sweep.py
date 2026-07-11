#!/usr/bin/env python
"""Extractor-ladder benchmark sweep (dev-only).

Finds the *minimum viable extraction model* — the lowest rung that beats
naive-RAG on knowledge-update + efficiency — by running the same corpus through
each rung's extractor and measuring:

  * gold_recoverable  — fraction of update-pairs whose CURRENT value is the one
                        the system returns (cortex for the SUT; top-k retrieval
                        for naive-RAG).
  * stale_leak        — fraction whose OLD (superseded) value is still returned.
  * tokens_per_query  — approx tokens the agent must read to answer (the cortex
                        fact block for the SUT; the top-k raw turns for naive).
  * search_latency_ms — mean answer latency.
  * extract_seconds   — wall-time to consolidate the whole corpus (off hot-path;
                        CPU rungs are slower — reported, not penalised).

Each rung writes ``evals/results/<rung>.json`` so the (slow, one-at-a-time CPU
and LAN) rungs can run incrementally overnight; ``--report`` aggregates them
into the per-rung table + minimum-viable verdict. ``--abstain <rung>`` runs the
abstention threshold sub-sweep.

Isolation + safety:
  * Runs against a DEDICATED ``pseudolife_memory_bench`` database (created if
    missing, truncated before each ingest). The live bank (``pseudolife_memory``)
    is NEVER touched.
  * Forces CPU (``CUDA_VISIBLE_DEVICES=-1``) so the 4090 is left alone.
  * Skips unreachable rungs (LAN endpoints down) and records status="unreachable".

See evals/README.md for endpoints, prerequisites, and how to read the verdict.
"""
from __future__ import annotations

# --- Force CPU + offline BEFORE importing torch/service (leave the 4090 alone,
#     fast embedder load). Must precede any pseudolife_memory import. ---
import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

WINDOW = 0   # --window: known-facts window size applied to every bench service

# ---------------------------------------------------------------------------
# Rung registry — every rung is the same OpenAI-compatible interface; only the
# base-URL + model change (endpoints resolved from the memory cortex).
# ---------------------------------------------------------------------------
RUNGS: dict[str, dict] = {
    # baseline: raw turns, no consolidation, answer via top-k vector search.
    "naive-rag": {"kind": "naive", "label": "naive-RAG (baseline)"},
    # deterministic floor: the no-LLM regex extractor (known weak).
    "floor": {"kind": "floor", "label": "deterministic floor"},
    # CPU sidecar rungs — both served on :8081 (operator swaps the GGUF between
    # runs; see README). Internal product service is profile 'extractor'; the
    # benchmark uses a host-published llama.cpp on 8081.
    "gemma-e2b": {"kind": "llm", "label": "Gemma 4 E2B (CPU sidecar)",
                  "base_url": "http://127.0.0.1:8081/v1", "model": "extractor"},
    "gemma-e4b": {"kind": "llm", "label": "Gemma 4 E4B (CPU sidecar)",
                  "base_url": "http://127.0.0.1:8081/v1", "model": "extractor"},
    # LAN rungs — resolved from cortex (homelab 5800X3D / 4090).
    # model ids match exactly what the servers report at /v1/models (incl. the
    # .gguf suffix) — llama.cpp ignores the field, LM Studio matches it.
    "qwen-a3b": {"kind": "llm", "label": "Qwen3.6-35B-A3B (homelab 5800X3D)",
                 "base_url": "http://192.168.0.130:1236/v1",
                 "model": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"},
    "qwen-27b": {"kind": "llm", "label": "Qwen3.6-27B (4090)",
                 "base_url": os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL",
                                            "http://127.0.0.1:1234/v1"),
                 "model": "Qwen3.6-27B-UD-Q4_K_XL.gguf"},
    # Sidecar-upgrade bake-off candidates (2026-07-04) — all served on :8081
    # like the gemma rungs (operator swaps the GGUF between runs).
    "qwen3.5-4b": {"kind": "llm", "label": "Qwen3.5-4B (candidate)",
                   "base_url": "http://127.0.0.1:8081/v1", "model": "extractor"},
    "granite-h-tiny": {"kind": "llm",
                       "label": "Granite 4.0-H-Tiny 7B-A1B (candidate)",
                       "base_url": "http://127.0.0.1:8081/v1", "model": "extractor"},
    "lfm2-8b-a1b": {"kind": "llm", "label": "LFM2-8B-A1B (candidate)",
                    "base_url": "http://127.0.0.1:8081/v1", "model": "extractor"},
    "ornith-9b": {"kind": "llm", "label": "Ornith-1.0-9B (candidate)",
                  "base_url": "http://127.0.0.1:8081/v1", "model": "extractor"},
    # served by evals/dg_shim.py (no llama-server support for diffusion archs)
    "diffusiongemma": {"kind": "llm",
                       "label": "DiffusionGemma 26B-A4B (candidate)",
                       "base_url": "http://127.0.0.1:8082/v1",
                       "model": "extractor"},
    "gemma4-26b-qat": {"kind": "llm",
                       "label": "Gemma 4 26B-A4B QAT-Q4_0 (candidate)",
                       "base_url": "http://127.0.0.1:8081/v1",
                       "model": "extractor"},
    "gemma-e4b-qat": {"kind": "llm",
                      "label": "Gemma 4 E4B QAT UD-Q4_K_XL (sidecar-swap candidate)",
                      "base_url": "http://127.0.0.1:8081/v1",
                      "model": "extractor"},
    "e4b-ft": {"kind": "llm",
               "label": "E4B QLoRA extractor fine-tune Q4_K_M (distill SFT 2026-07-06)",
               "base_url": "http://127.0.0.1:8081/v1",
               "model": "extractor"},
    # Cloud ceiling probe (2026-07-11, user-requested): Claude via the Max-plan
    # CLI, served by evals/sonnet_shim.py. Deliberately NOT in LADDER_ORDER —
    # the default sweep stays sovereign-only; run with --rung sonnet-5.
    "sonnet-5": {"kind": "llm",
                 "label": "Claude Sonnet 5 (Max-plan CLI shim, ceiling probe)",
                 "base_url": "http://127.0.0.1:8082/v1",
                 "model": "extractor"},
}
LADDER_ORDER = ["naive-rag", "floor", "gemma-e2b", "gemma-e4b",
                "qwen3.5-4b", "granite-h-tiny", "lfm2-8b-a1b", "ornith-9b",
                "diffusiongemma", "gemma4-26b-qat", "gemma-e4b-qat", "e4b-ft",
                "qwen-a3b", "qwen-27b"]

# ---------------------------------------------------------------------------
# Corpus — realistically-phrased "ingested conversation". Each update-pair
# states a value, then updates it later with natural phrasing that defeats the
# deterministic floor (no copula / confounding prefixes). gold = CURRENT value.
# ---------------------------------------------------------------------------
PAIRS = [
    {"entity": "checkout-service", "attribute": "default port",
     "stale": "8080", "gold": "9090",
     "initial": "For reference, the checkout-service listens on its default port 8080.",
     "update": "Update: the checkout-service default port is now 9090 after the migration.",
     "question": "What is the checkout-service default port?"},
    {"entity": "payments-db", "attribute": "host",
     "stale": "db-prod-1", "gold": "db-prod-2",
     "initial": "The payments database currently runs on host db-prod-1.",
     "update": "We migrated the payments database to db-prod-2 over the weekend.",
     "question": "Which host serves the payments database?"},
    {"entity": "api-gateway", "attribute": "timeout",
     "stale": "30s", "gold": "60s",
     "initial": "The api-gateway request timeout is set to 30s.",
     "update": "Bumped the api-gateway timeout up to 60s yesterday.",
     "question": "What is the api-gateway request timeout?"},
    {"entity": "auth-service", "attribute": "version",
     "stale": "1.4.2", "gold": "2.0.0",
     "initial": "auth-service is pinned at version 1.4.2.",
     "update": "Rolled auth-service forward to version 2.0.0 this morning.",
     "question": "What version is auth-service?"},
    {"entity": "cache-layer", "attribute": "engine",
     "stale": "redis", "gold": "valkey",
     "initial": "Our cache-layer uses the redis engine.",
     "update": "We've since swapped the cache-layer engine over to valkey.",
     "question": "What engine does the cache-layer use?"},
    {"entity": "build-runner", "attribute": "os",
     "stale": "ubuntu-20.04", "gold": "ubuntu-24.04",
     "initial": "The build-runner image is based on ubuntu-20.04.",
     "update": "Upgraded the build-runner to ubuntu-24.04 in the latest pipeline.",
     "question": "What OS does the build-runner use?"},
    {"entity": "metrics-store", "attribute": "retention",
     "stale": "7 days", "gold": "30 days",
     "initial": "metrics-store retention is 7 days.",
     "update": "We extended metrics-store retention to 30 days.",
     "question": "What is the metrics-store retention?"},
    {"entity": "search-index", "attribute": "shards",
     "stale": "4", "gold": "12",
     "initial": "The search-index runs with 4 shards.",
     "update": "Scaled the search-index out to 12 shards last sprint.",
     "question": "How many shards does the search-index run?"},
    {"entity": "cdn-provider", "attribute": "vendor",
     "stale": "fastly", "gold": "cloudflare",
     "initial": "Our cdn-provider vendor is fastly.",
     "update": "Cut the cdn-provider over from fastly to cloudflare.",
     "question": "Who is the cdn-provider vendor?"},
    {"entity": "job-queue", "attribute": "broker",
     "stale": "rabbitmq", "gold": "kafka",
     "initial": "The job-queue broker is rabbitmq.",
     "update": "Replaced the job-queue broker with kafka recently.",
     "question": "What is the job-queue broker?"},
]

DISTRACTORS = [
    "Our internal wiki lives at wiki.corp.local.",
    "The frontend bundle size is about 2MB after tree-shaking.",
    "Daily standups are at 9:30am in the main channel.",
    "db-prod-3 is provisioned but reserved for future use.",
    "The staging cluster autoscaler kicks in above 70% CPU.",
    "Release notes are published to the changelog every Friday.",
]

# Unanswerable probes — never stated (or entity stated but attribute absent).
# For the abstention sub-sweep: these SHOULD return low_confidence=True.
UNANSWERABLE = [
    ("billing-service", "port", "What port does the billing-service use?"),
    ("notification-service", "host", "Where is the notification-service hosted?"),
    ("ml-pipeline", "framework", "What framework does the ml-pipeline use?"),
    ("user-db", "engine", "What engine backs the user-db?"),
    ("cdn-provider", "price", "What is the monthly cdn-provider price?"),
    ("payments-db", "password", "What is the payments-db password?"),
]

TOP_K = 5

# Same-entity/different-attribute and different-entity/same-attribute pairs that
# must remain DISTINCT slots after consolidation. A resolver false-merge collapses
# one of these onto the other -> measured as false_merge in the supersession sweep.
#
# IMPORTANT: these entities/attributes must NOT collide with PAIRS or UNANSWERABLE —
# otherwise a no-merge text re-asserts a pair's value and contaminates the
# supersession measurement (and an UNANSWERABLE probe would stop being unanswerable).
NO_MERGE = [
    # same entity, different attribute -> must remain two slots
    {"a": ("invoice-service", "port"), "b": ("invoice-service", "region"),
     "a_text": "The invoice-service port is 7000.",
     "b_text": "The invoice-service region is us-west-2."},
    # different entity, same attribute (adversarial: 'ledger-db engine' vs
    # 'ledger-cache engine' are lexically close) -> must remain two slots
    {"a": ("ledger-db", "engine"), "b": ("ledger-cache", "engine"),
     "a_text": "The ledger-db uses the postgres engine.",
     "b_text": "The ledger-cache uses the memcached engine."},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def approx_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~chars/4, the common GPT proxy)."""
    return max(1, len(text or "") // 4)


def value_present(text: str, value: str) -> bool:
    """Word-boundary match so short values ('4') don't match '24'."""
    if not text or not value:
        return False
    return re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w.])",
                     text, re.IGNORECASE) is not None


def probe(base_url: str, timeout: float = 4.0) -> bool:
    """Reachability check — GET <base_url>/models (llama.cpp serves it)."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def bench_url() -> str:
    base = os.environ.get(
        "PSEUDOLIFE_BENCH_ADMIN_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres",
    )
    return base.rsplit("/", 1)[0] + "/pseudolife_memory_bench"


_ALL_TABLES = (
    "edges", "entity_aliases", "relations", "facts", "world_facts", "entries",
    "episodes", "entities", "meta",
)


def reset_bench() -> str:
    """Ensure the dedicated bench DB exists and is empty. Returns its URL.

    NEVER touches the live ``pseudolife_memory`` DB — this is its own database.
    """
    import psycopg

    admin = os.environ.get(
        "PSEUDOLIFE_BENCH_ADMIN_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres",
    )
    admin = admin.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            ("pseudolife_memory_bench",),
        ).fetchone()
        if row is None:
            conn.execute('CREATE DATABASE "pseudolife_memory_bench"')

    url = bench_url()
    from pseudolife_memory.storage.schema import ensure_schema
    with psycopg.connect(url, connect_timeout=5) as conn:
        conn.execute("SET search_path TO public")
        conn.commit()
        with conn.cursor() as cur:  # reap any leaked backends holding locks
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        conn.commit()
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE " + ", ".join(_ALL_TABLES)
                        + " RESTART IDENTITY CASCADE")
        conn.commit()
        ensure_schema(conn)
    return url


def build_service(tmp_dir: Path):
    from pseudolife_memory.service import MemoryService
    url = reset_bench()
    svc = MemoryService(data_dir=str(tmp_dir), database_url=url)
    svc.config.embedding.device = "cpu"
    # Isolate EXTRACTION quality from the cortex contender-parking policy: with
    # protect_provenance on (the product default), two same-tier agent claims for
    # one slot park the newer as a contender instead of superseding, so the
    # benchmark would conflate extraction with that policy. Pure newer-wins here
    # measures "did the extractor surface the CURRENT value" directly. (The
    # incremental-dream parking behaviour is noted as a separate product finding.)
    svc.config.memory.cortex.protect_provenance = False
    # Single-writer: measure the DREAM extractor alone, not the regex auto-promote
    # floor (which fragments compound slots). Explicit so the bench is independent
    # of the shipped default (which is now also False).
    svc.config.memory.cortex.auto_promote = False
    svc.config.memory.dream.known_facts_window = WINDOW
    return svc


def ingest(svc) -> None:
    """Store all turns (initials, then distractors, then updates) in order."""
    for p in PAIRS:
        svc.store(p["initial"], source="bench")
    for d in DISTRACTORS:
        svc.store(d, source="bench")
    for p in PAIRS:
        svc.store(p["update"], source="bench")


def make_extractor(rung: dict):
    if rung["kind"] == "floor":
        from pseudolife_memory.memory.dream import RegexExtractor
        return RegexExtractor()
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    # Generous budget + timeout: the CPU rungs are reasoning models (thinking
    # trace before the JSON) and run on CPU, so allow room and time. This is the
    # extractor's best-case quality; the shipped default (1024) is documented as
    # the floor for reasoning models.
    return OpenAICompatExtractor(
        rung["base_url"], rung["model"],
        max_tokens=4096, timeout_seconds=600.0,
    )


def consolidate(svc, extractor) -> tuple[float, dict]:
    t0 = time.perf_counter()
    tally = {"pulled": 0, "claims": 0, "inserted": 0, "superseded": 0}
    while True:
        res = svc.dream_run(extractor, limit=100)
        for k in tally:
            tally[k] += int(res.get(k, 0))
        if not res.get("pulled"):
            break
    return time.perf_counter() - t0, tally


def measure_cortex(svc) -> dict:
    """SUT metrics, measured through the ACTUAL recall path: ask each question
    via ``cortex_search`` and score what comes back. Symmetric with
    ``measure_naive`` (which scores ``search`` results), so the comparison is
    apples-to-apples — and, crucially, robust to *how the extractor named the
    entity*. A weak extractor that splits an update onto a sibling slot instead
    of superseding (e.g. ``payments-db`` vs ``payments_database_host``) is not
    rewarded: both the gold and the un-superseded stale value surface here, so
    stale_leak rises exactly as it should. (Exact entity-dump matching, the
    earlier approach, instead conflated extraction quality with whether the
    model reproduced the corpus's canonical entity string verbatim.)"""
    gold = stale = 0
    tok_sum = 0.0
    lat = []
    for p in PAIRS:
        t0 = time.perf_counter()
        res = svc.cortex_search(p["question"], top_k=5, min_score=0.3)
        lat.append((time.perf_counter() - t0) * 1000)
        vals = [e.get("value", "") for e in res.get("entries", [])]
        if any(value_present(v, p["gold"]) for v in vals):
            gold += 1
        if any(value_present(v, p["stale"]) for v in vals):
            stale += 1
        # tokens the agent reads to answer = the returned cortex fact values.
        tok_sum += sum(approx_tokens(v) for v in vals)
    n = len(PAIRS)
    return {
        "gold_recoverable": round(gold / n, 3),
        "stale_leak": round(stale / n, 3),
        "tokens_per_query": round(tok_sum / n, 1),
        "search_latency_ms": round(sum(lat) / len(lat), 1),
    }


def measure_naive(svc) -> dict:
    """Baseline metrics: answer via top-k vector search over the raw turns."""
    gold = stale = 0
    tok_sum = 0.0
    lat = []
    for p in PAIRS:
        t0 = time.perf_counter()
        res = svc.search(p["question"], top_k=TOP_K)
        lat.append((time.perf_counter() - t0) * 1000)
        texts = [e.get("text", "") for e in res.get("entries", [])]
        if any(value_present(t, p["gold"]) for t in texts):
            gold += 1
        if any(value_present(t, p["stale"]) for t in texts):
            stale += 1
        tok_sum += sum(approx_tokens(t) for t in texts)
    n = len(PAIRS)
    return {
        "gold_recoverable": round(gold / n, 3),
        "stale_leak": round(stale / n, 3),
        "tokens_per_query": round(tok_sum / n, 1),
        "search_latency_ms": round(sum(lat) / len(lat), 1),
    }


# ---------------------------------------------------------------------------
# Per-rung driver
# ---------------------------------------------------------------------------
def run_rung(name: str) -> dict:
    rung = RUNGS[name]
    import tempfile
    result = {"rung": name, "label": rung["label"], "kind": rung["kind"]}

    if rung["kind"] == "llm" and not probe(rung["base_url"]):
        result["status"] = "unreachable"
        result["base_url"] = rung["base_url"]
        return result

    # ignore_cleanup_errors: ChromaDB's PersistentClient keeps the chroma.sqlite3
    # handle open for the life of the process, so on Windows the temp-dir teardown
    # can't unlink it. The leaked dir is tiny and lives in %TEMP%; the alternative
    # (poking chromadb internals to force-close) is far more fragile.
    with tempfile.TemporaryDirectory(prefix=f"plbench_{name}_",
                                     ignore_cleanup_errors=True) as td:
        svc = build_service(Path(td))
        ingest(svc)
        if rung["kind"] == "naive":
            result.update(measure_naive(svc))
            result["extract_seconds"] = 0.0
            result["status"] = "ok"
            return result
        extractor = make_extractor(rung)
        secs, tally = consolidate(svc, extractor)
        result["extract_seconds"] = round(secs, 1)
        result["consolidation"] = tally
        result.update(measure_cortex(svc))
        result["status"] = "ok"
    return result


# ---------------------------------------------------------------------------
# Abstention threshold sub-sweep (on a chosen, consolidated rung)
# ---------------------------------------------------------------------------
# Floors bracket the embedder's ACTUAL score distribution for this corpus
# (answerable max-scores 0.75–0.98; unanswerable 0.38–0.78). Floors below ~0.5
# never fire — everything scores above them. The interesting band is 0.65–0.80.
def run_abstain(name: str, floors=(0.0, 0.5, 0.65, 0.70, 0.75, 0.80),
                guards=(0.3, 0.5, 0.65, 0.75, 0.85)) -> dict:
    rung = RUNGS[name]
    if rung["kind"] == "llm" and not probe(rung["base_url"]):
        return {"rung": name, "status": "unreachable"}
    import tempfile
    with tempfile.TemporaryDirectory(prefix=f"plabstain_{name}_",
                                     ignore_cleanup_errors=True) as td:
        svc = build_service(Path(td))
        ingest(svc)
        if rung["kind"] != "naive":
            consolidate(svc, make_extractor(rung))

        curve = []
        for g in guards:
            for f in floors:
                svc.config.memory.search_confidence_floor = f
                # Abstention recall on the unanswerable set (want low_confidence).
                abst = 0
                for _ent, _attr, q in UNANSWERABLE:
                    r = svc.search(q, top_k=TOP_K)
                    has_cortex = bool(
                        svc.cortex_search(q, top_k=5, min_score=g).get("entries"))
                    if r.get("low_confidence") and not has_cortex:
                        abst += 1
                # False-abstention on the answerable set (want to NOT abstain).
                wrong = 0
                for p in PAIRS:
                    r = svc.search(p["question"], top_k=TOP_K)
                    has_cortex = bool(
                        svc.cortex_search(p["question"], top_k=5,
                                          min_score=g).get("entries"))
                    if r.get("low_confidence") and not has_cortex:
                        wrong += 1
                curve.append({
                    "guard": g, "floor": f,
                    "abstain_recall_unanswerable": round(abst / len(UNANSWERABLE), 3),
                    "false_abstain_answerable": round(wrong / len(PAIRS), 3),
                })
    return {"rung": name, "status": "ok", "curve": curve}


def run_supersede(name: str, thresholds=(0.0, 0.80, 0.85, 0.90, 0.95)) -> dict:
    """Feature-A calibration: sweep dream_slot_match_threshold on a paraphrasing
    rung. Reports superseded / stale_leak (win) vs false_merge (cost)."""
    rung = RUNGS[name]
    if rung["kind"] == "llm" and not probe(rung["base_url"]):
        return {"rung": name, "status": "unreachable"}
    import tempfile
    curve = []
    for thr in thresholds:
        with tempfile.TemporaryDirectory(prefix=f"plsup_{name}_",
                                         ignore_cleanup_errors=True) as td:
            svc = build_service(Path(td))
            svc.config.memory.cortex.dream_slot_match_threshold = thr
            ingest(svc)
            for pair in NO_MERGE:                      # add the no-merge slots
                svc.store(pair["a_text"], source="bench")
                svc.store(pair["b_text"], source="bench")
            _, tally = consolidate(svc, make_extractor(rung))
            m = measure_cortex(svc)
            false_merge = 0
            for pair in NO_MERGE:
                a = svc.cortex_lookup(*pair["a"])
                b = svc.cortex_lookup(*pair["b"])
                if a is None or b is None:             # one slot vanished -> merged
                    false_merge += 1
            curve.append({
                "threshold": thr,
                "superseded": tally.get("superseded", 0),
                "stale_leak": m["stale_leak"],
                "gold_recoverable": m["gold_recoverable"],
                "false_merge": false_merge,
            })
    return {"rung": name, "status": "ok", "curve": curve}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _load_results() -> dict[str, dict]:
    out = {}
    if RESULTS_DIR.exists():
        for fp in RESULTS_DIR.glob("*.json"):
            try:
                out[fp.stem] = json.loads(fp.read_text())
            except Exception:
                pass
    return out


def report() -> None:
    res = _load_results()
    naive = res.get("naive-rag")
    cols = ("gold_recoverable", "stale_leak", "tokens_per_query",
            "search_latency_ms", "extract_seconds")
    hdr = f"{'rung':<28}{'gold↑':>8}{'stale↓':>8}{'tok/q↓':>9}{'lat ms':>8}{'extract s':>11}  status"
    print("\n" + hdr)
    print("-" * len(hdr))
    for name in LADDER_ORDER:
        r = res.get(name)
        if not r:
            print(f"{name:<28}{'—':>8}{'—':>8}{'—':>9}{'—':>8}{'—':>11}  (not run)")
            continue
        if r.get("status") != "ok":
            print(f"{r.get('label', name):<28}{'—':>8}{'—':>8}{'—':>9}{'—':>8}"
                  f"{'—':>11}  {r.get('status')}")
            continue
        print(f"{r.get('label', name):<28}"
              f"{r.get('gold_recoverable', 0):>8}"
              f"{r.get('stale_leak', 0):>8}"
              f"{r.get('tokens_per_query', 0):>9}"
              f"{r.get('search_latency_ms', 0):>8}"
              f"{r.get('extract_seconds', 0):>11}  ok")

    if not naive or naive.get("status") != "ok":
        print("\n[verdict] naive-RAG baseline not present — run `--rung naive-rag` "
              "first to compute the gate.")
        return
    # Gate (re-scoped to efficiency/sovereignty): a rung clears if it beats naive
    # on staleness AND gold, at <=60% of naive tokens/query.
    tok_gate = 0.6 * naive["tokens_per_query"]
    print(f"\n[gate] vs naive-RAG: stale_leak < {naive['stale_leak']}, "
          f"gold_recoverable > {naive['gold_recoverable']}, "
          f"tokens/query <= {tok_gate:.1f} (60% of naive {naive['tokens_per_query']})")
    winner = None
    for name in LADDER_ORDER:
        if name in ("naive-rag",):
            continue
        r = res.get(name)
        if not r or r.get("status") != "ok":
            continue
        clears = (r["stale_leak"] < naive["stale_leak"]
                  and r["gold_recoverable"] > naive["gold_recoverable"]
                  and r["tokens_per_query"] <= tok_gate)
        flag = "✓ clears" if clears else "· below gate"
        print(f"   {r.get('label', name):<30} {flag}")
        if clears and winner is None:
            winner = name
    if winner:
        print(f"\n[minimum viable] {RUNGS[winner]['label']}  ({winner})")
    else:
        print("\n[minimum viable] none of the run rungs cleared the gate yet.")

    ab = res.get("abstain")
    if ab and ab.get("status") == "ok":
        print(f"\n[abstention sub-sweep] rung={ab['rung']}")
        print(f"   {'guard':>6}{'floor':>6}{'abstain_recall(unans)':>24}{'false_abstain(ans)':>22}")
        for row in ab["curve"]:
            print(f"   {row['guard']:>6}{row['floor']:>6}"
                  f"{row['abstain_recall_unanswerable']:>24}"
                  f"{row['false_abstain_answerable']:>22}")

    sup = res.get("supersede")
    if sup and sup.get("status") == "ok":
        print(f"\n[supersession sub-sweep] rung={sup['rung']}")
        print(f"   {'threshold':>10}{'superseded':>12}{'stale_leak':>12}"
              f"{'gold_recov':>12}{'false_merge':>12}")
        for row in sup["curve"]:
            print(f"   {row['threshold']:>10}{row['superseded']:>12}"
                  f"{row['stale_leak']:>12}{row['gold_recoverable']:>12}"
                  f"{row['false_merge']:>12}")


# ---------------------------------------------------------------------------
def main() -> int:
    # The report table uses Unicode glyphs (↑ ↓ ✓ —); the default Windows
    # console codec (cp1252) can't encode them. Force UTF-8 so the tool prints
    # cleanly regardless of platform / redirection.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rung", choices=list(RUNGS), help="run one rung")
    ap.add_argument("--abstain", choices=list(RUNGS),
                    help="run the abstention threshold sub-sweep on this rung")
    ap.add_argument("--supersede", choices=list(RUNGS),
                    help="dream slot-match threshold sub-sweep")
    ap.add_argument("--report", action="store_true",
                    help="aggregate results/*.json into the table + verdict")
    ap.add_argument("--list", action="store_true", help="list rungs + endpoints")
    ap.add_argument("--window", type=int, default=0,
                    help="known-facts window size for every service built "
                         "by this run (0 = off; spec 2026-07-10)")
    args = ap.parse_args()
    global WINDOW
    WINDOW = args.window

    if args.list:
        for n in LADDER_ORDER:
            r = RUNGS[n]
            ep = r.get("base_url", "—")
            print(f"  {n:<12} {r['label']:<34} {ep}")
        return 0
    if args.report:
        report()
        return 0

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.abstain:
        out = run_abstain(args.abstain)
        (RESULTS_DIR / "abstain.json").write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2))
        return 0
    if args.supersede:
        out = run_supersede(args.supersede)
        (RESULTS_DIR / "supersede.json").write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2))
        return 0
    if args.rung:
        out = run_rung(args.rung)
        (RESULTS_DIR / f"{args.rung}.json").write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
