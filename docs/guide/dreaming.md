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
| **1 — agent-driven** | the **agent itself** is the gateway: the `/dream` command | the agent you already run | highest |
| **2 — shipped default** | daemon auto-sweep calls an OpenAI-compatible endpoint — the bundled sidecar out of the box, or any endpoint you point it at | nothing (sidecar) / one base-URL + key + model | high; free if local |

**Tier 1 — `/dream` (agent-driven).** Copy `examples/commands/dream.md` to
`.claude/commands/dream.md` in any project, then run `/dream`. The agent
reads `memory_dream(action="pull")`, extracts durable current-state facts,
writes them with `memory_fact_set`, and commits the cursor. To run it on a
cadence instead of by hand, point a scheduled agent/cron job at the same
prompt.

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
     `127.0.0.1:8082`; `-Model` picks the served default —
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

## Cadence — quiescence-gated, daemon-only

What gets consolidated and when is configurable under `memory.dream`
(`eligible_sources` / `exclude_sources`, and the `min_batch` /
`idle_seconds` backlog+quiescence thresholds that
`memory_dream(action="status")` reports).

The auto-sweep (Tier 2) fires when:

```
backlog ≥ min_batch (8)
  OR  (backlog ≥ 1 AND idle ≥ idle_seconds (600s))
  OR  an episode is awaiting outcome inference
```

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

## Chronicle events (schema v28) — dated occurrences beside facts

Facts answer "what is current"; they systematically lose *occurrences* —
things that happened at a time ("adopted the kitten on May 13") rather
than states that hold. `memory.dream.chronicle: true` makes the dream
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
- **Serving.** A temporally-cued `memory_search` (when/first/before…)
  adds an `events` block: matching live events, oldest first, each with
  `date` (or `null` plus the verbatim `phrase`), capped at 6. An
  **aggregation cue** (how many/count/total…) widens the cap to 30 and
  adds `events_total` — a computed property of the served list, so the
  answerer can do arithmetic over a long enumeration without recounting
  lines (a count over a capped prefix would be wrong by construction).
  No knob — an empty table serves nothing.

The knob is **off by default**. The extraction pipeline has since passed
its preregistered gates (separate-pass events, and the multi-task
sidecar fine-tune that serves it — see the CHANGELOG's measured
entries), so the remaining question is production behavior, not bench
quality: default-on waits on a soak review of the chronicle under real
workloads. (Lineage: the Phase 1 retrieval-side knobs of
`docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md`
measurably failed, which is what made extraction-time event capture the
live hypothesis.)

## Deep dream — full-corpus graph consolidation

The incremental dream (tiers above) is window-local: it distils only the
recent MIRAS tail into cortex facts. `memory_dream(action="deep")` is a
separate, manually-triggered full-corpus GRAPH pass (Phase-2 'C'). A
dry-run (default) returns a preview of what it would change: re-scored
edges, hard type-violation edges queued for supersession, exact-duplicate
entity pairs queued for merging, and semantic link *candidates* across
sessions (each with truncated context snippets; items the apply path would
dedupe are flagged `already_proposed`). Adding `apply=True` first dumps the
five graph tables to a JSON forensic record under `data_dir/graph_snapshots/`
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
