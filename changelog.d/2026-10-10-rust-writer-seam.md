### Added (2026-10-10 — Shared Rust writer statement ownership)
- Add a shared transaction and statement adapter for the Rust daemon port.
  Recover cancellation while BEGIN is pending before a later statement can
  inherit the abandoned transaction and return without committing.
