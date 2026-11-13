### Fixed (2026-11-13 — restore and backup refuse unusable `docker inspect` output cleanly)

- `pseudolife-mcp restore` and `backup` now answer `docker inspect` output
  that is a JSON object, or nested too deeply to parse, with the usual
  `cannot inspect <container>; restore its Docker container configuration
  and retry` (exit 2) instead of a `KeyError` or `RecursionError`
  traceback. The native restore now refuses the JSON object too, where it
  deferred to Python before.
