# Deep Dream — operator runbook

Manual, full-corpus graph consolidation. Writes touch the graph only
(cortex/MIRAS untouched); the lesson and world stores are additionally
*listed* for curation (cross-key duplicates), never written.

## 0. What runs by itself

Since 2026-09-02 the sweep timer works every queue this runbook describes
before a human or an agent session ever looks at it, one bounded batch per
queue per tick (`memory.deep_dream.judge_batch`), each mode-gated:

| queue | knob | what `auto` applies |
|---|---|---|
| merge proposals | `judge_mode` (`off` / `shadow` / `auto-reject` / `auto`) | single reject >= 0.8; two-vote reject (second opinion) >= 0.7 mean; `auto` only: two-vote accept on a non-`low_differential` row >= 0.6 mean, and only when the second opinion came from a different model (`judge_second_model`; Console: Deep dream → Merge judge second model, live) |
| link proposals | `link_judge_mode` | accept >= `link_accept_min_confidence` becomes a live edge, `decided_by='dream-judge'`; reject >= `link_reject_min_confidence`; a retype is recorded (`judge_relation`) for a reviewer to apply |
| junk proposals | `junk_judge_mode` | keep >= `junk_keep_min_confidence`; delete >= `junk_delete_min_confidence` only under the evidence bar (degree <= `junk_max_auto_degree`, at most one fact slot) |
| lesson / world duplicates | `curation_judge_mode` | `auto-distinct`: the reversible dismissal; `auto`: also retire the losing slot (reversible — `restore_slot` / `POST /api/lessons/restore`) after folding the carry-over into the surviving lesson |
| link candidates (Step C) | `candidate_judge_mode` (`off` / `shadow` / `auto`) | one slice per tick after each deep apply: `propose` files an edge proposal (source `deep-dream-judge`), `dismiss` marks the pair distinct (for the merge analyzer too); every judged pair is memoised for `candidate_rejudge_days` |

**Turning it all off:** `memory.deep_dream.judges_enabled: false` stops
every judge stage in one move (the mechanical tick keeps running;
`analyzer_file_duplicates` and `orphan_sweep` have their own switches, and
`dream.enabled: false` stops the sweep timer but not a manual deep apply).
A judge that is about to delete or fold writes the graph snapshot first
(the snapshot covers the five graph tables only; a curation `auto` forget
does not need it — since 2026-09-03 a forget retires the row with a
`store_decisions` audit entry and `lesson_restore` / `world_restore` undo
it); each judge applies at most one `judge_batch` slice per tick, which is
also the rate limit. Merge rows judged before this build
carry the judge's CONFIGURED model name; second opinions stamp the SERVED
name, so the distinct-model check also refuses a second opinion from the
same extractor object or the same configured name — a dated served id for
one physical model cannot pass as a second model. **Day-one behaviour on an existing bank:** with
`judge_mode: auto-reject` already in `config.yaml` (the live default since
2026-08-30) and `judge_second_opinion` defaulting on, the reject gate
widens from single-vote >= 0.8 to ALSO two agreeing votes at mean >= 0.7
without any config edit — measured 8/8 on the 2026-09-02 rows — and a
wrong reject — auto or human; since 2026-09-03 every merge reject writes
the canonical pair so the verdict outlives its proposal row — also writes
`dismissed_pairs`, which has no expiry and no un-dismiss route (a SQL
delete of the row is the only undo). Read the
orphan census before switching the sweep on: `memory_dream(action="deep")`
reports `would_orphan_count` / `would_orphan`.

**How much evidence the merge judge reads** is its own knob,
`memory.deep_dream.judge_snippet_max_chars` (Console: Deep dream →
Merge-judge snippet chars), separate from the review surfaces'
`snippet_max_chars`. Leave it at **240** — the cap every published judge
number was measured at. The 2026-09-03 rerun of the same 63 rows at 3000
chars made Opus accept more and be wrong more often (accept precision 0.70
vs 0.85, the two-vote auto-fold gate 6/7 vs 4/4, replicate disagreement
6/63 vs 2/63) while rejects stayed clean
(`evals/results/queue-judge-ladder-20260903-fulllen.json`). Note the
`low_differential` stamp is computed from the truncated texts, so the cap
also moves the auto-accept precondition. Raise it only behind a new ladder
run.

Every verdict is recorded on the row (`judge` / `judge2` blocks in the
review payloads; `curation_judgments` for slot pairs) whatever the mode, so
what is left pending is exactly what the judges could not settle: below-gate
confidence, a `split` second opinion, a `low_differential` accept, a junk
delete above the evidence bar. The apply pass also files the Console's live
analyzer duplicate findings into these queues (`analyzer_file_duplicates`)
and — once you switch it on — deletes week-old entities with no evidence
and no mention (`orphan_sweep`, off by default, at most
`orphan_max_per_apply` per pass, audited `dream-auto` / `deleted`). A
curation verdict memoised under a lower mode is not applied when you raise
the mode; the pair is re-judged after `curation_rejudge_days`. The measured
floor for
each judge is `evals/queue_judge_ladder.py` over
`evals/results/queue-judge-panel-20260902.json`; a mode is only flipped to
`auto` where that artifact supports it (see the CHANGELOG entry).

