### Added (2026-10-18 — the Rust daemon serves lesson search and the history chain)

- The Rust daemon now keeps the lesson store resident, and its MCP endpoint answers `memory_lesson_search` and `memory_history` without an `attribute` (the entity's event chain) as the Python daemon does. Lesson search flags lessons whose subject's facts changed since (`re_verify`), including through a graph alias, and logs each search. The chain merges the entity's fact history, source entries, graph edges and lessons into one timeline.
