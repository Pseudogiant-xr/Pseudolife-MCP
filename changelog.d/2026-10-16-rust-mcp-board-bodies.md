### Added (2026-10-16 — Native memory_agents and memory_message over MCP)

- The experimental Rust daemon serves the coordination board's two MCP tools: `memory_message` sends, receives, acknowledges and lists mail, and `memory_agents` lists peers (or, without an agent identity, the open sessions it can see), updates status and parks, and claims or releases leases and repository file claims, with the Python daemon's parameter checks and refusals.
- The shim and the daemon now compile one shared implementation of repository file-claim names.
