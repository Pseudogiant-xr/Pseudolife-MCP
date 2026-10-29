### Fixed (2026-10-27 — the Rust daemon orders board mail and memory writes on one clock)
- On the Rust daemon a board message's ordering stamp came from the board's
  own clock, separate from the clock that stamps facts, world facts and
  lessons, so a message could sort before a memory write made just before
  it. Board sends and the daemon's own notices now take the service clock's
  next tick, as on the Python daemon, after the service has fully started
  (a send now waits for startup to finish, as it does on Python).
