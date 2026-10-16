### Added (2026-10-16 — Native memory_store over MCP)

- The experimental Rust daemon serves `memory_store` on its MCP endpoint, the first bank tool it answers there: arguments bind as Python's do, the caller's session header attributes the entry, and a store whose client disconnects still lands. The write-path differential harness now compares both daemons' MCP answers and bank state step by step.
