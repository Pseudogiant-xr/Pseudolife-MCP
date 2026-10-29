## Shared PostgreSQL client preparation

The named **`pg-dsn-explicit-tls`** scope admits explicit sslmode/sslrootcert in
the installed/documented single-TCP-host DSN grammar and the platform default
root.crt behavior. `require` verifies an available CA as `verify-ca`; `prefer`
also verifies an available CA. Ambient client certificate/key and CRL files,
PGSSL*/PGTLS* variables and other libpq connection/session PG* controls refuse
with exit 1 and a diagnostic naming only the control. Unknown configured DSN
options or modes likewise refuse by option name; none is silently ignored.

`shim/tests/pg_client.rs` contains disposable SSLRequest, CA, hostname,
default-root, named-refusal, resolution/argv and connection-closure controls.
The first Windows preparation run passed six tests and 18 transport cells before
the invalid-SSL-response admission fix; that run remains historical. The final
focused test covers invalid SSL responses before startup, all five modes,
available/missing/default/wrong CA roots and connection closure.

The named **`pg-tls-webpki-hostnames`** rule restricts verify-full to WebPKI
hostname grammar: matching SAN `dNSName` and `iPAddress`. CN-only certificates
and IP addresses in `dNSName` refuse exit 1 with the named rule; the diagnostic
explains libpq's matching legacy acceptance. The original disposable comparison
retains libpq 18 success against native refusal for both certificates. The
additive controls require the named refusal before startup and separately show
that `verify-ca` still accepts their valid CA chains. No bespoke hostname
verifier, timestamp/hash relaxation or certificate normalization is added.

The shared-client checkpoint prepared the reusable dedicated client separately
from break/delegate/designate transactions and embedded/container lifecycle. Real PostgreSQL rows,
audit-chain effects, shared fixed-clock replay, Linux execution and fresh review remain pending; targeted Windows
format, both-feature Clippy and eight PG transport tests passed. Neither full CLI-LEASE nor any
new mode is promoted by these transport observations.
