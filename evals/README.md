# Extractor-ladder benchmark (`ladder_sweep.py`)

Dev-only sweep that answers one question: **what is the minimum viable
extraction model** for dream consolidation? It runs the same
knowledge-update corpus through each rung of the extractor ladder — from the
deterministic regex floor up to LAN GPU models — and reports whether each
rung beats naive-RAG on staleness, gold recovery, and token efficiency.

This is **not** part of the test suite or the shipped package. It was built
to make the "should the sidecar be default-on, and at which rung?" decision
(see `docs/specs/2026-06-18-pluggable-llm-extraction-design.md` §4) with data
instead of a guess — decided since: default-on, and the shipped bake has
climbed the ladder to the E4B v3 multi-task fine-tune (claims + dated
events in one adapter). It remains the harness for vetting any future
extractor change.

## Isolation & safety

- Runs against a dedicated **`pseudolife_memory_bench`** database (created if
  missing, truncated before each ingest). The live bank
  (`pseudolife_memory`) is **never** touched.

  > That guarantee is **per harness, not page-wide**. Most harnesses here use
  > the bench DB or no DB at all; `capture_metrics.py` reads the live bank
  > read-only; and `apply_entity_kinds.py` is the one harness in `evals/` that
  > **writes** the live bank. See "Entity-kind classification" at the end of
  > this page.

- Forces **CPU** (`CUDA_VISIBLE_DEVICES=-1`) for the embedder so the host GPU
  is left alone. The LLM rungs run wherever their endpoint runs (sidecar on
  CPU, LAN models on their own GPUs).
- Sets `protect_provenance=False` on the bench service so the measurement is
  pure *extraction quality*, not the cortex contender-parking policy.
- Unreachable LLM rungs are skipped and recorded as `status: "unreachable"`.

## Rungs

`LADDER_ORDER` (`ladder_sweep.py`) is the sweep, in rung order — 14 rungs.
`--list` prints the same set with live reachability; the table here is the
authoritative copy only until the code changes, so read the code if they
disagree.

| rung             | extractor                                    | endpoint                     |
|------------------|----------------------------------------------|------------------------------|
| `naive-rag`      | none — top-k vector search baseline           | —                            |
| `floor`          | deterministic regex (`RegexExtractor`)        | — (in-process)               |
| `gemma-e2b`      | Gemma 4 E2B (Q4) CPU sidecar                  | `http://127.0.0.1:8081/v1`   |
| `gemma-e4b`      | Gemma 4 E4B (Q4) CPU sidecar                  | `http://127.0.0.1:8081/v1`   |
| `qwen3.5-4b`     | Qwen3.5-4B (sidecar-upgrade candidate)        | `http://127.0.0.1:8081/v1`   |
| `granite-h-tiny` | Granite 4.0-H-Tiny 7B-A1B (candidate)         | `http://127.0.0.1:8081/v1`   |
| `lfm2-8b-a1b`    | LFM2-8B-A1B (candidate)                       | `http://127.0.0.1:8081/v1`   |
| `ornith-9b`      | Ornith-1.0-9B (candidate)                     | `http://127.0.0.1:8081/v1`   |
| `diffusiongemma` | DiffusionGemma 26B-A4B (candidate)            | `http://127.0.0.1:8082/v1` (via `evals/dg_shim.py` — no llama-server support for diffusion archs) |
| `gemma4-26b-qat` | Gemma 4 26B-A4B QAT-Q4_0 (candidate)          | `http://127.0.0.1:8081/v1`   |
| `gemma-e4b-qat`  | Gemma 4 E4B QAT UD-Q4_K_XL (sidecar-swap candidate) | `http://127.0.0.1:8081/v1` |
| `e4b-ft`         | **E4B QLoRA extractor fine-tune Q4_K_M — the shipped default** | `http://127.0.0.1:8081/v1` |
| `qwen-a3b`       | Qwen3.6-35B-A3B (homelab 5800X3D)             | `$PSEUDOLIFE_BENCH_A3B_URL` (default `http://127.0.0.1:1236/v1`) |
| `qwen-27b`       | Qwen3.8-27B (4090; migrated 2026-08-17, previously Qwen3.6-27B) | `$PSEUDOLIFE_BENCH_QWEN_URL` (default `http://127.0.0.1:1234/v1`) |

Five further rungs are **registered but deliberately outside `LADDER_ORDER`**,
so the default sweep is sovereign-only. They are runnable — `--rung sonnet-5`
etc. — and are ceiling probes, not candidates:

| rung       | extractor                                        | endpoint                   |
|------------|--------------------------------------------------|----------------------------|
| `sonnet-5` | Claude Sonnet 5 (Max-plan CLI shim)               | `http://127.0.0.1:8082/v1` |
| `opus-5`   | Claude Opus 5 (Max-plan CLI shim)                 | `http://127.0.0.1:8083/v1` |
| `fable-5`  | Claude Fable 5 (Max-plan CLI shim)                | `http://127.0.0.1:8084/v1` |
| `terra`    | GPT-5.6 Terra (ChatGPT-plan Codex CLI shim)       | `http://127.0.0.1:8086/v1` |
| `luna`     | GPT-5.6 Luna (same shim, per-request override)    | `http://127.0.0.1:8086/v1` |

The Claude three are served by `evals/claude_shim.py` (shells out to the
`claude` CLI) and the GPT two by `evals/codex_shim.py` (shells out to
`codex exec`; `luna` names its model per request, so one shim launch
serves both) — the only rungs that leave the machine. See "Everything
runs locally" under the LongMemEval bench below for the same caveat.

`terra` and `luna` were first measured 2026-09-01 (single runs, ChatGPT
free tier, 3 batched extraction calls each): both score
gold_recoverable 1.0 / stale_leak 0.0, matching the Claude ceiling rungs,
at 13.1 tokens/query (`terra`, artifact `results/terra.json`) and
14.6 tokens/query (`luna`, artifact `results/luna.json`) — inside the
≤60%-of-naive gate but roughly 10× the Claude rungs' 1.4: both write
wordier slot values. Reproducibility caveat: neither shim pins reasoning
effort — the Codex shim inherits the host's `~/.codex/config.toml`
(`model_reasoning_effort = "high"` for these runs) and the Claude shim
the `claude` CLI's per-model default — so cross-machine reruns may
measure a different effort setting.

Every `:8081` rung shares that **one** endpoint: the operator swaps the served
GGUF between runs (see below). Run one, then the next.

## Prerequisites

The benchmark talks to a **host-published** llama.cpp on `127.0.0.1:8081`.
Note this is *separate* from the default-on compose sidecar
(`pseudolife-mcp-extractor`), which is internal-only (`expose:`, not `ports:`)
and reachable only by the daemon on the compose network.

**Gemma E2B** — bake the E2B image (the shipped default is now the E4B v3
fine-tune, so E2B needs an explicit `MODEL_URL`), then serve it:

```bash
docker build -f ops/Dockerfile.extractor -t pseudolife-extractor:gemma4-e2b \
  --build-arg MODEL_URL=https://huggingface.co/unsloth/gemma-4-E2B-it-qat-GGUF/resolve/main/gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf ops
docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 \
  pseudolife-extractor:gemma4-e2b
```

