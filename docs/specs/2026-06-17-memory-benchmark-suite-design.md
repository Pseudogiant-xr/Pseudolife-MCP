# Memory benchmark suite — design spec

Status: **proposed (design)**, pending review + plan.
Target: **Pseudolife-MCP** `origin/master`. Dev-only; ships a new top-level
`evals/` tree, a small engine change (ingestion-time timestamp), docs, and a
results corpus. Does **not** touch the hot path or the public package.

> One-line intent: produce a **rigorous, reproducible, pre-registered**
> measurement of whether Pseudolife-MCP's memory is *good enough and
> differentiated enough* to productize — and, if so, the credibility artifact
> that sells it.

## 1. Problem

We want to decide whether to invest in productizing Pseudolife-MCP. That
decision must be **evidence-led**, not vibes-led, because:

1. **Internal go/no-go.** The agent-memory category is crowded and partly
   commoditized by model providers. We only proceed if we can *measure* an edge
   — specifically on **knowledge updates** and **abstention**, where our cortex
   (provenance-tiered supersession) and deterministic `fact_get` should beat
   vector-only systems, and where vector-only systems are known to be weak.
2. **External credibility.** In this space the benchmark *is* the marketing.
   The first thing a skeptical reader does is attack the methodology. So the
   harness must be fair, reproducible, and public, with baselines we ran
   ourselves under identical conditions (never self-reported competitor numbers).

There is **no eval scaffolding today** (`evals/`, `benchmarks/` absent). Clean
slate.

## 2. Goals / non-goals

**Goals**

- Quantify **accuracy per ability** on standard long-term-memory benchmarks.
- Quantify **operational cost**: end-to-end latency, tokens injected per query,
  ingestion throughput, storage growth.
- Run **baselines ourselves** (full-context, naive RAG, Mem0) under one harness.
- **Pre-register** exact win conditions (§3) before running, so results can't be
  goal-post-moved post hoc.
- **One-command reproducibility** with pinned datasets, models, and image digest.

**Non-goals**

- Not optimizing the engine in this work — **measure first**, tune later (a
  separate effort, on a held-out dev split only).
- Not building multi-tenancy / SaaS plumbing.
- Not a public leaderboard submission yet.
- Not claiming the neural (MIRAS) layer wins — we *measure* its contribution via
  ablation (§9) and let the numbers speak.

## 3. Pre-registered win conditions (the gate)

Decided **before** any run. These are the contract.

> **Thesis revision (2026-06-17), after Step 0 + Tier A.** The original gate
> (win *knowledge-update* + *abstention* by +5 pts) is retired. Step 0 found the
> bar is **Mem0 94.4 LongMemEval / 92.5 LoCoMo at ~7k tokens/call**, with
> *knowledge-update near-saturated industry-wide* — so raw-accuracy supremacy on
> update is not a realistic solo wedge. Tier A separately confirmed our
> mechanism *works* deterministically (update auto-supersedes, abstention returns
> null, multi-hop derives transitively). The open, defensible wedge is therefore
> **token-efficiency + latency + self-hosted sovereignty at competitive
> accuracy**, with *abstention* as a secondary edge. The gate below reflects that.

### 3.1 Efficiency-at-matched-accuracy gate (primary)

Across LongMemEval (+ LoCoMo secondary), identical answerer + judge across all
systems:

| Outcome | Condition | Action |
|---|---|---|
| **GREEN** | SUT accuracy within **3 pts** of the best of {full-context, naive-RAG, Mem0}, **AND** injects **≤ 60%** of naive-RAG's tokens/query (and in the ballpark of Mem0's ~7k), **AND** `search` p95 < §3.2 target | Pursue open-core on the *lean, self-hosted memory layer* positioning + writeup |
| **AMBER** | Competitive accuracy but **token parity** (not leaner), or leaner but accuracy 3–8 pts behind | Sovereignty/latency-only positioning; reconsider scope |
| **RED** | Accuracy >8 pts behind **AND** not leaner than naive-RAG | Stop. Keep as a superb personal tool |

Secondary (tie-breaker) edge: **abstention** — correctly declines on
unanswerable questions at a higher rate than the baselines (Tier A shows we
abstain cleanly by null; confirm it survives noisy real data).

