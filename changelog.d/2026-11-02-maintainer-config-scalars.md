### Fixed (2026-11-02 — Maintainer configuration scalar validation)

- Native maintainer setup retains non-string validation for YAML timestamps and binary values, accepts unbounded non-negative integer limits in read-only checks, and escapes YAML-forbidden Unicode noncharacters when writing configuration.
