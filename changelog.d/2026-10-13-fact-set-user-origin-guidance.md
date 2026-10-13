### Changed (2026-10-13 — agents are told to mark human-stated corrections as user)

- The standing memory instructions (the daemon's session-start block and
  `examples/CLAUDE.memory.md`) now say to pass `origin="user"` to
  `memory_fact_set` when the human stated the value. Since the dream keeps
  user-stated facts at user tier, a default-origin (agent) write parks
  against such a fact as a contender instead of replacing it.
- The memory-model, dreaming and configuration guides now say that a write
  supersedes only at an equal or stronger tier, and that the consolidation
  quarantine also parks a claim whose user-stated note fails the citation
  check.
