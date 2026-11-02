### Fixed (2026-11-02 — Native update interoperability and recovery)

- Native updates preserve Python updater compatibility, accept the published Compose bundle, retain the original project directory and mounts, and refuse unpublished Rust release images.
- Plain updates keep the existing health policy; engine activation and shadow mode retain authenticated version and schema checks.
- Checkout updates verify Compose project identity without requiring a loopback health URL, recognize Docker Desktop bind paths, and require complete state backups when the discovered engine is Rust. Releases missing native assets name the Python updater as the remedy.
- Client refresh preserves absent settings, reports migration failures, respects SSH configuration and requires a verified stable launcher for unattended work.
