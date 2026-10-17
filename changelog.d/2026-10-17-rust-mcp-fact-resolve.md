### Added (2026-10-17 — the Rust daemon serves memory_fact_resolve)

- The Rust daemon's MCP endpoint now answers `memory_fact_resolve` as the Python daemon does, with the same database changes: accepting a parked contender makes it the slot's current value (the old value kept as superseded history, the contender gaining the user tier and a fresh ordering stamp), and rejecting it retires the contender and keeps the current value. A slot with nothing parked answers `no_contender`; accepting at a slot that has since become a set answers `slot_holds_set`, while rejecting there is still allowed.
