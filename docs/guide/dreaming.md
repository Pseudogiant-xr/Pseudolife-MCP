# Dreaming — consolidating memories into facts

The dream pass, its extractor tiers (regex floor / agent-driven / headless
auto-sweep), the bundled CPU sidecar, upgrading to a bigger model, the
Sonnet-primary fallback setup, cadence, deep dream, and the deliberate
consolidation workflow. Part of the [user guide](../../README.md#documentation).

A **dream** distils the recent associative stream (MIRAS) into canonical
cortex facts: pull unconsolidated memories → extract
`(entity, attribute, value)` → `memory_fact_set` → advance a monotonic
cursor so each memory is processed once. Because it keys on the **cursor**,
not on "sessions", returning to an old session later just appends more
tail — nothing is reprocessed, and there is no "session finished" event to
detect.

Extraction is pluggable; pick the tier that fits — the stack ships with
tier 2 preconfigured (the extractor sidecar), and **no self-hosted model is
required** if you'd rather not run one:

| Tier | How it runs | Needs | Quality |
|------|-------------|-------|---------|
| **0 — baseline** | `memory_dream(action="run")` (regex floor) — headless, on-box, free | nothing | weak (`X is Y`, `key: value`, port/version) |
| **1 — agent-driven** | the **agent itself** is the gateway: the `/dream` command | the agent you already run | highest |
| **2 — shipped default** | daemon auto-sweep calls an OpenAI-compatible endpoint — the bundled sidecar out of the box, or any endpoint you point it at | nothing (sidecar) / one base-URL + key + model | high; free if local |

**Tier 1 — `/dream` (agent-driven).** Copy `examples/commands/dream.md` to
`.claude/commands/dream.md` in any project, then run `/dream`. The agent
reads `memory_dream(action="pull")`, extracts durable current-state facts,
writes them with `memory_fact_set`, and commits the cursor. To run it on a
cadence instead of by hand, point a scheduled agent/cron job at the same
prompt.

**Tier 0 — zero-config.** Call `memory_dream(action="run")` (or schedule
it) for a fully headless pass with the deterministic regex floor — no LLM,
nothing leaves the machine.

## Tier 2 — headless auto-sweep

Point the daemon at any OpenAI-compatible endpoint and it dreams on its
own — no agent, no manual trigger:

```powershell
$env:PSEUDOLIFE_DREAM_BASE_URL = "http://localhost:11434/v1"   # e.g. Ollama
$env:PSEUDOLIFE_DREAM_MODEL    = "qwen2.5:7b"
# $env:PSEUDOLIFE_DREAM_API_KEY = "sk-..."           # hosted endpoints (Haiku, OpenRouter, ...)
# $env:PSEUDOLIFE_DREAM_TIMEOUT_SECONDS = "240"      # raise for a slow CPU / big model (default 240)
# $env:PSEUDOLIFE_DREAM_MAX_TOKENS      = "2048"     # extractor output budget (default 2048)
```

The daemon runs a background sweep every
`memory.dream.sweep_interval_seconds`; each tick it checks the same
backlog+quiescence trigger and, if it fires, runs a dream with the
configured extractor. Under the single-writer cortex a *successful* pass
that finds no canonical facts writes nothing and advances the cursor; a
**failed** call (timeout, network, malformed output) instead **holds the
cursor**, so those memories are retried next sweep rather than skipped —
there is no regex fallback either way. The extractor timeout defaults to
**240s** in code; the Docker stack ships **480s**
(`PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` in the compose file) because the
default E4B sidecar generates at ~12–15 tok/s on CPU, so a full
`PSEUDOLIFE_DREAM_MAX_TOKENS` generation runs ~150–170s — raise it further
for slower hardware. The same env vars also upgrade
`memory_dream(action="run")`. A local model keeps all text on-box; a hosted
endpoint does not.

## The CPU extractor sidecar (batteries-included default)

The stack ships a llama.cpp sidecar with a model baked in (the bespoke
Gemma 4 E4B extractor fine-tune, ~5.3 GB — see "Upgrading the extractor"
below for the lighter E2B bake), and `ops/docker-compose.yml` starts it by
default and routes dream consolidation to it. It's internal-only (never
published to the host). Single-writer cortex relies on it: with no
extractor configured, the cortex is populated only by `memory_fact_set` and
the daemon logs a startup warning. Reasoning models work too — the
extractor disables their `<think>` trace so they return structured output
instead of an empty budget. The `evals/` extractor-ladder benchmark is how
the default was chosen (even the smallest bake, Gemma 4 E2B, beats
naive-RAG at ~25× fewer tokens/query); see
[`evals/README.md`](../../evals/README.md).

## Upgrading the extractor — bigger local models

