### Added (2026-10-16 — Native MCP tool argument binding)

- The experimental Rust daemon binds every MCP tool's arguments as the Python daemon does before any tool body runs: unknown names are refused with the same hint and accepted list, text stays literal, JSON-encoded lists and objects are decoded, and values are checked and coerced with pydantic's lax rules and its exact refusal sentences. The per-tool table is generated from each tool's pydantic schema, so a changed signature fails CI until the table is regenerated.
- A new in-process differential sweep compares the two binders on 36,880 generated argument sets per run, with no database or model, and requires 14 deliberate binder breaks to be caught. A tool body that panics now answers `internal_error`, as a Python exception does.
