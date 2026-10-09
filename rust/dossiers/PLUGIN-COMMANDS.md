# PLUGIN-COMMANDS

Source: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`. Every source location refers to that commit. This is a source-verified workflow/wiring/guard dossier; backend MCP, dream/graph mutations and install/update implementations remain owned dependencies. Their complete storage call graph is **not covered** here. No tests or harness cells were run.

## 1. What the row promises

The plugin ships the dream and memory-status user workflows, a stable plugin identity with separate release metadata, and eleven event-handler definitions. Its PreToolUse guard prevents a Claude Code subagent from mutating or acknowledging its parent's board address while preserving allowed reads and parent calls (`plugin/commands/dream.md:4`, `plugin/commands/memory-status.md:4`, `plugin/.claude-plugin/plugin.json:2`, `plugin/hooks/subagent-board-guard.sh:18`). Native Codex children have their own addresses, so the corresponding guard/liveness events are no-ops (`plugin/hooks/lifecycle.ps1:7`, `:16`).

## 2. Entry points and call graph

The two Markdown commands are instruction producers, not Python functions or new HTTP routes. The manifest's plugin name is `pseudolife-memory`, with no version field (`plugin/.claude-plugin/plugin.json:2`); version is carried by `plugin/release.json:1`. The hook script/endpoint graph is in [HOOKS.md](HOOKS.md), and the backend tool boundary remains owned by W2/W3.

Python directly concerned with the plugin script handshake: `pseudolife_memory/plugin_hooks.py:35 hooks_digest`, `:49 plugin_dir`, `:61 daemon_hooks_digest`; HOOK_SCRIPTS at `:30` selects the nine scripts. SessionStart notice functions `pseudolife_memory/web/session_hook.py:114 hooks_notice`, `:180 version_notice`, `:158 update_notice` consume this state; no additional plugin command interpreter exists in these files.

Verified backend dependency boundaries used by the command prose, not new implementations in this row:

| Producer operation | Python boundary and next operation |
|---|---|
| `memory_stats()` | `pseudolife_memory/mcp_server.py:1408` → `service.stats` (`:1413`). |
| `memory_toolset(action="expand")` | Async tool `pseudolife_memory/mcp_server.py:1449`; one step of minimal/core/full visibility (`:1453`). |
| `memory_dream` | `pseudolife_memory/mcp_server.py:2153`; status → dream_status (`:2194`), pull → dream_pull (`:2196`), commit → dream_commit (`:2198`), deep → deep_dream then bounded response (`:2210`). The workflow uses these four actions. |
| `memory_fact_set` / `memory_set_add` / `memory_set_remove` | Tool definitions `pseudolife_memory/mcp_server.py:1678`, `:1736`, `:1755`; workflow selects scalar versus set-valued slot (`plugin/commands/dream.md:49`). Transitive mutation graph is delegated. |
| `memory_forget` | `pseudolife_memory/mcp_server.py:2084`; workflow selects lesson/world scope (`plugin/commands/dream.md:115`). Transitive mutation graph is delegated. |
| `memory_graph_review` | `pseudolife_memory/mcp_server.py:2291`; list → graph_review (`:2338`), propose → graph_propose_links (`:2340`), dismiss_pair → graph_dismiss_duplicate (`:2352`), dismiss_slot_pair → curation_dismiss_duplicate (`:2357`), restore_slot → lesson_restore/world_restore (`:2369`); handlers for accept_link/reject_link/accept_merge/accept_junk begin at `:2383`. |

Guard execution is shell-local: stdin read (`plugin/hooks/subagent-board-guard.sh:39`) → optional coordination-setting gate (`:43`) → nonempty top-level-shaped agent_id (`:60`) → Claude/Codex host precedence (`:69`) → board tool-name match (`:79`) → first action in tool_input (`:91`) → allow list (`:95`) or denial JSON (`:108`). There is no daemon request, storage call or nontrivial Python parser on this path (`:35`). The native guard drains stdin and exits (`plugin/hooks/lifecycle.ps1:11`); native child-liveness events do the same (`:23`).

Hook registry census (exact Bash argv tails and native Event values; plugin-root path syntax and other fields remain as in the file):

| Host event | Bash target/argv | Native `lifecycle.ps1 -Event` | Timeout / flags | Source |
|---|---|---|---|---|
| UserPromptSubmit | `user-prompt-submit.sh` | UserPromptSubmit | 5 | `plugin/hooks/hooks.json:9` |
| UserPromptSubmit | `coordination-prompt.sh` | CoordinationPrompt | 5 | `:19` |
| SessionStart | `session-start.sh` | SessionStart | 15 | `:31` |
| SessionStart | `session-start.sh memory-policy` | MemoryPolicy | 15 | `:41` |
| SessionStart | `coordination-start.sh` | CoordinationStart | 10 | `:51` |
| SessionEnd | `session-end.sh` | SessionEnd | 3 | `:63` |
| Stop | setting gates, `bash -n stop-wake.sh`, then `exec bash stop-wake.sh` | Stop | 1209600; async, asyncRewake | `:75` |
| SubagentStart | `subagent-board.sh start` | SubagentBoardStart | 5; async | `:89` |
| SubagentStop | `bash -n stop-wake.sh`, then `exec bash stop-wake.sh subagent-stop` | SubagentStop | 10 | `:102` |
| SubagentStop | `subagent-board.sh stop` | SubagentBoardStop | 5; async | `:112` |
| PreToolUse | `bash -n subagent-board-guard.sh`, then `exec bash subagent-board-guard.sh` | SubagentBoardGuard | 5; matcher `^mcp__.+__memory_(agents\|message)$` | `:122` |

Bash commands use `bash "${CLAUDE_PLUGIN_ROOT}/hooks/<target>"`; Windows definitions use `pwsh -NoProfile -File "$env:CLAUDE_PLUGIN_ROOT/hooks/lifecycle.ps1" -Event <value>` (`plugin/hooks/hooks.json:9`, `:10`). Stop/guard syntax checks fail open before executing a missing/broken script (`:75`, `:126`).

## 3. State touched

| State | Contract |
|---|---|
| Plugin files | Manifest identity fields, separate release version, two commands, hooks.json, eight `.sh` scripts and `lifecycle.ps1` are read by host/packaging or digest code (`plugin/.claude-plugin/plugin.json:1`, `plugin/release.json:1`, `pseudolife_memory/plugin_hooks.py:30`). Digest reads bytes and normalizes CRLF only in Python (`:45`). |
| Guard inputs/environment | In-memory stdin string; agent_id/session_id/tool_name/tool_input.action; `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `PSEUDOLIFE_CODEX_HOOK`, PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT and coordination setting (`plugin/hooks/subagent-board-guard.sh:54`, `:69`). No tables, columns, sequences, files or transactions are written by the guard. |
| Hook lifecycle state | HTTP hook durable effects and local markers belong to HOOKS/DELIVERY; see HOOKS section 3. The guard itself consumes neither the saved board credential nor a marker. |
| Dream workflow effects | Command prose calls delegated backend operations: snapshot-first deep apply (`plugin/commands/dream.md:26`); manual facts/set membership and commit token (`:44`); proposals/verdicts (`:62`, `:86`, `:102`); reversible lesson/world retirement/restore (`:115`, `:119`). Complete SQL tables/columns/sequences/transactions and snapshot format remain **unverified in this dossier** and must come from the owned graph/dream/write/curation slices. |
| Memory-status effects | Read health/stats/dream status; command reports capacity, drops, degraded components and backup age/rotation (`plugin/commands/memory-status.md:11`, `:15`, `:29`, `:34`, `:39`). It suggests install/backup commands on failure; the prose does not itself execute them. |

