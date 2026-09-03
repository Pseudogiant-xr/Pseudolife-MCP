<!-- examples/commands/dream.md
Copy to .claude/commands/dream.md in any project to get /dream. -->
---
description: Run a judgment session over the Pseudolife memory bank — triage the review queues; extract facts only where no extractor can
---
Run a judgment session over the Pseudolife-MCP bank. The pipeline halves
run themselves — the auto-sweep extracts facts through the configured
extractor, and a need-based tick applies the deep dream's mechanical half
(rescore, guarded junk deletes, scope stamping, proposal filing, analyzer
duplicate filing, the unreachable-orphan sweep, snapshot-first). Since
2026-09-02 the sweep also judges every queue itself (merge second opinion,
link, junk, store-curation and candidate judges — each mode-gated, see
`docs/runbooks/deep-dream.md` §0), so what this command meets is the
residue: rows whose verdict sat below a gate, a `split` second opinion, a
`low_differential` accept, a junk delete above the evidence bar. Every
such row carries the judge's `judge` / `judge2` blocks — treat them as
leads, read the evidence, and disagree freely.

1. Call `memory_dream(action="status")` and read three things:
   - `deep_dream` — `{recommended, reason, new_entities, days_since}`:
     whether the mechanical pass is due. The tick normally handles this;
     if `recommended` is still true (tick disabled, or the daemon just
     restarted), run `memory_dream(action="deep", apply=true)` yourself —
     the daemon snapshots the graph tables first (`snapshot` in the
     response is the undo file); a `snapshot_failed` error means nothing
     was changed — investigate, don't retry blindly. Otherwise run
     `memory_dream(action="deep")` (dry-run) to fetch the queues with
     evidence snippets.
   - the extractor fields (`primary_url` / `fallback_url`): whether this
     deployment has ANY automatic cortex writer.
   - `backlog` / `would_fire`: whether unconsolidated memories are waiting
     for the next sweep.
2. ONLY when no extractor endpoint is configured (primary and fallback
   both null) or both are unreachable: you are this deployment's only
   cortex writer — run the manual extraction pass:
   - `memory_dream(action="pull")` (default limit).
   - From the pulled text, extract only **durable, current-state,
     slot-shaped** facts as `(entity, attribute, value)`. Skip narrative,
     in-progress work, and superseded states. Reuse existing slot keys
     where they fit.
   - Write each with `memory_fact_set` (origin `user` only for things the
     human stated; otherwise `agent`). If the fact is a MEMBER of a set
     the user maintains (bikes owned, pending tasks), use `memory_set_add`
     / `memory_set_remove` instead — `memory_fact_set` on a set-valued
     slot errors and names the right tool.
   - `memory_dream(action="commit", cursor=<newest timestamp from the
     pull>)`.
   - Surface any `contested` results to the user — those are conflicts to
     settle, not silent overwrites (an add onto a number-led scalar parks
     as a contender by design; `member_capped` means the 100-member cap
     was hit).
3. Work the returned `candidates` (Step C). For each pair, judge from the
   `src_snippets` / `dst_snippets` evidence, never from names alone:
   - **Related** (one uses/contains/produces the other, etc.): submit via
     `memory_graph_review(action="propose", proposals=[{src, relation, dst,
     rationale}])` with a specific relation and a one-line rationale.
   - **Distinct** (similar names or shared context only — e.g. opposite verbs,
     siblings under one parent):
     `memory_graph_review(action="dismiss_pair", src=..., dst=...)` so the pair
     never resurfaces and stops occupying a top-k slot.
   - **Unsure**: leave it — the pair stays visible for the Console's Atlas
     queue. Do not guess.
4. Triage the returned `merge_proposals` (near-duplicate entities, mostly from
   the write-time dedup detector). Each carries per-side `display`, `etype`,
   `degree`, `scopes`, and `snippets`; accepting folds `from` into `into` as
   shown — the direction is re-derived from current evidence at both display
   and accept time, so an accept earlier in the batch can flip a later
   pair's direction. Rows sharing a `group` value pivot on one entity and
   are ONE where-does-it-belong decision: accept at most one of them (the
   first accept deletes the shared entity), and settle the others with
   reject/dismiss or leave them. Rows may carry a `judge` block — the
   sweep's shadow pre-judgment (verdict/confidence/note + which model):
   treat it as a lead, not a decision; verify every accept against the
   evidence yourself (the shipped judge floor is measured in
   evals/results/judge-ladder-20260816.json). Judge
   from the snippets, never names alone — the bank's confirmed-distinct history
   (postgres vs postgres.py) is exactly why:
   - **Same referent** (naming-layer variants of one thing — file suffixes,
     abbreviations, display drift): `memory_graph_review(action="accept_merge",
     proposal_id=...)`. The merge applies immediately (the graph snapshot from
     step 1 is the undo artifact) and is logged to the recent-merges audit as
     decided_by=agent.
   - **Distinct things**: `memory_graph_review(action="reject_entity",
     proposal_id=...)` AND `memory_graph_review(action="dismiss_pair",
     src=..., dst=...)` so the pair never re-proposes.
   - **Unsure**: leave pending for the Atlas queue. Do not guess; scopes that
     don't overlap are a strong distinct signal.
5. Triage the junk verdict — over-extraction artifacts the analyzer wants
   pruned. The dry-run reports them as `would_junk`
   (`{entity, reason, already_proposed}`); apply reports a `junk_proposed`
   count and files them in the review queue, where
   `memory_graph_review(action="list")` surfaces a `junk_candidate` finding
   whose `entities` carry the `id` you need. Judge by the `reason`:
   - **Artifact** (`concat-artifact`, `list-artifact`, `compound-artifact`,
     `bare-number`, `status-word` …): `memory_graph_review(
     action="accept_junk", proposal_id=...)`. This DELETES the entity — the
     step-1 snapshot is the only undo.
   - **A real thing that merely looks thin** — short, weakly-connected names
     are often legitimate ("Go", "uv"):
     `memory_graph_review(action="reject_entity", proposal_id=...)`.
   - **Unsure**: leave it pending for the Atlas queue. Junk deletion is the
     one irreversible verdict in this flow — the step-1 snapshot is the
     only undo (lesson/world forgets below are reversible; see step 6).
6. Triage the returned `lesson_duplicates` / `world_duplicates` (cross-key
   near-duplicate slots in the lesson / world stores; listing-only — the
   dream never deletes them). Judge from the per-side values:
   - **Duplicate**: keep the better-keyed slot and drop the other via
     `memory_forget(scope="lesson"|"world", ...)`, folding anything the
     dropped slot added into the survivor first. This now RETIRES the
     slot rather than deleting it (row kept, `store_decisions` audit
     row) — a wrong call is undoable with
     `memory_graph_review(action="restore_slot", store="lesson"|"world",
     src="<entity>|<attribute>")`, so lean toward acting rather than
     leaving a genuine duplicate pending.
   - **Distinct**: `memory_graph_review(action="dismiss_slot_pair",
     store="lesson"|"world", src=<a_key>, dst=<b_key>)` so the pair
     never re-lists.
   - **Unsure**: leave listed. Do not guess.
7. Report: what the mechanical pass did (or that the tick already had),
   proposed / dismissed counts, merges you applied or rejected (they appear
   under "recent merge decisions" in Atlas, beside the accept-rate stat),
   junk entities deleted or kept, lesson/world pairs settled, contested
   facts if step 2 ran, and the snapshot filename. Link proposals still
   need a human verdict (`accept_link` / `reject_link` or Atlas).
