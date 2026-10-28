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
