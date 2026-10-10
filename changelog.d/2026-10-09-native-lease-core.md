### Added (2026-10-09 — native lease core parity)

- Verify native `lease check`, `lease list` and `lease run` against the Python
  CLI on Windows and Linux, including OS lock truth, FIFO board acquisition,
  child status and cleanup, and the declared closed-stdout diagnostic.
  The shared CLI differential harness compares every intermediate bank write
  and replays platform goldens in CI; phase 4 operator actions remain separate.
