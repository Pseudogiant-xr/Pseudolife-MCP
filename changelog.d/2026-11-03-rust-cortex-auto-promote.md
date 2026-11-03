### Fixed (2026-11-03 — Native store cortex auto-promotion)

- Enabling `memory.cortex.auto_promote` now promotes deterministic slot-shaped
  facts when storing memories, including below-surprise restatements, and
  reports the actual promotion count.
- Promoted facts preserve Python's confidence, provenance, labels, writer and
  session context, supersession, contenders and freshness behavior; they are
  immediately readable and survive a daemon restart.
- Native promotion also creates subject graph nodes and proposes write-time
  deduplication, while scalar claims for set-valued slots are skipped.
