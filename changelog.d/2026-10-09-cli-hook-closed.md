### Fixed (2026-10-09 — native briefing, prompt-hook and doorbell receipt closed)

- The native `briefing`, `prompt-hook` and `doorbell-prompt-seen` match the
  Python commands on all canonical cases (65 on Windows and on Linux) on Windows and Linux, checked by
  the CLI differential harness against a fixture daemon, including each
  request sent on the wire. Fixed on the way: `briefing --help` printed
  doubled carriage returns from a CRLF checkout, the Windows default (the help
  asset is now pinned to LF like its siblings). The hook requests now time
  each receive rather than the whole reply, try each resolved address in
  turn and follow urllib's redirect limits, over http and https: a daemon
  answering slowly but steadily, or a `localhost` URL on Windows, no longer
  leaves the native hooks silent where Python answered. CLI-HOOK is
  `ported-with-substitution`; episode start/end move to a separate, still
  deferred CLI-EPISODE row.
