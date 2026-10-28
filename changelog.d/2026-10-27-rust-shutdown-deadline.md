### Fixed (2026-10-27 — the Rust daemon stops within Docker's grace period and saves before it is killed)
- The Rust daemon waited without limit at exit: for open requests (a
  client parked on a board wait), for writes, for background work and for
  its final save. A write stuck on an unresponsive database held the
  daemon until Docker killed it, and the final save never ran. Shutdown
  now works within 9 seconds of the stop signal, inside Docker's default
  10-second grace: open requests are waited for up to 3 seconds, writes
  until 6, background work until 7.5, and the final save until 9. Work
  still running at the end is left to the exit rather than cancelled
  part way, and a write a lingering request submits after the first
  3 seconds is refused rather than started.
