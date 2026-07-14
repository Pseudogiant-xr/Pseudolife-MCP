# Pluggable LLM extraction + minimum-viable-model benchmark — design spec

Status: **proposed (design)**, pending review + plan.
Target: **PseudoLife-MCP** `origin/master`. Adds an optional compose service +
config + a small abstention knob + a dev-only `evals/` ladder sweep. Reuses the
existing `OpenAICompatExtractor` / dream machinery — no new extractor code on the
hot path. Touches `ops/docker-compose.yml`, a new `ops/Dockerfile.extractor`
(or upstream llama.cpp image), `pseudolife_memory/utils/config.py`,
`pseudolife_memory/service.py` (search abstention floor + a `dream_run` force
path), `pseudolife_memory/mcp_server.py` (surface the knob), `evals/`, docs, tests.

Related: [dream extractor design](2026-06-15-pluggable-dream-extractor-design.md)
(the pluggable extractor this builds on) and
[benchmark suite design](2026-06-17-memory-benchmark-suite-design.md) (the gate
this feeds).

## 1. Problem

The 2026-06-17 pre-flight (Tier A + Tier B) established that PseudoLife-MCP's
*mechanism* (cortex supersession, abstention-by-null, transitive graph) works in
clean conditions, but on **realistically-phrased ingested conversation** it
degrades to ≈ naive-RAG: stale-leak 0.33 == RAG 0.33, gold-recoverable 0.6 < RAG
1.0, only ~31% leaner on tokens. Root cause is **auto-extraction recall**, not
the cortex:

- The store-path auto-promote uses only the deterministic `extract_slots`, which
  is precision-first and brittle: "Update: …port is now 9090" yields entity
  *"Update the checkout-service"* + value *"now 9090"* (junk, no supersession);
  "migrated …to db-prod-2" has no copula `is` so nothing extracts at all.
- With a sparse cortex the system falls back to vector search — i.e. it *becomes*
  naive RAG.

This is the exact problem Mem0/Zep spend an LLM on (memory extraction). The
pluggable `OpenAICompatExtractor` already exists but only runs in the lazy
background dream sweep and only if a user configures an endpoint. **Out of the
box, extraction is the deterministic floor, and that is the bottleneck blocking
the productization wedge.**

## 2. Goals / non-goals

**Goals**
- Make **LLM extraction a first-class, batteries-included path** (a small CPU
  model shipped with the stack), behind the existing OpenAI-compatible interface.
- **Find the minimum viable extraction model** via a benchmark ladder sweep — the
  smallest/most-convenient rung that beats naive-RAG on the gate metrics.
- Fix the **abstention** miss (search returns distractors instead of declining).
- Keep extraction **off the store hot-path** (runs in consolidation).
- Preserve **self-hosted sovereignty** (local-first; cloud is opt-in comparison).

**Non-goals**
- No new extractor *algorithm* — reuse `OpenAICompatExtractor` + `build_extractor`.
- No synchronous store-time LLM calls (stores stay fast).
- Deterministic-floor hardening (prefix/"is now"/change-verbs) — **deferred**.
- Graph-from-free-text relation parsing — **deferred**.
- Multi-tenancy / SaaS plumbing — out.

## 3. The extractor ladder

Every rung is the same interface — an OpenAI-compatible `/chat/completions`
endpoint behind `OpenAICompatExtractor`. Only the base-URL + model change, so the
sweep is purely a config matrix.

| # | Extractor | Host | Sovereign | Role |
|---|---|---|---|---|
| 0 | Deterministic floor (`extract_slots`) | in-daemon | ✓ | baseline (known weak) |
| 1 | **Gemma 4 E2B** (Q4 GGUF) | CPU sidecar in compose | ✓ batteries-incl | smallest "just works" candidate |
| 2 | **Gemma 4 E4B** (Q4 GGUF) | CPU sidecar in compose | ✓ batteries-incl | bigger CPU candidate |
| 3 | **Qwen 3.6 35B A3B** | 5800X3D homelab (CPU MoE, ~3B active) | ✓ separate box | sweet-spot: near-GPU quality, CPU-fast |
| 4 | **Qwen 3.6 27B** | 4090 (GPU) | ✓ separate box | strong local ceiling |
| 5 | Haiku-class | cloud | ✗ | convenience / quality ceiling |

