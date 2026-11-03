### Fixed (2026-11-03 — Make mail parity recordings reproducible)

- The board mail compatibility harness now records explicit inactive and reactivated recipients for sends and replies, with a portable golden and exact Python activity-boundary cases. This closes the recording-pacing discrepancy without changing production wake decisions.
