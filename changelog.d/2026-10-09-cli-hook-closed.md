### Fixed (2026-10-09 — native briefing, prompt-hook and doorbell receipt closed)

- The native `briefing`, `prompt-hook` and `doorbell-prompt-seen` match the
  Python commands on all 52 canonical cases on Windows and Linux, checked by
  the CLI differential harness against a fixture daemon, including each
  request sent on the wire. The harness found that `briefing --help` printed
  doubled carriage returns from a CRLF checkout, the Windows default; the help
  asset is now pinned to LF like its siblings. CLI-HOOK is
  `ported-with-substitution`; episode start/end move to a separate, still
  deferred CLI-EPISODE row.