**Minimum viable = the lowest rung that clears the gate** (beats naive-RAG on
stale-leak + gold-recoverable at competitive tokens/query). Interpretation:
- rung 1/2 clears → **batteries-included** product (CPU, no GPU, no cloud) — the
  strongest self-hosted wedge.
- only rung 3/4 clears → product needs a local LLM (still sovereign).
- only rung 5 clears → sovereignty wedge weakens; reassess.

## 4. Architecture / components

- **`pseudolife-extractor` sidecar** (new, *optional* compose service):
  llama.cpp-server (CPU), exposing OpenAI-compatible on the compose-internal
  network only (not published to host). Two model-delivery modes:
  *(a)* **shipped default** — the chosen rung's GGUF is **baked into the extractor
  image** so `docker compose up` is genuinely batteries-included (no manual
  download step); *(b)* **benchmark/swap** — a GGUF mounted from a volume so the
  sweep swaps rungs 1 ↔ 2 (and arbitrary models) without rebuilding.
- **No new extractor code.** The daemon points `PSEUDOLIFE_DREAM_BASE_URL` /
  `_MODEL` at the sidecar (rungs 1/2) or at a LAN/cloud endpoint (rungs 3/4/5).
  `build_extractor` already resolves env → `OpenAICompatExtractor`.
- **Daemon image stays lean** — the model lives in the sidecar image/volume, not
  the daemon image.
- **This spec ships the sidecar opt-in.** Flipping it default-on (and which rung)
  is a follow-up gated on the §7 benchmark verdict — we don't bake a 2–3 GB model
  into the default stack until the numbers justify it.

```
memory_store (fast, deterministic floor only) ─────────────► entries + cortex(floor)
                                                                   ▲
consolidation sweep (off hot-path):                                │ claims
   dream_pull → OpenAICompatExtractor(sidecar/LAN/cloud) → dream_commit
```

## 5. Consolidation flow (where extraction runs)

- Extraction runs in the **dream sweep**, never on `memory_store`.
- The harness (and power users) trigger it on demand via `memory_dream_run`.
  **Verified (no `force` flag needed):** `service.dream_run(extractor, *,
  limit=None)` does *not* gate on the quiescence trigger (that lives only in
  `run_sweep_once`) — it consolidates the eligible backlog directly, capped at
  `limit` (default `max_batch`=40) and advancing the cursor. So a full
  consolidation is just: loop `dream_run` until `pulled == 0`. Only nicety: add an
  optional `limit` arg to the `memory_dream_run` MCP tool so a large one-shot
  sweep is a single call (else loop the existing no-arg tool).
- Cursor discipline is unchanged (session-agnostic, keyed on `dream_cursor`).

## 6. Abstention threshold (small, paired)

- Add `memory.search.min_score` (config) and/or a `min_score` arg to
  `memory_search`: when the top adjusted score is below the floor, return an
  empty result set flagged `low_confidence: true` instead of weak distractors,
  so the agent abstains rather than fabricates.
- Default conservative (off or low) to avoid harming normal recall.
- **Tested, not guessed (§7 axis).** Sweep a small set of thresholds
  (e.g. `{off, 0.15, 0.25, 0.35}`) on a **dev split** and report the tradeoff:
  *abstention precision/recall on the unanswerable subset* vs *gold-recoverable
  on the answerable subset*. Pick the knee, freeze it, and only then touch the
  test split. The shipped default is whatever the dev-split knee says — not a
  guess.

## 7. Benchmark harness (dev-only `evals/`)

