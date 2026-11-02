### Added (2026-11-02 — Native mailbox recovery and maintainer setup)

- The Rust CLI supports offline mailbox recovery and guided maintainer passkey
  setup, including the installed CLI inside Rust daemon images. Recovery retains
  pending mail and preserves credential generations; rebinding persists private
  state before committing a credential. Setup preserves unrelated configuration,
  backs up replaced files, asks before host changes, and keeps the separate
  Console passkey verification step.
