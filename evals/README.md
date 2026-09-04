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
wordier slot values. Reproducibility caveat: these runs predate the
shims' `--reasoning-effort` flag, so neither pinned an effort — the Codex
shim inherited the host's `~/.codex/config.toml`
(`model_reasoning_effort = "high"` for these runs) and the Claude shim
the `claude` CLI's per-model default. Cross-machine reruns may measure a
different effort setting; a rerun wanting comparability should pin it
with the flag (or the request-level `reasoning_effort` field both shims
now honour).

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

### Comparator arms — `--refind` and `--nomem` (added 2026-09-01, smoke-run)

The same two arms the BEAM adapter grew, wired into this harness as well
(they share one implementation — `serve_comparator_arms` in
`longmemeval_bench.py`, which the BEAM adapter calls too, so the harnesses
cannot drift into serving them differently). Smoke-run 2026-09-01 on 5
oracle questions — see "First smoke" in the BEAM section for what that
does and does not establish:

| arm | flag | context |
|-----|------|---------|
| `refind` | `--refind` | an agentic **lexical** loop over the same haystack turns the bank ingested, budget-matched to the rag control ([ReFind](https://arxiv.org/abs/2608.12888)) |
| `nomem` | `--nomem` | nothing — the question, its date, and this harness's own task framing, including its one-sentence answer cap ([MemTrapBench](https://arxiv.org/abs/2608.20202)) |

Both contexts are **persisted like every other arm**, so the split
extract/answer flow still works: `--phase extract` builds them once,
`--phase answer` (and a later `rebuild_contexts.py` re-answer) replays
them without re-paying extraction. One caveat the split does not survive
untouched — `--refind` plans its searches with the **answerer** model, so
an extract phase carrying that arm needs the Qwen endpoint up as well
(probed up front, rather than dying mid-question after paying an ingest).
`--phase answer --refind` is rejected outright: it would silently do
nothing, since that phase only answers what is already persisted. `replicate.py agg` and
`replicate.py compare --arm refind` read the arms off the rows, so a
five-arm run cannot aggregate into a three-arm table.

```bash
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle \
    --extractor qwen-27b --tag refind --refind --nomem --limit 5
```

The rag arm's ReFind counterpart searches the *identical* stored turn
text — `archive_from_lme_question` and `ingest_and_dream` are pinned
turn-for-turn against each other by
`test_archive_mirrors_what_ingest_stores_turn_for_turn`, because both
format and order the haystack independently.

Run over the committed `ceiling-e2e` artifact (78 knowledge-update
questions), the leak check finds **0 leaked rows**; its 27 untestable
rows are **all `trivial_gold`** — LongMemEval always has a gold string,
so unlike BEAM there is no `no_gold` class here, and what it cannot test
is short numeric-or-yes/no answers (`25`, `Yes.`, `six`). The arm means
it recomputes reproduce that run's published table exactly (rag 0.859,
hybrid 0.8333, cortex 0.6667). Artifact:
`longmemeval-ku-oracle-qwen-27b-ceiling-e2e.leakcheck.json`.

### Token-matched rag arms — `--rag-lite-top-k` / `--rag-budget-tokens` (added and run 2026-09-04)

Every comparison this harness has published so far scores a ~100-token fact
context (`cortex`) against a ~1,200-token raw-turn context (`rag`), and
reports the accuracy gap and the token gap as two separate findings — when
they are one trade-off. Nobody had ever run a **token-matched
non-consolidating comparator**, so "the fact spine costs 0.19 accuracy" has
never been read against "…and what does plain RAG score if you give it the
fact spine's tokens?". These arms answer exactly that: the rag control's
*identical* retrieval, ranking, formatting, answer prompt and judge, served
at a narrower budget and nothing else changed.

| arm | flag | context |
|-----|------|---------|
| `rag1`, `rag2`, … | `--rag-lite-top-k 1,2` | the first K turns of the rag control's own ranking |
| `ragb<N>` | `--rag-budget-tokens N` | the rag ranking truncated to the turns that fit N approximate tokens (`len//4`) — matches a fact-spine budget exactly instead of by turn count |

Both knobs live in `build_contexts`, which BOTH harnesses call, so the
LongMemEval bench and the BEAM adapter cannot drift into serving them
differently — the same single-implementation contract `serve_comparator_arms`
carries for the ReFind and no-memory arms. Each arm is a **strict prefix** of
`contexts["rag"]` by construction (same list, same separator), pinned by
`tests/test_rag_lite_arms.py`; a width at or above the control's is rejected
rather than serving a copy of the control under a second name. The budget arm
measures its budget on the **joined block** — the same string whose
`approx_tokens` the row records — and always serves at least one turn, so on a
question whose top-ranked turn alone exceeds the budget it overshoots rather
than turning into a second no-memory control. The contexts are persisted like
every other arm, `replicate.py agg`/`compare`/`strip_judged` read the arms off
the rows, and a baseline that predates them does not fail the gate for their
presence.

Adding them to an **already-extracted** run needs `evals/rag_lite_rebuild.py`,
not `--phase answer` (which only answers already-persisted keys) and not
`rebuild_contexts.py` (which copies the rag context verbatim; the fact-bank
dumps do not contain the ranked turn list, and splitting the persisted block
back into turns recovers it for only 6 of the 78 `ceiling-v38` rows, because
turn texts contain blank lines). The rebuild re-ingests the static haystack on
the CPU, re-runs the control's pinned search, and refuses to write unless the
re-derived rag context matches the judged one byte for byte. `--slug ku|all`
picks the run family for both the source and the destination filename.

#### What the runs found (2026-09-04)

Three runs, all committed; procedure and full per-arm tables in
`docs/runbooks/raglite-runs-20260904.md`.

**The budget flag does not reach a fact-spine budget on LongMemEval, and
cannot.** Truncation is turn-granular and the arm always serves at least one
turn, while one raw LongMemEval turn is already ~200 approximate tokens. So
`ragb100` — sized to match the cortex arm's 96.7 tokens — served a mean
**219.2** tokens, overshot on 36 of the 78 `raglite-v38` rows, and produced a
byte-identical context to `rag1` on 74 of them (accuracies 0.333 vs 0.321).
Read the arm's measured `context_tokens` and its `budget_overshoot_rows`, never
its name. `ragb400` does land (309.0 served on the 78-question run, 312.3 on
the 500-question one), and on BEAM — whose turns are shorter relative to the
budget — `ragb600` served 584.

So the honest token-matched pair on LongMemEval is **cortex at ~97 tokens
against one-turn RAG at ~206**, and over the 500-question six-type run
(`longmemeval-all-oracle-qwen-27b-raglite-all-fresh`, fresh extraction) the two
are indistinguishable: **cortex − rag1 = −0.006 ± 0.049, p 0.87**
(77 W / 80 L / 343 ties). Paired against the `rag` control over the same 500
rows, hybrid is **+0.040 ± 0.031 (p 0.015, 41 W / 21 L)** and cascade
+0.002 ± 0.022, while every truncated raw-turn arm is far below it
(ragb400 −0.230 ± 0.041, rag2 −0.232 ± 0.042, rag1 −0.374 ± 0.045, cortex
−0.380 ± 0.048). Arm means and costs on that run: hybrid 0.730 @ 1229.3
tokens, cascade 0.692 @ 843.7, rag 0.690 @ 1124.2, ragb400 0.460 @ 312.3,
rag2 0.458 @ 432.5, rag1 0.316 @ 206.3, cortex 0.310 @ 96.5.

Those means — and the paired deltas above — span all 500 rows, the 25 the
leak check flags as naming their own gold answer included, so every arm is
paired over the same questions. The leak-free reads live in the summary's
own `leak_check` block and are not the headline figures: over the 475
unleaked rows, **rag 0.6947, hybrid 0.7326, cortex 0.3158**.

The paired column is a committed artifact
(`…raglite-all-fresh.arms-vs-rag.json`) written by
`evals/beam_within_run_pairs.py` — harness-agnostic since 2026-09-04
(`--score-key correct|score`, `--type-key`, `--prefix`, `--pairs left:right`,
and a derived `cascade` arm) — and pinned by a byte-exact regeneration test.

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

## Cue-gated contiguity (offline re-read of `aggp1-variants-0803`)

**2026-09-04 — no new answer or judge calls.** The 2026-08-04 Phase-1
gates applied four retrieval knobs to *every* query and all four lost on
the weak types; contiguity lost hardest (−0.147). This asks the obvious
follow-up: would contiguity have helped if it fired only where the
engine's own aggregation/temporal **cue** detector says the query is
asking about order or counts? The run persisted per-arm contexts,
judged verdicts and token counts for all 500 questions, so a gated
policy — vanilla `hybrid` where the cue is off, the variant where it is
on — is a composite of verdicts that were *already judged*.
`evals/contiguity_cue_split.py` builds it, importing
`has_temporal_cue` / `has_aggregation_cue` / `has_date_cue` from
`pseudolife_memory/memory/cms.py` rather than re-implementing them
(artifact `contiguity-cue-split-20260904.json`; paired sign-flip
permutation, 10k draws, seed 0, the same `_perm_p` `compare_arms.py`
uses).

**The cue is not selective enough to gate on.** `any` (temporal OR
aggregation OR date — the engine's own chronicle-serving gate) fires on
**0.702** of the 500 questions: recall **0.947** on the weak types, but
precision only **0.718**, and it fires on **0.692** of knowledge-update
questions — the type contiguity must not disturb. The date predicate
fires **0.000** times: LongMemEval carries the date in a separate field,
never in the question text.

| type | n | temporal | aggregation | any |
|---|---|---|---|---|
| multi-session | 133 | 0.256 | 0.887 | 0.940 |
| temporal-reasoning | 133 | 0.820 | 0.421 | 0.955 |
| knowledge-update | 78 | 0.321 | 0.538 | 0.692 |
| single-session-user | 70 | 0.243 | 0.200 | 0.429 |
| single-session-assistant | 56 | 0.196 | 0.107 | 0.268 |
| single-session-preference | 30 | 0.000 | 0.000 | 0.000 |

**Contiguity loses hardest exactly where the cue fires**, which is the
one shape gating cannot rescue. Paired against the same-run vanilla
hybrid, split on the `any` cue:

| arm | slice | n | delta vs hybrid | 95% CI | p |
|---|---|---|---|---|---|
| `hybrid_ctg` | all, cue fired | 351 | −0.114 | [−0.153, −0.075] | 0.00000 |
| `hybrid_ctg` | all, cue quiet | 149 | −0.047 | [−0.107, +0.013] | 0.18170 |
| `hybrid_ctg` | weak types, cue fired | 252 | −0.147 | [−0.199, −0.094] | 0.00000 |
| `hybrid_ctg` | weak types, cue quiet | 14 | −0.143 | [−0.423, +0.137] | 0.61820 |

The gated composites, against vanilla hybrid (0.664 overall / 0.459 weak)
and the naive-RAG control (0.688 / 0.515):

| gated arm | overall | weak types | ungated weak | overall tokens |
|---|---|---|---|---|
| `hybrid_ctg` gated | 0.584 | 0.320 | 0.312 | 1096.4 |
| `hybrid_tl` gated | 0.640 | 0.447 | 0.447 | 803.4 |
| `hybrid_enum` gated | 0.626 | 0.387 | 0.387 | 857.5 |
| `hybrid_all` gated | 0.546 | 0.293 | 0.282 | 1089.0 |
| vanilla `hybrid` | 0.664 | 0.459 | — | 842.1 |

Gating buys contiguity **+0.008** on the weak types (0.312 → 0.320) out
of a 0.147 hole, and still costs −0.139 against vanilla hybrid there
(p 0.00000) and −0.080 overall (p 0.00000) — while adding **254 context
tokens** overall and 378 on the weak types. `hybrid_tl` gated is
*identical* to `hybrid_tl` ungated because the timeline channel is
already cue-gated inside the engine (`timeline_fired` in `cms.py`,
whose `has_temporal_cue` trigger is a strict subset of the `any`
gate used here, so the two policies serve the same context on every
row); that agreement is the check that the imported predicates
behave here the way they do in production.

A narrower gate does not save it either. Gating contiguity on the
temporal predicate alone, or on the aggregation predicate alone,
lands at **0.616** overall and **0.376** on the weak types — better
than the `any` gate, still well under vanilla hybrid's 0.664 / 0.459
(the artifact's `gated_by_cue` block carries all four gates per arm).

**Why contiguity hurts is displacement, not dilution.** The served
memory block is a fixed top-k (3 turns), so a neighbor turn does not
extend the context — it *evicts* a ranked hit. On cue-fired rows
`hybrid_ctg` adds a mean **1.46** turns and displaces the same **1.46**
(333 of the 351 cue-fired rows lose at least one ranked
hit), while the token count rises 362: neighbor turns are longer *and* worse.

**Measurement floor.** Across the four variants, **522 arm-rows** served
a context byte-identical to the vanilla hybrid one and were answered and
judged independently anyway (the bench makes one answer call per arm, no
caching). **Zero** disagreed — the reproducible q8_0 serving path, so the
splits above carry no answerer/judge noise to net out.

Caveats, in full: a single replicate from 2026-08-03 on the **retired
Qwen3.6 judge**, so every number inherits that instrument; a composite of
two already-judged arms is not a run; and a gated knob that had looked
promising here would still need its own judged run before shipping. It
did not look promising. **Verdict: gating does not rescue contiguity** —
the cue fires on 70% of questions, and the losses are concentrated
inside the fired set.

---

# Review-queue judge ladders (`judge_ladder.py`, `queue_judge_ladder.py`)

Two harnesses answer "can a judge model reproduce the ratified human panel"
for the daemon's autonomous review-queue judging (2026-09-02 — every queue
the Console's Atlas Review view surfaces now gets a shadow/auto-gated
verdict from the SHIPPED judge code path itself, not a separate scorer).

`judge_ladder.py` runs `OpenAICompatExtractor.judge_merges` against the
frozen `judge_eval_20260816.json` fixture and scores reject/accept
precision — the Phase-1 gate that decided `judge_mode: shadow` is the only
safe out-of-the-box default. `--caution` (added 2026-08-31) stamps the
production low-differential caution line on flagged rows and reports
paired flagged/clean-subset metrics; `--max-tokens` (default 400) raises
the verdict budget for high-reasoning-effort arms — the 2026-08-31 xhigh
run truncated all 30 true-accept rows at the default budget.

`queue_judge_ladder.py` (2026-09-02) extends the same idea to every queue
the sweep now judges: merges, links, junk, candidates, and store curation.
It replays the shipped `judge_merges`/`judge_links`/`judge_junk`/
`judge_candidates`/`judge_slot_pairs` prompts against a blind-panel pack
and simulates each shipped auto-gate. The evidence pack itself is PRIVATE
(freezes bank text, lives outside the tree under gitignored `evals/data/`);
what's committed is the scrubbed derivative
`evals/results/queue-judge-panel-20260902.json` (labels, gates, per-row
votes, no bank text) and the harness's own output
(`evals/results/queue-judge-ladder-20260902.json`). `--data`/
`--snippet-chars` (added 2026-09-03) reran the merge judge at full-length
(uncapped) evidence instead of the shipped 240-char cap — accept precision
fell to 0.70 (from 0.85 clipped), so the default cap stays 240
(`evals/results/queue-judge-ladder-20260903-fulllen.json`).

First run (`opus-r2`, claude-opus-5, two replicates,
`evals/results/queue-judge-ladder-20260902.json`): merge two-vote reject
8/8, two-vote non-low-differential accept 4/4; link auto-accept 4/4,
auto-reject 5/5; junk auto-delete-under-the-evidence-bar 6/6, auto-keep
7/7; candidate auto-propose 7/8, auto-dismiss 15/16; curation
auto-distinct 21/21 — while duplicate keep-side precision is only 0.5625,
which is why curation's `auto` (as opposed to `auto-distinct`) forgetting
mode ships off. `tests/test_eval_evidence.py` pins every number here to
its artifact.

**Companion: the v35 write-time label heuristic.**
`evals/label_heuristic_audit.py` (schema v35,
`pseudolife_memory/memory/labels.py`) measures the deterministic
`authority`/`distortion_tolerance` form heuristic against hand verdicts.
On the live bank (2026-09-03, 869 entries / 5,435 current facts) the
shipped rule fires on 86 facts, of which 73 read as a genuine rule (0.85
precision), on 1 of 869 entries; on the chip-5 BEAM chat-text bank (1,099
current facts) it fires on 8 values, all 8 genuine. Artifacts:
`evals/results/label-heuristic-audit-20260902.json` (pre-fix),
`-20260903.json`, `-20260903-prefix-rule.json` (rejected variant), and
`-20260903-beam-chip5.json`.

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

## Comparator arms — ReFind and no-memory (added 2026-09-01)

Two opt-in arms, both adopted from the 2026-09-01 briefing-backlog triage.
Both were smoke-run first (below) and then measured at the full 100K tier
on 2026-09-02 — the five-arm table further down is the first real
comparison; on LongMemEval they remain smoke-only.

| arm | flag | context | measures |
|-----|------|---------|----------|
| `refind` | `--refind` | an **agentic lexical loop** over the same formatted turns the bank holds: the answerer model plans BM25 queries for up to `--refind-rounds` rounds, narrowing by date range, never re-reading a turn it already inspected, with session-aware rank fusion; the surviving turns are budget-matched to the rag control | the honest lexical baseline ([ReFind, arXiv 2608.12888](https://arxiv.org/abs/2608.12888)) — single-shot BM25 badly understates it, and without it a claim about the structural stack (bands, cortex, graph) has no floor to beat |
| `nomem` | `--nomem` | nothing — the question and this harness's own task framing, answer-length policy included | the memory-off floor ([MemTrapBench, arXiv 2608.20202](https://arxiv.org/abs/2608.20202), where all five frameworks tested scored *below* it). If memory-on does not beat memory-off, the win is imaginary |

The ReFind loop only **retrieves**; its context is answered by the
harness's own answerer and graded by the harness's own judge, so the arm
is instrument-matched to `rag`/`cortex`/`hybrid` (the same rule the Cognee
adapter follows — retrieval modes, never completion modes). It searches
the *identical* formatted turns that were stored into the bank, so a
`refind` − `rag` delta is about the retrieval loop and nothing else. Cost
per question is `--refind-rounds` extra planner calls (default 3) on top
of the arm's own answer + judge calls; `--nomem` costs one answer + its
judge items.

The loop's knobs — session fusion weight 0.3, 3 rounds, 3 queries per
round, 8 turns inspected per query — are **declared defaults, not
measured values**: ReFind publishes no fusion weight and no sweep has been
run here. Every one of them is a flag (`--refind-session-weight`,
`--refind-rounds`, `--refind-max-queries`, `--refind-per-round-k`, plus
`--refind-top-k` to break the budget match deliberately) so they can be
measured before anything is claimed from a number this arm produces. The
one constant that is not a flag is the 400-character snippet the planner
sees per turn, which is display width, not retrieval.

Ranking runs the fusion twice, and the second pass is the one that
decides what is served: once inside a query, to choose what that query
inspects, and again over the **union of everything inspected** at serve
time. Normalising per query would put every query's best hit at exactly
1.0, so a lone weak hit from a late round would tie the strongest hit of
the first and win on tie-break — caught in review before the arm ever
ran, and pinned by
`test_serve_ranking_fuses_across_rounds_not_per_query`.

```bash
# both comparator arms alongside the usual three, one chat first
PYTHONPATH=. python evals/beam_adapter.py --beam-root <path-to-BEAM> \
    --tier 100K --extractor qwen-27b --out-tag refind-smoke \
    --refind --nomem --limit-chats 1
# the comparison proper: full tier, all five arms
PYTHONPATH=. python evals/beam_adapter.py --beam-root <path-to-BEAM> \
    --tier 100K --extractor qwen-27b --out-tag refind-100k --refind --nomem
```

### First smoke, 2026-09-01 — plumbing only, not a measurement

Both arms ran for the first time on the reproducible Qwen3.8 server
(stock `llama-server`, `--cache-type-k/v q8_0`, verified by process
inspection): BEAM 100K chat 1 (20 questions, all five arms) and
LongMemEval oracle (5 knowledge-update questions, all five arms).
Artifacts: `beam-100K-qwen-27b-refind-smoke.jsonl(.summary.json)` and
`longmemeval-ku-oracle-qwen-27b-refind-smoke.jsonl(.summary.json)`.

**No accuracy from these runs is quoted anywhere, here or in the
CHANGELOG, and none should be.** One chat and five questions cannot
separate arms — read the committed summaries if you want to see them, and
treat them as plumbing receipts.

What the smoke *does* establish, from the per-row `refind_trace`:

- The loop behaves like a loop. On BEAM it used 2.9 of its 3 rounds on
  average, issued 7.4 distinct queries per question (cap 9), and
  accumulated 49 inspected turns per question (cap 72) — reformulating
  between rounds rather than repeating itself, which is what
  skip-already-inspected is for.
- It served **exactly 6 turns on every question of both runs**, the rag
  control's budget.
- **0 plan failures and 0 fallbacks** across 25 questions: the local model
  returned parseable JSON plans every time, and no window emptied the
  search.
- Temporal narrowing fires but is not the main channel: 7 of 49 BEAM
  rounds proposed a date window, and one LongMemEval question narrowed to
  a 3-day range and answered correctly.
- The no-memory arm was served a genuinely empty context on every row and
  abstained on the LongMemEval questions, as its prompt tells it to.

One asymmetry worth carrying into any real run: the arms are matched by
**turn count, not characters**. ReFind's 6 turns averaged ~17.4k chars
against the rag control's ~14.2k (hybrid sits at ~16.1k), because the loop
tends to select longer turns. A future run reading a refind-vs-rag delta
should say so, or add a character-matched variant.

### Full tier, 2026-09-02 — the first five-arm measurement

`beam-100K-qwen-27b-chip12-b16.summary.json` (rows in the `.jsonl` beside
it): 20 chats, 400 questions, every arm at a matched 16-turn budget,
reproducible Qwen3.8 answerer and judge, **one replicate**. The `rag` and
`hybrid` rows reproduce the committed `p1-b16` run at a paired delta of
exactly 0.0000 over all 400 rows
(`beam-100K-qwen-27b-p1-b16.vs-chip12-b16.paired.json`; the chip-5
comparison beside it carries the same control at 0.0000), so the
cross-arm deltas below sit on a zero instrument-noise floor. The paired
column is written by `evals/beam_within_run_pairs.py` into
`beam-100K-qwen-27b-chip12-b16.arms-vs-rag.json` (sign-flip permutation,
10k draws, seed 0, so the smallest reportable p is 1/10001; the CI is
1.96 × SE over the 400 per-row deltas).

| arm | score | vs rag, paired | served chars/q |
|---|---:|---|---:|
| rag | 0.6425 | control | 22,158 |
| refind | 0.6272 | −0.0152 ± 0.0362 (p 0.41) | 41,757 |
| hybrid | 0.6226 | −0.0199 ± 0.0285 (p 0.18) | 24,398 |
| cortex | 0.2829 | −0.3595 ± 0.0485 (p < 0.0001) | 2,207 |
| nomem | 0.1812 | −0.4612 ± 0.0479 (p < 0.0001) | 0 |

Two findings, both of which bound earlier readings on this page:

- **The no-memory floor is not diffuse, and on abstention it wins.**
  `nomem` is exactly zero on 7 of the 10 types and scores 1.000 on
  abstention, 0.469 on preference_following and 0.344 on
  instruction_following. On abstention that beats every memory arm
  (cortex 0.950, rag 0.725, hybrid 0.650, refind 0.575): refusing is the
  correct answer there, and an arm served nothing always refuses.
  62 of 400 rows score full marks with an empty context. Any BEAM number,
  ours or a vendor's, carries this floor — and the cortex arm's abstention
  lead, the number this page and the README used to call the fact spine's
  one decisive win, is a calibration property of a small context, not
  evidence that memory recalled anything.
- **The agentic lexical loop does not beat naive cosine RAG.** `refind`
  served 1.9× the characters (41,757 vs 22,158 mean characters per
  question) for a delta that is negative and not significant. It sits above
  the control on three of the ten types, but only contradiction_resolution
  (0.616 vs 0.500) clears the judge-transfer floor this page reports
  (mean |item delta| 0.073); temporal_reasoning (0.669 vs 0.644) and
  event_ordering (0.496 vs 0.472) sit inside it. The arms are matched by
  turn count, not characters — the asymmetry the smoke flagged — so read
  the refind row as "more text, same score".

The LongMemEval side of these arms is still smoke-only.

### Gold-answer leak check (`leak_check.py`)

The [SR-TTT retraction](https://arxiv.org/abs/2603.06642) came down to the
gold answer already sitting in the context the model was handed, so the
reported win measured nothing. Every BEAM row now records
`gold_in_question` at answer time, `--report` carries a `leak_check` block
(how many rows named their own gold answer, and every arm's mean with
those rows excluded), and the same check runs standalone over any judged
artifact — BEAM `*_score` rows or LongMemEval `*_correct` rows:

```bash
python evals/leak_check.py --in evals/results/<artifact>.jsonl
```

It always writes its report (`<artifact>.leakcheck.json`) and exits 1 when
any row leaked, so it can gate a promotion. Rows whose gold answer is too
short or generic to test (`yes`, a bare number) are reported as
**untestable** rather than counted clean. It also flags a context-free arm
that was served a context — a `nomem` row with content in it would flatter
memory-off in exactly the comparison the arm exists to make.

Run over the committed 2026-08-21 BEAM run (400 rows), it finds
**0 leaked rows**. Its untestable rows split
**200 `no_gold`** and **10 `trivial_gold`**: five of BEAM's ten question
types are rubric-judged and carry no gold string at all, so this check
cannot speak to half of that benchmark — and says so rather than
reporting those rows clean. The arm means it recomputes reproduce the
run's committed summary exactly
(rag 0.5005, cortex 0.2918, hybrid 0.4682), which is what makes the
recomputation trustworthy as a leak-free comparator. Artifact:
`beam-100K-qwen-27b-beam100k-qwen38.leakcheck.json`.

Beside those, the report carries each arm's mean over only the 190 rows
the check could examine: **rag 0.4789, cortex 0.1759, hybrid 0.4229**.
That is a different slice of the same run, not a correction to it — and
the gap is a fact about where each arm earns its score, not about
leakage. The rubric-only types it drops include abstention, the cortex
arm's best type (0.950 above — and see the no-memory floor in the
five-arm table), so removing them costs that arm the most.

### Memory-only answerability + pathway evidence (`answerability_probe.py`)

[AWM](https://arxiv.org/abs/2608.25618) removed the source context and
asked whether each question could still be answered from the agent's
terminal memory alone — and found **42.5% of correct answers could not
be reproduced from memory alone**: right answers whose notes were too
thin to support them later. End-to-end QA cannot see that failure, and
this stack is structurally exposed to it (dream claims and digests are
written while the full session is still in context).
[PAST-Bench](https://arxiv.org/abs/2608.04003) asks the per-row sibling:
does a correct answer actually follow the save → retrieve → use pathway?

```bash
python evals/answerability_probe.py --in evals/results/<artifact>.jsonl
```

Per arm, over the persisted contexts (CPU-only re-parsing, no model):
is the gold contained in the arm's served context — a two-step ladder
(`span`: a gold variant as a contiguous normalized token sequence;
`tokens`: every content token present — the reading a sentence-shaped
BEAM gold needs), crossed with the arm's verdict into four cells. The
interesting ones: `answerable_wrong` (an answering failure, the context
sufficed) and `unanswerable_correct` — the **AWM red-flag candidates**,
right answers without containment support. Containment is a floor, not
a judge, and it errs in **both directions**: the strict `span` rung
misses inference-phrased golds ("you *increased* the limit" is not
containable in the cortex arm's served chain — `two cups`, earlier
`one cup` — which plainly supports it), while the loose `tokens` rung
can accept content tokens scattered across a large served context that
no single passage states. So the red-flag cell is a **noisy candidate
set, not a bound**; the per-arm `answerable_by` split says how much of
the answerable side rests on the loose rung, and the judge-based level
(`--judge`, "can this be answered from this context alone?") is wired
to decide the cell: it probes the judge server up front and fails fast,
annotates rows resumably (`{arm}_answerable_judge`, stripped by every
rebuild/replicate path), and has deliberately not been run yet. The
same parse emits per-row **pathway evidence** for every correct answer:
which served entries carry the gold (`supported` / `unsupported` /
`spanning` when the gold is only assembled across entries), with the
supported share per arm. Two row classes classify out with their own
reasons instead of polluting the cells: abstention rows (their gold
names an absence — a right abstention with no memory support is the
designed outcome) and context-free arms (`nomem` is served nothing by
construction, so its correct answers are the arm's accuracy, not red
flags). Both harnesses' `--report` carry the block on any artifact with
persisted contexts.

Over the committed ceiling-e2e run (**78 rows**, 45 testable per arm —
**27 `trivial_gold`, 6 `abstention`**): answerable shares
**rag 0.9556, hybrid 0.9111, cortex 0.6222**; red-flag candidates
**rag 2, hybrid 1, cortex 3** of each arm's correct-testable answers;
and the cortex arm's wrong answers are dominated by storage/retrieval
(**14** `unanswerable_wrong` against 4 `answerable_wrong`) — when cortex
is wrong, the fact context usually never contained the gold, matching
the extractor-bottleneck reading of the e2e table above. Pathway
supported shares among examined correct answers:
**rag 0.9189, hybrid 0.9429, cortex 0.8889**. A committed audit of all
**six red-flag arm-rows (three distinct questions)** records verdict
`inference_gap` for each, with the served-evidence snippet quoted per
arm-row: the served context supports the answer without containing its
wording (the engineers-led 4→5 chain, the one-cup→two-cups chain, the
listed road bike). So this run surfaces **no confirmed memory-support
failure** — deciding the cell for real is the judge level's job.
Artifacts:
`longmemeval-ku-oracle-qwen-27b-ceiling-e2e.answerability.json`,
`longmemeval-ku-oracle-qwen-27b-ceiling-e2e.redflag-audit.json`.

The committed 2026-08-21 BEAM run predates context persistence, so the
probe classifies all **400 rows** untestable — **200 `no_gold`,
10 `trivial_gold`, 190 `no_context`** — and can say nothing about it
retroactively; the artifact records exactly that
(`beam-100K-qwen-27b-beam100k-qwen38.answerability.json`, `n_testable`
**0** on every arm). The two refind-smoke artifacts (contexts persisted,
all five arms) carry probe artifacts as plumbing receipts — n is far too
small to read as measurement.

## Findings — 2026-08-03 to 2026-08-24

| finding | evidence |
|---|---|
| **Abstention is the fact spine's best type — and a no-memory arm beats it there.** On BEAM's abstention questions the cortex arm scores **0.950** against naive RAG's 0.775 — a small curated fact context refuses where a raw-turn context confabulates — and the number is **identical under two independent judges** (local Qwen3.8 and an Opus-class CLI judge over the same recorded answers). The 2026-09-02 five-arm run bounds it: on the same 40 questions an arm served no memory scores 1.000, so this is a calibration property of a small context, not evidence of recall. Retired as "the one decisive win" on 2026-09-04. | `beam-100K-qwen-27b-beam100k-qwen38.summary.json`, `beam-100K-qwen-27b-beam100k-qwen38.rejudge-opus5.summary.json`, `beam-100K-qwen-27b-chip12-b16.summary.json` |
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

## Retrieval-pool probe (`retrieval_pool_probe.py`, 2026-09-04)

A **retrieval proxy, not a verdict.** It answers "does the gold-bearing
turn reach the served window?" under each candidate-pool setting, and
nothing about whether an answerer then gets the question right. Only a
judged run decides these knobs — the standing regression gate does not
reach them (scope warning below), so a dedicated one was run: **both
settings lose**, and the verdict table is at the end of this section.

Run (CPU only; no Postgres, no GPU, no judge, no network):

```bash
python evals/retrieval_pool_probe.py          # writes results/retrieval-pool-probe-<today>.json
python evals/retrieval_pool_probe.py --haystack 0   # synthetic corpus alone
```

Corpus: the 10 knowledge-update pairs + 6 distractors from
`ladder_sweep.py`, ingested initials → distractors → updates, buried in
400 real conversational turns whose TEXT is read from the
`band_ablation.py` band-state dumps (`results/banks/s-qwen-27b-ablbands-flat`)
and re-encoded with the current backbone. That directory is gitignored, so
a fresh worktree has to copy it from the main checkout; without it the
probe runs synthetic-only and says so in the artifact.

Why not LongMemEval gold: no dump under `results/banks/` can score recall
over `cms.retrieve()`. `dump_bank` persists cortex facts only (turns
absent, `source_entries` stripped), and the band-state dumps carry no
gold-turn labels — the `has_answer` markers live in the dataset, not the
dump — and their own vectors are 384-d from the retired MiniLM backbone.

**Result — `results/retrieval-pool-probe-20260904.json` (null):**

| multiplier | fusion | reranker | recall@6 | stale leak | churn vs shipped | latency |
|---|---|---|---|---|---|---|
| 1 | weighted_sum | off | 0.700 | 0.300 | — (baseline) | 52 ms |
| 1 | weighted_sum | on  | 0.700 | 0.300 | 0.000 | 112 ms |
| 1 | rrf | off | 0.700 | 0.300 | 0.183 | 55 ms |
| 1 | rrf | on  | 0.700 | 0.300 | 0.183 | 214 ms |
| 4 | weighted_sum | off | 0.700 | 0.300 | 0.283 | 48 ms |
| 4 | weighted_sum | on  | 0.700 | 0.300 | 0.283 | 373 ms |
| 4 | rrf | off | 0.700 | 0.300 | 0.317 | 75 ms |
| 4 | rrf | on  | 0.700 | 0.300 | 0.333 | 560 ms |

Every cell scores 0.700 with the *same three misses*, so on this proxy the
knobs buy nothing: the misses are questions whose gold turn no pool width
reaches. What they do change is *which* turns are served — 18–33% of the
served set — which is exactly the difference a judged run scores. The
null here is uninformative rather than negative *as a proxy*; the judged
verdict below is what settled the knobs, and it is negative. The cost
side is not null either: multiplier 4 with the reranker on is 7–11x the
shipped latency, because rerank-then-cut hands the cross-encoder ~4x the
pairs.

Power caveat: 10 gold queries whose gold values are rare tokens the BM25
channel already nails, over a 426-entry bank. Read the table as "no signal
at this scale", not "no effect".

**Scope warning — the regression gate does not cover these knobs.**
`regression_gate.ps1` stage 1 runs `rebuild_contexts.py`, which rebuilds
the CORTEX fact ranking offline and copies the associative (`rag`, hybrid
raw-memory) context verbatim, because no band state was dumped. The
candidate-pool knobs live on `cms.retrieve`. Measuring them judged means a
full `--phase extract` re-run with the sanctioned env overrides, which
`ladder_sweep.build_service` applies and `bench_env_knobs()` stamps into
the summary:

```powershell
$env:PSEUDOLIFE_BENCH_POOL_MULT = "4"   # unset = shipped default 1
$env:PSEUDOLIFE_BENCH_FUSION    = "rrf" # unset = shipped weighted_sum
python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft `
    --tag arm1-pool --phase extract
python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft `
    --tag arm1-pool --phase answer      # Start-Qwen first (qwen_server.ps1)
```

An invalid value aborts rather than silently serving the default
(`tests/test_bench_pool_knobs.py`).

### Judged verdict (2026-09-04): the knobs lose

That `--phase extract` re-run was done. Three runs over the LongMemEval
knowledge-update **oracle** slice (n=78, qwen-27b extraction, identical
judge and answerer): the shipped control, multiplier 4 + rrf, and
multiplier 4 + weighted_sum. Accuracy @ mean context tokens, with the
paired delta against the control, its bootstrap p (10 000 draws, seed 0)
and per-question wins/losses:

| arm | shipped (`pool-ctl`) | mult 4 + rrf (`pool-m4rrf`) | mult 4 + weighted_sum (`pool-m4sum`) |
|---|---|---|---|
| naive RAG (top-6 turns) | 0.859 @ 1184.1 tok | 0.744 @ 1793.0 (-0.115, p 0.0506, 4W/13L) | 0.782 @ 1643.0 (-0.077, p 0.1071, 2W/8L) |
| cortex facts only | 0.667 @ 96.7 tok | 0.667 @ 96.7 (0.000, p 1.0, 0W/0L) | 0.667 @ 96.7 (0.000, p 1.0, 0W/0L) |
| hybrid (facts + top-3 turns) | 0.897 @ 1289.7 tok | 0.833 @ 1898.6 (-0.064, p 0.1265, 1W/6L) | 0.872 @ 1748.6 (-0.026, p 0.6194, 1W/3L) |
| commit-gated cascade | 0.846 @ 389.4 tok | 0.846 @ 598.7 (0.000, p 1.0, 1W/1L) | 0.859 @ 544.5 (+0.013, p 1.0, 2W/1L) |

**The cortex arm is the control with identical input.** It never touches
`cms.retrieve`, so it scores 0.667 in all three runs with 0 wins and 0
losses — a measured noise floor of exactly zero on this instrument. Every
delta above is therefore a real difference in the served context, not
judge jitter.

**Reading it honestly.** Nothing is positive except the cascade's single
+0.013 under weighted_sum, which is one question (2W/1L, p 1.0) and is
noise. Neither RAG delta clears p < 0.05 at n=78 — rrf's -0.115 lands at
p 0.0506, a hair outside — so the individually-significant claim is not
available. What *is* available is the pattern: every arm that moves at
all moves down, under both knobs, while the turn-serving arms' context
cost rises by 36-54% (the token columns above: +35.6% to +53.7% on
rag/hybrid/cascade, cortex unchanged). A
knob that costs that much more context to lose 0.115 on its primary arm
does not need a tighter p-value to be declined.

**The reranker-on cell is untested.** Both runs had the cross-encoder OFF
and an empty reference bank. That is the only combination measured, and
it is the only one the CAUTION on `SearchConfig.fusion` permits: under
rrf the reranker's `fusion_weight` collapses to cross-encoder-only
ordering and un-rescaled reference cosines outrank every memory. Whether
a widened pool pays off *with* the cross-encoder — the configuration the
whole retrieve-then-rerank shape was built for — remains unmeasured.

Artifacts (all committed):
`results/longmemeval-ku-oracle-qwen-27b-pool-{ctl,m4rrf,m4sum}.jsonl`
and their `.summary.json`; paired comparisons
`results/compare-pool-m4rrf-pairs.json` and
`results/compare-pool-m4sum-pairs.json`.

This is why both knobs ship at today's behaviour, stay off the Console
(`tests/test_console_knob_gapfill.py`), and are documented as measured
losers rather than as unmeasured options.

**Regression gate for the v35 label carrier (2026-09-03).** Two paired
checks confirmed the write-time `authority`/`distortion_tolerance` labels
(and their `constraint`-carrier dream logic) don't move numbers where no
label fires. `ladder_pair_compare.py` re-ran the extraction ladder's
deterministic metrics (`gold_recoverable`/`stale_leak`/`tokens_per_query`)
pre- and post-#245 on the unlabelled ladder corpus and found them
verdict-identical on both rungs, as predicted
(`evals/results/ladder-chip5-paired-verdict.json`).
`beam_cross_run_paired.py` paired the full BEAM 100K run at the matched
16/16 budget against the 2026-09-02 pre-#245 baseline on all 400
questions: the identical-input `rag` control moved 0.0000, hybrid
+0.0004±0.0014, cortex +0.0036±0.0029 — every delta inside the control's
own noise. The 30 rows whose served context differed all sit in the two
chats where the write-time heuristic labelled a slot `constraint` (3 of
1099 facts; `quoted` fired on 11), confirming the recall pin is the only
thing the label change touched
(`evals/results/beam-100K-qwen-27b-chip5-b16.vs-chip12-b16.paired.json`).

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

# Recall fan-out cap (`recall_fanout_bench.py`)

Does bounding `memory_recall`'s search fan-out cost it any answers? The walk
as shipped issued one seed search plus one re-query per newly discovered
entity per hop, which on a star-shaped graph is the whole cost of the call.
`memory.recall.max_searches_per_hop` / `max_total_searches` /
`time_budget_seconds` bound it; this harness runs the same 20 relational
questions with the caps off and on, against a **restored copy** of the live
bank (never the live bank, never the shared bench DB — `guard_dsn` refuses
both), and records per question: searches issued, wall time, served
characters, whether the expected entity surfaced, and how the added entities
arrived (hub / `part-of` / domain relation).

```
# one arm per invocation
python evals/recall_fanout_bench.py --arm before --dsn postgresql://.../pseudolife_memory_replay_YYYYMMDD --out before.json
python evals/recall_fanout_bench.py --arm after  --dsn postgresql://.../pseudolife_memory_replay_YYYYMMDD --out after.json
# pair them into the committed artifact
python evals/recall_fanout_bench.py --combine before.json after.json --out evals/results/recall-fanout-cap-20260904.json
```

Question set: the twelve relational questions the 2026-09-04 graph-ablation
probe ran (`evals/graph_ablation.py`, landing separately) plus eight written
for this bench, n=20. Every `expect` string also
occurs in the tracked repo tree, so the artifact carries no bank-private
names, and no query or entry text is emitted.

## Findings — 2026-09-04 (`recall-fanout-cap-20260904.json`)

Restored copy of the live bank (1,296 entries, 5,504 entities, flat preset),
CPU only, `top_k=6`, `hops=3`. BEFORE is the pre-change package (the knobs do
not exist in it at all); AFTER ran the caps at 6 / 20 / 20.0 s.

The AFTER arm's `code_commit` is `7595ce6f+dirty` — the working tree of the
branch before it was committed, so the arm is pinned by the artifact's `caps`
block rather than by a commit hash. Two edits landed after the run and neither
can move its numbers: the `skip_part_of_expansion` induced-subgraph fix is
inert with that knob off (`False` in the recorded `caps`), and the
negative-value normalisation of the three numeric knobs is a no-op for the
positive values recorded. The shipped `max_total_searches` default was later
raised from the 20 recorded here to 31 (a backstop above `1 + 6 x 5`, the most
the per-hop cap can spend at the tool's maximum `hops=5`); at `hops=3` a full
walk costs at most 19, the ceiling never fired in either arm, and the numbers
below stand unchanged.

Reruns meant to be reproducible should pin the AFTER arm's budget off with
`--time-budget-seconds 0`. A wall-clock budget makes the walk
machine-dependent — the same question can truncate on slow hardware and not on
fast — so leaving the 20.0 s default in place means a rerun that disagrees
cannot be told apart from a real regression. (The BEFORE arm is unaffected:
`apply_arm` forces every cap off for it.) The run below kept the 20.0 s
default and it never fired, which the artifact records as
`truncated_calls: 0`.

```
metric (per call)        before      after     ratio
searches issued  mean     89.15      12.40      7.2x fewer
                 median   58         13
                 max     205         19
recall wall (s)  mean     25.25        4.166     6.1x faster
                 median   16.38        4.49
                 max      57.67        7.51
served chars     mean  178,110     77,546      2.3x smaller
expected targets found    20/20      20/20
```

- **Nothing was lost, and the check could have failed.** `targets_lost` is
  empty: every expected target the uncapped walk surfaced, the capped walk
  surfaced too. Read that with the mechanism in mind — the caps bound the
  SEARCH budget and deliberately leave graph expansion alone, so the entity,
  edge and iteration counts are identical on all 20 questions
  (`structural_identity`) and the only channel that could lose a target is
  `texts`. `hit_channels` says how much of the question set actually rode
  that channel: 17 targets arrived on `entity` (where the check has no
  power) and **3 on `texts`** (where it does). Those three survived the cut
  — the finding is about three questions, not twenty.
- **The whole saving is supporting text.** 2,116 texts before, 558 after. The
  MCP layer caps `texts` at 6 anyway, so the character figure above is the
  service-level payload, not what a model sees — the honest headline is the
  wall time and the search count.
- **The per-hop cap does the work; the ceiling is a backstop.** With
  `max_searches_per_hop=6` and `hops=3` a full walk costs at most
  1 + 6 + 6 + 6 = 19 searches, so the `max_total_searches=20` this run used
  never fired on these questions (`truncated_calls: 0` in both arms) and
  neither did the 20 s budget. The shipped default has since been raised to
  **31** so the ceiling is a backstop at every `hops` the tool accepts
  (clamped 1..5, where the per-hop cap can spend 1 + 6 x 5 = 31) rather than
  binding at 4 and 5 hops; at `hops=3` that changes nothing here. The ceiling
  and the budget are pinned by unit tests (`tests/test_recall.py`), not by
  this run.
- **Search is still 16x cheaper.** Plain `memory_search` on the same questions
  is 0.26 s and 7,789 chars per call and found 18 of the 20 targets; recall
  buys the last two, and now costs 4.2 s instead of 25.3 s to do it.
- The eval-only `skip_part_of_expansion` knob is not exercised in this run;
  `arrivals_total` records the shape it targets (1,046 of the 1,763 added
  entities arrived via `part-of` alone, identically in both arms).

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

---

# Retrieval telemetry, offline replay, and the graph ablation (2026-09-04)

Three read-only harnesses over a **restored copy** of a live bank. None of
them touches `pseudolife_memory` or the shared `pseudolife_memory_bench`:
each refuses those two database names outright, in either DSN spelling and
regardless of case. `retrieval_telemetry_review.py` goes no further than
that — it is plain SQL over the log tables, loads no model and never opens
the search path. The two that do search (`retrieval_replay.py`,
`graph_ablation.py`) build a `MemoryService` against the restored copy and
then force `embedding.device = "cpu"` and
`memory.retrieval_log.enabled = False`, so a replay cannot append to the
log it is replaying.

Restore recipe (the 2026-09-04 run used
`pseudolife_memory_replay_20260904` on the bench Postgres):

```powershell
ops\backup.ps1 -OutDir <scratch>          # sanctioned dump path (pg_dump, read-only)
docker cp <dump>.sql.gz pseudolife-mcp-postgres:/tmp/replay.sql.gz
docker exec pseudolife-mcp-postgres psql -U pseudolife -d postgres `
  -c "CREATE DATABASE pseudolife_memory_replay_20260904 OWNER pseudolife"
docker exec pseudolife-mcp-postgres sh -c `
  "gunzip -c /tmp/replay.sql.gz | psql -U pseudolife -d pseudolife_memory_replay_20260904 -q"
```

Pass the **deployed** `config.yaml` (`docker cp pseudolife-mcp-daemon:/data/config.yaml .`)
with `--config` so the "shipped" arm is production and not the dataclass
defaults.

**Privacy.** Query text and entry text are private (this is a public repo),
and the graph holds personal names and machine identifiers. The artifacts
carry aggregates and ids only; `graph_ablation.py` emits an entity name
only when `git grep` finds it in the tracked tree, and writes `<redacted>`
otherwise.

## `retrieval_telemetry_review.py` — does the learned reranker have labels yet?

PR #168 logs the (query, served) half of the training tuple in
`retrieval_events`, and `retrieval_uses` records the implicit relevance
label: a `memory_get` / `memory_reinforce` on a served entry credits the
most recent in-session serving event within `use_window_seconds` (3600).
PR #200/#201 added `slot_reads`, `served_facts` and
`entries.explicit_reinforcements`.

The script separates the counters that mean **consumption** from the ones
that only mean **served**, which is the distinction the raw numbers hide:

| counter | what it actually means |
| --- | --- |
| `retrieval_uses` | consumption — a served entry was later dereferenced or reinforced |
| `entries.explicit_reinforcements` | consumption — moves only on `memory_reinforce` |
| `entries.access_count` | **serve count** — `cms.py` bumps it for every entry in a merged result set |
| `slot_reads.read_count` | **serve count** — `_track_slot_reads`: "count each slot SERVED as an answer" |

### Findings — 2026-09-04 bank (`retrieval-telemetry-review-20260904.json`)

| quantity | value |
| --- | --- |
| logged events | 1349 |
| distinct sessions / episodes | 60 / 101 |
| **events with any downstream signal** | **1** (0.074%) |
| `retrieval_uses` rows | 1 (`used_via=get`, served rank 0, 72 s after the serve) |
| `entries.explicit_reinforcements`, bank-wide sum | **0** |
| served-list length: mean / mode | 4.94 / 5 (146 events served exactly 1; 0 served nothing) |
| `params` coverage (v32+) | 790 / 1349 (58.6%) |
| `served_facts` coverage (v34+) | 160 / 1349 (11.9%), 798 facts |
| served entry ids that still resolve in `entries` | 6666 of 6666 (no dangling ids) |
| `slot_reads` | 605 slots, 807 serves — all serve-side |

The event log is healthy: it writes on every search, the ids all still
join, and 59% of rows carry the ranking-knob snapshot. The **label** side
is empty. One labelled event is not a small sample, it is a plumbing
check. Read against the plan's "a few hundred logged events", the correct
reading is a few hundred **labelled** events — an event with no target
trains nothing — so Phase 1 is 299 labelled events short of its own
floor.

Why: the label is only written by `memory_get` and `memory_reinforce`, and
agents overwhelmingly consume `memory_search`'s inline result text and
never dereference an id. Nothing about the current tool surface makes them.

**Cheapest changes that would actually produce labels**, in ascending cost:

1. **Credit `memory_fact_get` / `memory_fact_resolve` against `served_facts`.**
   The fact half of the tuple has been recorded since v34 and has no
   `uses` table at all; a fact-side read is a genuine consumption event
   the daemon already sees.
2. **An explicit `used_ids` parameter on `memory_outcome`.** The
   convention already requires an outcome at task end, so the caller is
   present and knows which memories mattered; today that knowledge is
   discarded. This is the only option that produces *positive* labels for
   the entries an agent actually reasoned from rather than clicked on.
3. **Treat a `memory_store` whose text quotes a served entry as a use.**
   Free (no tool-surface change) but noisy, and it labels writing, not
   reading.

Option 2 is the one worth shipping: it is a single optional list
parameter, it is written by the agent that just used the memories, and it
labels the whole served set rather than the one id someone happened to
dereference.

## `retrieval_replay.py` — the shipped knobs on the queries agents really asked

Re-runs the logged queries through an offline `MemoryService` on the
restored bank under several settings and scores each against a label set.

Label sources: `uses` (the real implicit labels — n=1 on this bank, so it
is a plumbing check), and `logged-top1` / `logged-top3`, which use the
entry ids the daemon itself served at those ranks as pseudo-labels. The
`logged-*` sources measure **agreement with the shipped ranker's own past
head**, i.e. how far a setting moves the served head — never relevance.

The `feat/retrieval-candidate-pool` arm probes the live config object for
pool/fusion knobs rather than trusting a branch name; on 2026-09-04 the
sibling worktree carried none, so the arm reports itself skipped.

**The bank has grown since these events were logged**, so absolute MRR and
hit@k are indicative only. Every arm sees the identical restored bank and
the identical query list, so the paired comparison across arms is the
valid read. The query-embedding LRU is cleared between arms — without
that, the second arm reads its query vectors out of cache and posts a
latency an order of magnitude below the first.

### Findings — 2026-09-04 (`retrieval-replay-20260904.json`), 250 sampled events, top_k=6

Latency is the **median** per-query wall time
(`results.logged-top1.arms.<arm>.median_latency_s` in the artifact).

| arm | MRR | hit@1 | hit@3 | hit@6 | median latency |
| --- | --- | --- | --- | --- | --- |
| `shipped` (deployed config) | 0.784 | 0.668 | 0.888 | 0.948 | 0.305 s |
| `bm25_off` | 0.689 | 0.544 | 0.812 | 0.920 | 0.140 s |
| `rerank_on` | 0.606 | 0.368 | 0.852 | 0.948 | 0.694 s |

Read as drift, three things:

- **BM25 is load-bearing for the head.** Turning it off moves 12.4 points
  of hit@1 and 9.4 of MRR while leaving hit@6 nearly intact — the lexical
  channel decides *which* of the right six goes first, which is what a
  reranker would be trained to do.
- **BM25 costs ~165 ms per query at this bank scale** (median 0.305 s
  vs 0.140 s), well above the 20-50 ms the config docstring quotes. That
  docstring number is due a re-measure; it is not pinned to an artifact.
- **The cross-encoder reranker reshuffles the head hard and does not
  obviously improve it.** hit@1 drops 30 points against `shipped` while
  hit@6 is unchanged — it is re-ordering the same six. Whether that
  re-order is better cannot be settled by this harness, because the label
  IS the shipped ranker's own head; it needs a judged run or real
  `uses` labels. It stays off by default, and that decision is untouched
  here.

## `graph_ablation.py` — lever 6, does `memory_recall`'s expansion earn its cost?

Two halves. `shape` describes the graph itself; `ablate` pairs
`memory_recall` against plain `memory_search` on the same queries and
classifies how each extra entity **arrived**: through a `part-of` edge
only (containment, the cheapest edge the extractor makes), through a
domain relation (`depends-on`, `uses`, `runs-on`, …), through a hub node
(degree >= p95), or unlinked (it came from the re-query's dense hits, not
from an edge at all).

Query sets: 30 hand-written relational questions in the bank's own domain
(each names the entity that should surface) plus a sample of the logged
retrieval events, scored on whether the entry the daemon served at rank 0
comes back. `--rel-limit` / `--logged-limit` cap both sets — `recall` at
the shipped defaults (3 hops, `max_entities=50`, `expand_budget=0`) issues
one search per newly-discovered entity per hop, which measured a **mean
of 32.4 s per call on the relational set and 44.3 s on the logged set,
worst case 73.0 s** on CPU against this bank
(`ablation.*.summary.recall.mean_wall_s` in
`graph-ablation-20260904.json`), so a full 30-question sweep still runs
to tens of minutes. The artifact records the `n` it actually asked.

### Findings — graph shape, 2026-09-04 (`graph-ablation-20260904.json`)

| quantity | value |
| --- | --- |
| entities | 5504 |
| edges (live / all versions) | 4020 / 4247 |
| degree p50 / p95 / max | 1 / 5 / 132 |
| `part-of` share of live edges | 19.0% |
| entities with no live edge at all | 1156 (21%) |
| dead weight (only `part-of` edges, no current fact) | 421 |

Live edges by relation: `prefers` 929, `part-of` 765, `uses` 736,
`configures` 272, `depends-on` 272, `related-to` 181, `implements` 181,
`avoids` 162, `tests` 148, `runs-on` 139, `stores-data-in` 116, `hosts`
70, `superseded-by` 49.

Two things the shape says on its own:

- **The graph is a hub-and-spokes star, not a mesh.** Median degree is 1
  and p95 is 5, while the top node (`pseudolife-mcp`) carries 132 — so
  most nodes are leaves hanging off a handful of hubs, which is exactly
  the topology the recall hub gate exists to refuse to expand through.
  1156 entities carry no live edge at all.
- **Comparator names the corpus argues about are missing from the
  graph.** Of the terms checked, `naive rag` (16 entries) and `titans`
  (21 entries) are mentioned in five or more entries and have **no
  node**, while `rag`, `longmemeval`, `cognee`, `bm25`, `beam` and `lme`
  all do. The extractor promotes subjects of claims, not the things
  claims are compared against — so the one relation a reader most wants
  ("what did we measure this against, and what happened") is the one the
  graph cannot answer.

### Findings — `recall` vs `search`, 2026-09-04 (same artifact)

8 of the 30 relational questions and 4 logged queries — the run size the
per-recall cost allowed (mean 32.4 s relational / 44.3 s logged, max
73.0 s), and small enough that the hit-rate column is a ceiling, not a
comparison.

| | relational (n=8) | | logged (n=4) | |
| --- | --- | --- | --- | --- |
| | `search` | `recall` | `search` | `recall` |
| mean served chars | 6932 | 184641 | 6649 | 74186 |
| mean wall time | 0.44 s | 32.4 s | 0.39 s | 44.3 s |
| expected entity/entry found | 8/8 | 8/8 | 4/4 | 4/4 |
| recall-only hits | — | 0 | — | 0 |

`recall` served **27× the characters at 74× the wall time** of plain
`search` on the relational set (11× / 114× on the logged set) and found
the expected target no more often, because plain `search` already found
it every time. That last clause is the honest limit of this run: at n=8
with both arms at 100%, the questions cannot separate the two arms on
quality — they only price the difference. A question set that plain
search *fails* is what a quality verdict needs, and writing one is the
obvious next step.

What the expansion is made of is measurable even at this n. Of the 524
entities `recall` added beyond its seeds on the relational set:

| arrival | count | share |
| --- | --- | --- |
| touches a hub (degree >= p95 = 5) | 520 | 99.2% |
| only `part-of` edges | 225 | 42.9% |
| at least one domain relation | 299 | 57.1% |
| unlinked (came from the re-query, not an edge) | 0 | 0% |

Essentially every entity the graph adds arrives through a hub, and over
two fifths arrive through containment alone. On a star-shaped graph with
median degree 1, "expand the neighbourhood" mostly means "enumerate a
hub's spokes" — which is why the payload is 27× larger without being
more likely to contain the answer. The hub gate stops recall expanding
*through* a hub; it does not stop a hub's spokes being pulled in as
results.
# Offline routing analysis (`router_offline.py`)

The engine concatenates channels for every query — the hybrid arm serves a
cortex fact block plus the top-k raw entries, whatever the question. The
only routing policy that has ever won a measurement is the commit-gated
cascade (serve cortex when it commits, else rag). This script asks whether
a router that reads the QUESTION SHAPE could beat that, and answers it
without a GPU: it re-aggregates the per-question verdicts that three
already-judged runs left behind.

**What these numbers are.** Offline re-use of judged verdicts. No new
answer calls, no new judge calls, a single replicate per source run, and a
local judge in every case. The oracle rows are fit on the very questions
they score, so they are BOUNDS on what a router could reach, never shipped
results. The realizable rows are 5-fold cross-validated by question — a
prediction always comes from a model that never saw that question — but
they still inherit the source runs' judge and era. The "best" realizable
router is a maximum over every cross-validated configuration the script
tries, so it carries the usual select-the-best optimism. There is one
feature representation throughout (`FEATURE_NAMES`); what varies is the
candidate ARM set, crossed with two classifiers (`tree_d3`, `logreg`) and
two label policies, plus the type-prediction and two-stage variants. That
is **16** configurations on LongMemEval-500 and on the 78-question slice —
two candidate sets each — and **22** on BEAM-400, which has three because
BEAM also carries a no-memory arm. So the optimism is largest on the
benchmark carrying the larger headline gain. The verdict does not depend
on it: the maximum still fails the preregistered bar on both benchmarks.

```bash
python evals/router_offline.py --out evals/results/router-offline-20260904.json
```

Deterministic and seeded (`SEED = 0`): two runs produce byte-identical
JSON, and `tests/test_router_offline.py` regenerates the committed
artifact and compares it.

## Sources and cost units

| tag | rows | source artifact | cost column |
| --- | --- | --- | --- |
| LME-500 | 500 | `longmemeval-all-oracle-qwen-27b-alltypes-0803.jsonl` | real `*_context_tokens` |
| LME-KU78 | 78 | `longmemeval-ku-oracle-qwen-27b-ceiling-v38.jsonl` | real `*_context_tokens` |
| BEAM-400 | 400 | `beam-100K-qwen-27b-chip12-b16.jsonl` | context **characters** |

BEAM rows carry no token column, so cost there is the length of
`contexts[arm]` in characters; the ratio column divides by a flat 4
chars/token and is labelled `est_tokens` in the artifact. The two units are
never mixed. LongMemEval scores are binary judge verdicts; BEAM scores are
the paper-faithful float rubric means.

The cascade arm is not re-implemented here — `replicate.cortex_commits` and
its cost rule are imported, and a test asserts the derived arm matches
`replicate.cascade_correct` / `cascade_context_tokens` row by row. As a
sanity gate the script also recomputes each run's published per-arm table
from the rows: LME-500 reproduces its summary exactly (max score delta
0.0000), LME-KU78 and BEAM-400 to within the summaries' own rounding
(< 5e-4). If that gate ever drifts, nothing below it is trustworthy.

## LongMemEval, 500 questions, six types

Accuracy and mean served tokens side by side, plus accuracy per 1k tokens
so the trade is one number rather than two.

| policy | accuracy | mean tokens | acc / 1k tok |
| --- | --- | --- | --- |
| cortex only | 0.416 | 158 | 2.629 |
| hybrid (facts + top-k) | 0.664 | 842 | 0.789 |
| **rag — best single arm** | **0.688** | 1210 | 0.569 |
| cascade (shipped policy) | 0.690 | 883 | 0.782 |
| oracle by type (arms + cascade) | 0.712 | 893 | 0.797 |
| oracle per question (ceiling) | 0.778 | 419 | 1.857 |
| best cross-validated router | 0.690 | 883 | 0.782 |
| router via predicted type | 0.686 | 1002 | 0.685 |
| two-stage: cascade, then router | 0.690 | 883 | 0.782 |
| two-stage, token-greedy labels | 0.656 | 667 | 0.983 |

The oracle-by-type bound is **+0.024** over the best single arm, at 316
fewer tokens. The best realizable router is **+0.002**, and it is the
shipped policy in a different shape: the two-stage variant serves cortex on
the 193 questions where cortex commits and rag on the other 307, landing on
0.690 at 883 tokens — the cascade's own score and cost, to the digit. A
router that reads only the question's shape does not get there. The best
single-stage one ties the best single arm at 0.688 on 1205 tokens, and the
two variants free to pick the cascade as well tie at 0.678, on 1005 and
1009 tokens, agreeing with the oracle-by-type choice on 0.226 of questions.

## BEAM 100K, 400 questions, ten types

| policy | score | mean chars | score / 1k est-tok |
| --- | --- | --- | --- |
| no memory | 0.181 | 0 | n/a |
| cortex only | 0.283 | 2 207 | 0.513 |
| cascade | 0.552 | 14 294 | 0.154 |
| hybrid | 0.623 | 24 398 | 0.102 |
| refind | 0.627 | 41 757 | 0.060 |
| **rag — best single arm** | **0.642** | 22 158 | 0.116 |
| oracle by type (arms + cascade) | 0.683 | 22 861 | 0.120 |
| oracle by type (+ the no-memory arm) | 0.688 | 22 635 | 0.122 |
| oracle per question (ceiling) | 0.789 | 17 672 | 0.179 |
| best cross-validated router | 0.651 | 22 829 | 0.114 |
| router via predicted type | 0.620 | 27 780 | 0.089 |
| two-stage: cascade, then router | 0.554 | 14 364 | 0.154 |

Here the oracle-by-type bound is larger — **+0.046** — but it costs 477
chars MORE than rag, not fewer, because the types it moves off rag it moves
onto refind and hybrid, both of which serve more context. The best
realizable router recovers **+0.008** of that, also at more cost. The
cascade is not the strong policy on BEAM that it is on LongMemEval: cortex
alone scores 0.283 there, so committing to it costs 0.09.

## LongMemEval knowledge-update, 78 questions (ceiling-v38)

| policy | accuracy | mean tokens | acc / 1k tok |
| --- | --- | --- | --- |
| cortex only | 0.667 | 97 | 6.894 |
| hybrid | 0.846 | 731 | 1.157 |
| cascade | 0.846 | 389 | 2.173 |
| **rag — best single arm** | **0.859** | 1184 | 0.725 |
| oracle by type | 0.859 | 1184 | 0.725 |
| oracle per question (ceiling) | 0.962 | 318 | 3.021 |
| two-stage: cascade, then router | 0.846 | 382 | 2.212 |

This slice is one question type, so a type router is degenerate on it by
construction — the oracle-by-type row is the best single arm, exactly. It
is here for the per-question ceiling: **0.962** over the three channels,
against 0.936 for the rag∪cortex union on the same rows. (The 0.949 union
published in the guide is a different run — the e2e ceiling — and a
two-channel union; the two are not interchangeable.)

## Why the routers do not reach the bound

The question type IS partly predictable from surface text — 0.654 on
LME-500 and 0.652 on BEAM-400 by 5-fold CV, against majority baselines of
0.266 and 0.100. The gap is not in the classifier. It is that

- the per-type best-arm differences are small (LME-500: +0.024 for a
  perfect type oracle), so a classifier at 0.65 gives most of that back on
  its mistakes — `router_via_type` scores BELOW the best single arm on
  every dataset; and
- the per-question best-arm label is dominated by ties. Trained on it, both
  models collapse: 493/500 rag under accuracy-first tie-breaking on
  LME-500, or 475/500 cortex under cost-first, which trades 0.25 accuracy
  for the tokens.

The token-greedy variants are the one place a router earns something real,
and it is a cost win, not an accuracy win: two-stage with cost-first labels
serves LongMemEval at 0.656 on 667 tokens (0.983 acc/1k) against rag's
0.688 on 1210 (0.569). That is the same trade the cascade already makes,
made harder.

## Robustness across benchmarks

Of the four question types the two benchmarks share, the oracle's best-arm
choice agrees on **two**:

| LongMemEval type | BEAM type | LME best | BEAM best | agree |
| --- | --- | --- | --- | --- |
| knowledge-update | knowledge_update | cascade | cascade | yes |
| single-session-preference | preference_following | rag | rag | yes |
| temporal-reasoning | temporal_reasoning | hybrid | refind | no |
| multi-session | multi_session_reasoning | rag | hybrid | no |

A per-type choice that flips between benchmarks is a property of the
benchmark, not of the question shape, and cannot be shipped.

## Verdict

The criterion, fixed before the numbers were read and recorded in the
artifact: a cross-validated router must beat the best single arm by at
least 3 points at no more served cost, on BOTH benchmarks.

**It fails, and so does the oracle bound.** The realizable gains are +0.002
(LongMemEval, at 327 fewer tokens) and +0.008 (BEAM, at 671 MORE chars).
Even a router with perfect knowledge of the question type would fall short:
+0.024 on LongMemEval is under the bar, and BEAM's +0.046 comes at more
cost. The per-question ceilings — 0.778 and 0.789, +0.090 and +0.147 over
the best single arm — say the channels genuinely disagree and a *perfect*
selector would be worth a great deal; they also say the signal that picks
correctly is not in the question's surface form.

Read against the cascade: on LongMemEval the shipped cascade already sits
at 0.690/883 tokens, which the best router matches exactly and no router
beats. The gain is in the cascade already. A query-shape router is not
worth building; if the per-question ceiling is to be approached, the
selector needs a signal from the retrieved evidence (the cascade's
abstention gate is one such signal, and it is the one that works), not from
the question text.

---

# Smaller probes

Five tracked scripts, each answering one narrow question, without their
own section above:

- `beam_attrib_ablation.py` (2026-08-24) — re-answers a BEAM run's
  persisted contexts with the pre-Phase-1 answer prompt, holding the turn
  budget and ordinals fixed, to isolate the prompt term from the budget
  term in the Phase-1 delta.
- `digest_sidecar_probe.py` (2026-08-24/27) — generates session digests
  against a configured extractor endpoint for human review, gating
  `memory.dream.digest_enabled` on whether a small CPU sidecar's narrative
  prose is actually usable.
- `recall_cap_probe.py` (2026-08-25) — a synthetic-graph, DB-free
  measurement backing the `memory_recall` output-cap size claim (issue
  #186), reproducing the shape of the live audit without a daemon or bank
  (`evals/results/recall-cap-186-payload-probe.json`).
- `snippet_differential_replay.py` (2026-08-30) — replays a bank's pending
  merge proposals through the real snippet-attachment path, before/after,
  to measure low-differential evidence share
  (`evals/results/snippet-differential-live-20260830.json`).
- `queue_judge_fulllen_pack.py` (2026-09-03) — rebuilds a queue-judge
  evidence pack with full-length merge snippets, recovering the
  2026-09-02 panel's 240-char-clipped rows by prefix match against the
  bank they were built from, feeding the fulllen ladder rerun above.

---

# Agent-side token ledger (`agent_token_ledger.py`)

Every "fewer tokens" number this repo publishes measures **served benchmark
context** — the passage an answerer model reads to answer a LongMemEval or
BEAM question. Nothing measured the other side of the wire: what a real MCP
client reads *back* from a tool call, and pays for on every call, forever.
This ledger measures that side, and the payload cuts below were chosen from
it rather than from taste.

```bash
python evals/agent_token_ledger.py --daemon http://127.0.0.1:8765 \
    --out evals/results/agent-token-ledger-20260904-r3.json
```

The cited artifact is
`evals/results/agent-token-ledger-20260904-r3.json`. Two earlier runs stay
committed as **pre-review records** and are cited by no number below:

* `agent-token-ledger-20260904.json` (r1) measured the lean
  `memory_fact_get` projection while it was still dropping `source_entries`,
  and picked its five widest slots from a 2,000-row prefix of the fact dump
  rather than from the whole cortex. Both were fixed; the `fact_get` row
  moved as a result and says so in place.
* `agent-token-ledger-20260904-r2.json` measured `superseded_by_text`
  truncated to the same 600 chars as the entry's own text. That behaviour
  was **corrected before merge** — the field has no recovery path, since a
  compact entry carries no id for the superseding entry — so its headline
  (−41%) priced a payload this repo does not ship. The r3 run below prices
  the shipped one. (One slot label in r2 was redacted in place after the
  fact: it was a bare machine name, which `safe_label` did not catch until
  the same review taught it hostnames.)

The script refuses to overwrite an existing `--out`, which is why each
rerun is a new tag rather than a rewrite.

**Method.** Raw payloads are fetched once from the daemon's GET-only REST
(`/api/search`, `/api/recall`, `/api/facts`), then projected offline through
the MCP layer's own pure helpers (`mcp_server._project_search`,
`_lean_fact_record`, the `_cap_recall_*` family), so before/after is exactly
paired — same bytes in, two projections out. GET-only is not side-effect
free: `/api/search` runs the real retrieval path, so it appends
`retrieval_events` rows and touches per-entry access counters. It changes no
bank *content* — nothing is written, moved or reinforced. Sizes are
characters of the compact JSON an MCP client receives; approximate tokens
are `chars // 4`, the `ladder_sweep.approx_tokens` convention. Queries are a
fixed, committed list of 15 dev-session questions, deliberately **not** a
sample of the `retrieval_events` table: this is a public repo and real
queries carry paths and names. Numbers are bank-specific (measured on the
maintainer's live bank, 1,316 entries, `preset: flat`) and the artifact
records the entry count so a rerun elsewhere is not read as a regression.
The two cuts' parameters are read from `utils.config.McpConfig` rather than
restated in the harness — the values used are written to the artifact's
`config` block — so a future change to `entry_text_chars` re-prices the run
instead of quietly leaving the published numbers describing the old default.

## What a session costs before it asks anything

| Surface | chars | ~tokens |
| --- | --- | --- |
| tool manifest, `minimal` tier (9 tools) | 7,015 | 1,753 |
| tool manifest, `core` tier (22 tools) | 14,076 | 3,519 |
| tool manifest, `full` tier (35 tools) | 22,719 | 5,679 |
| served session-start block (`MEMORY_LOOP_BLOCK`) | 7,492 | 1,873 |

The manifest split is roughly two-thirds tool descriptions, one-third
inputSchema parameter descriptions (full tier: 14,523 + 8,196). Both halves
are already metered per tier by
`tests/test_tool_consolidation.py::test_descriptions_fit_tier_budgets`; this
ledger reads them through the same path so the two cannot disagree.

The session-start row is **raw** characters, not the JSON encoding the rest
of this page counts: the hook writes that block into the session as plain
text, so the escaping is not paid. (Its JSON size, 7,644, is in the artifact
under `chars` for comparability and is not the cost.) The block is capped at
`HOOK_CONTEXT_MAX_CHARS - 2,000` = 7,500 raw chars by
`tests/test_plugin_packaging.py`, which is why it is the one surface here
with almost no headroom.

## What a call costs — before and after the cuts

Mean over the 15 queries, `memory_search` at the tool's default `top_k=8`:

| Payload part | before | after | change |
| --- | --- | --- | --- |
| **total** | **14,745** | **9,951** | **−33%** |
| entries block | 12,637 | 7,842 | −38% |
| — entry `text` | 9,464 | 4,550 | −52% |
| — `superseded_by_text` | 2,406 | 2,406 | — |
| — entry metadata | 767 | 887 | +16% |
| cortex block | 1,853 | 1,853 | — |
| approx tokens | 3,686 | 2,487 | −33% |

Median total 15,325 → 9,613; p90 18,886 → 12,583. Entry `text` alone was
**64% of the whole payload**. The metadata line goes *up*, on purpose: the
`truncated: true` marker is what tells the reader that `memory_get` has more.

The `superseded_by_text` line is **exempt from the cap** and is why the
headline is 33% rather than the 41% the r2 run reported. It is a sixth of
the "before" payload and a quarter of what ships, so capping it looked like
free money — but it has no recovery path. A compact entry carries no id for
the superseding entry and nothing stores a pointer to one, so
`memory_get(entry.id)` returns the *superseded* text, not the replacement:
a clipped correction is unrecoverable by any tool call in any tier. Three
surfaces tell agents to prefer that field over the entry's own text (the
served session-start block, `examples/CLAUDE.memory.md`, and
`memory_search`'s own description), and 13 of these 15 queries had at least
one clipped under r2 (2,406 → 1,199 chars mean). It was published as a row
here rather than left inside "entries block" because the r2 breakdown left
those ~2,400 chars unlabelled between the block total and text + metadata
(2026-09-04 review finding).

One approximation, named: the narrow arm slices the width-5 cortex list
`/api/search` returns rather than re-running `cortex_search` at width 3, so
it would diverge from a real call on a bank where constraint pinning
re-budgets. The measured bank carries **0 of 5,509** labelled current facts,
so `_pin_constraint_facts` is a no-op and the two are the same set in the
same order. That validity condition is now counted by the run itself and
recorded in the artifact (`bank.facts_labelled` / `bank.facts_current`, with
`bank.facts_dump_truncated` false so the census saw the whole cortex) rather
than hand-checked; read this arm only while `facts_labelled` is 0.

At `top_k=3` — where the cortex-block narrowing actually bites, since
`min(5, top_k)` is inert at the default:

| Payload part | before | after | change |
| --- | --- | --- | --- |
| **total** | **6,870** | **4,290** | **−38%** |
| entry `text` | 3,537 | 1,712 | −52% |
| `superseded_by_text` | 931 | 931 | — |
| cortex block (5 facts → 3) | 1,853 | 1,107 | −40% |

`memory_fact_get`, over the five widest current slots in the bank: **2,175 →
1,296 chars** mean (median 2,281 → 1,128), a 40% cut from moving provenance,
support, writer/session id, tx/valid time and the supersession chain behind
`verbose=True` — 25 keys down to 12 or 13.

That cut is smaller than the r1 run reported (1,424 → 764, 46%), for
two reasons, both corrections rather than regressions. The projection now
keeps `source_entries`, the engram links: it is the only handle from a fact
back to the episodes that formed it, and the poisoned-memory procedure in
`docs/guide/security-posture.md` ("follow the engram links"), `memory_get`'s
core-tier justification, and
`tests/test_release_ux.py::test_core_tier_can_close_its_own_loops` all
depend on it being served by default. And the five widest slots are now
chosen from the whole cortex rather than from the first 2,000 rows the fact
dump returned, so both arms are measured on genuinely wider records.

Both arms price the RECORD, not the whole call, and in the same direction:
the "before" is the `/api/facts` dump row (`service.cortex_dump`), which
carries an `entity_id` the served `memory_fact_get` record never has, and
neither arm includes the tool envelope — `{record, contenders}` plus
`correct_with` and the correction note on an aged fact. Read the percentage
as the claim and the absolute chars as a floor. Closing either gap needs a
live service bound to the bank, which this script deliberately does not
have.

## The cap, and why 600

Served entry `text` runs mean **1,180** chars, median 1,149, p90 1,794 over
the 120 entries the 15 queries returned. A 600-char cap therefore clips 88%
of hits on this bank — deliberately: these are consolidated notes, not
one-liners, and 600 chars (~150 tokens) is enough to judge a hit and usually
to act on it, with `memory_get` for the rest. `memory_recall` has capped its
supporting texts at 200 since 2026-07-10 for the same reason; search entries
are the primary answer rather than walk evidence, so they get the wider cap.

## `memory_recall` is the expensive one

A 3-hop `memory_recall` issues **35 `service.search` calls on average** and
up to **66** on a single question — one seed search plus one per entity
newly discovered on each hop (`run_recall` + `MechanicalController.next_queries`;
derived from the response's `entity_hop`, not instrumented). Two of the five
relational questions resolved no seed entity and cost 1 search each; the
other three cost 50, 58 and 66. The *response* is already lean by comparison
— 4,243 chars mean against 10,349 for the same walk with `verbose=True` —
because the recall caps landed on 2026-07-10 and in #186. The call
amplification is untouched here and is the obvious next lever.

## What this does **not** measure

- Ranking, `min_score`, or anything an accuracy number depends on. Every cut
  is a projection above `service.*`; the eval harness calls the service
  directly, pinned by
  `tests/test_agent_payload_budget.py::test_eval_harness_does_not_read_the_mcp_projection`.
- Real client tokenisation. `chars // 4` is the house approximation, not a
  tokeniser.
- Whether a clipped hit ever costs an answer. That needs an end-to-end run
  with an agent in the loop, and is not attempted here.
