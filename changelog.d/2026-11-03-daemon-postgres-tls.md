### Fixed (2026-11-03 — Native daemon connects to TLS PostgreSQL)

- The native daemon now honors PostgreSQL TLS modes, including the default
`prefer`, CA and hostname verification, configured or default root files,
system roots and PEM client certificates. Board and principal refresh
connections use the same policy. Unsupported libpq security controls fail
with the setting name and an actionable alternative. A disposable PostgreSQL
comparison covers TLS negotiation, fallback and client authentication against
the Python engine; remaining libpq conventions stay explicitly tracked.
Benign TLS environment defaults and unused certificate files no longer block
plaintext connections, and system trust tolerates malformed individual roots.
Root/CRL loading follows libpq file-root rules; four further benign environment
defaults honor DSN precedence. Multi-host fallback is tested against libpq; the remaining GSS-prefer
difference is explicitly documented.
URI query parameters now replace authority fields as libpq does, so a
query-supplied Unix socket or port works in native images and external DSNs.
