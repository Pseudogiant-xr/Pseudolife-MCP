### Fixed (2026-11-04 — Preserve runtime loading in native doctor proofs)

- The native disposable doctor proof preserves ONNX Runtime library resolution when the daemon is discovered through a symlink or the caller supplies a Windows drive-relative path. It also recognizes libpq TLS aliases and reports the proof's TLS limitation before creating a database. Native child-process contracts now check the isolated launcher's runtime override forwarding without requiring provisioned models.
