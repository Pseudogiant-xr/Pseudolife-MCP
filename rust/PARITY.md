# Behaviour parity register

Oracle: Python 0.15.0 at `3691f5cb75487d3fda54a6bde6fab35dcf32c681`, schema 53.
Recount at this pin: 38 MCP tools, 62 ConsoleRoutes registrations plus the
separate POST /api/pair route, eight hook endpoints, 15 coordination actions
and 25 CLI modes. The current Console is v3 at /ui/; /ui/next/ is absent.
No production Rust surface has been accepted. `ported` requires the brief's
wire, differential and measurement gates; `deferred` is unfinished;
`retired-by-decision` requires a recorded maintainer decision. No retirement
has been authorized.

Existing test files are immutable. On 2026-10-03 the maintainer selected an
external pytest plugin instead of modifying `tests/conftest.py`. The test pools
below are an inventory, not a claim that whole files can target Rust. The
executable manifest must enumerate actual selected nodes, fail closed for
unsupported mappings, and record Python-only cases explicitly.

Source paths naming a Python module without a root refer to `pseudolife_memory/`;
test names without a root refer to `tests/`. Registrations below are a source
inventory and still require runtime schema/transcript evidence.

## Parity rows

BASE and RULES record the completed phase 0 instruments and measurements; historical evidence from PR #540 retains its original pin. Phase 0b PR readiness still requires the final evidence review and green checks on its updated head, as recorded in PORT-STATE.md. All production behavior rows remain deferred. No behavior retirement is authorized. `P` means real process/wire eligible nodes; `I` means Python internals requiring additive wire cases or Rust unit equivalents; `A` means artifact/static contract. Paths below are repository-relative.

| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| BASE | 0 | Commit/host/bank/run-count RSS idle and workload, shim cold start, search/store/fact-set and send/receive p50/p95; CI lane timing. `evals/`, `.github/workflows/ci.yml` | `tests/test_eval_evidence.py`, `test_report_redaction.py` A; current-pin Linux scaling in `evals/results/rust-phase0b-daemon-scaling-linux.json`, fixed-reference CI noise in `evals/results/rust-phase0b-ci-same-head.json`; historical #540 shim/write evidence retains its original pin | ported |
| RULES | 0 | Error/type/ownership/unsafe/HTTP/SQL/float mappings; immutable manifest and trial corrections. `CLAUDE.md`, guides, atlas | `test_release_ux.py`, `test_atlas_currency.py`, `test_llms_txt.py`, `test_eval_evidence.py`, `test_i18n_readme.py` A; PORTING.md and `evals/results/rust-port-phase0b-acceptance.json`; current code reviewed after wire-span and candidate-release fixes | ported |
| MCP-WIRE | 1 | initialize instructions/capabilities, tool annotations/schema/help, supported revision negotiation, JSON-RPC errors, streams, cancellation, notifications. `mcp_server.py`, `shim.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py` mixed P/I; `test_mcp_server.py`, `test_mcp_string_arguments.py`, `test_mcp_client_neutrality.py`, `test_mcp_stdio_errlog.py` I/A; help fixture A | deferred |
| MCP-TIER | 1/3 | Principal-scoped list filtering, 12h TTL/precedence, cumulative 9/24/38 visibility, hidden calls accepted, list_changed. `toolset_tiers.py`, `mcp_server.py` | `test_shim.py` notification nodes P; `test_toolset_tiers.py`, `test_mcp_server.py` I; add 38-tool differential schema/argument/error corpus | deferred |
| SHIM-LIFECYCLE | 1 | Canonical origin validation, discovery/probe, local spawn/reuse, no-spawn waiting, ownership, child exit and recovery, version notices and gated client updates. `shim.py`, `daemon_url.py`, `runtimes.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py`, `test_connection_loss_recovery.py`, `test_shim_runtimes.py` mixed; `test_client_environment.py`, `test_credentials.py`, `test_version_handshake.py`, `test_update_offer.py` I/mixed | deferred |
| SHIM-AUTH | 1 | Token-file precedence and reload, unsafe/malformed files fail closed, writer/session/agent/bank/principal headers; sanitized uncertain-write failures without replay. `credentials.py`, `writer_context.py`, `shim.py` | `test_shim_transport_recovery.py` mixed; `test_writer_keying.py`, `test_principals.py`, `test_credentials.py`, `test_session_identity.py` I; add wire rotation and malformed byte probes | deferred |
| SHIM-BOARD | 1 | Registration, scoped identity, addressed-mail continuity, shared-host refusal, local file claims, board retry, default doorbells and optional delivery invoked by the shim; standalone channel mode remains phase 4. `coordination_adapter.py`, `coordination_identity.py`, `codex_doorbell.py`, `codex_delivery.py`, `repository_claims.py` | `test_shim_board_retry.py`, `test_shim_channel.py`, `test_channel.py`, `test_coordination_roster_hygiene.py`, `test_codex_doorbell.py`, `test_codex_delivery.py`, `test_coordination_adapter.py`, `test_repository_claims.py` I/mixed; add full binary identity/attachment/recovery tests | deferred |
| CLI-DISPATCH | 1/2/3/4/5 | All 25 modes, aliases, unknown-mode exit 2, help bytes and runtime version metadata, light import/startup. `cli.py` and help fixture | `test_cli_dispatch.py`, `test_release_ux.py`, `test_client_install_ux.py` I/A; add argv subprocess records | deferred |
| CLI-LEASE | 2 | OS lock truth, crash release, FIFO tickets, board mirror, check/run/hold/list/break and child status. `lease_cli.py`, `os_lock.py` | `test_lease_cli.py`, `test_lease_cli_board.py`, `test_coordination_leases.py`, `test_coordination_leases_api.py` mixed/I; subprocess nodes suitable after dispatcher audit | deferred |
| CLI-MAIL | 2 | `.seen`/digest watermark race, exits 0 mail/3 timeout/2 setup, output and durable wait cleanup. `wait_mail_cli.py`, `private_state.py` | `test_wait_mail_cli.py`, `test_coordination_mail_continuity.py`, `test_stop_wake_hook.py` mixed/I/A | deferred |
| CLI-HOOK | 2 | Briefing text and bounded hook JSON, memory-change note; episode start/end CLI exit/output. `briefing_cli.py`, `episode_cli.py`, `web/session_hook.py` | `test_briefing.py`, `test_episode_cli.py`, `test_memory_changes_hook.py`, `test_web.py` I/mixed; add fake HTTP server subprocess cases | deferred |
| CLI-DOCTOR | 2 | Read-only diagnostics default, disposable proof explicit, daemon and identity/transport readiness, no incidental mutation. `doctor_cli.py`, `coordination_proof.py`, `wake_liveness.py` | `test_doctor_cli.py`, `test_doctor_coordination.py`, `test_coordination_proof.py`, `test_coordination_probe.py` I/mixed | deferred |
| CLI-CONNECT | 2 | Origin/credential validation, verify-before-write, all-or-nothing backup/rollback/dry-run; connect re-points existing client registrations and never creates a registration. `connect_cli.py`, `client_config.py`, `client_updates.py` | `test_connect.py`, `test_client_credentials_setup.py`, `test_client_sessions.py`, `test_client_environment.py`, `test_client_install_ux.py` I/mixed | deferred |
| CLI-AUDIT | 2 | Export/verify/redact/stats, chain/head semantics, reports and body-expiry distinctions. `board_audit_cli.py`, `board_audit_stats.py`, `storage/coordination.py` | `test_board_audit_cli.py`, `test_board_audit_stats.py`, `test_coordination_audit.py`, `test_coordination_report.py` I/mixed | deferred |
| CLI-BACKUP | 2 | pg_dump + compressed state, no create-on-backup, exclusions and post-success-only own-file rotation. `backup_cli.py` | `test_backup_cli.py`, `test_ops_backup_integrity.py`, `test_bank_dumps.py` I/mixed; add fake pg_dump subprocess fixtures | deferred |
| CLI-TRANSFER | 2 | ZIP/JSONL manifest; exact exported/excluded table rosters in the transfer appendix; float4/ids/HLC/JSONB/time/sequences; import refuses a non-empty bank (only meta and builtin relations are exempt), dimension/column mismatches and other live connections. `transfer_cli.py` | `test_transfer_cli.py` direct calls I/DB; add real CLI export/import against disposable banks | deferred |
| CLI-MODE-OWNERSHIP | 1/2/3/4/5 | All 25 modes have explicit ownership in the CLI checklist: help/version/shim phase 1; tunnel and coordination-recovery phase 2; channel phase 4; serve phase 3/4; embedded phase 3/5; update phase 5. `cli.py`, `tunnel_*.py`, `channel.py`, `coordination_recovery.py` | `test_tunnel_cli.py`, `test_tunnel_service.py`, `test_tunnel_bridge.py`, `test_tunnel_profiles.py`, `test_tunnel_runtime.py`, `test_coordination_recovery.py`, `test_channel.py` I/mixed; no supported mode disappears by implication | deferred |
| HTTP-SECURITY | 3 | Health public/degraded semantics; MCP/REST bearer gates on UTF-8/Latin-1 bytes, fail-closed map, remote bind/trust policy, DNS/Origin/rebinding, JSON/body limits, error status, redirect refusal. `daemon.py`, `web/api.py`, `principals.py`, `utils/no_redirect.py` | `test_daemon_http.py` P/I; `test_web.py`, `test_principals.py`, `test_extractor_no_redirect.py`, `test_shim_transport_recovery.py`, `test_dim_mismatch_health.py` I/mixed | deferred |
| HTTP-STATIC | 3 | Root redirect, Console v3 assets at /ui/; /ui/next/ is absent at this pin, traversal/security/content types. `web/api.py`, `web/static/`, `frontend/` | `test_web.py` I/mixed; `test_console_build.py`, `test_console_source_guards.py` A; preserve current Console v3 behavior | deferred |
| HTTP-READS | 3 | Every GET Console route including configuration, episodes, graph/review/provenance and telemetry reads. `web/routes.py`, `web/config_io.py` | `test_web.py`, `test_coordination_console.py`, `test_coordination_web.py`, `test_web_hardening.py` I/mixed; add HTTP corpus for every GET route | deferred |
| PG-HYDRATE | 3 | Exact v53 rows/types/defaults/indexes/extension/vector(1024), idempotent startup/restarts, malformed-state refusal and single writer. `storage/schema.py`, `postgres.py`, `sync.py`, `service.py` | `test_schema_version.py`, `test_schema_ddl_shape.py`, `test_schema_healing.py`, `test_pg_storage.py`, `test_pgvector_compat.py`, `test_fail_closed_hydration.py`, `test_hydrate_capacity.py`, `test_storage_connect.py`, `test_writer_lease.py`, `test_disposable_database_guard.py` I/DB; catalog fixture checks can observe Rust directly | deferred |
| EMBEDDING | 3 | Qwen 1024/MiniLM 384, document/query asymmetry, dtype, cache, dimension refusal, offline load-only validated ONNX artifacts. `memory/embedding.py`, `onnx_artifacts.py`, `utils/config.py` | `test_embedding_backend.py`, `test_embedding_precision.py`, `test_embedding_asymmetry.py`, `test_query_side_encoding.py`, `test_embedding_dim_guard.py`, `test_embedding_cache.py`, `test_embedding_cpu_dtype.py`, `test_onnx_artifacts.py`, `test_onnx_model_provision.py` I/A; real-model subprocess corpus needed | deferred |
| RETRIEVAL | 3 | Five pools, exact cosine/tie order, source/episode/tag/band filters, BM25 fusion, recency/retention, supersession pointers, abstention and strict rerank whole-pool budget. `memory/cms.py`, `miras/`, `bm25.py`, `reranker.py`, `abstain.py` | `test_retrieval_golden.py`, `test_retrieval_pool.py`, `test_retrieval_replay.py`, `test_band_cosine.py`, `test_band_ablation_flat.py`, `test_bm25.py`, `test_cortex_bm25.py`, `test_reranker.py`, `test_reranker_margin_gate.py`, `test_abstain.py`, `test_retention_boost.py`, `test_superseded_visibility.py`, `test_meta_filter.py` I; add exact order corpus, floors alone insufficient | deferred |
| READ-CORE | 3 | memory_get/recent/fact_get/world_search/lesson_search/history/stats/recall/episode summaries, memory_graph, document_search and memory_consolidation_candidates, compact/verbose/explain payloads. `service.py`, `mcp_server.py`, `memory/recall.py`, `context_builder.py`, cortex/world/lessons | `test_mcp_server.py`, `test_recall.py`, `test_constraint_pinning.py`, `test_cortex.py`, `test_world_cortex.py`, `test_lessons_service.py`, `test_episode_service.py` I/mixed; full MCP response corpus needed | deferred |
| WRITE-CMS | 4 | Store surprise admission, non-destructive possible conflicts, bounded capacity, explicit supersede/reinstate/consolidate/forget/reinforce semantics and audit lineage. `cms.py`, `contradiction.py`, `consolidation.py`, `compaction.py`, `service.py` | `test_service.py`, `test_consolidation.py`, `test_consolidation_atomicity.py`, `test_correction_atomicity.py`, `test_correction_identity.py`, `test_entry_reinstatement.py`, `test_forget_cascade.py`, `test_access_count_semantics.py`, `test_store_retirement.py`, `test_slot_keyed_supersession.py` I; add before/after rows and restart corpus | deferred |
| WRITE-CORTEX | 4 | HLC authority, scalar/set slots, contenders/promotions, provenance trust, freshness/stale, labels, canonical and world/lesson histories and engrams. `hlc.py`, `cortex.py`, `world_cortex.py`, `lessons.py`, `freshness.py`, `labels.py` | `test_hlc.py`, `test_cortex_sets.py`, `test_cortex_contenders.py`, `test_cortex_promotion.py`, `test_assistant_provenance.py`, `test_contender_stamps.py`, `test_freshness.py`, `test_label_pair.py`, `test_lessons_storage.py`, `test_entity_provenance.py` I/DB | deferred |
| WRITE-EPISODES | 4 | Daemon-owned session handles, pointers/tombstones, nested episodes, reap/resume/title stamps, isolated caller identity and inferred outcomes. `episodes.py`, `writer_context.py`, `service.py`, `web/session_hook.py` | `test_session_identity.py`, `test_episodes.py`, `test_episode_service.py`, `test_session_title.py`, `test_outcome_inference.py` I/mixed; add two-client restart/reap corpus | deferred |
| BACKGROUND | 4 | Autosave/errors, clean-exit flush, release check, heap trim, memory headroom, retry initialization and lock separation/cancellation. `daemon.py`, `service.py`, `release_check.py`, `utils/` | `test_loop_health.py`, `test_service_lock_discipline.py`, `test_init_retry_model_reuse.py`, `test_heap_trim.py`, `test_memory_headroom.py`, `test_update_offer.py`, `test_connection_loss_recovery.py` I/mixed; observed subprocess lifecycle and durable restart cases required | deferred |
| DREAM | 4 | Pull/ack/commit cursors, scheduled quiescence, retry/fallback/extractor protocol/redirection, atomic run rollback, quarantine, literal gate, digests/chronicle. `service_dream.py`, `memory/dream.py`, `dream_token.py` | `test_dream.py`, `test_dream_acknowledgement.py`, `test_dream_ack_storage.py`, `test_dream_runs.py`, `test_dream_stall.py`, `test_dream_quarantine.py`, `test_dream_chronicle.py`, `test_session_digest.py`, `test_extractor_fallback.py`, `test_extractor_no_redirect.py`, `test_literal_gate.py` I/DB; fake sidecar E2E and ladder evidence | deferred |
| GRAPH-REVIEW | 4 | NetworkX-derived graph/order, communities, aliases/relations, deep dream, safe cleanup, scoped proposals/merge veto/fold direction, review queue/judges/audit. `graph.py`, `memory/graph_*.py`, `review_*.py`, `relation_quality.py`, `service_dream.py` | `test_graph.py`, `test_graph_store.py`, `test_graph_insight.py`, `test_graph_review.py`, `test_graph_consolidation.py`, `test_deep_dream.py`, `test_merge_queue.py`, `test_review_decisions.py`, `test_review_judgment_freshness.py`, `test_queue_judges_service.py`, `test_entity_proposals.py`, `test_edge_proposals.py`, `test_relation_quality.py` I/DB | deferred |
| BOARD-STORE | 4 | All 15 actions, FIFO durable leases, nonce/request dedup, mailbox authorization/clock/retention/redaction/hash chain, bank identity/recovery. `coordination.py`, `storage/coordination.py`, `private_state.py` | `test_coordination_integration.py`, `test_coordination_storage.py`, `test_coordination_tools.py`, `test_coordination_auth.py`, `test_coordination_audit.py`, `test_coordination_clock.py`, `test_coordination_recovery.py`, `test_coordination_secrets.py`, `test_coordination_multi_ack.py`, `test_coordination_mail_continuity.py` I/DB/mixed | deferred |
| BOARD-ASYNC | 4 | Receive <=30s, max64 waiters/16 permits/4-worker isolation, cancellation retains capacity, subscribe-before-read, generations/attachments and notifier shutdown. `web/coordination.py`, `coordination_adapter.py` | `test_coordination_wait.py`, `test_coordination_dispatch_isolation.py`, `test_coordination_adapter.py` I/mixed; add concurrent wire disconnect/timeout/restart cases | deferred |
| DELIVERY | 4 | Codex app-server messages/doorbell, opt-in host transports, wake/park leases and Claude channel behavior. `codex_delivery.py`, `codex_doorbell.py`, `codex_connection.py`, `channel.py`, `wake_liveness.py` | `test_codex_delivery.py`, `test_codex_doorbell.py`, `test_codex_doorbell_probe.py`, `test_codex_coordination.py`, `test_channel.py`, `test_stop_wake_hook.py`, `test_coordination_turn_digest.py` I/mixed/A; fake local host E2E only | deferred |
| HOOKS | 4 | Eight endpoint authorization/method/output/identity contracts; tokenless Host/Origin gate in `_browser_gate`, skipped when a token is configured (not a peer-address gate); plugin 11 handler entries, eight shell scripts plus multiplexed `lifecycle.ps1`; Codex launcher approval identity. `web/api.py`, `session_hook.py`, `plugin_hooks.py`, `plugin/hooks/`, `ops/setup-codex-hooks.py` | `test_web.py`, `test_session_identity.py`, `test_hooks_digest.py`, `test_codex_hooks.py`, `test_codex_hook_launcher.py`, `test_codex_hook_setup.py`, `test_codex_reapproval.py`, `test_hook_glob_ranges.py`, `test_plugin_packaging.py` I/mixed/A; changing hooks.json definitions imposes renewed user approval | deferred |
| HTTP-WRITES | 4 | Every POST Console route, coercion/error mapping, config whitelist/live/restart classification, backup preservation. `web/routes.py`, `config_io.py` | `test_web.py`, `test_console_source_guards.py`, `test_correction_interfaces.py`, `test_web_hardening.py` I/mixed; record POST routes and their storage effects | deferred |
| STORAGE-COMPAT | 3/4/5 | Embedded PG18 lite startup/stable data-dir, files `.pt` state+import/partial restore/weights, Chroma reference-bank ingest/search, parser/chunk rules. `storage/embedded_pg.py`, `migrate.py`, `memory/reference_bank.py`, `document_parser.py`, `utils/atomic_io.py` | `test_embedded_pg.py`, `test_cms_pt_schema.py`, `test_cms_legacy_load.py`, `test_migration.py`, `test_restore_from_pt.py`, `test_atomic_weights.py`, `test_reference_bank.py`, `test_document_parser.py`, `test_document_ingest_batch_bound.py` I/mixed. No simplification is pre-authorized | deferred |
| OPS-CUTOVER | 5 | CPU/offline images, models, compose byte equality, update backup/rollback/daemon-only health/clients, runtimes beside current sessions, unattended upgrade policy, four release surfaces. `ops/Dockerfile.daemon`, compose copies, `update_cli.py`, `unattended_update.py`, `runtimes.py`, `.github/workflows/` | `test_daemon_image_is_cpu_only.py`, `test_update_cli.py`, `test_ops_update_wrappers.py`, `test_shim_runtimes.py`, `test_unattended_update.py`, `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_ops_script_modes.py` I/A/mixed; fresh disposable install/update proof, no deploy | deferred |
| DOCS-CUTOVER | 5 | Guide/provider/tunnel/config/schema/atlas/defaults/help/LLMS and benchmark claim currency, oracle retained pending maintainer retirement. `docs/guide/`, `docs/atlas/`, READMEs, `llms*`, `server.json`, `pyproject.toml`, release manifests | `test_release_ux.py`, `test_atlas_currency.py`, `test_llms_txt.py`, `test_i18n_readme.py`, `test_eval_evidence.py`, `test_extractor_model_lists.py`, `test_repository_claims.py` A; regenerate LLMS after doc edits | deferred |

