### Fixed (2026-11-02 — Native update interoperability and recovery)

- Native updates preserve Python updater compatibility, accept the published Compose bundle, retain the original project directory and mounts, and refuse unpublished Rust release images.
- Plain updates keep the existing health policy; engine activation and shadow mode retain authenticated version and schema checks.
- Client refresh preserves absent settings, reports migration failures, respects SSH configuration and requires a verified stable launcher for unattended work.
