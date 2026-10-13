### Added (2026-10-13 — Rust coordination board)

- Port the coordination board to the Rust daemon, including addressed mail,
  resource leases, wake decisions, audit chains and the read-only Console view.
  Board transactions use a dedicated client and reject aborted commits; the
  ready board calls bypass embedding initialization, and PostgreSQL startup
  has a ten-second deadline. The parity runner waits for storage readiness before taking Console snapshots.
  Pairing over HTTP remains disabled, and timing-sensitive mail replay remains
  an explicit parity limitation pending the Rust cutover.
