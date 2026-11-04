### Fixed (2026-11-04 — Make local-only lease outcomes explicit)
- `lease run` now ends with a clear `LOCAL LOCK ONLY` result and the command's exit code when it could not acquire a board lease, including the missing-bearer fix; it still serializes locally and preserves the command's exit status.
