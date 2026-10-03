# Behaviour parity register

Oracle: Python 0.15.0 at `136a34ae95e981a691fcc31ba9fb4f35d83d4249`, schema 52.
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

All rows are deferred until their acceptance evidence is reviewed. No behavior retirement is authorized. `P` means real process/wire eligible nodes; `I` means Python internals requiring additive wire cases or Rust unit equivalents; `A` means artifact/static contract. Paths below are repository-relative.

| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| BASE | 0 | Commit/host/bank/run-count RSS idle and workload, shim cold start, search/store/fact-set and send/receive p50/p95; CI lane timing. `evals/`, `.github/workflows/ci.yml` | `tests/test_eval_evidence.py`, `test_report_redaction.py` A; add baseline scripts and immutable artifacts, no inferred performance result | deferred |
| RULES | 0 | Error/type/ownership/unsafe/HTTP/SQL/float mappings; immutable manifest and trial corrections. `CLAUDE.md`, guides, atlas | `test_release_ux.py`, `test_atlas_currency.py`, `test_llms_txt.py`, `test_eval_evidence.py`, `test_i18n_readme.py` A; add harness self-validation | deferred |
| MCP-WIRE | 1 | initialize instructions/capabilities, tool annotations/schema/help, supported revision negotiation, JSON-RPC errors, streams, cancellation, notifications. `mcp_server.py`, `shim.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py` mixed P/I; `test_mcp_server.py`, `test_mcp_string_arguments.py`, `test_mcp_client_neutrality.py`, `test_mcp_stdio_errlog.py` I/A; help fixture A | deferred |
| MCP-TIER | 1/3 | Principal-scoped list filtering, 12h TTL/precedence, cumulative 9/24/38 visibility, hidden calls accepted, list_changed. `toolset_tiers.py`, `mcp_server.py` | `test_shim.py` notification nodes P; `test_toolset_tiers.py`, `test_mcp_server.py` I; add 38-tool differential schema/argument/error corpus | deferred |
| SHIM-LIFECYCLE | 1 | Canonical origin validation, discovery/probe, local spawn/reuse, no-spawn waiting, ownership, child exit and recovery, version notices and gated client updates. `shim.py`, `daemon_url.py`, `runtimes.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py`, `test_connection_loss_recovery.py`, `test_shim_runtimes.py` mixed; `test_client_environment.py`, `test_credentials.py`, `test_version_handshake.py`, `test_update_offer.py` I/mixed | deferred |
| SHIM-AUTH | 1 | Token-file precedence and reload, unsafe/malformed files fail closed, writer/session/agent/bank/principal headers; sanitized uncertain-write failures without replay. `credentials.py`, `writer_context.py`, `shim.py` | `test_shim_transport_recovery.py` mixed; `test_writer_keying.py`, `test_principals.py`, `test_credentials.py`, `test_session_identity.py` I; add wire rotation and malformed byte probes | deferred |
| SHIM-BOARD | 1 | Registration, scoped identity, addressed-mail continuity, shared-host refusal, local file claims, board retry, default doorbells and optional delivery invoked by the shim; standalone channel mode remains phase 2. `coordination_adapter.py`, `coordination_identity.py`, `codex_doorbell.py`, `codex_delivery.py`, `repository_claims.py` | `test_shim_board_retry.py`, `test_shim_channel.py`, `test_channel.py`, `test_coordination_roster_hygiene.py`, `test_codex_doorbell.py`, `test_codex_delivery.py`, `test_coordination_adapter.py`, `test_repository_claims.py` I/mixed; add full binary identity/attachment/recovery tests | deferred |
| CLI-DISPATCH | 2 | All 21 modes, aliases, unknown-mode exit 2, help bytes and runtime version metadata, light import/startup. `cli.py` and help fixture | `test_cli_dispatch.py`, `test_release_ux.py`, `test_client_install_ux.py` I/A; add argv subprocess records | deferred |
| CLI-LEASE | 2 | OS lock truth, crash release, FIFO tickets, board mirror, check/run/hold/list/break and child status. `lease_cli.py`, `os_lock.py` | `test_lease_cli.py`, `test_lease_cli_board.py`, `test_coordination_leases.py`, `test_coordination_leases_api.py` mixed/I; subprocess nodes suitable after dispatcher audit | deferred |
| CLI-MAIL | 2 | `.seen`/digest watermark race, exits 0 mail/3 timeout/2 setup, output and durable wait cleanup. `wait_mail_cli.py`, `private_state.py` | `test_wait_mail_cli.py`, `test_coordination_mail_continuity.py`, `test_stop_wake_hook.py` mixed/I/A | deferred |
| CLI-HOOK | 2 | Briefing text and bounded hook JSON, memory-change note; episode start/end CLI exit/output. `briefing_cli.py`, `episode_cli.py`, `web/session_hook.py` | `test_briefing.py`, `test_episode_cli.py`, `test_memory_changes_hook.py`, `test_web.py` I/mixed; add fake HTTP server subprocess cases | deferred |
| CLI-DOCTOR | 2 | Read-only diagnostics default, disposable proof explicit, daemon and identity/transport readiness, no incidental mutation. `doctor_cli.py`, `coordination_proof.py`, `wake_liveness.py` | `test_doctor_cli.py`, `test_doctor_coordination.py`, `test_coordination_proof.py`, `test_coordination_probe.py` I/mixed | deferred |
| CLI-CONNECT | 2 | Origin/credential validation, verify-before-write, all-or-nothing backup/rollback/dry-run, provider registration. `connect_cli.py`, `client_config.py`, `client_updates.py` | `test_connect.py`, `test_client_credentials_setup.py`, `test_client_sessions.py`, `test_client_environment.py`, `test_client_install_ux.py` I/mixed | deferred |
| CLI-AUDIT | 2 | Export/verify/redact/stats, chain/head semantics, reports and body-expiry distinctions. `board_audit_cli.py`, `board_audit_stats.py`, `storage/coordination.py` | `test_board_audit_cli.py`, `test_board_audit_stats.py`, `test_coordination_audit.py`, `test_coordination_report.py` I/mixed | deferred |
| CLI-BACKUP | 2 | pg_dump + compressed state, no create-on-backup, exclusions and post-success-only own-file rotation. `backup_cli.py` | `test_backup_cli.py`, `test_ops_backup_integrity.py`, `test_bank_dumps.py` I/mixed; add fake pg_dump subprocess fixtures | deferred |
| CLI-TRANSFER | 2 | ZIP/JSONL manifest, all-table roster, float4/ids/HLC/JSONB/time/sequences, empty-bank/dim/column/active-connection refusal. `transfer_cli.py` | `test_transfer_cli.py` direct calls I/DB; add real CLI export/import against disposable banks | deferred |
| CLI-UNALLOCATED | 2/5 | `tunnel`, `channel`, `coordination-recovery`, `embedded`, `version/help` are real supported modes beyond phase-2 leaf list. `tunnel_*.py`, `channel.py`, `coordination_recovery.py`, `cli.py` | `test_tunnel_cli.py`, `test_tunnel_service.py`, `test_tunnel_bridge.py`, `test_tunnel_profiles.py`, `test_tunnel_runtime.py`, `test_coordination_recovery.py`, `test_channel.py` I/mixed. Assign ownership before phase completion; legacy embedded/file/channel behavior cannot silently disappear | deferred |
| HTTP-SECURITY | 3 | Health public/degraded semantics; MCP/REST bearer gates on UTF-8/Latin-1 bytes, fail-closed map, remote bind/trust policy, DNS/Origin/rebinding, JSON/body limits, error status, redirect refusal. `daemon.py`, `web/api.py`, `principals.py`, `utils/no_redirect.py` | `test_daemon_http.py` P/I; `test_web.py`, `test_principals.py`, `test_extractor_no_redirect.py`, `test_shim_transport_recovery.py`, `test_dim_mismatch_health.py` I/mixed | deferred |
| HTTP-STATIC | 3 | Root redirect, both Console assets/UI paths, traversal/security/content types. `web/api.py`, `web/static/` and `web/static/next/` | `test_web.py` I/mixed; `test_console_static_js.py`, `test_console_next.py`, `test_coordination_console_ui.py` A. Folding frontends needs a decision | deferred |
| HTTP-READS | 3 | Every GET Console route including configuration, episodes, graph/review/provenance and telemetry reads. `web/routes.py`, `web/config_io.py` | `test_web.py`, `test_coordination_console.py`, `test_coordination_web.py`, `test_web_hardening.py` I/mixed; add HTTP corpus for every GET route | deferred |
| PG-HYDRATE | 3 | Exact v52 rows/types/defaults/indexes/extension/vector(1024), idempotent startup/restarts, malformed-state refusal and single writer. `storage/schema.py`, `postgres.py`, `sync.py`, `service.py` | `test_schema_version.py`, `test_schema_ddl_shape.py`, `test_schema_healing.py`, `test_pg_storage.py`, `test_pgvector_compat.py`, `test_fail_closed_hydration.py`, `test_hydrate_capacity.py`, `test_storage_connect.py`, `test_writer_lease.py`, `test_disposable_database_guard.py` I/DB; catalog fixture checks can observe Rust directly | deferred |
| EMBEDDING | 3 | Qwen 1024/MiniLM 384, document/query asymmetry, dtype, cache, dimension refusal, offline load-only validated ONNX artifacts. `memory/embedding.py`, `onnx_artifacts.py`, `utils/config.py` | `test_embedding_backend.py`, `test_embedding_precision.py`, `test_embedding_asymmetry.py`, `test_query_side_encoding.py`, `test_embedding_dim_guard.py`, `test_embedding_cache.py`, `test_embedding_cpu_dtype.py`, `test_onnx_artifacts.py`, `test_onnx_model_provision.py` I/A; real-model subprocess corpus needed | deferred |
| RETRIEVAL | 3 | Five pools, exact cosine/tie order, source/episode/tag/band filters, BM25 fusion, recency/retention, supersession pointers, abstention and strict rerank whole-pool budget. `memory/cms.py`, `miras/`, `bm25.py`, `reranker.py`, `abstain.py` | `test_retrieval_golden.py`, `test_retrieval_pool.py`, `test_retrieval_replay.py`, `test_band_cosine.py`, `test_band_ablation_flat.py`, `test_bm25.py`, `test_cortex_bm25.py`, `test_reranker.py`, `test_reranker_margin_gate.py`, `test_abstain.py`, `test_retention_boost.py`, `test_superseded_visibility.py`, `test_meta_filter.py` I; add exact order corpus, floors alone insufficient | deferred |
| READ-CORE | 3 | memory_get/recent/fact_get/world_search/lesson_search/history/stats/recall/episode summaries, compact/verbose/explain payloads. `service.py`, `mcp_server.py`, `memory/recall.py`, `context_builder.py`, cortex/world/lessons | `test_mcp_server.py`, `test_recall.py`, `test_constraint_pinning.py`, `test_cortex.py`, `test_world_cortex.py`, `test_lessons_service.py`, `test_episode_service.py`, `test_mcp_server.py` I/mixed; full MCP response corpus needed | deferred |
| WRITE-CMS | 4 | Store surprise admission, non-destructive possible conflicts, bounded capacity, explicit supersede/reinstate/consolidate/forget/reinforce semantics and audit lineage. `cms.py`, `contradiction.py`, `consolidation.py`, `compaction.py`, `service.py` | `test_service.py`, `test_consolidation.py`, `test_consolidation_atomicity.py`, `test_correction_atomicity.py`, `test_correction_identity.py`, `test_entry_reinstatement.py`, `test_forget_cascade.py`, `test_access_count_semantics.py`, `test_store_retirement.py`, `test_slot_keyed_supersession.py` I; add before/after rows and restart corpus | deferred |
| WRITE-CORTEX | 4 | HLC authority, scalar/set slots, contenders/promotions, provenance trust, freshness/stale, labels, canonical and world/lesson histories and engrams. `hlc.py`, `cortex.py`, `world_cortex.py`, `lessons.py`, `freshness.py`, `labels.py` | `test_hlc.py`, `test_cortex_sets.py`, `test_cortex_contenders.py`, `test_cortex_promotion.py`, `test_assistant_provenance.py`, `test_contender_stamps.py`, `test_freshness.py`, `test_label_pair.py`, `test_lessons_storage.py`, `test_entity_provenance.py` I/DB | deferred |
| WRITE-EPISODES | 4 | Daemon-owned session handles, pointers/tombstones, nested episodes, reap/resume/title stamps, isolated caller identity and inferred outcomes. `episodes.py`, `writer_context.py`, `service.py`, `web/session_hook.py` | `test_session_identity.py`, `test_episodes.py`, `test_episode_service.py`, `test_episode_service.py`, `test_session_title.py`, `test_outcome_inference.py` I/mixed; add two-client restart/reap corpus | deferred |
| BACKGROUND | 4 | Autosave/errors, clean-exit flush, release check, heap trim, memory headroom, retry initialization and lock separation/cancellation. `daemon.py`, `service.py`, `release_check.py`, `utils/` | `test_loop_health.py`, `test_service_lock_discipline.py`, `test_service_lock_discipline.py`, `test_init_retry_model_reuse.py`, `test_heap_trim.py`, `test_memory_headroom.py`, `test_update_offer.py`, `test_connection_loss_recovery.py` I/mixed; observed subprocess lifecycle and durable restart cases required | deferred |
| DREAM | 4 | Pull/ack/commit cursors, scheduled quiescence, retry/fallback/extractor protocol/redirection, atomic run rollback, quarantine, literal gate, digests/chronicle. `service_dream.py`, `memory/dream.py`, `dream_token.py` | `test_dream.py`, `test_dream_acknowledgement.py`, `test_dream_ack_storage.py`, `test_dream_runs.py`, `test_dream_stall.py`, `test_dream_quarantine.py`, `test_dream_chronicle.py`, `test_session_digest.py`, `test_extractor_fallback.py`, `test_extractor_no_redirect.py`, `test_literal_gate.py` I/DB; fake sidecar E2E and ladder evidence | deferred |
| GRAPH-REVIEW | 4 | NetworkX-derived graph/order, communities, aliases/relations, deep dream, safe cleanup, scoped proposals/merge veto/fold direction, review queue/judges/audit. `graph.py`, `memory/graph_*.py`, `review_*.py`, `relation_quality.py`, `service_dream.py` | `test_graph.py`, `test_graph_store.py`, `test_graph_insight.py`, `test_graph_review.py`, `test_graph_consolidation.py`, `test_deep_dream.py`, `test_merge_queue.py`, `test_review_decisions.py`, `test_review_judgment_freshness.py`, `test_queue_judges_service.py`, `test_entity_proposals.py`, `test_edge_proposals.py`, `test_relation_quality.py` I/DB | deferred |
| BOARD-STORE | 4 | All 15 actions, FIFO durable leases, nonce/request dedup, mailbox authorization/clock/retention/redaction/hash chain, bank identity/recovery. `coordination.py`, `storage/coordination.py`, `private_state.py` | `test_coordination_integration.py`, `test_coordination_storage.py`, `test_coordination_tools.py`, `test_coordination_auth.py`, `test_coordination_audit.py`, `test_coordination_clock.py`, `test_coordination_recovery.py`, `test_coordination_secrets.py`, `test_coordination_multi_ack.py`, `test_coordination_mail_continuity.py` I/DB/mixed | deferred |
| BOARD-ASYNC | 4 | Receive <=30s, max64 waiters/16 permits/4-worker isolation, cancellation retains capacity, subscribe-before-read, generations/attachments and notifier shutdown. `web/coordination.py`, `coordination_adapter.py` | `test_coordination_wait.py`, `test_coordination_dispatch_isolation.py`, `test_coordination_wait.py`, `test_coordination_adapter.py` I/mixed; add concurrent wire disconnect/timeout/restart cases | deferred |
| DELIVERY | 4 | Codex app-server messages/doorbell, opt-in host transports, wake/park leases and Claude channel behavior. `codex_delivery.py`, `codex_doorbell.py`, `codex_connection.py`, `channel.py`, `wake_liveness.py` | `test_codex_delivery.py`, `test_codex_doorbell.py`, `test_codex_doorbell_probe.py`, `test_codex_coordination.py`, `test_channel.py`, `test_stop_wake_hook.py`, `test_coordination_turn_digest.py` I/mixed/A; fake local host E2E only | deferred |
| HOOKS | 4 | Eight endpoint authorization/loopback/method/output/identity contracts; plugin 11 handler entries and paired shell/PowerShell scripts; Codex launcher approval identity. `web/api.py`, `session_hook.py`, `plugin_hooks.py`, `plugin/hooks/`, `ops/setup-codex-hooks.py` | `test_web.py`, `test_session_identity.py`, `test_hooks_digest.py`, `test_codex_hooks.py`, `test_codex_hook_launcher.py`, `test_codex_hook_setup.py`, `test_codex_reapproval.py`, `test_hook_glob_ranges.py`, `test_plugin_packaging.py` I/mixed/A; changing hooks.json definitions imposes renewed user approval | deferred |
| HTTP-WRITES | 4 | Every POST Console route, coercion/error mapping, config whitelist/live/restart classification, backup preservation. `web/routes.py`, `config_io.py` | `test_web.py`, `test_console_knob_gapfill.py`, `test_correction_interfaces.py`, `test_web_hardening.py` I/mixed; record POST routes and their storage effects | deferred |
| STORAGE-COMPAT | 3/4/5 | Embedded PG18 lite startup/stable data-dir, files `.pt` state+import/partial restore/weights, Chroma reference-bank ingest/search, parser/chunk rules. `storage/embedded_pg.py`, `migrate.py`, `memory/reference_bank.py`, `document_parser.py`, `utils/atomic_io.py` | `test_embedded_pg.py`, `test_cms_pt_schema.py`, `test_cms_legacy_load.py`, `test_migration.py`, `test_restore_from_pt.py`, `test_atomic_weights.py`, `test_reference_bank.py`, `test_document_parser.py`, `test_document_ingest_batch_bound.py` I/mixed. No simplification is pre-authorized | deferred |
| OPS-CUTOVER | 5 | CPU/offline images, models, compose byte equality, update backup/rollback/daemon-only health/clients, runtimes beside current sessions, unattended upgrade policy, four release surfaces. `ops/Dockerfile.daemon`, compose copies, `update_cli.py`, `unattended_update.py`, `runtimes.py`, `.github/workflows/` | `test_daemon_image_is_cpu_only.py`, `test_update_cli.py`, `test_ops_update_wrappers.py`, `test_shim_runtimes.py`, `test_unattended_update.py`, `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_ops_script_modes.py` I/A/mixed; fresh disposable install/update proof, no deploy | deferred |
| DOCS-CUTOVER | 5 | Guide/provider/tunnel/config/schema/atlas/defaults/help/LLMS and benchmark claim currency, oracle retained pending maintainer retirement. `docs/guide/`, `docs/atlas/`, READMEs, `llms*`, `server.json`, `pyproject.toml`, release manifests | `test_release_ux.py`, `test_atlas_currency.py`, `test_llms_txt.py`, `test_i18n_readme.py`, `test_eval_evidence.py`, `test_extractor_model_lists.py`, `test_repository_claims.py` A; regenerate LLMS after doc edits | deferred |