If you have a GPU (or a beefier box on your LAN), any OpenAI-compatible
server can replace the sidecar — the ladder measured a Qwen3.6-27B on a
single RTX 4090 at the ladder ceiling (gold 1.0 / stale-leak 0.0) while
extracting ~5× faster than the CPU sidecar. The win is speed, not recall:
in the replicated LongMemEval-KU comparison
([`evals/README.md`](../../evals/README.md), 2026-07-18) the bundled
fine-tune outscores the generic 27B class end-to-end (hybrid 0.762 ± 0.027
vs 0.695 ± 0.017), so point at a bigger *generic* model for faster dreams,
not better answers. Two ways to switch:

*From the Console (no restart):* the **Extractor** panel in the Cortex
Console's config view edits the endpoint, model, timeout, and token budget
live — flip its "Settings source" switch to `config` first (while it is
`env`, the default, the `PSEUDOLIFE_DREAM_*` variables below own the
settings and the panel's values are ignored). The API key stays env-only
either way.

*Via env:* for the Docker stack, set the override in `ops/.env` (the
compose file interpolates it into the daemon) and restart the daemon
(`docker compose -f ops/docker-compose.yml up -d --no-deps pseudolife-daemon`):

```dotenv
# ops/.env — point dream consolidation at a local model server.
# From inside the container the host machine is host.docker.internal, NOT
# localhost (works on Linux too via the extra_hosts entry shipped in
# ops/docker-compose.yml).
PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:1234/v1
PSEUDOLIFE_DREAM_MODEL=qwen3.6-27b
```

Per-runtime defaults (all serve the same `/v1/chat/completions` shape):

| Runtime | Typical base URL (from the container) | `PSEUDOLIFE_DREAM_MODEL` |
|---------|----------------------------------------|--------------------------|
| **LM Studio** | `http://host.docker.internal:1234/v1` | the model's API identifier shown in LM Studio's server tab |
| **Ollama** | `http://host.docker.internal:11434/v1` | the tag, e.g. `qwen2.5:14b` |
| **llama.cpp** (`llama-server`) | `http://host.docker.internal:8080/v1` | anything (single-model server ignores it) |
| **vLLM** | `http://host.docker.internal:8000/v1` | the `--served-model-name` |
| LAN box | `http://192.168.x.x:PORT/v1` | per the runtime above |

The unused sidecar can be stopped (`docker compose -f ops/docker-compose.yml
stop pseudolife-extractor`) or left running as a fallback to switch back to.
The default bake is the bespoke
[Pseudolife extractor fine-tune](https://huggingface.co/Pseudogiant-xr/pseudolife-extractor-gemma-4-e4b)
(Gemma 4 E4B QLoRA); constrained machines can bake the lighter **Gemma 4
E2B QAT** instead (also ladder-verified) — see the `MODEL_URL` build-arg in
`ops/Dockerfile.extractor`, or mount any GGUF over `/models/extractor.gguf`
via a machine-local `ops/docker-compose.override.yml` (gitignored; example
in the compose file). If you run the daemon *outside* Docker (embedded
stdio mode), the `$env:` variables above apply directly and `localhost`
URLs work as-is. A local or LAN model keeps all memory text on your
network; the same env triple pointed at a hosted endpoint does not.

## Sonnet primary with local fallback

With a Claude Max plan, the dream pass can use Claude Sonnet as its primary
extractor and keep the bundled local sidecar as an automatic fallback. The
installer does all of this in one go —
`ops/install.sh --extractor sonnet-fallback` (or `sonnet-only` to skip the
sidecar entirely; `ops\install.ps1 -Extractor ...` on Windows). The manual
steps:

1. Register the CLI shim (`evals/sonnet_shim.py`) to start automatically —
   requires a logged-in `claude` CLI:
   - Windows: `ops\install-shim-autostart.ps1` (Task Scheduler, at logon,
     `127.0.0.1:8082`).
   - Linux: `ops/install-shim-autostart.sh` (systemd `--user` unit; binds
     the docker bridge IP so the daemon container can reach it —
     `host-gateway` routes container→host traffic to the bridge, where a
     loopback bind is invisible).
2. Set in `ops/.env` (both vars must flip together — pointing only one at
   the shim leaves dreams silently on the sidecar):
   `PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:8082/v1`,
   `PSEUDOLIFE_DREAM_MODEL=extractor`,
   `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1`,
   `PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor`,
   `PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto` (or `primary`/`fallback` to force
   a side — also switchable live in the Console's Extractor panel).
3. Redeploy (`ops/update.ps1` / `ops/update.sh`), then **verify**:
   `memory_dream(action="status")` should show `fallback_url` populated
   and, with the shim up, `primary_healthy: true`; after the next dream,
   `last_dream_extractor.which` should read `primary` against the `:8082`
   URL. The daemon also logs a startup warning for the common
   half-configurations (unresolvable `host.docker.internal`, `auto` without
   a fallback, primary == fallback).

When the shim is unreachable or the CLI is logged out, dreams automatically
use the fallback; the Console's Observatory shows which extractor is
active. Leave `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL` unset to keep the
existing single-extractor behavior.

## Cadence — quiescence-gated, daemon-only

What gets consolidated and when is configurable under `memory.dream`
(`eligible_sources` / `exclude_sources`, and the `min_batch` /
`idle_seconds` backlog+quiescence thresholds that
`memory_dream(action="status")` reports).

The auto-sweep (Tier 2) fires when:

```
backlog ≥ min_batch (8)   OR   (backlog ≥ 1 AND idle ≥ idle_seconds (600s))
```

polled every `sweep_interval_seconds` (600s). It runs **only in the
daemon** — the embedded stdio mode never sweeps. There is **no turn-based
trigger** (the cortex does not "dream every N turns"), by design:
consolidating mid-session would distil half-formed, still-changing state
into canonical facts and burn the CPU extractor during your foreground
work. So during an active session, prose-stored facts stay in the
searchable bands and reach the cortex once you go quiet (~10 min idle) or a
backlog of 8 accumulates.

**Want a fact canonical *now*, mid-session?** Two on-demand paths bypass
the wait: `memory_fact_set` writes a canonical fact instantly, and
`memory_dream(action="run")` forces a full consolidation sweep on the spot
(the `/dream` command wraps it). `memory_search` finds the original prose
the entire time regardless.

**Privacy & cost.** Tier 0 is on-box and free. Tier 1 spends the agent
tokens you already pay for (a scheduled daily dream is small but non-zero).
Tier 2 with a *cloud* endpoint sends memory text off-box — a local model
(e.g. Ollama) keeps it on-machine.

## Deep dream — full-corpus graph consolidation

The incremental dream (tiers above) is window-local: it distils only the
recent MIRAS tail into cortex facts. `memory_dream(action="deep")` is a
separate, manually-triggered full-corpus GRAPH pass (Phase-2 'C'). A
dry-run (default) returns a preview of what it would change: re-scored
edges, hard type-violation edges queued for supersession, exact-duplicate
entity pairs queued for merging, and semantic link *candidates* across
sessions (each with truncated context snippets; items the apply path would
dedupe are flagged `already_proposed`). Adding `apply=True` first dumps the
five graph tables to a JSON undo file under `data_dir/graph_snapshots/`
(refusing with `snapshot_failed` if it can't), then commits the safe
self-clean (re-score + supersede violations + merge exact dups) and returns
`candidates` for review. The agent then drives Step C in the same session
(see the `/dream deep` flow in `examples/commands/dream.md`): judge each
candidate from its snippets, post the real relations with
`memory_graph_review(action="propose")` — they land in the Atlas Review
queue (`proposed_link` findings) for per-item accept/reject before anything
reaches live edges — and record clearly-distinct pairs with
`memory_graph_review(action="dismiss_pair")` so they stop resurfacing. See
[the deep-dream runbook](../runbooks/deep-dream.md) for the operator
procedure.

## Consolidation workflow (agent-driven dedup)

Long-running banks accumulate near-duplicate memories — the same fact
phrased five different ways across five sessions. The literature on
agent memory ([HiMem 2026](https://arxiv.org/abs/2601.06377);
[MIRIX 2024](https://arxiv.org/abs/2507.07957); the
[ICML 2025 position paper](https://arxiv.org/abs/2502.06975)) calls
consolidation — turning episodes into reusable semantic notes — *the*
most-important under-implemented capability of long-term LLM memory.

The dream pass (the extractor sidecar) handles fact extraction server-side,
but the server can't borrow *Claude's* judgment mid-call (Claude Code
doesn't yet expose MCP sampling — see
[feature request #1785](https://github.com/anthropics/claude-code/issues/1785)),
so near-duplicate cleanup is surfaced as clusters for Claude to consolidate
deliberately:

```
memory_consolidation_candidates(query="MCP transport choice", top_k=20)
# → {clusters: [{cohesion: 0.84, size: 3, members: [<entry>, ...]}, ...]}

memory_consolidate(
  replaces=["MCP uses stdio transport", "stdio was chosen for MCP", "decided on stdio for MCP"],
  new_text="MCP transport is stdio — chosen over TCP to avoid port conflicts.",
  tags=["consolidated"],
)
# → {superseded_count: 3, new_memory_stored: true, ...}
```

The clustering is deterministic greedy: highest-relevance entry seeds
the cluster, any unclustered candidate whose cosine with the seed
clears `min_cohesion` (default 0.6) joins, cohesion is the mean
intra-cluster cosine, clusters are sorted by `cohesion × size`. Cost
is O(N²) within the candidate pool, bounded to `top_k` candidates.

`memory_consolidate` reuses the supersession machinery so the
predecessors stay in the bank but rank below the canonical note —
the audit trail survives but retrieval defaults to the current
phrasing. Useful idiom: tag the consolidation with `["consolidated"]`
so you can later scan with `memory_search(..., tags=["consolidated"])`
to see what's been distilled.
