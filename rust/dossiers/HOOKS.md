# HOOKS

Source: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`. All source locations below refer to that commit. This is source preparation, not executed parity evidence. Coverage is complete for the eight HTTP hook wrappers and the Python rendering-function inventory; launcher installation internals and the complete client filesystem lifecycle remain incomplete.

## 1. What the row promises

Session hooks supply bounded memory context, preserve session attribution and let the coordination hooks report liveness without making a failed daemon request block ordinary work. The eight endpoints have different authorization and output contracts, so their shared HTTP prefix does not imply a shared unauthorized response (`pseudolife_memory/web/api.py:464`, `:512`, `:534`, `:558`, `:598`, `:624`, `:651`, `:676`). Plugin hook definitions and approved Codex launcher identity also belong to this row (`rust/PARITY.md:282`).

## 2. Entry points and call graph

`build_console_app` (`pseudolife_memory/web/api.py:231`) returns the ASGI `app` (`:404`). Every hook first invokes `_browser_gate` (`:304`): with authentication configured it skips Host/Origin checks; otherwise it admits absent headers or loopback Host/Origin, independent of peer address (`:312`). `_hdr` (`:286`), `_host_part` (`:292`), `_principal` (`:272`), `_authorized` (`:280`), `_resolve` (`:269`), `_resolve_detailed` (`:256`), `_parse_query` (`:193`), `_read_body` (`:127`), `_send_bytes` (`:115`), `_send_json` (`:102`) and `_principals_unavailable` (`:283`) are shared wrapper dependencies.

| Route | Method and wrapper location | Handler and contract |
|---|---|---|
| `/api/hook/session-start` | GET, `pseudolife_memory/web/api.py:464` | Executor closure `start_hook` at `:484` binds/restores request identity, then `hook_session_start` (`pseudolife_memory/web/session_hook.py:650`). Unauthorized callers lose `session_id`/`source`, receive public instructions and may receive version/digest notices; they cannot register identity (`pseudolife_memory/web/api.py:472`). UTF-8 text, no-store. |
| `/api/hook/memory-policy` | GET, `pseudolife_memory/web/api.py:512` | `hook_memory_policy` (`pseudolife_memory/web/session_hook.py:602`); unauthorized query parameters are discarded. Separate full policy only for `full_separate_hook`; resume/compact return empty text. |
| `/api/hook/memory-changes` | GET, `pseudolife_memory/web/api.py:534` | `hook_memory_changes` (`pseudolife_memory/web/session_hook.py:757`); unauthorized body empty. Authorized response starts with a cursor line and optionally a note. |
| `/api/hook/session-end` | POST, `pseudolife_memory/web/api.py:558` | Resolves principal, returns 401 for missing authorization or 503 for unavailable principals; reads at most 16 KiB, maps malformed JSON/non-object to `{}`, calls `hook_session_end` (`pseudolife_memory/web/session_hook.py:787`) and returns JSON `{"ok": true}` for handler failures (`pseudolife_memory/web/api.py:566`, `:577`). |
| `/api/hook/coordination-start` | GET, `pseudolife_memory/web/api.py:598` | `coordination.unavailable_reason` (`pseudolife_memory/coordination.py:360`), then CHECKIN_TEXT or empty text; `X-PL-Board: on` or `off; reason=<code>`. The eligibility check has no storage I/O (`pseudolife_memory/coordination.py:364`). |
| `/api/hook/park-gate` | GET, `pseudolife_memory/web/api.py:624` | Authorized callers invoke `coordination.park_gate` (`pseudolife_memory/coordination.py:273`); unauthorized text empty. Handler failures/invalid ownership yield `allow\n`; valid unparked state may yield `block\n<message>\n`. |
| `/api/hook/woke` | POST, `pseudolife_memory/web/api.py:651` | Authorized callers invoke `coordination.woke` (`pseudolife_memory/coordination.py:306`); `ok\n` when recorded, otherwise empty. Agent travels in query, not body. |
| `/api/hook/subagent` | POST, `pseudolife_memory/web/api.py:676` | Authorized callers invoke `coordination.subagent` (`pseudolife_memory/coordination.py:329`); query selects start/stop and child identity. `ok\n` for recorded state, empty on unavailable/invalid/error. |

The first gate rejects with JSON 403 and the method gate with JSON 405 before calling any hook (`pseudolife_memory/web/api.py:465`, `:469`, and corresponding route blocks). Paths in the table are relative to `pseudolife_memory/`.

All functions in `pseudolife_memory/web/session_hook.py` reachable through these rendering hooks:

| Locations | Roles |
|---|---|
| `:62 launcher_command`, `:72 _checkout_built`, `:84 plugin_update_commands`, `:90 daemon_update_commands`, `:95 all_update_commands` | Validate launcher path and render the correct update command. |
| `:114 hooks_notice`, `:150 _version_key`, `:158 update_notice`, `:180 version_notice` | Shape-check and prioritize release/version/script mismatch notices. |
| `:383 _cold_bank`, `:392 _custom_instructions`, `:405 _utf8_len`, `:409 _bounded_custom_instructions` (nested `pack`, `:423`) | Read bank emptiness/override and pack complete paragraphs within UTF-8 byte budgets. |
| `:440 ab_arm_index`, `:449 memory_policy_variant`, `:465 _startup_policy`, `:485 _continued_context`, `:509 session_start_context` (nested `remaining`, `:520`, `add`, `:523`) | Stable SHA-256 A/B arm; fresh/continued context and bounded authorized briefing. |
| `:560 _episode_advertisement`, `:590 _log_memory_policy`, `:602 hook_memory_policy`, `:620 _dream_stall_line`, `:635 _review_queue_line`, `:650 hook_session_start` | Registration, notices and context composition. |
| `:726 _excerpt`, `:733 _render_memory_changes`, `:757 hook_memory_changes`, `:787 hook_session_end` | One-line excerpts, cursor/note rendering and fail-open close. |

Storage call graph: `_episode_advertisement` → `service.episode_start_session` (`pseudolife_memory/service.py:6365`) → `_register_client_session_locked` (`:6402`) → `PostgresStorage.register_client_session` (`pseudolife_memory/storage/postgres.py:1704`); new roots also call `_persist_episodes` (`pseudolife_memory/service.py:6735`) → `upsert_episode` (`pseudolife_memory/storage/postgres.py:1648`). It then calls `set_active_session` (`pseudolife_memory/service.py:1013`) → `set_meta` (`pseudolife_memory/storage/postgres.py:2722`). End calls `episode_end_session` (`pseudolife_memory/service.py:6428`) → `_close_session_locked` (`:6469`), `_end_client_session_locked` (`:6531`) → `end_client_session` (`pseudolife_memory/storage/postgres.py:1751`), persistence/prune helpers; independently `clear_active_session` (`pseudolife_memory/service.py:1031`) → `set_meta`. The optional close-triggered dream is an owned background/dream dependency (`pseudolife_memory/service.py:6465`).

Changes call `service.memory_changes_since` (`pseudolife_memory/service.py:8579`) and scan the in-memory episode/band/lesson stores under the service lock. Board hooks invoke `unavailable_reason`, validate identity, take `_coordination_lock` and call storage `park_gate` (`pseudolife_memory/storage/coordination.py:2999`), `woke` (`:3044`), `subagent_started` (`:1741`) or `subagent_stopped` (`:1774`), via `pseudolife_memory/coordination.py:295`, `:319`, `:346`; the board implementation remains owned elsewhere.

Digest helpers are `hooks_digest` (`pseudolife_memory/plugin_hooks.py:35`), `plugin_dir` (`:49`) and `daemon_hooks_digest` (`:61`). They read nine files in `HOOK_SCRIPTS` order (`:30`), hash `name NUL content NUL` with CRLF→LF normalization (`:45`), and return None when any script is unreadable.

Codex launcher dependency inventory, not a completed implementation dossier: `ops/setup-codex-hooks.py:154 backup`, `:163 atomic_write`, `:186 codex`, `:196 inventory`, `:211 bundle_bytes`, `:216 complete_set`, `:231 bundle_digest`, `:246 _definitions`, `:259 manual_definitions`, `:266 hooks_root`, `:270 launcher_definitions`, `:278 launcher_installed`, `:290 _pointer`, `:298 _launcher_shipped`, `:303 _vet_launcher`, `:334 _write_bundle`, `:354 _write_launchers`, `:368 _launched`, `:376 _point`, `:380 _prune`, `:392 refresh_manual`, `:417 owned_manual`, `:441 legacy_commands`, `:460 is_legacy`, `:466 select_hooks`, `:486 vet_plugin`, `:528 _roles_complete`, `:538 vet_manual`, `:581 install_manual`, `:641 trust_hooks`, `:676 _user_layer`, `:684 _mailbox_mode`, `:701 mailbox_approval`, `:754 mailbox_notice`, `:778 configure_credential_file`, `:789 credential_environment`, `:818 daemon_request`, `:832 board_checkin_expected`, `:845 episode_open`, `:852 wait_for_daemon`, `:865 verify`, `:937 standing_instructions`, `:962 consent`, `:1001 setup`, `:1086 main`. Definitions are source-verified; their complete transitive installation/file-effect graph is **incomplete** here.

## 3. State touched

| State | Reads/writes and transaction boundaries |
|---|---|
| `episodes` | `id,title,hint,started_at,ended_at,closed_by_new_start,session_key,parent_id`; upsert SQL `pseudolife_memory/storage/postgres.py:1652`, delete SQL `:1679`, each under its own `_txn`. CMS episode map changes under service `_lock` (`pseudolife_memory/service.py:6385`, `:6495`). |
| `client_sessions` | Registration writes `session_key,registered_via,principal,started_at,policy_variant,episode_ids,start_times`; repeat registration clears `ended_at,end_reason`, preserves first non-null principal/policy and appends starts (`pseudolife_memory/storage/postgres.py:1717`). Close writes `ended_at,end_reason,episode_ids` (`:1763`). Each operation is its own transaction, not one transaction enclosing the entire hook. |
| `meta` / `_active_session` | Key `active_session_pointer` (`pseudolife_memory/service.py:966`); value `{session_id,ts}` or JSON null. SQL upsert `pseudolife_memory/storage/postgres.py:2725`. Owner test + clear + persistence share `_lock` (`pseudolife_memory/service.py:1037`). Start registration and pointer publication are separate service calls (`pseudolife_memory/web/session_hook.py:571`, `:575`). |
| Band entries and current lessons | Read `source,superseded_at,timestamp,episode_id,seq,text`, episode session keys/start time, and lesson `asserted_at,value,polarity` (`pseudolife_memory/service.py:8612`, `:8627`, `:8636`); no direct SQL or writes on this path. |
| Coordination hook reads | `coordination_agents` owned/unrevoked row (`pseudolife_memory/storage/coordination.py:3008`); live park/status; `coordination_wakes` delivered/served stamps joined to `coordination_messages` wake metadata (`:3019`); `coordination_events` register/update payload/status fields (`:3033`). |
| Coordination hook writes | Woke locks owned agent row, counts last-hour served wakes, appends audit (`pseudolife_memory/storage/coordination.py:3052`). Subagent start/stop lock row via `_hook_children_row` (`:1728`), write `children,last_activity` (`:1734`) and append audit (`:1736`) in the same `_txn` (`:1758`, `:1779`). `_append` hashes ordered events and inserts them (`:1248`, `:1269`); audit `seq` comes from chain head +1 (`:1256`), not a hook-created sequence. Exact shared audit insert/DDL and rollback contract belong to the board slice; full sequence census is **incomplete**. |
| Files/config | Read daemon `<data_dir>/hook-instructions.md` (`pseudolife_memory/web/session_hook.py:395`), plugin scripts (`pseudolife_memory/plugin_hooks.py:42`), `PSEUDOLIFE_PLUGIN_DIR` (`:53`), build source (`pseudolife_memory/web/session_hook.py:78`), memory-policy config (`:454`). Client marks: `<digest dir>/<sha256(session id)>.mark` (`plugin/hooks/session-start.sh:164`); written after printed note (`:211`), old marks pruned at first note (`:209`). Agent marker consumed by Stop/subagent scripts (`plugin/hooks/stop-wake.sh:217`, `plugin/hooks/subagent-board.sh:87`). Full digest, host-record, lock and cleanup file census is **incomplete**. |

SessionStart may initialize/read additional bank state through `stats`, `session_briefing`, stall/review helpers (`pseudolife_memory/web/session_hook.py:387`, `:548`, `:626`, `:641`); their owned service/read/graph SQL is a dependency, not reimplemented by this wrapper dossier.

## 4. Shipped producers

This is a dossier-local census, pending `rust/producer-census.json`; values below are disposable examples, and identifiers stand for producer-selected values.

| Producer | Canonical shape and source |
|---|---|
| Bash SessionStart | Stdin includes `{"session_id":"demo-session","source":"startup"}` (fallback key `session_start_reason`); GET `/api/hook/session-start?session_id=<sid>&source=<source>[&plugin_version=<version>][&plugin_hooks_digest=<64-lower-hex>][&launcher=<encoded-path>]` (`plugin/hooks/session-start.sh:155`, `:257`). Version `+` encoded as `%2B`; launcher emitted only when off PATH (`:260`, `:271`). |
| Separate policy | Same stdin; shell argv `session-start.sh memory-policy`; GET `?session_id=<sid>&source=<source>` when sid is present, otherwise no query (`plugin/hooks/session-start.sh:221`). |
| Prompt note | `user-prompt-submit.sh` sources `session-start.sh` with argv `memory-changes` (`plugin/hooks/user-prompt-submit.sh:7`). GET `/api/hook/memory-changes?session_id=<sid>[&since=<saved-cursor>]` (`plugin/hooks/session-start.sh:193`). |
| SessionEnd | POST JSON `{"session_id":"demo-session"}`, `content-type: application/json`; ignores response and transport errors (`plugin/hooks/session-end.sh:257`). |
| CoordinationStart | GET `/api/hook/coordination-start`, no query, optional bearer (`plugin/hooks/coordination-start.sh:370`). |
| Stop | GET `/api/hook/park-gate?agent=<32-lower-hex>[&since=<turn-start>]`, then POST `/api/hook/woke?agent=<same-id>` after wake, no JSON body (`plugin/hooks/stop-wake.sh:386`, `:415`, `:436`). Wait/wake workflow is DELIVERY/board work. |
| Subagent hooks | Shell argv `subagent-board.sh start` or `stop`; POST `/api/hook/subagent?agent=<parent-board-id>&event=<start-or-stop>&child=<host-agent-id>&type=<kind>` (`plugin/hooks/subagent-board.sh:16`, `:185`). |
| Native Windows Codex | `pwsh -NoProfile -File <plugin-root>/hooks/lifecycle.ps1 -Event <event>`; eleven event values declared at `plugin/hooks/lifecycle.ps1:3`. Requests mirror start/policy/end shapes (`:521`, `:528`, `:561`), prompt `?session_id=<sid>[&since=<cursor>]` (`:483`), park gate (`:440`) and coordination check-in (`:466`). Context stdout is `{"hookSpecificOutput":{"hookEventName":"<mapped-event>","additionalContext":"<text>"}}`, compressed with non-ASCII escapes (`:28`). SubagentBoardStart/Stop and guard are silent no-ops (`:11`, `:23`), because native children own board addresses. |

Wiring is the 11 handler entries in `plugin/hooks/hooks.json:1`; see [PLUGIN-COMMANDS.md](PLUGIN-COMMANDS.md) for event mapping and PreToolUse guard. Codex CLI-hook leaves are separately owned; do not conflate their scoped parser/wire dispositions with these ASGI routes (`rust/PARITY.md:282`, `rust/SEMANTICS-CHECKLIST.md:10`).

## 5. Python incidentals to defer

Apply the canonical-producer rule (`rust/SEMANTICS-CHECKLIST.md:10`). Defer argparse abbreviations and arbitrary launch argv outside shipped definitions; do not redesign the source notices around Python tuple/version parser corner cases (`pseudolife_memory/web/session_hook.py:150`). Invalid JSON, auth failures, method failures and non-object session-end bodies are explicit wrapper behavior, not incidental (`pseudolife_memory/web/api.py:563`, `:584`). Invalid UTF-8 session-end bytes escape the JSONDecodeError-only catch; no shipped producer identified here emits them (`pseudolife_memory/web/api.py:584`), so mark any Rust refusal as a named divergence rather than assuming fail-open covers it.

Do not apply CRLF normalization to all output: it is scoped to the digest (`pseudolife_memory/plugin_hooks.py:45`). Bash's digest producer removes every CR (`plugin/hooks/session-start.sh:249`), whereas Python replaces CRLF; lone-CR script content is an unexercised producer corner, not proven cross-language equivalence.

## 6. Oracle tests

Behavioral pins (inventory verified at this source; tests **not run** for this dossier):

- `tests/test_session_identity.py::test_hook_start_registers_and_advertises` (`:373`), `::test_hook_end_closes_and_clears_only_owner` (`:389`), `::test_hook_endpoints_unauthorized_with_token_do_not_mutate` (`:414`), `::test_hook_endpoints_authorized_with_token_mutate_normally` (`:439`).
- `tests/test_web.py::test_hook_session_start_token_set_no_bearer_instructions_only` (`:568`), `::test_hook_session_start_capped_under_hook_stdout_limit` (`:633`), `::test_hook_session_start_resume_or_compact_serves_the_handle_not_the_block` (`:673`), `::test_hook_session_start_endpoint_honours_source_only_when_authorized` (`:718`), `::test_hook_session_start_large_override_is_complete_blocks_with_warning` (`:733`).
- `tests/test_memory_changes_hook.py::test_the_cursor_rounds_down_never_past_a_write` (`:151`), `::test_note_is_bounded_and_one_line_per_change` (`:181`), `::test_endpoint_serves_nothing_without_the_bearer` (`:235`), `::test_prompt_hook_prints_only_changes_and_advances_its_cursor` (`:330`), `::test_prompt_hook_keeps_its_cursor_on_an_empty_or_malformed_answer` (`:354`).
- `tests/test_stop_wake_hook.py::test_an_unparked_session_is_asked_once_to_park` (`:902`), `::test_a_firing_hook_posts_a_woke_marker_for_its_board_address` (`:928`), `::test_a_woke_marker_the_daemon_does_not_take_still_wakes` (`:962`).
- `tests/test_subagent_board_hook.py::test_one_request_names_the_session_address_and_the_subagent` (`:140`), `::test_lifecycle_ps1_subagent_events_are_a_silent_no_op` (`:273`).
- `tests/test_hooks_digest.py::test_hooks_digest_is_sha256_over_names_and_lf_normalised_bytes` (`:61`), `::test_a_copy_of_the_scripts_sends_the_same_digest_in_either_line_ending` (`:241`).
- `tests/test_codex_hook_launcher.py::test_a_refresh_moves_the_bundle_and_leaves_the_approved_commands_alone` (`:90`), `::test_the_bash_launcher_runs_the_current_bundle_with_its_arguments_and_input` (`:268`), `::test_the_powershell_launcher_runs_the_current_bundle_with_its_event_input_and_exit_code` (`:287`).

Packaging/source pins: `tests/test_plugin_packaging.py::test_plugin_hook_wiring` (`:194`), `::test_coordination_has_independent_start_and_prompt_handlers` (`:305`), `tests/test_codex_hook_launcher.py::test_the_plugins_codex_approved_hook_definitions_are_pinned` (`:377`). These guard approval identity/source packaging; they do not establish durable endpoint equivalence. The full endpoint-specific coordination/storage test census remains **incomplete**.

## 7. Proposed harness cases and mutants

Proposed, **unrun** cells: cross each endpoint's allowed/wrong method with tokenless-loopback, tokenless-foreign-Origin, correct bearer, missing bearer and unavailable-principals state; record exact status/headers/body and assert no unauthorized mutations. Register two sessions, replay resume/compact, end the first after the second publishes its pointer, and compare episodes/client_sessions/meta after every step. Render multibyte long custom paragraphs and late briefing items under the 9,500-byte budget. Replay prompt baseline/change/quiet/future-cursor/error, including a failed local cursor save. Exercise owned/unowned board ids through park, woke, duplicate child start and missing child stop; compare audit rows and child state using the board owner's fixture. Run actual shipped Bash and native hook definitions against a disposable responder on both OSes; retain stdout, requests and marker bytes.

Six source mutants with rejecting controls:

1. Pass unauthorized SessionStart `session_id` into registration: reject by unchanged episodes/client_sessions/meta.
2. Clear pointer without the owner comparison: reject the two-session stale-end cell.
3. Count Unicode characters instead of UTF-8 bytes: reject multibyte budget cell.
4. Round cursor instead of flooring: reject a write in the last half microsecond.
5. Omit principal ownership in woke/subagent row selection: reject foreign-row/audit mutation.
6. Replace approved launcher command with bundle-specific path: reject refresh identity/approval-definition comparison.

## 8. Dependencies and risks

Needs W2 service writes/identity/board, read briefing and W3 graph/dream/background implementations; those dependencies supply effects described above, not new ownership for this dossier. HTTP-SECURITY and HTTP-BODY-LIMITS own the shared gate/body-reader policies (`pseudolife_memory/web/api.py:304`, `:91`); DELIVERY owns the long watcher/ring path. PLUGIN-COMMANDS owns workflow/manifest/guard packaging (`rust/PARITY.md:303`).

Service calls and DB operations are not one atomic hook transaction; preserve failure-open ordering and independently attempted close/pointer clear (`pseudolife_memory/web/session_hook.py:792`). Clock rollback/recovered old stamps can miss per-turn notes; Python names these limitations (`pseudolife_memory/service.py:8596`). Executor context must be restored even on error (`pseudolife_memory/web/api.py:492`, `:498`). Changing hooks.json approved fields requires renewed Codex approval (`tests/test_codex_hook_launcher.py:377`); leave definitions stable while changing handler implementation.

Open evidence gaps: full Codex setup/refresh file-effect graph; all hook client marker/lock/cleanup paths; full board audit/sequence comparison fixture; daemon init/briefing transitive SQL; exhaustive test-to-route map. These are not claims of completed parity.

## 9. Effort estimate

Large: eight distinct endpoint contracts, bounded context, session identity, board audit effects and two platform producer/approval paths must agree; the breadth follows the row and function inventories (`rust/PARITY.md:282`, `pseudolife_memory/web/api.py:464`, `ops/setup-codex-hooks.py:1001`).
