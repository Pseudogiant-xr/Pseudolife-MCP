### Fixed (2026-10-13 — preserve drained MCP refusal connections)

- The experimental Rust MCP mount now uses its 4 MiB budget when discarding
  refused request bodies, and keeps a fully drained in-budget connection
  reusable. Incomplete, over-budget and Expect refusals retain bounded
  cleanup and close the connection.
- The MCP divergence ledger names the missing service prerequisite for each
  unserved tool, the retained tokenless 501 boundary and the pending board
  identity check. No bank tool is enabled by this change.
