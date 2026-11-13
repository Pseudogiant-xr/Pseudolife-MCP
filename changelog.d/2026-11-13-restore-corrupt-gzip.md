### Fixed (2026-11-13 — restore and backup refuse a corrupt gzip stream instead of crashing)

- `pseudolife-mcp restore` given a dump or state archive whose deflate data
  is corrupt now refuses it as unreadable (exit 2, nothing restored) instead
  of printing a `zlib.error` traceback. `update` and `migrate-flat`, which
  share the dump check, now read such a dump as incomplete too.
- A safety or update backup whose dump or state archive comes out of Docker
  corrupt now fails with a clean error naming the retry, after the daemon
  is restarted, instead of a traceback; the state backup's error now says
  it is the copy out of Docker that failed rather than blaming an input.
- The native CLI now refuses a corrupt deflate stream and a gzip CRC or
  length mismatch in either file, where it deferred to Python before.
