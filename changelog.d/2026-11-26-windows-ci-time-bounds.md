### Fixed (2026-11-26 — Windows CI jobs no longer run within minutes of their time limits)
- Raise four hosted Windows time bounds to about 40% over their slowest runs of 12-26 November: the Rust job (75 to 101 minutes, 100 to 127 on an ordinary pull request), `test-lite-windows` (40 to 51) and the cli parity shard (35 to 42), each of which had a run at 84-96% of its old bound.