**Gemma E4B** — stop the E2B container, then serve the E4B GGUF on the same
port. The ladder's `gemma-e4b` rung is the QAT *base* model (the shipped
default image bakes the v3 *fine-tune*, a different artifact — mount or bake
the base explicitly for a like-for-like rung):

```bash
docker build -f ops/Dockerfile.extractor -t pseudolife-extractor:gemma4-e4b-base \
  --build-arg MODEL_URL=https://huggingface.co/unsloth/gemma-4-E4B-it-qat-GGUF/resolve/main/gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf ops
docker rm -f pseudolife-mcp-extractor-bench
docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 \
  pseudolife-extractor:gemma4-e4b-base
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

**Never hand-roll the `:1234` Qwen-27B server.** Dot-source `evals/qwen_server.ps1`
and let it pick the config:

```powershell
. .\evals\qwen_server.ps1
if (-not (Start-Qwen))       { throw "server did not come up" }   # reproducible
if (-not (Start-Qwen -Fast)) { throw "server did not come up" }   # throughput only
```

The default is the stock `llama-server` with `--cache-type-k/v q8_0`, which is
bit-reproducible. `-Fast` now launches the mainline embedded-MTP build
(`run-server-qwen38.bat`) — measured byte-deterministic and verdict-lossless
on b10488 (2026-08-19), a 2.3× extraction-decode speedup. The retired
TurboQuant fork (whose fused `tbq4_0` KV flipped ~7% of judged verdicts) is
the reason the reproducible/q8_0 rule exists; judged runs still use the
default config. Both configs bind `:1234`, so "something answered the probe"
is not proof the right one is running; the helper checks which config is up,
refuses a foreign server, and replaces its own.

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

**Never overwrite a canonical result file — tag the rerun and promote it
deliberately.** `resolve_out_path` enforces this: an untagged run may only
*create* `results/<rung>.json`, never replace one, and it refuses **before**
the (hours-long) run rather than after. A rerun goes to a sibling:

```bash
PYTHONPATH=. python evals/ladder_sweep.py --rung gemma-e2b --out-tag 2026-07-29-recheck
# inspect, then promote by copying over the canonical file if it should win
```

This guard is not decoration. A 2026-07-21 rerun silently rewrote
`results/sonnet-5.json` in place while also writing its own tagged file, and
an earlier untagged rerun overwrote five of the six 2026-06-18 ladder
artifacts — which is why the dated table further down no longer reproduces
that sweep (see "Findings — 2026-06-18 sweep").

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
{0.0, 0.5, 0.65, 0.70, 0.75, 0.80}` and reports, per cell:

- **`abstain_recall_unanswerable`** ↑ — fraction of never-stated probes that
  correctly return `low_confidence=True`.
- **`false_abstain_answerable`** ↓ — fraction of answerable questions wrongly
  flagged low-confidence.

> **The floor values are stale as of the schema-v25 backbone swap (2026-07-28).**
> They were chosen on 2026-06-18 to bracket MiniLM's measured score
> distribution on this corpus (answerable max-scores 0.75–0.98, unanswerable
> 0.38–0.78; floors below ~0.5 never fired). The v25 swap to
> Qwen3-Embedding-0.6B did not merely rescale that distribution: `encode_query`
> now prepends an instruction prefix, so these thresholds gate a
> *prefixed-query-to-document* cosine — an asymmetric quantity the old numbers
> never measured. Re-measure the distribution before reading anything into a
> specific floor.

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
supersession spine is built for. `--types` (comma list or `all`; default
`knowledge-update`, with byte-identical artifact names) extends a run to
the other five LongMemEval question types — 422 more questions for
statistical power and LME-500 comparability. Non-KU rows are graded by a
generic judge variant that drops only the KU-specific update clause;
extended runs get a type-slug artifact prefix and a per-type summary
breakdown.

**Locality, precisely.** The default configuration runs entirely **locally** —
extractor, answerer and judge are all served on this host or the LAN, and
nothing leaves the machine. The exception is opt-in and explicit: the
`sonnet-5` / `opus-5` / `fable-5` extractor rungs are cloud ceiling probes.
They are served by `evals/claude_shim.py`, which shells out to the `claude`
CLI, so **selecting one of those rungs sends the corpus turns to Anthropic**.
They are never selected by default (they sit outside `LADDER_ORDER` and are
not the default `--extractor`); you have to ask for them by name. The
answerer and judge remain local in every configuration.

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
| `cortex` | top-24 canonical facts at `min_score` 0.2 (`CORTEX_TOP_K` / `CORTEX_MIN_SCORE`), each with its supersession chain (`svc.history`) appended | the fact spine alone |
| `hybrid` | facts + top-3 raw turns | the product posture |

A fourth line, `cascade`, is **derived** from the judged `cortex` and `rag`
arms (no extra answer calls, never persisted per-row): the cortex answer is
served when that arm commits, with fallback to the rag answer when it says
"I don't know". Summaries (`--report`), `replicate.py agg`, and
`replicate.py compare --arm cascade` all report it, including retroactively
on old JSONLs. Motivation: on the 2026-07-30 `ceiling-e2e` run the cortex
arm's commit precision was 46/46, making the commit signal a strong router —
cascade ~~0.936~~ vs rag 0.859 at ~57% of the tokens (out-of-sample check on the
five `_s` Phase-A replicates: cascade 0.428±0.023 vs hybrid 0.367±0.015 vs
rag 0.321±0.027, commit precision 0.76±0.05).

