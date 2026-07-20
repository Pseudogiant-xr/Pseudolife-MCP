# The memory model — cortex, world facts, lessons, and time

The canonical-fact layers in depth: the slot-keyed cortex, provenance
contenders, the cited world cortex, procedural lessons, and the temporal /
multi-writer stamp. Part of the [user guide](../../README.md#documentation).

## Canonical facts — the cortex (schema v8)

Alongside the associative continuum (the 8 MIRAS bands) sits the
**cortex**: a slot-keyed canonical-fact store. Where the continuum is
similarity-ranked and decaying, the cortex is **identity-not-similarity,
supersession-not-decay, currency-not-frequency** — one *current* value per
`(entity, attribute)` slot, retrievable out of the context window.

- **Single-writer capture.** The LLM **dream** pass (the extractor sidecar)
  is the sole *automatic* writer of canonical facts, plus deliberate
  `memory_fact_set` calls. The deterministic regex auto-promote on `store`
  is now **opt-in** (`memory.cortex.auto_promote`, default **off**): it
  mis-splits compound entity names (`"payments database host"` →
  `payments` / `database host`) and fragments slots, so it ships off — see
  [the single-writer cortex design](../specs/2026-06-19-single-writer-cortex-design.md).
  (When enabled it still uses the precision-first dev lexicon:
  `<entity> <attr> is <value>` with the attribute drawn from a closed set —
  port / version / host / branch / default timeout / … — plus
  `my <attr> is <value>`, `<Entity>'s <attr> is <value>`,
  `the <attr> of <entity> is <value>`, and single-line
  `<entity> <attr>: <value>`.) A one-time `ops/dedup_cortex.py`
  (dry-run-first, reversible) collapses sibling slots left by past
  auto-promotes.
- **Documented vs enacted.** A fact stated by a *document* you shared (a
  spec, policy, protocol, runbook) is captured under that document's
  subject, distinct from a fact about what was actually done — the rule and
  the practice occupy different slots and can disagree without either
  overwriting the other. See [what the extractor
  captures](dreaming.md#what-the-extractor-captures).
- **Deterministic read.** `memory_fact_get("project", "language")` returns
  the one current value — no ranking, no stale duplicates. `memory_search`
  also surfaces matching facts ahead of associative hits (a `"cortex"`
  block) — the hybrid shape that outperforms either channel alone (see
  [Benchmarks](benchmarks.md)).
- **Deliberate write / correction.** `memory_fact_set(entity, attribute,
  value, origin="user")` asserts a fact at higher confidence; setting a new
  value at an existing slot supersedes the old (kept as audit history).

## Provenance contenders — never silently overwrite a user fact

Every cortex fact carries a provenance tier: **`user` > `action` >
`agent`** (set via `origin=`, or defaulted from `source`). A write may only
*supersede* a slot whose current value is backed by an equal-or-weaker
tier. A **weaker-tier** write (e.g. an `agent` value conflicting with a
`user`-stated fact), or one below the confidence margin, is **not
applied** — it's parked as a *contender*:

```python
memory_fact_set("db", "host", "10.0.0.5", origin="user")   # current
memory_fact_set("db", "host", "10.0.0.9", origin="agent")  # -> action="contested"
# current stays 10.0.0.5; "10.0.0.9" is parked. memory_fact_get shows both;
# memory_search flags the fact "contested": true.
memory_fact_resolve("db", "host", accept=True)   # human said yes -> adopt (user-confirmed)
# or accept=False -> discard the contender, current unchanged.
```

This catches the case where the agent *decides* to update something and the
human only said "yes/proceed": the discrepancy surfaces (at the write, in
search, and in `memory_fact_get`) so the agent can check in rather than
overwrite. Set `memory.cortex.protect_provenance: false` in `config.yaml`
to disable and restore pure newer-wins.

## World knowledge — the world cortex (schema v9)

A third layer sits beside the personal cortex: the **world cortex**, for
durable facts about *external* reality that a frozen training cut-off may
have wrong or stale — a current model version, a price, who holds a role, a
research finding. It's a separate slot-keyed store (its own `world_facts`
table, `origin=source`), so external claims never mingle with the
user/project facts.

```
memory_world_set("anthropic", "latest-model", "opus-4.8",
                 source_url="https://...", source_quote="Opus 4.8 is the latest...",
                 freshness_class="volatile")   # weeks | "slow" months | "evergreen" never
memory_world_search("which Claude model is current")
# → entries with effective_confidence (age-decayed), a `stale` flag, and the citation
```

Each fact carries a **citation** (`source_url` + the 1–2 sentence
`source_quote`, not the whole page) and a `freshness_class` that drives
**age-decayed trust** at read time: past 2×TTL a fact is flagged `stale`
(a lead to re-verify, not truth). The trust contract: prefer a fresh,
*cited* world fact over frozen training intuition when they conflict — but
cite it ("as of <date>, per <source>") rather than presenting it as your
own knowledge; your own cortex/episodic facts stay the highest-trust ground
truth. `memory_search` surfaces matching world facts in a separate block,
and the Console's world view (`/api/world`) lists them all for audit.

> The world cortex here is populated **manually** via `memory_world_set`.
> The live-web `research_ingest` action (fetch + distil cited world facts
> automatically) is an agent-side capability that depends on the agent's
> web tool — it is not part of the standalone MCP server.

## Procedural memory — the lessons store (schema v10)

A fourth layer learns from the agent's *own work*. Where the cortex stores
*declarative* facts ("X is Y"), the lessons store is *procedural*: keyed by
a **task-type** and an **aspect** (`approach` / `pitfall` / `tool-choice` /
`correction`), each lesson carries an **outcome** (`success` / `failure` /
`correction`) and a **polarity** (`+` do-this / `-` avoid). Its own
`lessons` table keeps it isolated from the personal and world cortex.

Capture is cheap and in-session; synthesis is single-writer (the dream):

```
# during a task, log what happened — this writes a SIGNAL, not a lesson:
memory_outcome("deploy engine to host", "failure",
               about="tar --same-owner", detail="chown errors aborted the extract")
memory_outcome("deploy engine to host", "success", about="tar --no-same-owner")
# user corrections are auto-captured when a user-tier memory_fact_set supersedes a value.

# the dream later distils accumulated signals into durable lessons; recall them at task start:
memory_lesson_search("how do I deploy the engine to a host")
# → [{task, aspect, lesson, about, polarity:"-"|"+", outcome, confidence, score}, ...]
```

Lessons are also **traversable in the graph**: a task-type becomes an
`etype='task-type'` entity, and each lesson adds a `prefers` (positive) or
`avoids` (negative / dead-end) edge to the tool/source it concerns — so
`memory_graph("deploy engine to host")` shows what to reach for and what to
avoid. Retrieval is embedding-on-query (mirrors `memory_world_search`); the
graph edges power structured traversal.

> Single-writer: `memory_outcome` only ever logs a signal — the dream's LLM
> extractor is the sole writer of lessons. With no extractor configured,
> signals accumulate (pruned by retention) and no lessons are synthesised,
> exactly as the cortex behaves without an extractor. The synthesised
> lessons are **auto-injected at session start** by the
> `pseudolife-mcp briefing` SessionStart hook (the "lessons from past work"
> block) — see [Episodes & session lifecycle](episodes.md).

## Sense of time + multi-writer attribution (schema v11)

Every canonical write (cortex, world, lessons) carries a **temporal /
provenance stamp** so the agent has a real sense of *when* a fact held and
*who* set it — and so concurrent writers can't silently clobber each other:

- **`tx_time`** — when this version was *written* (wall-clock display).
- **`valid_time`** — when the fact became *true* (event time). A lesson
  synthesised from an outcome signal inherits the signal's observation
  time, not the dream's write time, so the two clocks stay honest
  (bitemporal).
- **`(hlc_phys, hlc_logical)`** — a **Hybrid Logical Clock** that is the
  *ordering authority* for supersession. Wall clocks can jump backwards
  (NTP steps, clock skew across sessions); the HLC is monotonic, so "newer
  wins" is jitter-proof — a later write always supersedes, even if its wall
  time reads earlier. Wall time is display-only.
- **`writer_id` / `session_id`** — which writer/session made the change.
  The daemon reads an `X-PL-Writer` header per request (the stdio shim
  forwards `PSEUDOLIFE_WRITER_ID`) and resolves the session id through the
  five-tier [session-identity](configuration.md#session-identity) contract
  (the shim's `X-PL-Session` header preferred), so a Codex session, a second
  Claude session, and the dream are all distinguishable.

Reads surface this: serialised facts include the stamp plus a human `age`
("3 days ago"), and **`memory_history(entity, attribute)`** returns the
full version timeline — current + superseded, oldest→newest, each
attributed. The supersession log records the writer/session too.

> **Writer topology.** The live path is a single daemon with a coarse lock
> (`write_mode=snapshot`) — correct by construction. The schema also lays a
> dormant `write_mode=occ` seam (a `version` column + per-row
> compare-and-swap) for a future multi-process writer; selecting it raises
> `NotImplementedError` until that Phase-2 path is built.
>
> **Collision fix (v0.4) + AGE removal.** The DB role is `pseudolife`; the
> old Apache AGE graph was also named `pseudolife`, which made AGE create a
> `pseudolife` schema that shadowed the real `public` bank. AGE has since
> been removed entirely — edges live in the relational `edges` table (the
> source of truth), so the collision can no longer recur.
> `ops/migrate_drop_age.py` drops the AGE graph + extension from an
> existing bank (back up first), and every connection still pins
> `search_path` to `public` (asserted on startup).
> `ops/retire_by_writer.py` supersedes a rogue writer's rows in one shot.