## Per-phase selection and acceptance gaps

Phase 0: unchanged Python control over `test_shim.py`, `test_daemon_http.py`, selected recovery boundary nodes and docs/evidence guards; new generated corpus/schema snapshot/harness negative controls. Baselines must identify backend and use quiet CPU host with lease free. Disposable trial (three bounded units, one implementer/two adversarial reviewers, retain rulebook corrections only) is still a deliverable, not satisfied by this inventory.

Phase 1: selected real stdio/HTTP nodes in `test_shim.py` plus audited subprocess recovery cases; original entire shim/credential/tier/identity/recovery file pools remain Python. `test_shim_autostart.py`, `test_shim_python.py` and `test_shim_prompt.py` are extractor tooling, not MCP shim coverage; do not select them based on name alone. For `_proxy` private snippets add equivalent binary probes rather than claim they exercise Rust.

Phase 2: subprocess nodes audited from lease/mail/doctor/connect/briefing/episode leaves. Existing backup/transfer/audit/CLI dispatcher tests largely call functions directly or install Python fakes; they remain oracle, with new argv/HTTP/fake-executable and disposable-PG cases. Explicitly allocate tunnel/recovery/channel/embedded/version/help modes. These cannot become unspecified deferrals at a purported completed cutover.

Phase 3: real boundary nodes in daemon HTTP; every Console GET and MCP read response differential; v52 catalog/rows, binary restart/hydration checks and frozen real-model exact-order corpus. All Python embedding/CMS/BM25/reranker/recall units remain control plus Rust equivalents. Cannot claim same default ONNX graph or derive exact order parity from current quality floors.

