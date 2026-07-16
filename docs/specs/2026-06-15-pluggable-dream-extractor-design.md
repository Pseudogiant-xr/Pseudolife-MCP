# Pluggable dream extractor — design spec

Status: **proposed (design)**, pending review + plan.
Target: **Pseudolife-MCP** `origin/master`. Core-only; no external-agent deps.
Touches `pseudolife_memory/service.py`, `mcp_server.py`, `daemon.py`,
`utils/config.py`, a new `pseudolife_memory/memory/dream.py` (extractor
abstraction + LLM client), `commands/` (a `/dream` recipe), docs, and tests.

## 1. Problem

The dream pass — consolidating recent associative (MIRAS) memories into
canonical cortex facts — is real in the engine but **headless-incomplete** for a
public user:

- The extraction work exists only as `MemoryService` methods (`dream_pull`,
  `extract_slots_regex`, `dream_commit`) and is **not exposed as MCP tools**.
- The comment in `service.py` says the LLM step is "gateway-side." That was true
  for an external agent deployment, where the agent's *provider* is the gateway
  and routes extraction to a background model. **A public install has no such
  gateway and no self-hosted model.**
- The README/CHANGELOG advertise dreaming, but out of the box nothing runs it.

The hosting assumption is the trap: dreaming needs *an* LLM, not a *self-hosted*
one. A public user of an MCP server already has the most capable LLM attached —
**the agent they run it from** — and may also have a cheap API key or a local
Ollama. None of them should be *required*.

## 2. Goal

A single dream code path with a **graceful fallback chain** so that:

- **Zero-config installs still consolidate** (regex floor + embedder-only dedup),
  fully headless, no LLM, no cost, no data leaving the box.
- **The default high-quality path is the agent itself** (`/dream` command or a
  scheduled routine) — uses the LLM the user already pays for, zero infra.
- **Power users can go fully headless and high-quality** by pointing the daemon
  at any OpenAI-compatible endpoint (Ollama, LM Studio, Anthropic/Haiku,
  OpenRouter, Groq, *or* a self-hosted model — all the same slot).

Non-goals: changing the continuum/bands or the cortex write path; multi-slot
reasoning; any external agent provider (those live in their own repos, keep
their own background routes, and are unaffected).

## 3. The three tiers

| Tier | Extractor | Needs | Quality | Where it runs |
|---|---|---|---|---|
| **0 — baseline** | `extract_slots_regex` + embedder-only `memory_consolidate` | nothing (embedder already ships) | weak (`X is Y`, `key: value`, port/version) + real dedup | daemon sweep (headless) |
| **1 — default** | the agent (Claude Code / any MCP client) is the gateway | the client already in use | highest | `/dream` command or scheduled routine |
| **2 — opt-in** | BYO OpenAI-compatible endpoint | one base-URL + key + model | high; free if local | daemon sweep (headless) |

Tier 2 generalizes the background-route pattern: a self-hosted model is now just
one backend behind a config triple, indistinguishable from Ollama or Haiku.

## 4. Extractor abstraction (`memory/dream.py`)

```python
class DreamExtractor(Protocol):
    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]: ...
    # Claim = {entity, attribute, value, origin, confidence}
```

Implementations:

- `RegexExtractor` — wraps the existing `extract_slots` floor (Tier 0). Always
  available; `origin="agent"`, `confidence≈0.55`.
- `OpenAICompatExtractor` — Tier 2. POSTs a slot-extraction prompt to
  `<base_url>/chat/completions` with `response_format=json_object`, parses claims,
  reuses `vocab` (from `cortex_vocab`) so the model **reuses existing slot keys**
  instead of reinventing them. Bounded by `max_tokens` + hard timeout; on any
  failure returns `[]` so the caller falls back to the regex floor — never raises
  into the daemon.

Tier 1 has no extractor object: the agent *is* the extractor and calls
`memory_fact_set` directly (see §6).

The shared driver — pull → extract → `fact_set` → `dream_commit` — lives once in
`service.dream_run(extractor)`, so all tiers share cursor discipline and the
regex fallback.

## 5. Config surface (`utils/config.py`)

New `DreamConfig` (sibling of `ReflectionConfig`):

```python
@dataclass
class DreamConfig:
    enabled: bool = True              # the headless sweep (Tiers 0/2)
    # Tier 2 extractor — all None => Tier 0 regex floor only.
    extractor_base_url: str | None = None   # env PSEUDOLIFE_DREAM_BASE_URL
    extractor_api_key: str | None = None    # env PSEUDOLIFE_DREAM_API_KEY
    extractor_model: str | None = None      # env PSEUDOLIFE_DREAM_MODEL
    max_tokens: int = 400
    timeout_seconds: float = 20.0
    # Sweep trigger (see §6).
    sweep_interval_seconds: float = 600.0   # daemon checks every 10 min
    min_batch: int = 8                       # fire if backlog >= this
    idle_seconds: float = 1800.0             # or backlog>=1 and quiet >= 30 min
    max_batch: int = 40                      # cap one dream's pull
```

