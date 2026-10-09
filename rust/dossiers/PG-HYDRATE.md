# PG-HYDRATE

Source commit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`. All `path:line` anchors below refer to that commit. Source preparation only: no database, build, test or differential cell was run for this dossier.

## 1. What the row promises

Opening a bank must establish the exact Python catalog and a single writer before serving resident memory, and a restart must reload the stored records without losing history or changing their identities. Failed required hydration must refuse service and writes until recovery succeeds; a replacement connection must detect an intervening writer and discard stale resident state. The PARITY row says v53 (`rust/PARITY.md:267`), but this source implements v55 (`pseudolife_memory/storage/schema.py:20`); that ledger drift needs an explicit disposition before closure.

## 2. Entry points and storage call graph

There is no dedicated hydration HTTP route. Ordinary service operations reach `_ensure_init`; `/health` observes readiness and database liveness without requesting full model initialization (`pseudolife_memory/daemon.py:231`, `:586`). Route/tool admission belongs to its existing HTTP/MCP rows, rather than becoming part of a schema migration.

| Python entry | Role and next call |
|---|---|
| `pseudolife_memory/service.py:796` `MemoryService.__init__` | Retains DSN/config and initializes resident-state/readiness sentinels; no Postgres connection here. |
| `pseudolife_memory/service.py:1212` `_ensure_postgres_storage` | Cheap durable-only opening, cached connection, writer-refusal backoff and search-path invariant. |
| `pseudolife_memory/service.py:1047` `_assert_public_search_path` | `SHOW search_path`; refuse a missing/public-shadowed namespace and discard the failed connection. |
| `pseudolife_memory/service.py:1268` `_ensure_init` | Verify writer, reconcile handover/recovery, connect before loading model, then hydrate and reseed HLC. |
| `pseudolife_memory/service.py:1352` `_arm_retry_backoff`, `:1422` `_refuse_while_backing_off`, `:1435` `_abandon_partial_init` | Failed store builds drop all four resident stores and open bounded retries; retained embedder/storage avoid repeated allocations/connections. |
| `pseudolife_memory/service.py:1361` `_rehydrate_if_bank_changed_hands` | Drop resident stores, dirty-slot/recovery and meta-backed caches before rereading a changed bank. |
| `pseudolife_memory/service.py:1469` `_hydrate_resident_stores` | Construct CMS/cortex/world/lessons, invoke migration and sync loaders; optional/tolerant branches are enumerated below. |
| `pseudolife_memory/service.py:1142` `_refuse_on_stale_hydrated_dims` | Post-load dimension guard, including file-mode vectors. |
| `pseudolife_memory/service.py:1638` `_reseed_hlc` | Reseed from resident canonical stamps and durable coordination highwater; clocks remain ordered across restart. |
| `pseudolife_memory/storage/postgres.py:391` `PostgresStorage.__init__` | `_open_session` -> `ensure_schema` -> `register_vector` -> relation seeds -> writer epoch. Close session on any constructor failure. |
| `pseudolife_memory/storage/postgres.py:434` `_open_session`, `:603` `_connect` | Connect with autocommit, timeout and public search path; acquire lease on that same session. |
| `pseudolife_memory/storage/postgres.py:92` `connect_retrying_local_ports`, `:77` `_local_port_error_code`, `:369` `_application_name` | Bounded Windows local-port retry and credential-free application name. |
| `pseudolife_memory/storage/postgres.py:447` `_acquire_writer_lease`, `:482` `_bump_lease_epoch`, `:501` `_read_lease_epoch` | Session advisory lock and durable handover counter. |
| `pseudolife_memory/storage/postgres.py:510` `verify_writer_session`, `:548` `_probe_session`, `:551` `_pinned`, `:627` `conn` | Probe/reconnect, retain transaction-pinned sessions, re-register vector adapter, detect handover and fail closed. |
| `pseudolife_memory/storage/postgres.py:556` `acknowledge_rehydration`, `:562` `_lease_holders`, `:584` `_describe_lease_holder` | Owner acknowledgement plus holder discovery/refusal. |
| `pseudolife_memory/storage/postgres.py:702` `ping`, `:721` `cached_bank_id`, `:768` `close` | Dedicated health connections/cache and raw close without reconnect. |
| `pseudolife_memory/storage/schema.py:1145` `ensure_schema`, `:1091` `_refuse_on_embedding_dim_mismatch`, `:1072` `_backfill_trace_invalidations` | Atomic DDL and compatibility updates, with dimension sentinel before DDL. |
| `pseudolife_memory/storage/postgres.py:754` `_seed_relations` | Ordered builtin relation inserts, `ON CONFLICT DO NOTHING`. |
| `pseudolife_memory/storage/sync.py:82` `row_to_entry`, `:107` `hydrate_cms` | `load_entries` -> float32 entries -> append -> rebalance -> band stamp reconciliation; `load_episodes` -> episode manager. |
| `pseudolife_memory/storage/sync.py:43` `_stamp_from_row`, `:273` `hydrate_cortex`, `:372` `hydrate_world_cortex`, `:474` `hydrate_lessons` | Canonical records, temporal defaults, support/provenance, indexes and cursor/log meta. |
| `pseudolife_memory/storage/postgres.py:1424` `load_entries`, `:1669` `load_episodes`, `:1918` `load_facts`, `:1966` `load_world_facts`, `:1987` `load_lessons`, `:2711` `meta_get` | Ordered SELECTs and vector output conversion (`:230` `_embedding_out`). |
| `pseudolife_memory/storage/postgres.py:935` `update_entry`, `:2665` `meta_set`, `:775` `_txn`, `:798` `transaction` | Hydration stamp write-through and explicit/pinned mutation transaction primitives. |

The initialization branches into `pseudolife_memory/storage/migrate.py:109` `migrate_legacy`, CMS weights/file loaders and ReferenceBank (`pseudolife_memory/service.py:1479`, `:1515`, `:1546`); their compatibility contract is the STORAGE-COMPAT row. Dream-state initialization (`pseudolife_memory/service.py:3606`, storage `pseudolife_memory/storage/postgres.py:1459`) and pending correction/curation/lesson recovery are integration dependencies, not newly assigned dream/write/curation scope. Exact helper entry-line verification for those dependencies remains incomplete.

## 3. State touched, SQL and transaction boundaries

The schema authority is `pseudolife_memory/storage/schema.py:22` `SCHEMA_SQL`, composed with coordination (`:488`), principals (`:806`) and maintainer (`:832`) DDL; the additive migration tail starts at `:1191` and ends with the `schema_version` JSONB upsert at `:1656`. Preserve table/column/default/nullability/constraint/index/sequence definitions from those statements, including the v54/v55 guarded additions, rather than reconstructing a v53 approximation. The complete table-name inventory is `:895` `BENCH_RESET_TABLES`:

`meta`, `episodes`, `entries`, `entities`, `entity_aliases`, `relations`, `edges`, `edge_evidence`, `edge_proposals`, `entity_proposals`, `entity_kinds`, `dismissed_pairs`, `facts`, `world_facts`, `lessons`, `outcome_signals`, `lesson_search_events`, `communities`, `entity_communities`, `memory_traces`, `memory_trace_invalidations`, `entry_reinstatement_decisions`, `entity_sources`, `client_sessions`, `merge_decisions`, `dream_runs`, `dream_run_slots`, `chronicle_events`, `retrieval_events`, `retrieval_uses`, `slot_reads`, `curation_judgments`, `store_decisions`, `coordination_agents`, `coordination_messages`, `coordination_events`, `coordination_leases`, `coordination_lease_waiters`, `coordination_wakes`, `principals`, `maintainer_passkeys`, `maintainer_bootstrap`, `maintainer_nonces`.

DDL observes/creates all those tables. Record hydration directly reads `entries`, `episodes`, `facts`, `world_facts`, `lessons`, and `meta`; it does not load the whole catalog into resident stores. No exhaustive per-column transcription of the peripheral tables is supplied here: their exact definitions above plus a catalog capture are required before closure.

| State or SQL | Contract |
|---|---|
| `pseudolife_memory/storage/schema.py:39` entries and `pseudolife_memory/storage/postgres.py:104` `_ENTRY_COLS` | `id` BIGSERIAL, band/text, required `vector(1024)`, surprise REAL, ts DOUBLE PRECISION, counters/source, supersession, turn, episode/title, JSONB tags/slots, nullable authority/tolerance, dream state; loaders also read reinforcement counters. Entry IDs remain stable. |
| `pseudolife_memory/storage/schema.py:185`, `:245`, `:283`; `pseudolife_memory/storage/postgres.py:122`, `:144`, `:157` column tuples | Full canonical slot/history records and nullable vector(1024); facts include kind/value_norm/stance/labels and personal evergreen freshness, world has citations and volatile freshness, lessons carry about/outcome. Shared `:117` stamp tuple is tx/valid times, HLC physical/logical, writer/session and version. |
| `pseudolife_memory/storage/schema.py:28`; `pseudolife_memory/storage/postgres.py:1669` | Episode IDs/title/hint/times/closed-by-new-start/session-key/parent. Only `ORDER BY started_at`, with no added tie key; last open episode in that observed order becomes current (`pseudolife_memory/storage/sync.py:151`). |
| `pseudolife_memory/storage/schema.py:1091` | Read `public.entries.embedding` typmod through `pg_attribute`; positive mismatch against 1024 refuses before any DDL. Unconstrained typmod is a separate case, not automatically a mismatch. |
| `pseudolife_memory/storage/schema.py:1160` | One `conn.transaction()` contains all DDL, healing, backfill and schema meta update; `SET LOCAL` lock/statement timeouts do not leak. Vector extension is required. |
| `pseudolife_memory/storage/schema.py:1072`, `:1184` | Trace invalidation backfill runs only when its table was absent before creation. Repeated startup must not manufacture new invalidations. |
| `pseudolife_memory/storage/schema.py:1536` | Duplicate live canonical rows are healed by last_confirmed DESC/id DESC before unique indexes; facts/current healing is scalar-only. Preserve multiple current members and per-value member uniqueness. |
| `pseudolife_memory/storage/schema.py:360` | Add temporal columns to facts/world/lessons/edges and backfill missing tx_time/valid_time from asserted_at and writer_id to legacy; no unconditional history rewrite. |
| `pseudolife_memory/storage/schema.py:601`, BIGSERIAL declarations | Explicit `coordination_lease_fence` plus serial sequences, their bounds and positions are catalog/post-state observations; schema opening must not reset them. |
| `pseudolife_memory/storage/postgres.py:459`, `:482`, `:754` | Session keepalives; writer lock `pg_advisory_lock(hashtextextended('pseudolife-bank-writer',0))`; epoch upsert; transactional relation seeds in inverse-FK order (`:191`). These are separate commits from schema DDL; startup as a whole is not one transaction. |
| `pseudolife_memory/storage/postgres.py:1427`, `:1921`, `:1969`, `:1990` | Full records ordered by id; embedding values decoded to float32 and optional vectors preserve None. |
| `pseudolife_memory/storage/sync.py:112` | Band entry lists, dirty matrix/slot index, capacity seating, entry bank stamps, episode map/current ID. Stamp repair invokes `update_entry`, each in its own transaction; repair failure logs but does not fail boot. `pseudolife_memory/memory/cms.py:2022` `rebalance_bands` moves objects, writes band changes, and preserves even the deepest-band overflow without deleting entries. |
| `pseudolife_memory/storage/sync.py:273`, `:372`, `:474` | Replace resident canonical lists/indexes. Personal scalar healing marks dirty slots (`pseudolife_memory/memory/cortex.py:1810`); cursor/log use `cortex_dream_cursor` and `cortex_supersession_log`. |
| `pseudolife_memory/service.py:1309`, `:1383`, `:1638` | Active session pointer, tombstones, deferred empty roots, recovery records, saved fingerprint, HLC and epoch-associated readiness. Handover discards meta-backed cache values before reload, including a value another writer cleared. |
| `pseudolife_memory/storage/postgres.py:775`, `:798` | Explicit mutation transaction and savepoint nesting; unsuccessful COMMIT raises rather than claiming durability. A pinned multi-method transaction cannot reconnect midway. |

Required-store failure abandons CMS/cortex/world/lessons (`pseudolife_memory/service.py:1435`); autosave/exit must not flush partial resident state. Migration partiality, weights, optional reference bank, dream tracking and file-mode loaders are deliberately tolerant (`:1469`), with their own health/reset flags. Connection loss without a reachable competing writer may continue resident reads, while storage calls fail (`pseudolife_memory/storage/postgres.py:510`; behavioral test below); do not replace this with universal outage refusal.

## 4. Shipped producers

Own source census, pending `rust/producer-census.json`; no claim that prep-census has landed.

| Producer | Canonical shape |
|---|---|
| Daemon import (`pseudolife_memory/daemon.py:522`, `pseudolife_memory/mcp_server.py:109`) | `MemoryService(data_dir=os.environ.get('PSEUDOLIFE_MCP_DATA_DIR'), config_path=os.environ.get('PSEUDOLIFE_MCP_CONFIG'))`; DSN comes from service constructor's explicit argument or `PSEUDOLIFE_MCP_DATABASE_URL` (`pseudolife_memory/service.py:810`). |
| Ordinary initialized service operations (`pseudolife_memory/service.py:1268`; HTTP registration `pseudolife_memory/web/routes.py:109`, MCP wrappers `pseudolife_memory/mcp_server.py:282`) | No client sends hydration arguments: the first ordinary operation triggers configured startup, later calls verify writer continuity. Full route-by-route producer census is shared with the route dossiers and remains incomplete here. |
| Internal startup (`pseudolife_memory/service.py:1235`, `:1541`, `:1573`, `:1594`, `:1607`) | `PostgresStorage(self._db_url)` with default writer lease on; each `hydrate_*(resident, self._storage)`. `writer_lease=False` is a deliberate secondary probe, not daemon startup. |
| CMS/canonical persistence (`pseudolife_memory/storage/sync.py:57`, `:170`, `:320`, `:409`) | Entry row dictionary uses `_ENTRY_COLS`; canonical row dictionaries use their declared tuple plus stamp fields. JSONB arrays, None and exact IDs must survive reload; these serializers define the in-process producer shapes. |
| `/health` (`pseudolife_memory/daemon.py:231`; `pseudolife_memory/storage/postgres.py:702`, `:721`) | Cheap dedicated liveness/bank-ID probes; no full hydration request and no DDL on the bank-ID read. |

## 5. Python incidentals to defer

Apply `rust/SEMANTICS-CHECKLIST.md:1` canonical-shape rules. Defer arbitrary objects accepted by duck-typed embedding conversion, exact numpy/torch object classes, psycopg exception class spelling and log wording where no consumer reads it. Preserve vector values/dimensions, refusal category/readiness, default/null distinctions, ordering, rollback and writer ownership; none of those is a Python incidental. Fake storage objects missing `update_entry` are supplemental unit producers, not authority to omit real stamp write-through (`pseudolife_memory/storage/sync.py:131`). Embedded pg0 startup remains the named embedded-Postgres deferral; documentation does not authorize its port.

## 6. Oracle tests

Behavioral pins (source read/inventory verified; not executed):

- `tests/test_pg_storage.py::test_entry_crud_roundtrip`, `::test_fact_roundtrip`, `::test_vector_column_roundtrip`, `::test_reads_leave_connection_idle_not_in_transaction`, `::test_ddl_timeouts_do_not_leak_into_session`, `::test_failed_mutation_does_not_poison_connection`, `::test_txn_rolls_back_compound_cursor_shape`.
- `tests/test_schema_ddl_shape.py::test_table_declares_its_required_columns`, `::test_columns_declare_their_types`, `::test_entries_dream_state_defaults_to_pending_under_its_named_check`, `::test_a_v53_bank_gains_the_v54_message_columns_through_the_guarded_pass`.
- `tests/test_schema_healing.py::test_ensure_schema_healing_is_kind_aware`, `::test_member_current_uniqueness_allows_multiple_values_same_slot`.
- `tests/test_fail_closed_hydration.py::test_failed_hydration_refuses_writes_and_keeps_durable_history`, `::test_not_ready_is_reported_on_health_until_a_retry_succeeds`, `::test_backoff_grows_between_consecutive_failures_and_resets_on_success`.
- `tests/test_hydrate_capacity.py::test_hydrate_of_an_unknown_band_name_also_rebalances`, `::test_hydrate_loses_nothing_and_deletes_nothing`, `::test_an_overfull_bank_is_not_silently_truncated_at_startup`.
- `tests/test_storage_connect.py::test_connect_is_reused_not_rebuilt`, `::test_failed_search_path_invariant_leaves_no_reusable_connection`.
- `tests/test_writer_lease.py::test_second_storage_on_same_database_refuses_naming_the_holder`, `::test_failed_open_releases_the_lease_for_its_own_retry`, `::test_reconnect_fails_closed_when_another_writer_took_the_lease`, `::test_a_writer_that_lost_the_bank_rehydrates_before_it_serves_or_writes`, `::test_reconnecting_with_no_other_writer_keeps_the_resident_bank`, `::test_an_unreachable_bank_still_serves_reads_from_memory`, `::test_a_reread_after_a_handover_forgets_what_the_other_writer_cleared`.
- `tests/test_disposable_database_guard.py::test_assert_disposable_refuses_the_default_bank`, `::test_an_unresolved_record_fails_every_check_closed`: harness admission safeguards, not runtime hydration itself.

Implementation/inventory pins: `tests/test_schema_version.py::test_schema_meta_version_is_pinned` checks a literal; `tests/test_schema_ddl_shape.py::test_facts_table_declares_freshness_class_defaulting_to_evergreen` inspects source DDL; `::test_schema_import_does_not_load_database_driver` checks import discipline. `tests/test_pgvector_compat.py::test_embedding_out_handles_vector_objects` pins dependency-wrapper compatibility; the native contract is value/type conversion, not Python wrapper identity.

## 7. Proposed harness and mutants

Extend `rust/daemon/harness/run.py:1` and its catalog/row observer `rust/daemon/harness/dbstate.py:23`, `:126`; use disposable banks and the existing daemon launcher. Do not use the older shape-oriented `compare.py` as whole-row acceptance. Existing `rust/daemon/harness/run.py:50` exclusions for dream/curation/telemetry remain named integration gaps; close or explicitly retain each when validating this row.

1. Open identical empty banks, capture whole catalog/rows/sequences after opening and each restart; compare extension, vector typmods, defaults/nulls, unique/partial indexes, FK presence/absence, relation seeds and epoch increments. Add v53 and early-v54 upgrade fixtures and duplicate scalar/member data seeded through approved fixture paths.
2. Seed entries/episodes/canonical scalar and member histories through Python producer writes; restart with flat and custom capacity/preset changes. Compare ID/order/null/vector values and band write-through immediately, then again on second hydration; deepest-band overflow must remain intact.
3. Inject one loader failure at a time after entries/cortex/world/lessons starts; call a read, write, save and health. Verify refusal, unchanged durable histories, all resident stores abandoned, retained model, then successful retry with the same connection when valid.
4. With two owned sessions, close writer A, acquire/write/close B, then operate A; assert lease refusal while B lives and rehydration before A serves/saves after handover. Repeat without B and with an unreachable server; compare their different resident-read behavior.
5. A wrong constrained vector column must refuse before any schema/seed/epoch mutation. An unconstrained vector and a missing table are separate controls. Force constructor failure and verify lease is released before retry.

Six source mutants, each expected to make a named case fail:

| Mutant | Rejecting case |
|---|---|
| Omit writer advisory lock | Second live writer in case 4 succeeds. |
| Skip lease epoch read or stale-store invalidation | Case 4 serves A's old slot/meta values. |
| Catch required loader errors and retain an empty store | Case 3 accepts writes or destroys stored history. |
| Apply duplicate healing to member rows | Case 1/2 loses current set members. |
| Skip stale band stamp persistence | Case 2 DB/API bank names differ or second boot repeats the repair. |
| Move dimension check after DDL / omit an additive column | Case 5 changes refused-bank state / case 1 catalog differs. |

No cases or mutants have been implemented or run by this documentation task.

## 8. Dependencies and risks

CONFIG-LOAD chooses the preset/paths/backend; STORAGE-COMPAT owns legacy migration and tolerant file branches; EMBEDDING/ONNX-PREREQUISITE owns model artifacts/dimension readiness; write, dream, graph and coordination rows supply recovery/HLC integrations. Existing W1-A foundation already contains schema-opening harness work; coordinate a remaining row boundary rather than duplicate it (`rust/daemon/harness/run.py:50`).

Single synchronous writer session plus service lock is the Python concurrency model (`pseudolife_memory/storage/postgres.py:1`); the native async equivalent must preserve pinned transaction ownership and session lock lifetime. Reconnect is not a schema rerun, timeouts are scoped, row ordering is observable, and per-entry hydration repairs are not a single global snapshot. Catalog extension versions/collation/owner differences need explicitly recorded fixture policy. Full enumeration of recovery helpers and all startup meta mutations is incomplete here; review those paths before treating this as a closed row.

## 9. Effort estimate

Large: most primitive SQL/schema opening already has foundation support, but catalog upgrades, failed partial initialization and cross-session handover require durable post-state and lifecycle proof across several subsystem boundaries.
