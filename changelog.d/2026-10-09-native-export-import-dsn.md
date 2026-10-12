### Added (2026-10-09 — native export and import for an explicit DSN)

- The experimental native `pseudolife-mcp export` and `import` work against a
  bank named by `PSEUDOLIFE_MCP_DATABASE_URL`. Export writes the same JSONL
  members and manifest as the Python command from one read-only snapshot;
  import fills a fresh bank that is already at this build's schema with the
  same refusals, row loading, backfill and sequence advance. The lite tier's
  embedded bank, banks that would need schema work, and other shapes defer
  by name before doing anything.
