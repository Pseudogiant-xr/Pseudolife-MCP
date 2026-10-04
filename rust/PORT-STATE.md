# Rust port state

The Python oracle for phase 1 is pinned to master
`f709abb54f7912ae9cd767998d0926ca33df4bcd` (0.16.1, schema 54).
The historical phase 1 pin is `0b015f9279a778f996e71ee78510695e5fee7196` (0.16.0, schema 53);
existing receipts retain their original source identities.
The historical phase 0b oracle remains
`3691f5cb75487d3fda54a6bde6fab35dcf32c681` (0.15.0, schema 53);
existing phase 0b receipts retain that source identity.
Phase 0 is complete. All five phase 0b gaps are closed with verified evidence in
[PR #546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546).
The final appendix review and all ten CI checks passed at `9a62ed02`.
Subsequent PR updates require fresh review and current-merge-ref CI.
No production Rust surface has been accepted.

| Phase / item | Status | PR | Evidence |
|---|---|---|---|
| 0: Inventory, rulebook and disposable trial | Complete; phase 0b closes the historical gaps | [#540](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/540), [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | Historical source pin 136a34ae; trial corrections retained in PORTING.md; current-pin gap evidence below |
| 0: Historical selfcheck and full-bank replay | Retained as evidence about the original oracle, not current Rust parity | #540 | evals/results/rust-port-phase0-selfcheck-r6/; evals/results/rust-port-phase0-full-bank-r5/ |
| 0b / 2.1: Candidate harness and graded HTTP/MCP controls | Implemented; acceptance evidence verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | evals/results/rust-port-phase0b-acceptance.json; 25 full-bank cases with zero differences, command and URL lanes passed, seven named controls rejected, garbage Rust rejected |
| 0b / 2.2: Linux daemon and Windows/Linux client captures | Implemented; same-platform replay evidence verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | evals/results/rust-port-phase0b-full-bank-linux/run.json; rust-port-phase0b-selfcheck-linux/selfcheck.json (11 cases); rust-port-phase0b-cli-windows/selfcheck.json (two CLI cases); zero replay differences |
| 0b / 2.3: Exhaustive register and per-file buckets | Implemented; source inventory verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | PARITY.md; test-buckets.json; contract-inventory.json; test_contract_inventory.py; audit: 406 files, five candidate nodes, no missing surfaces; 21 audit tests pass |
| 0b / 2.4: Representative daemon baseline and noise floor | Implemented; Linux matrix and hosted CI measurements verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | evals/results/rust-phase0b-daemon-scaling-linux.json; 12 fresh-bank runs, eight cells, three repeats; source reconstruction manifest and preserved helper; rust-phase0b-ci-same-head.json: successful attempts 1/3/4, five jobs, job-span noise 86 seconds |
| 0b / 2.5: Housekeeping and rulebook corrections | Implemented; evidence verified | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | PORTING.md; evals/rust_port/README.md historical pointer; retained R6 selfcheck and full-bank R5; R4/R5 selfchecks removed from current tree |
| 0b: Independent review and CI | Complete at 9a62ed02; code and final appendix approved, all ten checks passed | [#546](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546) | Integrated selection: 178 passed, 11 subtests passed; independent reviews at c317adc4 and 9a62ed02, no blocking code findings; [PR checks](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/546/checks) |
| 1: Stdio shim | In progress; master-forward inventory closed; acceptance gates pending | [#560](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/560) (draft) | Frozen 2e628b27 checks and strict judges pass on both platforms; all 125 scoped equivalents have evidence. Paired measurements are recorded; required full suites and final-head CI remain pending; no Rust acceptance claimed |
| 2: Client CLI leaves | deferred | — | CLI rows and 26-mode checklist in PARITY.md |
| 3: Daemon read path | deferred | — | HTTP/read/ranking rows and ONNX prerequisite in PARITY.md |
| 4: Daemon writes and background duties | deferred | — | Mutation/durability/dream/coordination/hook rows in PARITY.md |
| 5: Cutover and retirement | deferred | — | Maintainer owns merge/deploy and behavior retirement |

## Decisions and constraints

- Phase 1 starts from master `0b015f92` on `codex/rust-phase1`.
  PR #546 was still open at branch creation; its branch head `31f8475e`
  was merged into the phase 1 branch at `93012944`, as the phase 1 brief
  permits. PR #546 and its branch are unchanged. The maintainer owns its merge.
- The phase 1 work window ends at 2026-10-04 12:30 AEDT (01:30 UTC).
  Incomplete work will retain explicit pending evidence and a draft PR.
- The phase 0b brief re-pins the oracle to master at phase start. The port makes
  no DDL changes, adds no tables and repurposes no columns. Upstream schema bumps
  follow CLAUDE.md's seven-place checklist and a new phase pin.
- Daemon captures and baselines run on Linux; current phase 0b receipts were
  captured on WSL2, while production runs in a Linux container. Shim and CLI
  captures run on Windows and Linux. Every receipt records its platform and uses anonymous host
  labels with hardware/software details.
- Existing files under `tests/`, including `tests/conftest.py`, stay immutable; candidate routing uses the external
  pytest plugin and the concrete nodes in phase1-test-buckets.json only.
- The old Console was replaced by Console v3 on master, with /ui/next/ removed.
  Other proposed simplifications remain open and are not implemented by the port.
- Historical captures keep their recorded source identity. R6 selfcheck and
  full-bank R5 remain in-tree; superseded R4/R5 selfchecks are preserved outside
  the tree and recoverable from PR #540's recorded tree at `f2ee1524`.
- The phase 0b daemon baseline uses its historical `3691f5cb` oracle. Hosted CI noise controls use
  the reviewed PR #540 head `f2ee15241c29e439c9aaad6fd271683a7a065b3e` and
  Actions run 37090575829; their distinct source identity remains recorded.
  The receipt rust-phase0b-ci-same-head.json includes successful attempts 1, 3
  and 4. Failed attempt 2 (a wait-mail timing test) and the interrupted master
  repeat are excluded. Job-span median is 1,651 seconds with an 86-second
  observed range. Original-created-time totals include earlier attempts and
  are not execution latency. Appendix review and all ten phase 0b CI checks
  passed separately at `9a62ed02`.
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
- All 125 scoped internal cases have targeted Rust equivalents; 56 other
  internal cases remain outside Phase 1's scope. Inventory and component
  coverage alone do not close production behavior rows.
- The legacy MCP negotiation evidence from #540 is about its original pin;
  current and earlier protocol revisions require separate current-pin evidence.

## Resume

Phase 1 close-out uses Python 0.16.1/schema 54 at `f709abb54f7912ae9cd767998d0926ca33df4bcd`
and the stacked phase 0b branch above. PR #546 remains open and unmerged;
PR #560 remains a draft. All production behavior rows remain deferred.

Frozen candidate `2e628b27226ac28344d3218f679ff2c2de99422e`
(tree `6ee4ecde802fefa88f5bc64f3aeebe4c41289f91`) passes Rust 1.94
fmt, all-target check, clippy and release build on both platforms. Windows
nextest passes 280/280 and Linux 262/262, with no skipped tests. The combined
harness/inventory selection passes 214 tests; the Phase 1 audit accounts for
408 files, 13 routed process nodes and 26 CLI modes with no missing surfaces.
Of the routed nodes, eight public stdio cases select Rust; five historical CLI
nodes remain separately accounted for. All 125 scoped internal assertions name
Rust targets with Windows/Linux evidence; 56 internal functions remain outside
this phase. These overlapping selections are not summed into a coverage count.
Existing files under `tests/`, including `tests/conftest.py`, remain unchanged
relative to the merged master base.

The strict frozen Rust judges pass eight public subprocess tests on each
platform with zero retained-wire differences and verified cleanup:
`evals/results/rust-port-phase1-rust-windows-2e628b27.json` and
`evals/results/rust-port-phase1-rust-linux-2e628b27.json`. Each covers both
protocol eras on fresh guarded banks, EOF observations and fault cells.
Real public-process identity passes; wrong-protocol and duplicate-key controls
reject their named defects under `stdio-raw-compared`. The six stdio mutation
controls and seven generic HTTP proxy controls reject their intended defects;
the fresh-bank generic identity proxy passes with zero mutations/differences.
Generic Python controls establish judge policy, not Rust daemon parity.
The only wire allowances remain named source-newline spans and the observed
final EOF error order set. Earlier receipts retain their original candidate
and oracle identities and are not relabeled as current acceptance.

The initial independent whole-branch review of `4d31fd56` found four blockers.
A fresh independent review of `2e628b27` verified their fixes and approved the
code conditional on the remaining acceptance gates. The selected Python SDK
preflight remains preserved before daemon traffic; its startup cost belongs
in the measurements. No maintainer retirement decision is implemented.

The first hosted Rust workflow attempt at `2e628b27` failed in new fixtures:
Linux ran 171/262 (169 passed, two Board retry failures); Windows ran 230/280
(227 passed, three updater-fixture failures). Parity jobs were skipped.
The original two-helper repairs have conditional independent code approval.
The final fixture adjustment requires fresh review and completion evidence;
final-head CI remains pending. The failed
attempt remains evidence and is not replaced by the local passing counts.

Candidate-routed committed
full-suite receipts on the authorized WSL/box paths, green final-head CI,
validation of any integrated fixture repairs
remain open. The close-out brief permits #546 to remain open and stacked. No Phase 1 parity row flips on inventory, component coverage or
these frozen judge receipts alone. Subsequent substantive PR updates require
fresh review of changed source and current-merge-ref CI.

The first two-helper validation passed Windows but failed one Linux case:
`board_retry_cancelled_attach_waits_out_attachment_busy` observed one attach
call instead of three. A controlled startup-before-attach schedule reproduced
that failure with the old 90 ms reply. Changing only that NEW fixture reply to
350 ms, beyond the existing 300 ms retry-attempt deadline, produced the required
one register, three attach and one detach while preserving the assertions.
The final Windows PATH-only target passes 23/23 with fmt/check/clippy exit 0.
Final Linux also passes 23/23 with fmt/check/clippy exit 0. Both final
controllers completed; cleanup verified zero owned processes on both platforms
at approximately 00:32:40Z (Windows) and 00:32:42Z (Linux). The earlier explicit absolute-selector updater
selection passed three cases on each platform. Fresh review of the final
fixture adjustment, committed full suites and final-head CI remain gates;
no green hosted CI or Phase 1 acceptance is claimed.

## Phase 1 paired measurement evidence

Paired measurements at `2e628b27` completed three repeats of ten samples per
arm on each platform. All 120 timing/RSS launches per platform exited cleanly.
The real Python SDK preflight remains inside Rust startup; its descendants
contribute to sampled process-tree RSS. Receipts are
`evals/results/rust-phase1-measurement-windows-2e628b27.json` and
`evals/results/rust-phase1-measurement-linux-2e628b27.json`.

| Platform | Metric | Python p50 / p95 | Rust p50 / p95 | Python floor p50 / p95 | Rust floor p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| Windows | First stdout frame, ms | 811.794 / 852.468 | 871.665 / 903.501 | 8.382 / 26.310 | 12.934 / 15.408 |
| Windows | Sampled peak tree RSS, bytes | 87,924,736 / 91,459,584 | 100,642,816 / 100,904,960 | 143,360 / 4,829,184 | 131,072 / 24,576 |
| Windows | Executable file, bytes | 274,712 / 274,712 | 13,487,616 / 13,487,616 | 0 / 0 | 0 / 0 |
| Linux | First stdout frame, ms | 704.398 / 724.289 | 686.487 / 699.918 | 2.012 / 113.563 | 6.224 / 33.229 |
| Linux | Sampled peak tree RSS, bytes | 73,633,792 / 73,879,552 | 74,027,008 / 74,166,272 | 53,248 / 135,168 | 57,344 / 36,864 |
| Linux | Executable file, bytes | 21,662,864 / 21,662,864 | 16,644,008 / 16,644,008 | 0 / 0 | 0 / 0 |

Floors are observed ranges of three identical-input repeat-block quantiles,
not confidence intervals. Windows Rust is slower and uses more sampled RSS.
Linux pooled latency is modestly lower; the p95 difference is within observed
floors, so it does not establish a p95 improvement. RSS is a sampled lower
bound and can miss peaks between 5 ms polls. Size covers only the selected
executable, excluding Python runtime/dependencies; it is not deployment footprint.
The pair uses warm filesystem caches and an empty loopback fixture, with no
PostgreSQL, models, daemon spawn or coordination. Desktop activity is uncontrolled.
Historical r5 uses a different timing/RSS boundary and cannot substitute for
this pair. These source-bound measurements establish no general speed claim
or Phase 1 acceptance.

## Phase 2 close-out item a

Merged actual `origin/master` at `f709abb54f7912ae9cd767998d0926ca33df4bcd` forward;
only `CHANGELOG.md` conflicted and both entries were preserved. PR #546
remains open and stacked, as the close-out brief permits; no upstream branch
from phase 0b was merged or rewritten. The current inventory audit accounts for 419 test
files (68 oracle, 2 candidate, 349 internal), 189 function mappings and 13
routed process nodes. Its 28 CLI modes include upstream `maintainer` and
`test-login`, deferred outside the original 26-mode phase 2 scope. Historical
phase 0b manifests and all historical receipts retain their pins.

Item a is complete: both inventory audits pass with no missing surfaces;
the inventory/provenance selection passes 56 tests, including unknown-reference
negative controls. Validation: `python rust/contract_inventory.py --phase1`,
`python rust/contract_inventory.py` and the targeted inventory/provenance files.
Next: apply the authorized
retirements and substitutions in item b, then run item c; no compilation,
full suite or receipt replay is claimed for this integration alone.
