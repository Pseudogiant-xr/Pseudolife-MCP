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
| `qwen-a3b`  | Qwen3.6-35B-A3B (homelab 5800X3D)  | `http://192.168.0.130:1236/v1` |
| `qwen-27b`  | Qwen3.6-27B (4090)                 | `http://192.168.0.10:1234/v1`  |

The cloud rung is intentionally omitted — this is a sovereign-only sweep.

`gemma-e2b` and `gemma-e4b` share the **same** `:8081` endpoint: the operator
swaps the served GGUF between the two runs (see below). Run one, then the
other.

## Prerequisites

The benchmark talks to a **host-published** llama.cpp on `127.0.0.1:8081`.
Note this is *separate* from the opt-in compose sidecar
(`docker compose --profile extractor`), which is internal-only (`expose:`, not
`ports:`) and reachable only by the daemon on the compose network.

**Gemma E2B** (default baked image):

```bash
docker run -d --name pl-extractor-bench -p 127.0.0.1:8081:8081 \
  pseudolife-extractor:gemma4-e2b
```

**Gemma E4B** — stop the E2B container, then serve the E4B GGUF on the same
port. Either bake a second image:

```bash
docker build -f ops/Dockerfile.extractor -t pseudolife-extractor:gemma4-e4b \
  --build-arg MODEL_URL=https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf ops
docker rm -f pl-extractor-bench
docker run -d --name pl-extractor-bench -p 127.0.0.1:8081:8081 \
  pseudolife-extractor:gemma4-e4b
```

…or mount any GGUF over the baked default without a rebuild:

```bash
docker rm -f pl-extractor-bench
docker run -d --name pl-extractor-bench -p 127.0.0.1:8081:8081 \
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

The abstention sub-sweep (`--abstain`) sweeps `search_confidence_floor` over
`{0.0, 0.5, 0.65, 0.70, 0.75, 0.80}` (the floors bracket the embedder's actual
score distribution — answerable max-scores 0.75–0.98, unanswerable 0.38–0.78;
floors below ~0.5 never fire) and reports, per floor:

- **`abstain_recall_unanswerable`** ↑ — fraction of never-stated probes that
  correctly return `low_confidence=True`.
- **`false_abstain_answerable`** ↓ — fraction of answerable questions wrongly
  flagged low-confidence.

Pick the highest floor that keeps `false_abstain_answerable` at/near zero.

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
  (future work).
