### Changed (2026-10-27 — a Rust board send retries the way the Python daemon does after a writer change)
- When a board message send on the Rust daemon finds that the bank's writer
  epoch moved under it, its one retry now rechecks the database session at
  once (even inside the one-second probe interval) and runs the service's
  initialization before trying again, as the Python daemon's retry does.
  The send still goes through on its retry, or is refused with nothing
  written.
