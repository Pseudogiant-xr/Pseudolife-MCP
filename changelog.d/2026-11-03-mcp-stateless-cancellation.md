### Fixed (2026-11-03 — Native MCP transport compatibility)

- The native daemon now serves the stateless MCP protocol, including direct HTTP discovery and subscriptions, while preserving handshake clients. Request cancellation follows the Python daemon: interrupted async handlers stop, and shielded writes finish without delivering a cancelled request's ordinary result.
