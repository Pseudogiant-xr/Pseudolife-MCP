### Fixed (2026-10-13 — preserve drained MCP refusal connections)

- The experimental Rust MCP mount now uses its 4 MiB budget when discarding
  refused request bodies, and keeps a fully drained in-budget connection
  reusable. Incomplete, over-budget and Expect refusals retain bounded
  cleanup and close the connection.
- The MCP divergence ledger names the missing service prerequisite for each
  unserved tool, the retained tokenless 501 boundary and the pending board
  identity check. No bank tool is enabled by this change.
- Initialization now validates known nested client capabilities while allowing
  unknown capability fields, matching the pinned SDK.
- Standalone SSE notifications use bounded delivery; DELETE and disconnect
  release blocked producers. Mutation controls now require differences beyond
  an explicit ordinary baseline report.
- JSON-RPC preserves arbitrary-width integer IDs and real object keys that
  resemble serde's internal number marker. Fixture runs use separate homes,
  resolve binary paths before launch and fail on execution or cleanup errors.
- Toolset transitions are atomic. SDK-derived request and notification schemas
  validate metadata, task parameters, client information and nested fields
  before readiness or tier changes. A generated live sweep checks response
  codes and state before and after initialization.
