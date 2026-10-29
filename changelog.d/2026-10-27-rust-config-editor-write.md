### Added (2026-10-27 — the Rust daemon saves the Console's settings, applied at the next start)

- The Rust daemon now answers `POST /api/config`, which returned 501 `not_implemented`. It refuses what the Python daemon refuses, in the same order and with the same messages (an empty patch, the protected `coordination.maintainer` keys, an unknown knob, an out-of-range or mistyped value), and writes config.yaml byte for byte as the Python daemon does, after a timestamped backup. Unlike the Python daemon it applies no setting to the running daemon: every saved knob is reported as needing a restart, and the Console's settings read shows the saved value until then.
- A config.yaml holding a YAML timestamp or binary value is refused (500, file untouched) rather than rewritten.
