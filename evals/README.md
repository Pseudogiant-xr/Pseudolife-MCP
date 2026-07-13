# Extractor-ladder benchmark (`ladder_sweep.py`)

Dev-only sweep that answers one question: **what is the minimum viable
extraction model** for dream consolidation? It runs the same
knowledge-update corpus through each rung of the extractor ladder — from the
deterministic regex floor up to LAN GPU models — and reports whether each
rung beats naive-RAG on staleness, gold recovery, and token efficiency.

This is **not** part of the test suite or the shipped package. It exists to
make the "should the sidecar be default-on, and at which rung?" decision
(see `docs/specs/2026-06-18-pluggable-llm-extraction-design.md` §4) with data
instead of a guess.

## Isolation & safety

- Runs against a dedicated **`pseudolife_memory_bench`** database (created if
  missing, truncated before each ingest). The live bank
  (`pseudolife_memory`) is **never** touched.
- Forces **CPU** (`CUDA_VISIBLE_DEVICES=-1`) for the embedder so the host GPU
  is left alone. The LLM rungs run wherever their endpoint runs (sidecar on
  CPU, LAN models on their own GPUs).
- Sets `protect_provenance=False` on the bench service so the measurement is
  pure *extraction quality*, not the cortex contender-parking policy.
- Unreachable LLM rungs are skipped and recorded as `status: "unreachable"`.

## Rungs

| rung        | extractor                          | endpoint                     |
|-------------|------------------------------------|------------------------------|
| `naive-rag` | none — top-k vector search baseline| —                            |
| `floor`     | deterministic regex (`RegexExtractor`) | — (in-process)           |
| `gemma-e2b` | Gemma 4 E2B (Q4) CPU sidecar       | `http://127.0.0.1:8081/v1`   |
| `gemma-e4b` | Gemma 4 E4B (Q4) CPU sidecar       | `http://127.0.0.1:8081/v1`   |
| `qwen-a3b`  | Qwen3.6-35B-A3B (homelab 5800X3D)  | `$PSEUDOLIFE_BENCH_A3B_URL` (default `http://127.0.0.1:1236/v1`) |
| `qwen-27b`  | Qwen3.6-27B (4090)                 | `$PSEUDOLIFE_BENCH_QWEN_URL` (default `http://127.0.0.1:1234/v1`) |

The cloud rung is intentionally omitted — this is a sovereign-only sweep.

`gemma-e2b` and `gemma-e4b` share the **same** `:8081` endpoint: the operator
swaps the served GGUF between the two runs (see below). Run one, then the
other.

## Prerequisites

The benchmark talks to a **host-published** llama.cpp on `127.0.0.1:8081`.
Note this is *separate* from the default-on compose sidecar
(`pseudolife-mcp-extractor`), which is internal-only (`expose:`, not `ports:`)
and reachable only by the daemon on the compose network.

**Gemma E2B** (default baked image):

```bash
docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 \
  pseudolife-extractor:gemma4-e2b
```

**Gemma E4B** — stop the E2B container, then serve the E4B GGUF on the same
port. Either bake a second image:

```bash
docker build -f ops/Dockerfile.extractor -t pseudolife-extractor:gemma4-e4b \
  --build-arg MODEL_URL=https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf ops
docker rm -f pseudolife-mcp-extractor-bench
docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 \
  pseudolife-extractor:gemma4-e4b
```

…or mount any GGUF over the baked default without a rebuild:

```bash
docker rm -f pseudolife-mcp-extractor-bench
docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 \
  -v /abs/path/gemma-4-E4B-it-Q4_K_M.gguf:/models/extractor.gguf:ro \
  pseudolife-extractor:gemma4-e2b
```

**LAN rungs** need the endpoints in the table reachable (an OpenAI-compatible
`/v1` server such as llama.cpp or LM Studio). Confirm with
`python evals/ladder_sweep.py --list`; unreachable rungs are skipped cleanly.

## Running

All commands from the repo root. `PYTHONPATH=.` lets the script import
`pseudolife_memory`; `TORCHDYNAMO_DISABLE=1` just silences torch's CPU
compile-fallback warnings (cosmetic — the script already forces HF offline).

