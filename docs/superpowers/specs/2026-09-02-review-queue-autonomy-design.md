# Review-queue autonomy — every queue gets a judge

Status: IMPLEMENTING (this branch). Extends the 2026-08-16 Step-C merge judge
(schema v30, `memory.deep_dream.judge_mode`) to the four queues it left for
humans, and closes the two mechanical gaps that keep refilling them.

## Problem

The user's standing directive (2026-08-16): PseudoLife is a memory system by
agents for agents; the human review queue should hold only what machine
judgment genuinely cannot settle. On 2026-09-02, 34 hours after a full
manual clear-out, the live queue held:

| queue | pending | what settles it today |
|---|---|---|
| merge proposals | 63 (31 judge-accept / 32 judge-reject, all below the 0.8 auto-reject gate) | a human or an agent session; the judge only auto-rejects at >= 0.8 |
| junk proposals | 20 (all evidence-bearing, so the zero-structure auto-delete skipped them) | nobody |
| link proposals | 37 (cross-project and retyped co-mention edges, ~1-3 filed per day) | nobody |
| Step-C link candidates | 42 per dry-run | an agent session (`/dream`) |
| lesson / world duplicate slots | 20 + 20 (top-k listings) | nobody |
| analyzer duplicate findings | ~90 (live token-Jaccard, recomputed per Console load) | nobody — never filed anywhere |
| weakly-connected entities | 2428 (informational) | nobody |

Each of these is judged today, when it is judged at all, by an interactive
agent session running the same small policy with the same evidence the
daemon already attaches. The 2026-08-21 shadow comparison
(`evals/results/judge-shadow-live-20260821.json`) and the 2026-08-30 panel
established the merge judge's floor: auto-reject at >= 0.8 is 1.000 precise
(76/76, Wilson 95% CI 0.95-1.0); accepts are 0.61-0.70 precise; the
0.7-0.8 band agrees 89%, below 0.7 65%.

## Design

One principle throughout: **the daemon applies a verdict only where a wrong
verdict is cheap or reversible; everything expensive stays pending with the
model's opinion attached.** Every applied verdict is audited
(`decided_by='dream-judge'` in `merge_decisions`, `status` + judge columns
on proposal rows, `curation_judgments` rows) and every apply runs after the
same graph snapshot the deep dream already writes.

### A. Merge proposals — second opinion + guarded auto-accept

* **Second opinion.** A pending merge whose first verdict sits below the
  apply gate is re-judged once (`judge_second_opinion: true`), in a fresh
  batch (different neighbours; independent sample from the same model, or
  `judge_second_model` when set). A reject is applied when BOTH opinions
  say reject and the mean confidence >= `judge_reject_min_confidence_2`
  (0.7). Disagreement stamps `judge_note = "split: …"` and the row stays
  pending — that is the class humans should see.
* **Auto-accept, gated.** `judge_mode: auto` applies an accept only when
  ALL hold: both opinions say accept, the row is NOT `low_differential`
  (its shown evidence can tell the sides apart), and the mean confidence
  >= `judge_accept_min_confidence` (0.6), and the two opinions come from
  DIFFERENT models (`judge_second_model`) — a same-model second vote at
  temperature 0 is independent only through batch composition (2/129
  flips on the 2026-08-16 ladder), enough to double-check a reject, not
  to authorize a fold; the name vetoes (`merge_veto`, `variant_conflict`)
  hold at apply time. A single vote never folds. The
  fold direction is re-derived from current evidence at apply time as for
  every accept, so a thin side always folds into the richer one. The
  2026-08-05 proposal's human tripwire (refuse when both sides are
  evidence-rich) was measured and not shipped — see "Measured" below.
  Default stays `auto-reject`; `auto` is opt-in behind the eval gate.

### B. Junk proposals — a judge with a provenance brief

`judge_junk(rows)` sends each pending junk proposal with the evidence a
reviewer uses: display, detector class, degree, its edges (capped), fact
count and fact text (capped), whether it is a **lesson-minted object**
(only `prefers`/`avoids` edges of origin `action`), scopes, and mention
snippets. Verdicts `delete | keep | leave`. Recorded in the existing v30
judge columns (`entity_proposals` rows of kind `junk`).
`junk_judge_mode: off | shadow | auto`. In `auto`:

* `keep` at >= `junk_keep_min_confidence` (0.8) → `reject_entity`
  (non-destructive; the tombstone table is untouched).
