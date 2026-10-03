# Rust port state

The Python oracle for phases 0b and 1 is pinned to master
`3691f5cb75487d3fda54a6bde6fab35dcf32c681` (0.15.0, schema 53).
All five phase 0b gaps are implemented with verified evidence in
[PR #546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546).
The PR records final appendix review and current-head CI; both must pass before
it is ready. No production Rust surface has been accepted.

| Phase / item | Status | PR | Evidence |
|---|---|---|---|
| 0: Historical inventory, rulebook and disposable trial | Reviewed historical evidence; phase 0 gaps require 0b closure | [#540](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/540) | Historical source pin 136a34ae; trial corrections retained in PORTING.md |
| 0: Historical selfcheck and full-bank replay | Retained as evidence about the original oracle, not current Rust parity | #540 | evals/results/rust-port-phase0-selfcheck-r6/; evals/results/rust-port-phase0-full-bank-r5/ |
| 0b / 2.1: Candidate harness and graded HTTP/MCP controls | Implemented; acceptance evidence verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | evals/results/rust-port-phase0b-acceptance.json; 25 full-bank cases with zero differences, command and URL lanes passed, seven named controls rejected, garbage Rust rejected |
| 0b / 2.2: Linux daemon and Windows/Linux client captures | Implemented; same-platform replay evidence verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | evals/results/rust-port-phase0b-full-bank-linux/run.json; rust-port-phase0b-selfcheck-linux/selfcheck.json (11 cases); rust-port-phase0b-cli-windows/selfcheck.json (two CLI cases); zero replay differences |
| 0b / 2.3: Exhaustive register and per-file buckets | Implemented; source inventory verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | PARITY.md; test-buckets.json; contract-inventory.json; test_contract_inventory.py; audit: 406 files, five candidate nodes, no missing surfaces; nine audit tests pass |
| 0b / 2.4: Representative daemon baseline and noise floor | Implemented; Linux matrix and hosted CI measurements verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | evals/results/rust-phase0b-daemon-scaling-linux.json; 12 fresh-bank runs, eight cells, three repeats; source reconstruction manifest and preserved helper; rust-phase0b-ci-same-head.json: successful attempts 1/3/4, five jobs, job-span noise 86 seconds |
| 0b / 2.5: Housekeeping and rulebook corrections | Implemented; evidence verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | PORTING.md; evals/rust_port/README.md historical pointer; retained R6 selfcheck and full-bank R5; R4/R5 selfchecks removed from current tree |
| 0b: Independent review and CI | Code approved at c317adc4; final appendix review and current-head CI are PR readiness gates | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | Integrated selection: 178 passed, 11 subtests passed; independent review: 63 tests passed, no blocking code findings; all ten checks passed at c317adc4; [current PR checks](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546/checks) |
| 1: Stdio shim | deferred; phase 0b must merge before parity rows can flip | — | MCP-WIRE, SHIM-LIFECYCLE and related rows in PARITY.md |
| 2: Client CLI leaves | deferred | — | CLI rows and 25-mode checklist in PARITY.md |
| 3: Daemon read path | deferred | — | HTTP/read/ranking rows and ONNX prerequisite in PARITY.md |
| 4: Daemon writes and background duties | deferred | — | Mutation/durability/dream/coordination/hook rows in PARITY.md |
| 5: Cutover and retirement | deferred | — | Maintainer owns merge/deploy and behavior retirement |

## Decisions and constraints

- The phase 0b brief re-pins the oracle to master at phase start. The port makes
  no DDL changes, adds no tables and repurposes no columns. Upstream schema bumps
  follow CLAUDE.md's seven-place checklist and a new phase pin.
- Daemon captures and baselines run on Linux; shim and CLI captures run on
  Windows and Linux. Every receipt records its platform and uses anonymous host
  labels with hardware/software details.
- Existing tests and conftest stay immutable; candidate routing uses the external
  pytest plugin and the concrete nodes in test-buckets.json only.
- The old Console was replaced by Console v3 on master, with /ui/next/ removed.
  Other proposed simplifications remain open and are not implemented by the port.
- Historical captures keep their recorded source identity. R6 selfcheck and
  full-bank R5 remain in-tree; superseded R4/R5 selfchecks are preserved outside
  the tree and recoverable from PR #540's recorded tree.
- The daemon baseline uses the phase oracle above. Hosted CI noise controls use
  the reviewed PR #540 head `f2ee15241c29e439c9aaad6fd271683a7a065b3e` and
  Actions run 37090575829; their distinct source identity remains recorded.
  The receipt rust-phase0b-ci-same-head.json includes successful attempts 1, 3
  and 4. Failed attempt 2 (a wait-mail timing test) and the interrupted master
  repeat are excluded. Job-span median is 1,651 seconds with an 86-second
  observed range. Original-created-time totals include earlier attempts and
  are not execution latency. Appendix review and green CI on the final phase
  0b head remain separate gates.
- The Linux matrix's shared provenance helper was frozen before the final
  harness helper. Its exact bytes are retained in
  evals/results/rust-phase0b-baseline-runtime-provenance-32740c866530c6b4.py
  and bound by rust-phase0b-baseline-source-reconstruction.json; no baseline
  capture was edited or relabelled.
- Phase 0b is test/evaluation-only and requires the touched/dependent selection,
  not a local full suite under the brief and CLAUDE.md. When required later,
  full-suite receipts come from ops/wsl-suite.ps1 or ops/remote-suite.ps1 with
  machine and commit in the shared suite-results directory.

## Deferred prerequisites

- The pyproject.toml onnx-extra comment claims bit-identical embeddings while
  docs/guide/configuration.md says default Qwen has no supplied ONNX artifact.
  Both remain unchanged here. Phase 3 requires maintainer docs resolution and a
  named same-graph/tokenizer comparison with measured error before a tolerance
  or embedding-equivalence claim is accepted.
- All internal test equivalents marked pending are future implementation work,
  not accepted wire parity. Inventory coverage alone does not close production
  behavior rows.
- The legacy MCP negotiation evidence from #540 is about its original pin;
  current and earlier protocol revisions require separate current-pin evidence.

## Resume

All five gap implementations and measurement receipts are present. PR #546
records the final appendix review and current-head checks; phase 0 is complete
when those pass and the PR is ready. Phase 1 parity rows stay deferred until
phase 0b is merged.
