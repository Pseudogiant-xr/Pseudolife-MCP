## Graph traversal: deferred 2026-10-12, reversed 2026-10-19

**Reversed.** Maintainer decision, 2026-10-19: graph and dream are to be
ported after all. Both surfaces below return to scope, planned as the graph and
dream batch: graph store writes (task M), graph reads with recall's graph
expansion (task N), graph review (task O), and the dream with the deep-dream
edge pass and graph-insight refresh (task P). Recall's graph expansion is
served over MCP since task N2 ("memory_recall over MCP" in
`rust/daemon/spec.md`; `GET /api/recall` is not yet served). The deep-dream
edge pass is served with the deep dream (edge re-scoring, hard-violation
cleanup and the Step C link proposals it files: "memory_dream deep" in
`rust/daemon/spec.md`), and the graph-insight refresh with every dream run
under Option B (GI1-GI6 there, a structurally compared declared divergence).
The decision of 2026-10-12 and its evidence follow as history.

**History.** Maintainer decision, 2026-10-12 (reaffirmed 2026-10-16): two
graph-traversal surfaces are not ported. The evidence is the completed graph-assisted versus
plain recall eval (`graph-recall-prep-20260930`, judged 2026-09-29/30, turn 2).
Its preregistered primary verdict was inconclusive; on the needed-evidence
proxy over failure queries, plain search beat graph recall under both judges.
The 2026-09-04 ablation
(`graph-ablation-20260904.json`) adds the cost: graph recall was no more
complete than plain search and far more costly in text served and latency.

| Deferred by decision (reversed 2026-10-19) | Python source |
|---|---|

Still in scope, because the cortex depends on them: entity and alias
storage, entity naming, and the entity merge, duplicate and junk proposals
with their fold direction and review decisions (including those
`deep_dream` files). The Python daemon keeps serving the deferred surfaces
until cutover. Retiring them is a separate `retired-by-decision` entry. A
further labelled eval, with a rubric that can tell complete needed-evidence
coverage, was to be a separate future check that could reopen the decision;
the 2026-10-19 reversal returned both surfaces to scope without it.
