### Fixed (2026-11-04 — Run oracle-dependent stress checks before merge)

- Branch stress dispatches now select the Python oracle from their merge base with master, using the existing pull-request rule for release-only changes. The stress plan freezes the remote master once for all iterations; receipts retain the resolved commit IDs, candidate source-pin checks remain required, and ordinary CI dispatch stays restricted to a frozen master head.
- Case-distinct branch names such as `Master` also freeze the oracle base; only the exact `refs/heads/master` ref uses the master dispatch path.
