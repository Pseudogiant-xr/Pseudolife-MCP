### Changed (2026-10-13 — agents are told to mark human-stated corrections as user)

- The standing memory instructions now say to pass `origin="user"` to
  `memory_fact_set` when the human stated the value: the compact
  session-start core every session gets, the full memory-loop block
  (`full_separate_hook`) and `examples/CLAUDE.memory.md`. Since the dream
  keeps user-stated facts at user tier, a default (agent) write is parked as
  a contender, not applied, when the slot's value is user- or action-backed.
- The memory-model, dreaming and configuration guides now say that a write
  supersedes only at an equal or stronger tier, and that the consolidation
  quarantine also parks a claim whose user-stated note fails the citation
  check.