Phase 4: complete MCP mutation/POST/hook/15-action wire corpus plus disposable rows before/after and restart, fake extractor/host transport tests, cancellation/ownership/concurrency fault cases. Existing storage/service/session/dream/graph/board tests remain Python control with named Rust unit counterparts. Full suite required for process lifecycle/shared harness infrastructure where CLAUDE applies; runtime dispatch adapters cannot bypass admission.

Phase 5: artifact/static guards can run unchanged because they inspect files; direct Python update/runtime tests remain oracle until maintainer decides retirement. New fresh-install/update/restore proofs must exercise the actual Rust installed entry points. Do not delete packaging/source the guards inspect just to make them pass. A documented dependency-light shim includes package install/runtime/provider registration, not binary compilation alone.


## Internal-only gaps requiring explicit equivalents

1. Python registration/thread wrappers and sync-callable tool function constraints (`test_mcp_server.py`) are implementation assertions; preserve observable concurrency/annotations/schema/help via wire and test Rust ownership/thread behavior beside the port.
2. Most REST tests instantiate `build_console_app(stub_mcp, ..., FixtureService)`; this is ASGI in one Python process, not a daemon boundary. Mocked service forwarding tests remain unit oracle; add real network cases with deterministic synthetic bank and response expectations.
3. `pg_service`, `pristine_service`, file-mode CMS, direct `PostgresStorage`/cortex/graph/embedding objects and monkeypatches cannot point at a Rust daemon through an environment variable. Translate assertions to additive boundary fixtures where possible; direct migration/DDL/storage helpers require Rust units and shared catalog/result contracts.
4. Existing transport recovery creates Python `_proxy` programs and inspects internal error classifier/cancellation routines; preserve them unchanged and add binary fault server cases for redirect refusal, lost response, token rotation, event-size limits and no uncertain-write replay.
5. HLC wall clock, ordering, equal-score ties, stale thresholds and retention need deterministic time-aware cases; normalizing away these fields destroys coverage. Compare state transitions, not merely final counts.
6. ML defaults and optional fallback behavior are public contracts: ONNX runtime alone does not port sentence-transformers preprocessing/tokenizer/pooling/Dense/Normalize or cross-encoder inference. Record unsupported module/fallback behavior explicitly; no permission to retire torch fallback, NLI, Chroma, `.pt`, either Console or embedded/channel mode is implied.
7. Public PII/evidence guards may need additive new guard tests outside immutable existing files; schema is fixed v52 until at least phase 5, so normal seven-place schema updates cannot happen during phases 1–4. Resolve this before any claimed schema change.
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

