### Security (2026-10-28 — maintainer passkeys refuse degenerate Ed25519 keys)

- The maintainer passkey verifier accepted an Ed25519 (EdDSA, COSE alg -8)
  public key of small order: the identity, the points of order 2, 4 and 8,
  and their non-canonical encodings. Such a key verifies a degenerate
  signature over any message, so once enrolled anyone with a bearer could
  sign maintainer actions as it. Enrolling one already needed the bootstrap
  code or an approving signature. These keys are now refused (`cose_key` in
  the daemon log) at registration and at every assertion, so a key enrolled
  before this fix no longer verifies; revoke it and enrol a real passkey. No
  authenticator generates such a key.
