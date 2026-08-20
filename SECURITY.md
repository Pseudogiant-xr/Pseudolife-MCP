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
- **Token-gated off loopback.** A daemon run directly on a host *refuses* to
  bind a non-loopback address without `PSEUDOLIFE_MCP_TOKEN` (or a parsed
  per-principal `PSEUDOLIFE_MCP_TOKENS` map) set — it logs the refusal and
  exits. Per-principal tokens also *become* the writer identity of their
  caller, which narrows the blast radius of a leaked credential: a
  compromised principal token asserts only its own writer id, where the
  singular shared token's holder may still assert any writer via
  `X-PL-Writer`.
- **`PSEUDOLIFE_MCP_TRUST_BIND` is the documented exception to that, and the
  shipped compose stack sets it.** In a container the daemon must bind
  `0.0.0.0` to be reachable at all, so the flag is the operator's assertion
  that the boundary is enforced *outside* the process — and in the shipped
  stack it is: the container's port is published only to `127.0.0.1`. With
  the flag set the daemon warns and continues instead of exiting. Do not set
  it for a host-run daemon; there is nothing outside the process enforcing
  the boundary there.
- **The Cortex Console is two surfaces, gated differently.** The static SPA
  shell (`/`, `/ui/*`) is open by design — it is code, no data. The data
  endpoints (`/api/*`) sit behind the same bearer token as `/mcp` *when a
  token is configured*. With no token set the token check passes
  unconditionally, and what actually guards `/api` is a loopback
  `Origin`/`Host` check: a non-loopback `Origin` (CSRF) or `Host` (DNS
  rebinding) is rejected. That is a browser-facing guard, not
  authentication — anything that can make a loopback-looking request to a
  tokenless daemon can read the bank.
- **`/health` is unauthenticated and verbose.** It is an open liveness probe
  by design, and it reports more than "ok": schema version, storage backend,
  whether a token is set, durable-save error count, and — when the daemon is
  degraded — a startup-refusal message and the raw database error string,
  which can carry DSN-shaped detail. Anyone who can reach the port reads all
  of it. Fine on the loopback default; a reason not to publish the port.
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
- **No content screen is a defense either.** The 2026 attack literature
  (MAFIA, arXiv 2608.03844) defeats *audited* memory stores from the
  query interface alone: "factual cloaks" keep high semantic similarity
  while preserving malicious effect, dropping audit detection from ~83%
  to under 8% at ~90% attack success. Any check that inspects *what the
  text says* — semantic screens, and this project's literal-faithfulness
  gate, which checks fidelity to source, not trustworthiness of source —
  is in the evaded class. The same work optimizes *placement* so poisoned
  records win retrieval competition against a large benign pool; ranking
  machinery is part of the attack surface, not a defense. Defenses that
  key on *who wrote* (provenance tiers, writer keying, source tags) are
  the class this attack does not straightforwardly evade.
- **Dreams amplify.** The consolidation pass promotes episodic text into
  canonical cortex facts that outrank raw entries at recall time. A
  poisoned entry that survives to a dream becomes a poisoned *fact* with
  elevated authority. Mitigations that exist today: provenance tiers
  (`user` origin outranks `action`, which outranks `agent` — a planted
  agent-origin claim cannot silently overwrite a user-stated fact),
  per-entry `source` tags, `source="status"` exclusion from dream
  extraction, the engram cross-index (every cortex fact links back to
  its source entries, so a bad fact is auditable to the entry that fed
  it), and — opt-in via `memory.dream.quarantine_low_trust` — the
  two-man rule: an untrusted agent-tier claim parks as a visible
  contender instead of taking `current`, promotable only by an explicit
  human resolve or an independent second witness. The quarantine's claim
  is deliberately narrow: it does not stop a poisoned entry from being
  stored or retrieved — episodic search still surfaces it — it stops
  poison from silently gaining *canonical* authority.
- **Remediation is deletion, not correction.** If a poisoned memory lands:
  `memory_forget` the entry, then follow its engram links and retire any
  cortex facts derived from it. Supersession history is your audit trail.
  Telling the agent "that was wrong" only adds a correction alongside live
  poison.

The consolidation quarantine above implements the preregistration in
`docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md`
(ships off; writer identity remains self-reported over MCP, so the rule
raises the bar from "one convinced model call" to "two independent-looking
writes or one human act" — a mitigation, not an authentication scheme).
Not yet built (roadmap, not promises): cryptographic writer authentication,
the stronger form.

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
  footgun configuration it can detect. Setting `PSEUDOLIFE_MCP_TRUST_BIND`
  on a host-run daemon deliberately waives that refusal; doing so and then
  exposing the port is an operator choice, not a defect.
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