Hook paths (web/api.py; phase 4; oracle test_web.py/test_session_identity.py): session-start, memory-policy, memory-changes, session-end, coordination-start, park-gate, woke, subagent, all under /api/hook/.

Coordination action rows (coordination.py; phase 4 with phase-1 shim clients): context, register, update, agents, lease, release, leases, attach, heartbeat, detach, send, receive, history, ack, attempt, all under /api/coordination/. Compare each action's parameter whitelist, required fields, principal/instance rules, status and public error codes. Unknown actions fail; receive alone accepts wait_seconds under the bounded hub contract.

## Hook endpoints acceptance checklist

Source: `pseudolife_memory/web/api.py`. Each item requires normal, invalid-input, authentication and durable-effect evidence as applicable.

| Item | Phase | Status | Evidence |
|---|---|---|---|
| session-start | 4 | deferred | No accepted Rust evidence |
| memory-policy | 4 | deferred | No accepted Rust evidence |
| memory-changes | 4 | deferred | No accepted Rust evidence |
| session-end | 4 | deferred | No accepted Rust evidence |
| coordination-start | 4 | deferred | No accepted Rust evidence |
| park-gate | 4 | deferred | No accepted Rust evidence |
| woke | 4 | deferred | No accepted Rust evidence |
| subagent | 4 | deferred | No accepted Rust evidence |

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

