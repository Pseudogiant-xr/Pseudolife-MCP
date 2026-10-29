## Maintainer sent native candidate

The SQL-first endpoint oracle is `0b46bb8e2010cf0e428dd650b98cedb165ad3d75`.
The bounded native `serve` entry point owns the listener, request admission,
query limits, initialized-bank SQL projection and exact response bytes for
`GET /api/maintainer/sent`. It uses the committed shared PostgreSQL module
from `0341ad38e04df356360b1d3cbd4b51b5faefa5b4`, with explicit DSN/TLS
and the named `pg-dsn-explicit-tls` and SAN-only
`pg-tls-webpki-hostnames` policies; no Python implementation is called.

The transport oracle is libpq 18.6 (observed version 180006), upstream
[`REL_18_6`, commit `724edf9bde9d356724ad384a2e196edc3c9f80f7`](https://github.com/postgres/postgres/tree/724edf9bde9d356724ad384a2e196edc3c9f80f7).
Available roots trigger peer verification in
[`fe-secure-openssl.c:930,976,1352–1353`](https://github.com/postgres/postgres/blob/724edf9bde9d356724ad384a2e196edc3c9f80f7/src/interfaces/libpq/fe-secure-openssl.c#L1352).
For `prefer`, failed TLS retries a fresh plaintext connection
([`fe-connect.c:3856–3862`](https://github.com/postgres/postgres/blob/724edf9bde9d356724ad384a2e196edc3c9f80f7/src/interfaces/libpq/fe-connect.c#L3856));
a server startup/authentication ErrorResponse also selects the next encryption
method except `cannot_connect_now`
([`4134–4162`](https://github.com/postgres/postgres/blob/724edf9bde9d356724ad384a2e196edc3c9f80f7/src/interfaces/libpq/fe-connect.c#L4134)).
Native keeps available-root validation and one `prefer` retry within the
existing connect deadline; `require`, `verify-ca` and `verify-full` never
retry plaintext. Invalid SSLRequest replies and pre-connect configuration
refusals do not retry. `allow` remains explicitly deferred. Real owned PG
controls assert both whole HTTP bytes and `pg_stat_ssl`; protocol unit controls
are not TLS acceptance evidence.

Historical frozen `55f28613` Linux controls served exact HTTP/SQL bytes over
DNS/IP SAN TLS and retained named legacy-name refusals. Its wrong-CA `prefer`
control exposed Python HTTP200 over plaintext versus native HTTP503. That
receipt does not validate the later retry repair, which still needs its own
exact source/image proof and independent review. Broader recovery/concurrency,
graceful shutdown, snapshot/export and cutover acceptance remain deferred.
Its first Windows disposable cell matched a nonempty SQL result against the
Python store plus JSON contract, and verified process-tree/database cleanup.
That cell used JSON-as-YAML and does not establish whole HTTP oracle parity,
YAML parity, the later candidate source, Linux execution or performance.
Subsequent Windows candidate controls executed the whole Python ASGI/SQL
response against real native HTTP on separate sequential disposable banks:
33 additive controls passed, including exact bytes, ordered IDs, SQL proof
failures, admission, installer YAML and named startup refusals. The nine-case
native serializer corpus and one eligible immutable tokenless sent GET node
also passed. These are bounded local checks; independent review, hosted checks,
snapshot refresh convergence and graceful shutdown remain pending.

Source integration of accepted master `59a8624e` retains its lease/version
admission and the sent candidate through an ordinary merge. The earlier local
Windows controls belong to `6a582f7f` and do not validate this combined source.
The sent/PG implementation and lock remain unchanged; the combined entrypoint
and inherited dependency features require a fresh bounded Windows gate before
acceptance. Snapshot recovery/concurrency, graceful shutdown and real TLS
integration remain pending.

Pinned Python `serve` parses no trailing flags: it calls `run_daemon()` and
reads `PSEUDOLIFE_MCP_HOST`, `PORT`, `DATABASE_URL`, `CONFIG`, `TOKEN`, `TOKENS`
and `TRUST_BIND` using the `PSEUDOLIFE_MCP_` prefix. The candidate follows this
entry point; no new Python mode or command-line flags are introduced.
All other paths, including `/health`, return HTTP 501 with the same body:
`{"error": "route_deferred", "candidate": "rust-maintainer-sent", "deferred_route": "all paths other than /api/maintainer/sent"}`.
The candidate retains malformed/unsupported explicit DSNs as read-unavailable
configuration: it starts the listener and reaches private 503 only after the
usual admission gates, with `pg-dsn-explicit-tls` alone on stderr. It does not
change Python startup policy. Actual pinned daemon measurements used Python
3.11.17 and four synthetic DSNs (malformed, invalid port, unsupported option,
`allow` with an unreachable loopback endpoint); all reached the admitted 503.
The `python:3.12-slim` daemon image was unavailable in the existing cache, so
that runtime remains unmeasured. Unsupported typed YAML still refuses startup.

Only the sent GET implements application work. The owned sent path retains
pre-dispatch method/body refusals: an authenticated valid or bodyless POST
returns the pinned known-route `400 invalid_request`; this implements no POST
operation and does not imply another route. Nonfinite/invalid-UTF8 POST bodies
remain outside the tested GET contract.
Affirmative readiness is a flushed stdout JSON notice, verified against the
fixture's nonce, listener port and owned process. Fixture shutdown uses the
existing process-tree owner; graceful signal shutdown remains separately
unverified. No installation, production shadow or cutover is enabled.

All boundaries in this candidate remain deferred in the validated register.

| Boundary | Candidate coverage | Status | Notes |
|---|---|---|---|

Actual JSONB object keys remain ordinary application data, including
`$serde_json::private::Number`. The decoder reversibly prefixes object keys
before serde validation and removes exactly that prefix before projection;
values and database-observed member order remain unchanged. This avoids serde's
internal arbitrary-precision number tag without normalizing response bytes.
The one shared serde feature addition relative to the accepted base is
`unbounded_depth`; only the sent JSONB decoder disables the recursion limit,
inside the named 2048-container bound and stack protection. The unused
`tokio-postgres/with-serde_json-1` feature is removed so row decoding cannot
bypass this decoder. Canonical send admission and paired whole HTTP
controls cover numeric-string, text, numeric-value and nested object cases.