| CONFIG-LOAD | 1/2/3/4/5 | YAML path/missing-file defaults, unknown-key filtering, recursive dataclass construction, section-specific load rules and env precedence at each reader. `utils/config.py`, service/daemon/client/ops readers; every dataclass and variable is enumerated below and in `contract-inventory.json` | `test_memory_config.py`, `test_ops_env_example.py`, `test_dream.py`, `test_client_environment.py` I/A; preserve defaults, coercion and validation errors | deferred |
| BACKGROUND-SWEEP | 4 | `mcp_server.start_dream_sweep` starts once when dream OR retrieval logging is enabled; sweep drives superseded compaction, dream-run journal pruning and retrieval-log pruning even with dreams off; automatic dream remains backlog/quiescence gated. `memory/dream.py:run_sweep_once` | `test_dream.py`, `test_compaction.py`, `test_dream_runs.py`, `test_retrieval_log.py` I/DB; daemon wire/restart equivalents pending | deferred |
| BACKGROUND-SESSIONS | 4 | `mcp_server.start_session_reaper` idempotence, idle/reap intervals, close/end dream, empty-session resume window and tombstones; `start_background_durability` registers clean-exit flush, autosave and warmup exactly once. `service.py`, `mcp_server.py` | `test_session_identity.py`, `test_episode_service.py`, `test_loop_health.py` I; startup/clean-exit/reap wire cases pending | deferred |
| HEALTH | 3 | Open /health payload mandatory and conditional fields and 200/503/500 mapping listed below; bank fingerprint only after successful DB ping; warnings that leave status ok remain non-fatal. `daemon.py:_build_health_payload`, `web/api.py` | `test_daemon_http.py`, `test_dim_mismatch_health.py`, `test_daemon_moved_fence.py` I/P; network health corpus pending | deferred |
| MCP-MOUNT | 3 | /mcp and /mcp/* pass to SDK after bearer gate; Streamable HTTP mount and token-aware transport security: tokenless DNS-rebinding protection allows loopback Host/Origin patterns; configured auth disables SDK rebinding protection. `mcp_server.transport_security_for`, `build_streamable_http_app`, `daemon.py`, `web/api.py` | `test_daemon_http.py`, `test_mcp_client_neutrality.py`, `test_web.py` I/P; negotiate and probe mount over HTTP | deferred |
| DISPOSABLE-GUARDS | 2/3/4/5 | `storage/schema.py:refuse_production_database` rejects known production names and fails closed if the production bank cannot be resolved; `assert_disposable_database` asks the connected server for its actual database and applies that same refusal before reap/DDL/truncate. It does not impose a disposable-name prefix allowlist. Existing guards precede destructive work. | `test_disposable_database_guard.py`, `test_bench_production_port_guard.py` I/DB; equivalent Rust refusal tests pending | deferred |
| SEARCH-COMPACTION | 3 | `memory_search` compact payload uses `memory.mcp.entry_text_chars` (600 default), literal `truncated: true` only when own text is clipped; verbose bypasses compaction. `replaced_by` is {id, at, preview, verified, current}, preview 120 chars; null id/date and false current remain distinct. Full text available via memory_get. `mcp_server.py:_compact_entry`, `_replaced_by` | `test_mcp_server.py`, `test_superseded_visibility.py` I; compact/verbose/successor wire corpus pending | deferred |
| CONTRADICTION | 4 | Structured-slot identity first, then negation asymmetry, affirmative replacement and anchor-tier state transition heuristics; optional bounded NLI scorer is implemented but unwired by service. Detection admits possible conflicts and never retires whole notes automatically. `memory/contradiction.py`, `memory/nli.py`, `memory/cms.py`, `service.py` | `test_contradiction_cost.py`, `test_service.py`, `test_store_retirement.py` I; four paths and missing-scorer equivalents pending | deferred |
| HTTP-BODY-LIMITS | 3/4 | Per-route byte limits and /api/agents?view=coordination response selection listed below; wrong methods, oversized and malformed bodies preserve oracle statuses. `web/api.py:_body_limit`, `web/routes.py` | `test_web_hardening.py`, `test_coordination_web.py`, `test_coordination_console.py`, `test_pair_endpoint.py` I; additive HTTP boundary cases pending | deferred |
| PRINCIPALS-V53 | 1/2/3/4 | Durable principals, SHA-256 token/code storage, refresh/exclusion rules, expiry/revocation and fail-closed unavailable identities; invited caller operator restrictions. `principal_store.py`, `principals.py`, `storage/schema.py` | `test_principal_store_pg.py`, `test_stored_principals.py`, `test_stored_principals_daemon.py`, `test_stored_principals_mcp.py`, `test_stored_principals_daemon_wiring.py` I/DB | deferred |
| CLI-PAIRING | 2/3/4 | invite principal creation/list/revoke; pair mints owner-only local token, sends only hash to POST /api/pair, updates existing registrations; browser-origin refusal, uniform pairing_refused, concurrency/rate budget, unavailable mapping. `invite_cli.py`, `pair_cli.py`, `web/api.py` | `test_invite_cli.py`, `test_pair_cli.py`, `test_pair_endpoint.py` I/mixed | deferred |
| CLI-EXPOSE-MOVE | 2/3/5 | expose requires token and owns only its Tailscale forward; move final stopped-daemon backup, source fencing, restore/row verification, one destination start, client re-pointing and rollback; moved/unfinished move startup refusal. `expose_cli.py`, `move_cli.py`, `daemon.py:moved_refusal` | `test_expose_cli.py`, `test_move_cli.py`, `test_daemon_moved_fence.py` I/mixed; disposable host stand-ins pending | deferred |
| MAIL-RING-V53 | 1/2/4 | wait-mail wakes on an authorized daemon ring, plain addressed mail never wakes; digest/watermark cleanup, mailbox attribution to parent/subagent, generation/attachment continuity and optional Codex mailbox-tool approval. `wait_mail_cli.py`, `coordination_adapter.py`, `ops/setup-codex-hooks.py` | `test_wait_mail_cli.py`, `test_coordination_adapter.py`, `test_codex_subagent_stop.py`, `test_codex_hook_setup.py` I/mixed | deferred |
| OPS-INSTALL-RESTORE | 5 | install.sh/ps1; install-autostart.ps1; install-shim-autostart.sh/ps1 and install-codex-shim-autostart.sh/ps1; install-hook.sh/ps1; restore.sh/ps1 and restore_from_pt.py; preflight.sh/ps1; migrate-pg18.ps1; backup.sh/ps1 and install-backup-task.ps1; prune-rollbacks.sh/ps1, prune-build-cache.sh/ps1 and install-cache-retention.ps1; register_claude_desktop.py; setup-codex-coordination.py; update.sh/ps1/update.py/update_clients.py. Preserve checks/refusals/rollback/platform and client ownership. `ops/` | `test_ops_script_modes.py`, `test_ops_backup_integrity.py`, `test_ops_restore_rehearsal.py`, `test_ops_restore_skips_held.py`, `test_ops_prune_rollbacks.py`, `test_ops_prune_build_cache.py`, `test_ops_install_backup_task.py`, `test_ops_install_cache_retention.py`, `test_ops_update_wrappers.py`, `test_client_install_ux.py` I/A/mixed | deferred |
| OPS-IMAGES | 5 | Dockerfile.extractor sidecar model/config/health contract; Dockerfile.pg PostgreSQL18/pgvector build/runtime contract; preserve daemon CPU/offline and compose byte equality. `ops/Dockerfile.extractor`, `ops/Dockerfile.pg`, `ops/Dockerfile.daemon` | `test_daemon_image_is_cpu_only.py`, `test_update_cli.py`, `test_extractor_model_lists.py` A; disposable image proofs pending | deferred |
| PLUGIN-COMMANDS | 2/4/5 | plugin/commands/dream.md and memory-status.md user workflows; .claude-plugin/plugin.json identity and release.json release metadata; hooks.json 11 entries/eight shell scripts/lifecycle.ps1; PreToolUse subagent-board-guard allows list/default agents and receive-only mail, denies board writes/send/ack on parent address. `plugin/`, `plugin_hooks.py` | `test_plugin_packaging.py`, `test_subagent_board_guard.py`, `test_subagent_board_hook.py`, `test_hooks_digest.py`, `test_codex_reapproval.py` A/I/mixed; cross-reference HOOKS and hook acceptance checklist | deferred |
| ONNX-PREREQUISITE | 3 | pyproject.toml onnx-extra comment says bit-identical embeddings; docs/guide/configuration.md says default Qwen has no supplied ONNX artifact. Packaging/docs disagreement remains unchanged here; phase 3 requires identified same graph/tokenizer, measured error and maintainer docs resolution before accepting embedding parity. | `test_onnx_artifacts.py`, `test_onnx_model_provision.py`, `test_embedding_backend.py` I/A; no accepted tolerance or equivalence | deferred |

## Per-phase selection and acceptance gaps

Phase 0b: unchanged Python control over `test_shim.py`, `test_daemon_http.py`, selected recovery boundary nodes and docs/evidence guards; new generated corpus/schema snapshot/harness negative controls. Baselines identify backend and use a quiet CPU host with lease free. The historical disposable trial is complete and its corrections remain in PORTING.md; phase 0b closes the five new gaps rather than repeating that trial.

Phase 1: selected real stdio/HTTP nodes in `test_shim.py` plus audited subprocess recovery cases; original entire shim/credential/tier/identity/recovery file pools remain Python. `test_shim_autostart.py`, `test_shim_python.py` and `test_shim_prompt.py` are extractor tooling, not MCP shim coverage; do not select them based on name alone. For `_proxy` private snippets add equivalent binary probes rather than claim they exercise Rust.

Phase 2: subprocess nodes audited from lease/mail/doctor/connect/briefing/episode leaves. Existing backup/transfer/audit/CLI dispatcher tests largely call functions directly or install Python fakes; they remain oracle, with new argv/HTTP/fake-executable and disposable-PG cases. Tunnel/recovery are phase 2, channel phase 4, embedded phase 3/5, and version/help phase 1 as recorded in the mode checklist. These cannot become unspecified deferrals at a purported completed cutover.

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
7. Public PII/evidence guards may need additive new guard tests outside immutable existing files; the port performs no DDL, table addition or column repurposing. The phase-start schema is 53; upstream schema bumps follow the seven-place checklist and a newly recorded phase pin.
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
| memory_lesson_search | core | 3 | deferred |
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

Source: `pseudolife_memory/cli.py`. The default is `shim`; help aliases are `-h` and `--help`, and version also accepts `--version`. Every mode preserves exit status, both output streams and argument validation. Subcommands remain part of each mode's contract.

| Mode | Phase | Status | Evidence |
|---|---|---|---|
| help | 1 | deferred | No accepted Rust evidence |
| version | 1 | deferred | No accepted Rust evidence |
| shim | 1 | deferred | No accepted Rust evidence |
| serve | 3/4 | deferred | No accepted Rust evidence |
| embedded | 3/5 | deferred | No accepted Rust evidence |
| channel | 4 | deferred | No accepted Rust evidence |
| coordination-recovery | 2 | deferred | No accepted Rust evidence |
| board-audit | 2 | deferred | No accepted Rust evidence |
| briefing | 2 | deferred | No accepted Rust evidence |
| prompt-hook | 2 | deferred | No accepted Rust evidence |
| doctor | 2 | deferred | No accepted Rust evidence |
| connect | 2 | deferred | No accepted Rust evidence |
| tunnel | 2 | deferred | No accepted Rust evidence |
| update | 5 | deferred | No accepted Rust evidence |
| backup | 2 | deferred | No accepted Rust evidence |
| export | 2 | deferred | No accepted Rust evidence |
| import | 2 | deferred | No accepted Rust evidence |
| episode-start | 2 | deferred | No accepted Rust evidence |
| episode-end | 2 | deferred | No accepted Rust evidence |
| wait-mail | 2 | deferred | No accepted Rust evidence |
| lease | 2 | deferred | No accepted Rust evidence |

| invite | 2 | deferred | No accepted Rust evidence |
| pair | 2 | deferred | No accepted Rust evidence |
| expose | 2 | deferred | No accepted Rust evidence |
| move | 2 | deferred | No accepted Rust evidence |

## Test-file buckets

The pinned oracle contains 406 `tests/test_*.py` files: 68 oracle, 1 candidate and 337 internal. Candidate coverage is exactly 5 concrete nodes from `test_cli_dispatch.py`; unlisted nodes stay oracle-only. Every internal file explicitly records its Rust unit/wire equivalent as `pending`; the bucket inventory establishes no implemented parity. Run `python rust/contract_inventory.py` and `python -m pytest rust/test_contract_inventory.py -q`.

## Health, HTTP bodies and logical transfer

Mandatory /health fields: status, version, schema, storage, auth, bank, persist_errors and memory. Conditional fields: build; coordination (enabled/wake); updates (check_releases/latest_release/checked_at/unattended_clients/unattended_daemon); extractor; stall (since/reason/consecutive_failures/last_success_at); hooks_digest; init_refusal; not_ready; migration_partial; dream_tracking_error; capacity_warning; lesson_reconciliation_required; embedder; db; last_backup. `status == "ok"` (or absent status) maps to 200, other payload status to 503, payload exception to 500 with {status: error, error: text}. DB failure, init_refusal and not_ready degrade; extractor/stall/migration/backup/headroom warnings alone do not. Bank fingerprint may be null and is filled after a successful storage ping. Nested optional structures preserve the source shape.

Nested health fields: build {git_sha, dirty, built_at, source}; embedder {backend, device, dtype}; last_backup {at, age_hours, rotation}; coordination.wake carries every WakeConfig field (per_recipient_per_hour, urgent_per_sender_per_hour, nightly_total, fan_out_stagger_seconds, active_seconds). memory has source (cgroup/process/unavailable), and where available current_bytes, working_set_bytes, limit_bytes, used_fraction, near_limit, events (max/oom/oom_kill), anon_bytes, file_bytes, rss_bytes and rss_peak_bytes; absent values and null limits remain distinct.

Console POST body limits are 262144 bytes except /api/facts/set, /api/consolidate and /api/supersede (4194304 bytes); all /api/coordination/<action> bodies are 32768 bytes; hook session-end is 16384 bytes; POST /api/pair is 1024 bytes. GET hooks consume query/header inputs, and woke/subagent POST hooks use query/header inputs rather than parsing a body. /api/agents?view=coordination returns the coordination roster projection rather than the default agent view. Unknown routes and method errors remain oracle behavior.

`EXPORTED_TABLES` (26 tables): `meta`, `episodes`, `entries`, `entry_reinstatement_decisions`, `entities`, `entity_aliases`, `relations`, `edges`, `edge_evidence`, `edge_proposals`, `entity_proposals`, `entity_kinds`, `dismissed_pairs`, `facts`, `world_facts`, `lessons`, `outcome_signals`, `communities`, `entity_communities`, `memory_traces`, `memory_trace_invalidations`, `entity_sources`, `merge_decisions`, `chronicle_events`, `curation_judgments`, `store_decisions`.

`EXCLUDED_TABLES` (14 tables): `dream_runs`, `dream_run_slots`, `retrieval_events`, `retrieval_uses`, `slot_reads`, `lesson_search_events`, `client_sessions`, `coordination_agents`, `coordination_messages`, `coordination_events`, `coordination_leases`, `coordination_lease_waiters`, `coordination_wakes`, `principals`.

Also exclude outcome_signals.used_ids and bank-local meta keys: schema_version, active_session_pointer, dream_ack_secret_v1, coordination_hlc_highwater, coordination_bank_id, writer_lease_epoch, and every *_schema_version key. Import requires all exported tables empty except meta and builtin relations; excluded runtime tables never travel in logical exports.

## Configuration sections

One row per dataclass in utils/config.py, including nested band specs and aggregate sections; every declared field is recorded in contract-inventory.json. All rows preserve missing/YAML/default/env precedence, type/error/coercion and validation behavior at the source reader.

| Section | Declared fields | Phase | Status |
|---|---:|---|---|
| `EmbeddingConfig` | 9; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MIRASBandSpec` | 6; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MIRASConfig` | 2; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `ReferenceConfig` | 5; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `NLIConfig` | 4; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `BM25Config` | 7; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `RerankerConfig` | 5; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `DreamConfig` | 41; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `DeepDreamConfig` | 49; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `CortexConfig` | 11; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `LessonsConfig` | 11; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `RetrievalLogConfig` | 3; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `CompactionConfig` | 3; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MetaFilterConfig` | 1; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `GraphInsightConfig` | 8; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `TracesConfig` | 2; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `ScopesConfig` | 2; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `RecallConfig` | 12; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `SearchConfig` | 6; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `McpConfig` | 2; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MemoryConfig` | 29; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `ContextConfig` | 1; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `TimeConfig` | 1; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `StorageConfig` | 1; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `MemoryPolicyConfig` | 2; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `WakeConfig` | 5; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `CoordinationConfig` | 6; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `UpdatesConfig` | 4; see `contract-inventory.json` | 1/2/3/4/5 | deferred |
| `AppConfig` | 8; see `contract-inventory.json` | 1/2/3/4/5 | deferred |

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
| `PSEUDOLIFE_CODEX_BIN` | `ops/setup-codex-coordination.py`, `pseudolife_memory/codex_doorbell.py`, `pseudolife_memory/shim.py` |
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
| `PSEUDOLIFE_DAEMON_MEM_LIMIT` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py` |
| `PSEUDOLIFE_DESKTOP_TOKENS_SOURCE` | `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_DESKTOP_TOKEN_SOURCE` | `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_DIGEST_DIR` | `plugin/hooks/coordination-prompt.sh`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/briefing_cli.py`, `pseudolife_memory/coordination_identity.py`, `pseudolife_memory/wait_mail_cli.py` |
| `PSEUDOLIFE_DOCKER` | `pseudolife_memory/update_cli.py` |
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
| `PSEUDOLIFE_MCP_CONFIG` | `ops/dedup_cortex.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/web/config_io.py` |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `ops/client_credentials.py`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/remote-suite.ps1`, `ops/setup-codex-coordination.py`, `ops/setup-codex-hooks.py`, `ops/wsl-suite.ps1`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/client_config.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/daemon_url.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/runtimes.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_cli.py`, `pseudolife_memory/tunnel_runtime.py`, `pseudolife_memory/unattended_update.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_MCP_DATABASE_URL` | `ops/backfill_edge_confidence.py`, `ops/dedup_cortex.py`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `ops/measure_reverify_population.py`, `ops/migrate_drop_age.py`, `ops/migrate_embeddings.py`, `ops/restore_from_pt.py`, `ops/retire_by_writer.py`, `pseudolife_memory/backup_cli.py`, `pseudolife_memory/board_audit_cli.py`, `pseudolife_memory/cli.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/coordination_recovery.py`, `pseudolife_memory/daemon.py`, `pseudolife_memory/invite_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/service.py`, `pseudolife_memory/storage/embedded_pg.py`, `pseudolife_memory/storage/schema.py`, `pseudolife_memory/transfer_cli.py` |
| `PSEUDOLIFE_MCP_DATA_DIR` | `ops/Dockerfile.daemon`, `ops/dedup_cortex.py`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `ops/restore_from_pt.py`, `pseudolife_memory/backup_cli.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/storage/embedded_pg.py`, `pseudolife_memory/transfer_cli.py`, `pseudolife_memory/update_cli.py` |
| `PSEUDOLIFE_MCP_HOST` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py` |
| `PSEUDOLIFE_MCP_NO_SPAWN` | `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/setup-codex-coordination.py`, `pseudolife_memory/client_updates.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/runtimes.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_runtime.py` |
| `PSEUDOLIFE_MCP_PORT` | `ops/Dockerfile.daemon`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/daemon.py`, `pseudolife_memory/invite_cli.py` |
| `PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS` | `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_MCP_SHARED_HOST` | `pseudolife_memory/shim.py` |
| `PSEUDOLIFE_MCP_STORAGE` | `pseudolife_memory/mcp_server.py`, `pseudolife_memory/storage/embedded_pg.py` |
| `PSEUDOLIFE_MCP_TIER_MAP` | `ops/docker-compose.yml`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/invite_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/principals.py`, `pseudolife_memory/toolset_tiers.py` |
| `PSEUDOLIFE_MCP_TOKEN` | `ops/client_credentials.py`, `ops/docker-compose.yml`, `ops/install-autostart.ps1`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/remote-suite.ps1`, `ops/setup-codex-coordination.py`, `ops/setup-codex-hooks.py`, `ops/wsl-suite.ps1`, `plugin/hooks/coordination-start.sh`, `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-end.sh`, `plugin/hooks/session-start.sh`, `plugin/hooks/stop-wake.sh`, `plugin/hooks/subagent-board.sh`, `pseudolife_memory/board_status.py`, `pseudolife_memory/briefing_cli.py`, `pseudolife_memory/cli.py`, `pseudolife_memory/client_config.py`, `pseudolife_memory/codex_connection.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/credentials.py`, `pseudolife_memory/daemon.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/episode_cli.py`, `pseudolife_memory/expose_cli.py`, `pseudolife_memory/lease_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/move_cli.py`, `pseudolife_memory/principal_store.py`, `pseudolife_memory/principals.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_cli.py`, `pseudolife_memory/unattended_update.py`, `pseudolife_memory/update_cli.py`, `pseudolife_memory/web/api.py` |
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
| `PSEUDOLIFE_SHIM_LAUNCHER` | `plugin/hooks/lifecycle.ps1`, `plugin/hooks/session-start.sh`, `pseudolife_memory/runtimes.py` |
| `PSEUDOLIFE_SHIM_PYTHON` | `ops/install.ps1`, `ops/install.sh` |
| `PSEUDOLIFE_SHIM_RUNTIMES` | `pseudolife_memory/runtimes.py` |
| `PSEUDOLIFE_SHIM_USER_BIN` | `pseudolife_memory/runtimes.py` |
| `PSEUDOLIFE_STATE_VOLUME` | `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `pseudolife_memory/compose/docker-compose.yml` |
| `PSEUDOLIFE_SUITE_COMMIT` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_DISPATCHED` | `ops/remote-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_ENV_FILE` | `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_GIT_COMMON` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_LEASE` | `ops/wsl-suite.sh`, `pseudolife_memory/lease_cli.py` |
| `PSEUDOLIFE_SUITE_LOCK` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_SUITE_LOCK_DIR` | `ops/wsl-suite.sh`, `pseudolife_memory/board_audit_cli.py`, `pseudolife_memory/lease_cli.py` |
| `PSEUDOLIFE_SUITE_NAME` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_PYTHON` | `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_REMOTE` | `ops/remote-suite.ps1` |
| `PSEUDOLIFE_SUITE_SLOTS` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_SUITE_VENV` | `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_SUITE_WHERE` | `ops/remote-suite.ps1` |
| `PSEUDOLIFE_SUITE_WINDOWS` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TEST_CUDA` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TEST_DATABASE_URL` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/storage/schema.py` |
| `PSEUDOLIFE_TEST_EMBEDDER` | `ops/ci_tests.sh`, `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TEST_PG_HOST_PORT` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, `ops/wsl-suite.sh` |
| `PSEUDOLIFE_TEST_PG_PASSWORD` | `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE` | `pseudolife_memory/tunnel_bridge.py`, `pseudolife_memory/tunnel_runtime.py` |
| `PSEUDOLIFE_WRITER_ID` | `ops/docker-compose.yml`, `ops/install.ps1`, `ops/install.sh`, `ops/register_claude_desktop.py`, `ops/setup-codex-coordination.py`, `pseudolife_memory/compose/docker-compose.yml`, `pseudolife_memory/connect_cli.py`, `pseudolife_memory/doctor_cli.py`, `pseudolife_memory/mcp_server.py`, `pseudolife_memory/service.py`, `pseudolife_memory/shim.py`, `pseudolife_memory/tunnel_runtime.py`, `pseudolife_memory/writer_context.py` |
| `PSEUDOLIFE_WSL_DISTRO` | `ops/remote-suite.ps1`, `ops/wsl-suite.ps1` |
| `PSEUDOLIFE_WSL_SUITE_SOURCE` | `ops/wsl-suite.ps1` |
| `_PSEUDOLIFE_PRODUCTION_DB` | `pseudolife_memory/storage/schema.py` |
