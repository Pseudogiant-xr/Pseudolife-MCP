### Fixed (2026-11-18 — native maintainer commands finish or explain their refusal)
- Native `maintainer` commands now report malformed arguments, unsupported database settings and internal failures directly without exposing connection values or credentials.
- `maintainer list` writes long reports, reports read and output failures, and prints UTC timestamps with a trailing Z instead of depending on local time-zone agreement.
