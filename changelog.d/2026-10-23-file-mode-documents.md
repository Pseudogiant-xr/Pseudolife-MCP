### Deprecated (2026-10-23 — file mode no longer stores documents)

- File mode (the v0.1 escape hatch with no Postgres: no DSN and no lite tier)
  has no document store from schema v57 on, because documents now live in
  the Postgres bank: `document_ingest` refuses, naming
  `PSEUDOLIFE_MCP_DATABASE_URL` and the lite tier, and `document_search`
  returns nothing. A file-mode `chromadb/` directory is left on disk and is
  imported into the bank the first time that data dir runs with Postgres.
  File mode itself retires with the move to the Rust daemon, which keeps a
  one-time import of its `.pt` files.
