### Added (2026-10-09 — native backup for explicit-DSN and file mode)

- The experimental native `backup` leaf now backs up a bank named by
  `PSEUDOLIFE_MCP_DATABASE_URL` (pg_dump through gzip, found in the `~/.pg0`
  bundle or on PATH as Python finds it) and archives the data dir's state
  with rotation of its own files, matching the Python command's output,
  files and archive contents. Canonical arguments and paths only; the lite
  tier's embedded bank and other shapes still defer by name before doing
  anything.
