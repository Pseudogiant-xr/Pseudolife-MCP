### Fixed (2026-10-10 — duplicate JSON keys in audit archives)
- Native `board-audit verify --input` rejects duplicate object keys that match serde's internal number key, including escaped spellings, with the same diagnostic as Python. A single occurrence remains ordinary payload data.
