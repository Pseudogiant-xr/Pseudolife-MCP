# Embedding backbone v25 — Qwen3-Embedding-0.6B, asymmetric encode, vector(1024)

**Date:** 2026-07-28
**Status:** design, approved for planning (user: "spec it and proceed")
**Schema:** v25 (`vector(384)` -> `vector(1024)` on `entries`, `facts`,
`world_facts`, `lessons`)

## Why, and why this model

The bank's retrieval quality is bounded by the bi-encoder. Measured on our
own corpus (150 LongMemEval questions, 74,183 haystack turns, 299 gold;
PR #59's three committed artifacts):

| backbone | R@10 | vs shipped MiniLM |
|---|---|---|
| all-MiniLM-L6-v2 (shipped) | 0.572 | — |
| bge-base-en-v1.5 (PR #44's winner) | 0.742 | +54/−9 @5, p≈0 |
| **Qwen3-Embedding-0.6B** | **0.809** | **+81/−6 @5, p≈0** |

Qwen3 beat every arm at every k; the direct paired test vs bge-base is
significant at every k (+32/−12 @10, p=0.004). The quantization round
settled the rest of the field: Q8_0 is statistically identical to fp32;
the 4B at Q4_K_M is significantly WORSE than the 0.6B fp32 (embedding
geometry is precision-sensitive); 8B-Q4 and Nemotron-3-Embed-1B are washes
at 4–8x the footprint. Two leaderboard claims failed to transfer to our
corpus. There is no better candidate to wait for at this scale.

Deploy target fit (16 GB, no GPU sidecar): fp32 in-process 2.4 GB RAM,
**82 ms median / 101 ms p90 per query on CPU in the project venv as-is**
(sentence-transformers 5.5.1 / transformers 4.57.6 — no dependency bump).
The Q8_0 GGUF (0.6 GB via llama-server) produces interchangeable vectors
(cosine 0.9987) and remains a documented alternative, not the default —
the default install deliberately adds no serving process.

## The one design wrinkle: asymmetry

MiniLM is symmetric; Qwen3-Embedding is instruction-asymmetric — queries
carry a card-verbatim instruction prefix, passages do not:

```
Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:{query}
```

(no space after `Query:` — exact string, instruction-tuned embedders swing
on wording; the shootout recorded prefixes verbatim in its artifacts for
exactly this reason.)

**API change in `EmbeddingPipeline`:**

- `encode_query(text)` — applies `config.query_prefix`, then encodes.
- `encode_document(text)` / existing `encode`/`encode_single` — bare.
- `EmbeddingConfig` gains `query_prefix: str` (default: the Qwen3 string).
  Config-driven, no model-name sniffing: a config that swaps back to a
  symmetric model sets `query_prefix=""`. The pair (model_name,
  query_prefix) ships together in defaults.

**Call-site rule (the load-bearing judgment):** a text is QUERY-side iff it
is a retrieval probe that never gets stored; everything persisted is
DOCUMENT-side. Concretely:

- Query-side: `memory_search`/`cms.retrieve` query text, `cortex_search`
  query, world search query, lesson search query, recall seed queries,
  curation/duplicate-scan probe queries.
- Document-side: stored entries, fact claim text (`"{entity} {attribute}
  {value}"`), slot embeddings (`"{entity} {attribute}"` — compared
  slot-to-slot, i.e. doc-to-doc), world facts, lessons, entity name
  embeddings (name-to-name comparison).
- Anything compared TEXT-TO-TEXT among stored items (dedup, supersession
  cues, graph name similarity) is document-side on both ends. The prefix
  exists only for the query->passage direction.

`service.py` has ~23 encode call sites; the implementer enumerates ALL of
them (grep, not memory) and classifies each in a table in the PR body.
Misclassification fails safe-ish (a bare query still retrieves, just below
peak) but the entire +23.7-point win rides on query-side prefixing, so the
classification table gets reviewed explicitly.

**Similarity-threshold caveat:** config thresholds calibrated on MiniLM
cosines (`alias_candidate_min_cosine` 0.65 with a documented 2026-07-07
MiniLM calibration, `curation_min_similarity` 0.80, surprise gate, recall
`min_score` 0.2/0.25 floors) are doc-to-doc comparisons whose absolute
cosine DISTRIBUTIONS shift under a new backbone. This spec does NOT
recalibrate them — they are behavior-neutral defaults to revisit with
live data — but the migration notes must name them, and the regression
gate (which exercises retrieval end-to-end including min_score floors) is
the empirical check that nothing breaks at the defaults.

## Schema v25

All four embedding columns become `vector(1024)`:
`entries.embedding` (NOT NULL), `facts.embedding`, `world_facts.embedding`,
`lessons.embedding`. 1024 < pgvector's 2000-dim HNSW cap; the
`entries` HNSW index (`hnsw-index-entries-embedding-idx`) rebuilds.

- `SCHEMA_SQL` (fresh installs): columns declared `vector(1024)` directly.
- `ensure_schema` (existing installs): **additive-only stays the rule.**
  A dimension change is NOT additive — `ensure_schema` does NOT attempt
  it. Instead it detects a dim mismatch (query `atttypmod` on
  `entries.embedding`) and, when the bank is v24-dimensioned, REFUSES to
  start the write path with a clear message naming the migration script.
  A daemon that silently half-migrated four tables at startup, or worse
  wrote 1024-d vectors into 384-d columns, is the failure mode this
  refusal exists to prevent.
- `SCHEMA_META_VERSION = 25`; the version bump is stamped by the
  MIGRATION script, not by `ensure_schema` (the one deliberate exception
  to daemon-stamped versions, mirroring how the backfill owned
  `entity_kinds` rows).

## The migration script — `ops/migrate_embeddings.py`

Human-gated, dry-run by default, backup-first, same discipline as
`apply_entity_kinds.py`:

1. Preflight: refuse without a fresh backup marker arg
   (`--backup-verified`); refuse if daemon is reachable on /health
   (it must be STOPPED — a live writer during re-embed corrupts);
   count rows per table and print the plan.
2. Per table, in one transaction each: drop dependent vector indexes,
   `ALTER COLUMN embedding TYPE vector(1024) USING NULL` (drop NOT NULL
   on `entries` first, restore after), re-embed every row **through the
   daemon's own `EmbeddingPipeline` and the SAME text-construction the
   write paths use** (imported, not re-implemented — single-copy rule),
   write vectors back, restore constraints, rebuild HNSW.
3. Stamp `SCHEMA_META_VERSION` 25 in meta. Print per-table counts.
4. Rollback story: restore the pre-migration pg_dump + the pre-migration
   image tag. No in-place downgrade path — say so plainly in the script
   docstring.

Scale check: live bank is ~2.4k facts + ~500 entries + world + lessons
≈ 4k texts; at CPU batch encode that is minutes, not hours.

## Docker / deploy surfaces

- `ops/Dockerfile` bakes the model at build (as it does MiniLM today).
  Qwen3-0.6B weights ~1.2 GB (bf16 safetensors) — the image grows by
  roughly that; the README/docs image-size claims are in the docs-currency
  drift list and MUST be updated (a stale "what's bundled/default
  (embedding weights)" claim is the exact class the release checklist
  names).
- ONNX: `_resolve_onnx_source` falls back to torch when no ONNX export
  exists for the repo — Qwen3-0.6B has no official ONNX in-repo, so the
  daemon runs the torch backend. The ONNX-preferred machinery is left
  intact for MiniLM compatibility and future exports.
- Embedding LRU cache: entries are now ~4 KB at dim 1024 (comment says
  ~1.5 KB at 384) — update the comment; cap unchanged.

## Eval / gate obligations

- `evals/regression_gate.ps1` runs BEFORE commit (this is *the*
  retrieval-affecting change). Judged on the reproducible server via
  `Start-Qwen` — never hand-rolled. Expected direction is improvement;
  exit 1 means regression and blocks the branch.
- Committed eval artifacts' stored embeddings stop being comparable across
  the swap (the harness docstring already warns this); artifacts are
  score-level, so no artifact rewrite is needed — but the benchmarks guide
  gets one sentence noting pre-/post-v25 banks embed differently.
- LIVE bank migration + deploy are USER-GATED morning steps, not overnight
  ones: merge PR -> backup -> stop daemon -> migrate -> deploy new image
  -> verify live (a `memory_search` end-to-end + the motivating-cluster
  probe). Overnight work ends at a green PR.

## Out of scope

- Threshold recalibration (named above; revisit with live data).
- Q8/llama-server serving mode as default (documented alternative only).
- Matryoshka dims other than native 1024.
- Re-running historical benchmarks on the new backbone.