> **The 0.936 is retired as a published claim (2026-08-25, #188).** The
> router's input is the *answerer's* abstention behaviour, so it does not
> transfer across bench instruments. Re-running the same 78 questions on the
> Qwen3.8 stack (`ceiling-v38`, n=3, std 0.0000) gives cascade **0.846**
> against an unchanged naive-RAG control of 0.859: the cortex arm abstains
> 22/78 instead of 32/78 and its commit precision drops from 46/46 to 0.839,
> so nine wrong answers are served where RAG would have rescued them. The
> derived metric stays in the harness — it is a real serving policy and
> worth measuring — but any cascade number must name the answerer it was
> measured with. See
> [the benchmarks guide](../docs/guide/benchmarks.md#the-knowledge-update-slice-78-of-the-500).

Model roles are split so extraction quality is the **only** variable:

- **Extractor** (varies): `gemma-e2b` (the smallest ladder-verified sidecar
  bake — the shipped default is now the E4B v3 fine-tune — GPU-served for
  bench speed, ladder-verified identical output at temperature 0) = the
  **floor**; `qwen-27b` = the local **ceiling**.
- **Answerer + judge** (constant): Qwen3.8-27B for every run since the
  2026-08-17 migration (published pre-migration tables were judged by
  Qwen3.6-27B and say so), LongMemEval's LLM-as-judge protocol. All calls
  request `temperature: 0`.

Serving config: Qwen3.8-27B **Unsloth UD-Q4_K_XL** (~4.5bpw) on the **stock**
`llama-server` with `--cache-type-k/v q8_0`, started via `Start-Qwen` from
`evals/qwen_server.ps1`. That pairing is the reproducible one — byte-identical
inputs give byte-identical outputs. Do **not** serve a judged run from a
non-reproducible serving config: the retired TurboQuant fork's 4.25-bit
(`tbq4_0`) fused-attention KV flipped ~7% of verdicts (see "Variance and
replication" below), which is why `Start-Qwen` checks and replaces whatever
is bound to `:1234`. The weight quantization trades some fidelity for
fitting 24GB — treat the ceiling as "27B-class local", not "27B at BF16".

Ingestion mirrors the product cadence: turns are stored session-by-session
in chronological order and the dream consolidates after each session.
Results are per-question JSONL (append-only, atomic rewrite) so any run can
be killed and resumed. `--phase extract` / `--phase answer` split the work
so only one model needs the GPU at a time; `--tag` namespaces experiment
runs. Every extract run also dumps the question's full fact bank (values +
history chains) to `results/banks/` and stamps rows with
`answer_in_current_fact` / `answer_in_history_only`, so a failure is
attributable to never-extracted vs overwritten vs not-retrieved.

Start the answerer/judge server through the helper first — every command below
is judged, so it needs the reproducible config and must not be hand-rolled:

```powershell
. .\evals\qwen_server.ps1
if (-not (Start-Qwen)) { throw "bench server did not come up" }
```

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

> **SUPERSEDED — the headline oracle hybrid 0.705 in this table is retired,
> twice over.** (1) It is **unreplicable**: the run predates per-question
> context persistence, so its bank cannot be rebuilt. Its replicable sibling
> `ceiling-v2` puts the qwen-27b class at hybrid **0.710 ± 0.019** on the
> same TurboQuant stack (itself re-based 2026-07-29 on the reproducible
> server to 0.7308 — `ceiling-v25`, the figure the front-door tables now
> publish) — read
> 0.705 as that band's edge, not as a measurement (see the 2026-07-19
> addendum). (2) Every number in this table was measured on the
> **nondeterministic TurboQuant server**, whose fused `tbq4_0` KV flips ~7% of
> verdicts; values measured there are not comparable to values measured on the
> reproducible q8_0 config, and the spread is not centred on the deterministic
> value (see "Variance and replication"). Kept because the *shape* of the
> result — hybrid > rag > cortex, and the flat rag control across extractors —
> reproduced under replication. Do not quote the cells.

Accuracy / context-tokens-per-question, 78 questions, judge = local
Qwen3.6-27B:

| dataset | extractor | rag (control) | cortex | hybrid |
|---|---|---|---|---|
| oracle | qwen-27b (ceiling) | 0.615 / 1638 | 0.564 / **59** | ~~**0.705**~~ / 979 (retired — see above) |
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
  bigger local model — see "Upgrading the extractor" in
  `docs/guide/dreaming.md`.)
- **Supersession chains matter**: surfacing each fact's earlier values
  lifted the whole board vs current-value-only contexts (hybrid 0.590 →
  0.705 on oracle) — knowledge-update questions ask about the original
  value as often as the current one. The pre-history baseline is kept at
  `results/longmemeval-ku-oracle.v1-nohistory.jsonl`.
- **Abstention holds**: 6/6 abstention variants correct in the hybrid arm
  on both datasets.
