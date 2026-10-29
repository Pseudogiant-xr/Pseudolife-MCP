### Fixed (2026-10-30 — Codex setup finishes on a client whose system Python lacks the daemon's packages)
- On a fresh Linux client the installer's Codex step reported "Runtime
  defaults not confirmed", and `setup-codex-coordination.py --runtime-defaults`
  answered only "needs-configuration" with no cause: its config backup
  imported the coordination adapter, which needs `anyio`, and the installer
  runs the helper under the system `python3`. The backup now uses the
  standard-library private-file helper, so the step completes there. An
  unexpected failure in that helper now names its exception type (still no
  transport details), so the next one says what went wrong.