- Reuse the Tier B efficiency screen. For each reachable rung: set the extractor
  env → ingest the corpus → `memory_dream_run(force=True)` → query
  (`fact_get`/`search`/`graph`) → measure **stale-leak, gold-recoverable,
  tokens/query, search latency**, plus **extraction wall-time per rung** (CPU
  rungs are slower; that's expected and reported since it's off the hot-path).
- Output: a per-rung table + the minimum-viable verdict against §3.
- Baseline: naive-RAG (pgvector top-k over raw turns, same embedder) as in the
  2026-06-17 screen.
- **Abstention sub-sweep:** with the best extractor rung fixed, sweep
  `min_score ∈ {off, 0.15, 0.25, 0.35}` on the dev split and emit the
  abstention-precision/recall vs gold-recoverable curve (§6) to pick the
  shipped default.

## 8. Error handling / degradation

- Sidecar down / extractor timeout / malformed JSON → `OpenAICompatExtractor`
  returns `[]` → dream falls back to the deterministic floor (rung 0). No crash.
- Benchmark **skips unreachable rungs** (4090 off, homelab off) and reports N/A.
- Abstention floor failure mode is conservative: a too-high floor reduces recall
  (caught by the dev-split tuning), never fabricates.
- **Sidecar JSON output:** `OpenAICompatExtractor` requests
  `response_format: json_object`; not every server build honors it. Mitigation:
  the extractor already parses leniently and returns `[]` on malformed JSON (→
  floor fallback), and the sidecar's llama.cpp build should enable JSON/grammar-
  constrained output. Verify per rung in the sweep — a rung that can't emit clean
  JSON is itself a finding.

## 9. Operational caveats (flagged, not blockers)

- **Rung 4 (4090)** is the daily-driver GPU — opt-in; requires llama.cpp serving
  there + explicit go-ahead before running (standing caution).
- **Rung 3 (homelab 5800X3D)** requires Qwen 3.6 35B A3B served (llama.cpp/
  Ollama) and reachable on the LAN (192.168.x.x).
- **Rungs 1/2** are self-contained (run unattended); cost is image/volume size
  (~1.5–3 GB Q4 GGUF) and CPU extraction time.

## 10. Testing

- Unit: sidecar-extractor config wiring (`build_extractor` resolves sidecar URL);
  abstention floor (top-score below → `low_confidence` empty; above → normal).
- Integration: ingest → `dream_run(force=True)` with a **stub extractor** (no
  network) → assert cortex populated + old value superseded (proves the
  consolidation path end-to-end without depending on a live model).
- The ladder sweep itself lives in `evals/` (dev-only, not unit tests).

## 11. Touches

- `ops/docker-compose.yml` — optional `pseudolife-extractor` service + volume.
- `ops/Dockerfile.extractor` (or pin an upstream llama.cpp server image).
- `pseudolife_memory/utils/config.py` — `search.min_score` (+ extractor defaults).
- `pseudolife_memory/service.py` — search abstention floor (`dream_run` already
  takes `limit`; no change needed there).
- `pseudolife_memory/mcp_server.py` — surface `min_score` on `memory_search`;
  optional `limit` on `memory_dream_run` for one-shot full sweeps.
- `evals/` — ladder sweep + report (extends the 2026-06-17 screen).
- `README.md`, `CHANGELOG.md`, tests.

## 12. Deferred / out of scope

- Deterministic-floor hardening (prefix stripping, "is now", change-verbs).
- Graph edges from free-text relational statements.
- In-process GGUF extractor (Approach ②) — sidecar chosen instead.

## 13. Decisions (locked at design)

1. Posture: **hybrid** (deterministic floor for zero-config + LLM for quality).
2. Integration: **sidecar** (Approach ①), reuse `OpenAICompatExtractor`.
3. Extractor default: **local-first**, cloud opt-in; **benchmark compares both**.
4. Scope: extractor ladder + min-viable benchmark + abstention threshold;
   floor-hardening and graph-from-text deferred.
5. Ladder rungs: deterministic / Gemma 4 E2B / E4B / Qwen 3.6 35B A3B (5800X3D) /
   Qwen 3.6 27B (4090) / Haiku-class (cloud).