```bash
# list rungs + endpoints, with reachability
PYTHONPATH=. python evals/ladder_sweep.py --list

# run rungs one at a time (each writes results/<rung>.json)
PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --rung naive-rag
PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --rung floor
PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --rung gemma-e2b
# … gemma-e4b, qwen-a3b, qwen-27b

# abstention threshold sub-sweep on a chosen (consolidated) rung
PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --abstain gemma-e2b

# aggregate everything in results/ into the table + verdict
PYTHONPATH=. python evals/ladder_sweep.py --report
```

Each rung is its own process and writes its own `results/<rung>.json`, so the
slow CPU/LAN rungs can run incrementally — kill and resume between rungs
without losing finished ones. `--report` reads whatever is present.

> On Windows the per-rung temp dir may leak (ChromaDB keeps the SQLite handle
> open for the life of the process); the harness ignores the cleanup error and
> the OS reaps `%TEMP%` later. Harmless.

## Metrics

Per rung, measured over the update-pair corpus:

- **`gold_recoverable`** ↑ — fraction of pairs whose **current** value the
  system returns (cortex fact block for the SUT; top-k turns for naive-RAG).
- **`stale_leak`** ↓ — fraction whose **old**, superseded value is still
  returned.
- **`tokens_per_query`** ↓ — approx tokens the agent must read to answer
  (cortex block vs. raw top-k turns). The efficiency case for consolidation.
- **`search_latency_ms`** — mean answer latency.
- **`extract_seconds`** — wall-time to consolidate the whole corpus. Off the
  hot path (dreaming is background), so reported, **not** penalised — CPU
  rungs are slower by construction.

## Reading the verdict

`--report` prints the per-rung table, then the gate. A rung **clears** if it
beats naive-RAG on both staleness and gold recovery while reading **≤60% of
naive's tokens/query**:

```
stale_leak < naive.stale_leak
gold_recoverable > naive.gold_recoverable
tokens_per_query <= 0.6 * naive.tokens_per_query
```

The lowest rung (in ladder order) that clears is the **minimum viable**
extractor — the cheapest model worth shipping as the default.

The abstention sub-sweep (`--abstain`) sweeps a **2-D grid** of the cortex guard
`guard_min_score ∈ {0.3, 0.5, 0.65, 0.75, 0.85}` × `search_confidence_floor ∈
{0.0, 0.5, 0.65, 0.70, 0.75, 0.80}` (the floors bracket the embedder's actual
score distribution — answerable max-scores 0.75–0.98, unanswerable 0.38–0.78;
floors below ~0.5 never fire) and reports, per cell:

- **`abstain_recall_unanswerable`** ↑ — fraction of never-stated probes that
  correctly return `low_confidence=True`.
- **`false_abstain_answerable`** ↓ — fraction of answerable questions wrongly
  flagged low-confidence.

Pick the `(guard, floor)` pair that maximises `abstain_recall` while keeping
`false_abstain_answerable` at/near zero. The guard is the binding constraint:
any cortex fact scoring `≥ guard_min_score` is surfaced as an answer and
suppresses abstention, so the floor alone can't recover near-misses where a weak
topically-adjacent fact is present.

The supersession sub-sweep (`--supersede`) ingests the update-pair corpus plus
`NO_MERGE` distractors (same-entity/different-attribute and
different-entity/same-attribute pairs that must stay distinct) and sweeps
`dream_slot_match_threshold ∈ {off, 0.80, 0.85, 0.90, 0.95}`, reporting
`superseded` ↑, `stale_leak` ↓ (the win) and `false_merge` ↓ — distractor slots
wrongly collapsed (the cost). The shipped default is the lowest threshold that
drives `stale_leak` down at `false_merge = 0`; if none does, the resolver stays
off.

---

# LongMemEval knowledge-update benchmark (`longmemeval_bench.py`)

