### Added (2026-10-29 — the Rust daemon serves the full memory briefing)
- On the Rust daemon `GET /api/briefing`, which `pseudolife-mcp briefing`
  and the shims read, answered 501. It now answers as the Python daemon
  does: the graph's open questions and surprising links, avoid-first
  lessons, fresh world facts, where the last session left off, the
  dream-stall and review-queue lines, and the other open sessions as the
  caller may see them.
