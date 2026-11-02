### Added (2026-11-02 — Optional Rust daemon image publisher)
- Add a release-workflow job for the separate `pseudolife-daemon-rust` image, downstream of the existing publication approval. It stays disabled unless the maintainer enables `PSEUDOLIFE_PUBLISH_RUST_DAEMON`; Python image publication and engine defaults are unchanged.