## 4. Shipped producers

Dossier-local census, pending `rust/producer-census.json`. These are exact tool parameter structures the prose specifies; user-selected identifiers/text and omitted backend defaults are not invented here.

| Workflow | Parameter shapes and ordering |
|---|---|
| Reach hidden tools | `memory_toolset({"action":"expand"})`, once per tier until full, then rediscover (`plugin/commands/dream.md:17`, `plugin/commands/memory-status.md:6`). |
| Dream start | `memory_dream({"action":"status"})`; if mechanical pass recommended, `{"action":"deep","apply":true}`, else dry run `{"action":"deep"}` (`plugin/commands/dream.md:22`). A snapshot_failed reply means no mutation and investigation, not blind retry (`:28`). |
| Manual extraction | Only no configured extractor or both unreachable: `memory_dream({"action":"pull"})`, scalar `memory_fact_set` with entity/attribute/value and selected origin, or member `memory_set_add`/`memory_set_remove`; then `memory_dream({"action":"commit","commit_token":"<returned-token>"})` (`plugin/commands/dream.md:41`). Additional set-member tool parameter names are not spelled out by this prose and are **not inferred** here. |
| Link candidates | `memory_graph_review({"action":"propose","proposals":[{"src":"demo-a","relation":"implements","dst":"demo-b","rationale":"<evidence>"}]})`; distinct `{"action":"dismiss_pair","src":"demo-a","dst":"demo-b"}`; unsure emits no write (`plugin/commands/dream.md:59`). |
| Entity merges | `{"action":"accept_merge","proposal_id":<id>}`; distinct uses `{"action":"reject_entity","proposal_id":<id>}` plus dismiss_pair; unsure no write (`plugin/commands/dream.md:70`). At most one accepted proposal per shared group, direction read from current evidence (`:74`). |
| Junk | Read `memory_graph_review({"action":"list"})`; `{"action":"accept_junk","proposal_id":<id>}` for artifact or reject_entity for real referent; unsure no write (`plugin/commands/dream.md:95`). |
| Lesson/world duplicate slots | `memory_forget({"scope":"lesson"\|"world",...})` after folding extra content; undo `memory_graph_review({"action":"restore_slot","store":"lesson"\|"world","src":"<entity>\|<attribute>"})`; distinct `{"action":"dismiss_slot_pair","store":...,"src":"<a-key>","dst":"<b-key>"}` (`plugin/commands/dream.md:111`). The ellipsis is genuinely unspecified by this producer prose, not a complete JSON request. |
| Memory-status | GET `/health`; `memory_stats({})`; `memory_dream({"action":"status"})`; mention `/ui/` (`plugin/commands/memory-status.md:11`, `:15`, `:29`, `:46`). No mutation is requested by the ordinary status flow. |
| PreToolUse | Stdin `{"session_id":"demo-session","agent_id":"demo-child","tool_name":"mcp__demo__memory_agents","tool_input":{"action":"update","status":"demo"}}`; denial stdout `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"<source-specific-reason>"}}` and exit 0 (`plugin/hooks/subagent-board-guard.sh:54`, `:108`). Missing agent_id represents the parent and passes; agent tool absent/default/list passes; message receive/history passes, while send/ack/default/unknown actions deny in guarded Claude child context (`:60`, `:95`). |

