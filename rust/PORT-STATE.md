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
Phase 1 implementation acceptance at `8b7a6c95` is recorded in the separate close-out ledger below; current documentation PR-head gates remain required before ready status.

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
| 1: Stdio shim | ready-for-review | [#560](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/560) (draft) | Implementation acceptance at 8b7a6c95 is recorded in `evals/results/rust-phase1-closeout-8b7a6c95.json`. Measurements and four local judges retain their executed 690bb8ac identities. Current PR-head suites, integrated CI and final review must pass before #560 is marked ready. |
| 2: Client CLI leaves | deferred | — | CLI rows and 26-mode checklist in PARITY.md |
| 3: Daemon read path | deferred | — | HTTP/read/ranking rows and ONNX prerequisite in PARITY.md |
| 4: Daemon writes and background duties | deferred | — | Mutation/durability/dream/coordination/hook rows in PARITY.md |
| 5: Cutover and retirement | deferred | — | Maintainer owns merge/deploy and behavior retirement |

## Decisions and constraints

- Phase 1 starts from master `0b015f92` on `codex/rust-phase1`.
  PR #546 was still open at branch creation; its branch head `31f8475e`
  was merged into the phase 1 branch at `93012944`, as the phase 1 brief
  permits. PR #546 and its branch are unchanged. The maintainer owns its merge.
- The historical phase 1 window ended at 2026-10-04 12:30 AEDT (01:30 UTC).
  The close-out window ends at 2026-10-05 13:00 AEDT (02:00 UTC);
  incomplete acceptance retains explicit pending evidence and a draft PR.
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
- Historically, `2e628b27` named targeted Rust equivalents for all 125 scoped
  internal cases. The current mapping has 121 completed equivalents, 3 SDK
  retirements and 1 pending postframe substitution with targeted evidence on
  both platforms; 55 other internal cases remain outside Phase 1's scope.
  Inventory and component coverage alone do not close production behavior rows.
- The legacy MCP negotiation evidence from #540 is about its original pin;
  current and earlier protocol revisions require separate current-pin evidence.

## Historical phase 1 evidence

Phase 1 close-out uses Python 0.16.1/schema 54 at `f709abb54f7912ae9cd767998d0926ca33df4bcd`
and the stacked phase 0b branch above. PR #546 remains open and unmerged;
PR #560 remains a draft. Broader production behavior rows remain deferred; named close-out decisions are recorded below.

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
code conditional on the remaining acceptance gates. That historical candidate
preserved the Python SDK preflight before daemon traffic, and its startup cost
belongs in the historical measurements. Close-out item b below implements the
subsequent authorized retirement and substitutions.

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

## Historical phase 1 paired measurement evidence

Paired measurements at `2e628b27` completed three repeats of ten samples per
arm on each platform. All 120 timing/RSS launches per platform exited cleanly.
That historical candidate includes the real Python SDK preflight inside Rust
startup; its descendants contribute to sampled process-tree RSS. Receipts are
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
not confidence intervals. Each arm retains its own source, runtime and
descriptive floors. RSS is a sampled lower
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
Item b implementation evidence follows; current close-out status is recorded
in the final section. Item a alone establishes no full suite or receipt replay.

## Phase 2 close-out item b

The authorized SDK preflight and wake-reason table retirements and explicit
daemon-spawn and postframe-update substitutions are implemented over integrated
HEAD `7d0ee5997acb4c74196a0d46fa9f2fc20aa3e860`. Exactly three SDK mapping rows
are retired-by-decision. The update-scheduling row stays pending with its
proposed Rust target and current Windows/Linux targeted evidence; the other
121 completed equivalents retain their recorded component evidence.
The harness and external adapter explicitly bind the selected Python runtime,
and the Parity workflow selects its prepared oracle interpreter.

Frozen tree `a4ff3236eb349aaed427d80129513fe22cf0183f` passed Windows and Linux
all-target check/clippy and 71 affected Rust cases (66 integration, 5 wire).
Windows evidence is `closeout-check-windows.log`, `closeout-clippy-windows.log`,
`closeout-targeted-green-windows.log`, `closeout-final-assertions-green-windows.log`
and `closeout-wire-windows.log`. The aggregate Windows run retained a transient
ConnectionReset; its exact rerun and the full nine-test final-assertions file
passed, so no clean aggregate-run claim is made. Linux evidence is
`closeout-check-linux.log`, `closeout-clippy-linux.log`, `closeout-targeted-linux.log`
and `closeout-wire-linux.log`, each with a saved exit 0. These private logs bind
to the frozen tree; they do not validate later changes. Both platforms reported
zero owned candidates after cleanup.

The two interim review findings were repaired in the interpreter bindings and
evidence descriptions. This intermediate component validation did not close
items c-g. The current final section records later CI, receipts, measurements
and remaining acceptance gates; no acceptance follows from these logs alone.

## Current close-out evidence at `690bb8ac`

The candidate and committed instrument were frozen at `690bb8acd855250c29145d623a039ddcd4dee6cb`,
tree `9116cc4c98e79e947322fe2772476e255b73f033`, over the Python
0.16.1/schema-54 oracle `f709abb54f7912ae9cd767998d0926ca33df4bcd`.
The Windows and native Linux Python self-replays and Rust judges each pass
all eight actual stdio subprocess nodes with zero retained-wire differences.
Actual JUnit per-node outcomes and its digest are recorded; exit zero alone
is insufficient. Each replay binds 32 candidate cells: two real-bank, seven
EOF, ten fault, seven startup and six concurrent non-EOF cells. All sections,
controls, owned processes, disposable banks and outer PostgreSQL cleanup were
checked. The executable/source hashes identify the actual bytes; they are
not a build attestation. Windows and Linux retain their actual raw checkout
hashes, including platform line endings.

Current combined receipts are
`evals/results/rust-port-phase1-python-windows-690bb8ac.json`,
`evals/results/rust-port-phase1-python-linux-690bb8ac.json`,
`evals/results/rust-port-phase1-rust-windows-690bb8ac.json` and
`evals/results/rust-port-phase1-rust-linux-690bb8ac.json`.
The separate five dispatcher nodes execute the Python public CLI on each OS,
as recorded in `rust-port-phase1-cli-python-windows-690bb8ac.json` and
`rust-port-phase1-cli-python-linux-690bb8ac.json` under `evals/results/`.
Those five passes are not Rust CLI coverage and are not added to the eight
stdio passes. Runtime metadata is CPython 3.11.9 on Windows and 3.11.15 on Linux,
MCP 2.1.1, with actual package and distribution version 0.16.1.

[The Rust workflow run](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37233810056)
completed successfully at `690bb8ac`; both Parity jobs executed.

| Job | Platform | Wall time |
| --- | --- | --- |
| [Rust](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37233810056/job/111528841770) | Ubuntu | 2m 11s |
| [Rust](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37233810056/job/111528841590) | Windows | 4m 34s |
| [Parity](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37233810056/job/111529715899) | Ubuntu | 6m 54s |
| [Parity](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37233810056/job/111529715959) | Windows | 15m 12s |

The brief's 2.1 close-out status is:

| Item | Status | Evidence |
| --- | --- | --- |
| a: Master integration and inventory | Complete | Python oracle f709abb5/schema 54; both inventory audits; 419 test files, 189 function mappings, 13 routed nodes |
| b: Named retirements and substitutions | Implemented and validated; scoped decisions recorded | SDK preflight retired; explicit daemon spawn, postframe update scheduling and Rust wake-reason predicates named in PARITY.md and PORTING.md; both-platform targeted checks and executing CI |
| c: Executing Rust and Parity CI | Complete at 690bb8ac | Run 37233810056, all four jobs successful; wall times above |
| d: Actual outcomes, source/binary bindings, self-replay and pruning | Complete at 690bb8ac | Four schema-2 combined receipts; eight actual nodes each, 32 bound cells each, all sections and cleanup checked; one current replay kind/platform; two named historical strict-judge receipts retained |
| e: Post-retirement paired measurement | Complete at 690bb8ac | Both platform receipts below, three repeats of ten samples per arm; all normal and separate RSS launches clean; canonical historical LF artifact binding |
| f: Small review items | Implemented and validated at 690bb8ac | Close-race built-empty assertion; doctor oracle subprocess classification; local unsafe allowances; lazy launcher scan; Python-first observed concurrent-order contract; seven exact startup stderr cells and executing CI |
| g: Final acceptance | Implementation evidence complete at 8b7a6c95; current documentation PR-head gates pending | Both candidate-routed committed full suites, executing green integrated CI and the reviewed production-equivalent source chain are recorded in the separate close-out ledger below; current PR-head suites, CI and final independent review remain required before #560 is marked ready |

## Current post-retirement paired measurements

The two current receipts are
`evals/results/rust-phase1-measurement-windows-690bb8ac.json` and
`evals/results/rust-phase1-measurement-linux-690bb8ac.json`.
Each contains three identical-input repeats of ten samples per arm, with
alternating Python/Rust timed launches and separate RSS launches: 60 timed
and 60 RSS launches per platform. Every normal and RSS cleanup has exit zero,
no forced termination and confirmed subtree cleanup. The source, instrument,
executable and actual Python runtime bindings were checked for each platform.

| Platform | Metric | Python p50 / p95 | Rust p50 / p95 | Python floor p50 / p95 | Rust floor p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| Windows | First stdout frame, ms | 812.083 / 860.987 | 25.992 / 27.217 | 16.399 / 38.308 | 0.755 / 1.414 |
| Windows | Sampled peak tree RSS, bytes | 87,785,472 / 91,738,112 | 17,547,264 / 22,208,512 | 282,624 / 4,599,808 | 1,724,416 / 749,568 |
| Windows | Executable file, bytes | 274,712 / 274,712 | 13,293,056 / 13,293,056 | 0 / 0 | 0 / 0 |
| Linux | First stdout frame, ms | 668.437 / 695.709 | 5.888 / 6.548 | 12.771 / 16.679 | 0.132 / 0.456 |
| Linux | Sampled peak tree RSS, bytes | 73,904,128 / 74,223,616 | 11,649,024 / 11,845,632 | 221,184 / 172,032 | 196,608 / 65,536 |
| Linux | Executable file, bytes | 21,662,864 / 21,662,864 | 16,742,560 / 16,742,560 | 0 / 0 | 0 / 0 |

The reported quantiles pool 30 samples per arm and use nearest rank.
Each arm's floor is the range of its three repeat-block quantiles, computed
separately for p50 and p95; these are descriptive ranges, not confidence
intervals. With ten samples, each repeat-block p95 is its maximum. First-frame
latency observes the first complete stdout line before JSON parsing. RSS is a
sampled lower bound from a separate launch at 5 ms polls; executable size
excludes Python runtime and dependencies. The fixture uses warm filesystem
cache, an empty authenticated loopback server, no daemon spawn and disabled
coordination; ambient desktop activity and cache evolution are uncontrolled.

The before-retirement numeric table above retains its original source/runtime
bindings. The Python source/schema/runtime and other Rust close-out behavior
changed between captures; their difference is not an isolated estimate of SDK
retirement. Historical r5 remains noncomparable: its Windows Python
initialize-return p50 is 644.687 ms, versus 811.841 ms in the earlier paired
capture, whose first-frame p50 is 811.794 ms. Source pins, versions,
dependencies, instrumentation and process ownership differ, and no matched
crossover isolates the cause. The canonical LF r5 Git blob
`a7ddf073f0e8102cecdc2947bffc0b2f546151cd` has SHA256
`3d29fcba85601c4647208da7448e85245861af16671ec22a33c9393c43fac581`.
The historical artifact is unchanged; checkout CRLF bytes do not replace this
canonical reference. No general performance claim or completed Phase 1
acceptance follows from these tables.

## Phase 1 implementation acceptance at `8b7a6c95`

The [separate acceptance ledger](../evals/results/rust-phase1-closeout-8b7a6c95.json)
records the tested implementation tree, source chain, artifact digests, actual
suite routing/outcomes, cleanup and current ready-status gates. The
`phase1-test-buckets.json` acceptance fields remain the frozen pre-acceptance
inventory mapping; this ledger records final implementation acceptance.
MCP-TIER acceptance covers the client boundary only; daemon tier enforcement
remains Phase 3 deferred. The four exact channel remainder nodes stay deferred
to Phase 2 in the ledger; the named SDK retirement and three substitutions remain.

| Suite host label | Tested checkout | Passed / skipped | Pytest seconds | Mapped nodes passed once |
| --- | --- | --- | --- | --- |
| linux-wsl | 8b7a6c95 | 17,950 / 81 | 1,681.36 | 8 Rust stdio; 5 Python dispatcher |
| linux-box | 8b7a6c95 | 17,955 / 76 | 1,689.51 | 8 Rust stdio; 5 Python dispatcher |

Both suites execute the native ELF built at `690bb8ac`, with its original hash
and build-source identity. `d3022bcc` only promotes documentation/data;
`8b7a6c95` changes one independently reviewed Windows `cfg(test)` assertion.
No production or executable instrument bytes changed in that source chain.
Both exact owned process/database censuses are empty; staged copies are removed
and native locks are free. Lease board verification was unavailable.

[Integrated Rust workflow 37238403052](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37238403052)
passed all four executing jobs at `8b7a6c95`: Rust Ubuntu 2m12s, Rust Windows
4m41s, Parity Ubuntu 4m05s and Parity Windows 9m56s. All fourteen PR checks
passed at that implementation head. The whole-closeout draft review approved
`d3022bcc`; the sole subsequent fixture assertion has independent bounded
approval. Final review of this acceptance record and evidence followup remains
required. Current documentation PR-head suites and integrated CI must pass
before #560 is marked ready; these earlier results do not claim the dirty
documentation head has been tested. Measurements and all four local judges
retain their executed `690bb8ac` identities.

## Phase 2b item b: truthful startup and nonfatal command configuration

The lifecycle fix and startup regressions are integrated from frozen tree
`a83878bfa389c0e009d18a3c6d28387267fb1fb5`. Missing configuration no longer claims
that the caller set `PSEUDOLIFE_MCP_NO_SPAWN`; malformed explicit serve argv emits
one named note before health and follows the no-spawn path. Healthy unconfigured
daemons retain silent startup, and the dead `spawn_daemon` helper is removed.

[Local proof summary](phase2b-fixes-evidence.json) binds the original source/test
blobs and retained watched-RED logs `fixes-red-b.log` (two failures) and
`fixes-red-b-healthy-silence.log` (the caught healthy-startup regression). Final
isolated Windows startup checks passed 12/12. This closes implementation item b;
final integrated-head CI, suites and review remain pending. No later head is
claimed tested by those isolated runs.

## Phase 2b item c: restored Unicode 14 wake reasons

The committed Python generator and Rust full-range contract hash two predicate
bytes per code point from U+0000 through U+10FFFF, including surrogates, to
`f4a77aec4b67a7770e67e22fe029b97d93300d0eafcf13b0968a885d2b9ff2fe`. The
CPython 3.11 / Unicode 14 table preserves L* and N* alphanumeric categories and
Python whitespace, including U+001C–001F. Existing claim tables remain intact;
`reason14.rs` stays deleted.

[Local proof summary](phase2b-fixes-evidence.json) retains watched RED
`fixes-red-bcd.log`: the separator and full-range hash both failed before the
fix. Both restored contract tests passed in the isolated Windows targeted run.
Implementation item c is closed; the row's earlier Rust-predicate substitution
is withdrawn, with final integrated-head acceptance still pending before a
`ported` status.

## Phase 2b item d: invalid OS arguments

`main.rs` now reads `args_os`; an invalid-Unicode mode uses Python-compatible
unknown-mode repr and exit 2 instead of panicking. Valid shim/channel argument
behavior is unchanged. One corpus case is defined for each OS: invalid UTF-8
with Unix surrogateescape, and an unpaired UTF-16 surrogate on Windows.

[Local proof summary](phase2b-fixes-evidence.json) binds watched RED
`fixes-red-d.log` (Windows exit 101 instead of 2) and the isolated Windows
GREEN case. Implementation item d is closed. The Unix corpus is implemented
but unexecuted locally; current-head both-platform validation remains pending.
The broader Phase 2 CLI dispatcher is not part of this item.

## Phase 2b item e: bounded fixtures and updater callback proof

The two update-health accept/join paths now share a cancellable nonblocking
fixture with a five-second accept deadline, bounded header reads/writes and an
8 KiB header cap. The Windows doorbell test preserves unsuccessful descendant
status and restores its `!status.success()` assertion. Nextest uses 60-second
slow periods and terminates after two; Parity install/build/judge steps are
bounded at 10/10/15 minutes within the unchanged 25-minute job.

The built binary sends its first flushed client frame before a disposable
explicit-interpreter updater writes its sentinel. The test observes no update
at upstream startup, then pins callback argv and result after the client frame.
It makes no latency assertion and performs no client installation.

[Local proof summary](phase2b-fixes-evidence.json) binds watched RED
`fixes-red-e-bounds.log` and `fixes-red-e-doorbell.log`, plus the static baseline
configuration RED `fixes-red-e-timeouts.log` (not a runtime timeout trial). The
isolated Windows targeted run passed the updater sentinel and cancellation
checks; the native doorbell group passed 10/10. The callback proof is positive
only: production callback wiring was already correct and is unchanged.
Implementation item e is closed; final integrated-head validation is pending.
