### Added (2026-11-02 — Opt-in native host updates)

- Add native release and checkout update transactions, explicit Python or Rust engine selection and read-only shadow lifecycle management without a host Python dependency. Preserve update exclusion, verified backups, rollback protection, authenticated health gates and client failure status.
- Release updates consume checksum-verified Compose assets and retain the selected engine. Checkout wrappers select the native CLI only with `--native`, `-Native` or `PSEUDOLIFE_NATIVE_UPDATE=1`; the existing Python updater remains available.
