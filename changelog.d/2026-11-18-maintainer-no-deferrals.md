### Fixed (2026-11-18 — native maintainer commands finish or explain their refusal)
- Native `maintainer` commands now report malformed arguments, unsupported database settings and internal failures directly without exposing connection values or credentials.
- `maintainer list` writes long reports, reports read and output failures, and replaces local time with UTC timestamps in `YYYY-MM-DDTHH:MM:SSZ` form at whole seconds.
- Waiting `maintainer enrol-code` rejects invalid polling intervals before opening the bank or storing a code. Ordinary `application_name` and `connect_timeout` database options are admitted natively.
