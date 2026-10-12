### Fixed (2026-10-11 — native backup member types and Windows names)

- Fix native backups of hardlinks, FIFOs, device nodes, raw POSIX names and
  Windows symlinks and internal junctions. Archives preserve member identity
  and raw link targets when extracted with Python. Windows backups honour CRT
  TZ settings and rotate matching Unicode names while retaining unrelated files.
