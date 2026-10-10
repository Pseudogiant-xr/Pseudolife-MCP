### Fixed (2026-10-10 — Concurrent differential harness databases)

- CLI and daemon differential harness runs allocate unique disposable database
  names and refuse cleanup of names allocated by another run, so concurrent
  rows on one test PostgreSQL server cannot drop each other's banks.
- Forked children refuse inherited database names and cached fixtures before
  they can return a parent database URL or run a cached case.
- Shared-template creation briefly retries while another bootstrap connection
  is using the extension template, without ending that connection.
