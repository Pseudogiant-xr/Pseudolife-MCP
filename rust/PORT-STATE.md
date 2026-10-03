# Rust port state

The Python oracle for phases 0b and 1 is pinned to master
`3691f5cb75487d3fda54a6bde6fab35dcf32c681` (0.15.0, schema 53).
Phase 0b is in progress; PR #540 is historical phase 0 evidence and does not
close its identified gaps. No production Rust surface has been accepted.

| Phase / item | Status | PR | Evidence |
|---|---|---|---|
| 0: Historical inventory, rulebook and disposable trial | Reviewed historical evidence; phase 0 gaps require 0b closure | [#540](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/540) | Historical source pin 136a34ae; trial corrections retained in PORTING.md |
| 0: Historical selfcheck and full-bank replay | Retained as evidence about the original oracle, not current Rust parity | #540 | evals/results/rust-port-phase0-selfcheck-r6/; evals/results/rust-port-phase0-full-bank-r5/ |
| 0b / 2.1: Candidate harness and graded HTTP/MCP controls | In progress; final control receipt pending | — | evals/rust_port/; final evidence links pending |
| 0b / 2.2: Linux daemon and Windows/Linux client captures | In progress; platform-labelled capture/replay evidence pending | — | Final capture paths pending |
| 0b / 2.3: Exhaustive register and per-file buckets | Source inventory verified; final branch review pending | — | PARITY.md; test-buckets.json; contract-inventory.json; test_contract_inventory.py; audit: 406 files, five candidate nodes, no missing surfaces; nine audit tests pass |
| 0b / 2.4: Representative daemon baseline and noise floor | Linux matrix verified; hosted CI repeats pending | — | evals/results/rust-phase0b-daemon-scaling-linux.json; 12 fresh-bank runs, eight cells, three repeats; source reconstruction manifest beside receipt |
| 0b / 2.5: Housekeeping and rulebook corrections | Implemented here; harness README pointer/final evidence pending | — | PORTING.md; retained R6 selfcheck and full-bank R5 |
| 0b: Independent review and CI | Pending final frozen tree and review | — | Lead records final validation and reviewed head |
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

Close the five phase 0b gaps against the pin above, attach the final capture,
control and baseline evidence, verify the inventory audit, then obtain a fresh
independent review and green CI on the final head. Phase 0b remains in progress
until those gates are recorded; phase 1 parity rows cannot flip before its merge.