Rationale: Mem0's own headline metric is **tokens/call**, not accuracy — the
category competes on cost-efficiency at a saturated accuracy ceiling. Our cortex
returns *one* canonical value rather than a pile of chunks, which should be
structurally token-lean. If we can't be leaner than plain vector RAG at matched
accuracy, the cortex/graph complexity isn't earning its keep.

### 3.2 Operational gate (necessary, not sufficient)

A memory layer that taxes every agent turn is unshippable regardless of
accuracy. Thresholds (loopback, warm daemon, CPU embed):

| Metric | Target | Hard fail |
|---|---|---|
| `memory_search` p95 | < 250 ms | > 750 ms |
| `memory_fact_get` p95 | < 120 ms | > 400 ms |
| Tokens injected / query (p95) | < 1500 | > 4000 |
| Ingestion throughput | > 20 turns/s | < 5 turns/s |

Numbers are provisional; confirm against a first measurement, then freeze.

## 4. Benchmark selection

| Benchmark | Role | Why | Pin |
|---|---|---|---|
| **LongMemEval** | **Primary** | ~500 questions across five abilities — *information extraction, multi-session reasoning, temporal reasoning, knowledge updates, abstention* — which map directly onto our cortex/graph design. Newer ⇒ less contamination. | git commit + `_S` (~115k-token contexts) variant for v1; `_M` later |
| **LoCoMo** | Secondary | The comparison readers expect (Mem0/Zep cite it). Report for continuity. | git commit; note saturation/contamination caveats in writeup |
| MSC, LongMemEval-oracle | Optional later | Ceiling / additional coverage | — |

**Explicitly excluded:** generic QA/RAG benchmarks (not memory-specific) and any
set with known train-split contamination for our answerer model.

**Contamination stance:** treat LoCoMo as possibly seen by the answerer; lean on
LongMemEval as primary and always report the **delta vs full-context** (B0) so a
contaminated absolute number can't flatter us.

## 5. System-under-test (SUT) adapter

Pseudolife-MCP is driven **only through its MCP tools** (no private internals) so
the adapter measures the actual product.

**Ingestion** (per benchmark conversation):
- `memory_episode_start` per session; `memory_episode_end` at session close.
- Each dialogue turn → `memory_store(text, source=<speaker>, origin=...)` with
  speaker + session metadata.
- **Timestamp fidelity (required engine change, see §12):** benchmark turns carry
  their own timestamps; `memory_store` currently stamps `now()`, which breaks the
  *temporal-reasoning* category. Add an optional `occurred_at` (epoch) to the
  store path so the adapter replays original time. Without it, temporal-category
  results are invalid and must be reported as N/A.

**Retrieval** (per question), two ablatable modes:
- `search` — `memory_search(query, top_k=k)` only (k tuned to the token budget,
  not a fixed count).
- `full` — `memory_search` + `memory_fact_get` for slot-shaped questions +
  `memory_graph` for multi-hop, merged into the context.

**Answering:** a **fixed answerer model** receives `[question + retrieved
context]` and produces an answer. The answerer is identical across SUT and all
baselines — we measure the *memory layer*, not the model.

## 6. Baselines (apples-to-apples, run by us)

| ID | System | Isolates |
|---|---|---|
| **B0** | Full-context (whole history in prompt, where it fits) | Upper bound on accuracy; abstention floor; the "do you even need memory" question |
| **B1** | Naive RAG (plain pgvector top-k over raw turns, no cortex/graph/neural) | The value added by *our architecture* over plain vector search — the most important baseline |
| **B2** | Mem0 (OSS, pinned) | The market comparator |
| **B3** | Zep / Graphiti (OSS) | Optional v2; graph-memory comparator |

Every baseline uses the **same answerer, same judge, same context-token budget**.
We never cite competitors' self-reported numbers — only numbers we reproduce here.

## 7. Threats to validity & controls

The doc readers will attack this section; it must be airtight.

