### Fixed (2026-10-10 — Concurrent differential harness databases)

- CLI and daemon differential harness runs allocate unique disposable database
  names and refuse cleanup of names allocated by another run, so concurrent
  rows on one test PostgreSQL server cannot drop each other's banks.
- Shared-template creation briefly retries while another bootstrap connection
  is using the extension template, without ending that connection.
