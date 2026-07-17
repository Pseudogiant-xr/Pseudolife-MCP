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

## Memory poisoning

A memory system has an attack surface ordinary tools don't: content the
agent *reads* can try to get itself *stored*, and anything stored shapes
every later session. The 2026 literature (MINJA, arXiv 2601.05504) shows
query-only injection against agent memories succeeding at high rates — and
that agents cannot be *talked* out of a poisoned memory (conversational
correction relapses; deletion is the only remediation).

How this maps onto Pseudolife:

- **The write path is the agent.** Nothing writes to the bank except MCP
  tool calls made by your model. A hostile web page or document cannot
  write directly — but it can try to convince the model to call
  `memory_store` on its behalf. That instruction-following boundary is the
  model's, not this server's; assume it will occasionally fail.
- **The surprise gate is an admission filter, not a trust filter.** It
  drops near-duplicates. Novel malicious content passes it *preferentially*.
  Do not mistake novelty gating for a defense.
- **Dreams amplify.** The consolidation pass promotes episodic text into
  canonical cortex facts that outrank raw entries at recall time. A
  poisoned entry that survives to a dream becomes a poisoned *fact* with
  elevated authority. Mitigations that exist today: provenance tiers
  (`user` origin outranks `action`, which outranks `agent` — a planted
  agent-origin claim cannot silently overwrite a user-stated fact),
  per-entry `source` tags, `source="status"` exclusion from dream
  extraction, and the engram cross-index (every cortex fact links back to
  its source entries, so a bad fact is auditable to the entry that fed it).
- **Remediation is deletion, not correction.** If a poisoned memory lands:
  `memory_forget` the entry, then follow its engram links and retire any
  cortex facts derived from it. Supersession history is your audit trail.
  Telling the agent "that was wrong" only adds a correction alongside live
  poison.

Not yet built (roadmap, not promises): trust-weighted consolidation and a
quarantine tier for low-trust sources ahead of the dream pass.

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
- A path that lets *content* (a stored memory, an ingested document, graph
  text) execute, exfiltrate, or write to the bank without a tool call —
  i.e. a break in the "the write path is the agent" boundary above.

## Out of scope

- The default `pseudolife` Postgres password on the stock loopback-only
  stack (documented boundary; override via `POSTGRES_PASSWORD` in
  `ops/.env` if your setup differs).
- Deployments that publish the daemon or Postgres beyond loopback *without*
  the token, contrary to the docs — the daemon already refuses the
  footgun configuration it can detect.
- Resource exhaustion of your own local daemon by your own client.
- The model *choosing* to store attacker-authored text it read (prompt
  injection against the agent) — that boundary belongs to the model/host;
  this file documents how to contain and remediate it, and hardening that
  containment is in scope.
- Vulnerabilities purely in upstream dependencies (report upstream; we
  track and take patched releases).

## Hardening pointers

See the guide's sections on
[LAN sharing](docs/guide/configuration.md#sharing-memory-on-the-lan)
(`PSEUDOLIFE_MCP_TOKEN`) and
[backups](docs/guide/configuration.md#backups), plus the compose file's
port-binding comments — backups are part of your security posture too.
