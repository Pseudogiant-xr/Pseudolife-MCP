### Fixed (2026-11-03 — Deferred privileged doorbells)
- Python and native Codex doorbells retain a maintainer or conductor offer only after a queue reservation is deferred, so an already-rung offer is not retained again. Handoff, successful receive, delivery, an empty mailbox, origin expiry or explicit daemon withdrawal clears retained authorization; newer permitted peer attention preserves it.