The first **external** benchmark: the knowledge-update subset (78 questions)
of [LongMemEval](https://arxiv.org/abs/2410.10813) — the ability the HLC
supersession spine is built for. Everything runs **locally** (extractor,
answerer, judge); nothing leaves the machine.

## Dataset

Download from HuggingFace (`xiaowu0162/longmemeval-cleaned`) into
`evals/data/` (gitignored):

- `longmemeval_oracle.json` — evidence-only sessions (~15MB). Isolates
  extraction + supersession quality with no retrieval noise.
- `longmemeval_s_cleaned.json` — full haystacks (~265MB), median ~48
  sessions / ~122k tokens per question. The realistic setting.

## Design

Three arms answer every question from the same ingested memory:

| arm | context | measures |
|-----|---------|----------|
| `rag` | top-6 raw turns (vector search) | naive-RAG baseline — **never touches the extractor**, so it doubles as a cross-run control |
| `cortex` | top-8 canonical facts, each with its supersession chain (`svc.history`) appended | the fact spine alone |
| `hybrid` | facts + top-3 raw turns | the product posture |

Model roles are split so extraction quality is the **only** variable:

- **Extractor** (varies): `gemma-e2b` (the shipped CPU sidecar's model,
  GPU-served for bench speed — ladder-verified identical output at
  temperature 0) = the **floor**; `qwen-27b` = the local **ceiling**.
- **Answerer + judge** (constant): Qwen3.6-27B for every run, LongMemEval's
  LLM-as-judge protocol. All calls request `temperature: 0`.

Serving config for reproducibility: Qwen3.6-27B **Unsloth UD-Q4_K_XL**
(~4.5bpw) on a llama.cpp MTP fork with 4.25-bit (`tbq4_0`) KV cache.
Both quantizations trade some fidelity for fitting 24GB — treat the
ceiling as "27B-class local", not "27B at BF16".

Ingestion mirrors the product cadence: turns are stored session-by-session
in chronological order and the dream consolidates after each session.
Results are per-question JSONL (append-only, atomic rewrite) so any run can
be killed and resumed. `--phase extract` / `--phase answer` split the work
so only one model needs the GPU at a time; `--tag` namespaces experiment
runs. Every extract run also dumps the question's full fact bank (values +
history chains) to `results/banks/` and stamps rows with
`answer_in_current_fact` / `answer_in_history_only`, so a failure is
attributable to never-extracted vs overwritten vs not-retrieved.

```bash
# full run, one extractor
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle --extractor qwen-27b
# split phases (exclusive GPU tenancy), tagged experiment
PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor gemma-e2b --phase extract --tag exp1
PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor gemma-e2b --phase answer --tag exp1
# report from existing results
PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor qwen-27b --report
# the whole floor+ceiling night, unattended (watchdog restarts crashed servers)
evals\overnight_longmemeval.ps1
```

`retrieval_sweep.py` replays cortex retrieval over the dumped banks under
different `top_k` × `min_score` knobs **offline** — fact embeddings are a
pure function of fact text and cortex search is plain cosine, so the replay
is exact and needs no re-extraction (and no GPU).

## Findings — 2026-07-04

Accuracy / context-tokens-per-question, 78 questions, judge = local
Qwen3.6-27B:

| dataset | extractor | rag (control) | cortex | hybrid |
|---|---|---|---|---|
| oracle | qwen-27b (ceiling) | 0.615 / 1638 | 0.564 / **59** | **0.705** / 979 |
| oracle | gemma-e2b (floor) | 0.564 / 1638 | 0.192 / 112 | 0.474 / 1031 |
| s | qwen-27b | 0.321 / 2056 | 0.205 / 27 | **0.372** / 1114 |
| s | gemma-e2b | 0.346 / 2076 | 0.141 / 142 | 0.308 / 1229 |

- **Hybrid beats naive RAG on both datasets with the ceiling extractor** —
  +9pp on oracle at ~40% less context. Cortex alone reaches 92% of RAG's
  oracle accuracy on **3.6%** of its token budget (59 vs 1638 tok/q).
