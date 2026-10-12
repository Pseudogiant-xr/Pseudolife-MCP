### Added (2026-10-09 — native test-login create)

- The experimental native CLI answers `pseudolife-mcp test-login create`
  for its canonical options, on both the `--admin-url` path (the crate's
  PostgreSQL client) and the container path (`docker exec ... psql`), with
  the Python command's exit codes, report lines, `--json` report, refusals,
  SCRAM-only password handling and owner-only login file. Other spellings,
  and inputs whose answer it cannot reproduce byte for byte (a login file it
  could not write, an admin URL or daemon DSN outside the shared client's
  grammar, libpq environment controls that client refuses), defer before
  any connection or file change.
- The CLI differential harness row `test_login` proves it against the
  Python oracle on a throwaway PostgreSQL cluster or a disposable container
  per arm, never the bench server, the stack's own container or the real
  login file, comparing the server's roles, memberships, database ACLs and
  template1 extensions and validating the written login against that
  server.
