# STORAGE-COMPAT

Source commit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`. All repository-relative line anchors refer to that commit. Producer attribution: dossier author's source census, pending `rust/producer-census.json`. One original Python retention test and a pure chunk-unit oracle check were run for review corrections; no model, database, build or Rust harness was executed for this dossier.

## 1. What the row promises

Existing file checkpoints, their recovery/import paths and document inputs must retain the behavior shipped Python producers rely on, including partial import recovery and safe loading of tensor state. The row also names embedded PostgreSQL and Chroma-backed documents, but both remain explicit deferrals under the standing port rulings; describing their contracts does not authorize porting them. PostgreSQL resident hydration/catalog opening is the separate [PG-HYDRATE.md](PG-HYDRATE.md) boundary (`rust/PARITY.md:284`).

## 2. Entry points and storage call graph

| Entry/source | Role |
|---|---|
| `pseudolife_memory/storage/embedded_pg.py:65` `available`, `:74` `default_lite_data_dir`, `:99` `resolve_daemon_storage` | Select explicit DSN, explicit files or optional lite backend; stable per-user path. Named deferral. |
| `pseudolife_memory/storage/embedded_pg.py:128` `attach_or_start`, `:182` `start_embedded`, `:195` `stop_embedded` | Start/attach and retain only owned process handles. Named deferral. |
| `pseudolife_memory/storage/embedded_pg.py:206` `_start_lock`, `:261` `_preflight_path`, `:284` `_guard_pg_major`, `:309` `_instance_name` | Serialize first boot, platform/path and PG-major refusal, path-scoped registry identity. Named deferral. |
| `pseudolife_memory/daemon.py:472` `run_daemon`, `:504` storage-resolution call | Resolve storage before importing MCP service. |
| `pseudolife_memory/service.py:1469` `_hydrate_resident_stores` | Legacy migration -> required PG loaders -> weights cache; file-mode CMS/cortex loads are tolerant branches. |
| `pseudolife_memory/storage/migrate.py:109` `migrate_legacy`, `:64` `_fingerprint`, `:84` `_already_imported` | Resumable CMS/cortex .pt import, fingerprint and legacy multiset identity. Local helpers `_cursor_state` `:223`, `_source_state` `:300`, `_checkpoint` `:346` implement source-order progress and classification. |
| `pseudolife_memory/memory/cms.py:2239` `save`, `:2293` `save_weights`, `:2315` `load_weights`, `:2346` `load` | File checkpoint and counter-only cache producer/consumer; CMS file schema is 7 (`:155`), independent of PG schema 55. |
| `pseudolife_memory/memory/cms.py:2422` `_load_schema_v1`, `:2464` `_load_schema_v2`, `:2549` `_load_legacy_hopfield` | Old layouts -> band loaders -> `rebalance_bands` `:2022`; legacy Hopfield field audit incomplete. |
| `pseudolife_memory/memory/miras/band.py:220` `get_state_dict`, `:249` `load_state_dict` | Exact entry persistence; deliberately ignore old MLP/optimizer blocks. |
| `pseudolife_memory/memory/cortex.py:1688` `save`, `:1745` `load`, `:1797` `_reindex_current` | Canonical file records/cursor/log; default older fields and rebuild scalar/member indexes. |
| `pseudolife_memory/utils/atomic_io.py:27` `atomic_torch_save`, `:23` `_bak`, `:43` `_load_one`, `:54` `load_with_backup`, `:19` `WeightsCorrupt` | Tmp/replace/backup rotation, safe tensor loading and corrupt primary/backup result. |
| `ops/restore_from_pt.py:67` `main`, `:50` `_dim_mismatches` | Deliberate additive same-era restore; preflight both snapshots before content writes; does not re-embed. |
| `pseudolife_memory/mcp_server.py:2967` `document_ingest`, `:2987` `document_search` | MCP paths `{path,source?}` and `{query,top_k?}` into service. Named Chroma deferral. |
| `pseudolife_memory/service.py:3481` `ingest_document`, `:3518` `search_documents` | Service lock/init, server-side file path, embed and reference bank. No direct document Console route was found in `pseudolife_memory/web/routes.py:109` registration; REST producer inventory is not claimed complete. |
| `pseudolife_memory/memory/reference_bank.py:98` constructor, `:116` `_store_exists`, `:122` `_open`, `:158` `size` | Optional/lazy Chroma collection, failure cached and surfaced differently for reads/writes. Named deferral. |
| `pseudolife_memory/memory/reference_bank.py:162` `ingest_text`, `:227` `ingest_file`, `:242` `retrieve`, `:290` `list_documents`, `:313` `clear`, `:321` `stats` | Document storage/query operations. Named deferral. |
| `pseudolife_memory/memory/reference_bank.py:37` `cosine_similarity_from_distance`, `:46` `_chunk_text`, `:60` `_read_file` | Similarity conversion, deterministic character chunks, parser dispatch. |
| `pseudolife_memory/memory/document_parser.py:12` `extract_text`, `:42` `_extract_pdf`, `:75` `_extract_html` | UTF-8 replacement text; PDFium then pypdf; HTML element removal then text or import-time regex fallback. |

Import call graph to durable SQL: `migrate_legacy` -> `PostgresStorage.load_entries/load_facts` (`pseudolife_memory/storage/postgres.py:1424`, `:1918`) -> episodes `upsert_episode` (`:1648`) -> entries `insert_entry` (`:916`) inside `entry_import_transaction` (`:885`) with `meta_set` (`:2665`) -> facts `replace_facts/replace_slot_facts` (`:1866`, `:1857`) -> `advance_dream_cursor` (`:2675`). Serialization uses `pseudolife_memory/storage/sync.py:170` `_record_to_row`. Restore uses constructor/catalog opening -> `upsert_episode/insert_entry/upsert_fact` (`pseudolife_memory/storage/postgres.py:1777`). File loaders have no direct SQL; service's later persistence is a separate path.

## 3. State touched, SQL and transactions

| State | Source and contract |
|---|---|
| `data_dir/memory_state/cms_state.pt`, `data_dir/cortex_state.pt` | Migration paths `pseudolife_memory/storage/migrate.py:135`; originals become `.pre-v8.bak`, never deleted (`:479`). Fingerprint contains per-source byte size and SHA256 (`:64`). |
| CMS checkpoint | `pseudolife_memory/memory/cms.py:2262` dictionary: `schema_version`, `preset_name`, `bands`, `interaction_count`, `logical_turn_count`, `surprise_history`, `consolidation_events`, `tier_hits`, `tier_queries`, `episodes`, `dream_ack_secret`, `dream_display_cursor`. `bands` maps names to `{entries:[...]}`. |
| Entry dictionary | `pseudolife_memory/memory/miras/band.py:224`: text, CPU tensor embedding, surprise_score, timestamp, access_count, source, superseded_at/by_text, last_logical_turn, slots, episode_id/title, tags, authority, distortion_tolerance, dream_state, dream_id. No MLP weights in this producer. |
| Legacy CMS layouts | `pseudolife_memory/memory/cms.py:2346`: absent schema means v1 `instant/short_term/long_term`; v2–7 use bands. Pre-v7 entries get a dream ID; unknown schema refuses loading and preserves fresh resident state. For supported band layouts, unknown saved bands route their entries into the first configured band (`:2479`), then load rebalances (`:2420`); PG hydration uses the same retention fallback. Individual unrestorable entries can still be skipped (`:2517`). |
| `memory_state/weights.pt` | `pseudolife_memory/memory/cms.py:2293`: `{schema_version,kind:'weights',preset_name,interaction_count,logical_turn_count,surprise_history,consolidation_events,tier_hits,tier_queries}`. Despite historical method/doc names, current producer stores counters and no entries or MLP weights. Missing cache returns false; both corrupt sets `weights_reset`; backup recovery returns true. |
| Cortex snapshot | `pseudolife_memory/memory/cortex.py:1688`: version/config margins/cursor/log plus records containing claim/value/polarity/confidence/status/kind, provenance/support, assertion/confirmation/supersession, embedding/slot_embedding, temporal/HLC/writer/session/version/freshness, stance/labels. Preserve current member rows, not scalar-heal them. This save uses `torch.save` directly; do not attribute atomic CMS backup guarantees to it. |
| Atomic checkpoint files | `pseudolife_memory/utils/atomic_io.py:27`: write `.tmp`, rotate old primary to `.bak`, replace tmp as primary. No fsync appears in this implementation; do not claim power-loss durability beyond the observed rename behavior. `_load_one` uses CPU, `weights_only=True` only. |
| `meta.legacy_migration` | `pseudolife_memory/storage/migrate.py:228`, `:326`, `:346`, `:410`, `:489`: status, stage, source fingerprint, entries_done, optional entry_cursor, started/updated/finished times. New imports bind progress to a source prefix, not `(text,ts)` dedup; legacy interrupted imports without cursor use a Counter preserving duplicate multiplicity. |
| Entries/episodes/facts + sequences | `pseudolife_memory/storage/migrate.py:355`, `:389`, `:438`; SQL `pseudolife_memory/storage/postgres.py:916`, `:1648`, `:1777`, `:1835`, `:1866`. Episodes are upserted before entries; current DDL has no entries->episode FK (`pseudolife_memory/storage/schema.py:50`). Imported embeddings of wrong dimension are re-encoded through the passed live embedder; claim text is `f'{entity} {attribute} {value}'.strip()` (`pseudolife_memory/storage/migrate.py:437`). |
| Import transaction boundary | `pseudolife_memory/storage/migrate.py:406`, `pseudolife_memory/storage/postgres.py:885`: each new entry and its cursor update commit together on a pinned connection. Entire import is not atomic: episode upserts, entries, facts/meta operations and renames are separate. Progress is in_progress before content; done is recorded only after renames. Fresh import replaces empty facts; resume fills only unoccupied normalized slots and preserves live supersession log (`pseudolife_memory/storage/migrate.py:447`). |
| Dream state/meta | `pseudolife_memory/storage/migrate.py:266`: validate v7 checkpoint authority and finite display cursor before content writes; state classification uses eligible/excluded sources and states pending/acknowledged/legacy-covered; target signing secret is not copied. Cursor advances monotonically (`pseudolife_memory/storage/postgres.py:2675`), never regresses on resume. |
| Restore | `ops/restore_from_pt.py:88`: constructor has schema/seed/epoch effects before snapshot mismatch check. A mismatch writes no imported entry/episode/fact content and exits 1; it does not mean the constructor had no effects. Additive restore does not provide one atomic whole-run rollback or source-order resume. |
| Embedded backend | `pseudolife_memory/storage/embedded_pg.py:128`: `<data_dir>/embedded_pg`, PG_VERSION major check, start lock and registry name, `_owned` instance handles. Explicit DSN wins; files bypasses pg0; only started instances are stopped. Named deferral. |
| Reference bank | `pseudolife_memory/memory/reference_bank.py:104`, `:135`, `:212`, `:253`: persistent directory/collection configured for cosine, `_client/_collection/_open_error/_open_lock`; `upsert(ids,embeddings,documents,metadatas)` and query. Chunk ID is MD5 UTF-8 `source:index:first100chars`; metadata is source/chunk_index/timestamp. No PostgreSQL document table exists at this source. Named deferral. |

Chroma ingest encodes slices of at most 8 (`pseudolife_memory/memory/reference_bank.py:34`, `:189`), retaining chunk/vector correspondence before upsert; no SQL transaction guarantee applies to that backend. Chunking sets `char_size=chunk_size*4`, `char_overlap=chunk_overlap*4`, then `step=max(char_size-char_overlap,1)` in characters; defaults advance 1792 characters. Slices use `char_size`, are stripped and omit empty text (`:46`). Chroma score is max(0,1-distance), query count is min(k,collection count), optional bank reads return empty while ingest refuses a failed open (`:122`, `:242`). Backend index/tie behavior is not established by wrapper inspection.

## 4. Shipped producers

Own source census; arbitrary model-selected tool arguments remain an open corpus until reconciled with prep-census.

| Producer | Exact shape |
|---|---|
| Service startup `pseudolife_memory/service.py:1515` | `migrate_legacy(self.data_dir,self._storage,self._embedder,eligible_sources=self.config.memory.dream.eligible_sources,exclude_sources=self.config.memory.dream.exclude_sources)`; no caller-supplied arbitrary pickle objects. |
| CMS save/cache `pseudolife_memory/memory/cms.py:2239`, `:2293` | Tensor/plain-container dictionaries listed in section 3; nested band entries from the actual band serializer. Runtime save calls `pseudolife_memory/service.py:3541`, flush mode selection near `:3786`. Full flush helper-line audit is incomplete. |
| File importer `pseudolife_memory/storage/migrate.py:237` | `torch.load(str(cms_path),map_location='cpu',weights_only=True)`; cortex uses `CortexStore.load(cortex_path)`. |
| Operator restore `ops/restore_from_pt.py:67` | `python ops/restore_from_pt.py --dsn <disposable-dsn> --data-dir <dir>` with optional `--cms <snapshot> --cortex <snapshot>`; defaults select `.pre-v8.bak` paths. Existing script is additive and same-era; not a second automatic migration path. |
| MCP `pseudolife_memory/mcp_server.py:2967`, `:2987`; user guide `docs/guide/memory-model.md:762` | `document_ingest({path:<server-visible path>,source:<string|null>})`, omitted source defaults None; `document_search({query:<string>,top_k:<positive integer>})`, omitted top_k defaults 5. Tool schema is an exposed producer boundary, not a fixed corpus of file formats or query strings. Named deferral. |
| Reference wrapper `pseudolife_memory/memory/reference_bank.py:227` | `ingest_text(extracted_text, source or file_path.name, embedder)`; chunk overrides absent. CMS/search supplies query tensor and top_k to retrieve; document service uses `encode_query(query)` (`pseudolife_memory/service.py:3528`). Named deferral. |
| Daemon backend chooser `pseudolife_memory/daemon.py:504` | `resolve_daemon_storage(os.environ)`; nonempty `PSEUDOLIFE_MCP_DATABASE_URL` wins, `PSEUDOLIFE_MCP_STORAGE=files` opts out, auto with pg0 installed starts embedded backend. Named deferral. |

No Console document upload producer was established by this census. Backup archives must retain reference state independently of PG dump at this source (`ops/backup.sh:260`, `ops/backup.ps1:259`); backup implementation remains the CLI owner's scope.

## 5. Python incidentals to defer

Per `rust/SEMANTICS-CHECKLIST.md:1`, defer arbitrary argparse abbreviations/negative-number ambiguities for restore, pathlib inputs no producer sends, arbitrary Python objects/full-pickle extensions, and exact third-party exception/log wording. The old Cortex loader's TypeError compatibility retry without `weights_only` (`pseudolife_memory/memory/cortex.py:1752`) is a separately named dependency compatibility corner, not authorization to add unsafe native deserialization. Historical MLP/optimizer file blocks are explicitly ignored by the current band loader; do not rebuild removed machinery to satisfy an old checkpoint label.

Keep named deferrals **embedded PostgreSQL pg0 lite lifecycle** and **Chroma-backed document/reference pool** until their separately approved implementation boundary changes. Parser fallback/provider output, file schemas, partial-progress identity, duplicate multiplicity, refusal before imported content and backup rotation remain observable requirements wherever the accepted producer reaches them.

## 6. Oracle tests

Behavioral retention pin: `tests/test_flat_migration.py::TestStateRestoreFallback::test_v2_restore_routes_unknown_bands_into_first_band` checks the supported saved-band fallback. This original Python node was rerun for the documentation correction; it is not Rust parity evidence.

Behavioral:

- `tests/test_cms_pt_schema.py::test_save_load_preserves_episode_and_tag_fields`, `::test_pre_v6_save_loads_with_defaults`, `::test_save_load_round_trips_episode_manager_state`, `::test_save_load_preserves_superseded_by_text`.
- `tests/test_cms_legacy_load.py::test_band_load_ignores_legacy_weight_block`, `::test_cms_load_tolerates_legacy_state`.
- `tests/test_migration.py::test_migration_roundtrip`, `::test_interrupted_migration_records_progress_and_resumes`, `::test_interrupted_entries_loop_resumes_without_duplicates`, `::test_real_bank_without_migration_state_is_left_alone`, `::test_partial_migration_refuses_a_different_legacy_source`, `::test_resume_survives_a_half_completed_rename`, `::test_resume_after_hydrate_rewrote_band_stamps_does_not_duplicate`, `::test_resume_merges_facts_instead_of_replacing_live_writes`, `::test_resume_refuses_when_a_recorded_source_vanished`.
- `tests/test_restore_from_pt.py::test_restore_from_pt_loads_cms_snapshot_with_weights_only`, `::test_refuses_on_entry_dim_mismatch_and_writes_nothing`, `::test_refuses_on_fact_dim_mismatch_and_writes_nothing`.
- `tests/test_atomic_weights.py::test_atomic_save_rotates_backup`, `::test_backup_recovery_on_corrupt_primary`, `::test_both_corrupt_raises`, `::test_cms_weights_roundtrip_counters_only`, `::test_cms_weights_corrupt_sets_flag`.
- `tests/test_document_parser.py::test_extract_text_plain`, `::test_extract_pdf`, `::test_extract_pdf_pypdf_fallback`; parser provider parity remains conditional on installed provider/fixture bytes.
- Deferred Chroma: `tests/test_reference_bank.py::test_chroma_distance_to_similarity`, `::test_ingest_opens_the_store_and_a_restarted_bank_reads_it`, `::test_a_store_that_cannot_open_reads_empty_and_refuses_ingest`; `tests/test_document_ingest_batch_bound.py::test_slicing_keeps_every_chunk_paired_with_its_own_vector`.
- Deferred embedded: `tests/test_embedded_pg.py::test_explicit_dsn_wins`, `::test_start_embedded_attaches_without_taking_ownership`, `::test_start_embedded_owns_and_stops_what_it_started`, `::test_start_lock_is_mutually_exclusive`, `::test_pg_major_mismatch_is_refused`.

Implementation/inventory pins: `tests/test_cms_pt_schema.py::test_pt_schema_version_constant_is_v7` pins a Python literal; `tests/test_document_ingest_batch_bound.py::test_the_ingest_bound_stays_small` pins the constant. The bounded batch/vector pairing behavior matters, not the function name or tensor class. The safe-load test records torch kwargs; a native format bridge needs its own observable refusal control. New dream-ack/file-checkpoint and cursor-prefix oracle inventory beyond the named migration tests is incomplete; do not infer complete schema-7 coverage.

## 7. Proposed harness cases and mutants

Use existing daemon/CLI harness isolation and DB observer (`rust/daemon/harness/run.py:1`, `rust/daemon/harness/dbstate.py:126`). A file compatibility cell must drive actual service load/save and compare semantic tensor/plain-container contents, file existence/rotation, full PG rows/catalog/sequences after each import write/checkpoint, and refusal/health values. Raw `.pt` bytes need an explicit format/comparator decision; zip serialization incidental differences must not hide changed tensors, identity or metadata. No Python host storage bridge is implied by this proposal.

1. Produce current schema-7 files with episodes, scalar/member history, labels and pending/acknowledged entries through Python; load/save/restart with native and compare all retained fields. Add actual v1/v2/pre-v6 golden producers and current unknown-version refusal.
2. Import matching-dimensional files, then a legacy-dimensional source with a deterministic fake embedder; assert re-embed call text and vectors for entries/facts, episode identity, cursor and source preservation. Restore the same mismatched source separately and require refusal before imported content.
3. Interrupt after entry+cursor commit, then add a live fact and restart with exactly the same source; compare full duplicate multiplicity, source prefix and untouched live slot/history. Repeat after band stamp repair, a half rename and a vanished/replaced source.
4. Interrupt each atomic save rename boundary; corrupt primary/backup independently; assert old-or-new complete data, backup selection/reset flag, unchanged resident entries in weights-only load. Cortex direct-save limitations remain visible.
5. Supplemental parser/chunk cell: synthetic UTF-8 replacement/plain/HTML/PDF fixtures; size/overlap boundaries, whitespace-only tail and astral characters; record provider. Pin character units with 2048 non-whitespace characters at default size512/overlap64: exactly two chunks of lengths 2048 and 256, with the second starting at offset1792 (`pseudolife_memory/memory/reference_bank.py:46`). Defer backend Chroma ranking and pg0 lifecycle cells by name.

6. Renamed-layout case: save a custom two-band checkpoint containing one entry in each band, restore under the flat preset, and assert both texts and all saved fields survive in the first band before/after rebalance (`tests/test_flat_migration.py:148`).

Eight mutants:

| Mutant | Rejecting observation |
|---|---|
| Mark migration done before renaming | Interrupted case 3 falsely completes with originals still present. |
| Commit entry and cursor separately | Case 3 duplicates/skips source prefix after interruption. |
| Use a set or include mutable band in old resume identity | Case 3 loses duplicates or duplicates a reconciled prefix. |
| Replace all facts on resume | Case 3 destroys a live intervening slot. |
| Read cache in place / omit backup fallback | Case 4 loses recoverable checkpoint or resets incorrectly. |
| Omit kind/labels/dream fields from file serializer | Case 1 loses members or checkpoint identity on restart. |
| Skip unknown saved band names instead of routing entries | Renamed-layout case 6 loses the old second band's entry. |
| Compute stride in token units without the character conversion | Case 5 produces extra chunks or wrong offsets. |

These are proposals only; none was implemented or run.

## 8. Dependencies and risks

PG-HYDRATE supplies catalog/single-writer/SQL primitives; CONFIG-LOAD determines paths/preset; EMBEDDING supplies genuine dimension conversion; W3-H owns dream authority/cursor behavior; CLI backup/transfer owns archive/export boundaries. Native .pt compatibility needs a reviewed format/transition decision and safe tensor decoder before implementation; no simplification is pre-authorized (`rust/PARITY.md:284`).

File operations and PG commits cannot be one atomic transaction, hence fingerprint/progress/rename recovery. Platform locks/path normalization and same-directory replacement require Windows proof. Legacy import partiality is deliberately nonfatal, unlike required PG hydration failure. Startup rebalance preserves every entry including overflow (`pseudolife_memory/memory/cms.py:2022`). Chroma ranking/index tie order, complete old Hopfield layout, parser fallback provider outputs and new dream checkpoint test inventory are explicit unresolved coverage gaps, not guessed contracts.

## 9. Effort estimate

Large for the whole row: legacy durable progress plus tensor files and two backend lifecycle boundaries need separate proofs; the authorized non-deferred slice is smaller only after an explicit native file-format decision.