## 1. Preview (no writes)
Call `memory_dream(action="deep")` (dry-run by default). Review:
- `rescored` — agent edges whose confidence will change.
- `would_supersede` — hard type-violation edges to be auto-superseded.
- `would_merge` — exact-duplicate entity pairs to be merged.
- `would_merge_propose` / `would_junk` — review-queue proposals; items flagged
  `already_proposed: true` will be skipped by apply (the dedupe indexes cover
  any status, so rejected proposals are sticky).
- `candidates` — semantic cross-session link candidates (src/dst + truncated
  context snippets; `snippets=false` omits them).
- `merge_proposals` — pending near-duplicate merges (write-time dedup +
  analyzer), each side enriched with display/etype/degree/scopes/snippets;
  accept folds `from` into `into` as shown. Each side's snippets lead with
  entries exclusive to that side (shared co-mentions only fill remaining
  slots), and the row carries `evidence_overlap` (shared share of the shown
  snippets) plus `low_differential: true` when a side has no snippets, the
  sides share at least half, or one side's evidence pool is wholly
  contained in the other's — evidence that cannot distinguish the
  referents. (Both fields ride the snippets: `snippets=false` omits them
  along with the evidence itself.) The direction is re-derived
  from CURRENT evidence (degree + fact count) at both display and accept
  time — insert-time orientation goes stale as the graph grows — so a
  batch of accepts can legitimately flip a later pair's direction between
  the listing and the click.
- `lesson_duplicates` / `world_duplicates` — cross-key near-duplicate slot
  pairs in the lesson / world stores (slot supersession only dedups within
  one key, so these accumulate silently). Listing-only, in dry-run AND
  apply; settle them in step 3c.

## 2. Apply self-clean
`memory_dream(action="deep", apply=true)`. The daemon first dumps the five
graph tables to `data_dir/graph_snapshots/graph-<stamp>.json` (the `snapshot`
field in the response; newest `memory.deep_dream.snapshot_keep` files kept) and
refuses with `snapshot_failed` if the dump can't be written. A full
`pwsh ops/backup.ps1` on the host remains good practice before big passes, but
the in-daemon snapshot is now the enforced floor.

Apply then re-scores agent edges and — **unless you have set
`memory.deep_dream.auto_apply_safe: false`, it defaults to `True`, so this
happens automatically and unreviewed** — supersedes violating edges and merges
exact-duplicate entities. Those two halves are NOT equally reversible:

- **Supersession is reversible.** It is an `UPDATE edges SET superseded_at`
  (`storage/postgres.py::supersede_edge`); the row survives and an explicit
  human re-assertion revives it.
- **An exact-duplicate merge is a delete, and is not reversible from inside
  the daemon.** After re-pointing edges, fact/lesson refs, aliases and sources
  onto the survivor, `merge_entity` runs `DELETE FROM entities` on the
  folded-away entity, and the FK CASCADE takes that entity's leftover aliases,
  sources, community rows and edges with it. Nothing anywhere in the daemon
  reads a `graph_snapshots/*.json` file back in — the snapshot path only
  writes and prunes. Treat the snapshot as a forensic record of what the graph
  looked like beforehand, not as a restore path. Rolling back a bad merge
  means `ops/restore.ps1` from a backup.

Set `memory.deep_dream.auto_apply_safe: false` if you want the merges to
require a verdict instead. The review-queue proposals (`merge_proposed`,
`junk_proposed`) are populated either way — that half is non-destructive.

## 3. Step C — settle candidates (this session)
Judge each `candidate` from its `src_snippets`/`dst_snippets` (dispatch
subagents for large batches — reuse the
`evals/relation_extraction_bench.py --emit-prompts` prompt shape):
- **Related** → collect `[{src, relation, dst, similarity, rationale}]` and call
  `memory_graph_review(action="propose", proposals=...)`. The gate
  (edge_confidence + is_hard_type_violation) drops junk automatically.
- **Distinct** (name-similarity or shared-context noise) →
  `memory_graph_review(action="dismiss_pair", src=..., dst=...)` — the pair
  stops resurfacing and frees its top-k slot.
- **Unsure** → leave for Atlas; don't guess.

## 3b. Step C — triage entity proposals (this session)

