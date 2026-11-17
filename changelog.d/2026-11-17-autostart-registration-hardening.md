### Fixed (2026-11-17 — Autostart registration hardening)
- Native Windows autostart registration runs Windows PowerShell by its full path, verifies the script and registration data before using them, and resolves paths entirely in memory to keep registration inputs consistent.
