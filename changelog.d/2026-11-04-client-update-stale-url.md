### Fixed (2026-11-04 — Keep client updates with the daemon's release)

- Attended client updates refuse a silent or unversioned daemon endpoint,
  naming `--daemon-url` to correct it. They no longer install the newest PyPI
  release when a stale URL hides the daemon's version. The native updater uses
  the same rule; reachable daemons and explicit tags on checkout builds retain
  their existing behavior.
