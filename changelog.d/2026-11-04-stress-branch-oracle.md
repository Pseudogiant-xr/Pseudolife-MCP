### Fixed (2026-11-04 — Run oracle-dependent stress checks before merge)

- Branch stress dispatches now select the Python oracle from their merge base with master, using the existing pull-request rule for release-only changes. Receipts retain the resolved commit IDs, candidate source-pin checks remain required, and ordinary CI dispatch stays restricted to a frozen master head.
