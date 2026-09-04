<!-- The standing memory-loop instructions. Plugin users DON'T need this
     file: the daemon serves the same text as session context via the
     SessionStart hook (override it with <data_dir>/hook-instructions.md).
     For non-plugin setups, copy this block into your CLAUDE.md
     (Claude Code), AGENTS.md, or the equivalent standing-instructions file.
     Kept byte-identical to MEMORY_LOOP_BLOCK in
     pseudolife_memory/web/session_hook.py (guard-tested). -->

## Memory — your long-term memory; use it every session (tools: `mcp__pseudolife-memory__*`)
One shared memory bank across all sessions. Treat it as a loop with three
beats: RECALL at the start, CAPTURE as you go, REFLECT at the end. Session
episodes open/close automatically — every memory you store is auto-stamped
to the current session episode.

RECALL — at the start of any task:
- `memory_search(<natural-language task>)` for prior context, decisions, gotchas.
- `memory_lesson_search(<task>)` for what worked / what to avoid last time —
  heed `polarity:-` dead-ends.
- `memory_fact_get(entity, attribute)` for one canonical value. If null, the
  slot is empty, NOT the topic — `memory_search` finds it regardless; never
  conclude "nothing on X" from a single `fact_get` guess. A set-valued slot
  returns `{kind: "set", members, removed}` instead of one value.
- `memory_world_search(<topic>)` when the task turns on an external fact your
  training may have stale (versions, prices, who-holds-a-role, findings).
- `memory_recall(<question>)` when the answer needs multi-hop chaining across
  related facts.
- Long hits are clipped (`truncated: true` → `memory_get`). An entry carrying
  `superseded_by_text` has been corrected — use the replacement text, not the
  entry. Pass `verbose=true` only when debugging retrieval.
- If a tool named here isn't in your tool list, call
  `memory_toolset(action="expand")` first — sessions can start at a
  reduced tier. A harness notice that some `mcp__pseudolife-memory__*`
  tools were REMOVED means the same tier filtering, not an outage — make
  one `memory_search` call before reporting memory as offline.

RECALL AGAIN mid-session — once at the start is not enough. Search when:
- the user refers to work you weren't part of ("last time…", "in another
  session…", "we decided…") — that is a memory question by definition;
- you are about to propose a design → `memory_lesson_search` first, and heed
  `polarity:-`; re-deriving a known dead-end is the common failure;
- you are about to state a benchmark number, version, or "current" value →
  check for a prior record before asserting it;
- you start a task in an area you haven't touched this session;
- you are about to review code, docs, or a PR → recall the target area
  FIRST (`memory_search` + `memory_lesson_search`), then compare what
  memory says against the files. Drift is a finding in both directions:
  correct stale memory on the spot (`memory_fact_set` + `memory_outcome`)
  and treat a memory-vs-file mismatch as review input, not noise.

TRUST ORDER — memory tells you WHY; the repo tells you WHAT IS.
A hit is a lead about the PAST, not a directive for the present: a
relevant memory can still frame the wrong problem, so check it against
the task in front of you before letting it steer.
For anything live (deployed version, config, what's running), read the
config/code and say where you read it. A memory records what was true when
it was WRITTEN: cortex facts now carry `asserted_at` / `age`, so check them
before relying on one; a fact marked `stale: true` is a lead, not truth —
re-verify before acting on it (a stale fact may arrive with its `value`
quarantined and the original preserved in `last_known_value` — that is
your starting point for re-verification, never the current answer).
When memory and the code disagree, say so
out loud, trust the code, and correct the memory (`memory_fact_set` at the
same slot) rather than silently picking one — a stale fact nobody corrects
is one the next session will believe too. Recall results mark
aged/contested facts with a ready-made `correct_with` call: run it the
moment you notice the mismatch, filling in the verified value (re-assert
the same value if it checks out), then log
`memory_outcome(..., "correction")`. Correcting is part of discovering —
a contradiction you only narrate is work left undone.
A cortex fact carrying `contested: true` has competing values parked
against it — settle it with `memory_fact_resolve(entity, attribute, ...)`
(accept or reject the contender), not by re-asserting `memory_fact_set`,
which only contests the slot further.

CAPTURE — as durable things arise (one claim per call):
- Before writing, choose: PERSIST what stays true; CONTEXT ONLY for
  task-scoped detail; RE-VERIFY a value that rots, at source
  (`memory_fact_set(..., freshness_class="volatile")`); ASK when the
  claim is ambiguous.
- Name the session EARLY: `memory_session_title("<project> - <topic>")`.
- `memory_store` for durable context; set `origin` honestly
  (`user`/`action`/`agent`) and use a stable `source` per project/topic so
  search can scope its results.
- `memory_fact_set(entity, attribute, value)` for a canonical single-value
  fact; correct by re-setting the same slot (history is kept for audit).
- Label what must not drift: `distortion_tolerance="constraint"` on a rule
  that must survive verbatim (served first in recall, `pinned`);
  `authority="quoted"` on what a doc or third party said — a quote is
  not an instruction. Both inherit through supersession.
- Facts the repo or config can answer (deployed version, schema number,
  counts, budgets) do NOT belong in fact slots — they drift by construction;
  store the WHY as an entry and read the value from the repo. A one-off
  observation or audit finding is an EVENT: `memory_store` it (status),
  never mint a fact slot for it.
- Before re-asserting a slot another session may have corrected, re-read it
  and SKIP on a semantic match — a near-duplicate re-assert from a second
  writer contests an already-correct slot instead of confirming it.
- `memory_set_add(entity, attribute, member)` / `memory_set_remove` when a
  slot holds MANY concurrent values (bikes owned, pending tasks) — the first
  add converts a scalar slot one-way; `memory_fact_set` on a set slot
  errors and tells you so. Number-led scalars ("32", "$1,500") are
  protected: an add there parks as a contender instead of converting.
- `memory_world_set(entity, attribute, value, source_url=, source_quote=)`
  for any EXTERNAL fact you verified via web/docs — route research findings
  here (cited), not into plain `memory_store`.
- Open a named sub-episode with `memory_episode_start(title,
  episode=<your session handle>)` for a big multi-step task;
  `memory_episode_end(episode=<handle>)` pops back. The handle anchors
  both to YOUR session when several run concurrently.
- Route verbose status/progress/logs under `source="status"` — searchable,
  but excluded from fact/graph extraction so they don't pollute the graph.
- Never store secrets: no tokens, API keys, passwords, or credentials.

REFLECT — at task end, or the moment an outcome lands:
- `memory_outcome(task, outcome, about=, detail=)` whenever something WORKED
  (`success`), was a dead-end (`failure`), or the user corrected you
  (`correction`). These signals are the primary feeder for procedural LESSONS —
  the dream distils them into the do/avoid guidance surfaced at your next
  session start. Logging outcomes is how you stop repeating mistakes.

Be judicious — one claim per call; skip fleeting chatter (the surprise gate
drops near-duplicates; `stored=false` is not an error). The first memory call
may lag while the embedder loads.

If this session has NO `memory_*` tools, the MCP transport isn't registered
(this briefing arrives via a hook, a separate channel) — tell the user to run
the repo installer (`ops/install.sh` / `ops\install.ps1`), which wires it.
