### Fixed (2026-11-17 — Autostart registration hardening)
- Native Windows autostart registration uses the operating system's Windows PowerShell path, verifies the script and registration data before using them, protects registration scratch files, and builds its path-resolution interop helper in memory to keep registration inputs consistent.