**Near-duplicate merges.** Judge each `merge_proposals` item from its per-side
snippets/scopes. A proposal a background sweep has already judged carries a
`judge` block (verdict/confidence/note/model, schema v30) — treat it as a
lead, never a decision: read the evidence yourself and disagree freely.
A `low_differential: true` item warrants extra skepticism: its shown
evidence cannot tell the two names apart, so a merge needs support beyond
the snippets (name shape alone is not enough — rule 1 of the judge prompt).
Items sharing a `group` value pivot on one entity (the
write-dedup detector files up to three matches per mint) and are ONE
where-does-it-belong decision — accept at most one; the first accept
deletes the shared entity:
- **Same referent** → `memory_graph_review(action="accept_merge",
  proposal_id=...)` — applies immediately and irreversibly (it deletes the
  `from` entity, per step 2); logged to the recent-merges audit as
  `decided_by=agent`.
- **Distinct** → `memory_graph_review(action="reject_entity", proposal_id=...)`
  plus `dismiss_pair` so the pair never re-proposes.
- **Unsure** → leave pending; disjoint `scopes` is a strong distinct signal.

**Junk entities.** The dream also proposes over-extraction artifacts for
deletion — `would_junk` in the dry run, counted as `junk_proposed` on apply.
On `apply=true`, a pending junk proposal whose entity carries **no edges
and at most the one fact slot it was minted from** is auto-deleted
(`junk_deleted` in the result, recorded as a `dream-auto` decision in
`merge_decisions`; the pre-apply graph snapshot is the undo, and the node
simply re-mints on next mention). Accepting a junk proposal also writes a
durable tombstone in `merge_decisions`: if the same name re-mints and is
re-flagged, it is auto-deleted with the **degree** half of that guard
relaxed to the detector's own `junk_max_degree` bar — so an `apply=true`
deletes more than the zero-edge rule alone would; never-judged names keep
the strict guard. The fact-count half holds either way: a tombstone is
permanent (nothing removes a `merge_decisions` row), so a name that has
since become a real, fact-bearing entity must not be deleted unattended
on the strength of an old verdict. Fact slots are counted by subject name
as well as through the fact-to-entity cross-index, so facts orphaned by an
earlier deletion (which NULLs the link) still count as evidence. Anything
evidence-bearing — an edge past the applicable bar, more than one fact
slot — sits pending until someone votes.
No merge proposal is filed whose side is junk-flagged.
`merge_proposals` does NOT carry them: get their `proposal_id`s from
`memory_graph_review(action="list")`, where they appear as `kind: "junk"`.
- **Genuinely junk** (extraction noise: a fragment, a mis-parsed span, an
  entity with no real referent) → `memory_graph_review(action="accept_junk",
  proposal_id=...)`. This is a **hard delete of the entity**, with the same
  CASCADE and the same non-reversibility as a merge — read the reason and the
  display name before accepting, and prefer leaving it pending when unsure.
- **A real entity** → `memory_graph_review(action="reject_entity",
  proposal_id=...)` — keeps the entity, closes the proposal, and records a
  `junk:<canonical>` keep tombstone in `dismissed_pairs` so the name is
  never re-filed or auto-deleted after a re-mint.
- **Unsure** → leave pending; it costs nothing but a queue slot.

## 3c. Step C — settle lesson/world duplicate listings (this session)
Judge each `lesson_duplicates` / `world_duplicates` pair from the values shown
(each side carries entity/attribute/value, plus polarity/outcome/about for
lessons and source_url for world facts). Nothing is ever auto-deleted:
- **Duplicate** → keep the better-keyed slot; drop the other via
  `memory_forget(scope="lesson"|"world", ...)` (or re-write the surviving
  slot first to fold in anything the dropped one added). A forget RETIRES
  the slot (row kept, audit row in `store_decisions`): undo it with
  `memory_graph_review(action="restore_slot", store=..., src="entity|attribute")`
  or `POST /api/lessons/restore` / `POST /api/world/restore`;
  `GET /api/curation/retired` lists what is currently retired.
- **Distinct** → `memory_graph_review(action="dismiss_slot_pair",
  store="lesson"|"world", src=<a_key>, dst=<b_key>)` (REST equivalent:
  `POST /api/curation/dismiss-duplicate`) — the pair is persisted
  (namespaced in `dismissed_pairs`) and never re-listed.
- **Unsure** → leave listed; the pair costs one of the
  `memory.deep_dream.curation_top_k` slots until settled.

The same pairs are reviewable by a human in the Console: the Atlas Review
drawer's "Store curation" panel (fed by the standing
`GET /api/curation/duplicates`, so no dream run is needed) renders each side's
entity/attribute/value plus context and offers the distinct verdict as a
confirm-gated "Mark distinct" button.

## 4. Confirm in Atlas
Open Atlas Review → `proposed_link` findings → accept (promotes to a real edge)
or reject, per item. With `link_judge_mode: auto` the sweep has already
settled every link whose verdict cleared its gate; what remains carries the
link judge's `judge` block (verdict, confidence, note, and the corrected
relation of a retype) beside the evidence. Nothing reaches `edges`/recall
until a verdict — yours, an agent's, or the judge's — accepts it. The
"recent merge decisions" list under the queue shows what was applied or
rejected in step 3b (decided_by=agent, or dream-judge for the sweep's own
verdicts), newest first.
