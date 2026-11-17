### Fixed (2026-11-17 — Complete native audit operations)
- A failed audit export to a file no longer leaves an empty or partial archive behind.
- Published audit archives, including container-forwarded exports, are now owner-only (0600 on Unix, subject to the caller's umask, and an owner-only ACL on Windows); group and service-account read access is no longer inherited.
- Native `board-audit` reports unsupported inputs and operational failures with their cause and next step instead of deferring to the retired Python client; supported database connection options work natively.
