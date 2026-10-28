| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| Maintainer passkey verifier (`maintainer_webauthn.rs`): an RS256 key above 8192 bits | `cryptography` (OpenSSL) verifies PKCS#1 v1.5 signatures up to 16384-bit moduli | aws-lc-rs caps PKCS#1 verification at 8192 bits, so such a key enrols but every signature it makes is refused (`signature`, answered `assertion_invalid`) | no platform authenticator makes RSA keys over 4096 bits; ES256 and EdDSA are unaffected |
