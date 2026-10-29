### Added (2026-10-28 — the Rust daemon issues the maintainer's passkey challenges)

- The Rust daemon now answers `POST /api/maintainer/challenge`, which returned 501 `not_implemented`, behind the same gate as the maintainer's reads. For each purpose (send, enrolment, cancel, revoke, repudiate and the four role changes) it validates the request as the Python daemon does, builds the payload the passkey signs with its expiry and MAC, and returns the WebAuthn options and the preview the Console shows before the prompt. Completing a signed action still returns 501 on the Rust daemon.
