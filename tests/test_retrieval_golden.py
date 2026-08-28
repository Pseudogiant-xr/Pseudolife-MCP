"""Retrieval-quality golden set (P2, 2026-07-02 review).

The first thing on master capable of catching a *ranking* regression: a
fixed corpus of realistic memory-bank entries with one paraphrase query
per entry, asserting recall@5 / MRR@5 floors on the default retrieval
path and a top-3 floor on the BM25-fused identifier queries. Unit tests
elsewhere pin mechanisms (fusion math, recency, filters); this pins the
outcome.

The harness mirrors the production encode sides exactly: corpus entries
go through document-side ``encode`` (what ``MemoryService.store`` does
via ``encode_single``; the batch call is the same model with the same
result, just one forward pass for 50 texts instead of 50), and every
query goes through ``encode_query`` (what ``MemoryService.search`` does —
the rule is pinned call site by call site in
``tests/test_query_side_encoding.py``). This matters from schema v25 on:
the default embedder Qwen/Qwen3-Embedding-0.6B is instruction-asymmetric,
so ``encode_query`` prepends an instruct prefix and lands the probe in a
different region of the space than a document encode of the same string.

Floors are set with slack below the measured baseline so embedder or
scoring *drift* passes but a real regression (fusion weight typo, filter
applied to the wrong pool, similarity math error) trips. If you improve
ranking and a floor becomes slack-free, raise it — never delete it.

Runs offline (HF_HUB_OFFLINE=1) on the cached default embedder; no
Postgres needed (file-mode CMS).

Pre-2026-08-28 provenance, retired: the floors here used to be 0.92 /
0.85 / 0.85, measured 2026-07-02 against all-MiniLM-L6-v2 with queries
encoded *document-side* (``encode_single``) — a regime production has not
run since the v25 Qwen3 swap made the embedder asymmetric. Those numbers
described a retrieval path that no longer exists; see the floor block
below for what replaced them and why.
"""

from __future__ import annotations

import pytest

