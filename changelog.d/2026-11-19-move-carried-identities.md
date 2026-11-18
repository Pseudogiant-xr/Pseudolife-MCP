### Added (2026-11-19 — Check bearer settings before a native bank move)
- Native `move --dry-run` checks whether the source's bearer settings can be carried into an installed Docker target. The check sends only which settings are present to the target. Running the move still requires `--no-keep-tokens` until the native handover is available.
