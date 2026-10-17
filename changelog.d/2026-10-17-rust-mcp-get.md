### Added (2026-10-17 — the Rust daemon serves memory_get)

- The Rust daemon's MCP endpoint now answers `memory_get` as the Python daemon does: the stored memory with the facts it formed, a gentle reinforcement (its access count rises), credit to the search that served it in the same session, and `replaced_by` for a superseded memory.