- **Known `_s` gap — the starvation half is FIXED (2026-07-06), the churn half
  is still open.** As measured here, at `min_score 0.3` / `top_k 8`, 45/78
  haystack questions retrieved **zero** cortex facts: terse canonical fact
  strings score low cosine against verbose questions. `retrieval_sweep.py`
  replayed the dumped banks offline and `rebuild_contexts.py` re-judged them,
  and commit `6136d359` landed the fix — `top_k 8 → 24`, `min_score 0.3 →
  0.2` (now `CORTEX_TOP_K` / `CORTEX_MIN_SCORE`), taking starvation **60% →
  28%** at unchanged judged accuracy. 0.1 was tried and rejected: it serves
  more gold facts but the extra weak ones dilute the context and the answerer
  abstains on questions it previously got right. The **supersession churn**
  (~10× oracle's, 970–1245 events) was *not* addressed and remains open.

**Comparability caveat:** published LongMemEval numbers (TiMem 76.9%,
EverMemOS 83% overall) use GPT-4o-class answerers/judges and all 500
questions; these runs are all-local (27B answerer, 4-bit quant) on the
78-question knowledge-update slice. Compare arms and extractors *within*
this table, not against leaderboards.

## Variance and replication

> **Root-caused 2026-07-27: most of the "judge noise" below was a server
> bug, and it is fixed.** The spread came from the TurboQuant fork's fused
> TBQ4_0 flash-attention KV cache, which is not bit-reproducible — identical
> inputs flip ~7 % of verdicts. It is not MTP and not the prompt cache: both
> were tested off and the spread remained (`judge_determinism_check.py`,
> `results/judge-determinism-check.json`). The stock `llama-server` with
> `--cache-type-k/v q8_0` reproduces exactly. `evals/qwen_server.ps1` now
> serves that config by default to every harness; pass `-Fast` only for
> throughput work whose output is never judged.
>
> Measured after the switch, the gate slice at n=7 replicates spanning a
> server restart: **std 0.0000 on all three arms**
> (`regression_gate-2026-07-27-establish-q8-n7-crossrestart.agg.json` — rag
> 0.6282, cortex 0.7051, hybrid 0.7692, identical on every replicate). The
> cross-restart part matters: it rules out a warm process holding the result
> steady.
>
> Historical means on this page were measured on the noisy server and are
> **not comparable** to values measured on the reproducible one — the spread
> is not centred on the deterministic value, and it shifts differently per
> arm (on the gate slice: rag deterministic 0.6282 vs a noisy range topping
> out at 0.6154; hybrid deterministic 0.7692 vs a noisy *minimum* of 0.7692).
> Re-measure rather than reinterpret.

Historically, single runs of this bench looked irreducibly noisy: three runs
of the identical sonnet-5-v1 config (same bank, byte-identical contexts,
temperature 0) scored cortex 0.808 / 0.731 / 0.782 — a ~7.7 pp spread
attributed at the time to the answerer/judge. MemDelta (arXiv 2606.29914)
documents the same failure across the field: identical aggregate scores can
disagree on 16–66 % of items, and single-run memory-bench comparisons
routinely measure judge noise. The lesson generalises even though our
instance had a fixable cause — before averaging noise away, check whether
the serving stack is reproducible at all, because a control arm makes that
free to measure.

Convention, updated:

- **Judge/answerer noise is now zero** on the reproducible config, so
  replicates no longer estimate it. Keep **2** as a drift canary:
  `replicate.py` prints a nondeterminism WARNING if replicates of
  byte-identical contexts ever disagree, which means the run was served by
  the fast fork.
- **Question-sampling variance does not go away** and is the real limit on
  small effects. A deterministic judge makes a measured difference *real*,
  not *significant*: config-vs-config claims still need the paired
  permutation test (`replicate.py compare`) or the paired McNemar test
  (`analyze_extractor_comparison.py`), and an adequate question count.
- **Carry a control arm whose input is identical across the configs being
  compared** — `rag` contexts are extractor-independent, so any disagreement
  there is measurement noise and bounds what the other arms can claim.

Findings tables in this file are point-in-time snapshots — where a
`.agg.json` exists next to a results file, the aggregate is authoritative.

Workflow (contexts are persisted at extract time, so replicates never
re-extract):

    python evals/replicate.py spawn --extractor e4b-ft --tag arm1 -n 4
    python evals/replicate.py run   --extractor e4b-ft --tag arm1
    #  ^ `-n` belongs to `spawn` only — `spawn` creates the stripped replicate
    #    files, `run` answers whatever is pending. `run … -n 5` exits 2.
    python evals/replicate.py agg   --extractor e4b-ft --tag arm1
    python evals/replicate.py compare --extractor e4b-ft --tag arm1 \
        --b-tag arm1-baseline --arm cortex

`evals/regression_gate.ps1` runs a pinned, replicated slice against the
committed baseline (`evals/results/regression_gate.baseline.json`) —
see the script header for scope and the `-Establish` flow.

> **SUPERSEDED 2026-07-27.** Everything in this block was measured on the
> nondeterministic turboq server. The baseline was re-established on the
> reproducible q8_0 config at commit `1f0f13a`: **rag 0.6282, cortex 0.7051,
> hybrid 0.7692, std 0.0000 on every arm, margin 0.03 (the floor) on every
> arm** — and the gate default went 10 → 2 replicates (~32 min → ~8 min)
> because replicates no longer estimate anything, they only canary drift.
> Note the margin *narrowed* (cortex 0.0637 → 0.03) while getting cheaper:
> the noise was buying nothing but insensitivity. The analysis below is kept
> because its reasoning about margins, false-fail rates and stale baselines
> is sound and reusable — but do not treat its numbers as current, and note
> its central diagnosis (that the spread was inherent judge noise) was wrong.
>
> **Re-established 2026-07-26 at 10 replicates (commit `959ecad`), and the
> gate's default `-Replicates` raised 3 → 10.** The previous baseline was
> stale and the gate under-powered; both are fixed, and the history is kept
> here because the failure mode is reusable.
>
> The old baseline claimed cortex **0.7051**. Across **18 honest replicates
> of the identical slice** (two independent establishes, n=8 then n=10):
> min 0.6154, mean 0.6674, max 0.7179 — **13 of 18 fall below the old
> baseline and only 1 exceeds it.** It had frozen near the top of the
> range, so every honest run afterwards looked like a regression. It failed
> on clean `origin/master` (cortex 0.6709 ± 0.0370) as readily as on the
> #38–#44 stack (0.6581 ± 0.0196) — a difference between them of
> **0.0128**, well inside the noise. Both pass the new baseline on all
> three arms.
>
> The two establishes agree to within **0.004 on every arm** (cortex 0.6651
> at n=8, 0.6692 at n=10), which is the evidence that the estimate is now
> stable rather than another lucky draw.
>
> The noise floor is directly measurable here, because the gate copies the
> **rag** arm's context verbatim: identical inputs, and the two runs differ
> by **0.021**. Any cortex delta below that is judge variance, not signal.
>
> Two independent reasons the 0.7051 baseline should not be trusted:
> - Its recorded `std` is **exactly 0.0** on two arms — three LLM-judge
>   replicates returning identical accuracy. Today's runs show 0.007–0.037.
>   The baseline was established 2026-07-18, before `LLAMA_ARG_CACHE_RAM=0`
>   turned the server's prompt cache off; caching plausibly suppressed the
>   replicate variance that has now reappeared.
> - It disagrees with this file's own more careful 5-replicate measurement
>   of the same slice below — **cortex 0.682 ± 0.017**. Both of today's
>   3-replicate runs sit nearer that figure than the baseline does.
>
> The margin was also too tight for the spread it had to survive: 0.03
> against a cortex std of 0.037 at n=3 is ~1.4 standard errors, so the gate
> failed a meaningful fraction of runs with no change at all.
>
> **What the new baseline is**, at 10 replicates:
>
> | arm | mean | std | margin |
> |---|---|---|---|
> | rag | 0.5757 | 0.0230 | 0.0460 |
> | cortex | 0.6692 | 0.0319 | 0.0637 |
> | hybrid | 0.7808 | 0.0165 | 0.0330 |
>
> The margin is not a constant: `make_baseline` uses
> `max(0.03, 2 x std)`, so it tracks the measured spread. The old 0.03 was
> the *floor* showing through, because that run's std was 0.0 — a baseline
> with no variance silently disables the gate's own calibration.
>
> **Cost of the choice.** The gate re-runs its replicate count on every
> invocation: ~3–3.75 min per replicate, so 10 replicates is **~32–37 min**
> per run (measured 28 min for 8, 37 for 10 — the spread is host load).
> Fewer replicates is cheaper but false-fails more: at n=3 the margin was
> ~1.2 standard errors of the difference, roughly a 1-in-5 false-fail rate.
>
> A ~0.064 cortex margin only catches regressions larger than about six
> points. That is the honest consequence of a judge this noisy, and the
> lever for catching smaller ones is **more replicates, not a narrower
> margin** — narrowing it is how the gate started lying. Note the margin
> barely moves with N (it is `2 × std`, and std estimates a population
> spread that does not shrink); what more replicates buy is a better
> estimate of the *mean* on both sides of the comparison.
>
> Evidence: `regression_gate-2026-07-26-establish-n10.agg.json` (the
> current baseline), `-establish-n8.agg.json` (the independent replication
> that agrees within 0.004), `-master-control.agg.json` (clean master under
> the old baseline) and `-stack-38-44.agg.json`.

### Findings — 2026-07-18 (first replicated comparison)

5 replicates per config (`overnight_replicates.ps1`), paired permutation
test over the 78 questions:

| config | rag | cortex | hybrid |
|---|---|---|---|
| `e4b-ft` arm1 (shipped default) | 0.574 ± 0.006 | 0.682 ± 0.017 | 0.762 ± 0.027 |
| `e4b-ft` arm1-baseline | 0.585 ± 0.015 | 0.603 ± 0.013 | 0.749 ± 0.015 |
| `qwen-27b` w0 | 0.579 ± 0.019 | 0.536 ± 0.025 | 0.695 ± 0.017 |

- **Arm-1 verdict**: cortex delta +0.0795 at paired **p = 0.17** (pre-registered
  threshold 0.05) — *not confirmed*; hybrid delta +0.0128 at p = 0.83. The
  original single-run "+0.102" deploy evidence was inflated by judge noise
  and question-level heterogeneity (the fine-tune fixes some questions,
  regresses others). The shipped default is flagged for revisit, not
  reverted — the point estimate is still positive and nothing here shows
  the fine-tune *hurting*.

  Artifacts, all `arm1` vs `arm1-baseline`, 78 questions, 10 000 permutations,
  seed 0 (`replicate.py compare`):

  | arm | Δ | p | artifact |
  |---|---|---|---|
  | cortex | +0.0795 | 0.16958 | `longmemeval-ku-oracle-e4b-ft-arm1-vs-baseline-cortex.compare.json` |
  | hybrid | +0.0128 | 0.82862 | `…-vs-baseline-hybrid.compare.json` |
  | **rag (control)** | **−0.0103** | **0.40586** | `…-vs-baseline-rag.compare.json` |

  The `rag` row is the **measurement floor**, and it is why the cortex result
  is read as "not confirmed" rather than "small but real". Its contexts are
  built from raw turns and never touch the extractor, so both sides of that
  comparison are byte-identical input: the −0.0103 it nonetheless shows is
  pure measurement noise. A claimed effect is only interesting once it clears
  that spread — and at the time these were measured, the noise came from the
  nondeterministic server (see "Variance and replication"), so the floor was
  wide. Re-run on the reproducible config before revisiting the verdict.
- **The untagged `qwen-27b` run (README's 0.705 hybrid) is unreplicable** —
  it predates per-question context persistence. Its nearest replicable
  sibling (`w0`, same knobs, different bank) puts the qwen-27b class at
  hybrid 0.695 ± 0.017; read 0.705 as that band's upper edge.
- Replicating is cheap: each 5-replicate config took ~17 minutes of
  answer-phase GPU time. There is no longer a reason to publish single-run
  comparisons.

**2026-07-19 addendum** (overnight replication sweep):

| config | rag | cortex | hybrid |
|---|---|---|---|
| `qwen-27b` ceiling-v2 (fresh oracle bank, context-persisted) | 0.567 ± 0.017 | 0.559 ± 0.029 | 0.710 ± 0.019 |
| `qwen-27b` `_s` haystack | 0.321 ± 0.027 | 0.195 ± 0.011 | 0.367 ± 0.015 |

- The historical single-run headline (oracle hybrid 0.705, unreplicable
  bank) is retired: ceiling-v2 replicates it inside the band and is fully
  reproducible (`--tag ceiling-v2` banks + contexts persisted).
- The `_s` (realistic full-haystack) single-run 0.372 also holds under
  replication (0.367 ± 0.015). The tight low cortex band (0.195 ± 0.011)
  is the starvation signature — the known `_s` weak spot.
- Cross-model: the shipped E4B v2 fine-tune's hybrid (0.762 ± 0.027)
  beats the 27B ceiling's (0.710 ± 0.019) on this subset — a same-stack
  (TurboQuant) comparison, valid only within that stack.
- **2026-07-30:** the ceiling-v2 row above is superseded as a published
  number — `ceiling-v25` re-judged the same contexts on the reproducible
  q8_0 server (rag 0.6282 / cortex 0.5897 / hybrid 0.7308; 3
  byte-identical replicates, std 0.0000) and is what the README and guide
  tables now show. The v2 figures remain the same-stack baseline for
  everything else measured on the TurboQuant fork.

---

# BEAM long-term-memory benchmark (`beam_adapter.py`)

The second external benchmark, and the one that keeps LongMemEval honest:
**BEAM** ([arXiv 2510.27246](https://arxiv.org/abs/2510.27246), ICLR 2026;
MIT) probes ten memory *abilities* — abstention, contradiction resolution,
event ordering, information extraction, instruction following, knowledge
update, multi-session reasoning, preference following, summarization,
temporal reasoning — over procedurally generated conversations at 100K to
10M tokens, scored by an LLM judge against per-question rubric items rather
than a single gold string. Only the **100K tier** (20 chats, 400 questions)
is measured here.

The BEAM checkout (data + prompts) stays **outside** this repo; the
adapter extracts BEAM's own `unified_llm_judge_base_prompt` from the
harness clone with `ast` at runtime and never vendors it. Each chat is
ingested turn by turn into a fresh bench service, dreaming after every
BEAM batch (the production cadence), and each question is answered through
the same `rag` / `cortex` / `hybrid` arms as LongMemEval — `rag` again
doubling as the extraction-independent control.

```bash
PYTHONPATH=. python evals/beam_adapter.py --beam-root <path-to-BEAM> \
    --tier 100K --extractor qwen-27b --out-tag beam100k-qwen
# re-judge an existing run's recorded answers with a frontier judge
# (retrieval and answering are NOT re-run, so any movement is judge effect)
PYTHONPATH=. python evals/beam_rejudge.py --in evals/results/<run>.jsonl \
    --beam-root <path-to-BEAM> --tag opus5
# reader/volume sweep: budget arms answered by a frontier CLI model, no GPU
PYTHONPATH=. python evals/beam_reader_sweep.py --beam-root <path-to-BEAM> \
    --tag opus-sweep --phase serve      # then --phase answer
```

Scoring note recorded in every artifact: BEAM's paper defines a
1.0/0.5/0.0 per-item scale, but the reference code `int()`-floors the
judge's score, turning 0.5 into 0. Both readings are recorded (`score` =
paper-faithful float, `score_intfaithful` = code-faithful).

**These numbers are not comparable to published BEAM leaderboard results**
(Cognee, Mem0, Hindsight): those are GPT-judged, and the runs below are
judged locally or by an Opus-class CLI judge. Cognee's 0.79 is also a
20-question single-conversation protocol. Compare within a row.

## Findings — 2026-08-03 to 2026-08-24

| finding | evidence |
|---|---|
| **The fact spine's one decisive win is abstention.** On BEAM's abstention questions the cortex arm scores **0.950** against naive RAG's 0.775 — a small curated fact context refuses where a raw-turn context confabulates. The number is **identical under two independent judges** (local Qwen3.8 and an Opus-class CLI judge over the same recorded answers), which is what makes it the most transferable claim the memory has. | `beam-100K-qwen-27b-beam100k-qwen38.summary.json`, `beam-100K-qwen-27b-beam100k-qwen38.rejudge-opus5.summary.json` |
| **Budget-matched, the hybrid arm ties the raw-turn control — it does not lose.** The hybrid arm (facts + turns) historically served 3 raw turns against rag's 6; at a matched 16/16 budget with the Phase-1 fixes, rag 0.6425 vs hybrid 0.6226 (−0.020 ± 0.029, a wash). Earlier "hybrid loses" readings were the halved turn window. | `beam-100K-qwen-27b-p1-b16.summary.json`, `beam-reader-volume-grid-verdict.json` |
| **Judge transfer on BEAM is small — measured, not assumed.** Re-judging 400 identical responses with an Opus-class judge moved rag −0.002, cortex +0.007, hybrid −0.016, against a same-judge stability floor of mean \|item delta\| 0.073. Deltas below that floor are not findings. | `beam-100K-qwen-27b-beam100k-qwen38.rejudge-opus5.summary.json` |
| **Most of the gap to published leaderboard numbers is the reading stack, not the memory layer.** Context volume dominates: widening naive-RAG context from 6 to 48 turns (roughly the published systems' budget) is +0.186 ± 0.041 and takes a local 27B reader to 0.665 full-tier, while swapping in a frontier reader over byte-identical contexts adds only ~+0.04 (not significant at 48 turns). | `beam-readersweep-verdict.json`, `beam-reader-volume-grid-verdict.json` |
| **Three weaknesses survive any reading stack**: summarization (0.38 → 0.47 across the whole budget sweep — a whole-chat rubric needs a mid-density layer, not more turns), event ordering (0.21 → 0.52, still the weakest type), and abstention *degrading* with volume (0.62 → 0.50 — wider context invites confabulation, which is exactly what the small-context fact channel avoids). | `beam-readersweep-verdict.json` |
| **Cross-bench agreement.** BEAM's per-ability shape matches the LongMemEval 500-question per-type shape: strong on canonical-fact abilities, weak wherever an answer must be aggregated across sessions or ordered in time. Two independent benchmarks say the gap is cross-session aggregation, not fact fidelity. | `beam-100k-verdict.json` (`cross_bench_convergence`) |

Caveats that bound all of the above: single replicate per configuration
(no significance claims except where a verdict file states a CI); the
reader sweep is 116 of 400 rows, chats 1–7, so per-type rows are n=10–12
and directional only; a CLI answerer/judge is not bit-reproducible; and
only the 100K tier has been run — 500K/1M/10M are unmeasured.

Bank dumps and served contexts persist per run under
`evals/results/banks/beam-<tier>-<extractor>-<tag>/` (gitignored), so a
serving-knob rerun or a re-judge recomposes from persisted state instead of
re-paying the ~5h ingest/extraction phase.

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

The prime optimisation target is the **shipped sidecar** — whatever
`ops/Dockerfile.extractor` bakes, which since 2026-07-06 is an **E4B-class**
model and currently the Gemma 4 E4B QLoRA fine-tune (`e4b-ft`), not the 2B the
findings below were measured on. **Qwen3.6-27B** (4090) is the quality CEILING,
not the target. The `_LESSON_SYSTEM_PROMPT` here is tuned, then ported to
`memory/dream.py`.

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
  chatter. (The default sidecar has since moved to E4B-class — 2026-07-06,
  now the E4B v3 multi-task fine-tune — which narrows this gap.)
- Gemma already handles the **merged fail→success** case and **clustering** well
  — better than the v1 live smoke suggested (that smoke's inversion was not
  systematic at temperature 0).

## Findings — the ladder sweep (originally 2026-06-18)

> **This table has been reconciled to the COMMITTED artifacts, and they are no
> longer the 2026-06-18 files.** Five of the six canonical `results/*.json`
> were overwritten in place by a later untagged rerun, before
> `resolve_out_path`'s `--out-tag` guard existed — only `qwen-a3b.json` still
> carries its original values. The rows below are therefore *the surviving
> measurements*, not a single dated sweep; they mix run dates and every
> LLM-rung number reflects the post-fix extractor path (see "Reasoning models
> need thinking disabled", below, which was one of the things that changed
> between them). Read them as "what the committed evidence says today". This
> is the failure the `--out-tag` rule in "Running" exists to prevent.

```
rung                           gold↑  stale↓   tok/q↓  extract s   artifact
naive-RAG (baseline)             0.7     0.3     58.3        0.0   naive-rag.json
deterministic floor              0.1     0.1      0.9        0.1   floor.json
Gemma 4 E2B (CPU sidecar)        1.0     0.0      1.4        6.7   gemma-e2b.json
Gemma 4 E4B (CPU sidecar)        1.0     0.0      1.4       68.0   gemma-e4b.json
Qwen3.6-35B-A3B (homelab CPU)    1.0     0.1      2.3       45.8   qwen-a3b.json  (original)
Qwen3.6-27B (4090)               1.0     0.0      1.4        9.0   qwen-27b.json
```

- **All four LLM rungs clear the gate.** Even the smallest CPU sidecar (Gemma 4
  E2B) beats naive-RAG on every axis — gold 1.0, stale 0.0, **~40× fewer tokens
  per query** (1.4 vs 58.3). **Minimum viable = Gemma 4 E2B.** This verdict is
  the one thing the overwrite did not disturb: it held on the original files
  and holds more strongly on these.
- **Quality ceiling = Qwen3.6-27B** — on *quality per second*, not on the
  headline metrics, which the surviving artifacts no longer separate: E2B, E4B
  and 27B all land at gold 1.0 / stale 0.0 / 1.4 tok-q. The original sweep
  distinguished them (27B alone reached stale_leak 0.0, because it was the only
  rung that consistently named the entity the same way across the `initial` and
  `update` turns, so the update *superseded* the stale value; the smaller models
  split initial/update onto sibling slots, superseded=0, leaving one stale value
  retrievable at stale_leak 0.1). **That distinction is now unbacked** — its
  artifacts were the overwritten ones. `qwen-a3b`, the one original file, still
  shows the split-slot signature at stale_leak 0.1.
- **Reasoning models need thinking disabled for extraction.** Before the fix,
  Qwen3.6 spent its whole 4096-token budget on a `<think>` trace and returned
  empty content → silent regex-floor fallback (gold 0.1, 399s). Adding
  `chat_template_kwargs:{enable_thinking:false}` + tolerant JSON parsing (strip
  ```json fences) to `OpenAICompatExtractor` fixed it (homelab 399s→46s; and it
  even sped up + improved Gemma E2B: 58s→17s, gold 0.8→0.9). Those E2B
  before/after figures are from the **original 2026-06-18 run**, whose result
  file was later overwritten; the committed `gemma-e2b.json` now reads 6.7s at
  gold 1.0. Neither number contradicts the other — they are different runs —
  but only the 6.7/1.0 pair has a committed artifact behind it.
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

This is **not** part of the test suite or the shipped package. It's the
harness that validated the loop before promotion: the MemCoT retrieval loop
now ships as the read-only `memory_recall` MCP tool
(`pseudolife_memory/memory/recall.py`), and this bench remains the
measurement rig for tuning it.

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

---

# Capture metrics (`capture_metrics.py`)

Read-only report over the **live** bank measuring the memory loop's beats:
capture coverage, outcome coverage of substantive sessions, per-session
store density, failure+correction share, and the explicit-vs-inferred
outcome mix. Carries the 2026-07-18 pre-auto-outcome baseline in its
docstring and the success criteria for the 2-3-week re-measurement.

    python evals/capture_metrics.py [--json] [--since YYYY-MM-DD]

---

# LongMemEval-V2 pilot (`lme_v2_smoke.py`)

[LongMemEval-V2](https://arxiv.org/abs/2605.12493) swaps chat sessions for
**WorkArena agent trajectories** — what an agent saw and clicked in an
enterprise portal — so it stresses a content class the KU benchmark never
touches: *procedures*. This is a pilot harness, not a production bench: one
category (`procedure`), a small slice, deterministic scoring by the
benchmark's own eval functions plus the same LLM judge as `longmemeval_bench`.

## Pieces

- `lme_v2_adapter.py` — trajectory → turn adapter. Resolves action `bid`s to
  the human-readable labels they clicked (against the pre-action
  accessibility tree), caps page context, and captures **knowledge-article
  body text** as a framed `[article] <title>: <body>` turn, once per
  trajectory. That last part is load-bearing: the gold answers for several
  procedure questions are drawn from protocol articles the agent *read*, not
  from what it then did.
- `lme_v2_smoke.py` — three-arm smoke (rag / cortex / hybrid) with a dream
  per trajectory (one trajectory ≈ one session), a trajectory-mode extraction
  prompt, and a cross-trajectory synthesis pass that clusters procedure claims
  into canonical `typical workflow` facts.
- `lme_v2_check0.py`, `lme_v2_check_fixd.py` — **offline** corpus gates
  (no inference, CPU-only). Run these before spending GPU time: they rebuild
  the corpus and assert the gold-supporting text is actually present.

## Running

```bash
# offline gates first — no model needed
python evals/lme_v2_check_fixd.py

# one question, full 100-trajectory haystack, all retrieval channels on
python evals/lme_v2_smoke.py --limit 1 --max-trajectories 100 \
    --bm25 --rerank --lexical-cortex --out-tag fixe

# re-score an EXISTING run's persisted contexts with a different answer
# prompt — no ingest, no dreams, no GPU-side re-extraction
python evals/lme_v2_smoke.py --reanswer-from fixe \
    --answer-prompt compose --out-tag fixe-compose
```

`--reanswer-from` is the cheap iteration loop: contexts are persisted per
row, so answer-prompt A/Bs cost one answer+judge pass instead of a full
re-ingest. Runs resume from their per-question JSONL cursor, so a crashed
model server costs one question, not the run.

## Findings — 2026-07-20

10 `procedure` questions × 3 replicates, deterministic scorer
(`lme-v2-smoke-slice1*.json`):

| arm | default prompt | composition-aware prompt |
|-----|---------------|--------------------------|
| naive RAG | 0.300 [0.30–0.30] | 0.500 [0.40–0.60] |
| cortex only | 0.167 [0.00–0.30] | 0.233 [0.10–0.30] |
| hybrid | **0.533 [0.50–0.60]** | **0.633 [0.60–0.70]** |

Hybrid beat both single channels in *every* replicate under both prompts.
Treat the absolute numbers as a pilot: 10 questions, one category, no paired
testing.

> **CORRECTED 2026-08-25 (scorer defect #173).** The multiple-choice
> scorer's no-box fallback matched the English article "a" and scored it as
> answer **A**. Re-scored
> (`evals/results/lme-v2-smoke-slice1-rescored-strictmc.agg.json`, written
> by `evals/rescore_strict_mc.py`): naive RAG
> `0.300 [0.30–0.30] | 0.433 [0.40–0.50]`, cortex only
> `0.167 [0.00–0.30] | 0.200 [0.10–0.30]`, hybrid
> `0.500 [0.40–0.60] | 0.533 [0.50–0.60]`. Hybrid still leads on every
> mean, but the "*every* replicate" claim above no longer holds: under the
> composition-aware prompt one of the three replicates is now a tie with
> naive RAG.

Every arm scored **0.000** before five fixes, and the decisive one was
self-inflicted — the trajectory extraction prompt said "extract exactly two
kinds of claim and nothing else", so the extractor correctly discarded the
protocol documents the answers came from. The lesson (**an extraction prompt
that enumerates what to extract makes an obedient model silently drop
everything it doesn't name — no error, no partial result**) was folded back
into the shipped `_SYSTEM_PROMPT` and the Sonnet override prompt.

---

# Band-structure ablation (`band_ablation.py`)

Does the 8-band continuum actually beat **one** cosine table on retrieval
ranking? CPU-only, offline: `replay` re-ingests the KU haystacks without
dreaming and serialises each question's full band state; `rebuild` then
re-ranks the raw-turn selection under two policies (`continuum` — the CMS's
real Pool-1 ranking, band-depth-modulated recency; `flat` — one pool, single
recency term) × two timestamp regimes (`wall` — everything stamped now;
`hist` — realistic aging), emitting four tagged JSONLs ready for the GPU
answer phase.

```bash
python evals/band_ablation.py replay --extractor e4b-ft --src-tag arm1
python evals/band_ablation.py rebuild --extractor e4b-ft --src-tag arm1
# then answer/score each tag with the normal replicate machinery
python evals/replicate.py run --extractor e4b-ft --tag arm1-abl-flat-hist -n 5
```

## Findings — 2026-07-19 (5 replicates, paired permutation, 78 questions)

| arm | Δ continuum − flat (`wall`) | p | Δ (`hist`) | p |
|-----|---------------------------|------|-----------|------|
| naive RAG | −0.067 | 0.10 | **−0.090** | **0.015** |
| cortex only | +0.008 | 0.76 | −0.010 | 0.53 |
| hybrid | −0.023 | 0.24 | +0.018 | 0.47 |

The continuum never beats a flat pool, and under realistic aging it is
*significantly worse* at raw-turn selection. Whatever the banding earns, it
is not retrieval ranking — which left the write side (eviction, capacity,
consolidation cadence) as the remaining defence. It did not hold either.

## Write-side ablation — `--band-preset flat` (2026-07-25)

The ranking ablation holds *ingest* fixed: both arms re-rank the same
survivors, so it is blind to what banding does at write time.
`replay --band-preset flat` re-runs ingest through **one flat band at the
continuum's total capacity** (5,250 = the sum of all eight tiers),
injected via a `config.yaml` the service reads at construction, with the
arms' configs verified identical outside `memory.miras`. Run on the `s`
full-haystack dataset (~488 turns/question), where capacity pressure is
real — the `oracle` corpus stores ~23 turns/question and never evicts,
which makes the write side untestable there.

```bash
python evals/band_ablation.py replay  --dataset s --extractor qwen-27b --src-tag "" --band-preset flat
python evals/band_ablation.py rebuild --dataset s --extractor qwen-27b --src-tag "" --band-preset flat
```

Findings (5 replicates, paired permutation, 78 questions). `iso` holds the
ranking flat on both arms so only the survivor sets differ; `sys` is the
continuum as designed vs flat everything. The cortex arm is definitionally
null (both arms build the same fact block) and is not compared.

| comparison | arm | Δ (`wall`) | p | Δ (`hist`) | p |
|---|---|---|---|---|---|
| write-side isolation | naive RAG | −0.090 | 0.17 | −0.097 | 0.15 |
| write-side isolation | hybrid | **−0.110** | **0.018** | **−0.108** | **0.027** |
| whole system | naive RAG | **−0.274** | **0.0001** | **−0.251** | **0.0001** |
| whole system | hybrid | **−0.141** | **0.0038** | **−0.123** | **0.0153** |

Mechanism, visible without answering at all: the continuum **evicts 31.1%
of everything stored** (the 200-entry `working` band overflows faster than
promotion drains it) while a flat pool of equal total capacity evicts
nothing — see `longmemeval-ku-s-qwen-27b-wabl-survival.json`.

Bounding the claim: since the flat arm never evicts on this corpus, this
measures *partition-forced eviction vs none*, not one eviction policy vs
another; testing the policy would need >5,250 turns/question. What is
established: partitioning a fixed capacity into recency tiers discards
entries an unpartitioned store of the same size keeps, and costs accuracy.

---

# Needle survival (`needle_survival.py`)

The write-side ablation above establishes that the continuum evicts 31.1% of
what it stores. Survival *rate* cannot say whether that costs anything:
discarding 31% of filler is free, discarding the answer evidence is fatal.
LongMemEval marks its evidence turns `has_answer`, so the eviction rate **on
needles** is directly measurable and directly comparable to the base rate.

It is not free. **Needles are evicted at 1.21× the base rate** — 37.5% vs
31.1% — and **58% of questions lose at least one needle**. The mechanism is
structural rather than incidental: eviction and promotion both rank on novelty
(`1 - max cos`), and knowledge-update evidence is by construction a
*restatement* of an attribute already mentioned, hence unsurprising, hence
preferentially destroyed. This is the measurement that justifies the overflow
fix in the CHANGELOG.

Offline and CPU-only. It reads the band dumps written by `band_ablation.py
replay` (gitignored — hundreds of MB of embeddings) and writes a small
**tracked** JSON so the published numbers have committed evidence:

```bash
python evals/needle_survival.py --dataset s --extractor qwen-27b
# -> evals/results/longmemeval-ku-s-qwen-27b-needle-survival.json
```

That artifact (72 questions, 35,117 turns ingested, 144 needles) is pinned by
`tests/test_eval_evidence.py`, which re-derives 37.5 / 31.1 / 58 from it and
fails if the prose and the file diverge.

---

# Embedding-backbone shootout (`embedder_recall.py`)

The eval behind the **schema-v25 backbone swap**. Replacing the bi-encoder is a
schema migration — the pgvector columns were declared `vector(384)` in four
tables, every stored row must be re-embedded, and every committed artifact's
embeddings stop being comparable — which is far too much to spend on a
literature claim. So this measures the thing the swap is supposed to buy, on
our own corpus: **recall@k of the turns LongMemEval marks `has_answer`**,
ranking every haystack turn of a question by cosine to the question text. Pure
retrieval; no reader, no judge, no DB. Runs on GPU when torch sees one — recall
is device-independent, so bench on GPU and deploy on CPU.

Candidates carry their **card-verbatim** query/passage prefixes. Instruction-
tuned embedders swing on exact wording, so an arm run with the wrong (or no)
prefix understates that model and the comparison stops being fair; the
committed artifact records the prefix *strings*, not a bool, for that reason.
The first `--arms` entry is the paired-McNemar baseline.

```bash
python evals/embedder_recall.py --questions 30            # quick smoke
python evals/embedder_recall.py --arms minilm bge-base-prefix qwen3-0.6b \
    --out evals/results/embedder-recall-<tag>.json
```

## Findings — 2026-07-27/28

Seven-arm shootout, 150 questions, 74,183 haystack turns, 299 gold turns
(`embedder-recall-shootout-20260727.json`):

| arm | dim | R@10 |
|---|---|---|
| all-MiniLM-L6-v2 (shipped at the time) | 384 | 0.572 |
| granite-embedding-english-r2 | 768 | 0.662 |
| bge-base-en-v1.5 (query prefix) | 768 | 0.716 |
| snowflake-arctic-embed-l-v2.0 (query prefix) | 1024 | 0.732 |
| bge-base-en-v1.5 | 768 | 0.742 |
| bge-large-en-v1.5 (query prefix) | 1024 | 0.742 |
| **Qwen3-Embedding-0.6B (instructed)** | **1024** | **0.809** |

The head-to-head against the runner-up, same corpus, paired McNemar
(`embedder-recall-qwen-vs-bge-20260728.json`): Qwen3-Embedding-0.6B **gains 32
questions and loses 12** against bge-base at k=10, **p = 0.0037**. The margin
holds at the other cut-offs (k=5: +42/−12, p = 5.2e-05; k=20: +24/−6,
p = 0.0014), which is what makes it a backbone choice rather than a k-tuning
artifact.

That is the swap that shipped: `vector(384)` → `vector(1024)`, and
`encode_query` now prepends the model card's instruction prefix — which is why
any threshold calibrated against the old symmetric MiniLM cosine (the
abstention floors near the top of this page, for one) is stale rather than
merely rescaled.

---

# Entity-kind classification (`classify_entity_kinds.py`, `apply_entity_kinds.py`)

> **These two are the exception to this page's isolation guarantees.**
> `classify_entity_kinds.py` **reads** the live bank (`pseudolife_memory`), and
> `apply_entity_kinds.py` **writes** it — the only harness in `evals/` that
> does. Everything else here uses `pseudolife_memory_bench`, reads read-only,
> or touches no DB at all. **Back up first** (`ops/backup.ps1`).

A one-time, human-gated pair that classifies cortex entities as
`artifact | system | concept` (schema v24), so the freshness policy can tell an
entity whose attributes genuinely go stale from one whose don't.

**Step 1 — classify (never writes the DB).** Writes a JSON artifact and stops.
Note the default judge is `claude-fable-5` served through the shim on
`$PL_SHIM_URL` (`:8082`), so **the scoped entity names leave the machine**;
`--scope-only` prints the funnel counts with no model call and no shim at all:

```bash
python evals/classify_entity_kinds.py --out evals/results/entity-kinds-<tag>.json
python evals/classify_entity_kinds.py --scope-only     # just the funnel counts
python evals/classify_entity_kinds.py --gold tests/fixtures/entity_kinds_gold.json
```

Scoping is the dominant token lever, not batch size: an entity only matters if
it carries at least one transient-looking attribute, since otherwise every one
of its facts resolves evergreen whatever its kind. On the live bank that was
2423 facts → 265 scoped → 33 rule-confident → 232 needing model judgement, a
**10.4×** reduction before a single model call (measured 2026-07-27; the counts
drift as the bank grows — reproduce with `--scope-only`). Batch size is 50:
larger batches degrade through lost-in-the-middle attention, label streaking,
correlated failure on one malformed response, and no retry granularity, while
batching at all helps because this is a *comparative* judgement.

**Step 2 — apply (writes the live bank; human-gated).** Dry run by default:

```bash
python evals/apply_entity_kinds.py --artifact <path>            # dry run
python evals/apply_entity_kinds.py --artifact <path> --apply
docker restart pseudolife-mcp-daemon                            # REQUIRED
```

Two writes, both reversible: `entity_kinds` rows, and a recompute of
`facts.freshness_class` through the **same** `resolve_class` the write path
uses — one policy, not two implementations that drift. Reverting:
`UPDATE facts SET freshness_class='evergreen'` restores the pre-run state
wholesale, and dropping `entity_kinds` reverts the write path. The daemon
restart is not optional: it caches the entity-kind map for the life of its
process and this script runs out-of-process, so until it restarts every new
fact keeps resolving evergreen.

## Findings — 2026-07-31/08-01 (the extractor-op saga: three gates and a pass)

Whether the extraction prompt should ask for claim-level `op`
(`"add"`/`"remove"`, targeting set-valued slots) took four pre-registered
KU-oracle e2e runs to answer. Artifacts, in order:

| run | verdict artifact | outcome |
|---|---|---|
| op block, first attempt | `c2-gate-verdict.json` | feature inert (0/78 adoption — a parse bug, fixed in `1eb0e2c6`) |
| op block, firing | `c2op-gate-verdict.json` | cascade −0.141 (p = 0.006) vs the op-less control → **block held** |
| op block + apply-time aggregate guard | `c2op-guard-verdict.json` | 0/78 flips — damage is extraction-side (count updates re-routed to member-adds), not apply-side |
| op block + count-exclusion rule (`ku_op_prompt_v5.txt`) | `c2op-count-verdict.json` | cascade back to exactly the control (delta 0.0, p = 1.0); count-class recovered; sets still form |

Supporting pieces: `op_probe.py` (prompt-format battery; count-update
decoys added 2026-08-01), `analyze_frozen_totals.py` + `c2op-count-census.json`
(the CPU forecast that sized the final arm before any GPU), and the
`--qids` bench flag (targeted per-question extraction, which turned
prompt-wording iteration from 47-minute e2e cycles into 6-minute probes).
The extraction-variance baseline (`var-base`, per-question identical to
its control) makes all these paired comparisons exact on the reproducible
q8_0 server. The shipped extraction prompt still carries **no** op block:
v5 is the shipping candidate, pending a ladder rung run and an explicit
reversal of the hold decision.
