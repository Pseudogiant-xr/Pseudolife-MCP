# Embedding Backbone v25 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the bi-encoder to Qwen3-Embedding-0.6B (schema v25, `vector(1024)`, asymmetric query/document encoding) with the live-bank migration left as a user-gated morning step.

**Architecture:** `EmbeddingPipeline` grows `encode_query` (config-driven instruction prefix) beside the existing document-side encodes. Schema v25 redeclares four columns `vector(1024)`; `ensure_schema` stays additive-only and instead REFUSES to run against a 384-d bank, pointing at `ops/migrate_embeddings.py` — a human-gated, dry-run-default, backup-first offline re-embed that stamps the version itself.

**Tech Stack:** sentence-transformers 5.5.1 / transformers 4.57.6 (already pinned — no bumps), psycopg 3, pgvector HNSW, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-28-embedding-backbone-v25-design.md`. Its call-site rule is binding: query-side iff a retrieval probe that is never stored; everything persisted, and every stored-to-stored comparison (dedup, slots, entity names), is document-side.
- Query prefix is the card-verbatim string `"Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"` — no space after `Query:`. Config-driven (`EmbeddingConfig.query_prefix`), never model-name sniffing.
- `ensure_schema` remains additive-only. It never ALTERs a vector dim. On a dim mismatch it refuses loudly, naming the migration script.
- The migration script is dry-run by default, requires `--backup-verified`, refuses while the daemon answers /health, re-embeds through the daemon's own `EmbeddingPipeline` and the same text-construction the write paths use (imported, not re-implemented), and stamps schema 25 itself.
- The live bank is NOT migrated and the daemon is NOT deployed by this plan. Overnight ends at a green PR.
- Tests run with `HF_HUB_OFFLINE=1` — Qwen3-Embedding-0.6B is already in the local HF cache; do not add network needs to tests.
- TDD with a watched RED; full suite (bench PG up on 127.0.0.1:5433, 0 skips) before each commit; `evals/regression_gate.ps1` before the final commit (reproducible server via `Start-Qwen` from `evals/qwen_server.ps1` — never hand-rolled).
- No PII/machine identifiers in tracked files. CHANGELOG entry mentions **v25**.

---

### Task 1: Asymmetric encode API in `EmbeddingPipeline`

**Files:**
- Modify: `pseudolife_memory/memory/embedding.py`, `pseudolife_memory/utils/config.py` (EmbeddingConfig only)
- Test: `tests/test_embedding_asymmetry.py` (create)

**Interfaces:**
- Produces: `EmbeddingConfig.query_prefix: str` (default the Qwen3 string above), `EmbeddingConfig.max_seq_length: int = 512`; `EmbeddingPipeline.encode_query(text, normalize=True) -> torch.Tensor`; existing `encode`/`encode_single` unchanged in signature and remain document-side.
- The pipeline applies `max_seq_length` to the loaded model (Qwen3 defaults to 32k; unbounded inputs are a latency and RAM hazard, and 512 matches the shootout's measured configuration).
- `encode_query` must flow through the SAME LRU cache as document encodes with the prefixed text as key (prefix makes the keyspace disjoint — a query and a document of identical text must NOT share a cache slot).

Steps: write tests (prefix applied on query only; `query_prefix=""` makes `encode_query` byte-identical to `encode_single`; cache disjointness; max_seq_length applied), watch RED, implement, suite, commit.

### Task 2: Schema v25 + default model + test-fixture dims

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (four `vector(384)` -> `vector(1024)`; `SCHEMA_META_VERSION = 25`; dim-mismatch refusal in/next to `ensure_schema`), `pseudolife_memory/utils/config.py` (`model_name = "Qwen/Qwen3-Embedding-0.6B"`, cache comment ~4 KB/entry), version-pin tests (v13/v16/v22/v23/v24/temporal_stamp -> 25), every test fixture that hardcodes 384 (grep `zeros(384)`, `vector(384)`, `dim=384`, `384` in tests/ and fixtures — enumerate in the report).
- Test: `tests/test_schema_v25.py` (create): version pin; all four columns report `atttypmod` for 1024 in a live PG; the refusal fires against a table ALTERed back to `vector(384)` and its message names `ops/migrate_embeddings.py`; a fresh service round-trip stores and retrieves with dim 1024 end-to-end.

The refusal is load-bearing — RED-check it by disabling and confirming the test goes red. Expect the suite to slow (real 0.6B embeds in service tests); report before/after suite wall time.

### Task 3: Query-side threading in `service.py`

**Files:**
- Modify: `pseudolife_memory/service.py` (and any other module that encodes retrieval probes — grep `encode_single`/`.encode(` repo-wide)
- Test: `tests/test_query_side_encoding.py` (create)

Enumerate ALL ~23 encode call sites; classify each per the spec's rule into a table (goes verbatim into the task report AND the PR body); switch query-side sites to `encode_query`. Tests use a recording fake pipeline asserting: search/retrieve/recall/world/lesson query paths call `encode_query`; store/fact-write/slot/dedup paths never do. RED-check by reverting one search site and watching the specific assertion fail.

### Task 4: `ops/migrate_embeddings.py`

**Files:**
- Create: `ops/migrate_embeddings.py`
- Test: `tests/test_migrate_embeddings.py` (create, PG-backed)

Per the spec: preflight (backup flag, daemon-unreachable check, row counts, dry-run default prints the plan and writes nothing); per-table transaction (drop vector indexes, drop NOT NULL on `entries`, ALTER TYPE ... USING NULL, re-embed via imported pipeline + the write paths' own text construction, restore constraints, rebuild HNSW); stamps meta version 25 last. Tests build a miniature 384-d bank in the test DB (ALTER columns down first), run the migration with a stub pipeline, assert dims/values/index/meta and that dry-run mutates nothing.

### Task 5: Docs + CHANGELOG

CHANGELOG `[Unreleased]` entry mentioning **v25** with the decision numbers already pinned by `tests/test_eval_evidence.py` (do not restate unpinned numbers). README + `docs/guide/configuration.md` (bundled model, DSN row, version-history row for v25, image-size claim), `docs/guide/retrieval.md` / `memory-model.md` embedder mentions, `docs/guide/benchmarks.md` one-liner that pre-/post-v25 banks embed differently. Migration runbook section (order: merge -> backup -> stop daemon -> migrate -> deploy -> verify) in `docs/runbooks/` or the configuration guide, whichever holds deploy notes today — follow the existing pattern.

### Task 6: Regression gate + final review

Run `evals/regression_gate.ps1` (GPU, reproducible server via `Start-Qwen`); attach the verdict to the report. Full suite. Whole-branch review (most capable model), one fix wave if findings, PR.
