# Divergence triage and closure index

This is the B14a inventory and B14b routing list for the declared daemon
divergences. Every entry has one D7 bucket and code evidence in its own file;
`_section.md` files and this index are not entries.

The audit covers master `ac275ac984178570cae449a92be7192989247995`
(2026-11-02). The original 259 entries were checked at `54063c1788f2`;
#1126 added the reranker tie entry and clarified the model-provisioning row.
Those two rows were reconciled against `ac275ac9`; the daemon implementation
did not change between these two heads. This is source verification, not a
new differential, installed-execution or recovery proof. R2 subsequently closed
the two stats projection entries with the shared REST handler and authenticated
REST/MCP/REST and token-gate coverage; the residual route family contains only
the five D9-retired routes with no frontend caller.

Under D7, behaviour with a caller must close before G2. Presentation and
no-caller rows remain explicit dispositions for the maintainer; the two
sent-YAML presentation deferrals are already accepted. A closed row cites
the implementation or PR that removed its difference, or the maintainer
decision that accepted it as a deliberate refusal. Existing accepted
algorithm choices retain their behavioural bucket and named disposition.

On 2026-11-07 the maintainer accepted 75 caller rows (the D7 triage of that
date): 67 harmless ones move to "behaviour with no caller" as no caller
impact, each row saying why; 8 named refusals or substitutions (the PDFium
refusal, the three model-boundary rows, the native updater, the Codex
bootstrap authority, TLS and the shutdown budget) are closed by that
acceptance and keep their named errors. Each row says "maintainer,
2026-11-07".
Rows can overlap one closure obligation, so entry counts are not counts of
unique bugs or implementation batches.

Python remains the documented default until G2. This index changes no
runtime default and does not close B12, G1 or G2.

PR #1133 adds the native Codex bootstrap authority boundary and records the
closed no-spawn planning difference.
Native registration recovery adds the rollback-ownership row, accepted by
the maintainer on 2026-11-07.

The entry totals per D7 bucket are derived from the entry files, never
committed here: `python rust/daemon/split_divergences.py --counts` prints them.

TLS wiring now covers six modes and PEM client identities; its named refusals and remaining libpq conventions were accepted by the maintainer on 2026-11-07.

## Behaviour with a caller: B14b routing

Each link names the exact ledger row. Owners are the implementation streams
R1–R6 where their briefs cover the work, otherwise the named cutover batch;
the conductor assigns those batch obligations. B14b itself is a named
cutover batch and is a valid owner for residual divergence dispositions.
Explicit acceptance-harness callers keep open proof obligations in this
list; they are not treated as no-caller dispositions. Naming an owner does not
claim that its implementation or acceptance evidence has landed. A row
whose remaining item is a proof keeps that boundary visible in its entry.