Hook HTTP/argv producers are specified in HOOKS section 4; the event table above is their packaging identity. Release metadata is currently `{"version":"0.17.0"}` (`plugin/release.json:1`), not a new plugin-manifest version key.

## 5. Python incidentals to defer

Canonical shapes are those selected by shipped host definitions/workflow prose (`rust/SEMANTICS-CHECKLIST.md:10`). Do not turn shell regex extraction into a general JSON parser contract: the guard intentionally fails open for unreadable payloads (`plugin/hooks/subagent-board-guard.sh:24`, `:54`). Preserve the host-marker precedence and quoted-string controls reached by actual host payloads (`:62`, `:74`). Backend argparse/coercion/slot-key corner cases stay in their owned rows; the instruction producer does not establish new reachable shapes.

Do not transplant Python digest CRLF normalization onto command stdout, manifests or all filesystem observations (`pseudolife_memory/plugin_hooks.py:45`). The row includes exact approval-relevant definitions: timeout, async, matcher, command and Windows command are not incidental packaging details (`tests/test_codex_hook_launcher.py:377`).

## 6. Oracle tests

Behavioral pins, inventory verified and **not run**:

- `tests/test_subagent_board_guard.py::test_a_subagents_board_write_is_denied_with_a_reason_it_can_act_on` (`:93`), `::test_the_parents_own_board_write_is_untouched` (`:103`), `::test_a_subagents_board_read_is_allowed` (`:108`), `::test_any_server_name_carrying_the_board_tools_is_guarded` (`:113`).
- `tests/test_subagent_board_guard.py::test_an_action_quoted_inside_a_message_is_not_the_calls_action` (`:128`), `::test_a_parent_message_quoting_agent_id_is_still_the_parents` (`:136`), `::test_a_payload_it_cannot_read_is_allowed` (`:150`), `::test_codex_context_allows_a_childs_board_write` (`:167`), `::test_claude_codes_own_hook_stays_claudes_whatever_codex_marker_it_inherits` (`:185`), `::test_a_broken_copy_of_the_script_blocks_nothing` (`:240`), `::test_the_native_command_allows_everything` (`:265`).
- `tests/test_subagent_board_hook.py::test_one_request_names_the_session_address_and_the_subagent` (`:140`), `::test_lifecycle_ps1_subagent_events_are_a_silent_no_op` (`:273`).
- `tests/test_hooks_digest.py::test_hooks_digest_is_sha256_over_names_and_lf_normalised_bytes` (`:61`), `::test_hooks_digest_is_none_when_a_script_is_missing` (`:86`).
- `tests/test_codex_reapproval.py::test_a_changed_hooks_json_needs_approval_and_is_named` (`:89`), `::test_changed_scripts_alone_are_behind_and_keep_their_approval` (`:96`).

