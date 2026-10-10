### Fixed (2026-10-10 — preserve Console static responses in the Rust daemon)

- Resolve existing link ancestors before checking static-path containment, including requests for missing files beneath escaping links, and select content types from resolved targets.
- Match the shipped vendor notice's platform content type and verify every committed Console asset, fallback, redirect and security header through the daemon differential harness on Linux and Windows.
- Preserve missing-file fallbacks for ignored OS filename errors, read optional WebP mappings from the platform database, and refuse noncanonical Windows space-suffixed parent segments.
