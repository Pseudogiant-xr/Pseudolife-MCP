### Fixed (2026-10-16 — Rust search keeps the legacy transport-session attribution)

- The Rust daemon's `memory_search` and `GET /api/search` now attribute their retrieval events to the MCP transport session when `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` is set and there is no `X-PL-Session` header or live active-session pointer, as Python does (an empty `mcp-session-id` is kept as given). Before, the event's session and episode were left empty in that case.
