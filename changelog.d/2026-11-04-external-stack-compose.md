### Added (2026-11-04 — Supported compose for external stack managers)

- Provide a self-contained GHCR compose file for Portainer, Dockge, Komodo and plain Compose folders, with the installer's service identities, environment settings, external volumes and daemon hardening. Automatic extraction is unconfigured by default; the unpublished CPU sidecar is a separate opt-in build.
- Document first credentials, client-only setup, release updates, manager redeploys and paired backups; pin every runtime option to the source-build stack with a drift guard.
