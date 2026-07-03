# Deep Dream — operator runbook

Manual, full-corpus graph consolidation. Graph-only (cortex/MIRAS untouched).

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
  accept folds `from` into `into` exactly as shown (write-dedup rows are
  stored lower-degree → higher-degree at insert time).

## 2. Apply self-clean
`memory_dream(action="deep", apply=true)`. The daemon first dumps the five
graph tables to `data_dir/graph_snapshots/graph-<stamp>.json` (the `snapshot`
field in the response; newest `memory.deep_dream.snapshot_keep` files kept) and
refuses with `snapshot_failed` if the dump can't be written. A full
`pwsh ops/backup.ps1` on the host remains good practice before big passes, but
the in-daemon snapshot is now the enforced floor. Apply then re-scores +
(when `memory.deep_dream.auto_apply_safe`, default `True`) supersedes
violations and merges exact dups. Supersede-not-delete — reversible.

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

## 3b. Step C — triage near-duplicate merges (this session)
Judge each `merge_proposals` item from its per-side snippets/scopes:
- **Same referent** → `memory_graph_review(action="accept_merge",
  proposal_id=...)` — applies immediately (snapshot from step 2 is the undo),
  logged to the recent-merges audit as `decided_by=agent`.
- **Distinct** → `memory_graph_review(action="reject_entity", proposal_id=...)`
  plus `dismiss_pair` so the pair never re-proposes.
- **Unsure** → leave pending; disjoint `scopes` is a strong distinct signal.

## 4. Confirm in Atlas
Open Atlas Review → `proposed_link` findings → accept (promotes to a real edge)
or reject, per item. Nothing reaches `edges`/recall until you accept. The
"recent merge decisions" list under the queue shows what the model applied or
rejected in step 3b (decided_by=agent), newest first.
