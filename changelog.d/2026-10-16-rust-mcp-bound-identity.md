### Added (2026-10-16 — Native MCP bound-identity check)

- The experimental Rust daemon checks an MCP request's `X-PL-Bank` and `X-PL-Principal` headers against the caller's coordination board context before the MCP layer, as the Python daemon does, and answers the same coordination refusal on a mismatch. Bank tool bodies can now be registered behind it.
