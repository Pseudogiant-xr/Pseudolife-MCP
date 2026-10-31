### Added (2026-11-01 — complete passkey-signed maintainer actions in the Rust Console)

- The Rust daemon now completes maintainer passkey enrolment, role changes, messages, cancellation, withdrawal and self-revocation from the Console. These six routes previously returned 501. They verify the signed challenge, credential and nonce before changing the board, keep refused actions from spending a challenge, and preserve the enrolment confirmation and quarantine rules.
