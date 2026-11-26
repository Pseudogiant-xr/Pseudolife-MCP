### Changed (2026-11-27 — the release workflow verifies mcp-publisher and always publishes the Rust image)
- The registry job checks the downloaded `mcp-publisher` tarball against a
  SHA-256 pinned beside `PUBLISHER_VERSION` before it unpacks or runs it, so a
  replaced upstream asset fails the job instead of running with the job's OIDC
  token.
- The Rust daemon image job no longer depends on the
  `PSEUDOLIFE_PUBLISH_RUST_DAEMON` repository variable. An unset variable used
  to skip the image and `RELEASE-READY.json` without failing the run, so
  native clients never saw a release that PyPI and GHCR already served.
