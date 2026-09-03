# Dreaming — consolidating memories into facts

The dream pass, its extractor tiers (regex floor / agent-driven / headless
auto-sweep), the bundled CPU sidecar, upgrading to a bigger model, the
Sonnet-primary fallback setup, cadence, deep dream, and the deliberate
consolidation workflow. Part of the [user guide](../../README.md#documentation).

A **dream** distils the recent associative stream (MIRAS) into canonical
cortex facts: pull unconsolidated memories → extract
`(entity, attribute, value)` claims (a claim may also carry
`op: "add"|"remove"` to target a [set-valued
slot](memory-model.md#set-valued-slots) — solicited by the shipped prompt
since 2026-08-01, paired with a counts-are-never-members rule; see the
[memory model](memory-model.md#dream-extraction) for the measurement
story) → `memory_fact_set` → advance a monotonic
cursor so each memory is processed once. Because it keys on the **cursor**,
not on "sessions", returning to an old session later just appends more
tail — nothing is reprocessed, and there is no "session finished" event to
detect.

Extraction is pluggable; pick the tier that fits — the stack ships with
tier 2 preconfigured (the extractor sidecar), and **no self-hosted model is
required** if you'd rather not run one:

| Tier | How it runs | Needs | Quality |
|------|-------------|-------|---------|
| **0 — none** | no extractor configured — the dream still runs, prunes and advances its cursor, but writes no canonical facts | nothing | none (single-writer cortex: `memory_fact_set` is your only writer) |
| **1 — agent-driven** | the **agent itself** is the gateway: the `/dream` judgment session (its manual-extraction branch fires only when no endpoint is configured) | the agent you already run | highest |
| **2 — shipped default** | daemon auto-sweep calls an OpenAI-compatible endpoint — the bundled sidecar out of the box, or any endpoint you point it at | nothing (sidecar) / one base-URL + key + model | high; free if local |

**Tier 1 — `/dream` (agent-driven).** Copy `examples/commands/dream.md` to
`.claude/commands/dream.md` in any project, then run `/dream`. The command
is a **judgment session**: the agent reads `memory_dream(action="status")`
— including the `deep_dream: {recommended, ...}` nudge — runs the
mechanical graph pass if the tick hasn't, and works the review queues
(link candidates, merge proposals, junk, store curation). Its manual
extraction branch (`pull` → extract → `memory_fact_set` → `commit`) fires
ONLY when no extractor endpoint is configured or reachable — on such
deployments the agent is the sole cortex writer. To run it on a cadence
instead of by hand, point a scheduled agent/cron job at the same prompt.

**Tier 0 — no extractor.** With no endpoint configured the cortex has no
automatic writer: `memory_dream(action="run")` still drains the backlog,
prunes outcome signals and advances the cursor, but extracts no facts, and
the daemon logs a startup warning. Populate the cortex with deliberate
`memory_fact_set` calls, or configure tier 1 or 2.

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
up to three times. A batch that keeps failing is re-run entry by entry,
the individual offenders are quarantined, and the cursor advances past
them, so one unparseable memory cannot stall consolidation indefinitely.
There is no regex fallback either way. The extractor timeout defaults to
**240s** in code; the Docker stack ships **480s**
(`PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` in the compose file) because the
default E4B sidecar generates at ~12–15 tok/s on CPU, so a full
`PSEUDOLIFE_DREAM_MAX_TOKENS` generation runs ~150–170s — raise it further
for slower hardware. The same env vars also upgrade
`memory_dream(action="run")`. A local model keeps all text on-box; a hosted
endpoint does not.

Every extractor request the daemon builds carries `cache_prompt: false`
(`memory.dream.extractor_cache_prompt`, default `false`): llama-server's
prompt cache measurably changes extraction output once populated, and the
pin's cost — ~7s of shared-prefix prefill per call on the shipped sidecar
(`evals/results/sidecar-cache-latency-sidecar-cache-0809.json`) — is noise
for a background sweep. Set the knob to `null` to restore the server
default if your deployment prefers the latency; non-llama.cpp endpoints
ignore the field.

## What the extractor captures

The tier-2 prompt (`_SYSTEM_PROMPT` in `pseudolife_memory/memory/dream.py`,
shared by the bundled sidecar and any endpoint you point the daemon at)
asks for three things and deliberately skips the rest — narrative,
opinions, meta-chat about the conversation, and values a later note already
superseded:

- **Durable current-state facts**, one slot per real fact.
- **Updates, landed on the slot the fact already had.** When several notes
  state or update the same fact, only the *current* value is emitted, under
  the same entity and attribute — so the cortex supersedes rather than
  accumulating near-duplicate slots.
- **The source's epistemic stance, kept.** A hedged or negated claim
  ("probably X", "no longer Y") lands with a `stance` marker on the fact
  (schema v29; the v10 update-anchored prompt is the live one) instead of
  hardening into a flat assertion — see
  [memory-model — how current is this fact?](memory-model.md#how-current-is-this-fact).
- **What a document prescribes.** When a note quotes or summarizes a spec,
  policy, protocol, runbook, or guide, its prescription is itself a durable
  fact, stored under the *document's* subject — and kept separate from what
  was actually done. Paste your deploy runbook, then mention a deploy that
  skipped a step, and you get two facts (the documented rule, and the
  incident), not one blurred into the other.

That third one is deliberate, and it is the reason the prompt names its
content classes rather than merely forbidding noise: an extraction prompt
that enumerates what to extract makes an obedient model **silently discard
whatever it doesn't name** — no error, no partial result, just a class of
knowledge that never reaches the cortex. It cost a whole benchmark category
to find (see [Benchmarks](benchmarks.md#longmemeval-v2--agent-trajectories-and-procedures)),
and it is worth remembering before narrowing this prompt further.

The Sonnet override prompt (`evals/prompts/sonnet_extractor_v2.md`, used
when you run the shim below) carries the same three, tuned for a larger
model.

**Literal-faithfulness gate.** After extraction, every claim's digit-bearing
tokens (dates exempt — format variance makes digit matching unsafe there)
are checked against the pull's source notes: a fabricated number or
identifier is dropped and counted under the default
`memory.dream.literal_gate = "enforce"` (since 2026-08-02), or merely
counted under `"log"`.
The corpus is the whole batch's note union by default — derived sums and
cross-note values are measured false-drop classes under per-note gating.
The matcher normalizes the re-formattings extractors legitimately produce:
spelled numbers back digits ("three week" backs "3-week"), hyphenated
ranges and unit compounds gate per digit part ("1-3" ↔ "1 to 3",
"66-acre" ↔ "66 acres"), `N+` minimums match their base number, and
`~`-marked approximations are exempt like dates — classes triaged from the
at-scale firing probe (`evals/results/gate-firing-verdict.json`, where 15
of 17 batch-scope flags were normalization gaps, not fabrications).
The post-matcher re-probe left the survivors dominated by genuinely
unbacked literals — derived aggregates and imported world knowledge — at
1.3–1.7% of gateable claims, which is what made enforcement the default
(`evals/results/gate-firing-normfix-verdict.json`;
`literal-fidelity-verdict.json` has the original opt-in decision).
A companion prompt rule mandating verbatim literals was built, measured,
and **held** — it significantly degraded the KU cascade (same verdict
artifact).

## The CPU extractor sidecar (batteries-included default)

The stack ships a llama.cpp sidecar with a model baked in (the bespoke
Gemma 4 E4B extractor fine-tune, ~5.3 GB — multi-task since the v3 bake:
one adapter serves both the claims pass and the chronicle events pass —
see "Upgrading the extractor"
below for the lighter E2B bake), and `ops/docker-compose.yml` starts it by
default and routes dream consolidation to it. It's internal-only (never
published to the host). Single-writer cortex relies on it: with no
extractor configured, the cortex is populated only by `memory_fact_set` and
the daemon logs a startup warning. Reasoning models work too — the
extractor disables their `<think>` trace so they return structured output
instead of an empty budget. The `evals/` extractor-ladder benchmark is how
the default was chosen (even the smallest bake, Gemma 4 E2B, beats
naive-RAG at ~40× fewer tokens/query); see
[`evals/README.md`](../../evals/README.md).

The sidecar **unloads its model when idle**: after 5 minutes without a
request (llama-server `--sleep-idle-seconds`, tunable via
`PSEUDOLIFE_EXTRACTOR_SLEEP_IDLE_SECONDS` in `ops/.env`, `-1` = always
resident) the server frees the ~7 GB of weights and drops to a few hundred
MB. This matters most on shim installs where the sidecar is only the
*fallback* dreamer and would otherwise hold that memory around the clock
against a rare failure path. Waking is transparent and needs no operator:
`/health` keeps answering while asleep, and the next dream call simply
blocks while the model reloads (seconds on an SSD) — comfortably inside
`PSEUDOLIFE_DREAM_TIMEOUT_SECONDS`, so an unattended sweep that falls back
mid-run waits instead of failing. The first fallback dream after a long
idle is a little slower; nothing else changes.

## Upgrading the extractor — bigger local models

If you have a GPU (or a beefier box on your LAN), any OpenAI-compatible
server can replace the sidecar — the ladder measured a Qwen3.6-27B on a
single RTX 4090 at the ladder ceiling (gold 1.0 / stale-leak 0.0) while
extracting ~5× faster than the CPU sidecar — a bar the shipped bakes now
also clear, so the ladder no longer separates them; the separation is in
the LongMemEval numbers below. The win is speed, not recall: in the
replicated LongMemEval-KU comparison
([`evals/README.md`](../../evals/README.md), 2026-07-18) the bundled
fine-tune outscores the generic 27B class end-to-end (hybrid 0.762 ± 0.027
vs the 27B ceiling's 0.710 ± 0.019 — a same-stack comparison on the
since-retired TurboQuant server; point estimates from separate runs, not a
paired test, and not comparable to the ceiling's re-based 0.731), so point
at a bigger *generic* model for faster
dreams, not better answers. Two ways to switch:

*From the Console (no restart):* the **Extractor** panel in the Cortex
Console's config view edits the endpoint, model, timeout, and token budget
live — flip its "Settings source" switch to `config` first (while it is
`env`, the default, the `PSEUDOLIFE_DREAM_*` variables below own the
settings and the panel's values are ignored). The API key stays env-only
either way. The *model alone* needs no source flip: the **Dreamer** card at
the top of the same view writes a model-only override
(`memory.dream.extractor_model_override`) that wins over both owners while
the endpoint wiring keeps its owner.

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
(Gemma 4 E4B QLoRA — the v3 bake is multi-task, claims + dated events;
the v2 claims-only GGUF stays published on the same repo for rollback);
constrained machines can bake the lighter **Gemma 4
E2B QAT** instead (also ladder-verified) — see the `MODEL_URL` build-arg in
`ops/Dockerfile.extractor`, or mount any GGUF over `/models/extractor.gguf`
via a machine-local `ops/docker-compose.override.yml` (gitignored; example
in the compose file). If you run the daemon *outside* Docker (embedded
stdio mode), the `$env:` variables above apply directly and `localhost`
URLs work as-is. A local or LAN model keeps all memory text on your
network; the same env triple pointed at a hosted endpoint does not.

## Claude primary with local fallback

With a Claude Max plan, the dream pass can use a Claude model as its primary
extractor and keep the bundled local sidecar as an automatic fallback. The
installer does all of this in one go —
`ops/install.sh --extractor sonnet-fallback` (or `sonnet-only` to skip the
sidecar entirely; `ops\install.ps1 -Extractor ...` on Windows). The manual
steps:

1. Register the CLI shim (`evals/claude_shim.py`) to start automatically —
   requires a logged-in `claude` CLI:
   - Windows: `ops\install-shim-autostart.ps1` (Task Scheduler, at logon,
     `127.0.0.1:8082`; needs an elevated PowerShell — open it fresh from
     the Start menu, never from a terminal inside Claude Desktop or another
     Store-packaged app, or that app's next update fails to launch until a
     reboot — see
     [anthropics/claude-code#61635](https://github.com/anthropics/claude-code/issues/61635);
     `-Model` picks the served default —
     `claude-opus-5` since the 2026-08-02 dreamer comparison; the one-shot
     installer prompts for this choice on Claude-shim installs).
   The shim also honors a concrete `claude-*` model named per request, so
   the Console's **Dreamer** card switches the dreamer live — one click
   between `claude-opus-5` / `claude-sonnet-5` / `claude-haiku-4-5` /
   `claude-fable-5` (or any `claude-*` name typed in), no shim restart and
   no settings-source flip; alias names like the compose default
   `extractor` keep the launch model.
   - Linux: `ops/install-shim-autostart.sh` (systemd `--user` unit, same
     `--model` choice; binds the docker bridge IP so the daemon container
     can reach it — `host-gateway` routes container→host traffic to the
     bridge, where a loopback bind is invisible).
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

## OpenAI primary — the Codex CLI shim

The same pattern works on an OpenAI subscription: `evals/codex_shim.py` is
the ChatGPT-plan twin of the Claude shim. It wraps headless `codex exec`
(the signed-in Codex CLI's included usage — no API key) as an
OpenAI-compatible endpoint on `127.0.0.1:8086`, serving `gpt-5.6-terra` by
default. The one-shot installer wires the whole mode:

```bash
ops/install.sh --extractor codex-fallback     # or codex-only; Windows: ops\install.ps1 -Extractor codex-fallback
```

which prompts for the GPT-5.6 dreamer (Sol / Terra / Luna), registers the
shim to start automatically (`ops/install-codex-shim-autostart.ps1` — Task
Scheduler, elevated pwsh opened from the Start menu, same caveat as the
Claude shim above; `.sh` — systemd `--user`, docker-bridge bind),
and writes the env triple for you. The autostart raises the shim's
health-probe interval to 1800 s (`--health-ttl`) because every `/health`
refresh is a real CLI call — metered spend on a free ChatGPT tier; a
stale-ok window only costs one failed primary attempt before the dream
falls back. To run it by hand instead:

```bash
python evals/codex_shim.py    # --model gpt-5.6-sol / gpt-5.6-luna to change the default
```

then point the env triple at it exactly as in step 2 above, with
`PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:8086/v1` (and, on
Linux, the same docker-bridge bind note as the Claude shim — pass `--host`
accordingly). Either way the shim honours a concrete `gpt-*` or `codex-*`
name per request, so the Console's **Dreamer** card switches between
`gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` live, exactly like the
Claude presets. On Windows the shim finds the official installer's
`codex.exe` on its own (the `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\`
layout is off PATH and rotates on auto-update — the shim re-resolves the
newest at every start).

Extraction quality: the `terra` and `luna` ladder rungs
(`evals/ladder_sweep.py --rung terra` / `--rung luna`, first measured
2026-09-01) score GPT-5.6 Terra and Luna at parity with the Claude
ceiling rungs on the extraction bench — see the ceiling-probe table in
`evals/README.md` for the numbers and the single-run caveats. (The shim
is also live-verified — smoke-tested 2026-08-31 against codex-cli
0.151.0-alpha on a free ChatGPT tier: health warm-up, a
production-prompt extraction, and a per-request model switch all pass.)
And no shim is required for any endpoint that already speaks
`/v1/chat/completions`: a hosted OpenAI API key or any local runtime
works directly via the env triple in the previous sections.

## Reasoning effort — the dreamer's thinking budget

By default neither CLI shim sets a reasoning effort: the Claude shim runs
at the `claude` CLI's per-model default and the Codex shim inherits the
host's `~/.codex/config.toml`, so what the dreamer actually spends is
decided outside this repo. To pin it, set
`memory.dream.extractor_reasoning_effort` (Console → Extractor panel, or
the **Effort** row on the Dreamer card). A set value rides every primary
extractor request as `reasoning_effort`:

- the Claude CLI shim maps it to `claude --effort`
  (`low`/`medium`/`high`/`xhigh`/`max`),
- the Codex CLI shim maps it to `-c model_reasoning_effort=`
  (`minimal`/`low`/`medium`/`high`/`xhigh`),
- OpenAI-compatible servers read the field natively; most local runtimes
  ignore the unknown field, though a hosted API may reject an unsupported
  value with a clear 400 — the failure is loud, never silent.

Empty (the default) means the field is never sent — exactly the pre-knob
behavior. The fallback sidecar never receives it, same rule as the
model-only override. Both shims also take a `--reasoning-effort` launch
flag for a pinned default without touching daemon config; a request's
value wins over the launch flag either way.

## Cadence — quiescence-gated, daemon-only

What gets consolidated and when is configurable under `memory.dream`
(`eligible_sources` / `exclude_sources`, and the `min_batch` /
`idle_seconds` backlog+quiescence thresholds that
`memory_dream(action="status")` reports).

Dream runs are single-flight: a `memory_dream(action="run")` that lands
while another run holds the guard returns `{"skipped":
"dream_in_progress"}` instead of racing it — scripted callers should treat
that as a normal outcome (the next sweep tick retries), not an error.

The auto-sweep (Tier 2) fires when:

```
backlog ≥ min_batch (8)
  OR  (backlog ≥ 1 AND idle ≥ idle_seconds (600s))
  OR  an episode is awaiting outcome inference
  OR  (an episode is awaiting a session digest AND backlog = 0)
```

The digest condition is gated on an empty backlog because the digest
stage runs only on the empty-pull (idle) branch of the dream cycle: with
entries still pending, firing on digest backlog alone would consolidate a
partial batch every sweep tick without making digest progress. The normal
cadence drains the entries first; the digest backlog then fires the quiet
ticks.

`idle` is time since the newest band entry, not since the last request — a
session that only *reads* stays quiescent. Polled every
`sweep_interval_seconds` (600s). It runs **only in the
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

## Session digests (opt-in) — one prose memory per closed session

With `memory.dream.digest_enabled` on, the idle dream cycle writes one
narrative prose digest per closed session episode — a mid-density layer
between raw turns and atomic facts, aimed at arc-shaped questions ("how
did the deadline change and why") that no single entry answers. Each
digest is a normal `source="digest"` band entry stamped to the episode it
summarizes: it competes in ordinary dense retrieval, is filterable like
any source, and is never re-mined for facts (`digest` is in
`exclude_sources`). The session briefing's recap renders the digest body
for the most recently closed session.

Mechanics mirror outcome inference: a cursor advances monotonically per
closed episode, transport failures hold the cursor, malformed or
unwritable digests get two attempts before the cursor advances past the
episode. Long sessions are split on line boundaries at
`digest_context_chars` (default 24,000) and map-reduce merged; the prose
length target is `digest_target_chars` (default 1,200 — re-targeted from
800 to the length the extractor naturally writes, measured in the
2026-08-27 sidecar probe). When first enabled, the
zero-start cursor backfills all history, `digest_max_per_cycle` (default
4) episodes per dream pass. Default-off: enablement gates on a human
review of what the configured extractor actually writes —
`evals/digest_sidecar_probe.py` generates that evidence.

## Dream runs — audit and rollback (schema v27)

Every dream pass that produces claims records a **run row** and a per-claim
**pre-image journal** — what each touched slot held before the write. The
journal lives outside the facts supersession chain on purpose: superseded-row
compaction purges that chain in steady state, so it was never durable enough
to revert from. Passes that write nothing (outages, zero-claim batches)
leave no row.

- `memory_dream(action="runs")` lists recent passes: id, cursor movement,
  tallies (including the literal-gate counters), and lifecycle status
  (`running | committed | failed | rolled_back`). A `failed` run means a
  claim write blew up mid-pass — partial writes are journaled and the
  cursor was held.
- `memory_dream(action="rollback")` reverts the **latest committed** pass by
  replaying its journal in reverse through the normal write paths — a
  superseded value is superseded back (history preserved, nothing deleted),
  a dream-inserted slot is retired, member adds/removes are mirrored.
  Rollback covers fact writes only (not relations/lessons/graph), keeps the
  source traces, and never rewinds the dream cursor. It refuses when a newer
  run is `failed`/`running` (unjournaled uncertainty) and on double
  rollback. Both actions are full-tier tools — expand with
  `memory_toolset(action="expand")` from a core-tier session.
- `memory_history(entity, attribute, as_of=...)` answers "what did this slot
  say on date X?" from the version chain (ISO or epoch). Compaction keeps
  only the newest few non-live versions past ~30 days, so a very old
  `as_of` may return an incomplete chain.

Retention: the newest `memory.dream.runs_keep` (default 50) runs survive;
older rows and their journals are pruned on the sweep tick beside
superseded-row compaction. Design doc:
`docs/superpowers/specs/2026-08-01-dream-run-journal-design.md`.

## Consolidation quarantine — the two-man rule (opt-in)

`memory.dream.quarantine_low_trust` (ships **off**) closes the dream's
poisoning-amplifier path with a defense keyed on *who wrote*, never on
what the text says (the MAFIA result in `SECURITY.md` closes the argument
that content inspection can defend this path). When on, a **scalar** claim
whose backing entry is agent-tier — its `source` maps to origin `agent` —
and outside `memory.dream.trusted_sources` never takes `current`
directly:

- it **parks** via the ordinary contender machinery (visible as
  `contested` in `memory_fact_get`, `quarantine:low_trust` in its
  provenance), including at a brand-new slot (a currentless contender);
- it **promotes** on exactly two routes: an explicit
  `memory_fact_resolve(accept=true)`, or an **independent second
  witness** — a later matching claim backed by a different witness token
  (the entry's episode, else its source) or by a non-agent origin —
  **and only when the witness's tier is not below the standing current's
  origin**: the two-man rule is never weaker than the provenance guard
  it reinforces, so two agent witnesses cannot supersede a user-stated
  fact. The same witness restating merely re-confirms the parked value;
  an automated promotion is stamped agent-supported (a literal, never
  the claim's own origin field), and never as a user act.

Run results carry `quarantine_parked` / `quarantine_held` /
`quarantine_promoted`; parks and promotions are journaled (a promotion
under its own action) and `memory_dream(rollback)` reverses both. Nothing
is dropped or hidden — quarantined claims stay stored, searchable, and
auditable through their engram links. Honest scope: episodic search still
surfaces a poisoned *entry*; the quarantine only denies it silent
*canonical* authority. Member ops are outside v1 (members are never
contested by design; the aggregate guard already parks the dangerous
member-over-scalar case). Preregistration:
`docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md`.

## Constraint entries survive verbatim — TypeCompact + guard (schema v35)

The dream is a compression step, and the compaction cliff (arXiv
2608.22752) is what compression does to a rule: a safety rule and an
episodic log are summarised at the same rate, but only the rule needs
its exact wording to stay enforceable. An entry whose
`distortion_tolerance` is `constraint` (set explicitly on `memory_store`,
or inferred by the `auto` heuristic for rule-sized deontic text — see
[memory-model](memory-model.md#who-said-it-and-how-exactly-must-it-survive-schema-v35))
is therefore treated as zero-distortion by the dream:

- **The carrier (TypeCompact).** Among the claims the extractor cites
  the entry for, at least one scalar claim must contain the entry's text
  verbatim (whitespace-collapsed, case-preserving). If none does, ONE
  claim's *value* is replaced with the entry text: the claim whose
  content tokens overlap the rule most (at least one must), and only if
  its target slot is empty or already holds a constraint — a standing
  non-constraint fact is never overwritten and a claim about something
  else is never hijacked, whatever position it has in extractor output.
  The extractor's entity and attribute are kept (slotting is what it is
  good at; wording is not), sibling claims are left alone, member (`op`)
  claims are never carriers, and with no eligible claim the carrier
  refuses and leaves the miss to the guard. Only the carrier earns
  `distortion_tolerance: constraint` (and the pin in recall); a
  paraphrased sibling is an observation and inherits its slot's label.
- **The guard verifier.** After the claims loop, every constraint entry
  in the processed window must have a derived item carrying its text
  verbatim (a parked contender counts; so does a slot the same entry
  formed on an earlier pass). Misses are reported on the run result as
  `constraint_misses` (entry id + text) beside `constraint_verbatim`, on
  the run row's tallies as `constraint_missed`, and logged at WARNING.
  This is a **flag, not a hard fail**: the paper fails a compaction whose
  input is still there to retry, but here the raw entry is never
  discarded (it stays in the associative store and is served by
  `memory_search`), and holding the cursor would hostage every other
  claim in the batch to one rule the extractor could not slot. The
  typical miss is an extractor that emitted no scalar claim for the
  entry at all — inventing a slot is not the dream's business.

**Authority rides along.** Every derived fact takes the *source entry's*
`authority` (`quoted` / `directive` / observation) and inherits the slot's
label when the source is unlabelled — never anything the extractor wrote,
which is model output and steerable by note text (the same trust class
as claim `origin`). Under the two-man rule above, a `quoted` source is
low-trust whoever relayed it: a third party's remark parks as a contender
instead of taking `current` on the relayer's tier. The label only ever
*demotes*; promotion stays keyed on entry metadata, so dressing a note up
as a quote gains nothing but a park. Rollback restores the previous
version's labels along with its value.

## Chronicle events (schema v28) — dated occurrences beside facts

Facts answer "what is current"; they systematically lose *occurrences* —
things that happened at a time ("adopted the kitten on May 13") rather
than states that hold. `memory.dream.chronicle` (**on by default**; needs
Postgres) makes the dream
pass extract those too, into `chronicle_events`, via a **separate
extractor call per batch** — a dedicated events pass with its own pinned
prompt artifact (`evals/prompts/events_pass_v1.txt`), run after the
claims call. The events pass failing is non-fatal by design: claims
commit regardless and the result carries `events_pass_failed: true`. The
bundled sidecar model is fine-tuned for both passes (see the sidecar
section above).

- **Event time vs record time.** `occurred_at` is when it happened;
  `recorded_at` is when the dream stored it. A date is accepted only as
  an exact `YYYY-MM-DD` *and* only when the batch actually contained
  date information — otherwise the event stores undated with the
  source's verbatim `occurred_phrase` ("a while back") and sorts behind
  dated rows. A date is never fabricated.
- **Additive-only.** Nothing updates a stored event; contradiction
  handling sets `invalidated_at` (invalidated rows stop serving but stay
  auditable). Exact restatements dedup against the live row.
- **Gated like claims.** The literal gate applies to event descriptions
  (batch scope, same `enforce`/`log`/`off` modes and counters).
- **Journaled like claims.** Event writes journal into the run's
  pre-image journal (kind `event`), so `memory_dream(action="rollback")`
  deletes exactly the rows that pass created — safe precisely because
  records are additive-only.
- **Serving.** A temporally-cued `memory_search` (when/first/before…, or
  an explicit year-first calendar date like `2026-08-08`) adds an
  `events` block: matching live events, oldest first, each with
  `date` (or `null` plus the verbatim `phrase`), capped at 6. An
  **aggregation cue** (how many/count/total…) widens the cap to 30 and
  adds `events_total` — a computed property of the served list, so the
  answerer can do arithmetic over a long enumeration without recounting
  lines (a count over a capped prefix would be wrong by construction).
  No serving knob — an empty table serves nothing.

Extraction is **on by default** (and surfaced as a Console knob) since
the 2026-08-12 soak review: the pipeline passed its preregistered gates
(separate-pass events, and the multi-task sidecar fine-tune that serves
it — see the CHANGELOG's measured entries), then ran a 2026-08-05..08-12
production soak (188 events, 0 incorrect dates, historical backdating
correct, dedup and literal gates load-bearing, ~160 kB/week volume).
Set `memory.dream.chronicle: false` to opt out. (Lineage: the Phase 1
retrieval-side knobs of
`docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md`
measurably failed, which is what made extraction-time event capture the
live hypothesis.)

## Deep dream — full-corpus graph consolidation

The incremental dream (tiers above) is window-local: it distils only the
recent MIRAS tail into cortex facts. `memory_dream(action="deep")` is a
separate full-corpus GRAPH pass (Phase-2 'C'). Its MECHANICAL half also
runs itself: a need-based tick on the sweep timer applies Steps A/B —
rescore, guard-passing junk auto-delete, scope stamping, proposal
filing, snapshot-first — once the bank has grown by
`memory.deep_dream.auto_min_new_entities` (default 150) since the last
apply or `auto_interval_days` (default 7) have passed; every apply,
manual or tick, resets that clock (`auto_tick: false` disables). Step C
(judgment) is autonomous too, in measured stages: each sweep also sends a
bounded batch of pending merge proposals — with the same evidence pack the
review surfaces show, plus a caution line on pairs stamped
`low_differential` (whose snippets cannot tell the sides apart) — to the
configured model (`memory.deep_dream.judge_mode`,
default `shadow`; the dream extractor, or a dedicated `judge_url`), and
records the verdict + confidence + note on the proposal row (schema v30),
shown beside the evidence in every review surface. In `auto-reject` mode,
reject verdicts at/above `judge_reject_min_confidence` are applied
(`decided_by='dream-judge'`, pair dismissed). A row whose first verdict sat
below that gate gets a **second opinion** on a later sweep
(`judge_second_opinion`, optionally `judge_second_model` — both Console knobs) — a fresh batch,
so an independent sample: two rejects at mean >= `judge_reject_min_confidence_2`
apply, a disagreement stamps `split` on the note and leaves the row for a
human. `judge_mode: auto` goes one step further and folds a pair when two
independent accepts agree on a row that is not `low_differential` at mean
>= `judge_accept_min_confidence` — the only path that ever auto-applies an
accept. Since 2026-09-02 the other queues have judges too, each riding the
same sweep as a bounded batch, all but one defaulting to `shadow`: the
**link judge** (`link_judge_mode`; `auto` promotes accept verdicts to live
edges and applies rejects, each at its own gate — a *retype* is only
recorded, with its corrected relation on the row, for a reviewer to apply,
because the first ladder scored the judge's relation choice at 0/1; edges
are reversible, which is why this queue may run auto), the **junk judge**
(`junk_judge_mode`; `auto` deletes only under an evidence bar), the
**store-curation judge** (`curation_judge_mode`; `auto-distinct` applies
the reversible dismissal, `auto` also retires — never deletes — the losing
duplicate slot after folding its carry-over into the survivor), and the
**Step-C candidate judge** (`candidate_judge_mode`, defaulting to `off`;
after each deep apply, works through that apply's candidates one
`judge_batch` slice per sweep tick — `propose` files an edge proposal and
`dismiss` marks the pair distinct, and every judged pair is memoised for
`candidate_rejudge_days`). Two mechanical additions stop the queues
refilling: each apply files the Console's live analyzer duplicate findings
into the merge and link queues (`analyzer_file_duplicates`, on by default)
and — once you switch it on — deletes week-old entities that carry no
evidence and no mention at all (`orphan_sweep`, off by default, at most
`orphan_max_per_apply` per pass: it is the one destructive switch that
would fire on the first apply after an upgrade). Which models
judge reliably is measured, not assumed: `evals/judge_ladder.py` scores the
merge judge against ratified triage verdicts
(`evals/results/judge-ladder-20260816.json`) and `evals/queue_judge_ladder.py`
scores every queue's judge against the 2026-09-02 blind-panel set
(`evals/results/queue-judge-panel-20260902.json`), simulating each auto gate.
The same need signal rides `memory_dream(action="status")` as the
`deep_dream: {recommended, reason, ...}` block — a harness-agnostic
nudge any MCP client can surface to its user when a triage session is
worth scheduling. A
dry-run (default) returns a preview of what it would change: re-scored
edges, hard type-violation edges queued for supersession, exact-duplicate
entity pairs queued for merging, and semantic link *candidates* across
sessions (each with truncated context snippets; items the apply path would
dedupe are flagged `already_proposed`). Adding `apply=True` first dumps the
five graph tables to a JSON forensic record under `data_dir/graph_snapshots/`
(refusing with `snapshot_failed` if it can't), then commits the safe
self-clean (re-score + supersede violations + merge exact dups) and returns
`candidates` for review. The agent then drives Step C in the same session
(see the `/dream` flow in `examples/commands/dream.md`): judge each
candidate from its snippets, post the real relations with
`memory_graph_review(action="propose")` — they land in the Atlas Review
queue (`proposed_link` findings) for per-item accept/reject before anything
reaches live edges — and record clearly-distinct pairs with
`memory_graph_review(action="dismiss_pair")` so they stop resurfacing. See
[the deep-dream runbook](../runbooks/deep-dream.md) for the operator
procedure.

A duplicate finding whose two names are a source file and its own bare stem
(`band.py` ↔ `band`) now arrives with `action: "relate"` and a
`suggested_relation` (`implements`) instead of forcing merge-or-dismiss:
the concept usually has identity the file does not, and several files can
realize one role, so merging asserts something false and dismissing throws
a real relationship away. Settle it with one call —
`memory_graph_review(action="relate", src=<file>, relation="implements",
dst=<concept>)` writes the edge *and* dismisses the duplicate pair — or
one Relate button in the Atlas review drawer, which does the same.

**Draining the quarantine.** Quarantined edges are almost all untyped
`related-to` co-mentions, and about half of them name a real relationship
that merely got the wrong label. Each dream therefore re-asks the extractor
to *type* up to `memory.dream.retype_quarantined_max` quarantined pairs
(default `3`), showing it only the notes where both entities co-occur. A
pair that comes back with a real relation is filed as a fresh review
proposal — a retype is a second guess on suspect material, so it never
writes a live edge — and the untyped original is rejected either way, so
the queue drains instead of accumulating. The pass runs even on a dream
with no backlog (the quarantine grows fastest when dreams are rare),
no-ops on an empty quarantine, and reports
`retyped: {considered, retyped, settled}` from `memory_dream(action="run")`.
Set `0` to disable.

**What no longer reaches the graph.** Five sources of review-queue noise
were closed at the write path rather than cleaned up afterwards: dotted
pseudo-entities minted when an extractor read a flattened
`entity.attribute` vocabulary hint as a name; the `<artifact> <aspect>`
nodes `memory_outcome` mints, which shared nearly every token with the
artifact they mentioned and so dominated the duplicate and orphan
findings; merge proposals pointing *at* a contentless entity, now that
fold direction ranks on facts as well as degree; edges to git branch
names, which typed as unknown — and therefore as neutral — and sailed past
the confidence floor; and merge proposals for name-shape false-positive
classes — a broader name paired with a date/run-tag-stamped event node,
and sibling ids differing only by numeric tokens (`CT200`/`CT400`) or the
pre/post pair — vetoed at both filing sites, with the rules gated by a
replay of the 2026-08-11 full-queue triage (they suppress 12 of that
pass's 101 rejected proposals and none of its 38 accepted ones).

Two more queue-quality knobs shape what gets proposed at all. Link
candidates skip pairs whose evidence-support overlap exceeds
`memory.deep_dream.max_support_overlap` — measured as **containment**
(`|shared| / min(|a|,|b|)`), a stricter test than the Jaccard ratio the
same number would suggest — and exclude pairs with a pending link proposal
or a junk-flagged side before top-k selection, so the queue refills with
new work instead of settled work. The trace-less-entity fallback scan is
capped at `memory.deep_dream.max_fallback_mentions` (default 30) per
entity; trace-backed mentions are never capped.

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
