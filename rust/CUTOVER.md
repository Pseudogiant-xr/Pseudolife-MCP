# Rust cutover checklist (draft)

The Rust daemon is being prepared to run beside the Python daemon on disposable
state. This checklist records the work still needed before a default switch;
it does not authorize that switch, a release, an update, or a deploy.

Source audit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`, 2026-10-10.
All five scoped rows in [PARITY.md](PARITY.md#parity-rows) are **deferred**.
Every item below is **pending cutover acceptance**, including items whose
Python implementation or static guard already exists. A source reference is
not an execution result. Record later evidence against the actual tested head,
command, platform, exit code and CI run; do not reuse an older head's proof.

## Acceptance boundary

| Item | State | Evidence needed / current authority |
|---|---|---|
| Whole-row closure | Pending | Close every contract item before changing a PARITY row to ported; otherwise add only the evidence actually obtained. |
| Disposable installed paths | Pending | Fresh install, update and restore through the actual installed Rust entry points; compilation and Python fakes do not establish installed-runtime parity. [Phase 5 gap](PARITY.md#per-phase-selection-and-acceptance-gaps). |
| Differential acceptance | Pending | Reuse `daemon/harness/run.py`, `compare.py` and `dbstate.py`: response values and consumer-read headers, touched and potentially touched DB rows after writes, and files in isolated client homes. Retain declared normalizers, raw failures, and caught source mutants. |
| Platform and hosted gates | Pending | Linux disposable proofs, native Windows evidence for OS-sensitive client/process/file behavior, and completed exact-head Rust/Parity CI on both OSes; required repository guards and current-master integration. |
| Independent reviews | Pending | Required independent reviews and findings/dispositions in the PR body; a fixed finding receives a covering case and watched passing rerun. |
| Remaining divergences | Pending | Reconcile all port slices' declared divergences; the union must be empty or explicitly accepted by the maintainer. Existing unsupported embedded, document, model and client paths are not silently retired. |
| Maintainer execution | Pending | Default switch, release, update behavior changes, live-bank mutation and deploy remain separate maintainer decisions. Keep the Python oracle until its retirement is explicitly decided. |

## OPS-CUTOVER

Current sources: `ops/Dockerfile.daemon`, both compose files in `ops/` and their
copies in `pseudolife_memory/compose/`, `pseudolife_memory/update_cli.py`,
`unattended_update.py`, `runtimes.py`, and `.github/workflows/`.

| Item | State | Evidence needed |
|---|---|---|
| CPU image | Pending | Inspect built Rust image/runtime dependencies and execute on CPU with GPU hidden; retain Python CPU guards (`test_daemon_image_is_cpu_only.py`). The Python lock/install assertions alone do not inspect Rust dependencies. |
| Offline image and models | Pending | Cold start and authenticated model-dependent read without runtime downloads. Python bakes Qwen3 and MiniLM; Rust uses a verified Qwen3 ONNX export and dynamic ONNX Runtime (`daemon/src/embed.rs`, `daemon/Cargo.toml`, `daemon/harness/run.py`). Package tokenizer/config/artifacts and library paths; do not treat an ONNX graph as proof of all Python model/fallback contracts. |
| Image ONNX graph agreement | Blocked for cutover | The Rust image's ONNX graph is not the verified export; numerical parity unproven. Public export `onnx-community/ONNX_Qwen3-Embedding-0.6B` at `462e5a71e724575c710975d9b79309b690fd22ce` has graph SHA256 `cec22565ec783289a5e51bd94950f70b8cb7ca6c0b7ced255b5e8dbf3c60536b`; the historical prerequisite uses `f5fdee679d0ffb98e1cb0bb178e8ee2b86739ab7ab035a3a9327761d5c6b1f0b`. External fp32 tensors and tokenizer match. Before switching defaults, bake the verified export or prove top-8 ranking overlap mean ≥ 0.95 with no query below 0.75; until then keep this image opt-in. |
| Container runtime contract | Pending | Compare entry point, runtime user, bind/auth guard, port 8765, `/data` persistence/config, build stamp and health check. `ops/Dockerfile.daemon` has no explicit `USER`; do not invent an existing non-root guarantee. |
| Compose byte equality | Pending | Preserve `ops/docker-compose.yml` and `docker-compose.ghcr.yml` byte-for-byte against their bundled copies; run `test_update_cli.py::test_bundled_compose_files_match_the_checkouts`. An opt-in Rust file must leave the default stack unchanged. |
| Compose resources and isolation | Pending | Disposable side-by-side run on a separate port/bank: loopback publication, own project/resources, PostgreSQL healthy dependency, external production volume semantics, `/var/lib/postgresql` PG18 mount, daemon memory/swap cap, log rotation and internal-only extractor. Verify only owned disposable resources are cleaned up. |
| Backup before update | Pending | Real disposable dump plus state archive, complete-dump marker, failure/refusal before movement, correct checkout/bundled backup selection and original env/volume identity. `test_update_cli.py`, `test_ops_backup_integrity.py`. |
| Rollback before movement | Pending | Tag the running validated image, preserve collision/refusal rules, demonstrate rollback instructions and disposable recovery after unhealthy or version-mismatched replacement. `test_update_cli.py`, `test_ops_prune_rollbacks.py`. |
| Daemon-only recreation | Pending | Prove `up -d --no-deps` changes only the daemon, keeping PostgreSQL, extractor and bank/state volumes; preserve checkout build stamp and release pull paths. `test_update_cli.py`, `test_ops_update_wrappers.py`. |
| Health before clients | Pending | Successful expected-version health gate before client movement; unhealthy/mismatched health exits failure and moves no clients. Exercise the changed authenticated path, not health alone. Health appendix below remains required. |
| Release versus checkout update | Pending | Preserve newest/pinned release, downgrade/refusal and no-op semantics; checkout clean-tree/build/rollback safeguards and `--all`; `--daemon-only`, `--clients-only`, pip/lite/client-only routing and wrapper exit codes. Preparation changes no updater behavior. |
| Client completion and failures | Pending | Actual shim runtime, plugin cache and supported hook/client steps; daemon success plus client failure remains a failed client step (current updater exit 5). Preserve consent/ownership and useful next-command diagnostics. `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_update_cli.py`. |
| Runtime beside current sessions | Pending | Install/select a complete new runtime while an old session keeps working; next session uses the new runtime; keep running, registered and frozen-tunnel runtimes; fail closed on unreadable process/tunnel evidence. `test_shim_runtimes.py`. |
| Unattended daemon upgrade policy | Pending | Opt-in knob plus no active board session, readable board and update lock; unavailable/unknown board holds off; idle sessions retain runtimes. Preserve notices/logs and exits 0 updated, 3 current, 4 held, 2 unable to check, 1 failed. `test_unattended_update.py`, `unattended_update.py`. |
| Unattended client policy | Pending | Opt-in installed-runtime launch, correct daemon release target, one concurrent attempt and retry interval/result persistence; first successful frame and cancellation ownership. `test_update_offer.py`, `test_shim_runtimes.py`; see SHIM-CLIENT-UPDATES. |
| Fresh install/update UX | Pending | Detect host/tier/provider, finish chosen setup automatically, rerun safely, skip interactive work without hanging when unattended, and refuse before partial mutation. Contributor test-login setup stays confined to selected contributor paths. `CLAUDE.md`, `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_ops_script_modes.py`. |
| GitHub release surface | Pending | Future maintainer-controlled release procedure, version/notes/artifacts and release gate; current `.github/workflows/release.yml` and `.claude/skills/release-procedure/SKILL.md` remain authority. No release workflow change or publish in preparation. |
| PyPI surface | Pending | Future installed package/entry-point proof, version agreement and completed human-approved publisher evidence. Preserve source/packaging inspected by existing guards. |
| MCP registry surface | Pending | Future `server.json`/package identity/version agreement, registry metadata and served-version verification under the existing release procedure. |
| Plugin marketplace surface | Pending | Future manifest/marketplace/hooks currency, cache identity/digest and new-session adoption while current sessions retain their installed code. |
| GHCR image artifacts | Pending | Future maintainer-authorized versioned daemon/PG artifacts and inspectable revision/build metadata; preparation CI builds only and never pushes. GHCR images are additional release artifacts, not a substitute for any of the four public surfaces above. |

## OPS-IMAGES

| Item | State | Evidence needed / current source |
|---|---|---|
| Extractor model | Pending | `ops/Dockerfile.extractor`: baked default GGUF, optional `MODEL_URL`, runtime model path `/models/extractor.gguf`; inspect artifact and run disposable CPU/offline serving proof. Preserve default/model currency guards. |
| Extractor config | Pending | OpenAI-compatible CPU server, bind `0.0.0.0`, internal port 8081, context 8192, Jinja, parallel 1 and idle sleep 300/default override. Dockerfile CMD and compose command stay aligned (`test_extractor_idle_sleep.py`). |
| Extractor health | Pending | Compose overrides inherited health to `/health` on 8081; health answers while sleeping without waking the model; next completion reloads. Disposable startup/sleep/completion evidence, not model-list guards alone. |
| PostgreSQL build/runtime | Pending | `ops/Dockerfile.pg`: `pgvector/pgvector:pg18`, OS package updates, PostgreSQL 18 and pgvector availability; disposable init, vector access, persistence/recreate and dump/restore compatibility using intended PG18 mount. |
| Daemon CPU/offline | Pending | All OPS-CUTOVER image/model/runtime items above, Python image left intact, built Rust image inspection and network-independent execution. `test_daemon_image_is_cpu_only.py`, `test_release_ux.py`. |
| Compose and image selection | Pending | Byte equality and preserved release/local image semantics; no default Rust image selection. `test_update_cli.py`, `test_extractor_model_lists.py`. |
| Disposable image CI | Pending | Exact-head no-push build/start, disposable PostgreSQL, healthy `/health`, authenticated read and owned-resource cleanup; retain successful run URL and logs. Existing `.github/workflows/rust.yml` is the integration point. |

## OPS-INSTALL-RESTORE

For each script, preserve its canonical flags, preflight checks, refusal/exit
codes, rerun behavior, rollback and platform/client ownership. Exercise real
scripts with disposable homes, banks and stubbed platform registration where
needed; static assertions and Python fakes remain oracle evidence.

| Script item (`ops/` relative) | State | Evidence needed / guard |
|---|---|---|
| `install.sh`, `install.ps1` | Pending | Fresh tier/provider selection, complete setup, noninteractive/rerun behavior, credentials, runtime selection and fallbacks; `test_client_install_ux.py`, `test_client_credentials_setup.py`. |
| `install-autostart.ps1` | Pending | Selected daemon/interpreter/data/DSN/environment and Windows task ownership/start behavior in an isolated platform fixture. |
| `install-shim-autostart.sh`, `install-shim-autostart.ps1` | Pending | Claude extractor CLI/interpreter/model/prompt, bind verification, replacement/restart and sibling mode cleanup; `test_client_install_ux.py`, `test_extractor_model_lists.py`. |
| `install-codex-shim-autostart.sh`, `install-codex-shim-autostart.ps1` | Pending | Codex counterpart model/health/interpreter and sibling cleanup with matching platform behavior; same guards. |
| `install-hook.sh`, `install-hook.ps1` | Pending | Supported client hook store, prompt/session wiring, trust-review consent and ownership; preserve definitions users approve. `test_client_install_ux.py`. |
| `restore.sh`, `restore.ps1` | Pending | Disposable rehearsal and explicit apply; safety dump, drop/create failures, close PUBLIC before replay, every-table loss checks, held-dump selection/refusal and no-start semantics. `test_ops_restore_rehearsal.py`, `test_ops_restore_skips_held.py`. |
| `restore_from_pt.py` | Pending | Additive same-era snapshot restore, retain existing rows/source backups, stopped-daemon rehydration and dimension refusal before connection/write. Python `.pt` support is not retired by this draft. |
| `preflight.sh`, `preflight.ps1` | Pending | Selected providers/tier/CLI/pipx checks, official install layouts and useful refusals; `test_client_install_ux.py`. |
| `migrate-pg18.ps1` | Pending | Disposable dump/restore/verification across source major, target PG18 layout, retained old-volume rollback and failure paths; no live migration. |
| `backup.sh`, `backup.ps1` | Pending | Complete dump and state tar, marker/integrity checks, row-count manifest/baseline/drop hold, mirror/rotation safety and daemon backup record; `test_ops_backup_integrity.py`. |
| `install-backup-task.ps1` | Pending | Main/script checkout selection with output kept in main backup directory, installing-user task, absolute PowerShell path, missed-run behavior, log/HEAD and nonzero failures; `test_ops_install_backup_task.py`. |
| `prune-rollbacks.sh`, `prune-rollbacks.ps1` | Pending | Keep newest configured count, deployed/dangling/in-use protection and quiet no-op; `test_ops_prune_rollbacks.py`. |
| `prune-build-cache.sh`, `prune-build-cache.ps1` | Pending | Age/cap retention, repeated measured progress with budget/no-progress stop, dry-run, safe Docker verbs, optional Docker Desktop trim; `test_ops_prune_build_cache.py`. |
| `install-cache-retention.ps1` | Pending | Absolute/stable PowerShell executable including MSIX alias, correct encoded script/retention parameters and task scope; `test_ops_install_cache_retention.py`. |
| `register_claude_desktop.py` | Pending | Own-entry merge/migration, preserve foreign entries/settings, absolute launcher and packaged Windows config path, owner-only token-file handling and capability probe/refusals; `test_register_claude_desktop.py`, client credentials/UX guards. |
| `setup-codex-coordination.py` | Pending | Effective config/identity authority, opt-in messaging and wake capability, private backup and preserved preexisting settings; real installed client with disposable config. |
| `update.sh`, `update.ps1`, `update.py` | Pending | Every canonical flag/default maps to the checkout updater, streamed output and exact exit status; `test_ops_update_wrappers.py`, OPS-CUTOVER update sequence. |
| `update_clients.py` | Pending | Separate runtime/plugin/hook/provider install steps, owned cache/config, effective credentials and failed-step exit; actual installed-runtime proof with old sessions present. |
| Executable script modes | Pending | All tracked `ops/*.sh` retain index mode 100755 for Linux/macOS direct invocation; `test_ops_script_modes.py`. PowerShell has a separate native parse/runtime proof. |

## SHIM-CLIENT-UPDATES

| Item | State | Evidence needed / current source |
|---|---|---|
| Installer-backed unattended execution | Pending | Real installed client starts the intended updater after its first successful frame, only under configured policy; captures process ownership, concurrent startup, crash/failure/retry and persisted results. `unattended_update.py`, `client_updates.py`, `shim.py`, `test_update_offer.py`. |
| Installed runtime selection | Pending | Correct launcher, newest complete runtime marker and dependency closure; incomplete install is not selected, running/registered/tunnel-pinned runtimes survive pruning. `runtimes.py`, `test_shim_runtimes.py`. |
| Complete client installation | Pending | Actual package install, supported Claude Code/Codex/Claude Desktop/Gemini/generic provider registration, daemon URL, no-spawn and writer settings, plugin/hooks, credentials and next-session handshake. Dependency-light binary compilation alone is insufficient. |
| Client environment isolation | Pending | Remove inherited credentials/connections before importing test helpers; disposable home and explicit endpoint; owner-only files and Windows ownership/locale behavior. `test_client_environment.py`, `test_client_credentials_setup.py`. |
| Update offer/defaults | Pending | Check enabled by default, unattended clients/daemon off by default, valid config and release notices, installed command/launcher name and quiet current/unknown behavior. `test_update_offer.py`, `utils/config.py`. |
| Configuration ownership/recovery | Pending | Backup/preserve unrelated registrations and user credentials; refuse bad input without half-written state, keep current sessions usable and next ordinary operation successful after faults. `test_shim_runtimes.py`, `test_client_install_ux.py`. |

## DOCS-CUTOVER

| Item | State | Evidence needed / current source |
|---|---|---|
| Guides | Pending | Reconcile `docs/guide/` install, update, recovery and usage instructions with the accepted installed entry points; links/commands exercised on disposable state. |
| Providers | Pending | `docs/guide/providers.md` matches installers' supported client/transport/hook capability matrix, effective config, credential and ownership behavior; `test_client_install_ux.py`. |
| Tunnels | Pending | Guide/current tunnel runtime and CLI agree on forwarding, bridge/runtime pinning, auth, setup/refusals and recovery; named unsupported modes stay visible. |
| Configuration | Pending | Every section/field/default, missing/null/type validation and YAML/env precedence; all configuration/environment appendix items below accounted for. `utils/config.py`, `docs/guide/configuration.md`. |
| Schema | Pending | Current code version, README capability table, configuration DSN/history, schema pins/migration assertions, changelog and atlas agree. No schema bump is made here; follow all seven-place requirements if one later lands. `test_release_ux.py`, `test_schema_version.py`, `test_migrate_embeddings.py`. |
| System Atlas | Pending | Verify actual affected storage/runtime cards before updating metadata; `docs/atlas/atlas.json`, `test_atlas_currency.py`. |
| READMEs and translations | Pending | Accurate install/default/runtime/schema/capability claims across README and `docs/i18n/README.*.md`; `test_release_ux.py`, `test_i18n_readme.py`. |
| Defaults and model lists | Pending | Embedding/backend/fallback, extractor modes/models, preset/toolset and unattended defaults agree across source, menus, help and guides; no unmeasured default/recommended promotion. `test_extractor_model_lists.py`, `test_release_ux.py`. |
| Installed help and manifests | Pending | Actual installed command/help/schema/environment markers, `server.json`, `pyproject.toml` and release manifests describe shipped behavior; retain packaging/source until retirement decision. |
| LLMS generation | Pending | Regenerate with `python ops/gen_llms_txt.py` after covered doc edits, inspect `llms.txt`/`llms-full.txt` and run `test_llms_txt.py`; generation is not assumed from writing this draft. |
| Benchmark claim currency | Pending | Claims retain committed artifacts, instrument/version/retirement provenance and appropriate claim guards; no new benchmark or performance claim here. `test_eval_evidence.py`, CLAUDE.md benchmark rules. |
| Repository/publication guards | Pending | `test_repository_claims.py` remains the named coordination repository-claim guard; run all row-named guards without treating it as benchmark evidence. Inspect actual public diff/metadata and publication guard before publishing. |
| Oracle retirement | Pending | Explicit maintainer decision after installed/differential acceptance and divergence reconciliation; retain Python oracle tests, source and packaging before that decision. |

## Appendix acceptance dependencies

The configuration and environment inventories below enumerate the current
PARITY appendix names. They retain the canonical section/reader paths in
[Configuration sections](PARITY.md#configuration-sections) and
[Environment variable appendix](PARITY.md#environment-variable-appendix),
including field-level inventory in `phase1-contract-inventory.json` and reader
paths in `contract-inventory.json`. Every listed name is pending accounting
for its installed Rust consumer or an explicit maintainer-accepted disposition;
this list does not claim that every internal test variable belongs in the image.
Compare declared fields/reader paths, defaults, validation and missing/YAML/env
precedence rather than porting arbitrary Python parser behavior.

### Health and HTTP

State: **Pending**. Source: [health/body appendix](PARITY.md#health-http-bodies-and-logical-transfer),
`pseudolife_memory/daemon.py`, `web/api.py`, `rust/daemon/src/health.rs`.
Prove mandatory `status`, `version`, `schema`, `storage`, `auth`, `bank`,
`persist_errors`, `memory`; and conditional `build`, `coordination`, `updates`,
`extractor`, `stall`, `hooks_digest`, `init_refusal`, `not_ready`,
`migration_partial`, `dream_tracking_error`, `capacity_warning`,
`lesson_reconciliation_required`, `embedder`, `db`, `last_backup`.
Preserve 200 for ok/absent status, 503 for other payload status, exception 500,
the source degradation-versus-warning distinction and null bank fingerprint.

Nested items remain pending individually: `build.{git_sha,dirty,built_at,source}`;
`embedder.{backend,device,dtype}`; `last_backup.{at,age_hours,rotation}`;
`coordination.enabled` and `coordination.wake.{per_recipient_per_hour,urgent_per_sender_per_hour,nightly_total,fan_out_stagger_seconds,active_seconds}`;
`updates.{check_releases,latest_release,checked_at,unattended_clients,unattended_daemon}`;
`stall.{since,reason,consecutive_failures,last_success_at}`;
`memory.{source,current_bytes,working_set_bytes,limit_bytes,used_fraction,near_limit,events,anon_bytes,file_bytes,rss_bytes,rss_peak_bytes}`
with `events.{max,oom,oom_kill}` and absent versus null preserved.

HTTP acceptance remains pending: Console POST 262144 bytes except facts/set,
consolidate and supersede 4194304; coordination 32768; session-end 16384;
pair 1024; GET hooks and woke/subagent hooks use query/header inputs;
`/api/agents?view=coordination` projection, unknown routes and method errors.
Container health smoke covers only the exercised subset.

### Restore and logical transfer

State: **Pending dependency**, not a new port in this preparation. Physical
backup/restore and `.pt` recovery must remain distinct from logical transfer.
The [transfer appendix](PARITY.md#health-http-bodies-and-logical-transfer)
requires every exported/excluded table, `outcome_signals.used_ids`, bank-local
metadata and target emptiness rule to be preserved. Include them in disposable
export/import/restore state checks, not just counts.

Every exported table is pending: `meta`, `episodes`, `entries`,
`entry_reinstatement_decisions`, `entities`, `entity_aliases`, `relations`,
`edges`, `edge_evidence`, `edge_proposals`, `entity_proposals`, `entity_kinds`,
`dismissed_pairs`, `facts`, `world_facts`, `lessons`, `outcome_signals`,
`communities`, `entity_communities`, `memory_traces`,
`memory_trace_invalidations`, `entity_sources`, `merge_decisions`,
`chronicle_events`, `curation_judgments`, `store_decisions` (26).

Every exclusion is pending: `dream_runs`, `dream_run_slots`,
`retrieval_events`, `retrieval_uses`, `slot_reads`, `lesson_search_events`,
`client_sessions`, `coordination_agents`, `coordination_messages`,
`coordination_events`, `coordination_leases`, `coordination_lease_waiters`,
`coordination_wakes`, `principals` (14); `outcome_signals.used_ids`;
metadata `schema_version`, `active_session_pointer`, `dream_ack_secret_v1`,
`coordination_hlc_highwater`, `coordination_bank_id`, `writer_lease_epoch`
and every `*_schema_version` key. Target exported tables must be empty
except `meta` and builtin `relations`; excluded runtime tables never travel.

Import must delete the target's `curation_listing_spelling_v2` before importing
exported metadata so the export's value survives. Native closure of these
dependent rows is required or needs an explicit accepted disposition.

### Configuration inventory

| Section | State | Evidence needed |
|---|---|---|
| `EmbeddingConfig` | Pending | 9; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MIRASBandSpec` | Pending | 6; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MIRASConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `ReferenceConfig` | Pending | 5; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `NLIConfig` | Pending | 4; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `BM25Config` | Pending | 7; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `RerankerConfig` | Pending | 5; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `DreamConfig` | Pending | 41; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `DeepDreamConfig` | Pending | 49; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `CortexConfig` | Pending | 11; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `LessonsConfig` | Pending | 11; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `RetrievalLogConfig` | Pending | 3; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `CompactionConfig` | Pending | 3; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MetaFilterConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `GraphInsightConfig` | Pending | 8; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `TracesConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `ScopesConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `RecallConfig` | Pending | 12; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `SearchConfig` | Pending | 6; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `McpConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MemoryConfig` | Pending | 29; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `ContextConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `TimeConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `StorageConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MemoryPolicyConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `WakeConfig` | Pending | 6; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `CoordinationConfig` | Pending | 8; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `UpdatesConfig` | Pending | 4; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `AppConfig` | Pending | 8; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MaintainerConfig` | Pending | 3; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |

### Environment inventory

Each variable's reader/reference paths are the corresponding exact row in the
PARITY environment appendix; no values are copied into this checklist.

| Variable | State | Evidence needed |
|---|---|---|

| `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_COORDINATION` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_LABEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_PROJECT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_STATE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_STATE_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_TASK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_WAKE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_WAKE_HOOK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BACKUP_MIRROR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BACKUP_MIRROR_KEEP` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BANK_VOLUME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BENCH_DB` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BOARD_HARNESS_NAMES` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_DIRTY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_GIT_SHA` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_TIME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_LOG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_PROMPT_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_BIN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_DOORBELL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_HOOK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SERVER_TOKEN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SERVER_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_HEALTH_TTL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_LOG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DAEMON_EXEC` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DAEMON_MEM_LIMIT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DESKTOP_TOKENS_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DESKTOP_TOKEN_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DIGEST_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DOCKER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_BASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_EXTRACTOR_MODE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_FALLBACK_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_FALLBACK_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_MAX_TOKENS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_EMBEDDING_CPU_DTYPE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_EXTRACTOR_SLEEP_IDLE_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_HANDLE_RESUME_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_IMAGE_TAG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_INSTALLER_TOKEN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_INSTALLER_TOKENS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_JUDGE_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_JUDGE_SECOND_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_LEASES_HELD` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_LEASE_LOCK_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_CONFIG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_DATABASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_DATA_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_NO_SPAWN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_SHARED_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_STORAGE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TIER_MAP` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOKEN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOKENS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOKEN_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOOLSET` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TRUST_BIND` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MODELS_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_PLUGIN_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_PRIVATE_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_RECALL_DRIVER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_RELEASE_CHECK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_REQUIRE_TEST_POSTGRES` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SESSION_IDLE_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SESSION_REAP_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SESSION_RESUME_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_CLAUDE_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_CODEX_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_LAUNCHER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_RUNTIMES` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_USER_BIN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_STATE_VOLUME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_COMMIT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_DISPATCHED` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_ENV_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_GIT_COMMON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_KEEP` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_PRUNE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_PRUNE_DAYS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_LEASE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_LOCK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_LOCK_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_NAME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_REMOTE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_RUN_ID` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_SLOTS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_VENV` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_WHERE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_WINDOWS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_CUDA` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_DATABASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_EMBEDDER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_HOST_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_LOGIN_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_PASSWORD` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_USER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_WRITER_ID` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_WSL_DISTRO` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_WSL_SUITE_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `_PSEUDOLIFE_PRODUCTION_DB` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
