### Fixed (2026-10-26 — test-login says plainly when a server has no production database)
- `pseudolife-mcp test-login create` ended a successful run on a server with
  no production database with "...which cannot connect to no production
  database (none on this server)", a double negative. It now says "...logs in
  as pseudolife_test; this server has no production database for it to be
  kept out of." When production databases exist, the line still names them.
  The Rust shim's port prints the same line.