| Entry | Caller | Closing stream or batch |
|---|---|---|
| [CONSOLE-SIGNIN](routes-whose-handlers-belong-to/console-sign-in-sessions-and-routes.md) | the passkey half only (link sign-in served since 2026-11-03): the Console's passkey sign-in option against `/api/console/session`, `/api/console/passkey-challenge` and `/api/console/passkey-bind` (#1166) | B5 board; G2 |
| [One-time import of a pre-v57 chromadb store](document-store-b7-document/one-time-import-of-a-pre-v57-chromadb.md) | existing pre-v57 document banks reached by installer/update engine cutover | B11 legacy import via R4/R5 |
| [Graph identity Unicode version](graph-and-review/graph-identity-unicode-version.md) | Unicode entity/alias names supplied to `pseudolife_memory/mcp_server.py` graph tools and `rust/daemon/src/graph_write_tools.rs` | B1 graph identity; B13 oracle-runtime evidence |
| [A reconnect that fails after winning the lease (reading or bumping the epoch)](lifecycle-and-storage/a-reconnect-that-fails-after-winning-the.md) | `rust/daemon/src/service.rs::Service::verify_writer_session` after a reconnect's epoch read/bump fails | B4 lifecycle/storage |
| [Where a dead writer session heals](lifecycle-and-storage/where-a-dead-writer-session-heals.md) | `rust/daemon/src/dream/run.rs` long dream passes and `rust/daemon/src/board/dispatch.rs` sends during connection loss | B4 lifecycle/storage; B5 board |
| [Console static dir](startup-and-configuration/console-static-dir.md) | Console requests from `frontend/src/main.ts` to `/ui` on a native or image launch | B11 installed layout via R4/R5 |
| [CPU precision](startup-and-configuration/cpu-precision.md) | default `embedding.cpu_dtype: auto` and existing explicit bf16 settings, used by daemon search/store | R10 actionable refusal under D4; B8 bf16 export follow-up |
| [Plugin dir for `hooks_digest`](startup-and-configuration/plugin-dir-for-hooks-digest.md) | `pseudolife_memory/doctor_cli.py::_codex_hooks_line` and `rust/shim/src/cli/doctor/mod.rs` health digest checks on a native launch | B11 runtime/install layout via R4/R5 |
| [Storage modes](startup-and-configuration/storage-modes.md) | installed no-DSN startup through Python `pseudolife_memory/service.py::MemoryService` and the lite installer path | B9 embedded Postgres (outside current R1-R7 scope); B11 migration via R4/R5 |
| [`config-write-restart-only`: `POST /api/config` on a live knob](w2-d-completion-boundary-2026-10/config-write-restart-only-post-api.md) | `frontend/src/lib/api/config.ts::configApi.write`, used by `frontend/src/views/Settings.svelte` | B4 runtime configuration; B2d config API |
| [`POST /api/config` over a config.yaml this daemon cannot rewrite exactly: a YAML timestamp or `!!binary` value, a mapping or list shared through an anchor and alias (a merge of a mapping of scalars is fine), an integer past 64 bits](w2-d-completion-boundary-2026-10/post-api-config-over-a-config-yaml-this.md) | `frontend/src/lib/api/config.ts::configApi.write` against an existing Python-authored config.yaml during engine cutover | B15 configuration/environment; B2d config API |
| [Casefold in slot keys and pin scope](w2-d-get-api-search/casefold-in-slot-keys-and-pin-scope.md) | `pseudolife_memory/mcp_server.py::memory_fact_set` entity/attribute inputs and cortex search via `rust/daemon/src/read/cortex_search.rs` | B1 canonical identity; B2a search |
| [Pin-scope word characters, non-ASCII](w2-d-get-api-search/pin-scope-word-characters-non-ascii.md) | `pseudolife_memory/mcp_server.py::memory_search` cortex block and `frontend/src/lib/api/stream.ts::streamApi.search` | B2a cortex search |
| [A pre-v57 `memory_state/weights.pt` beside a bank with no counters row](write-path-w2-e/a-pre-v57-memory-state-weights-pt-beside.md) | `pseudolife_memory/update_engine.py::require_imported_bank` on `update --engine rust`, and daemon startup via `rust/daemon/src/service.rs::Service::load_counters` | R4 / B11 legacy-counter migration and installed update acceptance |
| [A `weights.pt` left beside a bank that has a counters row](write-path-w2-e/a-weights-pt-left-beside-a-bank-that-has.md) | `pseudolife_memory/daemon.py::run_daemon` startup after rollback, through `MemoryService._load_counters` | R4 / B11 rollback and legacy-counter migration |
| [`custom` band `retention_policy` other than `balanced`](write-path-w2-e/custom-band-retention-policy-other-than.md) | `rust/daemon/src/store.rs::store_body` at capacity with a custom preset loaded by `config::load` | B14b W2-E custom-retention closure |
| [The episode anchor's walk order](write-path-w2-e/memory-consolidation-candidates/the-episode-anchor-s-walk-order.md) | `rust/daemon/src/consolidation_tools.rs::mcp_memory_consolidation_candidates` with an episode anchor after multi-band stores | B14b W2-E multi-band promotion and consolidation closure |
| [`re.IGNORECASE` letter classes at the variant-token boundaries (`(?<![A-Za-z0-9])`)](write-path-w2-e/memory-dream-deep-the-deep-dream/re-ignorecase-letter-classes-at-the.md) | `rust/daemon/src/dream/deep.rs` `plan` invokes graph consolidation over entity names created by graph / fact tools | B1-P (dream engine), B14b |
| [`\w` in the identifier-keyed entity pattern](write-path-w2-e/memory-dream-deep-the-deep-dream/w-in-the-identifier-keyed-entity-pattern.md) | `rust/daemon/src/dream/deep.rs` `plan` calls `slot_duplicate_candidates` over lesson/world entity names | B1-P (dream engine), B14b |
| [Wrong-type `memory.deep_dream` deep knobs (`top_k_candidates: 2.5`, `auto_apply_safe: "yes"`)](write-path-w2-e/memory-dream-deep-the-deep-dream/wrong-type-memory-deep-dream-deep-knobs.md) | `rust/daemon/src/config.rs` `load` and `pseudolife_memory/utils/config.py` `load_config` consume the operator's persisted config.yaml | B15 (CONFIG-LOAD), B14b |
| [A digest's capacity victim with a deeper band configured](write-path-w2-e/memory-dream-run-outcome/a-digest-s-capacity-victim-with-a-deeper.md) | `rust/daemon/src/dream/stages.rs` `generate_digests_stage` / Python `generate_digests_stage`, when digest_enabled and a deeper band are configured | B1-P (digest) and B4 (resident store durability), B14b |
| [An uncertain lesson commit that cannot be reconciled](write-path-w2-e/memory-dream-run-outcome/an-uncertain-lesson-commit-that-cannot.md) | `rust/daemon/src/dream/run.rs` `run_locked` invokes lesson synthesis; `rust/daemon/src/lesson_tools.rs` lesson reads and later service calls follow an unresolved commit | B1-P (lesson synthesis) and B4 (recovery read barriers), B14b |
| [Betweenness above `betweenness_sample` nodes (200)](write-path-w2-e/memory-dream-run-the-graph/betweenness-above-betweenness-sample.md) | `rust/daemon/src/dream/run.rs` `run_locked` calls `graph_insight::safe_refresh`; Python `_refresh_graph_insight` builds digest on graphs above betweenness_sample | B1-P2b (graph insight / Option B disposition), B14b |
| [Louvain's visiting order](write-path-w2-e/memory-dream-run-the-graph/louvain-s-visiting-order.md) | `rust/daemon/src/dream/run.rs` `run_locked` calls `graph_insight::safe_refresh` on normal dream completion | B1-P2b (graph insight / Option B disposition), B14b |
| [The oversized-community split's subgraph order](write-path-w2-e/memory-dream-run-the-graph/the-oversized-community-split-s-subgraph.md) | `rust/daemon/src/graph_insight.rs` `compute` / Python `_refresh_graph_insight` split communities larger than max_community_fraction | B1-P2b (graph insight / Option B disposition), B14b |
| [Wrong-type `memory.graph_insight` knobs (`resolution: "1"`, `god_nodes_top_n: 2.5`)](write-path-w2-e/memory-dream-run-the-graph/wrong-type-memory-graph-insight-knobs.md) | `rust/daemon/src/config.rs` `load` / Python `load_config` consume operator config.yaml | B15 (CONFIG-LOAD), B14b |
| [The date mask, `isdigit` and NFC/NFKC in the gates](write-path-w2-e/memory-dream-status-pull-commit/the-date-mask-isdigit-and-nfc-nfkc-in.md) | `rust/daemon/src/dream/run.rs` `run_locked` applies the gates to user memory and model claims | B1-P (dream engine), B14b |
| [A forget whose commit outcome is unknown](write-path-w2-e/memory-forget-2026-10-19/a-forget-whose-commit-outcome-is-unknown.md) | MCP `memory_forget(scope="memory")` or Console `/api/delete` during connection loss around commit | B4 lifecycle / B14b W2-E commit-unknown recovery |
| [File mode](write-path-w2-e/memory-forget-2026-10-19/file-mode.md) | Python CLI `serve` file-mode fallback followed by `pseudolife_memory/mcp_server.py::memory_forget` | B9 / D1 file-mode retirement, with R7 / B12 deprecation documentation |
| [A curated restore whose commit outcome is unknown and whose reload cannot be classified](write-path-w2-e/memory-graph-review-list-and-the/a-curated-restore-whose-commit-outcome.md) | `rust/daemon/src/graph_review_tools.rs` `mcp_memory_graph_review(action="restore_slot")`, then later reads after ambiguous COMMIT | B1-O (curation restore) and B4 (recovery read barriers), B14b |
| [A curation judge fold whose commit outcome is unknown and whose reload cannot be classified](write-path-w2-e/memory-graph-review-list-and-the/a-curation-judge-fold-whose-commit.md) | `rust/daemon/src/dream/ticks.rs` `sweep` calls `deep_dream_judge_curation`; later ticks / reads follow an ambiguous fold commit | B1-P (curation judge) and B4 (recovery read barriers), B14b |
| [A `proposals` entry whose `rationale` is a list](write-path-w2-e/memory-graph-review-list-and-the/a-proposals-entry-whose-rationale-is-a.md) | `pseudolife_memory/mcp_server.py` `memory_graph_review(action="propose")` accepts list[dict]; Rust `actions::plan` / `propose` forwards those dict fields | B1-O (proposal input parity), B14b |
| [Wrong-type `memory.deep_dream.judges_enabled` / `analyzer_file_duplicates`](write-path-w2-e/memory-graph-review-list-and-the/wrong-type-memory-deep-dream-judges.md) | `rust/daemon/src/config.rs` `load` / Python `load_config` read operator config.yaml | B15 (CONFIG-LOAD), B14b |
| [A correction or reinstatement whose outcome is unknown (the lock session or connection lost around `COMMIT`)](write-path-w2-e/memory-supersede-and-memory/a-correction-or-reinstatement-whose.md) | MCP `memory_supersede`, `memory_consolidate` or `memory_reinstate` during connection loss around commit | B4 lifecycle / B14b correction and reinstatement recovery |
| [File-mode corrections (unique exact text without row ids, `id_unavailable`, the file-signature recovery)](write-path-w2-e/memory-supersede-and-memory/file-mode-corrections-unique-exact-text.md) | Python CLI `serve` file-mode fallback followed by MCP correction/reinstatement | B9 / D1 file-mode retirement, with R7 / B12 deprecation documentation |
| [The promotion walk after the replacement (`_consolidate_eligible`)](write-path-w2-e/memory-supersede-and-memory/the-promotion-walk-after-the-replacement.md) | MCP `memory_supersede` or `memory_consolidate` with an opt-in multi-band preset | B14b W2-E multi-band correction promotion |
| [Non-ASCII decimal digits in a generic session title](write-path-w2-e/non-ascii-decimal-digits-in-a-generic.md) | MCP `memory_session_title` or `/api/episode/start` supplying a Unicode-decimal generic-shaped title, then store/close/reap | B14b episode title-predicate closure / B3 hooks |
| [Per-call `_ensure_init` passes: correction / reinstatement / curation / lesson-synthesis recovery](write-path-w2-e/per-call-ensure-init-passes-correction.md) | Every MCP/Console service call after a write leaves recovery pending, including `store::mcp_memory_store` | B4 lifecycle / B14b cross-tool recovery barriers |
| [Presets with more than one band (`continuum` and aliases, multi-band `custom`)](write-path-w2-e/presets-with-more-than-one-band.md) | MCP `memory_store` with continuum/custom bands from `config::load` | B14b W2-E multi-band retention and promotion |
| [File mode (`graph_requires_postgres`)](write-path-w2-e/the-graph-store-s-write-tools/file-mode-graph-requires-postgres.md) | Python CLI `serve` file-mode fallback followed by MCP graph write tools | B9 / D1 file-mode retirement, with R7 / B12 deprecation documentation |
| [Autosave and clean-exit flush](write-path-w2-e/w2-e-first-increment-boundaries/autosave-and-clean-exit-flush.md) | Opt-in engine update on a pre-v57 bank through `pseudolife_memory/update_engine.py::require_imported_bank` | R4 / B11 installed legacy migration; D2 remains the documented Python exception |
| [Exact-duplicate boundary](write-path-w2-e/w2-e-first-increment-boundaries/exact-duplicate-boundary.md) | MCP `memory_store` at the duplicate/surprise boundary | B14b W2-E exact admission/rejection-reason acceptance |
| [Frozen store catalog snapshots](write-path-w2-e/w2-e-first-increment-boundaries/frozen-store-catalog-snapshots.md) | `rust/daemon/harness/w2e.py::run_golden` consumes the frozen store catalog for replay acceptance | B8 / B14b W2-E frozen-catalog acceptance |
| [Hosted write-path execution](write-path-w2-e/w2-e-first-increment-boundaries/hosted-write-path-execution.md) | `.github/workflows/rust.yml` `body-evidence` invokes `rust/ci/body_evidence.py::run` for D6/D11 acceptance | B13 scheduled-master evidence and Windows-specific comparisons |
| [New reaper recording's startup state](write-path-w2-e/w2-e-first-increment-boundaries/new-reaper-recording-s-startup-state.md) | `rust/daemon/harness/w2e.py::run_golden` consumes the reaper recording and `dbstate.py::dump` compares startup state | B4 / W3-H reaper-startup recording acceptance |

## Keeping the index current

When a stream closes a row, update its D7 bucket and cite the closing PR or
code, then remove its B14b routing row. New divergence
entries need a bucket, code evidence and, for behaviour with a caller, a
caller and closing stream or batch. Preserve the one-entry-per-file format.

Run `python rust/daemon/split_divergences.py --check` and
`python -m pytest rust/ci/test_divergences_ledger.py` after ledger edits.
The check rejects missing or unknown buckets, mismatched table widths,
a committed count table and incomplete or duplicate caller links.
The renderer excludes this summary and retains the per-entry evidence.
