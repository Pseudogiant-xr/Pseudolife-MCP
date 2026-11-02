### Changed (2026-11-02 — Share PostgreSQL certificate verification)

- Extracted the shim's PostgreSQL certificate verifier and TLS client builder for reuse by the native daemon, preserving the shim's connection and certificate policies.
