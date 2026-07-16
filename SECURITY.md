# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's private vulnerability reporting:
**Security tab → "Report a vulnerability"** on this repository. You'll get an
acknowledgement within 7 days. This is a solo-maintained project — fixes are
best-effort but security reports jump the queue.

## Supported versions

Pre-1.0: only the latest release (and current `master`) receives fixes.

## Threat model — what the design promises

Pseudolife-MCP stores *your agent's memory* — treat the bank as sensitive.
The shipped configuration is deliberately conservative:

- **Loopback by default.** The daemon and Postgres publish to `127.0.0.1`
  only; the extractor sidecar is never published to the host at all. The
  network boundary — not the default Postgres password — is the guard.
- **Token-gated off loopback.** The daemon *refuses* to bind a non-loopback
  host without `PSEUDOLIFE_MCP_TOKEN` set. The Cortex Console web UI is
  gated by the same token.
- **Postgres is never LAN-exposed.** Remote clients only ever reach the
  daemon.

## In scope

Reports that break one of those promises are exactly what we want to hear
about, e.g.:

- Bypassing the bearer-token gate on the daemon or Cortex Console.
- SQL injection through any MCP tool argument or Console endpoint.
- XSS or content injection in the Cortex Console (it renders
  memory/graph content — hostile memory text must stay inert).
- Path traversal / arbitrary file read via `document_ingest` or config
  endpoints.
- Unsafe deserialization (e.g. of legacy `.pt` state files).
- Anything that lets one MCP client read or write another machine's bank
  through the daemon.

## Out of scope

- The default `pseudolife` Postgres password on the stock loopback-only
  stack (documented boundary; override via `POSTGRES_PASSWORD` in
  `ops/.env` if your setup differs).
- Deployments that publish the daemon or Postgres beyond loopback *without*
  the token, contrary to the docs — the daemon already refuses the
  footgun configuration it can detect.
- Resource exhaustion of your own local daemon by your own client.
- Vulnerabilities purely in upstream dependencies (report upstream; we
  track and take patched releases).

## Hardening pointers

See the guide's sections on
[LAN sharing](docs/guide/configuration.md#sharing-memory-on-the-lan)
(`PSEUDOLIFE_MCP_TOKEN`) and
[backups](docs/guide/configuration.md#backups), plus the compose file's
port-binding comments — backups are part of your security posture too.
