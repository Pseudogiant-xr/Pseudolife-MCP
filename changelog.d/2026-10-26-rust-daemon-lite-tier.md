### Added (2026-10-26 — the Rust daemon runs the lite tier's embedded PostgreSQL)

- With no `PSEUDOLIFE_MCP_DATABASE_URL`, the Rust daemon now starts (or
  attaches to) the lite bank's embedded PostgreSQL in the per-user data dir,
  as the Python daemon does, instead of refusing to start. A bank either
  daemon created opens on the other.
- The first start without an installed runtime downloads the pinned
  PostgreSQL 18.1.0 and pgvector 0.8.5 build (about 55 MB, sha256-checked)
  into `native/postgresql` under the per-user data dir (also when
  `PSEUDOLIFE_MCP_DATA_DIR` moves the bank); `pseudolife-daemon provision
  postgresql` installs it ahead of time.
- `PSEUDOLIFE_MCP_STORAGE=files` is refused, naming what replaces it: file
  mode retires with the Rust daemon. A data dir whose `.pt` state was never
  imported is refused too, naming the one-time import (start the last
  Python release once).
- Before every connection to the lite server, the daemon checks, with SCRAM
  required, that the server on the bank's port is the bank's own.
