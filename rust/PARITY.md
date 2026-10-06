# Behaviour parity register

Current version-branch oracle target: Python 0.17.0 at `3c01bb31abd60178e15dea99adda369b4bbf92fc`, schema 55. Published `95d5402d` passed all 14 checks. Frozen `5220b5ee` now has both-OS named-console and warm-protocol CLI proofs; successor hosted checks and independent review remain pending, so version stays deferred.
Historical phase 1 close-out oracle: Python 0.16.1 at `f709abb54f7912ae9cd767998d0926ca33df4bcd`, schema 54.
Historical phase 1 receipts retain Python 0.16.0 at `0b015f9279a778f996e71ee78510695e5fee7196`, schema 53.
Historical phase 0b receipts retain Python 0.15.0 at `3691f5cb75487d3fda54a6bde6fab35dcf32c681`.
Recount at this pin: 38 MCP tools, 72 ConsoleRoutes registrations plus the
separate POST /api/pair route, eight hook endpoints, 15 coordination actions
and 28 CLI modes. The original 26-mode checklist remains the phase 2 scope;
`maintainer` and `test-login` were added upstream and stay deferred outside Tier A. The current Console is v3 at /ui/; /ui/next/ is absent.
Phase 1 implementation acceptance at `8b7a6c95` is recorded in
`evals/results/rust-phase1-closeout-8b7a6c95.json`. Phase 1 #560 merged at
`465d75d9` after final `a3e95642` suites, review and all 14 checks passed.
Its eight-route acceptance does not establish version-branch acceptance. `ported` requires the brief's
wire, differential and measurement gates; `deferred` is unfinished;
`retired-by-decision` requires a recorded maintainer decision. The SDK preflight
retirement below names the maintainer's 2026-10-05 decision.

Existing files under `tests/`, including `tests/conftest.py`, are immutable. On 2026-10-03 the maintainer selected an
external pytest plugin instead of modifying `tests/conftest.py`. The test pools
below are an inventory, not a claim that whole files can target Rust. The
executable manifest must enumerate actual selected nodes, fail closed for
unsupported mappings, and record Python-only cases explicitly.

Source paths naming a Python module without a root refer to `pseudolife_memory/`;
test names without a root refer to `tests/`. Registrations below are a source
inventory and still require runtime schema/transcript evidence.

## Parity rows

BASE and RULES record the completed phase 0 instruments and measurements; historical evidence from PR #540 retains its original pin. Phase 0b evidence review and all ten CI checks passed at 9a62ed02, as recorded in PORT-STATE.md; subsequent PR updates require fresh review and current-merge-ref CI. Broader production behavior rows remain deferred; named retirements and scoped substitutions are explicit. The SDK preflight retirement is authorized by the 2026-10-05 phase 2 decision; other unfinished behavior remains deferred. `P` means real process/wire eligible nodes; `I` means Python internals requiring additive wire cases or Rust unit equivalents; `A` means artifact/static contract. Paths below are repository-relative.

| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| BASE | 0 | Commit/host/bank/run-count RSS idle and workload, shim cold start, search/store/fact-set and send/receive p50/p95; CI lane timing. `evals/`, `.github/workflows/ci.yml` | `tests/test_eval_evidence.py`, `test_report_redaction.py` A; current-pin Linux scaling in `evals/results/rust-phase0b-daemon-scaling-linux.json`, fixed-reference CI noise in `evals/results/rust-phase0b-ci-same-head.json`; historical #540 shim/write evidence retains its original pin | ported |
| RULES | 0 | Error/type/ownership/unsafe/HTTP/SQL/float mappings; immutable manifest and trial corrections. `CLAUDE.md`, guides, atlas | `test_release_ux.py`, `test_atlas_currency.py`, `test_llms_txt.py`, `test_eval_evidence.py`, `test_i18n_readme.py` A; PORTING.md and `evals/results/rust-port-phase0b-acceptance.json`; current code reviewed after wire-span and candidate-release fixes | ported |
| MCP-WIRE | 1 | initialize instructions/capabilities, tool annotations/schema/help, supported revision negotiation, JSON-RPC errors, streams, cancellation, notifications. `mcp_server.py`, `shim.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py` mixed P/I; `test_mcp_server.py`, `test_mcp_string_arguments.py`, `test_mcp_client_neutrality.py`, `test_mcp_stdio_errlog.py` I/A; help fixture A | ported |
| MCP-TIER | 1/3 | Phase 1 client forwarding/visibility/list_changed boundary accepted; Phase 3 daemon implementation remains deferred: principal-scoped list filtering, 12h TTL/precedence, cumulative 9/24/38 visibility, hidden calls accepted, list_changed. `toolset_tiers.py`, `mcp_server.py` | `test_shim.py` notification nodes P; `test_toolset_tiers.py`, `test_mcp_server.py` I; add 38-tool differential schema/argument/error corpus | ported |
| SHIM-LIFECYCLE | 1/5 | Canonical origin validation, discovery/probe, explicit local spawn/reuse, truthful no-spawn waiting, ownership, child exit and recovery, version notices. Spawn is ported-with-substitution through an explicit command or the NO_SPAWN path; installer-backed client updates are deferred to the install-path decision in Phase 5. `shim.py`, `daemon_url.py`, `runtimes.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py`, `test_connection_loss_recovery.py`, `test_shim_runtimes.py` mixed; Phase 2b b/e isolated proof in `phase2b-fixes-evidence.json`; final integrated-head acceptance pending | ported-with-substitution |
| SHIM-SDK-PREFLIGHT | 1 | Python-only MCP SDK import preflight is retired in the Rust candidate by the maintainer's 2026-10-05 phase 2 decision, section 1; no Rust SDK diagnostic counterpart. `PORTING.md` no-python-before-first-frame | `test_shim.py` SDK guard nodes remain Python oracle tests; the three manifest rows explicitly record the decision | retired-by-decision |
| SHIM-DAEMON-LAUNCH | 1 | Explicit interpreter or JSON serve argv replaces Python `sys.executable`; missing configuration uses no-spawn waiting with a truthful named explanation. Malformed explicit argv emits `INVALID_SERVE_COMMAND_NOTE` before health and disables fallback spawning. `lifecycle.rs` | `nonboard_spawn_policy.rs`, `nonboard_startup.rs`; Phase 2b b implemented with original isolated-tree targeted proof in `phase2b-fixes-evidence.json`; current integrated-head acceptance pending in PORT-STATE.md | ported-with-substitution |
| SHIM-UPDATE-SCHEDULING | 1 | Explicit-interpreter unattended-update launch starts after the first successful stdout frame flush; manual version remedy precedes it. This row covers launch/scheduling only; installer-backed client updates remain Phase 5 deferred. `wire_json.rs`, `lifecycle.rs` | `wire_json.rs` callback ordering/failure tests, `nonboard_final_assertions.rs::built_binary_launches_updater_only_after_first_flushed_frame`, remote updater subprocess test; Phase 2b e isolated sentinel proof in `phase2b-fixes-evidence.json`; final integrated-head acceptance pending | ported-with-substitution |
| SHIM-CLIENT-UPDATES | 5 | Installer-backed unattended client update execution, installed runtime selection and complete client installation parity. `unattended_update.py`, `runtimes.py`, `update_cli.py` | `test_shim_runtimes.py`, `test_client_environment.py`, `test_update_offer.py`; installed-runtime end-to-end acceptance pending | deferred |
| SHIM-WAKE-REASON | 1 | Pinned CPython 3.11 Unicode 14 alnum/space predicates preserve bounded wake-reason sanitization, including U+001C–001F splitting; repository claim tables remain pinned. `board/liveness.rs`, `board/unicode14.rs` | `board_ring_reason_uses_python_unicode14_predicates`, `board_ring_unicode14_full_range_contract`, `evals/rust_port/unicode14_reason.py`; Phase 2b c implemented with original isolated-tree hash proof in `phase2b-fixes-evidence.json`; final integrated-head parity gates pending | ported |
| SHIM-AUTH | 1 | Token-file precedence and reload, unsafe/malformed files fail closed, writer/session/agent/bank/principal headers; sanitized uncertain-write failures without replay. `credentials.py`, `writer_context.py`, `shim.py` | `test_shim_transport_recovery.py` mixed; `test_writer_keying.py`, `test_principals.py`, `test_credentials.py`, `test_session_identity.py` I; add wire rotation and malformed byte probes | ported |
| SHIM-BOARD | 1 | Registration, scoped identity, addressed-mail continuity, shared-host refusal, local file claims, board retry, default doorbells and optional delivery invoked by the shim; channel process-boundary behavior is phase 1, with only named channel remainder deferred to phase 2. `coordination_adapter.py`, `coordination_identity.py`, `codex_doorbell.py`, `codex_delivery.py`, `repository_claims.py` | `test_shim_board_retry.py`, `test_shim_channel.py`, `test_channel.py`, `test_coordination_roster_hygiene.py`, `test_codex_doorbell.py`, `test_codex_delivery.py`, `test_coordination_adapter.py`, `test_repository_claims.py` I/mixed; add full binary identity/attachment/recovery tests | ported-with-substitution |
| CLI-DISPATCH | 1/2 | First slice: help aliases/trailing argv and documented unknown-command exit-2 cases with UTF-8 streams, valid Unicode scalar argv and Windows CRLF/Linux LF. Remaining mode/version/encoding contracts are separately deferred below. `cli.py` and help fixture | Five unchanged `cli-main-process` nodes; current 779c588c Windows/native Linux receipts pass 15 cases, 45 controls per OS and five Python/five Rust outcomes; both help 3x10 pairs and floors linked in PORT-STATE.md; all four Rust/Parity jobs passed in run 37245992895, with actual CI CLI outcomes verified at same-tree merge checkout b5f485c9; 6e936887 evidence retained as historical | ported |
| CLI-LEASE-core | 2 | Native check/list/run candidate: OS lock truth, FIFO/board mirror, child status and scoped output policies. Four Phase 4 actions excluded. | Final7e both-OS functional/policy receipts, Windows Ctrl-C and committed check/list samples in [bounded evidence](../evals/results/rust-phase2d-lease-7e6927b7/README.md); final hosted acceptance and independent review pending | deferred |
| CLI-LEASE | 4 remainder | Complete action set; `hold`, `break`, `delegate` and `designate` explicitly refuse exit 1 with `deferred in this candidate`, without Python/silent fallback. This candidate loses those actions until Phase 4. `lease_cli.py`, `os_lock.py` | Original lease/coordination tests remain Python baselines; full mode requires every action native plus its own acceptance | deferred |
| CLI-MAIL | 2 | `.seen`/digest watermark race, exits 0 mail/3 timeout/2 setup, output and durable wait cleanup. `wait_mail_cli.py`, `private_state.py` | `test_wait_mail_cli.py`, `test_coordination_mail_continuity.py`, `test_stop_wake_hook.py` mixed/I/A | deferred |
| CLI-HOOK | 2 | Briefing text and bounded hook JSON, memory-change note; episode start/end CLI exit/output. `briefing_cli.py`, `episode_cli.py`, `web/session_hook.py` | `test_briefing.py`, `test_episode_cli.py`, `test_memory_changes_hook.py`, `test_web.py` I/mixed; add fake HTTP server subprocess cases | deferred |
| CLI-DOCTOR | 2 | Read-only diagnostics default, disposable proof explicit, daemon and identity/transport readiness, no incidental mutation. `doctor_cli.py`, `coordination_proof.py`, `wake_liveness.py` | `test_doctor_cli.py`, `test_doctor_coordination.py`, `test_coordination_proof.py`, `test_coordination_probe.py` I/mixed | deferred |
| CLI-CONNECT | 2 | Origin/credential validation, verify-before-write, all-or-nothing backup/rollback/dry-run; connect re-points existing client registrations and never creates a registration. `connect_cli.py`, `client_config.py`, `client_updates.py` | `test_connect.py`, `test_client_credentials_setup.py`, `test_client_sessions.py`, `test_client_environment.py`, `test_client_install_ux.py` I/mixed | deferred |
| CLI-AUDIT | 2 | Export/verify/redact/stats, chain/head semantics, reports and body-expiry distinctions. `board_audit_cli.py`, `board_audit_stats.py`, `storage/coordination.py` | `test_board_audit_cli.py`, `test_board_audit_stats.py`, `test_coordination_audit.py`, `test_coordination_report.py` I/mixed | deferred |
| CLI-BACKUP | 2 | pg_dump + compressed state, no create-on-backup, exclusions and post-success-only own-file rotation. `backup_cli.py` | `test_backup_cli.py`, `test_ops_backup_integrity.py`, `test_bank_dumps.py` I/mixed; add fake pg_dump subprocess fixtures | deferred |
| CLI-TRANSFER | 2 | ZIP/JSONL manifest; exact exported/excluded table rosters in the transfer appendix; float4/ids/HLC/JSONB/time/sequences; import refuses a non-empty bank (only meta and builtin relations are exempt), dimension/column mismatches and other live connections. Before importing exported metadata, it clears the target's `curation_listing_spelling_v2` key so only the export's value stands. `transfer_cli.py` | `test_transfer_cli.py` direct calls I/DB, including `test_import_leaves_the_curation_spelling_flag_to_the_export`; add real CLI export/import against disposable banks | deferred |
| CLI-MODE-OWNERSHIP | 1/2/3/4/5 | All 26 modes have explicit ownership in the CLI checklist: help/version/shim phase 1; tunnel and coordination-recovery phase 2; channel phase 1/2; doorbell-prompt-seen phase 1/2; serve phase 3/4; embedded phase 3/5; update phase 5. `cli.py`, `tunnel_*.py`, `channel.py`, `coordination_recovery.py` | `test_tunnel_cli.py`, `test_tunnel_service.py`, `test_tunnel_bridge.py`, `test_tunnel_profiles.py`, `test_tunnel_runtime.py`, `test_coordination_recovery.py`, `test_channel.py` I/mixed; no supported mode disappears by implication | deferred |
| HTTP-SECURITY | 3 | Health public/degraded semantics; MCP/REST bearer gates on UTF-8/Latin-1 bytes, fail-closed map, remote bind/trust policy, DNS/Origin/rebinding, JSON/body limits, error status, redirect refusal. `daemon.py`, `web/api.py`, `principals.py`, `utils/no_redirect.py` | `test_daemon_http.py` P/I; `test_web.py`, `test_principals.py`, `test_extractor_no_redirect.py`, `test_shim_transport_recovery.py`, `test_dim_mismatch_health.py` I/mixed | deferred |
| HTTP-STATIC | 3 | Root redirect, Console v3 assets at /ui/; /ui/next/ is absent at this pin, traversal/security/content types. `web/api.py`, `web/static/`, `frontend/` | `test_web.py` I/mixed; `test_console_build.py`, `test_console_source_guards.py` A; preserve current Console v3 behavior | deferred |
| HTTP-READS | 3 | Every GET Console route including configuration, episodes, graph/review/provenance and telemetry reads. `web/routes.py`, `web/config_io.py` | `test_web.py`, `test_coordination_console.py`, `test_coordination_web.py`, `test_web_hardening.py` I/mixed; add HTTP corpus for every GET route | deferred |
| PG-HYDRATE | 3 | Exact v53 rows/types/defaults/indexes/extension/vector(1024), idempotent startup/restarts, malformed-state refusal and single writer. `storage/schema.py`, `postgres.py`, `sync.py`, `service.py` | `test_schema_version.py`, `test_schema_ddl_shape.py`, `test_schema_healing.py`, `test_pg_storage.py`, `test_pgvector_compat.py`, `test_fail_closed_hydration.py`, `test_hydrate_capacity.py`, `test_storage_connect.py`, `test_writer_lease.py`, `test_disposable_database_guard.py` I/DB; catalog fixture checks can observe Rust directly | deferred |
| EMBEDDING | 3 | Qwen 1024/MiniLM 384, document/query asymmetry, dtype, cache, dimension refusal, offline load-only validated ONNX artifacts. `memory/embedding.py`, `onnx_artifacts.py`, `utils/config.py` | `test_embedding_backend.py`, `test_embedding_precision.py`, `test_embedding_asymmetry.py`, `test_query_side_encoding.py`, `test_embedding_dim_guard.py`, `test_embedding_cache.py`, `test_embedding_cpu_dtype.py`, `test_onnx_artifacts.py`, `test_onnx_model_provision.py` I/A; real-model subprocess corpus needed | deferred |
| RETRIEVAL | 3 | Five pools, exact cosine/tie order, source/episode/tag/band filters, BM25 fusion, recency/retention, supersession pointers, abstention and strict rerank whole-pool budget. `memory/cms.py`, `miras/`, `bm25.py`, `reranker.py`, `abstain.py` | `test_retrieval_golden.py`, `test_retrieval_pool.py`, `test_retrieval_replay.py`, `test_band_cosine.py`, `test_band_ablation_flat.py`, `test_bm25.py`, `test_cortex_bm25.py`, `test_reranker.py`, `test_reranker_margin_gate.py`, `test_abstain.py`, `test_retention_boost.py`, `test_superseded_visibility.py`, `test_meta_filter.py` I; add exact order corpus, floors alone insufficient | deferred |
| READ-CORE | 3 | memory_get/recent/fact_get/world_search/lesson_search/history/stats/recall/episode summaries, memory_graph, document_search and memory_consolidation_candidates, compact/verbose/explain payloads. `service.py`, `mcp_server.py`, `memory/recall.py`, `context_builder.py`, cortex/world/lessons | `test_mcp_server.py`, `test_recall.py`, `test_constraint_pinning.py`, `test_cortex.py`, `test_world_cortex.py`, `test_lessons_service.py`, `test_episode_service.py` I/mixed; full MCP response corpus needed | deferred |
| WRITE-CMS | 4 | Store surprise admission, non-destructive possible conflicts, bounded capacity, explicit supersede/reinstate/consolidate/forget/reinforce semantics and audit lineage. `cms.py`, `contradiction.py`, `consolidation.py`, `compaction.py`, `service.py` | `test_service.py`, `test_consolidation.py`, `test_consolidation_atomicity.py`, `test_correction_atomicity.py`, `test_correction_identity.py`, `test_entry_reinstatement.py`, `test_forget_cascade.py`, `test_access_count_semantics.py`, `test_store_retirement.py`, `test_slot_keyed_supersession.py` I; add before/after rows and restart corpus | deferred |
| CURATION-RECOVERY | 4 | Slot duplicate retirement/undo transaction and fingerprint/audit reconciliation after uncertain commit; hydrate pending recovery under the service lock before ordinary operations; unknown durable state fails closed and prevents the affected lesson/world snapshot from overwriting uncertain durable state. `curation_safety.py`, `service.py` | `test_curation_safety.py` I/DB; rollback, uncertain commit, next-operation recovery, snapshot protection, third-state refusal and undo tests are Python oracle coverage; Rust runtime/restart evidence pending | deferred |
| WRITE-CORTEX | 4 | HLC authority, scalar/set slots, contenders/promotions, provenance trust, freshness/stale, labels, canonical and world/lesson histories and engrams. `hlc.py`, `cortex.py`, `world_cortex.py`, `lessons.py`, `freshness.py`, `labels.py` | `test_hlc.py`, `test_cortex_sets.py`, `test_cortex_contenders.py`, `test_cortex_promotion.py`, `test_assistant_provenance.py`, `test_contender_stamps.py`, `test_freshness.py`, `test_label_pair.py`, `test_lessons_storage.py`, `test_entity_provenance.py` I/DB | deferred |
| WRITE-EPISODES | 4 | Daemon-owned session handles, pointers/tombstones, nested episodes, reap/resume/title stamps, isolated caller identity and inferred outcomes. `episodes.py`, `writer_context.py`, `service.py`, `web/session_hook.py` | `test_session_identity.py`, `test_episodes.py`, `test_episode_service.py`, `test_session_title.py`, `test_outcome_inference.py` I/mixed; add two-client restart/reap corpus | deferred |
| BACKGROUND | 4 | Autosave/errors, clean-exit flush, release check, heap trim, memory headroom, retry initialization and lock separation/cancellation. `daemon.py`, `service.py`, `release_check.py`, `utils/` | `test_loop_health.py`, `test_service_lock_discipline.py`, `test_init_retry_model_reuse.py`, `test_heap_trim.py`, `test_memory_headroom.py`, `test_update_offer.py`, `test_connection_loss_recovery.py` I/mixed; observed subprocess lifecycle and durable restart cases required | deferred |
| DREAM | 4 | Pull/ack/commit cursors, scheduled quiescence, retry/fallback/extractor protocol/redirection, atomic run rollback, quarantine, literal gate, digests/chronicle. `service_dream.py`, `memory/dream.py`, `dream_token.py` | `test_dream.py`, `test_dream_acknowledgement.py`, `test_dream_ack_storage.py`, `test_dream_runs.py`, `test_dream_stall.py`, `test_dream_quarantine.py`, `test_dream_chronicle.py`, `test_session_digest.py`, `test_extractor_fallback.py`, `test_extractor_no_redirect.py`, `test_literal_gate.py` I/DB; fake sidecar E2E and ladder evidence | deferred |
| CURATION-SAFETY | 3/4 | Read-side auto-dismissal refresh can mutate durable metadata; lesson/world duplicate decisions bind immutable evidence, recheck resident and PostgreSQL row-locked records, commit durable changes before publishing resident stores, and fail closed during reconciliation. Folded-dismissal spelling migration and recovery are write contracts. `curation_safety.py` | `test_curation_safety.py` I/DB; process-boundary equivalents and durable recovery evidence pending | deferred |
| GRAPH-REVIEW | 4 | NetworkX-derived graph/order, communities, aliases/relations, deep dream, safe cleanup, scoped proposals/merge veto/fold direction, review queue/judges/audit. `graph.py`, `memory/graph_*.py`, `review_*.py`, `relation_quality.py`, `service_dream.py` | `test_graph.py`, `test_graph_store.py`, `test_graph_insight.py`, `test_graph_review.py`, `test_graph_consolidation.py`, `test_deep_dream.py`, `test_merge_queue.py`, `test_review_decisions.py`, `test_review_judgment_freshness.py`, `test_queue_judges_service.py`, `test_entity_proposals.py`, `test_edge_proposals.py`, `test_relation_quality.py` I/DB | deferred |
| BOARD-STORE | 4 | All 15 actions, FIFO durable leases, nonce/request dedup, mailbox authorization/clock/retention/redaction/hash chain, bank identity/recovery. `coordination.py`, `storage/coordination.py`, `private_state.py` | `test_coordination_integration.py`, `test_coordination_storage.py`, `test_coordination_tools.py`, `test_coordination_auth.py`, `test_coordination_audit.py`, `test_coordination_clock.py`, `test_coordination_recovery.py`, `test_coordination_secrets.py`, `test_coordination_multi_ack.py`, `test_coordination_mail_continuity.py` I/DB/mixed | deferred |
| BOARD-ASYNC | 4 | Receive <=30s, max64 waiters/16 permits/4-worker isolation, cancellation retains capacity, subscribe-before-read, generations/attachments and notifier shutdown. `web/coordination.py`, `coordination_adapter.py` | `test_coordination_wait.py`, `test_coordination_dispatch_isolation.py`, `test_coordination_adapter.py` I/mixed; add concurrent wire disconnect/timeout/restart cases | deferred |
| DELIVERY | 4 | Codex app-server messages/doorbell, opt-in host transports, wake/park leases and Claude channel behavior. `codex_delivery.py`, `codex_doorbell.py`, `codex_connection.py`, `channel.py`, `wake_liveness.py` | `test_codex_delivery.py`, `test_codex_doorbell.py`, `test_codex_doorbell_probe.py`, `test_codex_coordination.py`, `test_channel.py`, `test_stop_wake_hook.py`, `test_coordination_turn_digest.py` I/mixed/A; fake local host E2E only | deferred |
| HOOKS | 4 | Eight endpoint authorization/method/output/identity contracts; tokenless Host/Origin gate in `_browser_gate`, skipped when a token is configured (not a peer-address gate); plugin 11 handler entries, eight shell scripts plus multiplexed `lifecycle.ps1`; Codex launcher approval identity. `web/api.py`, `session_hook.py`, `plugin_hooks.py`, `plugin/hooks/`, `ops/setup-codex-hooks.py` | `test_web.py`, `test_session_identity.py`, `test_hooks_digest.py`, `test_codex_hooks.py`, `test_codex_hook_launcher.py`, `test_codex_hook_setup.py`, `test_codex_reapproval.py`, `test_hook_glob_ranges.py`, `test_plugin_packaging.py` I/mixed/A; changing hooks.json definitions imposes renewed user approval; see the [hook acceptance checklist](#hook-endpoints-acceptance-checklist) and [Console hook summary](#complete-console-registration-checklist) | deferred |
| HTTP-WRITES | 4 | Every POST Console route, coercion/error mapping, config whitelist/live/restart classification, backup preservation. `web/routes.py`, `config_io.py` | `test_web.py`, `test_console_source_guards.py`, `test_correction_interfaces.py`, `test_web_hardening.py` I/mixed; record POST routes and their storage effects | deferred |
| STORAGE-COMPAT | 3/4/5 | Embedded PG18 lite startup/stable data-dir, files `.pt` state+import/partial restore/weights, Chroma reference-bank ingest/search, parser/chunk rules. `storage/embedded_pg.py`, `migrate.py`, `memory/reference_bank.py`, `document_parser.py`, `utils/atomic_io.py` | `test_embedded_pg.py`, `test_cms_pt_schema.py`, `test_cms_legacy_load.py`, `test_migration.py`, `test_restore_from_pt.py`, `test_atomic_weights.py`, `test_reference_bank.py`, `test_document_parser.py`, `test_document_ingest_batch_bound.py` I/mixed. No simplification is pre-authorized | deferred |
| OPS-CUTOVER | 5 | CPU/offline images, models, compose byte equality, update backup/rollback/daemon-only health/clients, runtimes beside current sessions, unattended upgrade policy, four release surfaces. `ops/Dockerfile.daemon`, compose copies, `update_cli.py`, `unattended_update.py`, `runtimes.py`, `.github/workflows/` | `test_daemon_image_is_cpu_only.py`, `test_update_cli.py`, `test_ops_update_wrappers.py`, `test_shim_runtimes.py`, `test_unattended_update.py`, `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_ops_script_modes.py` I/A/mixed; fresh disposable install/update proof, no deploy | deferred |
| DOCS-CUTOVER | 5 | Guide/provider/tunnel/config/schema/atlas/defaults/help/LLMS and benchmark claim currency, oracle retained pending maintainer retirement. `docs/guide/`, `docs/atlas/`, READMEs, `llms*`, `server.json`, `pyproject.toml`, release manifests | `test_release_ux.py`, `test_atlas_currency.py`, `test_llms_txt.py`, `test_i18n_readme.py`, `test_eval_evidence.py`, `test_extractor_model_lists.py`, `test_repository_claims.py` A; regenerate LLMS after doc edits | deferred |
| CONFIG-LOAD | 1/2/3/4/5 | YAML path/missing-file defaults, unknown-key filtering, recursive dataclass construction, section-specific load rules and env precedence at each reader. `utils/config.py`, service/daemon/client/ops readers; every dataclass and variable is enumerated below and in `contract-inventory.json` | `test_memory_config.py`, `test_ops_env_example.py`, `test_dream.py`, `test_client_environment.py` I/A; preserve defaults, coercion and validation errors | deferred |
| BACKGROUND-SWEEP | 4 | `mcp_server.start_dream_sweep` starts once when dream OR retrieval logging is enabled; sweep drives superseded compaction, dream-run journal pruning and retrieval-log pruning even with dreams off; automatic dream remains backlog/quiescence gated. `memory/dream.py:run_sweep_once` | `test_dream.py`, `test_compaction.py`, `test_dream_runs.py`, `test_retrieval_log.py` I/DB; daemon wire/restart equivalents pending | deferred |
| BACKGROUND-SESSIONS | 4 | `mcp_server.start_session_reaper` idempotence, idle/reap intervals, close/end dream, empty-session resume window and tombstones; `start_background_durability` registers clean-exit flush, autosave and warmup exactly once. `service.py`, `mcp_server.py` | `test_session_identity.py`, `test_episode_service.py`, `test_loop_health.py` I; startup/clean-exit/reap wire cases pending | deferred |
| HEALTH | 3 | Open /health payload mandatory and conditional fields and 200/503/500 mapping listed below; bank fingerprint only after successful DB ping; warnings that leave status ok remain non-fatal. `daemon.py:_build_health_payload`, `web/api.py` | `test_daemon_http.py`, `test_dim_mismatch_health.py`, `test_daemon_moved_fence.py` I/P; network health corpus pending | deferred |
| MCP-MOUNT | 3 | /mcp and /mcp/* pass to SDK after bearer gate; a supplied bound identity must resolve an authenticated principal and match the coordination context, failing closed before forwarding to `mcp_app`, including on open loopback installs. Streamable HTTP mount and token-aware transport security: tokenless DNS-rebinding protection allows loopback Host/Origin patterns; configured auth disables SDK rebinding protection. `mcp_server.transport_security_for`, `build_streamable_http_app`, `daemon.py`, `web/api.py` | `test_daemon_http.py`, `test_mcp_client_neutrality.py`, `test_web.py` I/P; negotiate and probe mount over HTTP | deferred |
| DISPOSABLE-GUARDS | 2/3/4/5 | `storage/schema.py:refuse_production_database` rejects known production names after stripping trailing slashes and casefolding; unresolved production identity, including the literal `<unresolved>` sentinel, fails every check closed. `assert_disposable_database` asks the connected server for its actual database and applies that same refusal before reap/DDL/truncate. It does not impose a disposable-name prefix allowlist. Existing guards precede destructive work. | `test_disposable_database_guard.py`, `test_bench_production_port_guard.py` I/DB; equivalent Rust refusal tests pending | deferred |
| SEARCH-COMPACTION | 3 | `memory_search` compact payload uses `memory.mcp.entry_text_chars` (600 default), literal `truncated: true` only when own text is clipped; verbose bypasses compaction. `replaced_by` is {id, at, preview, verified, current}, preview 120 chars; null id/date and false current remain distinct. Full text available via memory_get. `mcp_server.py:_compact_entry`, `_replaced_by` | `test_mcp_server.py`, `test_superseded_visibility.py` I; compact/verbose/successor wire corpus pending | deferred |
| CONTRADICTION | 4 | Structured-slot identity first, then negation asymmetry, affirmative replacement and anchor-tier state transition heuristics; optional bounded NLI scorer is implemented but unwired by service. Detection admits possible conflicts and never retires whole notes automatically. `memory/contradiction.py`, `memory/nli.py`, `memory/cms.py`, `service.py` | `test_contradiction_cost.py`, `test_service.py`, `test_store_retirement.py` I; four paths and missing-scorer equivalents pending | deferred |
| HTTP-BODY-LIMITS | 3/4 | Per-route byte limits and /api/agents?view=coordination response selection listed below; wrong methods, oversized and malformed bodies preserve oracle statuses. `web/api.py:_body_limit`, `web/routes.py` | `test_web_hardening.py`, `test_coordination_web.py`, `test_coordination_console.py`, `test_pair_endpoint.py` I; additive HTTP boundary cases pending | deferred |
| PRINCIPALS-V53 | 1/2/3/4 | Durable principals, SHA-256 token/code storage, refresh/exclusion rules, expiry/revocation and fail-closed unavailable identities; invited caller operator restrictions. `principal_store.py`, `principals.py`, `storage/schema.py` | `test_principal_store_pg.py`, `test_stored_principals.py`, `test_stored_principals_daemon.py`, `test_stored_principals_mcp.py`, `test_stored_principals_daemon_wiring.py` I/DB | deferred |
| CLI-PAIRING | 2/3/4 | invite principal creation/list/revoke; pair mints owner-only local token, sends only hash to POST /api/pair, updates existing registrations; browser-origin refusal, uniform pairing_refused, concurrency/rate budget, unavailable mapping. `invite_cli.py`, `pair_cli.py`, `web/api.py` | `test_invite_cli.py`, `test_pair_cli.py`, `test_pair_endpoint.py` I/mixed | deferred |
| CLI-EXPOSE-MOVE | 2/3/5 | expose requires token and owns only its Tailscale forward; move final stopped-daemon backup, source fencing, restore/row verification, one destination start, client re-pointing and rollback; moved/unfinished move startup refusal. `expose_cli.py`, `move_cli.py`, `daemon.py:moved_refusal` | `test_expose_cli.py`, `test_move_cli.py`, `test_daemon_moved_fence.py` I/mixed; disposable host stand-ins pending | deferred |
| MAIL-RING-V53 | 1/2/4 | wait-mail wakes on an authorized daemon ring, plain addressed mail never wakes; digest/watermark cleanup, mailbox attribution to parent/subagent, generation/attachment continuity and optional Codex mailbox-tool approval. `wait_mail_cli.py`, `coordination_adapter.py`, `ops/setup-codex-hooks.py` | `test_wait_mail_cli.py`, `test_coordination_adapter.py`, `test_codex_subagent_stop.py`, `test_codex_hook_setup.py` I/mixed | deferred |
| OPS-INSTALL-RESTORE | 5 | install.sh/ps1; install-autostart.ps1; install-shim-autostart.sh/ps1 and install-codex-shim-autostart.sh/ps1; install-hook.sh/ps1; restore.sh/ps1 and restore_from_pt.py; preflight.sh/ps1; migrate-pg18.ps1; backup.sh/ps1 and install-backup-task.ps1; prune-rollbacks.sh/ps1, prune-build-cache.sh/ps1 and install-cache-retention.ps1; register_claude_desktop.py; setup-codex-coordination.py; update.sh/ps1/update.py/update_clients.py. Preserve checks/refusals/rollback/platform and client ownership. `ops/` | `test_ops_script_modes.py`, `test_ops_backup_integrity.py`, `test_ops_restore_rehearsal.py`, `test_ops_restore_skips_held.py`, `test_ops_prune_rollbacks.py`, `test_ops_prune_build_cache.py`, `test_ops_install_backup_task.py`, `test_ops_install_cache_retention.py`, `test_ops_update_wrappers.py`, `test_client_install_ux.py` I/A/mixed | deferred |
| OPS-MAINTENANCE | 5 | `ops/migrate_embeddings.py`: dry-run default, backup/daemon refusal gates and vector/schema migration; `ops/dedup_cortex.py`: dry-run/apply supersession, writer-lease refusal and changed-slot persistence; `ops/retire_by_writer.py`: dry-run/apply writer/session retirement; `ops/backfill_edge_confidence.py`: dry-run/apply confidence backfill; `ops/migrate_drop_age.py`: immediate AGE extension removal; `ops/measure_reverify_population.py`: consistent read-only counts and text/JSON output. Preserve each script's distinct mutation/refusal contract | `test_migrate_embeddings.py`, `test_dedup_cortex_script.py`, `test_writer_keying.py`, `test_backfill_edge_confidence.py`, `test_trace_invalidations_storage.py` I/DB/mixed; no dedicated `migrate_drop_age.py` test at the pin; public process and Rust evidence pending | deferred |
| OPS-IMAGES | 5 | Dockerfile.extractor sidecar model/config/health contract; Dockerfile.pg PostgreSQL18/pgvector build/runtime contract; preserve daemon CPU/offline and compose byte equality. `ops/Dockerfile.extractor`, `ops/Dockerfile.pg`, `ops/Dockerfile.daemon` | `test_daemon_image_is_cpu_only.py`, `test_update_cli.py`, `test_extractor_model_lists.py` A; disposable image proofs pending | deferred |
| PLUGIN-COMMANDS | 2/4/5 | plugin/commands/dream.md and memory-status.md user workflows; plugin/.claude-plugin/plugin.json identity and release.json release metadata; hooks.json 11 entries/eight shell scripts/lifecycle.ps1; PreToolUse subagent-board-guard allows list/default agents and receive-only mail, denies board writes/send/ack on parent address. `plugin/`, `plugin_hooks.py` | `test_plugin_packaging.py`, `test_subagent_board_guard.py`, `test_subagent_board_hook.py`, `test_hooks_digest.py`, `test_codex_reapproval.py` A/I/mixed; cross-reference HOOKS and hook acceptance checklist | deferred |
| ONNX-PREREQUISITE | 3 | pyproject.toml onnx-extra comment says bit-identical embeddings; docs/guide/configuration.md says default Qwen has no supplied ONNX artifact. Packaging/docs disagreement remains unchanged here; phase 3 requires identified same graph/tokenizer, measured error and maintainer docs resolution before accepting embedding parity. | `test_onnx_artifacts.py`, `test_onnx_model_provision.py`, `test_embedding_backend.py` I/A; no accepted tolerance or equivalence | deferred |

## Per-phase selection and acceptance gaps

Phase 0b: unchanged Python control over `test_shim.py`, `test_daemon_http.py`, selected recovery boundary nodes and docs/evidence guards; new generated corpus/schema snapshot/harness negative controls. Baselines identify backend and use a quiet CPU host with lease free. The historical disposable trial is complete and its corrections remain in PORTING.md; phase 0b closes the five new gaps rather than repeating that trial.

Phase 1: selected real stdio/HTTP nodes in `test_shim.py` plus audited subprocess recovery cases; original entire shim/credential/tier/identity/recovery file pools remain Python. `test_shim_autostart.py`, `test_shim_python.py` and `test_shim_prompt.py` are extractor tooling, not MCP shim coverage; do not select them based on name alone. For `_proxy` private snippets add equivalent binary probes rather than claim they exercise Rust.

Phase 2: subprocess nodes audited from lease/mail/doctor/connect/briefing/episode leaves. `tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes` launches the public `doctor` CLI and is an oracle subprocess node for phase 2, not an internal shim helper; its Rust routing remains deferred. Existing backup/transfer/audit/CLI dispatcher tests largely call functions directly or install Python fakes; they remain oracle, with new argv/HTTP/fake-executable and disposable-PG cases. Tunnel/recovery are phase 2, channel phase 4, embedded phase 3/5, and version/help phase 1 as recorded in the mode checklist. These cannot become unspecified deferrals at a purported completed cutover.

Phase 3: real boundary nodes in daemon HTTP; every Console GET and MCP read response differential; v53 catalog/rows, binary restart/hydration checks and frozen real-model exact-order corpus. All Python embedding/CMS/BM25/reranker/recall units remain control plus Rust equivalents. Cannot claim same default ONNX graph or derive exact order parity from current quality floors.

Phase 4: complete MCP mutation/POST/hook/15-action wire corpus plus disposable rows before/after and restart, fake extractor/host transport tests, cancellation/ownership/concurrency fault cases. Existing storage/service/session/dream/graph/board tests remain Python control with named Rust unit counterparts. Full suite required for process lifecycle/shared harness infrastructure where CLAUDE applies; runtime dispatch adapters cannot bypass admission.

Phase 5: artifact/static guards can run unchanged because they inspect files; direct Python update/runtime tests remain oracle until maintainer decides retirement. New fresh-install/update/restore proofs must exercise the actual Rust installed entry points. Do not delete packaging/source the guards inspect just to make them pass. A documented dependency-light shim includes package install/runtime/provider registration, not binary compilation alone.


## Internal-only gaps requiring explicit equivalents

1. Python registration/thread wrappers and sync-callable tool function constraints (`test_mcp_server.py`) are implementation assertions; preserve observable concurrency/annotations/schema/help via wire and test Rust ownership/thread behavior beside the port.
2. Most REST tests instantiate `build_console_app(stub_mcp, ..., FixtureService)`; this is ASGI in one Python process, not a daemon boundary. Mocked service forwarding tests remain unit oracle; add real network cases with deterministic synthetic bank and response expectations.
3. `pg_service`, `pristine_service`, file-mode CMS, direct `PostgresStorage`/cortex/graph/embedding objects and monkeypatches cannot point at a Rust daemon through an environment variable. Translate assertions to additive boundary fixtures where possible; direct migration/DDL/storage helpers require Rust units and shared catalog/result contracts.
4. Existing transport recovery creates Python `_proxy` programs and inspects internal error classifier/cancellation routines; preserve them unchanged and add binary fault server cases for redirect refusal, lost response, token rotation, event-size limits and no uncertain-write replay.
5. HLC wall clock, ordering, equal-score ties, stale thresholds and retention need deterministic time-aware cases; normalizing away these fields destroys coverage. Compare state transitions, not merely final counts.
6. ML defaults and optional fallback behavior are public contracts: ONNX runtime alone does not port sentence-transformers preprocessing/tokenizer/pooling/Dense/Normalize or cross-encoder inference. Record unsupported module/fallback behavior explicitly; no permission to retire torch fallback, NLI, Chroma, `.pt` or embedded/channel mode is implied; Console v3 replaces the old frontend by upstream product change at this pin.
7. Public PII/evidence guards may need additive new guard tests outside immutable existing files; the port performs no DDL, table addition or column repurposing. The historical phase-start schema is 53; the historical close-out pin is schema 54 and the current pin is schema 55; upstream schema bumps follow the seven-place checklist and a newly recorded phase pin.
8. Ranking comparisons have side effects (access counts/read logs, retention boosts, metadata); use independent identical seeded banks and operation order, and include mutation/read observability rather than resetting state after every request indiscriminately.


## Complete MCP registration checklist

All rows share `pseudolife_memory/mcp_server.py` and the `test_mcp_server.py`/`test_mcp_string_arguments.py` oracle pools; the differential harness must freeze tool inputSchema, descriptions, annotations, defaults, response and invalid-argument semantics for each. These registrations are source-derived, not an imported server introspection result.

| Tool | Assigned tier | Port phase | Status |
|---|---|---:|---|
| memory_agents | core | 1/4 | deferred |
| memory_message | core | 1/4 | deferred |
| memory_store | minimal | 4 | deferred |
| memory_search | minimal | 3 | deferred |
| memory_recent | full | 3 | deferred |
| memory_supersede | full | 4 | deferred |
| memory_reinstate | full | 4 | deferred |
| memory_stats | core | 3 | deferred |
| memory_get | core | 3 | deferred |
| memory_reinforce | full | 4 | deferred |
| memory_fact_get | minimal | 3 | deferred |
| memory_fact_set | minimal | 4 | deferred |
| memory_set_add | minimal | 4 | deferred |
| memory_set_remove | minimal | 4 | deferred |
| memory_fact_resolve | core | 4 | deferred |
| memory_history | full | 3 | deferred |
| memory_world_set | core | 4 | deferred |
| memory_world_search | core | 3 | deferred |
| memory_outcome | minimal | 4 | deferred |
| memory_lesson_search | minimal | 3 | deferred |
| memory_forget | full | 4 | deferred |
| memory_dream | full | 4 | deferred |
| memory_graph_review | full | 4 | deferred |
| memory_episode_start | core | 4 | deferred |
| memory_episode_end | core | 4 | deferred |
| memory_session_title | minimal | 4 | deferred |
| memory_episode_summary | full | 3 | deferred |
| memory_consolidation_candidates | full | 3 | deferred |
| memory_consolidate | full | 4 | deferred |
| memory_graph_relate | core | 4 | deferred |
| memory_graph_unrelate | full | 4 | deferred |
| memory_alias | full | 4 | deferred |
| memory_graph | core | 3 | deferred |
| memory_recall | core | 3 | deferred |
| memory_relation_define | full | 4 | deferred |
| document_ingest | core | 4 | deferred |
| document_search | core | 3 | deferred |
| memory_toolset | minimal | 1/3 | deferred |


## Complete Console registration checklist

Shared source: pseudolife_memory/web/routes.py; Python oracle pools: test_web.py and test_web_hardening.py. Every registration needs additive real HTTP normal/error/parameter/default/identity probes; POST rows also compare durable effects. GET rows that depend on later mutable subsystems still require their response contract in phase 3; writes are phase 4.

| Method | Path | Phase | Status |
|---|---|---:|---|
| GET | /api/stats | 3 | deferred |
| GET | /api/overview | 3 | deferred |
| GET | /api/facts | 3 | deferred |
| GET | /api/facts/history | 3 | deferred |
| POST | /api/facts/resolve | 4 | deferred |
| POST | /api/facts/set | 4 | deferred |
| POST | /api/facts/forget | 4 | deferred |
| GET | /api/world | 3 | deferred |
| GET | /api/lessons | 3 | deferred |
| GET | /api/briefing | 3 | deferred |
| GET | /api/agents | 3 | deferred |
| GET | /api/episodes | 3 | deferred |
| GET | /api/episodes/summary | 3 | deferred |
| POST | /api/episode/start | 4 | deferred |
| POST | /api/episode/end | 4 | deferred |
| POST | /api/episodes/prune | 4 | deferred |
| POST | /api/episodes/rename | 4 | deferred |
| POST | /api/episodes/merge | 4 | deferred |
| GET | /api/recent | 3 | deferred |
| GET | /api/search | 3 | deferred |
| GET | /api/trace | 3 | deferred |
| GET | /api/recall | 3 | deferred |
| GET | /api/chain | 3 | deferred |
| GET | /api/entry | 3 | deferred |
| POST | /api/reinforce | 4 | deferred |
| GET | /api/sources | 3 | deferred |
| GET | /api/graph | 3 | deferred |
| GET | /api/graph/projects | 3 | deferred |
| GET | /api/graph/digest | 3 | deferred |
| GET | /api/graph/communities | 3 | deferred |
| GET | /api/graph/path | 3 | deferred |
| GET | /api/graph/review | 3 | deferred |
| GET | /api/graph/proposal-evidence | 3 | deferred |
| POST | /api/graph/rejudge | 4 | deferred |
| GET | /api/wiki | 3 | deferred |
| GET | /api/graph/entity-provenance | 3 | deferred |
| POST | /api/graph/assign-scope | 4 | deferred |
| POST | /api/graph/unrelate | 4 | deferred |
| POST | /api/graph/relate | 4 | deferred |
| POST | /api/graph/bless-edge | 4 | deferred |
| POST | /api/graph/dismiss-duplicate | 4 | deferred |
| GET | /api/curation/duplicates | 3 | deferred |
| POST | /api/curation/dismiss-duplicate | 4 | deferred |
| GET | /api/curation/retired | 3 | deferred |
| POST | /api/lessons/restore | 4 | deferred |
| POST | /api/world/restore | 4 | deferred |
| POST | /api/graph/delete-entity | 4 | deferred |
| POST | /api/graph/merge | 4 | deferred |
| POST | /api/graph/accept-proposal | 4 | deferred |
| POST | /api/graph/reject-proposal | 4 | deferred |
| POST | /api/graph/accept-entity-merge | 4 | deferred |
| POST | /api/graph/accept-entity-junk | 4 | deferred |
| POST | /api/graph/reject-entity-proposal | 4 | deferred |
| GET | /api/dream/status | 3 | deferred |
| POST | /api/dream/run | 4 | deferred |
| GET | /api/consolidation | 3 | deferred |
| POST | /api/consolidate | 4 | deferred |
| POST | /api/delete | 4 | deferred |
| POST | /api/supersede | 4 | deferred |
| POST | /api/daemon-notice | 4 | deferred |
| GET | /api/config | 3 | deferred |
| POST | /api/config | 4 | deferred |
| POST | /api/pair | 3/4 | deferred |

Hook paths (web/api.py; phase 4; oracle test_web.py/test_session_identity.py; cross-reference HOOKS and the methods in the hook acceptance checklist): session-start, memory-policy, memory-changes, session-end, coordination-start, park-gate, woke, subagent, all under /api/hook/.

Coordination action rows (coordination.py; phase 4 with phase-1 shim clients): context, register, update, agents, lease, release, leases, attach, heartbeat, detach, send, receive, history, ack, attempt, all under /api/coordination/. Compare each action's parameter whitelist, required fields, principal/instance rules, status and public error codes. Unknown actions fail; receive alone accepts wait_seconds under the bounded hub contract.

## Hook endpoints acceptance checklist

Source: `pseudolife_memory/web/api.py`; cross-reference HOOKS and the Console checklist hook summary. The gate checks Host and Origin in tokenless mode only and is skipped with configured authentication. Each item requires normal, invalid-input, authentication and durable-effect evidence as applicable.

| Item | Phase | Status | Evidence |
|---|---|---|---|
| GET /api/hook/session-start | 4 | deferred | No accepted Rust evidence |
| GET /api/hook/memory-policy | 4 | deferred | No accepted Rust evidence |
| GET /api/hook/memory-changes | 4 | deferred | No accepted Rust evidence |
| POST /api/hook/session-end | 4 | deferred | No accepted Rust evidence |
| GET /api/hook/coordination-start | 4 | deferred | No accepted Rust evidence |
| GET /api/hook/park-gate | 4 | deferred | No accepted Rust evidence |
| POST /api/hook/woke | 4 | deferred | No accepted Rust evidence |
| POST /api/hook/subagent | 4 | deferred | No accepted Rust evidence |

## Coordination actions acceptance checklist

Source: `pseudolife_memory/coordination.py`. Each item requires normal, invalid-input, authentication and durable-effect evidence as applicable.

| Item | Phase | Status | Evidence |
|---|---|---|---|
| context | 1/4 | deferred | No accepted Rust evidence |
| register | 1/4 | deferred | No accepted Rust evidence |
| update | 1/4 | deferred | No accepted Rust evidence |
| agents | 1/4 | deferred | No accepted Rust evidence |
| lease | 1/4 | deferred | No accepted Rust evidence |
| release | 1/4 | deferred | No accepted Rust evidence |
| leases | 1/4 | deferred | No accepted Rust evidence |
| attach | 1/4 | deferred | No accepted Rust evidence |
| heartbeat | 1/4 | deferred | No accepted Rust evidence |
| detach | 1/4 | deferred | No accepted Rust evidence |
| send | 1/4 | deferred | No accepted Rust evidence |
| receive | 1/4 | deferred | No accepted Rust evidence |
| history | 1/4 | deferred | No accepted Rust evidence |
| ack | 1/4 | deferred | No accepted Rust evidence |
| attempt | 1/4 | deferred | No accepted Rust evidence |

## CLI mode acceptance checklist

Source: `pseudolife_memory/cli.py` at `f709abb54f7912ae9cd767998d0926ca33df4bcd`.
The default is `shim`; help aliases are `-h` and `--help`, and version also accepts
`--version`. Dispatch reads the first argument; help/shim/channel ignore trailing
arguments. Every mode preserves exit status, both output streams and argument
validation. Subcommands remain part of each mode's contract.

The requested 19 Phase 2 modes are ten Tier A leaves, six Tier B modes and three
Tier C modes. Help/version are two carried Phase 1 gaps; channel is the shared
Phase 1/2 remainder. The original 26 plus upstream maintainer/test-login make the
current 28. Tier labels record the requested scope, not a claim of HTTP-only
dependencies: recovery, audit and lease operator actions reach PostgreSQL directly.
They remain deferred without introducing a database client into this binary.

The first slice implements help and unknown dispatch and adds a separate 15-case
CLI corpus, strict byte self-replay/candidate judge, environment selector and help
cold-start-to-exit measurement instrument. Current receipts from committed
`779c588c` on Windows and native Linux pass 15 cases, 45 controls and five
Python/five Rust original nodes each; both help 3x10 measurement pairs and
descriptive repeat floors are linked in PORT-STATE.md. Scope is UTF-8
stdout/stderr and valid Unicode scalar argv, preserving Windows CRLF and Linux
LF. Locale/default and other output encodings and non-UTF-8/surrogate argv remain
deferred. Help and scoped CLI-DISPATCH acceptance are ported: both-OS local
receipts, help pairs and all four Rust/Parity jobs in workflow 37245992895 pass.
CI receipts bind actual merge checkout `b5f485c9` to the same tree as PR head
`779c588c`; historical `6e936887` records remain unchanged. Standard file locks
replace `fs2`; source validation passed 277 Linux and 296 Windows Rust tests
without skips. Current documentation-head review/checks remain separate.
All nine reported PR checks passed at `779c588c`. The five existing
`cli-main-process` nodes remain the only routed CLI nodes. Other entries below
name concrete oracle nodes or internal test-function pools awaiting a process
adapter; a function name without parameter suffixes is not a routed node claim.

| Mode | Phase | Status | Evidence |
|---|---|---|---|
| help | 1 | ported | Implementation in `shim/src/cli.rs`; `test_cli_dispatch.py::test_help_prints_usage_and_exits_zero[--help]`, `[-h]`, `[help]`, `test_help_lists_version`; current 779c588c UTF-8 stream/scalar argv Windows/native Linux receipts and help pairs linked in PORT-STATE.md; all four Rust/Parity jobs and actual five-node outcomes verified in run 37245992895 at same-tree merge checkout b5f485c9; historical 6e936887 evidence retained |
| version | 1 | ported | Scoped installer-manifest and named-console file admission in the executable's own `Scripts`/`bin` runtime; UTF-8 stdout/stderr with platform newlines. [Final merged-master proof at `df2dbf8a`](../evals/results/rust-phase2d-version-df2dbf8a/README.md) passes 28 exact cases/112 rejecting controls per OS, missing-console fallback, actual non-ASCII homes, native argv and eight unchanged original CLI nodes once/non-skipped per arm. All four warm 3x10 pairs and serial version contracts 8/8 per feature on both OSes pass. Executed tree `28823784` and fresh image identities remain separate from this evidence carrier; its review/checks remain required. Source ratio 184/125 = 1.472 is reporting, with no hard target. Manifest NaN/Infinity/lone-surrogate/beyond-u64, locale/default encodings and non-UTF-8/surrogate argv remain deferred; candidate not installed. Historical `5220b5ee`/cold-copy failures and unresolved Linux parallel ETXTBSY remain retained |
| shim | 1 | deferred | No accepted Rust evidence |
| serve | 3/4 | deferred | Outside client leaves; `daemon.py`, storage/model ownership; `test_daemon_http.py` oracle pool |
| embedded | 3/5 | deferred | Outside client leaves; in-process `mcp_server.py` with storage/models/install behavior; `test_mcp_server.py` internal pool |
| channel | 1/2 | deferred | Phase 1 process boundary; internal remainder `test_channel.py::test_channel_serializes_simultaneous_writes`, `test_channel_startup_failure_closes_output_without_opening_inbox`; inherited Phase 1 equivalents and receipts remain separately governed |
| coordination-recovery | 2 | deferred | Requested A, actual direct PostgreSQL via `coordination_recovery.py`/CoordinationStore; Phase 4 recovery transaction; `test_coordination_recovery.py::test_recovery_revokes_and_rebinds_private_state_without_exposing_key`, `test_rebind_state_failure_rolls_back_credential_issuance` |
| board-audit | 2 | deferred | Requested A, direct PostgreSQL/container operator path in `board_audit_cli.py`; Phase 3 reads/4 redact; `test_board_audit_cli.py::test_export_writes_json_lines_filtered_by_task_agent_and_time`, `test_verify_prints_the_head_and_fails_on_tampering_but_an_archive_still_verifies` |
| briefing | 2 | deferred | A HTTP/filesystem in `briefing_cli.py`; `test_briefing.py::test_briefing_no_daemon_prints_nothing`, `test_hook_json_serves_the_session_start_core_not_the_bare_briefing`; internal fakes need process equivalents |
| prompt-hook | 2 | deferred | A HTTP/private watermark in `briefing_cli.py`; `test_memory_changes_hook.py::test_prompt_hook_prints_only_changes_and_advances_its_cursor`, `test_prompt_hook_prints_nothing_when_it_cannot_save_its_cursor`; internal fakes need process equivalents |
| doctor | 2 | deferred | A offline/HTTP/MCP subprocess checks in `doctor_cli.py`; public `test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes`; `test_doctor_cli.py::test_doctor_hands_the_registration_credential_to_the_handshake` internal; adapter and corpus pending |
| connect | 2 | deferred | B installed-shim handshake/config transaction (`connect_cli.py`); `test_connect.py::test_the_handshake_ignores_a_pseudolife_memory_package_in_the_working_directory`, `test_a_relative_token_file_reaches_the_neutral_directory_handshake_resolved`; process seam audit pending |
| tunnel | 2 | deferred | B operator/runtime/bridge/network consent (`tunnel_cli.py`, `tunnel_profiles.py`, `tunnel_runtime.py`, `tunnel_bridge.py`); internal `test_tunnel_cli.py::test_setup_resumes_without_erasing_key_consent_or_local_config`, `test_handshake_failure_redacts_transport_output_and_does_not_write`; additive process fixtures pending |
| update | 5 | deferred | Outside client leaves; release/install/runtime transactions in `update_cli.py`; `test_update_cli.py`/`test_client_install_ux.py` pools |
| backup | 2 | deferred | C Phase 3/4; `backup_cli.py` imports embedded_pg, resolves direct DSN and runs pg_dump; `test_backup_cli.py::test_file_mode_backup_archives_state_only`, `test_dumpless_run_never_rotates_dumps`, `test_backup_roundtrip_embedded`; file-only coverage cannot accept whole mode |
| export | 2 | deferred | C Phase 3; `transfer_cli.py` direct psycopg/schema/vector text, torch-free at CLI module; `test_transfer_cli.py::test_export_import_roundtrip_preserves_every_table`, `test_export_skips_transient_meta_and_telemetry` |
| import | 2 | deferred | C Phase 4 direct psycopg durable writes; `test_transfer_cli.py::test_import_refuses_a_nonempty_bank`, `test_import_refuses_while_other_connections_hold_the_bank`, `test_import_refuses_embedding_dim_mismatch`, `test_import_leaves_the_curation_spelling_flag_to_the_export` |
| episode-start | 2 | deferred | A HTTP POST `/api/episode/start` in `episode_cli.py`; `test_episode_cli.py::test_daemon_down_is_silent_exit_zero`, `test_parses_session_key_from_stdin`, `test_post_does_not_forward_the_bearer_across_a_redirect`; stdin/HTTP process fixtures pending |
| episode-end | 2 | deferred | A HTTP POST `/api/episode/end` in `episode_cli.py`; same `test_episode_cli.py` nodes as episode-start; action-specific process fixtures pending |
| wait-mail | 2 | deferred | A private digest/seen/WaitListener (`wait_mail_cli.py`); `test_wait_mail_cli.py::test_a_rung_marker_ends_the_wait_once`, `test_non_ascii_peer_text_reaches_stdout_byte_for_byte`, `test_missing_digest_file_exits_2_and_says_what_to_do`; bounded disposable process fixtures pending |
| doorbell-prompt-seen | 1/2 | deferred | A receipt correlation/private filesystem lock (`codex_doorbell_state.py`); internal `test_codex_doorbell.py::test_prompt_hook_arrival_racing_queue_acceptance_does_not_restore_pending`, `test_a_linked_prompt_receipt_cannot_release_the_queue`; process equivalents pending |
| lease | 2 | deferred | Requested A; check/list/run/hold filesystem+HTTP (`lease_cli.py`, `os_lock.py`), break/delegate direct CoordinationConnection/CoordinationStore deferred Phase 4; `test_lease_cli.py::test_check_says_free_and_exits_0_without_a_board`, `test_check_exits_1_while_the_local_lock_is_held`, `test_hold_keeps_the_lease_while_the_pid_lives_then_releases`; operator `test_lease_cli_board.py::test_the_operator_breaks_a_lease_and_the_next_waiter_gets_it`, `test_the_operator_grants_a_projects_delegate`; `--for 7d` and hold grammar retained in deferred scope |
| invite | 2 | deferred | B direct operator SQL/psql/container (`invite_cli.py`); internal `test_invite_cli.py::test_an_invite_prints_the_code_once_and_stores_only_its_hash`, `test_a_malformed_name_is_a_usage_error`; direct-bank effect deferred Phase 4, process seam pending |
| pair | 2 | deferred | B HTTP pairing/owner-only token/retry (`pair_cli.py`); `test_pair_cli.py::test_read_code_takes_the_code_from_stdin`, `test_an_unknown_outcome_keeps_the_file_and_names_it`; stdin and retry process fixtures pending |
| expose | 2 | deferred | B Tailscale status/subprocess/HTTP (`expose_cli.py`); internal `test_expose_cli.py::test_success_runs_the_exact_command_and_prints_the_url`, `test_a_foreign_serve_on_the_port_is_never_replaced`; disposable executable equivalents pending |
| move | 2 | deferred | B Docker/ssh pg_dump/restore/fence/operator transaction (`move_cli.py`); `test_move_cli.py::test_declining_exits_2_and_changes_nothing`; process seam and disposable operator state pending; no live-bank work |
| maintainer | 4/5 | deferred | Added upstream at the schema-54 close-out pin; outside the original 26-mode phase 2 scope; `test_maintainer_cli.py`, `test_maintainer_setup.py` |
| test-login | 5 | deferred | Added upstream at the schema-54 close-out pin; outside the original 26-mode phase 2 scope; `test_test_login_cli.py` |

## Test-file buckets

The real-bank stdio judge declares `completed-readiness-wait-notice` as a
semantic normalization at `/stderr`. The producer is the shim's existing
no-spawn path: Python and Rust can print a five-second readiness wait notice
after an unsuccessful initial probe, then complete the same exchange.
Normalization requires matching stdout under the existing byte comparator,
successful integer exit 0 in both arms, at least one stdout frame, genuine
process evidence, and stderr consisting only of the exact notice or empty bytes.
The notice is limited to HTTP loopback `127.0.0.1`, a decimal port 1–65535, the
existing five-second text, and one LF or CRLF terminator. It never suppresses
extra diagnostics, timeout/boundary errors, or output/exit mismatches. Startup,
fault, EOF and concurrent-call policies remain strict; transport headers are
outside this rule. Raw streams remain in private captures and both arms' raw
stderr is also retained in each applied normalization event in public receipts.
The regression test retains the exact 172-byte Windows notice, with synthetic
matched frames; it is not a replay of historical stdout. The original cause of
the initial probe miss remains unproven, and this rule changes no production
lifecycle, timeout or diagnostic behavior.

The Phase 0b snapshot retains 406 `tests/test_*.py` files: 68 oracle, 1 candidate and 337 internal. Its historical manifests remain `test-buckets.json` and `contract-inventory.json`; validate them with `python rust/contract_inventory.py` and the unchanged `python -m pytest rust/test_contract_inventory.py -q`.

The current Phase 1 manifest at `3c01bb31abd60178e15dea99adda369b4bbf92fc` contains 423 `tests/test_*.py` files: 68 oracle, 2 candidate and 353 internal. Historical manifests at `f709abb54f7912ae9cd767998d0926ca33df4bcd` retain 419 files: 68 oracle, 2 candidate and 349 internal. Its manifests are `phase1-test-buckets.json` and `phase1-contract-inventory.json`. Candidate routing maps exactly 18 concrete nodes: 8 from `test_cli_dispatch.py` and 10 public stdio cases from `test_shim.py`; unlisted nodes stay oracle-only. The phase 1 function inventory separately classifies all 191 current-pin functions in its nine scoped files as 10 candidates, 1 public oracle CLI case and 180 internal cases. The two current-pin additions check tool refusals and unknown-parameter forwarding through the public default stdio launch; their source identity and adapter routing are covered. Both executed once/non-skipped per arm in the historical native Linux capture at `74b5998c`: tool refusal failed in both arms, while unknown-parameter refusal passed in both. At published `95d5402d`, both additions passed once/non-skipped in each OS candidate lane; no Python ten-stdio original lane was reported. The later console/warm successor remains pending its combined-head proof. Historical targeted-equivalence evidence retains its original candidate trees and pins. Historically, at `2e628b27`, all 125 scoped internal cases named Rust equivalents with targeted Windows and Linux evidence. The current mapping has 121 completed equivalents, exactly 3 SDK cases retired-by-decision, and 1 pending postframe update-scheduling substitution with targeted evidence on both platforms at frozen tree `a4ff3236eb349aaed427d80129513fe22cf0183f`; 55 internal cases remain outside this phase's scope. Windows retains the recorded transient ConnectionReset followed by successful exact and full-file reruns; Linux check/clippy, 66 integration and 5 wire cases passed with four saved exits 0. The mapping's pending postframe row remains subject to overall acceptance g; the register separately records its authorized substitution and current runtime evidence. Four historical 690bb8ac schema-2 receipts, both 3x10 paired measurements and executing CI are recorded below. Phase 1 later completed its own final integrated-head suites/review/CI before #560 merged; version-branch stdio acceptance remains separate. Run `python rust/contract_inventory.py --phase1` and `python -m pytest evals/rust_port/test_phase1_inventory.py -q`.

The final Phase 1 judge always adds seven startup and six concurrent non-EOF candidate cells through `evals/rust_port/stdio_scenarios.py`. The pinned byte templates and platform-specific common-release order evidence are committed in `stdio_startup_contract.json` and `stdio_concurrent_orders.json`; each cell must bind the actual candidate executable. Separate response releases retain exact AB or BA order, while a common release permits only the observed final call pair orders. These additive checks do not establish final-head acceptance or replace the existing EOF corpus.

Frozen candidate `2e628b27` passes 280 Windows and 262 Linux Rust tests,
214 combined harness/audit tests, and strict eight-node stdio judges on both
platforms with zero differences. Historical receipts are
`evals/results/rust-port-phase1-rust-windows-2e628b27.json` and
`evals/results/rust-port-phase1-rust-linux-2e628b27.json`; historical receipts
retain their recorded identities. The independent code verdict is conditional
approval after four original blockers were fixed. The first hosted attempt
failed in new fixtures on both platforms and skipped parity. That attempt
remains historical evidence; executing green CI at 690bb8ac is recorded below.
Required committed full suites, final integrated-head CI and fresh review
remain gates. PR #546 may remain open and stacked under the close-out brief.

## Health, HTTP bodies and logical transfer

Mandatory /health fields: status, version, schema, storage, auth, bank, persist_errors and memory. Conditional fields: build; coordination (enabled/wake); updates (check_releases/latest_release/checked_at/unattended_clients/unattended_daemon); extractor; stall (since/reason/consecutive_failures/last_success_at); hooks_digest; init_refusal; not_ready; migration_partial; dream_tracking_error; capacity_warning; lesson_reconciliation_required; embedder; db; last_backup. `status == "ok"` (or absent status) maps to 200, other payload status to 503, payload exception to 500 with {status: error, error: text}. DB failure, init_refusal and not_ready degrade; extractor/stall/migration/backup/headroom warnings alone do not. Bank fingerprint may be null and is filled after a successful storage ping. Nested optional structures preserve the source shape.

Nested health fields: build {git_sha, dirty, built_at, source}; embedder {backend, device, dtype}; last_backup {at, age_hours, rotation}; coordination.wake carries every WakeConfig field (per_recipient_per_hour, urgent_per_sender_per_hour, nightly_total, fan_out_stagger_seconds, active_seconds). memory has source (cgroup/process/unavailable), and where available current_bytes, working_set_bytes, limit_bytes, used_fraction, near_limit, events (max/oom/oom_kill), anon_bytes, file_bytes, rss_bytes and rss_peak_bytes; absent values and null limits remain distinct.

Console POST body limits are 262144 bytes except /api/facts/set, /api/consolidate and /api/supersede (4194304 bytes); all /api/coordination/<action> bodies are 32768 bytes; hook session-end is 16384 bytes; POST /api/pair is 1024 bytes. GET hooks consume query/header inputs, and woke/subagent POST hooks use query/header inputs rather than parsing a body. /api/agents?view=coordination returns the coordination roster projection rather than the default agent view. Unknown routes and method errors remain oracle behavior.

`EXPORTED_TABLES` (26 tables): `meta`, `episodes`, `entries`, `entry_reinstatement_decisions`, `entities`, `entity_aliases`, `relations`, `edges`, `edge_evidence`, `edge_proposals`, `entity_proposals`, `entity_kinds`, `dismissed_pairs`, `facts`, `world_facts`, `lessons`, `outcome_signals`, `communities`, `entity_communities`, `memory_traces`, `memory_trace_invalidations`, `entity_sources`, `merge_decisions`, `chronicle_events`, `curation_judgments`, `store_decisions`.

`EXCLUDED_TABLES` (14 tables): `dream_runs`, `dream_run_slots`, `retrieval_events`, `retrieval_uses`, `slot_reads`, `lesson_search_events`, `client_sessions`, `coordination_agents`, `coordination_messages`, `coordination_events`, `coordination_leases`, `coordination_lease_waiters`, `coordination_wakes`, `principals`.

Also exclude outcome_signals.used_ids and bank-local meta keys: schema_version, active_session_pointer, dream_ack_secret_v1, coordination_hlc_highwater, coordination_bank_id, writer_lease_epoch, and every *_schema_version key. Import requires all exported tables empty except meta and builtin relations; excluded runtime tables never travel in logical exports.

Before importing exported metadata, import deletes the target's
`curation_listing_spelling_v2` key. A fresh target daemon may already have set
that one-time migration marker; only the export's value must survive import.

## Configuration sections

One row per dataclass in utils/config.py, including nested band specs and aggregate sections; every declared field is recorded in contract-inventory.json. All rows preserve missing/YAML/default/env precedence, type/error/coercion and validation behavior at the source reader.

| Section | Declared fields | Phase | Status |
|---|---:|---|---|
| `EmbeddingConfig` | 9; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MIRASBandSpec` | 6; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MIRASConfig` | 2; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `ReferenceConfig` | 5; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `NLIConfig` | 4; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `BM25Config` | 7; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `RerankerConfig` | 5; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `DreamConfig` | 41; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `DeepDreamConfig` | 49; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `CortexConfig` | 11; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `LessonsConfig` | 11; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `RetrievalLogConfig` | 3; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `CompactionConfig` | 3; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MetaFilterConfig` | 1; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `GraphInsightConfig` | 8; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `TracesConfig` | 2; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `ScopesConfig` | 2; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `RecallConfig` | 12; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `SearchConfig` | 6; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `McpConfig` | 2; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MemoryConfig` | 29; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `ContextConfig` | 1; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `TimeConfig` | 1; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `StorageConfig` | 1; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MemoryPolicyConfig` | 2; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `WakeConfig` | 6; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `CoordinationConfig` | 8; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `UpdatesConfig` | 4; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |
| `AppConfig` | 8; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |

| `MaintainerConfig` | 3; see `phase1-contract-inventory.json` | 1/2/3/4/5 | deferred |

## Environment variable appendix

This conservative reader/reference inventory covers concrete PSEUDOLIFE_* variables (including internal names with a leading underscore) in tracked package, ops and plugin executable/configuration source. It includes declarations, propagation and comments beside dynamic readers as a superset of direct reads; values are never recorded. Imported constant-key readers and the fourteen autostart keys constructed from KINDS prefixes are resolved explicitly. Prefix families are excluded from the variable count; generated Console assets and Markdown prose are excluded. contract-inventory.json records deduplicated paths and the audit detects missing names or reader paths.

| Variable | Reader/reference source paths |
|---|---|
| `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` | `pseudolife_memory/service.py` |
| `PSEUDOLIFE_AGENT_COORDINATION` | `ops/setup-codex-coordination.py`, `ops/setup-codex-hooks.py`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/hooks.json`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board-guard.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/briefing_cli.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_LABEL` | `pseudolife_memory/codex_coordination.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_PROJECT` | `ops/wsl-suite.ps1`, `pseudolife_memory/codex_coordination.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_STATE` | `ops/setup-codex-coordination.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_STATE_DIR` | `ops/install.ps1`, `ops/install.sh`, `ops/setup-codex-coordination.py`, `pseudolife_memory/codex_coordination.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_TASK` | `pseudolife_memory/codex_coordination.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_WAKE` | `ops/setup-codex-coordination.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_AGENT_WAKE_HOOK` | `plugin/hooks/hooks.json`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/stop-wake.sh`, `pseudolife_memory/doctor_cli.py` |
| `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` | `plugin/hooks/stop-wake.sh` |
| `PSEUDOLIFE_BACKUP_MIRROR` | `ops/backup.ps1`, `ops/backup.sh`, `ops/install-backup-task.ps1` |
| `PSEUDOLIFE_BACKUP_MIRROR_KEEP` | `ops/backup.ps1`, `ops/backup.sh` |
| `PSEUDOLIFE_BANK_VOLUME` | `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `ops/migrate-pg18.ps1`, `pseudolife_memory/compose/docker-compose.yml` |
| `PSEUDOLIFE_BENCH_DB` | `pseudolife_memory/storage/schema.py` |
| `PSEUDOLIFE_BOARD_HARNESS_NAMES` | `pseudolife_memory/codex_coordination.py`, `pseudolife_memory/harness_names.py` |
| `PSEUDOLIFE_BUILD_DIRTY` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_BUILD_GIT_SHA` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_BUILD_SOURCE` | `ops/Dockerfile.daemon`, `pseudolife_memory/daemon.py`, `pseudolife_memory/web/session_hook.py` |
| `PSEUDOLIFE_BUILD_TIME` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_CLI` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_HOST` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_LOG` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_MODEL` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_PORT` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_PROMPT_FILE` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CLAUDE_SHIM_PYTHON` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_BIN` | `ops/setup-codex-coordination.py`, `pseudolife_memory/client_updates.py`, `pseudolife_memory/codex_doorbell.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_CODEX_DOORBELL` | `ops/setup-codex-coordination.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_CODEX_HOOK` | `ops/setup-codex-hooks.py`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board-guard.sh`, `plugin/hooks/subagent-board.sh` |
| `PSEUDOLIFE_CODEX_SERVER_TOKEN` | `ops/setup-codex-coordination.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_CODEX_SERVER_URL` | `ops/setup-codex-coordination.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_CODEX_SHIM_CLI` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_SHIM_HEALTH_TTL` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_SHIM_HOST` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_SHIM_LOG` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_SHIM_MODEL` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_SHIM_PORT` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_CODEX_SHIM_PYTHON` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_DAEMON_EXEC` | `pseudolife_memory/daemon_exec.py` |
| `PSEUDOLIFE_DAEMON_MEM_LIMIT` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py` |
| `PSEUDOLIFE_DESKTOP_TOKENS_SOURCE` | `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_DESKTOP_TOKEN_SOURCE` | `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_DIGEST_DIR` | `plugin/hooks/coordination-prompt.sh`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/briefing_cli.py`, `pseudolife_memory/coordination_identity.py`, `pseudolife_memory/wait_mail_cli.py` |
| `PSEUDOLIFE_DOCKER` | `pseudolife_memory/daemon_exec.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_DREAM_API_KEY` | `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/memory/recall.py` |
| `PSEUDOLIFE_DREAM_BASE_URL` | `ops/docker-compose.yml`, `ops/install-codex-shim-autostart.ps1`, `ops/install-codex-shim-autostart.sh`, `ops/install-shim-autostart.ps1`, `ops/install-shim-autostart.sh`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/memory/recall.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_DREAM_EXTRACTOR_MODE` | `ops/docker-compose.yml`, `ops/install-codex-shim-autostart.ps1`, `ops/install-codex-shim-autostart.sh`, `ops/install-shim-autostart.ps1`, `ops/install-shim-autostart.sh`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py` |
| `PSEUDOLIFE_DREAM_FALLBACK_API_KEY` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/utils/config.py`, `pseudolife_memory/web/config_io.py` |
| `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL` | `ops/docker-compose.yml`, `ops/install-codex-shim-autostart.ps1`, `ops/install-codex-shim-autostart.sh`, `ops/install-shim-autostart.ps1`, `ops/install-shim-autostart.sh`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/utils/config.py` |
| `PSEUDOLIFE_DREAM_FALLBACK_MODEL` | `ops/docker-compose.yml`, `ops/install-codex-shim-autostart.ps1`, `ops/install-codex-shim-autostart.sh`, `ops/install-shim-autostart.ps1`, `ops/install-shim-autostart.sh`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py` |
| `PSEUDOLIFE_DREAM_MAX_TOKENS` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/utils/config.py` |
| `PSEUDOLIFE_DREAM_MODEL` | `ops/docker-compose.yml`, `ops/install-codex-shim-autostart.ps1`, `ops/install-codex-shim-autostart.sh`, `ops/install-shim-autostart.ps1`, `ops/install-shim-autostart.sh`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/memory/recall.py`, `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/dream.py`, `pseudolife_memory/utils/config.py` |
| `PSEUDOLIFE_EMBEDDING_CPU_DTYPE` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/memory/embedding.py`, `pseudolife_memory/utils/config.py` |
| `PSEUDOLIFE_EXTRACTOR_SLEEP_IDLE_SECONDS` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml` |
| `PSEUDOLIFE_HANDLE_RESUME_SECONDS` | `pseudolife_memory/service.py` |
| `PSEUDOLIFE_IMAGE_TAG` | `ops/docker-compose.ghcr.yml`, `pseudolife_memory/compose/docker-compose.ghcr.yml`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_INSTALLER_TOKEN` | `ops/client_credentials.py`, `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_INSTALLER_TOKENS` | `ops/client_credentials.py`, `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_JUDGE_API_KEY` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/service_dream.py`, `pseudolife_memory/web/config_io.py` |
| `PSEUDOLIFE_JUDGE_SECOND_API_KEY` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/service_dream.py`, `pseudolife_memory/utils/config.py`, `pseudolife_memory/web/config_io.py` |
| `PSEUDOLIFE_LEASES_HELD` | `pseudolife_memory/lease_cli.py` |
| `PSEUDOLIFE_LEASE_LOCK_DIR` | `pseudolife_memory/lease_cli.py`, `pseudolife_memory/os_lock.py` |
| `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` | `pseudolife_memory/writer_context.py` |
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/utils/heap_trim.py` |
| `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` | `pseudolife_memory/mcp_server.py` |
| `PSEUDOLIFE_MCP_CONFIG` | `ops/dedup_cortex.py`, `pseudolife_memory/maintainer_setup.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/web/config_io.py` |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `ops/client_credentials.py`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/remote-suite.ps1`, `ops/setup-codex-coordination.py`, `ops/setup-codex-hooks.py`, `ops/wsl-suite.ps1`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/client_config.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/daemon_url.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/runtimes.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_cli.py`, `pseudolife_memory/tunnel_runtime.py`, `pseudolife_memory/unattended_update.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_MCP_DATABASE_URL` | `ops/backfill_edge_confidence.py`, `ops/dedup_cortex.py`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `ops/measure_reverify_population.py`, `ops/migrate_drop_age.py`, `ops/migrate_embeddings.py`, `ops/restore_from_pt.py`, `ops/retire_by_writer.py`, `pseudolife_memory/backup_cli.py`, `pseudolife_memory/board_audit_cli.py`, `pseudolife_memory/cli.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/coordination_recovery.py`, `pseudolife_memory/daemon.py`, `pseudolife_memory/daemon_exec.py`, `pseudolife_memory/invite_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/maintainer_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/service.py`, `pseudolife_memory/storage/embedded_pg.py`, `pseudolife_memory/storage/schema.py`, `pseudolife_memory/test_login_cli.py`, `pseudolife_memory/transfer_cli.py` |
| `PSEUDOLIFE_MCP_DATA_DIR` | `ops/Dockerfile.daemon`, `ops/dedup_cortex.py`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `ops/restore_from_pt.py`, `pseudolife_memory/backup_cli.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/maintainer_setup.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/storage/embedded_pg.py`, `pseudolife_memory/transfer_cli.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_MCP_HOST` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py` |
| `PSEUDOLIFE_MCP_NO_SPAWN` | `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/setup-codex-coordination.py`, `pseudolife_memory/client_updates.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/runtimes.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_runtime.py` |
| `PSEUDOLIFE_MCP_PORT` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/invite_cli.py` |
| `PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS` | `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_MCP_SHARED_HOST` | `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_MCP_STORAGE` | `pseudolife_memory/mcp_server.py`, `pseudolife_memory/storage/embedded_pg.py` |
| `PSEUDOLIFE_MCP_TIER_MAP` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/invite_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/principals.py`, `pseudolife_memory/toolset_tiers.py` |
| `PSEUDOLIFE_MCP_TOKEN` | `ops/client_credentials.py`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/remote-suite.ps1`, `ops/setup-codex-coordination.py`, `ops/setup-codex-hooks.py`, `ops/wsl-suite.ps1`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/board_status.py`, `pseudolife_memory/briefing_cli.py`, `pseudolife_memory/cli.py`, `pseudolife_memory/client_config.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/credentials.py`, `pseudolife_memory/daemon.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/episode_cli.py`, `pseudolife_memory/expose_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/maintainer_setup.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/principal_store.py`, `pseudolife_memory/principals.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_cli.py`, `pseudolife_memory/unattended_update.py`, `pseudolife_memory/update_cli.py`, `pseudolife_memory/web/api.py` |
| `PSEUDOLIFE_MCP_TOKENS` | `ops/client_credentials.py`, `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/daemon.py`, `pseudolife_memory/expose_cli.py`, `pseudolife_memory/invite_cli.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/principal_store.py`, `pseudolife_memory/principals.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/unattended_update.py`, `pseudolife_memory/update_cli.py`, `pseudolife_memory/utils/config.py`, `pseudolife_memory/web/routes.py`, `pseudolife_memory/writer_context.py` |
| `PSEUDOLIFE_MCP_TOKEN_FILE` | `ops/client_credentials.py`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/setup-codex-coordination.py`, `ops/setup-codex-hooks.py`, `ops/wsl-suite.ps1`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/cli.py`, `pseudolife_memory/client_config.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/credentials.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/storage/coordination.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_cli.py`, `pseudolife_memory/tunnel_runtime.py`, `pseudolife_memory/unattended_update.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_MCP_TOOLSET` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/toolset_tiers.py` |
| `PSEUDOLIFE_MCP_TRUST_BIND` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/mcp_server.py` |
| `PSEUDOLIFE_MODELS_DIR` | `pseudolife_memory/memory/nli.py` |
| `PSEUDOLIFE_PLUGIN_DIR` | `ops/Dockerfile.daemon`, `pseudolife_memory/plugin_hooks.py` |
| `PSEUDOLIFE_PRIVATE_FILE` | `plugin/hooks/coordination-start.sh`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh` |
| `PSEUDOLIFE_RECALL_DRIVER` | `pseudolife_memory/service.py`, `pseudolife_memory/utils/config.py` |
| `PSEUDOLIFE_RELEASE_CHECK` | `pseudolife_memory/release_check.py` |
| `PSEUDOLIFE_REQUIRE_TEST_POSTGRES` | `ops/ci_tests.sh`, `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_SESSION_IDLE_SECONDS` | `pseudolife_memory/mcp_server.py` |
| `PSEUDOLIFE_SESSION_REAP_SECONDS` | `pseudolife_memory/mcp_server.py` |
| `PSEUDOLIFE_SESSION_RESUME_SECONDS` | `pseudolife_memory/service.py` |
| `PSEUDOLIFE_SHIM_CLAUDE_CLI` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_SHIM_CODEX_CLI` | `ops/shim_autostart.py` |
| `PSEUDOLIFE_SHIM_LAUNCHER` | `plugin/hooks/coordination-prompt.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-start.sh`, `pseudolife_memory/runtimes.py` |
| `PSEUDOLIFE_SHIM_PYTHON` | `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_SHIM_RUNTIMES` | `pseudolife_memory/runtimes.py` |
| `PSEUDOLIFE_SHIM_USER_BIN` | `pseudolife_memory/runtimes.py` |
| `PSEUDOLIFE_STATE_VOLUME` | `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml` |
| `PSEUDOLIFE_SUITE_COMMIT` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_DISPATCHED` | `ops/remote-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_ENV_FILE` | `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_BOARD_HARNESS_NAMES` | `pseudolife_memory/codex_coordination.py`, `pseudolife_memory/harness_names.py` |
| `PSEUDOLIFE_SUITE_GIT_COMMON` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_KEEP` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_PRUNE` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_PRUNE_DAYS` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_LEASE` | `ops/wsl-suite.sh`, `pseudolife_memory/lease_cli.py` |
| `PSEUDOLIFE_SUITE_LOCK` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_SUITE_LOCK_DIR` | `ops/wsl-suite.sh`, `pseudolife_memory/board_audit_cli.py`, `pseudolife_memory/lease_cli.py` |
| `PSEUDOLIFE_SUITE_NAME` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_PRUNE` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_PRUNE_DAYS` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_PYTHON` | `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_REMOTE` | `ops/remote-suite.ps1` |
| `PSEUDOLIFE_SUITE_RUN_ID` | `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_SLOTS` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_SUITE_VENV` | `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_WHERE` | `ops/remote-suite.ps1` |
| `PSEUDOLIFE_SUITE_WINDOWS` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TEST_CUDA` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TEST_DATABASE_URL` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/storage/schema.py` |
| `PSEUDOLIFE_TEST_EMBEDDER` | `ops/ci_tests.sh`, `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TEST_PG_HOST_PORT` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_TEST_PG_LOGIN_FILE` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh`, `pseudolife_memory/test_login_cli.py` |
| `PSEUDOLIFE_TEST_PG_PASSWORD` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `pseudolife_memory/test_login_cli.py` |
| `PSEUDOLIFE_TEST_PG_USER` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `pseudolife_memory/test_login_cli.py` |
| `PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE` | `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_runtime.py` |
| `PSEUDOLIFE_WRITER_ID` | `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/setup-codex-coordination.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/service.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_runtime.py`, `pseudolife_memory/writer_context.py` |
| `PSEUDOLIFE_WSL_DISTRO` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_WSL_SUITE_SOURCE` | `ops/wsl-suite.ps1` |
| `_PSEUDOLIFE_PRODUCTION_DB` | `pseudolife_memory/storage/schema.py` |




## Historical phase 1 paired measurement evidence

Paired measurements at `2e628b27` completed three repeats of ten samples per
arm on each platform. All 120 timing/RSS launches per platform exited cleanly.
The real Python SDK preflight remains inside Rust startup; its descendants
contribute to sampled process-tree RSS. Receipts are
`evals/results/rust-phase1-measurement-windows-2e628b27.json` and
`evals/results/rust-phase1-measurement-linux-2e628b27.json`.

| Platform | Metric | Python p50 / p95 | Rust p50 / p95 | Python floor p50 / p95 | Rust floor p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| Windows | First stdout frame, ms | 811.794 / 852.468 | 871.665 / 903.501 | 8.382 / 26.310 | 12.934 / 15.408 |
| Windows | Sampled peak tree RSS, bytes | 87,924,736 / 91,459,584 | 100,642,816 / 100,904,960 | 143,360 / 4,829,184 | 131,072 / 24,576 |
| Windows | Executable file, bytes | 274,712 / 274,712 | 13,487,616 / 13,487,616 | 0 / 0 | 0 / 0 |
| Linux | First stdout frame, ms | 704.398 / 724.289 | 686.487 / 699.918 | 2.012 / 113.563 | 6.224 / 33.229 |
| Linux | Sampled peak tree RSS, bytes | 73,633,792 / 73,879,552 | 74,027,008 / 74,166,272 | 53,248 / 135,168 | 57,344 / 36,864 |
| Linux | Executable file, bytes | 21,662,864 / 21,662,864 | 16,644,008 / 16,644,008 | 0 / 0 | 0 / 0 |

Floors are observed ranges of three identical-input repeat-block quantiles,
not confidence intervals. Each arm retains its own source, runtime and
descriptive floors. RSS is a sampled lower
bound and can miss peaks between 5 ms polls. Size covers only the selected
executable, excluding Python runtime/dependencies; it is not deployment footprint.
The pair uses warm filesystem caches and an empty loopback fixture, with no
PostgreSQL, models, daemon spawn or coordination. Desktop activity is uncontrolled.
Historical r5 uses a different timing/RSS boundary and cannot substitute for
this pair. These source-bound measurements establish no general speed claim
or Phase 1 acceptance.

## Schema 54 upstream surfaces

These daemon-side maintainer registrations were added at the close-out oracle pin;
no Rust implementation or DDL change is claimed.

| Method | Route | Phase | Status | Oracle tests |
|---|---|---|---|---|
| GET | /api/maintainer | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/challenge | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/enrol | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/send | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/role | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/cancel | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/revoke | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| POST | /api/maintainer/repudiate | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| GET | /api/maintainer/sent | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |
| GET | /api/maintainer/inbox | 4 | deferred | `test_maintainer_web.py`, `test_maintainer_messages.py`, `test_maintainer_roles.py` |

## Current phase 1 close-out evidence

Candidate `690bb8ac` has four complete schema-2 Windows/Linux Python self-replay
and Rust stdio receipts, each with eight actual pytest passes, 32 executable-bound
cells, all judge sections, zero retained-wire differences and verified cleanup.
The separate five dispatcher passes per OS use Python; they establish no Rust
CLI coverage. Current receipt links, before/after numeric tables and the four
executed CI job durations are recorded in PORT-STATE.md. The SDK preflight
remains retired-by-decision. The historical evidence used daemon launch, update
scheduling and Rust wake-reason predicates as named substitutions. Phase 2b
withdraws the wake-reason substitution, restores pinned Unicode 14 predicates,
and keeps their final-head acceptance pending; daemon launch and explicit
interpreter update scheduling remain scoped substitutions. Installer-backed
client update execution is Phase 5 deferred. MCP-WIRE, MCP-TIER,
SHIM-LIFECYCLE, SHIM-AUTH and SHIM-BOARD now record bounded implementation
acceptance in the separate close-out ledger at `8b7a6c95`. MCP-TIER acceptance
covers only the Phase 1 client boundary; daemon tier enforcement remains Phase 3
deferred. The historical SDK retirement is unchanged; the Phase 2b register
corrections and first retention-only CI read are recorded in PORT-STATE.md.
The historical failed CI attempt and 2e receipts remain historical evidence.
PR #546 remained open and stacked for that historical acceptance. Phase 2b
requires master integration and a new pin after its maintainer-owned merge.
Both committed implementation-head suites and all fourteen
integrated CI checks passed at `8b7a6c95`, as recorded in PORT-STATE.md.
Those documentation-head suites, integrated CI and final review were historical
requirements before #560 merged at `465d75d9`, validated at `a3e95642`.
The complete prepared-state `phase1-test-buckets.json` blob remains unchanged.
Its two “not executed” reasons describe that historical inventory state; the
74b executions and current hosted acceptance are distinguished above and in
the final capture packet. The separate acceptance ledger records bounded statuses.
Only these four channel remainder nodes are deferred to Phase 2:

- `tests/test_shim_board_retry.py::test_late_adapter_feeds_the_channel_inbox`
- `tests/test_shim_channel.py::test_channel_proxy_preserves_upstream_instructions_and_tools`
- `tests/test_shim_channel.py::test_channel_cli_is_explicit_and_leaves_ordinary_shim_default`
- `tests/test_shim_channel.py::test_codex_channel_keeps_eager_channel_adapter`

## Version admission correction and scoped acceptance

The 2026-10-07 decision corrected the brief-origin own-executable substitution: a runtime must also contain its named console as a file, `Scripts/pseudolife-mcp.exe` on Windows or `bin/pseudolife-mcp` on POSIX, matching Python's `runtimes.py::list_runtimes`. The candidate retains its own executable, layout and canonical/as-written runtime matching. Valid fixtures seed the named console without changing positive assertions; new missing-console and directory controls constrain admission. These source and fixture changes are not covered by the earlier receipts.

Published `95d5402d` has all 14 checks green. [Rust/Parity run 37489572403](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37489572403) passed all four jobs and [general run 37489572384](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37489572384) passed all five jobs. The actual merge-test commit was `2caec278`, whose tree equals `95d5402d`'s `8156ec27`; neither identity is relabelled. On each OS, artifacts record 18 unique candidate nodes (10 stdio, eight CLI) and eight Python CLI nodes, each once/passed/non-skipped; no Python ten-stdio original lane was reported. Rust default/no-default contracts pass Linux 303/295 and Windows 314/306, with zero skips. These results validate the published own-executable criterion, not the subsequent named-console correction or warm measurement instrument.

The Windows installed Rust/Python medians of approximately 269/159 ms at `95d5402d` use the old protocol and retain the 109.798 ms regression, per-arm repeat floors and recorded desktop-load context. The bee4 110.030 ms and earlier 278/156 ms findings remain historical evidence. Fresh-copy/reuse diagnostics show a launch/cache condition affecting help and version; they do not establish its cause or prove a security-provider explanation. The final `df2dbf8a` proof warms both arms under the corrected protocol, retains exact byte/file/environment controls and records three repeats of ten paired samples with per-arm floors. Old results are not discarded or relabelled as warm-protocol captures.

Scoped CLI-VERSION is ported at merged master `df2dbf8adbe0d1fc84b87fb0d05da6457ba7d9ff`, tree `288237846a3923ad49dfb6cf11d099935461a4d8`; the [final packet](../evals/results/rust-phase2d-version-df2dbf8a/README.md) supplies fresh both-OS console/corpus/argv/original-node and warm-pair proof after #608 and #600 merged. The output contract is UTF-8 stdout/stderr with platform newlines; the harness sets `PYTHONIOENCODING=utf-8` on the Python arm. Both installed fixtures use actual non-ASCII homes. Locale/default encoding parity, non-UTF-8/surrogate argv and the named manifest deferrals remain deferred; the candidate is not installed. Other mode rows keep their own gates. This evidence-only carrier retains the executed identities and requires its own independent review and hosted checks; it claims no new stdio/full-suite proof.

The [frozen console and warm-image packet](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/ceb1e3bb0fbe84afeec6dc22f7b10cdd4c4379da/evals/results/rust-phase2d-version-5220b5ee/README.md) records 28 exact CLI output/state cases and 112 rejected controls per OS, including missing named-console fallback and actual non-ASCII homes. Both arms retain their file identities across six untimed warm starts and three blocks of ten alternating timed pairs per layout; the detailed protocol and timing qualifications remain in PORT-STATE.md. These executions belong to `5220b5ee4cf4b7ae235ee070109b8de17fa3a1d0`, tree `11e7267bd7a884ace22f57ebf5afd6c77ee25f91`; the historical documentation and master-forward carrier did not claim a new execution. Current scoped acceptance is bound to the final `df2dbf8a` packet above.

## Low-priority deferred version observations

These source-level observations remain deferred and add no merge gates.

- **Aliased runtime directories:** when multiple six-digit directory names resolve to the executable's own runtime, Rust `version.rs::runtime_line` selects the first complete matching entry from `read_dir`; Python `runtimes.py::list_runtimes` sorts by numeric sequence before `running_runtime` selects its first path match. The named runtime can therefore differ for multiple complete aliases (symlinks or junctions) to the same directory. This does not describe separate, unaliased installer runtimes.
- **Matching marker failures:** after Rust has matched the executable's own directory, an unreadable, invalid-JSON or non-object marker ends the whole scan with the package-version fallback; Python skips that entry and continues. A difference requires a later usable matching entry, for example another alias with a usable path or transient filesystem state; a single incomplete runtime falls back in both arms. Malformed markers in unrelated runtime directories are skipped before Rust reads them.
- **Platform selection:** Rust chooses runtime shape with `cfg!(windows)`; Python's `_windows(layout)` uses the launcher's `.exe` suffix. Python `default_layout` rejects a wrong-suffix override for the host and `_print_version` catches that rejection, while Rust `runtime_root` returns early, so ordinary CLI wrong-suffix overrides fall back in both arms. The remaining implementation distinction concerns manually constructed Python `Layout` values outside that ordinary admission path.

The subsequent integration of master `7b0abf921fe4bd4ef1c84864eef118309b400705` inherits the reviewed #607 users runtime/dependency changes without a conflict: 5 of the 341 capture-bound paths differed at `181a50d1` from executed `5220b5ee` (`rust/Cargo.lock`, `rust/Cargo.toml`, `rust/shim/Cargo.toml`, `rust/shim/src/board/state.rs`, `rust/shim/src/credentials.rs`), while the other 336 and all 146 original production/test guard paths remain exact. The `5220b5ee` measurements remain historical source-bound executions and do not validate the inherited users implementation or this combined tree; current hosted CI and fresh review are required.

Integration of master `57c007ff292cffdbb6755911bd54de837a00f9a5` retains #611 readiness evidence, #607 users and #608 executable resolution. Of the 341 paths bound to executed `5220b5ee`, 11 now differ (`evals/rust_port/phase1.py`, `evals/rust_port/phase1_receipts.py`, `evals/rust_port/stdio_judge.py`, `rust/Cargo.lock`, `rust/Cargo.toml`, `rust/shim/Cargo.toml`, `rust/shim/src/board/state.rs`, `rust/shim/src/credentials.rs`, `rust/shim/src/lifecycle.rs`, `rust/shim/tests/auth_windows_private_state/mod.rs`, `rust/shim/tests/nonboard_final_assertions.rs`); 330 remain exact, including all 146 original production/test guard paths. Incoming changes from reviewed `181a50d1` affect 9 bound paths. The old timing images and captures remain historical executions of `5220b5ee`, without acceptance claims for this combined tree.

## Lease phase 2c policy preparation

CLI-LEASE and the lease mode row remain deferred. The named decisions in
PORTING.md supersede historical references to unapproved groups 1, 3, 4 and 13;
they do not promote any historical receipt to current-head acceptance. The
23 retained instances below are preparation only: 14 traceback matches under
the approved contract (12 newly admitted and two earlier approved), eight
candidate refusals still requiring implementation, and one clock comparison
incomplete without captured invocation windows and a second oracle run.

| Case ID | Named policy | Governed fields | Retained disposition |
| --- | --- | --- | --- |
| `I3-0-text` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-0-json` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-1-text` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-1-json` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-2-text` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-2-json` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-3-text` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-3-json` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-4-text` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `I3-4-json` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `list-expected-extreme` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `list-waiter-extreme` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `approved-timeout-overflow-traceback` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `C3-large-list-closed-stdout` | `python-traceback-not-contract` | `stderr.traceback_header, stderr.traceback_frames` | historical-policy-match; final-head recapture pending |
| `D3-DEL-agent_id` | `http-forbidden-input-refused` | `agent_id` | implementation-pending; final-head recapture pending |
| `D3-DEL-credential` | `http-forbidden-input-refused` | `credential` | implementation-pending; final-head recapture pending |
| `D3-CR-agent_id` | `http-forbidden-input-refused` | `agent_id` | implementation-pending; final-head recapture pending |
| `D3-CR-credential` | `http-forbidden-input-refused` | `credential` | implementation-pending; final-head recapture pending |
| `D3-LF-agent_id` | `http-forbidden-input-refused` | `agent_id` | implementation-pending; final-head recapture pending |
| `D3-LF-credential` | `http-forbidden-input-refused` | `credential` | implementation-pending; final-head recapture pending |
| `D3-NUL-agent_id` | `http-forbidden-input-refused` | `agent_id` | implementation-pending; final-head recapture pending |
| `D3-NUL-credential` | `http-forbidden-input-refused` | `credential` | implementation-pending; final-head recapture pending |
| `lease-run-existing-successful-child` | `nondeterministic-bytes-semantic` | `post_files_b64/ran.json/t` | incomplete-missing-invocation-windows; final-head recapture pending |

The JSON ledger retains each source receipt basename and SHA256, platform,
terminal/diagnostic bytes, named field and exact shape/bound. The raw receipts
remain unchanged; missing metadata is explicitly null. No outside-rule retained
case was found in this scoped inventory. This is not a claim about unexamined
inputs, current CI, full mode coverage or final-head executable behavior.

Implementation after the version gate:

- Add a fatal forbidden-header failure path in `shim/src/cli/lease/board.rs`,
  inspecting the approved fields before constructing headers. Propagate it
  through `run.rs` and `view.rs` as the named diagnostic/exit 1, before local
  fallback, child launch or lock-file creation. Preserve the exact ordinary
  non-ASCII and surrogate behavior, and use checked header constructors.
- Add candidate-specific corpus inputs and expected refusal outputs in a new
  additive module; keep the oracle raw and label the candidate substitution.
  The retained DEL/CR/LF/NUL cases need both-platform final-head proofs.
  New required capture IDs: `D3-DEL-bearer`, `D3-CR-bearer`, `D3-LF-bearer`,
  `D3-NUL-bearer`, `D3-FOLDED-agent_id`, `D3-FOLDED-credential`,
  `D3-FOLDED-bearer`. Cover remaining C0 byte values with additive controls.
- Wire the named policies at the comparison boundary without rewriting raw
  observations. The existing corpus rejects normalization; changing that
  instrument is a later, explicit integration step, not done by this preparation.
- Retain start/end Unix windows around every arm invocation of
  `lease-run-existing-successful-child`, plus two oracle repetitions proving
  `/t` differs and all other bytes stay exact. Validate the strict decimal shape
  and own-window bound before any comparison; add successful-run timing only
  after this proof. Existing child source remains unchanged.
- Rebase only after the version branch is ported, then freeze and recapture
  each affected case at the final head on Windows and Linux, bind the resulting
  receipts and measurements, and verify routed/additive nodes in hosted CI.
  Broader host/storage/concurrency/recovery/signal gates remain deferred.

Static preparation checks do not run a daemon, child command or native binary.
Existing tests and the existing eval harness remain byte-identical.

## Lease timestamp display substitution

Lease timestamp display uses Chrono's platform local timezone for daemon-produced
Unix-second `expected_end` and `enqueued_at` values. Ordinary timestamps retain
local `HH:MM` on the current local date and `YYYY-MM-DD HH:MM` on other dates;
fractional seconds are floored before conversion. Unsupported calendar values
display `?`, including dates outside Chrono's representable domain, instead of
reproducing host-CRT date limits or CPython's calendar-year `OverflowError`.
JSON replies preserve the original numeric timestamps.

On POSIX, local-zone behavior includes supported `TZ` environment settings. On
Windows, the display uses the OS timezone; CRT-only `TZ` overrides are outside
this contract. Deterministic Rust timestamp fixtures inject their zone and
current date through an internal helper, without a production environment or
test switch. Public-binary fixtures cover ordinary current/other-day fractional
timestamps and unsupported-date text/JSON behavior. This substitution applies
only to these Unix-second lease fields; ISO holder timestamps retain their
existing scope.

The historical `list-expected-extreme` and `list-waiter-extreme` traceback rows
above retain their original diagnostics and receipt hashes. Their former
exception expectations are superseded for current candidates by this scoped
timestamp substitution; historical matches do not become current-head proof.
All other preparation policies and deferred lease actions remain separate.

## Lease forbidden-header implementation candidate

The native check/list/run candidate now propagates a fatal forbidden-header
failure before local fallback, lock creation or child launch. Instance reply
values are inspected before retaining a board session; a refused registration
therefore sends no lease or release. Environment bearers are inspected before
credential decoding, and file bearers after the existing private-file checks.
The ordinary decoder's trailing CR/LF file terminator remains outside the
header value. Checked header constructors retain ordinary non-ASCII/surrogate
failures and unrelated fallback behavior.

`evals.rust_port.lease_headers` adds executable inputs for the retained eight
instance cases, bearer/folded/all-C0 cases and field/output/file/request mutation
controls. It preserves raw pinned Python captures and separately labels the
approved refusal expectation. Historical preparation rows above retain their
old dispositions; they are not final-head acceptance receipts. CLI-LEASE
remains deferred, including successful-child invocation windows, Windows
unlock/Ctrl-C, hosted acceptance and measurements.

## Lease final7e bounded evidence and extreme-text exit rule

[The public evidence bundle](../evals/results/rust-phase2d-lease-7e6927b7/README.md)
binds executed `7e6927b7b72db62569281d3477dc8e8924c82292`, tree
`34b24d60d854e28448079bfc0c8e8eea83551d47`, to the actual debug images,
genuine pinned Python, successful-child own windows/repeat/controls, exact
nonzero child, deferred actions, release/interrupt effects and check/list
samples. Sixteen remaining policy cells per OS match their admitted scope;
the historical ledger remains unchanged. The eight D3 IDs are implemented
in the additive header corpus; final hosted header/general re-execution,
feature matrix and independent final review remain pending. Neither core
nor full CLI-LEASE is promoted by these bounded local receipts.

`python-traceback-not-contract` has one explicit additional outcome rule:
only `list-expected-extreme` and `list-waiter-extreme` text cells may return
native exit 0 and complete fixture text with `?`, empty stderr and unchanged
state when the oracle raises a validated OverflowError with exit 1. The
Linux raw exit pair is 1/0 in both cases; Windows is raw-exact 0/0. JSON
companions remain exact, including the original numeric timestamp. Any other
list exit-code difference fails. This narrow rule supersedes only the two
historical extreme-text exception expectations; raw observations are preserved.

| Platform | Complete final raw policy receipt SHA256 | Exact extreme-text scope |
| --- | --- | --- |
| Windows | `97bfcefebe9a397eb1323df10c93687dd276ac4ab128c9c0b79c98bb0171d45f` | `list-expected-extreme`, `list-waiter-extreme`: raw 0/0 |
| Linux | `0a819e13c9a1fcf8fc238f64a74447970ab413ef47b46d038faa093d55d67e44` | `list-expected-extreme`, `list-waiter-extreme`: raw 1/0, passed by named policy |

Per-cell raw response/expected hashes, actual exits, fixture bytes, original
instrument bindings and cleanup are in `policy-evidence.json`/`policy-inputs.json`
in the bundle. The new additive `test_lease_extreme_list_exit_policy.py`
rejects unrelated list exit differences and malformed/no traceback; the current
checker also replays all 32 captured cells and rejects 32 wrong-exit controls
offline. No old test file or assertion is changed.
