### Fixed (2026-11-02 — Clarify native compatibility notices)

- Equivalent Python and native prerelease version spellings no longer produce false plugin-version mismatch notices. Hook-digest checks still detect changed scripts for those matching versions.
- An explicit bf16 setting now explains the supported `fp32`/`auto` configuration and environment overrides, or how to retain the Python engine when bf16 is required. Native inference remains fp32.
