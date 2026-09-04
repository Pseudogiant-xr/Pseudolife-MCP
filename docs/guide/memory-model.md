# The memory model — cortex, world facts, lessons, and time

The canonical-fact layers in depth: the slot-keyed cortex, provenance
contenders, the cited world cortex, procedural lessons, the reference bank
for background documents, and the temporal / multi-writer stamp. Part of
the [user guide](../../README.md#documentation).

## Canonical facts — the cortex (schema v8)

Alongside the associative store (a flat similarity-ranked pool since
2026-08-15; an 8-band layout remains an opt-in preset) sits the
**cortex**: a slot-keyed canonical-fact store. Where the associative
store is similarity-ranked, the cortex is **identity-not-similarity,
supersession-not-decay, currency-not-frequency** — one *current* value per
`(entity, attribute)` slot (or a member set, for [set-valued
slots](#set-valued-slots)), retrievable out of the context window.

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

### How current is this fact?

A cortex fact is the *last thing asserted* at a slot, which is not the same
as a *true* thing now. Two fields make that gap visible rather than leaving
a reader to assume the value is live:

- **Every fact carries its dates.** Reads project `asserted_at` and
  `last_confirmed` (ISO-8601, second resolution) plus a human `age`
  (`"3d ago"`). A value with no other signal is still judgeable: a
  "currently deployed" fact last confirmed nine months ago is worth
  re-checking against the source before acting on it.
- **`freshness_class` says how fast the slot rots** — `evergreen`
  (never decays), `slow` (~9 months), `volatile` (~3 weeks). Non-evergreen
  facts report an `effective_confidence` that decays toward a per-class
  floor with time since `last_confirmed`, and are flagged `stale: true`
  past twice their TTL. Re-asserting the same value confirms it and
  restores full confidence.
- **`stance` records how the source *held* the value** (schema v29) —
  a dream claim extracted from hedged or negated text ("probably X",
  "no longer Y") carries that epistemic stance onto the fact, visible in
  `memory_fact_get`, recall cortex blocks, and `history` when set. It
  follows the latest asserting write (a plain restatement clears it), is
  never an input to confidence, ranking, or supersession, and is not
  settable via `memory_fact_set` — it exists so a hedge survives
  consolidation instead of hardening into a flat assertion.

Set it explicitly at write time: `memory_fact_set("pseudolife-mcp",
"extractor-prompt-version", "v2", freshness_class="volatile")` — an
explicit value always wins, and since v23 `POST /api/facts/set` threads it
too, so the Console and the REST fallback (the documented workaround for
clients that stringify MCP list params) no longer pin every fact they
write to `evergreen`. Left unset (the default is the sentinel
`"auto"`), the class is instead **inferred from the entity's kind**
(schema v24, `entity_kinds`): a `system` entity (live, mutable — the sort
of thing with a "currently deployed" answer) can resolve to `volatile`; an
`artifact` (frozen at a point in time, like a tagged release) or a
`concept` (abstract/definitional) always resolves `evergreen`, whatever
the attribute name. `0-9-0-release / schema-version` is permanently true;
`daemon / schema-version` rots — same attribute, opposite class, because
the entities differ.

The attribute still gets a vote, but only on a `system` entity and only in
one direction: a short, deliberately dumb list of state-shaped names
(`status`, `state`, `health`, `live`, `running`, `current`,
`deployment`/`deployed`, `url`, `version`) resolves `volatile`, everything
else stays `evergreen`, and event-shaped names (`…-date`, `…-at`,
`…-hash`, `…-commit`, `…-count`) are forced `evergreen` first, so a
recorded deployment *date* never inherits the volatility of the deployment
it describes.

An entity with no recorded kind — which is every entity until the offline
classifier assigns one — resolves `evergreen`, not `volatile`: personal
cortex facts are mostly durable, and defaulting the other way would
re-rank every existing bank on an assumption nothing has measured. Facts
already in the bank before schema v23, and any entity the classifier
hasn't reached, read back exactly as before, so nothing changes until an
entity is deliberately classified.

Both fields are descriptive, not enforcement — a stale fact is still
returned, marked. Nothing is auto-deleted or auto-superseded on age.
The read surface does nudge, though: once a fact has aged past a third
of its TTL (or sits contested), `memory_search`, `memory_fact_get`, and
`memory_world_search` attach a ready-made `correct_with` call to it — a
filled-in `memory_fact_set(...)` the reading agent can run the moment it
verifies the value, re-asserting or correcting at the same slot. The
`stale: true` flag is the louder, later signal (twice the TTL);
`correct_with` fires earlier so drift gets fixed at first contact.

**`re_verify` / `re_verify_reason`** is a third, independent signal — a
*retract-direction* read of the engram cross-index (schema v13;
`memory.traces.enabled`, on by default — see
[Built-in defaults](configuration.md#built-in-defaults-tuned-for-claudes-use-case)).
Where the fields above say a fact is old, `re_verify` says a fact's
*evidence* moved: a served fact whose source memories were superseded
(corrected) *after* the fact was last confirmed carries `re_verify: true`
plus a `re_verify_reason` naming how many source memories were corrected
since. It surfaces on `memory_fact_get`, `memory_search`'s cortex block,
and `memory_recall`; on a set-valued slot the comparison is against the
newest member's confirmation stamp, since a set is served as one grouped
answer. It is a flag, never a cascade, and deliberately not routed into a
`correct_with` call: keyed on `last_confirmed`, a slot re-asserted long
after its retracted contributor still fires it, which on a mature bank is
common — on the live bank on 2026-09-02 roughly a quarter of current facts
stood on a source memory contradicted since they were last confirmed.
Routing that into the same call `correct_with` tells the reader
to run *now* would turn a common, weak signal into a standing instruction
to rewrite a quarter of the cortex every session. Re-asserting or
re-confirming the slot moves `last_confirmed` forward and clears the flag.

The **active** affordance for retracted evidence lives on the correction
itself: `memory_supersede`'s result carries `derived_flagged` — the
canonical facts (slots) the dream built on the memories just corrected.
They are named, not touched: nothing is rewritten, the caller decides
whether each derivation still holds. Each row carries `has_current_value`
(a slot with no live value is still blast radius worth seeing, just
nothing to go re-check), the list is capped at 50 entries with live slots
first, and `derived_flagged_truncated` / `derived_flagged_total` say
whether a correction reached further than the cap.

Both signals are **best-effort**, on purpose: they are derived at read
time from evidence that still exists, so losing the evidence loses the
flag. `memory_traces.entry_id` is `ON DELETE CASCADE`, a true-drop
capacity eviction hard-deletes the entry row, and a superseded entry is
the top eviction candidate (contradiction decay multiplies its surprise by
0.3) — so a flag can appear and later vanish with no re-verification
having happened, and `memory_delete`, the strongest retraction of all,
raises no flag at any point. Both are gated on `memory.traces.enabled`;
turning it off silences both without changing anything else about how
facts are served.

Since 2026-07-25 **raw band entries follow the same slot rule.** When a
stored memory and an earlier one assert different values — or opposite
polarities — at the same normalised `(entity, attribute)` slot, the earlier
entry is marked superseded. This is deterministic and does not consult
embeddings, which matters because a value swap is a *minimal* edit: a real
correction is often more embedding-similar than a harmless near-duplicate,
so similarity alone is close to a coin flip for this judgment. It runs
ahead of the similarity-gated heuristics (negation asymmetry, affirmative
replacement, state transition), which still handle everything without
slots. Slot extraction is deliberately precision-gated — about 0.6% of
conversational turns yield one — so this path mostly serves deliberate,
fact-shaped writes, and its reach grows with extraction quality.

### Who said it, and how exactly must it survive? (schema v35)

Two labels ride on every entry and every fact, set at write time and
inherited through supersession unless a later write restates them:

- **`authority`** is the *speech act* of the text — `directive` (an
  instruction to the agent), `observation` (a plain statement; the
  default, stored as NULL and served as nothing), `quoted` (reported
  speech: a document, a paper, a third person). It is deliberately a
  separate axis from `origin`: `origin` says *who wrote* and is a tier
  that drives supersession arithmetic (the provenance guard, the two-man
  rule), while directive-vs-observation is not a tier ordering at all —
  and the failure this closes (arXiv 2608.01679, *authority collapse*)
  is exactly a third party's offhand remark consolidating into what reads
  as a standing user instruction. The pair `(origin, authority)` is what
  the paper calls authority. A `quoted` source is low-trust for the
  [consolidation quarantine](dreaming.md#consolidation-quarantine--the-two-man-rule-opt-in).
- **`distortion_tolerance`** is how exactly the text must survive
  consolidation (arXiv 2608.22752, *the compaction cliff*): `constraint`
  (zero — verbatim), `procedural`, `belief`, `preference`, `episodic`.
  Only `constraint` has consumers today: the dream copies a constraint
  entry's text verbatim onto a derived fact and a post-dream guard reports
  any constraint left without a carrier ([dreaming](dreaming.md#constraint-entries-survive-verbatim--typecompact--guard-schema-v35)),
  and in-scope constraint facts are served *ahead of* the cosine ranking
  ([retrieval](retrieval.md#constraint-pinning-schema-v35)).

Both are explicit parameters on `memory_store` and `memory_fact_set`;
the `auto` default is a deterministic form heuristic (no model call on
the store path) that asserts `constraint` only for rule-sized deontic or
imperative text (`must`, `shall`, `forbidden`, `rule:`, or an opener like
`Never run …`), `quoted` on an explicit reporting construction
(`according to`, `per the`, `the docs say`), and `directive` on an
instruction addressed to the reader — measured on the live bank before
shipping (`evals/results/label-heuristic-audit-20260902.json`) and
re-measured on 2026-09-03 after `must` as a noun or adjective ("a
must-read series") stopped counting as a deontic
(`label-heuristic-audit-20260903.json`, plus the chat-text replay in
`label-heuristic-audit-20260903-beam-chip5.json`). The other four
fidelity classes are accepted explicitly and carried.
Neither label ever feeds confidence or supersession routing; a
`constraint` label is the one label retrieval *ranks* on. Served only
when set, so an unlabelled record's payload is byte-identical to before.

## Set-valued slots (schema v26)

A cortex slot is scalar by default — one *current* value, corrected by
supersession. Some facts aren't a single current value at all: restaurants
you've tried, bikes you own, PRs pending review. Forcing a collection through
the scalar model destroys information — the second `memory_fact_set` call
supersedes the first, so "tried: Ramen-ya" then "tried: Pho Anh" leaves only
Pho Anh current. A **set-valued slot** holds many members concurrently
current instead of one.

- **Use a set** when the fact is naturally plural and members are added and
  retracted independently of each other — tags, memberships, an inventory,
  a pending-items list.
- **Keep scalar supersession** when there is one true value that changes over
  time (a job title, a deployed version, a phone number) — the existing
  `memory_fact_set` behaviour, unchanged.

### Lifecycle

`memory_set_add(entity, attribute, member)` adds a member or, if the same
value (exact match or near-duplicate by embedding) is already current,
*confirms* it — bumping `last_confirmed` and, if higher, confidence, rather
than inserting a duplicate. `memory_set_remove(entity, attribute, member)`
retracts one current member. Neither call touches any other member of the
slot. As with scalar supersession, nothing is hard-deleted: a removed member's
row survives with `status: "removed"`, so re-adding the same value later is a
fresh add, not an undo, and the full history (added, confirmed, removed,
re-added) stays inspectable via `memory_history` / the store's
`members(..., include_removed=True)` audit view.

Members are never *contested* — there is no provenance-tier dispute path for
a set the way there is for a scalar (see [Provenance
contenders](#provenance-contenders--never-silently-overwrite-a-user-fact)
below). A second value landing on an already-populated set slot is just a
second member, not a conflict to resolve. (The one exception: an *add*
against a slot that still holds a protected aggregate scalar — see
[Conversion rules](#conversion-rules) below — parks as a scalar contender.
Once a slot has actually converted to a set, this still holds: members
themselves are never contested.) A set slot also caps at 100 concurrent
members; further adds beyond the cap are dropped (`"member_capped"`) rather
than silently applied or queued.

### Conversion rules

Conversion between scalar and set is deliberately **one-way in both
directions of the story**:

- **Scalar → set**: the first `memory_set_add` call against a slot that
  currently holds a scalar value converts it. The scalar row is superseded
  (kept as audit history, same as any other supersession) and reinserted as
  the set's first member. From that point, `memory_fact_set` against the
  same slot raises an actionable error naming `memory_set_add` /
  `memory_set_remove` instead of the store's own `add_member`/`remove_member`
  vocabulary — there is no path back to scalar while any member is current.
- **Set → scalar**: only once *every* member has been removed. With no
  current record of either kind at the slot, `memory_fact_set`'s own guard
  (which checks for current members, not history) allows a fresh scalar
  write there. This is a byproduct of removing the last member, not a
  dedicated "revert" call — and the removed member rows stay as audit, they
  just no longer make the slot read as a set.

Set members are **evergreen-only, by design**: there is no way to give a
member a `freshness_class`, and the scalar → set conversion **drops** a
non-evergreen scalar's class rather than carrying it onto the member (a
`volatile` "deploy status: pending" that gets a second status added
becomes an ordinary evergreen membership). Three reasons, weighed
deliberately rather than left implicit:

- staleness decay exists to age scalar values that change without anyone
  saying so; a set already has an explicit "no longer true" channel —
  `memory_set_remove`, with removal tombstones — and decay layered on top
  would be a second, competing invalidation mechanism;
- there is no group-level staleness that honours the `stale_policy`
  contract: quarantining or demoting a whole set entry because one member
  aged would transform *fresh* members' payloads (the no-harm gate the
  policy eval preregistered), while per-member transforms would still leak
  raw stale text through the composed group `value`;
- the serving change would re-rank deployed banks on an unmeasured
  assumption — the same reason `freshness_class` itself defaulted personal
  facts to `evergreen` at v23.

The drop is audit-visible, not silent: the conversion's supersession-log
entry carries `dropped_freshness_class` (e.g. `"volatile"`) whenever the
converted scalar was non-evergreen, and the superseded scalar row keeps
its own class for history. If a fact's staleness matters, keep it scalar.

The scalar → set conversion carries one guard: if the current scalar is a
**number-led aggregate value** — a value with an optional leading currency
symbol (`$` `€` `£`), then an optional leading `+`/`-` sign, then a required
digit (`^[$€£]?[+-]?\d`; e.g. "32", "27 species", "$1,500") —
`memory_set_add` does not convert it.
Converting would destroy a stated total that no enumeration of members
recovers, which is exactly what a paired eval gate measured as a
net-negative effect on knowledge-update questions
(`evals/results/c2op-gate-verdict.json`). Instead the incoming member is
parked as a contender against the scalar (audit reason
`member_add_blocked_aggregate`), the same provenance-contender machinery
described below; the scalar stays current, and `memory_fact_resolve(...,
accept=True)` remains the explicit human path to overwrite the total. If
the incoming member equals the current scalar, it confirms the scalar
instead of parking a contender identical to itself. The guard applies
unconditionally — regardless of `memory.cortex.protect_provenance` —
because protecting a stated total isn't a provenance-tier concern.
Accepted v1 limitation: on content that enumerates members after stating a
count ("I own 3 bikes" then "picked up a gravel bike"), the guard likewise
suppresses set formation and leaves the latest add sitting as a contender —
correct for stated-total content, a measured trade-off for enumerating
content.

### Reading a set slot

`memory_fact_get(entity, attribute)` returns the scalar shape
(`{record, contenders}`) for a scalar slot, but a set-valued slot returns a
different shape instead: `{kind: "set", entity, attribute, members: [...],
removed: [...]}`. **`members: []` (every member removed) reads as EMPTY** —
the same signal a scalar miss gives a caller — not as "found, zero members."

`memory_search` and `cortex_search` surface a set slot's whole current
membership as **one entry**, not one hit per member: `{"kind": "set",
"value": "m1; m2 (2 members)", "members": [...], "score": <top member's
score>, "contested": false, ...}`. The composed value lists whichever members
individually ranked highest first, then any current member that didn't rank
on its own — so the full membership is always visible even when the query
only matched one member by name. A set entry carries `last_confirmed`,
`asserted_at`, and `age`, all anchored to the most recent add/confirm
activity across its members — **removing a member never moves these dates**.
It carries no `freshness_class` at all (it renders as `evergreen`, same as
any unclassified scalar); age-based decay is a scalar-only affordance, by
the deliberate evergreen-only rule under [Conversion
rules](#conversion-rules) above.

### Dream extraction

A dream claim may carry `"op": "add" | "remove"` to target set membership
instead of the scalar supersede path. `op` is for membership changes only —
a plain value update ("moved to Seattle") is still an ordinary scalar claim
with no `op`. A scalar claim (no `op`) landing on a slot that already holds
current members is dropped and logged, not routed or silently applied — the
only way to touch a set slot is an explicit `op` or the two MCP tools
themselves. A malformed `op` value degrades to the scalar path with a
warning rather than failing the dream. **Since 2026-08-01 the shipped
extraction prompt solicits `op`**, paired with a counts-are-never-members
rule: the bare op block measured net-negative on knowledge-update content
(the model re-routed count *updates* into `op:"add"` claims, freezing
stated totals — `evals/results/c2op-gate-verdict.json`), and the count
rule is what repaired it — the gated run landed the commit-gated cascade
exactly at the op-less control while genuine sets still formed
(`evals/results/c2op-count-verdict.json`, with sidecar-adoption and
ladder-rung validation in the same artifact). The shipped prompt is
byte-pinned to the measured artifact
(`evals/prompts/ku_op_prompt_v10_stance_update.txt`, pinned by
`test_op_prompt_artifact.py`); the op block was introduced at v5 and the
pin has since moved through the v10 update-anchored stance revision.
The aggregate-conversion guard (see [Conversion rules](#conversion-rules)
above) remains the apply-time backstop: an `op:"add"` that does land on a
stated-total scalar parks as a contender rather than converting, whether
it came from a live `memory_set_add` call or a dream claim.

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
to disable and restore pure newer-wins — for scalar-vs-scalar conflicts;
the aggregate conversion guard's contender parking (see [Conversion
rules](#conversion-rules) above) applies regardless of this setting, since
protecting a stated total isn't a provenance-tier concern.

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

**Retiring a world fact is reversible (schema v37).**
`memory_forget(scope="world", ...)` retires the slot rather than deleting
it: the row's `status` becomes `retired` and an FK-free `store_decisions`
row records who, why, and the verbatim record. The undo is
`memory_graph_review(action="restore_slot", store="world",
src="entity|attribute")` — restoring from the retired row while it still
exists, or from the audit snapshot once compaction has purged it; a bare
entity in `src` (no `|attribute`) restores every retired aspect of that
entity. `GET /api/curation/retired` lists what's currently retired across
both stores, and the Console's undo route is `POST /api/world/restore`.
Only `scope="memory"` and `scope="fact"` still hard-delete.

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

Not every synthesized lesson is written: one that near-matches an existing
*current* lesson at a different key with the same polarity is skipped as a
duplicate and counted (`lessons_deduped` in the dream-run row;
`memory.lessons.synthesis_dedup_min_similarity`, default `0.88`, `0`
disables). Opposite-polarity near-matches always write — a dead-end and a
success about the same thing are both worth keeping — and explicit
`lesson_write` calls are never gated.

Lessons are also **traversable in the graph**: a task-type becomes an
`etype='task-type'` entity, and each lesson adds a `prefers` (positive) or
`avoids` (negative / dead-end) edge to the tool/source it concerns — so
`memory_graph("deploy engine to host")` shows what to reach for and what to
avoid. Retrieval is embedding-on-query (mirrors `memory_world_search`); the
graph edges power structured traversal.

**Retiring a lesson is reversible (schema v37).**
`memory_forget(scope="lesson", ...)` retires the slot rather than deleting
it: the row's `status` becomes `retired` and an FK-free `store_decisions`
row records who, why, and the verbatim record. The undo is
`memory_graph_review(action="restore_slot", store="lesson",
src="entity|attribute")` — restoring from the retired row while it still
exists, or from the audit snapshot once compaction has purged it; a bare
entity in `src` (no `|attribute`) restores every retired aspect of that
entity. `GET /api/curation/retired` lists what's currently retired across
both stores, and the Console's undo route is `POST /api/lessons/restore`.
Only `scope="memory"` and `scope="fact"` still hard-delete.

> Single-writer: `memory_outcome` only ever logs a signal — the dream's LLM
> extractor is the sole writer of lessons. With no extractor configured,
> signals accumulate (pruned by retention) and no lessons are synthesised,
> exactly as the cortex behaves without an extractor. The synthesised
> lessons are **auto-injected at session start** by the
> `pseudolife-mcp briefing` SessionStart hook (the "lessons from past work"
> block) — see [Episodes & session lifecycle](episodes.md).

## Background documents — the reference bank

A fifth layer holds *source material* rather than memories: the **reference
bank**, a ChromaDB chunk store for background documents (papers, manuals,
specs, codebases). `document_ingest(path)` extracts the text of a `.txt` /
`.md` / `.pdf` / `.html` file, chunks it, and indexes it;
`document_search(query)` retrieves chunks by pure cosine similarity.
It is deliberately kept apart from conversational memory — nothing ingested
here feeds the cortex, the graph, or the dream pass, and `memory_search`
surfaces document chunks alongside memories without mixing the stores.

**Division of labor: agents extract meaning; the reference bank preserves
the verbatim source.** These are complementary, not competing paths:

- **Understanding is the agent's job.** A capable agent reading a PDF
  through its own harness (vision, layout, tables, OCR of scanned pages,
  judgment about what matters) will always beat the server's text-layer
  extraction. The intended pattern is that the *agent* reads the document
  and writes the load-bearing conclusions into memory — `memory_store` for
  context, `memory_fact_set` for canonical values, `memory_world_set` for
  cited external facts. That distillate is what future retrieval reasons
  over.
- **Verbatim recall is the reference bank's job.** Distillation is lossy at
  ingest time: the agent keeps what looked relevant *that day*, and
  everything else is gone. Ingesting the raw file as well means
  `document_search` can still answer questions nobody anticipated — a
  one-time local embedding pass instead of re-reading the document through
  an agent's context window on every question.

For an important document, do both: distil the key facts into memory *and*
`document_ingest` the file itself.

> **Scope of the server-side parser.** Extraction is intentionally minimal:
> the embedded text layer only, via `pypdf` (always available) or
> `pypdfium2` (the optional `pdf` extra — better quality, still
> copyleft-free). There is **no OCR** — a scanned, image-only PDF ingests
> as empty text; have the agent read it and store the distillate instead.
> Paths resolve on the **server's** filesystem: with the Docker daemon,
> `document_ingest` needs a path visible inside the container (a mounted
> volume), not a host path.

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

Reads surface this: a serialised fact includes the stamp plus a human `age`
("3 days ago"), and **`memory_history(entity, attribute)`** returns the
full version timeline — current + superseded, oldest→newest, each
attributed. The supersession log records the writer/session too. Since
2026-09-04 the *stamp itself* — `tx_time`, `valid_time`, `writer_id`,
`session_id` — is served by `memory_fact_get(..., verbose=True)`; the
default record carries `asserted_at`/`age`, `last_confirmed` and the
freshness flags, which is what a caller acts on. The REST/Console reads
(`service.*`) are unchanged.

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
