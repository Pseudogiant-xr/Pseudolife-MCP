### Added (2026-10-09 — the Rust daemon serves the MCP mount, tool catalogue and tiers)

- The contract-first Rust daemon (`rust/daemon/`, not installed by anything
  yet) now serves `/mcp`: the streamable-HTTP handshake the shims use
  (initialize, sessions, SSE responses, DELETE), the transport-security rules
  (Content-Type, and the loopback Host/Origin allowlist on tokenless
  installs), and the tool catalogue byte-for-byte as the Python daemon lists
  it at each tier (minimal 10, core 24, full 38).
- Tier resolution matches the Python daemon: per-principal `memory_toolset`
  overrides (12 h), `PSEUDOLIFE_MCP_TIER_MAP`, stored principals' tiers and
  `PSEUDOLIFE_MCP_TOOLSET`, with `tools/list_changed` pushed to the session's
  open stream. `memory_toolset` is served in full; every other tool answers a
  declared `not_implemented` refusal until its body lands
  (`rust/daemon/divergences.md`).
- `rust/daemon/src/mcp/dispatch.rs` is the interface the read, write and
  board slices plug their tool bodies into. `tests/test_rust_mcp_catalogue.py`
  fails when a tool's description, schema, annotations or tier changes in
  `mcp_server.py` without re-recording the Rust catalogue.
