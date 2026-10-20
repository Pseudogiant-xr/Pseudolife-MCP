### Changed (2026-10-20 — the CLI parity harness can widen the hooks' 250 ms daemon check)
- The hooks' `/health` check and the shim's loopback startup probe read
  `PSEUDOLIFE_HOOK_PROBE_MS` in both the Python and the Rust CLI: a positive
  whole number of milliseconds, clamped to 60000. Unset or invalid keeps
  250 ms, so hooks still never slow the host. This is a test-facing knob,
  not a user setting: the CLI parity harness sets 5000 ms in both arms, so
  a cold hosted runner stalling one arm's first connection no longer makes
  a hook silent in one arm only. The three per-read trickle cases keep
  250 ms.