# (memory text, paraphrase query). One golden target per query.
GOLDEN: list[tuple[str, str]] = [
    ("The staging database password rotates every 30 days via vault-agent",
     "how often does the staging db password rotate?"),
    ("Deploys go through the update script which always backs up the bank first",
     "what does the deploy script do before rebuilding?"),
    ("The daemon listens on loopback port 8765 and refuses public binds without a token",
     "which interface does the daemon bind to?"),
    ("Weekly grocery order arrives on Thursday mornings from the co-op",
     "when do the groceries get delivered?"),
    ("The GPU workstation has a single RTX 4090 with 24GB of VRAM",
     "how much video memory does the workstation card have?"),
    ("Backups are rotated after seven days and verified non-empty before rotation",
     "how long are database backups kept?"),
    ("The kids' school run alternates: Monday and Wednesday are my days",
     "which days am I responsible for the school run?"),
    ("Prefer tabs-off, four-space indentation in all Python projects",
     "what indentation style do I prefer for Python?"),
    ("The espresso machine needs descaling roughly every three months",
     "how often should the coffee machine be descaled?"),
    ("Production alerts page the on-call phone only between 8am and 10pm",
     "what hours do production alerts page the phone?"),
    ("The cat is allergic to chicken-based kibble; use the salmon formula",
     "what food can the cat not eat?"),
    ("Docker volumes for the memory bank are external so compose down cannot delete them",
     "why are the bank volumes safe from compose down?"),
    ("The router reserves 192.168.1.50 for the NAS via static DHCP lease",
     "which IP is reserved for the NAS?"),
    ("Tax documents for the accountant are due by the end of January",
     "when do I need to send documents to the accountant?"),
    ("The dream extractor is a small Gemma model running CPU-only in a sidecar",
     "what model does the background extractor use?"),
    ("Meeting notes live in the shared drive under projects slash minutes",
     "where are meeting notes stored?"),
    ("The garden irrigation runs at 6am for fifteen minutes on even days",
     "what's the watering schedule for the garden?"),
    ("Car insurance renews in March; last year switching saved about 200 pounds",
     "when does the car insurance renew?"),
    ("The test suite must run with HuggingFace offline env vars or it flakes",
     "what env vars make the tests deterministic?"),
    ("Landlord contact for the flat is via the agency, never directly",
     "how should I contact the landlord?"),
    ("The backup power bank in the hall drawer holds two full phone charges",
     "how many charges does the spare power bank hold?"),
    ("Postgres runs pinned to the public schema because the role name collides",
     "why is the database pinned to the public schema?"),
    ("The gym membership includes the pool but only before noon on weekends",
     "when can I use the pool at the gym?"),
    ("Server room temperature alarm triggers above 28 degrees celsius",
     "at what temperature does the server room alarm go off?"),
    ("The wedding anniversary dinner reservation is always at the harbour bistro",
     "where do we go for anniversary dinners?"),
    ("Graph edges written by the dream carry origin agent and modest confidence",
     "who writes the low-confidence graph edges?"),
    ("The blue recycling bin goes out on alternate Tuesdays",
     "which day does the recycling get collected?"),
    ("Passport renewal takes about ten weeks so start three months early",
     "how long does passport renewal take?"),
    ("The stand-up desk motor sticks; nudge it down slightly before raising",
     "how do I fix the desk when the motor sticks?"),
    ("Streaming subscriptions are reviewed and pruned every quarter",
     "how often do I review streaming subscriptions?"),
    ("The neighbour waters our plants when we travel; we take their parcels in",
     "who looks after the plants when we're away?"),
    ("Session episodes are opened lazily by the daemon and reaped after idling",
     "how do session episodes get closed?"),
    ("The dentist insists on six-monthly checkups after the crown work",
     "how often are my dental checkups?"),
    ("Heating schedule drops to sixteen degrees overnight from eleven pm",
     "what temperature is the heating overnight?"),
    ("The old laptop's battery swells if left plugged in continuously",
     "why shouldn't the old laptop stay plugged in?"),
    ("Model weights are an atomic disposable cache with bak rotation",
     "how are the model weight files protected on save?"),
    ("The ferry to the island only takes card payments since last summer",
     "can I pay cash on the island ferry?"),
    ("Broadband contract ends in November; the loyalty price doubles after",
     "when does the broadband contract expire?"),
    ("The toddler naps best between one and three in the afternoon",
     "when is the toddler's nap window?"),
    ("Memory searches serialize through a single service lock in the daemon",
     "what serializes concurrent memory searches?"),
    ("The allotment plot fee is due each April to the parish council",
     "when is the allotment fee due and to whom?"),
    ("Long-haul flights: book aisle seats near the front for quick exits",
     "what seats do I prefer on long flights?"),
    # ── identifier-flavoured pairs (BM25 fusion leg) ──────────────────────
    ("process_chunk_v2 raises IndexError when the input batch is empty",
     "process_chunk_v2 IndexError"),
    ("The fix for the flaky login shipped in release v3.7.2 of the portal",
     "which release fixed the flaky login? v3.7.2?"),
    ("ERR_CONN_REFUSED from the proxy means the upstream container is down",
     "what does ERR_CONN_REFUSED from the proxy mean?"),
    ("run_daemon() reads PSEUDOLIFE_MCP_TOKEN before binding the socket",
     "where is PSEUDOLIFE_MCP_TOKEN read?"),
    ("The retention knob traces.retention_boost defaults to zero in the library",
     "what is the default of traces.retention_boost?"),
    ("hydrate_cms fills band entries from storage rows at startup",
     "what does hydrate_cms do?"),
    ("The cross-encoder ms-marco-MiniLM reranks the top twenty candidates",
     "which model reranks candidates? ms-marco-MiniLM"),
    ("git bisect pinned the crash to commit a3f9c21 touching the scheduler",
     "which commit did git bisect blame? a3f9c21"),
]

_IDENTIFIER_START = 42  # index where the BM25-leg pairs begin

