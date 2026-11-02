### Fixed (2026-11-02 — Keep HTTP pairing independent of TLS trust overrides)
- Plain HTTP pairing and invite health checks continue to work when an overridden TLS certificate store is empty or missing; HTTPS retains certificate verification.
