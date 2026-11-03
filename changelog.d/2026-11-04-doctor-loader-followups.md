### Fixed (2026-11-04 — Preserve runtime loading in native doctor proofs)

- The native disposable doctor proof preserves ONNX Runtime library resolution when Linux discovers the daemon through a symlink, and resolves Windows drive-relative library paths before isolating the child. It also recognizes libpq TLS aliases and reports the proof's TLS limitation before creating a database. Native child-process contracts now check the isolated launcher's runtime override forwarding without requiring provisioned models.