- **Judge bias / noise.** LLM-as-judge at temperature 0; judge prompt published;
  judge calls cached by content hash. **Validate the judge:** human-label N≥50
  random items, report judge↔human agreement (Cohen's κ); if κ < 0.7, revise the
  judge before trusting any number.
- **Answerer parity.** One answerer model+version across all systems.
- **Budget parity.** Equal **token** budget (not k) — vector systems get more
  short chunks, ours fewer richer ones; tokens is the fair axis.
- **No test-set tuning.** Any hyperparameter choice uses a held-out dev split;
  the test split is touched once, at the end.
- **Contamination.** Primary = LongMemEval; always report delta-vs-B0.
- **Determinism.** `HF_HUB_OFFLINE=1`, fixed seeds, pinned dataset commits, model
  ids+dates, daemon image digest, locked requirements.
- **Isolation.** Each run gets a **fresh ephemeral Postgres** (own DB/volume) —
  no row leakage between runs/systems (cf. the known PG test-isolation gotcha).
- **Cost honesty.** Log tokens + wall-clock for every system; publish the bill.

## 8. Metrics

**Accuracy**
- Overall, and **per LongMemEval ability** (the per-category table is the story).
- Abstention reported separately as precision/recall of "I don't know" on the
  unanswerable subset (penalize confident wrong answers).

**Operational**
- `store` / `search` / `fact_get` latency p50/p95/p99.
- Tokens injected per query (mean, p95).
- Ingestion throughput (turns/s); storage bytes per 1k turns.

**Efficiency frontier**
- Accuracy vs tokens-injected scatter — the real product trade-off; a system that
  matches B0 at 1/10th the tokens is the actual win.

## 9. Experimental matrix (ablations)

Shows *which component earns its complexity* — vital both internally and for the
writeup's credibility.

| Variant | cortex | graph | neural blend | reranker | bm25 |
|---|---|---|---|---|---|
| `sut:full` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `sut:search` | ✓ | – | ✓ | – | – |
| `sut:no-cortex` | – | ✓ | ✓ | – | – |
| `sut:no-graph` | ✓ | – | ✓ | – | – |
| `sut:no-neural` | ✓ | ✓ | – | – | – |
| `sut:vector-only` (= B1) | – | – | – | – | – |

Each variant × {LongMemEval, LoCoMo}. The `no-*` deltas attribute credit to each
subsystem; if `no-neural` ≈ `full`, that's an honest signal the MIRAS layer isn't
pulling weight for this task (informs roadmap and marketing claims).

## 10. Harness architecture

New, dev-only, kept out of the installable package:

```
evals/
  README.md
  datasets/            # downloaders only; raw data gitignored
    longmemeval.py
    locomo.py
  adapters/
    base.py            # Adapter Protocol: ingest(conv) / query(q) -> Answer
    sut_pseudolife.py  # drives MCP tools over HTTP
    baseline_fullctx.py
    baseline_naive_rag.py
    baseline_mem0.py
  runner.py            # orchestration; fresh ephemeral PG per run
  judge.py             # LLM-as-judge, cached, temperature 0
  metrics.py           # accuracy/per-category + latency/token aggregation
  report.py            # results JSON -> markdown tables + frontier plot
  configs/*.yaml       # pinned models, budgets, dataset commits
  results/             # COMMITTED: per-question JSON + generated report.md
```

- **Adapter Protocol** is the single seam — adding a competitor = one file.
- Latency captured in-band from the SUT adapter (per-tool timings) and from
  `/health`-warmed daemon; micro-bench mode hammers each tool in isolation.
- Judge + answerer behind one OpenAI-compatible client (any provider; pinned).
- CLI: `python -m evals.runner --suite longmemeval --variant sut:full \
  --answerer <id> --judge <id> --budget-tokens 4000`.

## 11. Reproducibility & publication

- **Pin everything:** dataset commit hashes, model ids+dates, `pseudolife-daemon`
  image digest, a locked `evals/requirements.txt`.
- **Publish:** adapter + baseline code, judge prompt, **raw per-question results
  JSON**, and the report generator — so anyone can rerun and diff.
- **Repro command** documented in `evals/README.md`; CI-smoke on the 5-item
  sample so the harness can't rot.
- Blog/writeup template lives in `docs/` but is only filled if §3 is GREEN/AMBER.

## 12. Required engine change (small, in-scope)

- **Ingestion-time timestamp.** Add optional `occurred_at: float | None` to the
  store path (`memory_store` tool → `MemoryService.store` → entry `ts`). Defaults
  to `now()` (unchanged behavior). Needed for the temporal-reasoning category and
  generally useful for backfilling/imports. ~1 small change + a test. This is the
  only engine modification the benchmark *requires*; everything else is additive
  `evals/` code.

## 13. Risks

- **Judge unreliability** → mitigated by κ validation gate (§7).
- **Temporal category invalid** without §12 → ship §12 first or mark N/A.
- **Baseline-misconfig accusations** → publish exact baseline configs; invite
  issues.
- **Our own overfitting** → dev/test split discipline (§7).
- **Run cost.** ~500 Q × (answerer + judge) × ~6 SUT variants × 3+ baselines is
  real token spend. Estimate up front in `configs/`, cap with `--limit` for dev,
  full run once. Use a cheap-but-capable answerer/judge (decision in §15).
- **LoCoMo saturation** makes it low-signal → keep it secondary, lead with
  LongMemEval.

## 14. Phases (sequencing; effort rough)

| Phase | Deliverable | Effort |
|---|---|---|
| **0** | Harness skeleton: Adapter Protocol, runner, judge, a 5-item smoke set wired end-to-end (SUT + B0) | ~1–2 d |
| **1** | §12 timestamp change; LongMemEval loader; metrics+report; **SUT(full) + B0 + B1** | ~3–4 d |
| **2** | Mem0 baseline (B2); ablation variants (§9) | ~2–3 d |
| **3** | LoCoMo; latency micro-bench; efficiency-frontier plot | ~2 d |
| **4** *(gated on §3)* | Writeup + OSS results corpus | ~2 d |

Phases 0–1 alone answer the go/no-go for the *update + abstention* thesis vs
naive RAG — the cheapest decisive signal. Stop there if RED.

## 15. Decisions (locked 2026-06-17)

1. **Models — high-fidelity answerer.** Answerer = **Sonnet-class** (pin
   `claude-sonnet-4-6` by date), to measure closest to real product use. Judge =
   **Haiku-class** (`claude-haiku-4-5`), temperature 0, cached. *Same-family
   answerer↔judge optic is disclosed and mitigated by the κ-validation gate (§7);
   a cross-family judge remains a cheap rigor upgrade for the public run if the
   κ check is borderline.*
2. **Baselines v1 = B0 + B1 + Mem0.** Zep/Graphiti (B3) deferred to v2.
3. **Run-cost ceiling — see §16.** Provisional: dev with `--limit 25`; one full
   `_S` sweep; **B0 may run on a question subset** (it's only an upper-bound
   reference and is the dominant cost driver) with confidence intervals.
4. **§3 win numbers — proposed, pending final lock.** The +5 pt update/abstention
   bar and the §3.2 latency targets stand unless revised at review.

## 16. Cost estimate (high-fidelity answerer)

Order-of-magnitude for one full LongMemEval_S sweep (~500 Q), so there are no
surprises. Sonnet-class answerer is the driver; Haiku judge is rounding error.

| System | Answerer input / Q | × Q | Notes |
|---|---|---|---|
| **B0 full-context** | ~115k tok | 500 | **Dominant cost** — full history in prompt |
| SUT variants (×6, §9) | ~4–8k tok | 500 ea | Budget-bounded retrieved context |
| B1 naive RAG | ~4–8k tok | 500 | Same budget |
| Mem0 | ~4–8k tok | 500 | Same budget |

- **B0 alone** ≈ 500 × 115k ≈ **~58M input tokens** → low-hundreds of USD at
  Sonnet input rates. Everything *else combined* (~8 systems × 500 × ~6k ≈ 24M
  tokens) is materially cheaper than B0.
- **Mitigation:** run B0 on a **stratified subset** (e.g. 150 Q, balanced across
  the 5 abilities) and report with CIs — it's a reference bound, not the verdict.
  SUT/B1/Mem0 run the full 500. Keeps the bill to a **modest few hundred USD**,
  most of it optional (B0 subset).
- Pricing is model-dependent and changes; `evals/configs/*.yaml` carries a
  `--max-usd` guard and prints a dry-run estimate before any paid call.

Confirm you're comfortable with that ballpark (or set a hard `--max-usd`) and the
harness build can start at Phase 0.
