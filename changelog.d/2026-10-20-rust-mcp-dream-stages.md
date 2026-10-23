### Added (2026-10-20 — the Rust daemon's dream runs outcome inference, lesson synthesis and digests)

- `memory_dream(action="run")` on the Rust daemon now runs the stages after consolidation as the Python daemon does: it infers outcome signals for closed sessions that recorded none, turns pending outcome signals into lessons (clustered, or one rule per signal in rule mode, with lineage audits and the 0.4 confidence of machine-inferred batches), and writes a narrative digest per closed session when digests are enabled. Only the graph-insight refresh still refuses the run while it is on.
- `memory_graph_review(action="restore_slot")` now restores lessons whose retired rows compaction purged from their audit snapshots, as world facts already were, instead of refusing.
