"""Retrieval-quality golden set (P2, 2026-07-02 review).

The first thing on master capable of catching a *ranking* regression: a
fixed corpus of realistic memory-bank entries with one paraphrase query
per entry, asserting recall@5 / MRR@5 floors on the dense path and a
top-3 floor on the BM25-fused identifier queries. Unit tests elsewhere
pin mechanisms (fusion math, recency, filters); this pins the outcome.

Floors are set with slack below the measured baseline so embedder or
scoring *drift* passes but a real regression (fusion weight typo, filter
applied to the wrong pool, similarity math error) trips. If you improve
ranking and a floor becomes slack-free, raise it — never delete it.

Runs offline (HF_HUB_OFFLINE=1) on the cached MiniLM; no Postgres needed
(file-mode CMS).
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

# Floors: measured baseline minus slack. Measured 2026-07-02 with
# all-MiniLM-L6-v2: recall@5=1.000, MRR=0.990, BM25 top3=1.000 (all rank-1).
DENSE_RECALL_AT_5_FLOOR = 0.92   # tolerates 4 misses out of 50
DENSE_MRR_FLOOR = 0.85
BM25_TOP3_FLOOR = 0.85           # tolerates 1 of 8 slipping below top-3


@pytest.fixture(scope="module")
def golden():
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import AppConfig

    cfg = AppConfig()
    cfg.memory.surprise_threshold = 0.0     # store the whole corpus
    emb = EmbeddingPipeline(cfg.embedding)
    cms = ContinuumMemorySystem(cfg.memory)
    for text, _ in GOLDEN:
        stored, _s = cms.store(text, emb.encode_single(text), source="golden")
        assert stored, f"corpus entry rejected: {text[:50]}"
    return cms, emb


def _rank_of(cms, emb, query: str, target: str, *, bm25: bool | None = None,
             k: int = 5) -> int | None:
    res = cms.retrieve(emb.encode_single(query), top_k=k,
                       query_text=query, bm25=bm25)
    for i, e in enumerate(res.entries):
        if e.text == target:
            return i + 1
    return None


def test_dense_recall_and_mrr_floors(golden):
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
