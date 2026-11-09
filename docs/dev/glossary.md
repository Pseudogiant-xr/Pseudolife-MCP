# Glossary

Use these terms, and only these, with the maintainer, in PR bodies and in
commit messages. `CLAUDE.md` ("Glossary") keeps the few it uses itself and
points here for the rest.

- **continuum / bands**: the associative store's layout; default one flat
  band (`preset: flat`), the 8-band `working`→`forever` continuum opt-in.
  An **entry** is one memory in a band.
- **cortex**: the slot-keyed canonical-fact store, one *current* value per
  `(entity, attribute)` **slot**, superseded not decayed; a **set-valued
  slot** holds many concurrent members (add/remove).
- **contender**: a competing value parked against a slot instead of
  overwriting it (low-trust dream claims, number-led set adds).
- **supersede**: replace a slot's value, keeping the old one as audit
  history; nothing is silently overwritten.
- **freshness_class / stale**: `evergreen` / `slow` / `volatile`; `stale:
  true` (past twice the TTL) means re-verify at the source, never "the
  value is wrong".
- **HLC**: the hybrid logical clock that orders supersession; wall-clock
  times are display-only.
- **dream**: consolidation: pull the unconsolidated stream → extract
  `(entity, attribute, value)` claims → `fact_set` → advance the monotonic
  **cursor**.
- **session digest**: one prose entry per closed session episode
  (`source="digest"`) from the idle dream cycle; default-off
  (`digest_enabled`).
- **extractor / sidecar**: the dream-extraction model: the bundled CPU
  sidecar, the Sonnet CLI shim, or an OpenAI-compatible endpoint.
- **deep dream**: the manual full-corpus graph pass: safe self-clean plus
  merge/link candidates routed to review.
- **merge proposal / fold direction / merge veto**: graph entity dedup; a
  proposal folds one entity into another, fold direction (re-derived at
  review) says which survives, a veto is a name-shape rule blocking a bad
  fold at filing.
- **review queue**: pending graph proposals (the Console's Review view).
- **quarantine**: always qualified: *serving-side* is the `stale_policy`
  withholding a stale value; *consolidation* is the two-man rule parking
  low-trust dream claims as contenders.
- **world facts**: cited external facts (URL + quote), a separate cortex.
- **lessons / outcomes**: `memory_outcome` signals distilled into the
  do/avoid guidance shown at session start.
- **episode**: the session-scoped handle every write is stamped with.
- **bank / daemon / shim**: the durable memory volumes; the HTTP service
  owning them (one process, one bank); the stdio MCP bridge to the daemon.
- **principal / tier**: the bearer-token caller identity, which keys the
  toolset tier.
- **conductor**: the maintainer's board role (granted per project) that acts
  for them on the board, held as the lease `conductor:<project>` and granted
  with `lease conductor`; its old names (`delegate:<project>`, `lease
  delegate`, `delegate-watch`) are deprecated and still accepted.
- **Console**: the web UI (Cortex Console). The **System Atlas**
  (`docs/atlas/`) is the curated codebase architecture map, not the
  Console's Graph view of the bank.
