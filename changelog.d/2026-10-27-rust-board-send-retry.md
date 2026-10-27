### Fixed (2026-10-27 — a Rust board send retries the way the Python daemon does after a writer change)
- When a board message send on the Rust daemon finds that the bank's writer
  changed under it, its one retry now rechecks the database session at once
  and re-reads the bank first, as the Python daemon does, so the send either
  goes through once or is refused with nothing written.
