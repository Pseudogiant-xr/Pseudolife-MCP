### Added (2026-11-02 — opt-in native release assets)
- Add version-only Rust daemon image publication and five native CLI
  archives with a compose bundle and SHA256 checksums, after the existing
  publication approval and separate maintainer-enabled repository variables.
  A build-only dispatch proves all targets without publishing. The image
  guard now uses the native provisioner's admitted model pin, and its build
  context includes the shared extractor prompt for the native CLI.
