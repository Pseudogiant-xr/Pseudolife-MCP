### Fixed (2026-11-02 — Pairing interruption while waiting on a pipe)

- Native pairing accepts interruption while a piped pairing code is incomplete,
  restores the input descriptor flags, and leaves the token file untouched.
