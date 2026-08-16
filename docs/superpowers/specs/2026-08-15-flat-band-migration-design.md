# Flat-band migration — design (2026-08-15)

Executes the 2026-08-15 verdict
(`2026-08-14-flat-band-verdict-preregistration.md`): the 8-band
continuum earns nothing measurable anywhere, so the default store
becomes one flat band. This is a **default flip, not a demolition** —
the multi-band machinery (promotion, cascade, rebalance, per-tier
retention) stays in the tree, exercised by its invariant tests and
available via `preset: continuum`, so rollback is one config line and
the diff stays reviewable. Removal, if ever, is a later cleanup release.

User-ratified scope (2026-08-15): migration + webui corrections + full
README/docs audit.

## Decisions

1. **New preset `flat`, and it becomes the default**
   (`presets.py`, `MIRASConfig.preset` default `continuum` → `flat`).
   One band: `name=flat, max_entries=5250` (the continuum's measured
   total — capacity semantics unchanged), promotion unreachable
   (`promotion_access_count=10**9, promotion_surprise=1.1`),
   `update_interval=10**9`, `retention_policy=balanced` — byte-for-byte
   the ablation arm that tied everything
   (`band_ablation.write_flat_config`). `continuum` and its deprecated
   aliases stay in the registry. The deployed daemon's
   `/data/config.yaml` pins no preset (verified 2026-08-15), so the
   flip carries to production on the next `ops/update.ps1` deploy, and
   `preset: continuum` there is the rollback.
2. **n=1 eviction is a true drop, made intentional and visible**
   (E4 break #1). `_on_band_evict` already falls through to storage
   deletion when no deeper band exists; under flat that IS the design —
   retention-scored delete at genuine total capacity (the E1-measured
   behavior). Change: docstring rewritten to say so, an eviction
   counter (`true_drops`) surfaced in `stats()`, and an info log per
   drop. At 682/5250 resident, first drop is ~years out.
3. **`bands=` filters validate loudly** (E4 break #2): unknown band
   names raise `ValueError` naming the valid set, at the service
   boundary (`service.search` / `service.trace`), instead of returning
   silently-empty results. The MCP and web layers already surface
   exceptions as tool/API errors.
4. **File-mode state restore falls back like hydrate does** (E4 break
   #3): `_load_schema_v2` routes saved bands whose name no longer
   exists into `bands[0]` with a warning, mirroring `sync.py:103`,
   instead of silently skipping them (which lost every entry).
5. **Band stamps reconcile at hydrate, write-through, no schema bump.**
   After `hydrate_cms`'s rebalance, any entry whose `bank` stamp
   differs from its seating band gets the stamp rewritten in memory and
   in storage (`update_entry`). Config-aware where a schema migration
   cannot be, idempotent (second boot touches zero rows), and it also
   fixes the pre-existing phantom-key wrinkle in `_tier_hits` (stale
   stamps minting unreported telemetry keys). Covers the `.pt`
   importers too: legacy names they write are reconciled at the next
   hydrate. Promotion config fields on `MIRASBandSpec` stay (used by
   `continuum`/custom presets).
6. **Webui**: `observatory.js` renders a single-band store as a
   capacity meter without the "across N bands" framing (and never says
   "across 1 bands"); `stream.js` hides the band chip when only one
   band exists (constant chips are noise); the two read-time recency
   knobs leave the Console config surface (`config_io.py`) — inert
   under one band (`cms.py:773` short-circuits on n=1), config fields
   retained for multi-band configs; `web/fixtures.py` demo stats move
   to the flat shape (fixture-vs-real parity test updated with it).
7. **Docs audit** (same change): README continuum/band mentions;
   `docs/guide/configuration.md` miras + recency sections;
   `docs/guide/benchmarks.md` gets retire-at-site pointers from the
   July band numbers to the verdict; `docs/atlas/atlas.json` storage/
   cms cards (re-verified, not just renumbered — `test_atlas_currency`);
   `plugin/commands/memory-status.md` band-ladder prompt;
   `CLAUDE.md` glossary (continuum/bands entry updated to state flat
   default + retained machinery); `llms.txt`/`llms-full.txt`
   regenerated (`ops/gen_llms_txt.py`, pinned by `test_llms_txt`).
8. **Not changed**: NLI contradiction breadth (not wired in
   production); MTT `retention_boost` (band-count-agnostic); dream/
   deep-dream (zero band coupling, verified); schema version (no DDL);
   supersession/staleness forgetting (load-bearing, untouched).

## Test plan

New/updated, watched-RED where behavior changes:
- flat preset shape + default (`test_phase0_config`: default is flat,
  continuum preset still 8 bands).
- n=1 eviction: at-capacity store drops lowest retention score, deletes
  the storage row, bumps `true_drops`; multi-band cascade tests
  unchanged (custom configs).
- `bands=` validation: unknown name raises with valid names in the
  message; valid single-band filter works (`test_service` bands
  round-trip updated from `instant` to `flat`).
- `_load_schema_v2` fallback: 8-band save restores into flat bands[0]
  (replaces the E4 smoke's demonstrated total loss).
- Hydrate reconciliation: rows stamped `working`/`slow` hydrate into
  flat, stamps rewritten in memory AND storage; second hydrate touches
  nothing (idempotence); `test_pg_storage`'s `band == "slow"` pin
  updated.
- Fixture contract parity.
- Existing multi-band invariant suites (overflow/depth-prior, hydrate
  capacity, consolidation atomicity, slot index) keep passing on their
  custom configs — they are the retained machinery's spec.

## Verification & rollout

Full suite + bench Postgres. The change is retrieval-affecting →
`evals/regression_gate.ps1` before commit; the gate's committed
baseline was measured under the continuum default, so a failure that is
exactly the intended selection change (rag-control selection drift with
accuracy inside the verdict's noise band) is resolved by a DELIBERATE
re-baseline documented in the same commit — any other failure blocks.
Gate needs the reproducible GPU server (maintainer arranges the window;
the daily driver holds :1234).

Deploy (separate, post-merge, maintainer-clicked): normal
`ops/update.ps1` (backup → rollback tag → rebuild → health), then live
verify per checklist: `memory_stats` shows one `flat` band with 682
entries seated, a `memory_search` returns results, Console Observatory
renders the flat meter, and `SELECT DISTINCT band FROM entries` on the
bank shows only `flat` after first hydrate. Rollback: `preset:
continuum` in `/data/config.yaml` + redeploy — hydrate reseats and
reconciles stamps back automatically.
