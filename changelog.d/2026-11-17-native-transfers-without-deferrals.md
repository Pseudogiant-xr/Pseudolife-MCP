### Fixed (2026-11-17 — Native transfers without deferrals)
- Native `export` and `import` now handle supported inputs directly and
  refuse unsupported archives, database settings and values with the cause
  and a repair step. Help works at any terminal width; repeated options,
  `--option=value` and ordinary filesystem path spellings are accepted.
- Export normalizes timestamp rendering to UTC and supports PostgreSQL's
  infinite timestamps and years outside 1–9999.
- Import validates archive structure before connecting and commits schema
  initialization and imported data together. A refused import rolls back
  schema repairs too. With `--force`, connected clients now wait, including
  readers, until the import finishes; previously only writes waited during
  the data import. Stop the daemon before importing unless those connections
  are known to be inert.
