### Changed (2026-11-17 — Daemon release notices without PyPI)

- The daemon now contacts `api.github.com` instead of `pypi.org` for release
  checks, accepting stable releases only after all native release assets are
  uploaded. Checks remain unauthenticated and in the background; failures keep
  the last good answer and rate-limit responses delay the next attempt.
- The settings that turn the check off are unchanged:
  `updates.check_releases: false` or `PSEUDOLIFE_RELEASE_CHECK=0`.
