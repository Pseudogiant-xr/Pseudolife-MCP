### Added (2026-11-02 — native CLI in the opt-in Rust image)

- The Rust daemon image now includes `pseudolife-mcp` for operator commands,
  with a network-disabled version check in image CI. Native release asset
  names and the compose bundle layout are documented for installers and
  updaters; an opt-in compose overlay selects an explicit Rust release tag.