Packaging/prose/source pins (necessary but not behavioral execution of the commands):

- `tests/test_plugin_packaging.py::test_plugin_manifest_carries_no_version` (`:89`), `::test_plugin_release_matches_pyproject` (`:105`), `::test_plugin_ships_no_mcp_server` (`:182`), `::test_plugin_hook_wiring` (`:194`), `::test_coordination_has_independent_start_and_prompt_handlers` (`:305`).
- `tests/test_plugin_packaging.py::test_dream_command_reads_the_review_queue_health_block` (`:438`), `::test_plugin_dream_command_matches_examples` (`:446`), `::test_plugin_commands_reference_only_real_tools` (`:504`), `::test_plugin_commands_say_how_to_reach_hidden_tools` (`:515`).
- `tests/test_subagent_board_guard.py::test_the_matcher_selects_only_the_board_tools` (`:215`), `tests/test_codex_hook_launcher.py::test_the_plugins_codex_approved_hook_definitions_are_pinned` (`:377`).

No behavioral oracle was established here for an agent completing the entire prose dream/status workflow. These source tests establish shipped instruction integrity; backend operation oracles remain separate.

## 7. Proposed harness cases and mutants

Proposed, **unrun** guard cells invoke the actual registered command with canonical host stdin for parent/Claude child/Codex child/nested host markers, agents default/list/update/claim/release, message receive/history/send/ack/default, different MCP server names, quoted action/id in message text, malformed payload and coordination opt-out. Compare exact stdout/exit and assert zero HTTP/file effects. Repeat native overrides on Windows; remove or syntactically break only a disposable script copy and verify definition-level fail-open behavior.

Workflow source cells validate all referenced tools/actions, release/manifest identity, eleven definitions and every script in the digest; a disposable scripted agent transcript can supply status/deep/pull/proposal replies and check requested call ordering without running real graph mutations. Treat that transcript as workflow instrumentation, not proof of model judgment or complete durable backend parity. Pair backend dependency cells with their owners rather than inventing a second graph harness.

Six mutants:

1. Allow child memory_agents update: deny decision disappears in canonical child-write cell.
2. Deny message receive/history: allowed-read cell fails.
3. Let inherited Codex marker override a matching Claude session: host-precedence cell fails.
4. Remove bash syntax preflight from guard definition: disposable broken-script cell returns a blocking nonzero exit.
5. Add manifest version or remove separate release file: packaging identity/release pin fails.
6. Remove token from dream commit instructions or remove full-tier expansion: workflow source/call-order cell rejects missing contract.

## 8. Dependencies and risks

HOOKS supplies endpoint/rendering/identity behavior, DELIVERY supplies long watchers, W2 supplies MCP/tool tiers and board ownership, and W3 supplies graph/dream verdict/snapshot semantics (`rust/PARITY.md:303`, `plugin/commands/dream.md:17`, `:26`). Command prose is a shipped producer: backend ports must retain fields it inspects, including deep-dream recommendation, review_queue attention, snapshot failure, judge evidence and contested outcomes (`plugin/commands/dream.md:22`, `:54`, `:70`).

The guard's exit 0 denial JSON is deliberate; a syntax failure returning exit 2 could block parent calls, hence the preflight (`plugin/hooks/subagent-board-guard.sh:27`, `:108`). Hook definitions are Codex approval identity; script-only change and definition change have different reapproval consequences (`tests/test_codex_reapproval.py:89`, `:96`). Bash/native host behavior differs by design; native child liveness is a no-op, not an unported mutation (`plugin/hooks/lifecycle.ps1:16`).

Open gaps: transitive backend SQL/sequence/file recovery graph; exact dynamic scalar/set/forget parameters that workflow prose leaves unspecified; agent-level end-to-end judgment oracle; full install/update/reapproval implementation beyond the named source tests. These must not be filled by assuming source prose proves backend execution.

## 9. Effort estimate

Medium for this row's packaging/workflow/guard boundary, because most durable work is delegated; host attribution and approved-definition stability still require both platform paths (`plugin/hooks/subagent-board-guard.sh:62`, `plugin/hooks/lifecycle.ps1:11`, `tests/test_codex_hook_launcher.py:377`).
