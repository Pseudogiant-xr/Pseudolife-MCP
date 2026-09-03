# Security posture — memory poisoning (ASI06)

A memory system has an attack surface ordinary tools don't: content the
agent *reads* can try to get itself *stored*, and anything stored shapes
every later session. OWASP's agentic-threat catalogue lists persistent
memory poisoning as **ASI06** (published December 2025), and it is the
threat class that decides whether a self-hosted memory server is safe to
put in front of a working codebase.

This page states the threat model and maps every **shipped** mechanism to
the part of it that mechanism actually mitigates — and, at least as
importantly, names what is **not** defended. Vulnerability *reporting*,
the network/auth boundary, and scope live in
[SECURITY.md](../../SECURITY.md); this is the memory-integrity half.
Part of the [user guide](../../README.md#documentation).

Read the mitigations as *containment*, not prevention. None of them stops
a model from being talked into a bad `memory_store`. What they do is bound
how much authority that store can acquire, how visibly, and how cheaply it
can be undone.

## The threat model

**The write path is the agent.** Nothing writes to the **bank** except MCP
tool calls made by your model. A hostile web page, README, or ingested
document cannot write directly — it can only try to convince the model to
call `memory_store` or `memory_fact_set` on its behalf. That
instruction-following boundary belongs to the model and its host, not to
this server. Assume it fails occasionally.

**Dreams amplify.** The **dream** pass promotes episodic text into
canonical **cortex** facts, and cortex facts outrank raw entries at recall
time. A poisoned entry that survives to a dream becomes a poisoned *fact*
with elevated authority, cited to your own memory rather than to the web
page it came from. Consolidation is therefore the privilege-escalation
step, and most of the mechanisms below sit exactly there.

**Correction does not remediate.** The 2026 literature (MINJA,
arXiv 2601.05504) shows query-only injection against agent memories
succeeding at high rates, and shows that agents cannot be *talked* out of
a poisoned memory — conversational correction relapses. Deletion is the
remediation.

**Content inspection is in the evaded class.** MAFIA (arXiv 2608.03844)
defeats *audited* memory stores from the query interface alone: "factual
cloaks" preserve high semantic similarity and malicious effect while
dropping audit detection from ~83% to under 8% at ~90% attack success.
Anything that reasons about *what the text says* is in that class,
including this project's literal-faithfulness gate — which checks fidelity
to the source note, not trustworthiness of the source. The same work
optimizes *placement* so poisoned records win retrieval competition
against a large benign pool, which makes ranking machinery part of the
attack surface rather than a defense. Defenses that key on *who wrote*
are the class this attack does not straightforwardly evade, and that is
why the mechanisms below are provenance-shaped.

## Mechanism → threat map

Every row ships today. "Default" says what a fresh install does, because a
mitigation that is off by default is a mitigation you have not got yet.

| Mechanism | Poisoning step it mitigates | Default |
|---|---|---|
| **Provenance tiers** (`user` > `action` > `agent` origin on every write) | A planted `agent`-origin claim cannot silently supersede a user-stated value. Keys on *who asserted*, not on what the text says | On |
| **Contender parking** | A conflicting value is parked *against* the **slot** rather than taking `current`: `memory_fact_get` shows both, `memory_search` flags the slot `contested`, and `memory_fact_resolve` settles it. Poison must win an explicit decision, not a similarity contest | On |
| **Consolidation quarantine** (the two-man rule, `memory.dream.quarantine_low_trust`) | The dream's privilege-escalation step: an untrusted agent-tier claim parks as a **contender** instead of taking `current`, promotable only by an explicit human resolve or an independent second witness | **Off** — opt-in |
| **Human-reviewed merge queue** | Entity-level poisoning: folding a hostile entity into a trusted one would inherit its facts. Merges are **proposals** carrying their evidence; **merge vetoes** block bad **fold directions** at filing; accept/reject writes an audit-stamped `merge_decisions` row (`decided_by=agent` over MCP, `human` via the Console). By default (`judge_mode: shadow`) a background judge's verdict is a lead, never a decision; the opt-in `judge_mode: auto` folds a pair only when two accepts from DIFFERENT models agree on a non-low-differential row — measured 6/6 on one distinct-model pairing of the 2026-09-02 panel (n=6; not itself a recommendation to flip the default) | On (review required by default; `auto` is opt-in) |
| **Review-queue judges** (links, junk, lesson/world duplicates, deep-dream link candidates) | Same class of risk, scoped narrower: `link_judge_mode`'s `auto` accepts/rejects edge proposals (reversible via `memory_graph_unrelate`/supersede); `junk_judge_mode`'s `auto` deletes only under a structural evidence bar (degree <= 2, at most one fact slot); `curation_judge_mode`'s `auto-distinct` is a reversible dismissal, its `auto` retires (not deletes) the losing lesson/world slot; `candidate_judge_mode` turns deep-dream link candidates into filed proposals or dismissed pairs | `shadow` for links/junk/curation (verdict recorded, nothing applied); `candidate_judge_mode` off |
| **Unreachable-orphan sweep** (`orphan_sweep`) | Deletes entities carrying no evidence at all (no edge, fact, lesson, alias, scope or proposal) once older than `orphan_min_age_days`, capped per pass — the one review-queue mechanism that is a plain delete, not a judged accept/reject | Off — the one destructive default that would fire unattended on the first apply after an upgrade |
| **Dream rollback journal** (schema v27) | Blast radius of one bad consolidation pass. Every dream records a run row plus a per-claim pre-image of what each slot held before the write; `memory_dream(action="rollback")` replays it to revert the latest committed pass | On |
| **Engram cross-index** | Attribution and cleanup: every cortex fact links back to the source entries that produced it, so a bad fact is traceable to the entry that fed it and the rest of that entry's output can be found | On |
| **Supersession history** (**HLC**-ordered) | Audit. Nothing is silently overwritten, so "when did this value change, and which writer changed it" is answerable after the fact via `memory_history` | On |
| **Writer keying / per-principal tokens** | Narrows the blast radius of a leaked credential: a matched per-principal token *becomes* its caller's writer id, where the singular shared token's holder may assert any writer via `X-PL-Writer` | Single token / open loopback |
| **Source exclusion from consolidation** (`memory.dream.exclude_sources`, default `consolidation` / `reflection` / `status` / `log` / `digest`) | Keeps high-volume, low-value chatter out of the dream's input entirely — those entries stay searchable but are never mined for facts or graph edges, shrinking the surface that can reach canonical authority | On |
| **Serving-side quarantine** (`stale_policy`: `annotate` \| `demote` \| `quarantine`) | Not poisoning per se, but the same family: a fact past twice its TTL is flagged `stale`, can be demoted below fresh records, or — at `quarantine` — has its `value` replaced by a wrapper with the original moved to `last_known_value`, so a rotted value cannot be read as current. `stale: true` means *re-verify at the source*, never "the value is wrong" | `annotate` (flag only) |
| **`memory_forget` + engram links** | Remediation. `scope="memory"`/`"fact"` hard-delete; `scope="lesson"`/`"world"` retire (status flips, row kept with a `store_decisions` audit trail, undoable via `restore_slot` until compaction purges it) — a poisoned lesson/world fact is contained, not purged, until then. The links tell you what else to retire | On |

Two of these deserve their exact claim restated, because overselling them
would be the same failure this page is about:

- The **consolidation quarantine** does not stop a poisoned entry from
  being *stored* or *retrieved* — episodic search still surfaces it. It
  stops poison from silently gaining *canonical* authority. And writer
  identity is self-reported over MCP, so the two-man rule raises the bar
  from "one convinced model call" to "two independent-looking writes or
  one human act". That is a mitigation, not an authentication scheme. Its
  preregistration is
  `docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md`.
- The **merge queue** gates *folds*, not writes. A hostile fact attached
  to a correctly-named entity never goes near it.

## What is NOT defended

Stated flatly, because an inaccurate boundary claim is a worse trust
problem than the limitation it hides.

- **Prompt injection against your agent.** If a hostile document convinces
  the model to call `memory_store`, the store happens. This server has no
  view of the model's context and cannot arbitrate it.
- **Content screening.** There is none, deliberately — see the evaded-class
  note above. Do not read the literal-faithfulness gate, the novelty gate,
  or the contradiction detector as trust filters. In particular the
  **surprise gate** is an *admission* filter that drops near-duplicates:
  novel malicious content passes it preferentially.
- **Cryptographic writer authentication.** Writer identity is
  self-reported over MCP. Per-principal bearer tokens improve on this
  (the token determines the writer id) but there is no signing, no
  attestation, and no way to prove after the fact that a given writer
  really made a given write. This is roadmap, not a promise.
- **Ranking as a defense.** Retrieval scoring is optimizable by an
  attacker who can influence stored text. Treat rank as convenience, not
  as a trust signal.
- **Rate or volume anomaly detection.** Nothing notices that one session
  wrote four hundred facts about one entity. The **review queue** and the
  Console's counts are how a human would notice, and only if they look.
- **Ingested-document provenance.** `document_ingest` indexes what you
  point it at; the reference bank carries no trust tier of its own.
- **World-fact citations are not verified.** A **world fact** carries a
  source URL and a quote because someone asserted them. Nothing fetches
  the URL to confirm the quote is there.
- **The host.** Anything with local file or Postgres access owns the bank
  outright. The **bank** is as sensitive as the codebase it remembers, and
  `/health` is deliberately unauthenticated and verbose — reasons not to
  publish the port.
- **The quarantine ships off.** The strongest consolidation-side
  mitigation is opt-in. Turning it on costs you unattended promotion of
  agent-tier claims, which is the trade it exists to offer.

## If you think a memory is poisoned

1. **Do not try to correct it conversationally.** That leaves live poison
   with a correction beside it.
2. **Find it and delete it** — `memory_forget(scope="memory", ...)` for the
   entry.
3. **Follow the engram links** — `memory_fact_get` reports `source_entries`;
   retire any cortex facts derived from the entry
   (`memory_forget(scope="fact", ...)`).
4. **If a dream did it, roll the pass back** —
   `memory_dream(action="runs")` to find it, then
   `memory_dream(action="rollback")`, which restores each touched slot's
   pre-image.
5. **Read the history** — `memory_history(entity)` gives the dated causal
   chain, which is how you find what else arrived alongside it.

## Hardening checklist

- Turn on the consolidation quarantine
  (`memory.dream.quarantine_low_trust`) if unattended agents write to this
  bank.
- Keep the daemon loopback-bound. If it must be reachable, set
  `PSEUDOLIFE_MCP_TOKEN` — the daemon refuses a non-loopback bind without
  one — and prefer a `PSEUDOLIFE_MCP_TOKENS` map so each caller's token
  *is* its writer id.
- Prefer a local extractor. Consolidation reads everything in the memory
  stream; a hosted extractor endpoint sends it off the machine.
- Back up before you need to. Supersession history and the rollback
  journal are your audit trail, and both live in the bank
  ([backups](configuration.md#backups)).
- Work the **review queue**. Merge proposals accumulate; an unread queue is
  where an entity-level fold eventually gets accepted by whoever is in a
  hurry.

Reporting a break in any of the boundaries above:
[SECURITY.md](../../SECURITY.md).
