### Added (2026-11-02 — Opt-in native host updates)

- Add native release and checkout update transactions, explicit Python or Rust engine selection and read-only shadow lifecycle management without a host Python dependency. Preserve update exclusion, verified backups, rollback protection, authenticated health gates and client failure status.
- Release updates consume checksum-verified Compose assets and retain the selected engine. Checkout updates resolve backup and rollback containers from their own Compose project, and preserve Windows checkout paths when invoking PowerShell scripts.
- Printed release rollback commands carry the image tag needed by Compose interpolation, and Windows unattended tasks retain long layout overrides through the Task Scheduler action API.
