| Item | Python | Rust | Owner |
|---|---|---|---|
| MCP-LIVE-SESSIONS | Python on mcp 2.2.0 retains a live transport for every session its opening request established, until DELETE (the daemon passes `session_idle_timeout=None, max_sessions=None`, overriding 2.2.0's 30-minute idle close and 10,000-session cap) | the live session map is likewise unbounded, and keeps no history of ended sessions | resource-lifetime parity; future bounded admission needs an explicit policy |