* `delete` at >= `junk_delete_min_confidence` (0.85) → `accept_junk`
  ONLY under the evidence bar: degree <= `junk_max_auto_degree` (3) and
  at most one fact slot; a lesson-minted object passes the degree bar by
  construction (deleting it only nulls the lesson's pointer). Anything
  richer stays pending with the verdict attached.

### C. Link proposals — a judge whose accepts are reversible

`judge_links(rows)` judges pending `edge_proposals` with: both displays,
each side's existing edges (in/out, capped), scopes, the detector's
rationale, and the notes naming BOTH entities (`shared_mention_entries`;
per-side mentions when nothing names both). Verdicts
`accept | reject | retype(relation) | leave`. Schema v36 adds the same five
judge columns to `edge_proposals` plus `judge_relation` (the retype).
`link_judge_mode: off | shadow | auto`. In `auto`, accept at
>= `link_accept_min_confidence` (0.8) promotes the edge exactly as
`accept_link` does (origin `action`, confidence floored at 0.7); retype at
the same gate writes the corrected relation and marks the row `retyped`;
reject at >= `link_reject_min_confidence` (0.8) marks it rejected. Edges
are the one irreversible-free verdict in this system (a later
`memory_graph_unrelate` / supersede retracts), which is why this queue is
the first candidate for a default-on `auto`.

### D. Step-C candidates — filed, then judged as links

The deep dream's `candidates` (unlinked near-pairs) currently wait for an
agent to `propose` or `dismiss` them. The apply path now routes each
candidate through the link judge directly (`judge_candidates`: the same
prompt with "no relation" as a verdict): `propose(relation)` files an
`edge_proposals` row that the link judge then settles like any other;
`dismiss` records the pair in `dismissed_pairs`; `leave` keeps the slot.
The candidate never becomes a live edge without passing the link judge's
gate, so the two stages compose.

### E. Store curation — judged pairs remembered

`judge_slot_pairs(pairs)` judges the lesson/world duplicate listings with
both slots' full labels. Verdicts `duplicate(keep a|b) | distinct | leave`.
A new `curation_judgments` table (v36) records every verdict so a pair is
not re-sent every sweep (`curation_rejudge_days`, 30).
`curation_judge_mode: off | shadow | auto-distinct | auto`:
`auto-distinct` applies distinct verdicts at >= 0.8 via the existing
`curation_dismiss_duplicate` (reversible: delete the `dismissed_pairs`
row); `auto` additionally forgets the losing slot of a duplicate verdict at
>= `curation_forget_min_confidence` (0.9) after re-writing the survivor
with anything the judge said to fold in (lessons only — world facts are
cited values and are never concatenated). Ships `shadow`; `auto-distinct`
is the intended default once the ladder supports it.

### F. Analyzer duplicates — filed, not just displayed

`graph_review`'s live token-Jaccard duplicate findings were never filed
anywhere, so the Console re-listed the same ~90 pairs on every load and no
judge ever saw them. `deep_dream(apply=True)` now files them:
`action: merge` pairs into `entity_proposals` (kind `merge`, reason
`analyzer-duplicate`, same vetoes and junk-first routing as the write-dedup
detector; the dedupe index keeps rejected pairs sticky) and
`action: relate` file/concept pairs into `edge_proposals`
(`<file> implements <concept>`, source `analyzer`). From there the merge
and link judges settle them.

### G. Unreachable orphans — the one safe deletion class

Of the 2428 weakly-connected entities, 50 carry NO evidence at all: degree
0 counting superseded edges, no fact by id or by name, no lesson reference,
no alias, no scope, and — the apply-time check that needs the mentions map
— no current entry mentions them. Such a node cannot be reached by any
read path; it is a mint whose only source was later forgotten or
superseded. `orphan_sweep` (off by default; at most `orphan_max_per_apply` per
pass) deletes them in the apply pass when older than
`orphan_min_age_days` (7), audited as `dream-auto` /
`orphan-unreachable` in `merge_decisions`. Everything else in the orphan
finding stays informational.

### H. Stop the refill: lesson objects

`_link_lesson_graph` minted a graph entity for every lesson `about`, so a
list-shaped or sentence-shaped `about` ("evals/leak_check.py, committed
BEAM result artifacts") became a junk proposal on the next tick — 11 of the
20 pending junk rows on 2026-09-02 were exactly this. The mint now consults
`junk_name_reason` like every other write path; the lesson keeps its
`about` text, it just gets no graph node.

## Sweep integration

`run_sweep_once` already runs `deep_dream_judge` after the deep tick. The
judge stage becomes a sequence of bounded batches, each mode-gated and
`getattr`-guarded like the merge batch: merges (first + second opinion),
junk, links, candidates, curation. Each batch is one model call of at most
`judge_batch` rows; a transport failure marks nothing and the next sweep
retries. Rows the model skips are stamped `leave` at confidence 0 so a
stubborn batch head cannot starve the queue (the 2026-08-16 rule).

## Evaluation gate

The 2026-09-02 blind panel (seven Opus agents over group-aligned slices,
main-thread verification of every accept and delete) is the ratified set:
`evals/results/queue-judge-panel-20260902.json` carries every row's evidence
pack exactly as the judge saw it and the panel's verdict.
`evals/queue_judge_ladder.py` replays the SHIPPED prompts against it per
queue and reports, per arm, the metric that gates each auto mode:

| queue | gating metric | ships as | flip to |
|---|---|---|---|
| merge accept | two-vote non-low-differential accept precision | `shadow` (code default); the live bank runs `auto-reject` | `auto` when >= 0.95 (measured 6/6) |
| merge second opinion | two-vote reject precision | on (measured 8/8) | — |
| junk delete | delete precision at the gate, under the evidence bar | `shadow` | `auto` when >= 0.95 |
| link accept / reject | accept precision, reject precision at 0.8 | `shadow` | `auto` when both >= 0.9 (reversible) |
| curation distinct | distinct precision at 0.8 | `shadow` | `auto-distinct` when >= 0.9 |
| candidates | propose + dismiss precision at 0.6 | `off` | `auto` when both >= 0.85 |

The code defaults stay conservative so a fresh install never applies a
verdict its operator has not measured; the deployed bank's `config.yaml`
carries the flips the artifact supports.

A default is set from the artifact in the same commit that adds the row
to `tests/test_eval_evidence.py`; a number without its artifact does not
ship.

## Measured 2026-09-02 (merge queue, the 63 residual sub-gate rows)

Three replays of the shipped `judge_merges` path over the rows the blind
panel then settled (25 true folds; 38 not-a-merge, of which 25 were
related-but-distinct): the daemon's own shadow verdict, a second
claude-opus-5 pass in shuffled batches, and claude-fable-5
(`evals/results/queue-judge-panel-20260902.json`, `merge_gate_table`).

| policy | n applied | wrong | note |
|---|---|---|---|
| single shadow accept >= 0.8 | 9 | 0 | small n; 4/4 on 2026-08-21 |
| two-vote accept, mean >= 0.7 | 12 | 1 | the one is right-pair-wrong-direction (2220) |
| two-vote accept, mean >= 0.6, not low-differential | 6 | 0 | **shipped gate** (shadow Opus + Fable; the other distinct pairing, second Opus + Fable, is 5/6 — fair figure 11/12 on n=6 each) |
| two-vote accept, any confidence, not low-differential | 7 | 0 | |
| three-vote accept, not low-differential | 7 | 0 | Fable adds no coverage |
| single shadow reject >= 0.7 | 10 | 0 | the >= 0.8 gate had already cleared its rows |
| two-vote reject, mean >= 0.7 | 8 | 0 | **shipped gate** |
| two-vote reject, mean >= 0.6 | 23 | 2 | 2059, 2157 — thin folds the panel accepted at 0.60-0.65 |

Single-vote accept precision on these rows: shadow 0.74, second Opus pass
0.71, Fable 0.65. 43 of the 63 rows are `low_differential`; the flag, not
the vote count, is the binding constraint on accept coverage — better
evidence differentiation is the next lever, not a third judge. The human
tripwire from the 2026-08-05 proposal (both sides evidence-rich) was
measured and not shipped: it left one row in coverage.

## Ladder, first run (2026-09-02, claude-opus-5, two replicates)

`evals/results/queue-judge-ladder-20260902.json`, arm `opus-r2` — the
shipped prompts replayed over the private pack with the harness as first
committed (max_tokens 400, replicates in identical row order; the artifact
records the caveat). Majority vote of the two replicates against the panel:

| queue | metric at the shipped gate | result |
|---|---|---|
| links | auto-accept >= 0.8 / auto-reject >= 0.8 | 4/4 / 5/5 (accept precision overall 0.83 on 24) |
| junk | auto-delete >= 0.85 under the evidence bar / auto-keep >= 0.8 | 6/6 / 7/7 (delete 11/11, keep 9/9 overall) |
| curation | auto-distinct >= 0.8 / duplicate keep-side precision | 21/21 / 0.56 (9/16) — forgetting stays off |
| candidates | auto-propose >= 0.6 / auto-dismiss >= 0.6 | 7/8 / 15/16 (relation-strict propose 6/10) |
| merges | two-vote reject >= 0.7 / two-vote non-low-diff accept >= 0.6 | 8/8 / 4/4 |

Reading: every reversible or evidence-barred auto path scored perfectly at
its gate on this set, at small n; the destructive paths that did not — the
curation forget (keep-side 0.56) and unguarded merge accepts — stay behind
their gates. The ladder's two-vote accept is two replicates of ONE model,
a configuration the code refuses for folds; the artifact records that it
ran at max_tokens 400 in identical row order, which the committed harness
(2048, shuffled) does not reproduce — a rerun is owed before any accept
default moves. Retype scored 0/1, so retypes are recorded, never
auto-written. The candidate judge's relation choice differs from the panel's
in 4/10 proposals, which is why a proposal is filed for the link judge
rather than written as an edge.

## Schema v36

Additive, idempotent:

* `edge_proposals.judge_verdict / judge_confidence / judge_note /
  judge_model / judged_at / judge_relation / decided_by / decided_at` — the
  link judge's opinion and who settled the row (NULL = not yet judged,
  exactly the pre-v36 behaviour); `entity_proposals.judge2_*` — the merge
  judge's second opinion.
* `curation_judgments(store, a_key, b_key, verdict, keep, fold,
  confidence, note, model, judged_at)`, PRIMARY KEY (store, a_key, b_key).

## Out of scope

* Judging contested facts (verifier hooks, not LLM verdicts — unchanged).
* Any change to the extraction prompt (ladder-gated separately).
* Scope assignment for the 95 unattributed entities beyond the existing
  mention-derived stamping.
