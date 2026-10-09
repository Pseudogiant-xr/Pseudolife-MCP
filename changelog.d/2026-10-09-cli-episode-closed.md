### Fixed (2026-10-09 — native episode-start and episode-end closed)

- The native `episode-start` and `episode-end` match the Python commands on
  every canonical case on Windows and Linux: the request body, headers and
  silence rules against a fixture daemon, and the episode and client-session
  rows two real daemons write on disposable banks. The episode leaf now uses
  the hook transport, so a `localhost` daemon URL on Windows or a slowly
  answering daemon no longer drops the episode where Python recorded it.
  CLI-EPISODE is `ported-with-substitution`; source-vote titles stay out of
  scope until their producer lands.
