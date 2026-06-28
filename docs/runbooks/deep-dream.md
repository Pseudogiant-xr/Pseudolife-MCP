# Deep Dream — operator runbook

Manual, full-corpus graph consolidation. Graph-only (cortex/MIRAS untouched).

## 1. Preview (no writes)
Call `memory_deep_dream(apply=false)`. Review:
- `rescored` — agent edges whose confidence will change.
- `would_supersede` — hard type-violation edges to be auto-superseded.
- `would_merge` — exact-duplicate entity pairs to be merged.
- `candidates` — semantic cross-session link candidates (src/dst + context snippets).

## 2. Apply self-clean (backup first)
On the Windows host: `pwsh ops/backup.ps1`. Then `memory_deep_dream(apply=true)`.
This re-scores + (when `memory.deep_dream.auto_apply_safe`) supersedes violations
and merges exact dups. Supersede-not-delete — reversible.

## 3. Step C — propose links (this session, subagents)
For each `candidate`, dispatch an Opus subagent with the two entity displays + their
`src_snippets`/`dst_snippets` + the relation registry (reuse the
`evals/relation_extraction_bench.py --emit-prompts` prompt shape). The subagent
returns one closed-vocab relation or "reject". Collect survivors as
`[{src, relation, dst, similarity, rationale}]` and call
`memory_graph_propose_links(proposals)`. The gate (edge_confidence +
is_hard_type_violation) drops junk automatically.

## 4. Confirm in Atlas
Open Atlas Review → `proposed_link` findings → accept (promotes to a real edge)
or reject, per item. Nothing reaches `edges`/recall until you accept.
