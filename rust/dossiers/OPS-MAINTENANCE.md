# OPS-MAINTENANCE

Source commit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`.
Producer attribution: **dossier author's source census**. References below
name this revision; no operator command was executed against a bank.

## 1. What the row promises

Preserve the six maintenance scripts' distinct dry-run, mutation and refusal
contracts, rather than turn them into one generic migration command
(`rust/PARITY.md:301`; `ops/migrate_embeddings.py:322`). Some write SQL directly,
one uses the service writer lease, one drops AGE immediately, and one is
read-only (`ops/retire_by_writer.py:25`; `ops/dedup_cortex.py:38`;
`ops/migrate_drop_age.py:19`; `ops/measure_reverify_population.py:63`).
Preparation here grants no live-bank mutation or migration approval.

## 2. Entry points

| Python function | Role / next call |
|---|---|
| `ops/migrate_embeddings.py:148 _live_dim` | Catalog vector dimension SELECT |
| `:162 _row_count`, `:166 build_plan`, `:172 print_plan` | Table counts, dimensions and report |
| `:184 _daemon_reachable` | Any HTTP response or timeout counts as a live daemon |
| `:215 _claim_text`, `:231 _fetch_rows`, `:241 _row_text` | ID-ordered stored document-side text |
| `:250 migrate_table` | Per-table ALTER, locked fetch, embed/update and constraint restore |
| `:290 stamp_schema_version` | Final meta upsert after all pending tables |
| `:303 _build_pipeline` | Stock EmbeddingConfig, batch floor 32 |
| `:310 _apply_lock_timeout` | Bound ALTER waiting to ten seconds |
| `:322 run`, `:406 main` | Refusal gates, explicit transactions and CLI |
| `ops/dedup_cortex.py:38 main` | MemoryService -> cortex_dedup -> apply-only autosave |
| `service.py:6155 cortex_dedup` | Service lock/init, scalar slot backfill, sibling retirement |
| `service.py:3577 autosave_if_changed` | Changed-state durability; BACKGROUND/WRITE-CORTEX dependency |
| `ops/retire_by_writer.py:25 run`, `:73 main` | Filter writer/session, count and apply three-store retirement |
| `ops/backfill_edge_confidence.py:31 recompute_rows`, `:37 _dsn`, `:43 main` | Agent-edge projection -> relation_quality.edge_confidence -> changed-only updates |
| `memory/relation_quality.py:102 edge_confidence` | Shared confidence scorer; GRAPH-REVIEW dependency |
| `ops/migrate_drop_age.py:19 main` | Autocommit extension drop, no dry-run parser |
| `ops/measure_reverify_population.py:63 run`, `:83 main` | Read-only population SQL and text/JSON report |

All paths under `service.py`, `memory/` and `storage/` above mean
`pseudolife_memory/`. No HTTP route is introduced. Direct SQL scripts use
plain psycopg connections, not PostgresStorage's schema initialization.
Dedup follows `_ensure_init` -> PostgresStorage writer lease/hydration ->
resident cortex `dedup_siblings` -> `_save_cortex` -> changed-slot sync/storage.
That leaf's complete mutation/trace SQL belongs to WRITE-CORTEX; this dossier
does not duplicate its owned implementation.

## 3. State touched

| Script | Durable/resident effects and transaction boundary |
|---|---|
| Embedding migration | `facts`, `world_facts`, `lessons`, then `entries` (`migrate_embeddings.py:138`); `embedding` typmod/values, entries NOT NULL; final `meta.schema_version`. Autocommit connection at `:326`, one explicit transaction per table at `:267`, then separate stamp transaction at `:295`. No all-four-table atomicity. |
| Dedup | May initialize/hydrate service and startup metadata even in dry-run (`dedup_cortex.py:15`). Resident scalar missing `slot_embedding` backfill; set members skipped; apply retires sibling slots and persists changed slots under service lock (`service.py:6173`). Never use full-table snapshot as a replacement for the apply save. |
| Writer retirement | Current `facts/world_facts/lessons` filtered by `writer_id` plus optional `session_id`; updates `status='superseded',superseded_at=<one clock>`; one `conn.commit` after all updates (`retire_by_writer.py:29,39,57,68`). No writer-lease check in this direct script. |
| Edge backfill | Reads current agent-origin `edges` plus `entities.display`; updates only `edges.confidence` when delta exceeds `1e-6` (`backfill_edge_confidence.py:21,52,65`). Connection transaction, five-second lock and 30-second statement timeout, explicit commit. No DDL/ID allocation. |
| AGE drop | `DROP EXTENSION IF EXISTS age CASCADE` with autocommit (`migrate_drop_age.py:24`); removes extension-dependent objects. No dry run, backup verification, daemon probe or writer lease in the source. Relational public-bank preservation needs catalog/state proof. |
| Population report | `facts.status/entity_norm/attribute_norm/last_confirmed/asserted_at`, `memory_traces` slot/entry IDs, `entries.superseded_at`; read-only transaction with local public search path (`measure_reverify_population.py:34,70`). No explicit repeatable-read isolation. |

SQL anchors: migration dimension `:153`, counts `:163`, row SELECTs `:233,236`,
ALTER/UPDATE/NOT NULL `:269,272,282,287`, schema stamp `:297`; retirement
SELECT `retire_by_writer.py:39`, UPDATE `:62`; edge SELECT
`backfill_edge_confidence.py:21`, UPDATE `:65`; AGE DROP `migrate_drop_age.py:25`;
population's four SELECTs `measure_reverify_population.py:36`.
These direct scripts do not advance application sequences. Migration restores
entries' constraint after updating every row; earlier table commits survive a
later failure. Entries runs last so dimension refusal remains armed while
other stores are partly migrated (`migrate_embeddings.py:138`).

## 4. Shipped producers

These are operator workflows and docs examples, not background calls:

- `docs/runbooks/embedding-v25-migration.md:166,201,214` sends
  `python ops/migrate_embeddings.py` (dry-run), then
  `--apply --backup-verified`, optionally `--assume-daemon-stopped`.
  Canonical optional values are `--dsn <DSN>` and `--health-url <URL>`
  (`migrate_embeddings.py:406`). Backup verification is a boolean assertion;
  it does not inspect a backup file. DSNs in fixtures must be generated and
  never copied from installed operator configuration.
- `ops/dedup_cortex.py:24` documents no argv, `--threshold 0.92`, or `--apply`;
  env selects database/data/config. Default threshold is 0.90 (`:42`).
- `docs/guide/memory-model.md:846` names writer retirement;
  `retire_by_writer.py:78` takes positional `writer_id`, optional
  `--session-id <session>`, `--apply`, `--database-url <DSN>`.
- `ops/backfill_edge_confidence.py:10` documents no args and `--apply`;
  it uses membership of that exact string in argv, not argparse (`:44`).
- `docs/guide/memory-model.md:843` names AGE cleanup. Its script accepts no
  parsed options and always executes the drop (`migrate_drop_age.py:19`).
- `docs/guide/memory-model.md:139` cites population measurement;
  `measure_reverify_population.py:88` accepts `--database-url <DSN>` and
  `--json`; JSON is indented, sorted keys, counts plus one-decimal percentages.

The population module calls the counts a consistent transaction snapshot in
its docstring, but the implementation sets only READ ONLY (`:63,70`), not
repeatable-read. A stable multi-statement snapshot under concurrent writes is
**unverified**. Do not silently strengthen that into a promised guarantee.

## 5. Python incidentals to defer

Argparse abbreviations, float parser edge cases, arbitrary unknown argv for
membership-based scripts and platform repr/tracebacks are not canonical
operator shapes (`rust/SEMANTICS-CHECKLIST.md:10`). Preserve dry-run vs apply,
exit classes, per-table order and refusals before expensive work. Do not invent
a default dry-run for AGE drop or a writer-lease refusal for direct retirement.
Python rounding affects the population text/JSON report (`:78`); retain its
observable result for canonical counts rather than treating all output as free.

## 6. Oracle tests

Behavioral pins (none run for this dossier):

- `tests/test_migrate_embeddings.py::test_dry_run_mutates_nothing`
- `tests/test_migrate_embeddings.py::test_apply_without_backup_verified_refuses`
- `tests/test_migrate_embeddings.py::test_apply_refuses_against_hung_daemon`
- `tests/test_migrate_embeddings.py::test_assume_daemon_stopped_bypasses_only_the_health_gate`
- `tests/test_migrate_embeddings.py::test_lock_timeout_fires_on_queued_alter`
- `tests/test_migrate_embeddings.py::test_apply_crash_after_two_tables_keeps_entries_armed_then_resumes`
- `tests/test_migrate_embeddings.py::test_apply_migrates_all_four_tables`
- `tests/test_dedup_cortex_script.py::test_dry_run_leaves_every_fact_row_untouched`
- `tests/test_dedup_cortex_script.py::test_refuses_while_another_writer_holds_the_bank`
- `tests/test_writer_keying.py::test_retire_by_writer_supersedes_only_that_writer`
- `tests/test_backfill_edge_confidence.py::test_recompute_rows`
- `tests/test_trace_invalidations_storage.py::test_the_population_script_separates_the_served_flag_from_the_bare_test`
- `tests/test_trace_invalidations_storage.py::test_the_population_script_cannot_write_to_the_bank_it_measures`

Implementation pins: `tests/test_migrate_embeddings.py::test_entries_is_last_in_the_migration_order`
checks a Python tuple, and `tests/test_dedup_cortex_script.py::test_only_apply_persists_and_never_by_full_rewrite`
records Python methods. Native process/state tests should retain the reason
for each, not copy monkeypatch internals. No dedicated AGE-drop test is named
at this pin (`rust/PARITY.md:301`); process-boundary coverage is a gap.

## 7. Proposed harness cases and mutants

1. On isolated equivalent banks, run every canonical dry-run command; compare
   exit/streams, row/catalog/sequence state. Permit only dedup's demonstrated
   startup bookkeeping, not a blanket no-write claim for all dry runs.
2. Migration: refuse unverified backup and reachable/hung health endpoint
   before model load; inject table-two failure, compare prior commits and
   entries mismatch, then restart and migrate only remaining tables.
3. Hold a disposable table lock, require actionable bounded refusal, then
   release it and run the next ordinary migration successfully. Observe a
   concurrent inserted row around ALTER; fetch must occur after its lock.
4. Retirement: seed two writers, two sessions and superseded rows; dry-run
   leaves all unchanged; apply updates only matching current rows with one
   captured clock and retains IDs/history.
5. Backfill: agent/current vs non-agent/superseded edges; verify threshold,
   NULL handling, deterministic report and idempotent second apply.
6. AGE: create an owned extension fixture where supported, compare dependent
   catalog removals and unchanged relational rows; absent extension succeeds.
   Population: zero/nonzero counts and a supersession before/after confirmation;
   attempted write must be refused by the read-only transaction.

Eight mutants: fetch rows before ALTER; migrate entries first; stamp version
after a partial failure; skip backup assertion; treat health timeout as absent;
dedup save through full snapshot; retire all writers; omit READ ONLY.

## 8. Dependencies and risks

Needs EMBEDDING/PG-HYDRATE and owned WRITE-CORTEX/GRAPH scorer contracts.
Migration is per-table resumable, not an all-or-nothing bank transaction;
catalog comparison must include constraints and version. Direct scripts do
not inherit PostgresStorage's single-writer authority. Population READ ONLY
does not guarantee one stable snapshot under default isolation. DSN fallbacks
are script-specific and cannot be replaced by a shared guessed resolution.
Live execution, backup approval and stopping another owner's daemon remain
user/owner gates; proposed proofs use only disposable resources.

## 9. Effort estimate

**Large.** Six separate operator boundaries combine DDL, real-model work,
partial-commit recovery, direct SQL, service startup and report semantics.
