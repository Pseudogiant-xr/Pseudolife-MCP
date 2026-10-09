### Security (2026-10-10 — Rust principal store preserves identity refusals)
- Added durable principal service operations and source-aware resolution to the Rust daemon, with immediate redemption visibility, refresh race protection, expiry, revocation and fail-closed unavailable snapshots.
- Added differential principal-store coverage against the Python oracle, including database state after writes and source mutation controls; HTTP gates and tier mapping remain separate port slices.
