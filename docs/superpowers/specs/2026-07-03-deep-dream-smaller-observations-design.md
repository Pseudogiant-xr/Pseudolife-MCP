# Deep-dream smaller observations — design

**Date:** 2026-07-03
**Status:** approved (user, this session)
**Context:** Follow-up to the 2026-07-02 deep-dream review. Fixes 1+2 (snippet
fallback, dismissed-pair filter) shipped in `2d4684f`. This spec covers the five
remaining observations, per-item approach chosen with the user.

## 1. Dry-run `already_proposed` annotation

Dry-run previews (`would_junk`, `would_merge_propose`) count items the apply
path will silently dedupe against existing `entity_proposals` rows (the unique
indexes cover any status, so previously-rejected proposals are sticky). The
preview and the commit counters diverge (observed live: `would_junk: 2` →
`junk_proposed: 1`).

**Change:** new storage method `entity_proposal_keys()` returning keys for ALL
`entity_proposals` rows, shaped like the unique indexes:
`("junk", entity_id)` and `("merge", least_id, greatest_id)`. The dry-run
branch of `deep_dream` annotates each `would_junk` / `would_merge_propose`
item with `already_proposed: true|false`.

## 2. In-daemon graph snapshot before apply

"BACK UP FIRST" is a runbook convention the daemon cannot enforce (it can't
see the host backups dir or run `ops/backup.ps1`).

**Change:** before any `apply=true` write, the service dumps the five graph
tables (`entities`, `edges`, `entity_aliases`, `edge_proposals`,
`entity_proposals`) as JSON to `<data_dir>/graph_snapshots/graph-<UTC
timestamp>.json`, keeping the newest `deep_dream.snapshot_keep` (default 10)
and pruning older ones. If the snapshot cannot be written, apply returns
`{error: "snapshot_failed"}` and mutates nothing. The apply response gains
`snapshot: <filename>`. Restore is manual; pg_dump backups remain the real
recovery path — this is a targeted undo artifact for exactly the tables
deep-dream mutates.

## 3. Jaccard support-overlap filter

`candidate_pairs` drops pairs whose supporting-entry sets are *strictly
identical* (pure co-occurrence). Near-identical support (the accept/reject
verb cluster at sim ≈0.98) passes and floods the candidate window with
co-occurrence noise.

**Change:** replace the equality test with Jaccard overlap:
drop when `|mu ∩ mv| / |mu ∪ mv| >= deep_dream.max_support_overlap`
(default 0.8, config-exposed). Equality is the overlap-1.0 special case, so
the filter only widens.

## 4. Snippet truncation + opt-out

With fix 1 live, the default deep response is ~483KB (50 candidates × 6
full-length snippets), exceeding MCP tool output limits.

**Change:** `_attach_candidate_snippets` truncates each snippet to
`deep_dream.snippet_max_chars` (default 240). The `memory_dream` tool gains
`snippets: bool = True`; `snippets=false` skips attachment entirely
(candidates carry no snippet keys). Service signature:
`deep_dream(apply=..., include_snippets=True)`.

## 5. Step-C driver (agent-side)

The pipeline ends at "here are candidates"; nothing turns them into reviewed
proposals, and an agent working the candidates cannot record "these are
distinct" — the verdict that (since `2d4684f`) frees top-k slots.

**Changes:**
- New MCP verb: `memory_graph_review(action="dismiss_pair", src=..., dst=...)`
  wiring the existing `graph_dismiss_duplicate` service method (`src`/`dst`
  are entity names; normalized + order-insensitive in storage).
- Extend `examples/commands/dream.md` with the deep flow: dry-run →
  `apply=true` (daemon self-snapshots) → per candidate, judge from snippets:
  propose a typed link with rationale via `action="propose"`; `dismiss_pair`
  clearly-distinct noise; leave uncertain pairs for the Atlas queue.

## Config additions (`DeepDreamConfig`)

| key | default | item |
|---|---|---|
| `max_support_overlap` | 0.8 | 3 |
| `snippet_max_chars` | 240 | 4 |
| `snapshot_keep` | 10 | 2 |

## Testing

TDD throughout. Pure tests in `tests/test_graph_consolidation.py` (Jaccard);
service/PG tests in `tests/test_deep_dream.py` (annotation, snapshot write /
refusal / retention, truncation, opt-out); tool-level test for
`dismiss_pair`. Docs ride along: `memory_dream` / `memory_graph_review`
docstrings, README tool section, CHANGELOG.

## Out of scope

Snapshot restore tooling; daemon-side (extractor) candidate judging; paging
API for candidates; changes to the surprise gate or dream extractor.