- **Extraction quality is the bottleneck, isolated causally**: the RAG
  control stays flat across extractors (0.56–0.62; it never touches the
  extractor) while cortex collapses 0.564 → 0.192 when the extractor
  shrinks. The retrieval spine is fine; what goes *into* it decides
  everything. (This is the measured case for pointing the dream at a
  bigger local model — see the README's "Upgrading the extractor".)
- **Supersession chains matter**: surfacing each fact's earlier values
  lifted the whole board vs current-value-only contexts (hybrid 0.590 →
  0.705 on oracle) — knowledge-update questions ask about the original
  value as often as the current one. The pre-history baseline is kept at
  `results/longmemeval-ku-oracle.v1-nohistory.jsonl`.
- **Abstention holds**: 6/6 abstention variants correct in the hybrid arm
  on both datasets.
- **Known `_s` gap under diagnosis**: at `min_score 0.3`, 45/78 haystack
  questions retrieve zero cortex facts (terse canonical fact strings score
  low cosine against verbose questions) and supersession churn is ~10×
  oracle's (970–1245 events). Both are being worked with the bank-dump
  diagnostics + offline sweep.

**Comparability caveat:** published LongMemEval numbers (TiMem 76.9%,
EverMemOS 83% overall) use GPT-4o-class answerers/judges and all 500
questions; these runs are all-local (27B answerer, 4-bit quant) on the
78-question knowledge-update slice. Compare arms and extractors *within*
this table, not against leaderboards.

---

# Lesson-synthesis benchmark (`lesson_synthesis_bench.py`)

A separate eval for the **procedural** path (schema v10): how well does each model
turn outcome SIGNALS into LESSONS (`extract_lessons`)? It stresses the parts the
declarative sweep doesn't — **clustering** related signals, and the
discriminators **polarity** (`+` do / `-` avoid) and **direction** (don't invert
a correction). Six fixtures, scored on count, polarity, outcome, and a
direction/faithfulness token check; full self-contained (stdlib only).

Runs inside the daemon container, which reaches both endpoints
(`pseudolife-extractor:8081` for Gemma, `host.docker.internal:1234` for the
4090):

```bash
docker cp evals/lesson_synthesis_bench.py pseudolife-mcp-daemon:/tmp/lb.py
docker exec pseudolife-mcp-daemon python /tmp/lb.py --target all
```

The prime optimisation target is the shipped **Gemma 2B** sidecar (what an
end-user runs); **Qwen3.6-27B** (4090) is the quality CEILING, not the target.
The `_LESSON_SYSTEM_PROMPT` here is tuned, then ported to `memory/dream.py`.

## Findings — 2026-06-21

Baseline (original prompt) vs the ceiling, then after a prompt iteration:

```
model                      full-pass  polarity  notes
Gemma 2B (baseline)           4/6       4/5     missed correction-polarity + noise-skip
Qwen3.6-27B (ceiling)         4/4*      3/3     *2 simple cases timed out cold-start
Gemma 2B (tuned prompt)       5/6       5/5     correction-polarity FIXED
```

- The ceiling confirmed the two gaps were **prompt-fixable** (the 27B got both
  right; Gemma is capable, it just needed clearer instructions).
- A prompt tweak — an explicit polarity rule (**a correction is almost always
  `+`: state the corrected, now-correct behavior, not the mistake**) plus a
  bulleted field spec — lifted Gemma from **4/6 → 5/6** with polarity/outcome/
  direction all 5/5 and clean clustering. Ported to `memory/dream.py`.
- **Remaining gap — `noise_skip`:** Gemma 2B still emits a low-value lesson for a
  trivial signal ("printed hello") where the 27B correctly returns `[]`. A second,
  more aggressive skip instruction did **not** fix it and *regressed* clustering
  (merged 3→2, mis-polarised a success), so it was reverted. Accepted as a
  genuine small-model capability gap; **low real-world risk** because signals come
  from deliberate `memory_outcome` calls + correction auto-tags, not arbitrary
  chatter. Revisit if a larger default sidecar (Gemma E4B) is ever shipped.
- Gemma already handles the **merged fail→success** case and **clustering** well
  — better than the v1 live smoke suggested (that smoke's inversion was not
  systematic at temperature 0).

## Findings — 2026-06-18 sweep

```
rung                           gold↑  stale↓   tok/q↓  extract s
naive-RAG (baseline)             0.7     0.3     59.2        0.0
deterministic floor              0.1     0.1      0.9        0.0
Gemma 4 E2B (CPU sidecar)        0.9     0.1      2.3       17.4
Gemma 4 E4B (CPU sidecar)        1.0     0.1      2.3       31.4
Qwen3.6-35B-A3B (homelab CPU)    1.0     0.1      2.3       45.8
Qwen3.6-27B (4090)               1.0     0.0      2.1        6.2
```

- **All four LLM rungs clear the gate.** Even the smallest CPU sidecar (Gemma 4
  E2B) beats naive-RAG on every axis — gold 0.9, stale 0.1, **~25× fewer tokens
  per query** (2.3 vs 59.2). **Minimum viable = Gemma 4 E2B.**
- **Quality ceiling = Qwen3.6-27B**: the only rung that consistently named the
  entity the same way across the `initial` and `update` turns, so the update
  *superseded* the stale value (stale_leak → 0.0). The smaller models split
  initial/update onto sibling slots (superseded=0), leaving one stale value
  retrievable (stale_leak 0.1).
- **Reasoning models need thinking disabled for extraction.** Before the fix,
  Qwen3.6 spent its whole 4096-token budget on a `<think>` trace and returned
  empty content → silent regex-floor fallback (gold 0.1, 399s). Adding
  `chat_template_kwargs:{enable_thinking:false}` + tolerant JSON parsing (strip
  ```json fences) to `OpenAICompatExtractor` fixed it (homelab 399s→46s; and it
  even sped up + improved Gemma E2B: 58s→17s, gold 0.8→0.9).
- **Abstention is cortex-guard-limited, not floor-limited.** `false_abstain` is
  0.0 at every floor (the cortex guard fully protects answerable queries);
  `abstain_recall` plateaus at 0.33 because any topically-adjacent cortex fact
  (guard `min_score=0.3`) suppresses abstention. A floor of ~0.65 captures all
  the available abstention with zero false-abstain; raising it further buys
  nothing. Tightening the cortex-guard min_score is the lever for more recall
  (future work — done in the 2026-06-19 sweep below).

## Findings — 2026-06-19 guard + supersession calibration

The two knobs added on `feat/supersession-abstention-tuning`
(`cortex.guard_min_score`, `cortex.dream_slot_match_threshold`), calibrated on
`gemma-e2b`.

> Single-writer note: `build_service` pins `cortex.auto_promote = False`, so the
> sweep measures the dream extractor alone — not the regex auto-promote floor,
> whose slot fragmentation was the real cause of the residual stale-leak (see the
> single-writer-cortex design). This is also the shipped default now.

**Abstention guard (Feature B) — a clear win.** On the `(guard, floor)` grid,
the knee at `false_abstain = 0` is `abstain_recall = 0.667`:

```
guard  floor   abstain_recall   false_abstain
0.30   0.70        0.333            0.0      (today's hardcoded behaviour)
0.65   0.70        0.667            0.0      ← recommended
0.65   0.75        0.833            0.1      ✗ (false-abstains appear)
0.75   0.80        1.000            0.2      ✗
```

Raising the guard `0.3 → 0.65` (paired with `search_confidence_floor = 0.70`)
**doubles** abstention recall at zero false-abstain. Pushing the floor higher
trades into wrongly abstaining on answerable queries. **Recommended for an
abstention-on deployment: `guard_min_score = 0.65`, `search_confidence_floor =
0.70`.** Both knobs ship at their behaviour-preserving defaults (`0.3` / `0.0`).

**Dream slot resolver (Feature A) — no measurable benefit; ships off.** Sweeping
`dream_slot_match_threshold` (distractor-clean corpus) moved nothing:

```
threshold   superseded   stale_leak   false_merge
off (0.0)        0           0.1            0
0.80             1           0.1            1     ← a false-merge, no leak win
0.85–0.95        0           0.1            0
```

`stale_leak` is flat at 0.1 at every threshold, and `0.80` *introduces* a
false-merge. **Root cause is not paraphrase** — tracing the residual leak showed
the deterministic regex **auto-promote** (`service.py:_promote_slots`, every
`store`) and the LLM dream write to the cortex with different `(entity,
attribute)` conventions, fragmenting one fact across sibling slots. No fuzzy
resolver can safely reconcile that. The resolver ships **off by default**; see
`docs/specs/2026-06-19-single-writer-cortex-design.md` for the structural fix
(make the LLM dream the sole cortex writer). Anyone considering enabling the
resolver should note the false-merge risk above.

---

# Neural-blend retrieval eval (`neural_blend_bench.py`) — archived

The F1 eval that drove the v0.5 removal of the neural retrieval blend. Findings
(2026-06-21): pure cosine **beat** the shipped `w=0.6` blend at every scale
(n=73 MRR 0.979 vs 0.934 → n=150 0.936 vs 0.875), MLP-only ranking was ≈ random,
and `cos(M(x), x) ≈ 0.4` (a lossy reconstruction that corrupts clean cosine) —
a regime mismatch, not a tunable bug. Full analysis:
`docs/2026-06-21-neural-memory-investigation.md`.

The harness depends on the (now-removed) band MLP, so it lives on the
**`archive/neural-memory-titans`** branch alongside the neural machinery; it's
not runnable against the v0.5 cosine bands on `master`.

---

# MemCoT retrieval-loop bench (`memcot_bench.py`)

Dev-only harness that asks: **does an iterative retrieval loop unlock multi-hop
recall, and does graph traversal do the real work?** It runs a fixed 9-question
multi-hop corpus through three arms and isolates two attribution deltas —
lift from looping alone (arm B − baseline) and lift from adding graph traversal
(arm A − B).

This is **not** part of the test suite or the shipped package. It informs the
decision of whether a MemCoT retrieval loop (and the graph edge path in
particular) should become part of the default query path.

## Isolation & safety

- Runs against a dedicated **`pseudolife_memory_bench`** database (created if
  missing, seeded fresh on each run). The live bank (`pseudolife_memory`) is
  **never** touched.
- Forces **CPU** (`CUDA_VISIBLE_DEVICES=-1`) for the embedder; no GPU is used.
- Requires **no served LLM** — the loop controller is a deterministic
  `MechanicalController` that expands queries from known entities already in
  the retrieved context. No model endpoint, no network access.
- The corpus is seeded into the bench DB at the start of each run — snippets via the service `store` method, edges via `graph_relate`. There is no randomness: determinism comes from the fixed corpus literals (`CORPUS`/`DISTRACTORS`), so every run is reproducible.

## Arms

| arm | description |
|-----|-------------|
| `baseline` | Single-shot `memory_search` — one query, no loop, no graph. |
| `loop-no-graph` (B) | Iterative loop: re-queries with expanded terms, but expands only via vector search (no graph edges). |
| `loop+graph` (A) | Iterative loop: expansion uses **graph edges** (`memory_graph`) to traverse to related entities before re-querying. |

**Attribution deltas:**
- `lift_from_looping` = arm B − baseline (benefit of re-querying alone)
- `lift_from_graph` = arm A − arm B (additional benefit of graph traversal)

## Running

All commands from the repo root. No LLM endpoint required.

```bash
# run the bench and write evals/results/memcot.json
python evals/memcot_bench.py --run

# print the eval questions (hop-class, question, and gold answer)
python evals/memcot_bench.py --show-corpus

# adjust retrieval width per iteration (default: 5)
python evals/memcot_bench.py --run --top-k 3

# cap the number of loop iterations per query (default: 3)
python evals/memcot_bench.py --run --hop-cap 2
```

Results are written to `evals/results/memcot.json` with keys `baseline`,
`loop_no_graph`, `loop_graph`, `lift_from_looping`, `lift_from_graph`.

## Findings — 2026-06-23

```
arm              overall recall   1-hop   2-hop   3-hop   iters   tok/q   ms/q
baseline              0.333        1.0     0.0     0.0     1.0     59.1     6.0
loop-no-graph (B)     0.444        1.0     0.25    0.0     2.44   113.2    29.7
loop+graph (A)        1.000        1.0     1.0     1.0     3.0    137.4    69.1
```

**Attribution:**
- `lift_from_looping` (B − baseline) = **+0.111** — re-querying alone recovers
  some 2-hop questions but fails entirely on 3-hop.
- `lift_from_graph` (A − B) = **+0.556** — graph traversal is where almost all
  the lift comes from; it is the mechanism that closes 2-hop and 3-hop recall.

**Key findings:**

- **Single-shot retrieval cannot do multi-hop.** It recovers only 1-hop
  questions (recall 1.0) and fails completely on 2-hop and 3-hop (recall 0.0).
- **The graph traversal — not mere re-querying — is what unlocks multi-hop.**
  The lift is heavily concentrated in A − B (+0.556) versus B − baseline
  (+0.111). Looping without graph edges gets partial 2-hop credit but still
  misses 3-hop entirely.
- **No 1-hop regression.** All three arms achieve recall 1.0 on 1-hop
  questions — the loop and graph path introduce no degradation on simple queries.
- **A confidence gate alone cannot trigger the loop.** `gate_would_fire = 0/9`:
  the confidence heuristic never fires on multi-hop questions because
  single-shot returns high-scoring *distractors* confidently. A confidence-only
  signal is insufficient to decide when to loop; structural signals (hop-class
  or explicit entity-link structure) are needed.
- **Cost of arm A:** ≈ 3 iterations / 137 tok / 69 ms per query vs. baseline
  1 iter / 59 tok / 6 ms — roughly 2× tokens and 11× latency for a 3× recall
  gain on multi-hop corpora.
- **1-hop cost reflects the unenforced gate.** Arm A runs the full hop-cap on every question, so even 1-hop lookups cost ~3 iterations — recall is not regressed, but the wasted cost on easy questions is exactly what a real (currently unenforced) gate would suppress.

---

# Relation-extraction benchmark (`relation_extraction_bench.py`)

Dev-only. Answers the Phase-2 question the fact-ladder never did: **how good is
the dream graph-from-text path, per extractor model?** Scores each rung's
`extract_relations` over a hand-labeled corpus (`CORPUS`) — edge precision/
recall/F1 plus four defect-aligned diagnostics:

- `naming_consistency` (↓ to 1.0) — surface-form fragmentation (duplicate nodes)
- `type_violation_rate` (↓) — structural edges that violate `(src_type→dst_type)`
- `related_to_share` (↓) — laziness into the `related-to` catch-all
- `over_extraction_null_edges` / `over_extraction_halluc` (↓) — orphan minting

No DB and no embedder — `extract_relations` is a pure model call.

## Rungs

`floor` (n/a — regex has no relation extraction), `gemma-e2b`, `gemma-e4b`
(swap the `:8081` GGUF, as in the fact ladder), `qwen-27b` (LAN 4090, the
sovereign-local ceiling), and `opus-4.8` (the absolute ceiling, produced
in-session — below).

```bash
PYTHONPATH=. python evals/relation_extraction_bench.py --rung gemma-e2b
PYTHONPATH=. python evals/relation_extraction_bench.py --rung qwen-27b
PYTHONPATH=. python evals/relation_extraction_bench.py --report
```

Each rung writes `results/relations-<rung>.json` (including its raw predicted
triples — the silver labels for any future bespoke-model work).

## The opus-4.8 ceiling rung (in-session, no API key)

Produced by Claude Code subagents on your included usage — a **frozen
reference** (regenerate by repeating these steps; not headlessly re-runnable):

1. `PYTHONPATH=. python evals/relation_extraction_bench.py --emit-prompts`
   → `results/relations_corpus_prompts.json` (each note + the exact `system`
   prompt + registry the headless rungs use).
2. In a Claude Code session, dispatch subagents (Opus 4.8) to run the
   extraction over those prompts and return predicted triples as JSON.
3. Collect into `results/relations-opus-4.8.json`, matching the `--rung` output
   shape: `{"rung":"opus-4.8","status":"ok","predicted":[[["src","rel","dst"],…],…], …score keys…}`.
   Re-score by importing `relation_extraction_bench.score(predicted)` and
   merging its keys, so the file carries the same metrics as the headless rungs.
4. `--report` ranks every rung against the `qwen-27b` and `opus-4.8` ceilings
   (`gap_to_27b`). That gap drives the keep-repair vs retrench(C) vs
   bespoke-model decision (see the design doc).

**Step-C prompt reuse.** The `--emit-prompts` output (system prompt + registry)
is also the shape the deep-dream Step-C workflow reuses when dispatching Opus
subagents over `memory_deep_dream` candidates. Each candidate's
`src_snippets`/`dst_snippets` slot into the same prompt template, so a subagent
trained on the bench corpus transfers directly to the live consolidation run.
