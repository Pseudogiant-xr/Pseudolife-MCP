| Item | Python | Rust | Why | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| TLS and libpq conventions | psycopg/libpq supports TLS, all libpq DSN/environment/password-file controls and OpenSSL conventions | Native daemon supports all six `sslmode` values, default `prefer`, `sslrootcert` including `system`, libpq default certificate paths, `PGSSLMODE`/`PGSSLROOTCERT`/`PGSSLCERT`/`PGSSLKEY`, and PEM client certificates with unencrypted keys. Unsupported TLS/GSS controls are named refusals with a Python-engine alternative. Non-TLS libpq conventions remain bounded by tokio-postgres. | TLS-required startup is implemented; remaining libpq/OpenSSL conventions must not be mistaken for full driver parity | behaviour with a caller | `storage/tls.rs`, `storage/tls_transport.rs` and shared `pg_tls.rs` replace plaintext-only storage, board and principal connections. `harness/pg_tls.py` compares 92 cases against psycopg on disposable PostgreSQL and pins `harness/goldens/pg_tls.json`; protocol units cover fallback, invalid negotiation responses and timeout. Master `dc43e667` still used `NoTls`; the real-server RED mismatched 11/19 cases. B14b owner: R10 for TLS wiring; B4/B11 via R1/R5 for residual compatibility boundaries below. |

TLS follows libpq's transport order: `allow` starts plaintext and can retry TLS
after authentication refusal; `prefer` starts TLS and can retry plaintext after
a TLS failure. A server's `N` response uses the same plaintext socket only in
`prefer`; an invalid SSLRequest response never permits startup or fallback.
Roots and client identity load after the server accepts TLS, so a plain server
does not fail because of unused TLS files. A missing root is optional outside
the verification modes; an existing root authenticates the chain even in
`require`. `verify-full` also checks a DNS/IP subject alternative name.

The disposable recording includes TLS and plaintext servers, TLS-only HBA,
client-certificate HBA, valid/wrong/missing/malformed roots and system roots.
An expanded RED found that eager loading of a malformed root incorrectly
prevented `prefer` fallback; lazy loading made all 92 cells match. This is a
development-service proof, not a claim that hosted CI runs Docker TLS cases.
Parser, default-file, refusal and protocol tests run in the daemon unit suite.

The conductor accepted explicit refusal boundaries for encrypted keys, CRLs,
GSS, and legacy CN-only/IP-in-dNSName certificates. The errors name the option
or the SAN certificate repair and the Python engine. Other unsupported TLS
controls (including direct negotiation and OpenSSL protocol settings)
are refused by name; no unsupported security setting is silently ignored.
Only PEM identities are accepted; DER/OpenSSL-engine identities require the
Python engine. Ambient `root.crl` and encrypted keys are refused before a TLS
attempt, including `prefer`, instead of weakening their policy on fallback.

Non-TLS libpq environment defaults, `.pgpass`, service files, multi-host
alternate-transport ordering, and OpenSSL configuration are not closed by
this change. Ordinary DSN options retain tokio-postgres's parser and host
selection. The row stays in the caller bucket for these remaining obligations;
it is not a declaration of universal libpq parity or a G2 approval.

Follow-up after cutover: unify daemon and shim negotiation; the conductor
approved a daemon-local transport now to preserve the shim's existing behavior.
