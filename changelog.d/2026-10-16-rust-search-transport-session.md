### Fixed (2026-10-16 — Rust memory_search keeps the legacy transport-session attribution)
- The Rust daemon's `memory_search` now attributes its retrieval event to the MCP transport session when `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` is set and there is no `X-PL-Session` header or live active-session pointer, as Python does. Before, the event's session and episode were left empty in that case.