## Upstream drift: target decision pending

The register above describes the historical Python oracle at
`136a34ae95e981a691fcc31ba9fb4f35d83d4249`, schema 52. The integration branch
now includes master `d398068743e7276e884bd2693501484b83f981e9`, schema 53.
Integrating that tree has not changed the port target, retired an oracle
behavior or established additional Rust parity. The maintainer's decision
on the production target remains pending; the brief's schema-52 constraint
and existing measurement provenance remain in force.

Source and test paths in the historical register are references to the pinned
commit, including files subsequently removed upstream. In particular,
`pseudolife_memory/web/static/js/views/console.js`,
`pseudolife_memory/web/static/next/index.html`, `tests/test_console_next.py`,
`tests/test_console_static_js.py` and `tests/test_coordination_console_ui.py`
exist at the pinned commit; their absence from integration master does not
authorize deleting their historical contracts. Resolve historical paths with
`git show 136a34ae95e981a691fcc31ba9fb4f35d83d4249:<path>`. The source references
in the following delta table instead identify integration master at
`d398068743e7276e884bd2693501484b83f981e9`.

| Delta | Source at integration master | Target status |
|---|---|---|
| Schema 53 adds durable principals, hashed credentials, tier/board policy and revocation behavior. | `pseudolife_memory/storage/schema.py`, `pseudolife_memory/principal_store.py`, `pseudolife_memory/principals.py` | Maintainer decision pending |
| Four public CLI modes are added: `invite`, `pair`, `expose`, `move` (21 modes become 25). | `pseudolife_memory/cli.py`, `pseudolife_memory/invite_cli.py`, `pseudolife_memory/pair_cli.py`, `pseudolife_memory/expose_cli.py`, `pseudolife_memory/move_cli.py` | Maintainer decision pending |
| `POST /api/pair` redeems pairing codes; stored-principal failures introduce unavailable-identity responses. | `pseudolife_memory/web/api.py`, `pseudolife_memory/principals.py` | Maintainer decision pending |
| `/health` includes a bank fingerprint; daemon startup refuses moved or unfinished-move state. | `pseudolife_memory/daemon.py`, `pseudolife_memory/move_cli.py` | Maintainer decision pending |
| `wait-mail` wakes on a daemon ring rather than plain addressed mail. | `pseudolife_memory/wait_mail_cli.py`, `pseudolife_memory/coordination_adapter.py` | Maintainer decision pending |
| Subagent mail-notice attribution and optional Codex mailbox-tool approval change client behavior. | `pseudolife_memory/coordination_adapter.py`, `ops/setup-codex-hooks.py` | Maintainer decision pending |
| Console v3 replaces classic assets at `/ui/`; `/ui/next/` is removed, configuration exposes saved restart values and frontend CI is added. | `pseudolife_memory/web/api.py`, `pseudolife_memory/web/config_io.py`, `frontend/src/lib/dreamer.ts`, `.github/workflows/ci.yml` | Maintainer decision pending |
| Classic Console test files are removed upstream and replaced by frontend/source/build coverage; shared Python fixtures also change upstream. | `tests/test_console_build.py`, `tests/test_console_source_guards.py`, `tests/conftest.py`; historical deletions listed above | Separate integration coverage; historical oracle retained |

These are source-level scope differences, not measurements or accepted parity
results. Current-master validation and pinned-oracle validation are distinct
evidence. Preserve original artifacts with their recorded source identity;
any approved target change requires explicit parity updates and separately
identified comparison evidence.
