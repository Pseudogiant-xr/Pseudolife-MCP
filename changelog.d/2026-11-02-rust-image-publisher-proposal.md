### Added (2026-11-02 — opt-in native release assets)
- Add version-only Rust daemon image publication and five native CLI
  archives with a compose bundle and SHA256 checksums, after the existing
  publication approval and separate maintainer-enabled repository variables.
  A build-only dispatch proves all targets without publishing. The image
  guard now uses the native provisioner's admitted model pin, and its build
  context prepares the two prompts needed by the native extractor shims
  being integrated separately.
- Check native crate versions before PyPI publication, verify the exact
  downloaded asset set before attachment, and recover partial uploads by
  verifying existing bytes and uploading only missing assets.
  Archive metadata is fixed so packaging time does not change checksums.
