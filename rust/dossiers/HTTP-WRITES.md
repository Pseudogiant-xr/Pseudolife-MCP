# HTTP-WRITES

Source: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`. Every source location refers to that commit. This dossier inventories the complete ConsoleRoutes POST registry and config persistence implementation; the transitive storage graph for delegated service mutations is **incomplete** and belongs to the named owner slices. No tests or harness cells were run.

## 1. What the row promises

Console POST requests must preserve the Python route's argument/default/coercion choices, identity binding, status/body mapping and resulting service mutation. Config edits additionally validate a whitelist, preserve unmanaged YAML keys, back up the prior file, atomically replace it and publish only live settings (`pseudolife_memory/web/routes.py:109`, `pseudolife_memory/web/api.py:743`, `pseudolife_memory/web/config_io.py:841`). This row supplies HTTP adapters, not ownership of the W2 write/board/MCP or W3 graph/dream implementation (`rust/PARITY.md:283`).

## 2. Entry points and call graph

`pseudolife_memory/web/api.py:231 build_console_app` → nested `app` (`:404`) → `/api` branch (`:707`) → `_browser_gate` (`:304`) → `_resolve_detailed` (`:256`) → POST body/content-type parsing (`:743`) → executor `dispatch` (`:790`) → `ConsoleRoutes.dispatch` (`pseudolife_memory/web/routes.py:87`). Request headers/principal are bound in the worker and restored in `finally` (`pseudolife_memory/web/api.py:806`). Shared helpers: `_hdr` (`pseudolife_memory/web/api.py:286`), `_host_part` (`:292`), `_parse_query` (`:193`), `_read_body` (`:127`), `_body_limit` (`:91`), `_send_json` (`:102`), `_principals_unavailable` (`:283`), `_is_maintainer_path` (`:75`), `_maintainer_error` (`:79`) and `_send_coordination_error` (`:170`). `_wait_while_connected` (`:145`, nested `disconnected` `:147`) handles the separate owned coordination receive branch (`:774`), not the ConsoleRoutes registry below.

`ConsoleRoutes.__init__` (`pseudolife_memory/web/routes.py:80`) builds `table`; `_register` (`:109`, local GET/POST registration lambdas `:110`) installs every route below; `has` (`:93`) is used for 404/405 mapping. POST-specific local helpers are `_decided_by` (`:71`), `_tribool` (`:55`), `_dream_run` (`:380`), `_delete` (`:385`) and `_daemon_notice` (`:289`). Other `_s/_i/_i_opt/_f/_list` helpers serve GETs and are outside this POST implementation.

All locations prefixed `pseudolife_memory/web/`, `service.py`, `service_dream.py`, `maintainer.py` and `pseudolife_memory/storage/` below are relative to `pseudolife_memory/`.

| POST path | Registration | Arguments and next Python function |
|---|---|---|
| `/api/facts/resolve` | `pseudolife_memory/web/routes.py:124` | Required `entity,attribute`; `bool(accept)` → `pseudolife_memory/service.py:4447 cortex_resolve`. |
| `/api/facts/set` | `:130` | Required `entity,attribute,value`; `float(confidence default 0.8)`; `origin or agent` → support; `freshness_class,authority,distortion_tolerance` each `or auto` → `pseudolife_memory/service.py:3912 cortex_write`. |
| `/api/facts/forget` | `:137` | Required entity, optional attribute → `pseudolife_memory/service.py:6126 cortex_forget`. |
| `/api/episode/start` | `:161` | Optional session_key, `title or session`, hint → `pseudolife_memory/service.py:6365 episode_start_session`. |
| `/api/episode/end` | `:164` | Optional session_key; `bool(run_dream default True)` → `pseudolife_memory/service.py:6428 episode_end_session`. |
| `/api/episodes/prune` | `:166` | `bool(include_open default False)` → `pseudolife_memory/service.py:7177 episode_prune_empty`. |
| `/api/episodes/rename` | `:168` | Required id, `title or empty string` → `pseudolife_memory/service.py:7037 episode_rename`. |
| `/api/episodes/merge` | `:170` | `sources or []`, optional into/title/hint → `pseudolife_memory/service.py:7054 episode_merge`. |
| `/api/reinforce` | `:190` | `int(entry_id)` → `pseudolife_memory/service.py:6109 reinforce`. |
| `/api/graph/rejudge` | `:213` | `queue default all`, `limit default 32` (not coerced here) → `pseudolife_memory/service_dream.py:3834 review_rejudge`. |
| `/api/graph/assign-scope` | `:218` | Required entity/source → `pseudolife_memory/service.py:7680 graph_assign_scope`. |
| `/api/graph/unrelate` | `:219` | Required src/relation/dst → `pseudolife_memory/service.py:7694 graph_unrelate`. |
| `/api/graph/relate` | `:222` | Required src/relation/dst → `pseudolife_memory/service.py:7624 graph_relate`. |
| `/api/graph/bless-edge` | `:223` | Required src/relation/dst → `pseudolife_memory/service.py:7717 graph_bless_edge`. |
| `/api/graph/dismiss-duplicate` | `:224` | Required a/b → `pseudolife_memory/service.py:7862 graph_dismiss_duplicate`. |
| `/api/curation/dismiss-duplicate` | `:226` | Required store/a_entity/a_attribute/b_entity/b_attribute → `pseudolife_memory/service.py:7909 curation_dismiss_duplicate`. |
| `/api/lessons/restore` | `:231` | Required task, optional aspect, decided_by human/agent else human → `pseudolife_memory/service.py:5441 lesson_restore`. |
| `/api/world/restore` | `:233` | Required entity, optional attribute, decided_by as above → `pseudolife_memory/service.py:4867 world_restore`. |
| `/api/graph/delete-entity` | `:235` | Required entity → `pseudolife_memory/service.py:7745 graph_delete_entity`. |
| `/api/graph/merge` | `:236` | Required from/into → `pseudolife_memory/service.py:7759 graph_merge`. |
| `/api/graph/accept-proposal` | `:237` | Required id → `pseudolife_memory/service.py:8821 graph_accept_proposal`. |
| `/api/graph/reject-proposal` | `:238` | Required id → `pseudolife_memory/service.py:8889 graph_reject_proposal`. |
| `/api/graph/accept-entity-merge` | `:239` | Required id, decided_by as above → `pseudolife_memory/service.py:8925 graph_accept_entity_merge`. |
| `/api/graph/accept-entity-junk` | `:242` | Required id, decided_by as above → `pseudolife_memory/service.py:8964 graph_accept_entity_junk`. |
| `/api/graph/reject-entity-proposal` | `:244` | Required id, decided_by as above → `pseudolife_memory/service.py:8996 graph_reject_entity_proposal`. |
| `/api/dream/run` | `:250` | `_dream_run`: absent/null/empty limit → None, otherwise int → `pseudolife_memory/service_dream.py:2645 dream_run_auto`. |
| `/api/consolidate` | `:255` | Optional replaces/entry_ids/source/tags; required new_text → `pseudolife_memory/service.py:7427 consolidate`. Selected IDs are forwarded unchanged. |
| `/api/delete` | `:260` | `_delete`: at least one truthy text/substring/source/episode/tag; pass all five filters, `_tribool(confirm_bulk) is True` → `pseudolife_memory/service.py:3321 delete`. |
| `/api/supersede` | `:261` | Optional old_text/entry_id, required new_text → `pseudolife_memory/service.py:3197 supersede`. Selected ID is forwarded unchanged. |
| `/api/daemon-notice` | `:265` | `_daemon_notice` enforces listed, non-reserved request principal, strips/validates text, appends provenance → `pseudolife_memory/coordination.py:677 daemon_notice`; complete board storage graph delegated. |
| `/api/maintainer/challenge` | `:272` | Entire body → `pseudolife_memory/maintainer.py:454 maintainer_challenge`. |
| `/api/maintainer/enrol` | `:273` | Entire body → `pseudolife_memory/maintainer.py:457 maintainer_enrol`. |
| `/api/maintainer/send` | `:274` | Entire body → `pseudolife_memory/maintainer.py:460 maintainer_send`. |
| `/api/maintainer/role` | `:275` | Entire body → `pseudolife_memory/maintainer.py:464 maintainer_role`. |
| `/api/maintainer/cancel` | `:276` | Entire body → `pseudolife_memory/maintainer.py:467 maintainer_cancel`. |
| `/api/maintainer/revoke` | `:277` | Entire body → `pseudolife_memory/maintainer.py:470 maintainer_revoke`. |
| `/api/maintainer/repudiate` | `:278` | Entire body → `pseudolife_memory/maintainer.py:473 maintainer_repudiate`. |
| `/api/config` | `:284` | `body.patch or body` → `pseudolife_memory/web/config_io.py:829 write_config` → `:841 _write_config_locked`. |

This is the static registry census. Dynamic `/api/coordination/<action>` dispatch (`pseudolife_memory/web/api.py:779`), `/api/pair` (`:702`) and hook POSTs (`:558`, `:651`, `:676`) use separate handlers/owners; see [HOOKS.md](HOOKS.md). The service entries above are verified dependency boundaries, **not a complete call graph down to storage** for those owned implementations.

Config's complete helper graph: `write_config` takes `_CONFIG_WRITE_LOCK` (`pseudolife_memory/web/config_io.py:35`, `:837`) → `_write_config_locked` validates `_KNOB_BY_PATH` (`:685`), calls `_coerce` (`:719`), `config_path_for` (`:759`), `_nested_set` (`:705`) and `_set_by_path` (`:697`). Readback `read_config` (`:792`) → `_saved_file` (`:780`), `_nested_get` (`:771`), `_get_by_path` (`:690`) and `_coerce` for restart saved values (`:817`).

The HTTP wrapper maps: foreign tokenless Host/Origin to 403, absent principal to 401, unavailable principals to 503, methods outside GET/POST to 405, stored/invited principals on config/daemon-notice to 403; body over cap to 413, nonempty non-JSON content type to 415, malformed JSON to 400, non-object JSON to 400 (`pseudolife_memory/web/api.py:707`, `:730`, `:743`). Dispatch succeeds at HTTP 200 even when its returned JSON contains a service refusal. For ordinary routes, KeyError becomes 405 for a registered path or 404 otherwise, ValueError becomes 400, other Exception becomes 500 (`:814`, `:829`, `:836`). Consequently missing required body keys may map to 405 rather than 400; do not silently repair this adapter during a port. Maintainer errors have separate sanitized mapping (`:79`, `:818`, `:830`); invalid UTF-8 escapes ordinary-route parsing (`:762`).

## 3. State touched

| State | Verified effects and SQL/file locations |
|---|---|
| Route table and identity context | In-memory `(method,path) → callable` dictionary (`pseudolife_memory/web/routes.py:82`); worker request context bound/restored per dispatch (`pseudolife_memory/web/api.py:806`). No wrapper SQL transaction. |
| Config whitelist | `KNOBS` metadata (`pseudolife_memory/web/config_io.py:52`) plus `_KNOB_BY_PATH` (`:685`) drive types/ranges/enums/live/restart. Protected `coordination.maintainer` keys reject before writes (`:845`). Exact per-knob values should be taken from this table; this dossier does not duplicate a second whitelist. |
| Config file | `PSEUDOLIFE_MCP_CONFIG` else `<data_dir>/config.yaml` (`pseudolife_memory/web/config_io.py:759`). Read existing YAML, preserve unknown keys and merge dotted patch (`:859`); unique timestamp-prefixed `.bak` via mkstemp + copy2 (`:870`); unique same-directory `.tmp` + safe_dump + os.replace (`:882`); cleanup in finally (`:890`). No database table or sequence is touched directly. |
| Runtime config | All keys validated/coerced before touching disk (`pseudolife_memory/web/config_io.py:853`). After replacement, non-restart attributes are published into `service.config`; restart keys remain persisted only, and missing runtime attributes become restart_required (`:893`). Returned applied/restart_required preserve patch iteration order (`:908`). Lock covers read/merge/backup/replace/runtime publication (`:837`). |
| Config readback | Running values plus differing saved restart values (`pseudolife_memory/web/config_io.py:805`, `:813`); broken/absent saved YAML becomes empty view (`:780`). Readback adds no writes. |
| Episode dependency example | Episode service writes go through `_persist_episodes` (`pseudolife_memory/service.py:6735`) to `upsert_episode`, SQL `pseudolife_memory/storage/postgres.py:1652`; pruning deletes via `:1679`; registration uses `client_sessions` upsert SQL `:1717`, close `:1763`; merging retargets entry episode columns via `:1691`. See HOOKS for columns. These leaves are independently transacted, not one HTTP transaction. |

**Incomplete storage inventory:** exact tables, columns, sequences, transaction boundaries and file recovery state for facts, graph/review verdicts, corrections/consolidation, restore, dream, maintainer and daemon-notice are not asserted here. The table in section 2 locates every service boundary for the owner to supply its tested storage implementation. A port cannot claim HTTP-WRITES parity from route responses without those post-state observations.

## 4. Shipped producers

Dossier-local census, pending prep-census. Console `post` sends `JSON.stringify(body)`, `Accept: application/json`, JSON Content-Type when body exists, optional bearer, same-origin credentials and no-store (`frontend/src/lib/api/client.ts:63`, `:105`). Thus booleans/numbers/string fields below are typed JSON, not stringified MCP tool arguments.

| Producer | Exact body shape and source |
|---|---|
| Facts Console | Resolve `{entity,attribute,accept: boolean}`; set `{entity,attribute,value,origin: "user"\|"action"\|"agent",confidence: number}`; forget `{entity,attribute}` with empty attribute refused locally (`frontend/src/lib/api/facts.ts:95`, `:141`). |
| Settings Console | `/api/config` `{patch: {"dotted.path": value, ...}}` (`frontend/src/lib/api/config.ts:76`). Metadata from GET config selects the valid paths/types (`pseudolife_memory/web/config_io.py:803`). |
| Overview dream button | `/api/dream/run` `{}` (`frontend/src/lib/api/client.ts:132`). Review rejudge `/api/graph/rejudge` `{limit:32}` (`frontend/src/lib/api/review.ts:201`). |
| Stream correction | Supersede `{entry_id: number,new_text: string}` when ID present, else `{old_text: string,new_text: string}` (`frontend/src/lib/stream.ts:145`). Delete `{text}` and confirmed retry `{text,confirm_bulk:true}` (`:224`, `:230`); reinforce `{entry_id}` (`:245`). |
| Consolidation | `{entry_ids:[...],new_text}` when every member has an ID, otherwise `{replaces:[exact-texts...],new_text}`; mixed IDs are refused locally (`frontend/src/lib/consolidation.ts:17`, `:30`). |
| Review actions | Merge `{from,into}` (`frontend/src/lib/reviewActions.ts:97`); entity/link verdicts `{id}` (`:110`, `:122`, `:128`, `:134`, `:140`); relate/unrelate/bless `{src,relation,dst}` (`:149`, `:194`, `:207`); dismiss pair `{a,b}` (`:150`, `:165`); delete entity `{entity}` (`:221`); assign scope `{entity,source}` (`:230`); curation distinct `{store,a_entity,a_attribute,b_entity,b_attribute}` (`:179`). |
| Episode CLI hooks | Start `{session_key,title}` derived from hook stdin session_id/cwd; end `{session_key}` (`pseudolife_memory/episode_cli.py:65`, `:76`). CLI implementation is owned separately. |
| Unattended updater | `/api/daemon-notice` `{text}` (`pseudolife_memory/unattended_update.py:124`). Wrapper adds its own principal provenance line (`pseudolife_memory/web/routes.py:321`). |
| Maintainer Console | Challenge union: `{purpose:"send",to,text,urgent}`; grant/assign `{purpose,project,agent_id,hold}`; role revoke `{purpose,project}`; cancel/revoke-self `{purpose,credential_id}`; repudiate `{purpose,message_id}`; enrol `{purpose,label}` (`frontend/src/lib/api/maintainer.ts:91`). Signed action body `{payload,mac,assertion}` (`:139`, `:225`); enrol `{payload,mac,attestation,code?}` (`:221`). Nested WebAuthn assertion/attestation byte serialization is **not expanded here**; see `frontend/src/lib/webauthn.ts:126` for the attestation producer boundary. |

No executable producer was established here for episodes prune/rename/merge or lesson/world restore; documentation references exist (`docs/guide/episodes.md:117`, `:128`, `docs/guide/memory-model.md:539`, `:666`). They remain routes in the registry, not presumed unreachable. Full docs example bodies and shim lifecycle producers are **incomplete** in this local census.

## 5. Python incidentals to defer

Canonical shapes are chosen by actual producers (`rust/SEMANTICS-CHECKLIST.md:10`). Defer arbitrary truthy containers to bool/int coercions and Python numeric-string grammar that typed Console JSON does not emit; keep explicit canonical false/zero/empty/null/absent cases (`pseudolife_memory/web/routes.py:125`, `:132`, `:165`, `:383`). Do not cast correction IDs as convenience: the wrapper forwards them unchanged (`pseudolife_memory/web/routes.py:255`, `:261`; `tests/test_correction_interfaces.py:18`).

Malformed JSON/content type/object shape and missing-key status are observable wrapper behavior, even if normal Console requests are valid (`pseudolife_memory/web/api.py:743`, `:814`). Ordinary invalid UTF-8 is not caught here; any candidate refusal needs a named disposition rather than an invented Python 400 (`:762`). Do not import hook strict-JSON or lease parsed-object comparison rules into `_send_json`, which uses `json.dumps(default=str)` (`:102`, `rust/SEMANTICS-CHECKLIST.md:11`).

## 6. Oracle tests

Behavioral tests, inventory verified but **not run**:

- `tests/test_web.py::test_post_verdict_route_dispatches_and_returns_the_service_result` (`:290`), `::test_delete_route_passes_every_filter_and_confirm_bulk_through` (`:300`), `::test_facts_resolve_forwards_both_directions` (`:922`), `::test_api_post_with_body_requires_json_content_type` (`:912`).
- `tests/test_web.py::test_write_config_roundtrip_live` (`:44`), `::test_write_config_restart_classification` (`:89`), `::test_write_config_makes_backup_on_second_write` (`:107`), `::test_write_config_preserves_unmanaged_keys` (`:113`), `::test_write_config_rejects_bad_input` (`:130`), `::test_write_config_extractor_panel_roundtrip` (`:140`).
- `tests/test_web_hardening.py::test_config_rejects_nonfinite_without_persisting` (`:185`), `::test_config_reports_the_saved_value_of_a_restart_knob` (`:201`), `::test_config_concurrent_patches_preserve_both_updates` (`:230`), `::test_config_serializes_runtime_publication` (`:268`), `::test_config_backups_and_temporary_files_are_unique` (`:301`), `::test_config_io_failure_keeps_disk_runtime_and_cleans_temp` (`:317`).
- `tests/test_correction_interfaces.py::test_correction_dispatch_preserves_selected_ids` (`:18`). MCP coercion tests in that file belong to the separately owned MCP surface.

Implementation/source guards: `tests/test_console_source_guards.py::test_label_element_sets_text_content_only` (`:66`) guards frontend source shape, not route/storage parity. Registry coverage above does not establish a behavioral oracle for every service route; a full route-to-service-to-storage oracle inventory is **incomplete**.

## 7. Proposed harness cases and mutants

Proposed, **unrun** adapter cells: one canonical request per registry entry with a recording service verifies exact positional/keyword arguments and unchanged returned value; then use real owned storage implementations for supported producer cases and compare post-state after every write. Include unknown path, wrong verb, missing required key, ValueError and unexpected Exception to pin 404/405/400/500; separately pin sanitized maintainer/coordination errors. Exercise bodyless/nonempty JSON, content-type parameters, malformed/non-object body and boundary byte limits through the actual ASGI reader. Bind two identities on executor calls and force one handler failure to verify context restoration.

Config cells use a disposable directory: first write, second write with unmanaged keys, multi-key invalid patch with no disk/runtime mutation, live+restart patch/readback, two concurrent disjoint patches, and fault injection at backup copy/temp dump/replace. Compare old/new YAML semantically, backup bytes exactly, temp cleanup, runtime values and applied/restart order.

Six mutants:

1. Omit `authority`/`distortion_tolerance` forwarding from facts/set: reject recording-service arguments.
2. Coerce selected correction IDs with int: reject forwarded noncanonical selector control without changing the underlying service contract.
3. Replace config existing YAML instead of merging: reject retained unmanaged key.
4. Live-publish restart knob: reject runtime/readback pair.
5. Remove `_CONFIG_WRITE_LOCK`: reject scheduled concurrent read/merge/publication interleave.
6. Map every KeyError to 400: reject current missing-key/wrong-verb oracle statuses.

## 8. Dependencies and risks

W2 write/identity/board/MCP and W3 graph/dream owners supply the delegated mutation contracts. HTTP-SECURITY and HTTP-BODY-LIMITS supply gate/parser limits; HOOKS handles its independent POST policy. The wrapper must retain distinct maintainer/coordination diagnostics and invited-principal restriction (`pseudolife_memory/web/api.py:730`, `:797`, `:829`).

The config write lock is process-local, not a cross-process file transaction; neither fsync nor a bank-wide transaction is present in this implementation (`pseudolife_memory/web/config_io.py:35`, `:882`). A file replacement precedes runtime publication, and a failed copy/dump/replace must not pretend the runtime edit succeeded (`:877`, `:887`, `:899`). Keep the literal KNOBS restart classifications rather than guessing from field names (`:52`).

Open gaps: delegated mutation SQL/columns/sequences and recovery effects; complete shim/docs producer census; nested WebAuthn producer shapes; full per-route oracle map. None of these gaps closes a parity row.

## 9. Effort estimate

Large: the registry spans 38 POST adapters plus atomic config persistence and multiple error/identity surfaces; effort drops once owned service/board/graph dependencies are available (`pseudolife_memory/web/routes.py:124`, `:284`, `pseudolife_memory/web/api.py:790`).