Env vars override the dataclass so a user enables Tier 2 without editing config.
Default example in docs points at a **cheap** model (Haiku) so "headless" never
silently means "expensive."

## 6. Triggering — the cursor makes sessions irrelevant

There is no "session finished" event in an MCP client (a `Stop` hook fires per
*turn*, and sessions are resumable). So the dream never keys on sessions; it keys
on the **`cortex.dream_cursor`** high-water timestamp. The trigger is *backlog +
quiescence*, evaluated by whoever drives it:

```
backlog = count(entries where source="conversation" and ts > dream_cursor)
idle    = now - max(entry ts)
fire if  backlog >= min_batch  OR  (backlog >= 1 and idle >= idle_seconds)
```

This handles a long active session (fires on backlog), a quick session then
walk-away (fires on idle), and **returning to an old session days later** (its
new stores are just more tail past the cursor — picked up next sweep, never
reprocessed).

- **Tiers 0/2 (headless):** `daemon.py` runs a background timer every
  `sweep_interval_seconds`; on a fire it calls `service.dream_run(extractor)`.
- **Tier 1 (agent):** no daemon LLM, so the agent drives it:
  - `/dream` slash command — manual, user-paced.
  - a scheduled routine — daily cron agent running the dream prompt.
  - **nudge hook (optional):** a `SessionStart` hook reads backlog via a new
    read-only tool and injects *"N memories worth consolidating — run /dream"*
    into context. Event-ish, free, non-blocking.

## 7. MCP tool surface (`mcp_server.py`)

Expose the dream so any agent can be the gateway (Tier 1) and so the nudge hook
can read state:

- `memory_dream_pull(limit)` → recent unconsolidated turns (ts > cursor).
- `memory_dream_status()` → `{backlog, idle_seconds, dream_cursor, would_fire}`
  (read-only; safe for hooks).
- `memory_dream_commit(cursor)` → advance the cursor (monotonic).
- *(extraction itself is the agent reading the pull and calling the existing
  `memory_fact_set` — no new write tool needed.)*

These wrap methods that already exist; `memory_dream_status` is the only genuinely
new computation.

## 8. The `/dream` command (Tier 1 default)

A repo command that scripts the loop the agent runs by hand today:

1. `memory_dream_status` → if `would_fire` is false, report and stop.
2. `memory_dream_pull(max_batch)`.
3. Extract durable, slot-shaped, **current-state** claims (skip narrative,
   in-progress work, superseded states); reuse existing slot keys.
4. `memory_fact_set` each claim (origin reflects support; default `agent`).
5. `memory_dream_commit(newest_ts)`; report inserted / confirmed / contested.

## 9. Privacy & cost (state plainly in docs)

- **Tier 0:** nothing leaves the machine; no model cost.
- **Tier 1:** spends the user's existing agent tokens; a daily scheduled dream is
  small but non-zero — say so.
- **Tier 2 (cloud endpoint):** memory text is sent off-box to the chosen
  provider. **Local Ollama keeps it on-machine.** Make the trade explicit so the
  choice is deliberate.

## 10. Testing

- `RegexExtractor` + `dream_run` driver: pure, no LLM — cursor advance,
  idempotent re-run (identical slot = `confirmed`, not duplicate), regex fallback
  when extractor returns `[]`.
- `memory_dream_status` math (backlog/idle/would_fire) against a seeded PG bank.
- `OpenAICompatExtractor` against a stub HTTP server (claims parsed, timeout →
  `[]`, malformed JSON → `[]`).
- Sweep gate unit test (fires on batch, fires on idle, no-op when quiet+empty).
- All PG-backed tests pin `search_path` per the existing fixture contract.

## 11. Phasing

1. **Extractor abstraction + driver + tool surface** (`dream.py`, `dream_run`,
   the three MCP tools, `DreamConfig`). Tier 0 works headless end-to-end.
2. **`/dream` command + docs** (Tier 1 default; scheduled-routine recipe).
3. **`OpenAICompatExtractor` + daemon sweep** (Tier 2 + headless trigger).
4. **Optional nudge hook** recipe in docs.

Ship 1–2 first: every user gets baseline consolidation and an agent-driven dream
with zero new dependencies. 3–4 are additive and opt-in.