# Floors: measured baseline minus slack. Re-measured 2026-08-28 against
# the current default embedder Qwen/Qwen3-Embedding-0.6B (torch backend,
# CPU), 50-entry corpus, queries encoded query-side via `encode_query` —
# i.e. the regime `MemoryService.search` actually runs. Measured, and
# identical across 3 runs (no sampling anywhere on this path, so the
# harness is bit-deterministic): recall@5=1.000, MRR=0.990,
# BM25 top3=1.000 (all 8 at rank 1).
#
# Why the dense floors sit this HIGH (they were 0.92/0.85 before today):
# `memory.bm25.enabled` has defaulted to True since 2026-07-25, so the
# `bm25=None` leg below is BM25-*fused*, not dense-only. The 2026-08-28
# degradation sweep (query embedding replaced with noise of increasing
# scale) shows lexical fusion alone floors that leg at recall@5=0.960 /
# MRR=0.915 — a totally dead dense path still scores that. Floors of
# 0.92/0.85 were therefore unfailable: they sat BELOW what BM25 delivers
# with no embedder at all. 0.98/0.96 sit above that lexical floor, so a
# collapsed dense contribution now trips them, while still leaving slack
# for drift (0.98 tolerates 1 miss in 50; 0.96 tolerates 3 targets
# slipping from rank 1 to rank 2).
DENSE_RECALL_AT_5_FLOOR = 0.98
DENSE_MRR_FLOOR = 0.96
# 8 identifier pairs, so the metric is quantised to 1/8: 0.87 is the
# tightest floor that still tolerates exactly one of the 8 slipping below
# top-3. Measured 1.000 (2026-08-28, as above).
BM25_TOP3_FLOOR = 0.87


@pytest.fixture(scope="module")
def golden():
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import AppConfig

    cfg = AppConfig()
    cfg.memory.surprise_threshold = 0.0     # store the whole corpus
    emb = EmbeddingPipeline(cfg.embedding)
    cms = ContinuumMemorySystem(cfg.memory)
    # Document-side, one batched forward pass for the whole corpus (same
    # embeddings as 50 encode_single calls, ~1.5s cheaper in setup). Entries
    # still go in one store() at a time, as production does.
    texts = [text for text, _ in GOLDEN]
    for text, vector in zip(texts, emb.encode(texts)):
        stored, _s = cms.store(text, vector, source="golden")
        assert stored, f"corpus entry rejected: {text[:50]}"
    return cms, emb


def _rank_of(cms, emb, query: str, target: str, *, bm25: bool | None = None,
             k: int = 5) -> int | None:
    # encode_query, not encode_single: the query side of the asymmetric
    # default embedder, matching MemoryService.search.
    res = cms.retrieve(emb.encode_query(query), top_k=k,
                       query_text=query, bm25=bm25)
    for i, e in enumerate(res.entries):
        if e.text == target:
            return i + 1
    return None


def test_dense_recall_and_mrr_floors(golden):
    """``bm25=None`` = the production default, which since 2026-07-25 means
    dense fused with BM25 — not dense-only. See the floor block for what
    that costs in sensitivity and why the floors are pitched above the
    lexical-only ceiling."""
    cms, emb = golden
    ranks = [_rank_of(cms, emb, q, t) for t, q in GOLDEN]
    hits = [r for r in ranks if r is not None]
    recall5 = len(hits) / len(GOLDEN)
    mrr = sum(1.0 / r for r in hits) / len(GOLDEN)
    misses = [GOLDEN[i][1] for i, r in enumerate(ranks) if r is None]
    assert recall5 >= DENSE_RECALL_AT_5_FLOOR and mrr >= DENSE_MRR_FLOOR, (
        f"dense ranking regressed: recall@5={recall5:.3f} "
        f"(floor {DENSE_RECALL_AT_5_FLOOR}), MRR={mrr:.3f} "
        f"(floor {DENSE_MRR_FLOOR}); missed queries: {misses}")


def test_bm25_fusion_identifier_queries_hit_top3(golden):
    cms, emb = golden
    pairs = GOLDEN[_IDENTIFIER_START:]
    ranks = [_rank_of(cms, emb, q, t, bm25=True) for t, q in pairs]
    top3 = sum(1 for r in ranks if r is not None and r <= 3) / len(pairs)
    detail = [(p[1], r) for p, r in zip(pairs, ranks)]
    assert top3 >= BM25_TOP3_FLOOR, (
        f"BM25-fused identifier ranking regressed: top3={top3:.3f} "
        f"(floor {BM25_TOP3_FLOOR}); ranks: {detail}")
