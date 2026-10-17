### Added (2026-10-18 — the Rust daemon serves the episode tools)

- The Rust daemon's MCP endpoint now answers `memory_episode_start`, `memory_episode_end`, `memory_session_title` and `memory_episode_summary` as the Python daemon does, with the same database changes. A start nests a sub-episode under the caller's session (or under the handle's own root), an end pops one level and never closes the session root, and a title renames the session's root, the episode stamp on its memories, and the board rows registered under the session that no agent or harness named. The summary reports an episode's entry count, tag and source counts and its twenty newest memories.
- `rust/PARITY.md`'s MCP registration checklist now marks the sixteen tools the Rust daemon serves as ported.
