# CONFIG-LOAD

Source commit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`. Repository-relative line references below use that frozen commit. Producer attribution: dossier author's source census, pending `rust/producer-census.json`. Source preparation only; a pure config metadata/unknown-key/null-bool proof was executed for review corrections, with no service, model, database or build.

## 1. What the row promises

Shipped daemon, client and operator configuration must resolve the same effective settings from missing files, YAML, explicit arguments and each reader's environment, with the same default/null/validation distinctions. Python's generic YAML loader, service overlays and Console patch writer are different boundaries, so one blanket precedence/coercion rule cannot implement this row (`rust/PARITY.md:287`). This dossier enumerates every config dataclass and the audited central readers; the full peripheral environment-reader census remains incomplete and must be reconciled before closure.

## 2. Entry points and call graph

| Entry | Role and continuation |
|---|---|
| `pseudolife_memory/utils/config.py:1665` `load_config(path='config.yaml')` | Missing file -> new AppConfig; existing YAML -> section-specific constructors -> dataclass validation. Does not itself apply environment overrides. |
| `pseudolife_memory/utils/config.py:1648` `_dict_to_dataclass(cls,data)` | For mappings, filter to declared fields and pass their values to the constructor. These config classes have string `Field.type` values from future annotations, so the generic recursive branch never runs for them; manual section reconstruction/post-init supplies nesting. Nonmapping is returned unchanged. |
| `pseudolife_memory/service.py:796` constructor, `:578` `_user_yaml_leaves`, `:1072` `_apply_mcp_defaults` | Resolve data/config paths, load config, force save/reference dirs under data_dir, overlay only absent YAML leaves. |
| `pseudolife_memory/mcp_server.py:109` module-level service construction | Environment DATA_DIR/CONFIG -> MemoryService; environment database URL consumed by service (`pseudolife_memory/service.py:810`). |
| `pseudolife_memory/daemon.py:472` `run_daemon` | Storage-mode resolution before MCP import; CLI host/port or environment; token/map and bind policy at `:525`. |
| `pseudolife_memory/storage/embedded_pg.py:99` `resolve_daemon_storage` | Explicit DSN > files opt-out > auto optional embedded backend; writes resolved environment on embedded path. Named embedded-Postgres deferral. |
| `pseudolife_memory/memory/embedding.py:69` `_requested_cpu_dtype` | Trim/lower environment override first, config second; validate before backend/device resolution. EMBEDDING owns actual model behavior. |
| `pseudolife_memory/memory/dream.py:1984` `resolve_endpoints`, `:2351` `build_extractor`, fallback key at `:2208` | Reader-specific config/env ownership, endpoint/model/mode/numeric fallback and API keys; W3-H owns extraction runtime. |
| `pseudolife_memory/service.py:9068` `recall`, driver selection `:9085` | Explicit driver or environment default or config; public method starts at `:9068`. |
| `pseudolife_memory/credentials.py:378` `CredentialProvider.from_environment` | TOKEN_FILE key presence wins over TOKEN; configured empty file path refuses rather than falling back. Full credential lifecycle belongs to SHIM credential rows. |
| `pseudolife_memory/daemon_url.py:25` `_daemon_url`, `:30` `_validated_daemon_url` | DAEMON_URL environment or default origin; refuse credential/path/query/fragment/whitespace origin. |
| `pseudolife_memory/coordination_recovery.py:71` `_perform` | Requires an existing config path, loads it, and refuses enabled coordination before its recovery operations. CLI owner controls mutation scope. |
| `pseudolife_memory/invite_cli.py:271` `_listed_note` | Loads default-data-dir config for a courtesy allowed-principal note; catches loader failures. |
| `pseudolife_memory/web/routes.py:283` GET/POST `/api/config` | `read_config(svc)`; `write_config(svc,b.get('patch') or b)`. HTTP wrapper owns admission/status mapping. |
| `pseudolife_memory/web/config_io.py:759` `config_path_for`, `:780` `_saved_file`, `:792` `read_config` | Editable path environment override or data_dir; effective/saved restart values grouped by whitelist. |
| `pseudolife_memory/web/config_io.py:829` `write_config`, `:841` `_write_config_locked` | Validate all patch keys/values -> merge YAML -> unique backup -> tmp/os.replace -> publish live values. |
| `pseudolife_memory/web/config_io.py:690` `_get_by_path`, `:697` `_set_by_path`, `:705` `_nested_set`, `:719` `_coerce`, `:771` `_nested_get` | Separate Console path traversal/coercion; do not infer generic load_config behavior from these helpers. |

The call graph down to storage is configuration -> `MemoryService._ensure_postgres_storage` (`pseudolife_memory/service.py:1212`) -> `PostgresStorage` (`pseudolife_memory/storage/postgres.py:391`) -> `ensure_schema` (`pseudolife_memory/storage/schema.py:1145`), with model/preset/store construction in `pseudolife_memory/service.py:1469`. Loading YAML and writing `/api/config` contain no SQL themselves. Their downstream storage effects are mapped in [PG-HYDRATE.md](PG-HYDRATE.md) and the STORAGE-COMPAT row; a YAML patch is not authorization for schema/type migration.

Every dataclass below is declared in `pseudolife_memory/utils/config.py`; field lists were checked by static AST inspection at the source commit. Defaults/types are the field declarations at each anchor, not the possibly stale counts in `rust/PARITY.md:879`/contract inventory. The inventory is complete for this module; other env readers are explicitly not complete.

| Dataclass anchor | Declared fields |
|---|---|
| `pseudolife_memory/utils/config.py:16` EmbeddingConfig | model_name, device, batch_size, backend, onnx_file_name, cache_size, query_prefix, max_seq_length, cpu_dtype |
| `pseudolife_memory/utils/config.py:74` MIRASBandSpec | name, max_entries, update_interval, promotion_access_count, promotion_surprise, retention_policy |
| `pseudolife_memory/utils/config.py:90` MIRASConfig | preset, bands |
| `pseudolife_memory/utils/config.py:131` ReferenceConfig | persist_dir, collection_name, chunk_size, chunk_overlap, max_results |
| `pseudolife_memory/utils/config.py:141` NLIConfig | enabled, model_name, threshold, max_candidates |
| `pseudolife_memory/utils/config.py:155` BM25Config | enabled, k1, b, weight, top_n, min_score, cortex_enabled |
| `pseudolife_memory/utils/config.py:208` RerankerConfig | enabled, model_name, top_n, fusion_weight, skip_margin |
| `pseudolife_memory/utils/config.py:255` DreamConfig | enabled, eligible_sources, exclude_sources, digest_enabled, digest_context_chars, digest_target_chars, digest_max_per_cycle, min_batch, idle_seconds, max_batch, sweep_interval_seconds, stall_notice, stall_repeat_hours, quarantine_low_trust, trusted_sources, assistant_claims, extractor_base_url, extractor_api_key, extractor_model, extractor_source, extractor_model_override, extractor_reasoning_effort, extractor_max_tokens, extractor_cache_prompt, extractor_timeout_seconds, fallback_base_url, fallback_model, fallback_api_key, extractor_mode, extract_relations, min_relation_confidence, relation_quarantine_below, retype_quarantined_max, write_dedup_min_jaccard, alias_candidate_min_cosine, known_facts_window, literal_gate, literal_gate_scope, span_gate, runs_keep, chronicle |
| `pseudolife_memory/utils/config.py:513` DeepDreamConfig | min_similarity, top_k_candidates, max_context_snippets, auto_apply_safe, min_entity_mentions, max_fallback_mentions, merge_min_similarity, junk_max_degree, max_support_overlap, snippet_max_chars, snapshot_keep, curation_min_similarity, curation_top_k, auto_tick, auto_min_new_entities, auto_interval_days, review_queue_alert_pending, review_queue_alert_age_days, review_queue_alert_min_pending, judge_mode, judge_batch, judge_snippet_max_chars, judge_reject_min_confidence, judge_url, judge_model, judges_enabled, judge_second_opinion, judge_second_model, judge_second_url, judge_reject_min_confidence_2, judge_accept_min_confidence, link_judge_mode, link_accept_min_confidence, link_reject_min_confidence, junk_judge_mode, junk_keep_min_confidence, junk_delete_min_confidence, junk_max_auto_degree, curation_judge_mode, curation_distinct_min_confidence, curation_forget_min_confidence, curation_rejudge_days, candidate_judge_mode, candidate_min_confidence, candidate_rejudge_days, analyzer_file_duplicates, orphan_sweep, orphan_min_age_days, orphan_max_per_apply |
| `pseudolife_memory/utils/config.py:692` CortexConfig | enabled, auto_promote, promote_confidence, search_first, protect_provenance, supersede_confidence_margin, reinforce_rate, guard_min_score, dream_slot_match_threshold, read_tracking, pin_constraints |
| `pseudolife_memory/utils/config.py:747` LessonsConfig | enabled, top_k, min_confidence, signal_retention_days, signal_retry_days, synthesize_in_dream, infer_outcomes, infer_outcomes_max_signals, synthesis_dedup_min_similarity, synthesis_max_signals, rule_mode |
| `pseudolife_memory/utils/config.py:819` RetrievalLogConfig | enabled, retention_days, use_window_seconds |
| `pseudolife_memory/utils/config.py:860` CompactionConfig | enabled, keep_per_slot, min_age_days |
| `pseudolife_memory/utils/config.py:871` MetaFilterConfig | enabled |
| `pseudolife_memory/utils/config.py:882` GraphInsightConfig | enabled, algorithm, resolution, max_community_fraction, god_nodes_top_n, surprises_top_n, questions_top_n, betweenness_sample |
| `pseudolife_memory/utils/config.py:896` TracesConfig | enabled, retention_boost |
| `pseudolife_memory/utils/config.py:908` ScopesConfig | exclude, rollup; `scope_keys` method `:918` |
| `pseudolife_memory/utils/config.py:939` RecallConfig | driver, default_hops, default_top_k, max_entities, hub_gate, hub_percentile, hub_floor, expand_budget, max_searches_per_hop, max_total_searches, time_budget_seconds, skip_part_of_expansion |
| `pseudolife_memory/utils/config.py:1008` SearchConfig | contiguity_neighbors, timeline_channel, stale_policy, candidate_pool_multiplier, fusion, min_score |
| `pseudolife_memory/utils/config.py:1158` McpConfig | compact_payloads, entry_text_chars |
| `pseudolife_memory/utils/config.py:1206` MemoryConfig | embedding_dim, miras, reference, nli, bm25, reranker, search, dream, deep_dream, cortex, lessons, compaction, recall, mcp, graph_insight, traces, retrieval_log, scopes, meta_filter, recency_base_half_life_s, recency_boost_enabled, delete_confirm_threshold, surprise_threshold, top_k, ref_top_k, save_dir, hide_superseded, search_confidence_floor, slot_index_shadow_rate |
| `pseudolife_memory/utils/config.py:1304` ContextConfig | max_memory_tokens |
| `pseudolife_memory/utils/config.py:1309` TimeConfig | relative_age |
| `pseudolife_memory/utils/config.py:1317` StorageConfig | write_mode |
| `pseudolife_memory/utils/config.py:1339` MemoryPolicyConfig | variant, ab_arms |
| `pseudolife_memory/utils/config.py:1385` WakeConfig | per_recipient_per_hour, urgent_per_sender_per_hour, nightly_total, fan_out_stagger_seconds, active_seconds, authority_per_sender_per_hour |
| `pseudolife_memory/utils/config.py:1437` MaintainerConfig | rp_id, origin, maintainer_per_recipient_per_hour |
| `pseudolife_memory/utils/config.py:1505` CoordinationConfig | enabled, awareness_limit, allowed_principals, daemon_notice_principals, maintainer_principals, audit_retention_days, wake, maintainer |
| `pseudolife_memory/utils/config.py:1596` UpdatesConfig | check_releases, check_interval_seconds, unattended_clients, unattended_daemon |
| `pseudolife_memory/utils/config.py:1637` AppConfig | embedding, memory, context, storage, time, coordination, memory_policy, updates |

Construction hooks needing separate coverage: MIRAS `:116`, Dream `:503`, Search `:1134`, MemoryPolicy `:1361`, Wake `:1426`, Maintainer `:1460` (also `problem` `:1468`/`configured` `:1500`), Coordination `:1552`, Updates `:1628`. Ordinary dataclass annotations do not themselves enforce all declared types.

## 3. State touched and precedence

| Reader/state | Exact source behavior |
|---|---|
| `pseudolife_memory/utils/config.py:1665` path/YAML | `Path(path)`, missing -> fresh defaults, existing -> `yaml.safe_load(f) or {}` using ordinary `open(path)` encoding. Empty/null/falsy document becomes {}; malformed YAML propagates. No explicit root-mapping validation exists here. |
| `pseudolife_memory/utils/config.py:1648` unknown keys | Generic constructor filters known names; whole unknown top-level sections are ignored. Nonmapping section values are returned unchanged by helper and can fail later. All fields of these module's dataclasses have string runtime types from future annotations (`:3`); `_dict_to_dataclass` does not recursively construct their nested dictionaries. This was verified by a pure config proof for the review correction. |
| `pseudolife_memory/utils/config.py:1681` memory | Manual scalar `.get` defaults, followed by named nested constructors. Missing scalar values match MemoryConfig defaults, while explicit null is passed through. MIRAS bands are individually constructed; named preset replaces supplied bands; custom requires a nonempty list (`:116`). |
| `pseudolife_memory/utils/config.py:1701` nested memory | reference/nli/search/bm25/reranker/cortex/lessons/dream/recall/mcp/graph_insight/meta_filter/traces/compaction/retrieval_log/deep_dream/scopes handled explicitly. Exact sub-block anchors are in load_config; do not drop later blocks because an older inventory omits them. |
| `pseudolife_memory/utils/config.py:1552` coordination | Strict bool/int/list validation; normalize principal names strip/lower and deduplicate retaining order. wake/maintainer dicts are manually reconstructed; retired wake key `nudge_interval_seconds` is filtered, but unknown other keys in `coordination.wake` or `coordination.maintainer` raise TypeError at startup. Shared default/daemon principals cannot be maintainer principals. |
| `pseudolife_memory/utils/config.py:1134`, `:503`, `:1361`, `:1426`, `:1460`, `:1628` | Search fusion allowed modes weighted_sum/rrf; min_score excludes bool and must be numeric 0..1 then becomes float. Stall hours finite/positive/nonbool. Memory policy known variants and 0 or >=2 arms. Wake whole-number nonbool caps, active>=1. Maintainer fields/cap types then separate origin validity. Updates bools and interval integer>=60. Preserve validation timing. |
| `pseudolife_memory/service.py:844` path/overlay | Explicit data_dir or cwd/data (directory is created), explicit config_path or data_dir/config.yaml. Then memory.save_dir -> data_dir/memory_state and reference.persist_dir -> data_dir/chromadb even when YAML specified others. Only leaf-absent MCP defaults overlay. |
| `pseudolife_memory/service.py:1072` defaults | Absent leaves: surprise threshold 0, batch size16, meta_filter false, recency base86400 seconds, retention_boost1. Backend may auto-select locally available admitted ONNX only when not explicitly set (`:1121`). File-free dataclass defaults are distinct: batch64, recency3600, retention0. |
| `pseudolife_memory/service.py:1338` model dimension area | After model construction memory.embedding_dim is replaced with actual embedder dimension. Dataclass/manual YAML fallback is 384 (`pseudolife_memory/utils/config.py:1207`, `:1688`); default Qwen model is 1024-dimensional. Schema mismatch refusal is separately hardcoded at `pseudolife_memory/storage/schema.py:1069`, not a YAML-driven ALTER. |
| `pseudolife_memory/web/config_io.py:759` path | Nonempty PSEUDOLIFE_MCP_CONFIG wins over service.data_dir/config.yaml for Console edits. Constructor-supplied config_path is not retained by this helper; an explicit library-only path with no env override is a distinct boundary. |
| `pseudolife_memory/web/config_io.py:841` patch | All known/protected/value validation before persistence, unknown keys refused; preserve unmanaged YAML keys, while safe_dump rewrites comments/formatting (`:888`). One process lock `:34` covers file merge through runtime publication. Unique timestamped backup, unique same-dir temp and os.replace; live knobs publish after replacement, restart knobs only persist. No SQL/transaction appears here. |
| `pseudolife_memory/web/config_io.py:719` coercion | Bool strings trim/lower and recognized yes tokens; other strings false; null for a bool becomes False. int()/float() coercion with per-knob bounds and finite floats; enum membership; strings strip and empty/null clear; URL fields require http(s) prefix. Do not impose these conversions on raw YAML dataclasses. |
| `pseudolife_memory/web/config_io.py:792` read | Effective knob value plus metadata; restart knobs also saved value only if valid/different. Missing/unreadable saved file doesn't prevent effective read. Maintainer paths are protected against Console patching (`:846`). |

Audited environment readers (names only; no credential values):

| Variables | Reader and precedence |
|---|---|
| `PSEUDOLIFE_MCP_DATA_DIR`, `PSEUDOLIFE_MCP_CONFIG` | `pseudolife_memory/mcp_server.py:109` passes to constructor; Console config helper `pseudolife_memory/web/config_io.py:759` also reads CONFIG. |
| `PSEUDOLIFE_MCP_DATABASE_URL`, `PSEUDOLIFE_MCP_STORAGE` | `pseudolife_memory/service.py:810` explicit truthy database_url wins; `pseudolife_memory/storage/embedded_pg.py:99` explicit nonempty DSN bypasses mode; empty mode defaults auto, files opts out; any other mode raises RuntimeError, and auto without the optional extra falls back to files (`:111–118`). |
| `PSEUDOLIFE_MCP_HOST`, `PSEUDOLIFE_MCP_PORT`, `PSEUDOLIFE_MCP_TOKEN`, `PSEUDOLIFE_MCP_TOKENS`, `PSEUDOLIFE_MCP_TRUST_BIND` | `pseudolife_memory/daemon.py:525`: truthy explicit host/port wins; port int-cast, token/map auth/bind policy read independently. Detailed map/bind validation belongs to HTTP-SECURITY. |
| `PSEUDOLIFE_WRITER_ID`, `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` | `pseudolife_memory/service.py:893` writer default unknown; `:1005` numeric active-session TTL default21600, <=0 disables expiry. Other request attribution owns its own precedence. |
| `PSEUDOLIFE_EMBEDDING_CPU_DTYPE` | `pseudolife_memory/memory/embedding.py:69`: nonempty stripped/lowered env wins config; invalid precision refuses, not silently defaults. |
| `PSEUDOLIFE_DREAM_BASE_URL`, `PSEUDOLIFE_DREAM_MODEL`, `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL`, `PSEUDOLIFE_DREAM_FALLBACK_MODEL`, `PSEUDOLIFE_DREAM_EXTRACTOR_MODE`, `PSEUDOLIFE_DREAM_MAX_TOKENS`, `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` | `pseudolife_memory/memory/dream.py:1984`: extractor_source=config ignores these overrides; env mode chooses truthy env or config, numeric conversion errors revert config, invalid mode becomes auto. Primary extractor_model_override wins either ownership mode. |
| `PSEUDOLIFE_DREAM_API_KEY`, `PSEUDOLIFE_DREAM_FALLBACK_API_KEY` | `pseudolife_memory/memory/dream.py:2373`, `:2208`: each truthy env key wins its own config key; primary key is honored in either ownership mode, fallback doesn't inherit primary. |
| `PSEUDOLIFE_RECALL_DRIVER` | `pseudolife_memory/service.py:9085`: truthy explicit driver wins, otherwise env get(default=config driver); present empty env and missing env are distinct. |
| `PSEUDOLIFE_MCP_TOKEN_FILE`, `PSEUDOLIFE_MCP_TOKEN` | `pseudolife_memory/credentials.py:378`: file key presence, even empty, wins static token; separate credential validation. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `pseudolife_memory/daemon_url.py:25`: get(default=http://127.0.0.1:8765), then origin validation; present empty value refuses. |

The complete tracked-name/reference inventory is `rust/PARITY.md:917` environment appendix and `rust/contract-inventory.json`; references/propagation/comments there are a superset of readers, not proof of precedence. Per-variable function/line/default audits for remaining shim/autostart/install/update/tunnel/test/bench readers, judge endpoints/API keys and dynamic prefix families are incomplete here. No claim that all environment variables have been ported or exhaustively traced follows from this table.

## 4. Shipped producers

Own census until prep-census lands; reviewed central shapes:

| Producer | Exact shape |
|---|---|
| Daemon MCP service `pseudolife_memory/mcp_server.py:109` | `MemoryService(data_dir=<DATA_DIR or None>,config_path=<CONFIG or None>)`; configured YAML is a mapping of named sections and nested leaves. Missing file is a normal shipped install case. |
| Console Settings `frontend/src/views/Settings.svelte:124`, API `frontend/src/lib/api/config.ts:76` | POST `/api/config` JSON `{patch:{'<dotted known path>':<bool|number|string|null>,...}}`; GET same route for effective/saved values. Examples: `{patch:{'memory.top_k':8}}`, `{patch:{'memory.dream.extractor_model_override':null}}`. Arbitrary browser edits remain schema-governed inputs, not a closed value corpus. |
| Dreamer picker `frontend/src/components/settings/DreamerCard.svelte:59` | `pickPatch(kind,value,status)` -> API write of one/few known dotted keys. Detailed pickPatch branch census is incomplete. |
| Docker compose `ops/docker-compose.yml:145`, `:189`, `:197`, `:204`; packaged copy same anchors | DATA_DIR `/data`; CPU precision interpolation default empty; extractor URL/model/mode/token-limit/timeout env controls. Complete compose substitutions listed/pinned by `tests/test_ops_env_example.py`, not inferred from this subset. |
| User guide `docs/guide/configuration.md:22`, `:38`, `:2695` | CONFIG override path or `/data/config.yaml` on daemon; nested memory overrides added under existing memory mapping. Docs examples are supplemental YAML producers; full exact example inventory incomplete. |
| Recovery/invite `pseudolife_memory/coordination_recovery.py:71`, `pseudolife_memory/invite_cli.py:271` | Load named existing recovery config (coordination disabled) or default data-dir config for informational note. No parser corner or live recovery mutation is authorized by this dossier. |
| Client registration `ops/install.ps1:2571`, `:2689`, `:2824`; `pseudolife_memory/client_config.py:95`, `:121` | Shipped registration env carries writer ID/no-spawn, daemon URL, token file and sometimes agent state directory. JSON/TOML registrar semantics and installer changes are existing owners' scope; full client/env corpus incomplete here. |

## 5. Python incidentals to defer

Port the parameter shapes shipped producers actually send, and defer unreached Python mechanics by name: pathlib/argparse corners no real producer uses, Python object identity, mutable dataclass internals, arbitrary unsafe YAML tags/object construction, and CPython numeric grammar outside admitted shapes. Preserve omitted/null/false/empty distinctions, section-specific unknown filtering, range/type/finite validation and the actual reader's ownership; those affect shipped settings.

Python `yaml.safe_load` accepts forms not necessarily in the native typed YAML contract. Existing sent config substitution is separate (`rust/PARITY.md:212` `config-yaml-typed`); W3-J's folded/tab repair/image/checklist does not make this broader Python reader contract its work. Any proposed strict YAML tag/duplicate/ambiguity refusal must be named and tied to shipped producer evidence rather than silently relabeled Python parity. Embedded pg0 and Chroma configuration records remain readable source contracts with named runtime deferrals.

## 6. Oracle tests

Behavioral:

- `tests/test_memory_config.py::test_yaml_overrides`, `::test_yaml_memory_block_omitted_keys_keep_dataclass_defaults`, `::test_yaml_memory_scalar_key_present_is_read`, `::test_default_preset_is_the_flat_band`, `::test_continuum_preset_retained_yields_eight_cosine_bands`.
- `tests/test_mcp_defaults_overlay.py::test_user_yaml_survives_mcp_defaults`, `::test_mcp_defaults_apply_when_no_config_file`, `::test_user_backend_choice_survives_mcp_defaults`, `::test_default_model_without_onnx_artifact_stays_torch`.
- `tests/test_dream.py::test_build_extractor_selects_by_config`, `::test_build_extractor_config_source_ignores_env`.
- `tests/test_embedding_cpu_dtype.py::test_cpu_dtype_round_trips_through_config_yaml`, `::test_the_env_override_beats_the_config_value`, `::test_an_unknown_precision_is_a_config_error`.
- `tests/test_wake_defaults.py::test_wake_caps_are_read_from_config_yaml`, `::test_a_zero_cap_rings_nobody_and_the_windows_need_a_second`, `::test_the_retired_nudge_interval_still_loads_and_does_nothing`.
- `tests/test_memory_policy_variant.py::test_yaml_selects_variant_and_arms`, `::test_invalid_policy_config_is_refused`.
- `tests/test_web.py::test_write_config_roundtrip_live`, `::test_write_config_restart_classification`, `::test_write_config_preserves_unmanaged_keys`, `::test_write_config_rejects_bad_input`, `::test_write_config_bool_coercion`.
- `tests/test_web_hardening.py::test_config_rejects_nonfinite_without_persisting`, `::test_config_reports_the_saved_value_of_a_restart_knob`, `::test_config_concurrent_patches_preserve_both_updates`, `::test_config_serializes_runtime_publication`, `::test_config_io_failure_keeps_disk_runtime_and_cleans_temp`.
- `tests/test_maintainer_web.py::test_the_config_route_cannot_set_the_maintainer_keys`: protected file-only configuration boundary.

Implementation/inventory: `tests/test_ops_env_example.py::test_every_compose_variable_appears_in_the_env_template` is reference coverage, not effective precedence. `tests/test_client_environment.py::test_client_environment_removes_inherited_credentials_and_uses_disposable_home` protects the fixture, not a deployed loader. Default-literal tests are useful data pins but cannot establish per-reader construction/runtime behavior. Missing-file/unknown-section/nonmapping/duplicate-YAML and every field's direct coverage inventory is incomplete; add a case for a documented silent oracle gap before porting it.

## 7. Proposed harness and mutants

Extend shared daemon scenarios (`rust/daemon/harness/run.py:1`) and its config golden; do not replace Python oracle tests or use a parallel comparator. Pure loader cells can emit declared effective field values from disposable YAML/environment inputs; runtime cells must also assert health/tool behavior and files where the setting acts. Never include secrets in committed captures; credential tests use synthetic fixture values and emit admission/equality evidence.

1. Missing/empty YAML, every top-level section and every field from the dataclass table: compare defaults/explicit value/effective service overlay. Include a memory block omitting all scalar leaves and an explicit YAML leaf equal to its default, ensuring explicitness survives overlay.
2. Flat/named/custom MIRAS, band dictionaries, nonempty custom admission, retired wake keys and unknown top-level/section keys, strict bool-vs-int and invalid search floor/fusion/stall/update/policy values. Explicitly pin string `Field.type` metadata and absent generic recursion, and require TypeError at startup for unknown `coordination.wake` and `coordination.maintainer` keys. Each case records constructor or later-reader failure timing.
3. Matrix for config-vs-env dream ownership, absent/empty/invalid numeric env, model-only override, separate key selection; CPU precision and recall explicit/env/config priority. Capture fake extractor request settings, not a real network/model run.
4. Console GET/POST `{patch}` and direct dictionary accepted wrapper: live versus restart knobs, unmanaged key preservation, null string clearing versus null bool -> False, comment/format rewrite and protected maintainer paths; compare effective values, persisted YAML semantics, backup and temp files, then restart effective values. Invalid patch changes neither disk nor runtime.
5. Two concurrent nonoverlapping Console patches and an injected backup/write/replace error; observe both changes, publication order and cleanup. On Windows include Unicode paths and credential-file presence-vs-empty refusal with synthetic owner-only files.

Six mutants:

| Mutant | Rejecting observation |
|---|---|
| Treat present-null or explicit-default leaf as absent | Case 1 overlay changes user input. |
| Use an old scalar fallback (e.g. different top_k/preset) | Case 1 empty memory block differs from missing file. |
| Apply global environment precedence in config-owned dream mode | Case 3 fake extractor receives wrong endpoint/model/limits. |
| Remove unknown-key filtering or retired-key rule | Case 2 shipped old configuration refuses incorrectly. |
| Publish restart knob immediately or save live knob without publishing | Case 4 effective/saved/read-after-restart values differ. |
| Remove patch lock / publish before replacement | Case 5 loses a concurrent edit or reports runtime settings that failed to persist. |

Cases and mutants are proposals, unimplemented and unrun.

## 8. Dependencies and risks

CONFIG-LOAD spans daemon, shim and ops boundaries; agree the audited reader split with those owners and reconcile producer-census before assigning implementation. W3-J owns sent YAML repair/image/checklist; this dossier does not widen that work. HTTP-WRITES owns wrapper errors/gates, SHIM credential/config owns client files, W3-H owns dream endpoint use, and EMBEDDING owns backend/dtype behavior.

Two default layers, forced service paths, deferred annotation metadata, manual nested handling and live/restart publication create silent drift risks. Safe-load platform encoding differs from explicit UTF-8 saved-file readers; malformed-root behavior needs a named disposition if not canonical. Repeated read-side config parsing and external editors are not serialized by the Console's process-only lock. Full external env/CLI/installer/autostart reader census, exact guide/picker shapes and field-by-field oracle coverage remain explicit gaps; this is a preparation map, not row closure or approval for stricter parsing.

## 9. Effort estimate

Large for complete CONFIG-LOAD: the central typed field loader is bounded, but distinct service overlays, live patching and many independently owned environment readers need separate producer and behavioral proof.
