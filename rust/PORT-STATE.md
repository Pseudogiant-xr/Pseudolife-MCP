# Rust port state

The version branch targets the current Python oracle at master
`eb0c13e9c5036aa2b95e7fccb77f41ca1c095493` (0.17.0, schema 55); that master commit is integrated locally, while final current-head acceptance remains pending.
Historical phase 1 close-out evidence retains
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
The current version-branch manifests enumerate 423 test files, all 191 current-pin function mappings in the nine scoped files and 15 routed nodes (10 public stdio and 5 CLI). The two new public refusal nodes are candidate mappings with pending runtime execution and acceptance; source-identity checks and adapter-routing unit checks do not close those gates. The #560 after-#546 merge gate remains separate; existing receipts retain their original pins, and the new target claims no acceptance.

The native version candidate and shared CLI fixture have fresh independent code approval at `62e4f590` and local release evidence on both platforms: 26 passing public cases and 104 rejected controls per OS, plus 240 paired timing samples and eight untimed controls across bare/installed layouts. Windows installed timing regressed beyond the observed floors; the full measurements retain that result. The [version evidence packet](../evals/results/rust-phase2b-version-62e4f590/README.md) preserves exact code/oracle/runtime/binary/helper identities and cleanup. Required hosted CI and independent review/checks of the evidence/documentation successor remain pending; CLI-VERSION stays deferred. Earlier 0.16.1 captures remain historical prototypes. Raw comparison receipts stay private and hosted artifacts use allowlisted projections.

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
| 1: Stdio shim | implementation accepted; current CI blocked | [#560](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/560) (draft) | Implementation acceptance at 8b7a6c95 is recorded in `evals/results/rust-phase1-closeout-8b7a6c95.json`. Measurements and four local judges retain their executed 690bb8ac identities. Current documentation head 7f890590 has two hosted 2025-11-25 real-bank `/stderr` mismatches; raw bytes and cause remain unresolved. #560 stays draft; current PR-head suites, integrated CI and final review must pass before it is marked ready. |
| 2: Client CLI leaves | Help/scoped unknown dispatch ported; remaining leaves deferred | — | All 28 modes classified; 19 requested leaves plus carried help/version/channel accounted for. Current native Windows/Linux receipts at 779c588c each pass 15 cases, 45 controls and five Python/five Rust nodes; both help 3x10 pairs and floors are below. Standard file locks replace fs2 with precise unused dependency pruning; source validation passes 277 Linux/296 Windows Rust tests. All four Rust/Parity jobs passed in run 37245992895 with actual CLI outcomes at same-tree merge checkout b5f485c9. Historical 6e936887 evidence retained. Scope is UTF-8 streams, valid Unicode scalar argv and platform newlines; version, other encodings and other modes stay deferred. Final documentation-head review/checks remain required. |
| 3: Daemon read path | deferred | — | HTTP/read/ranking rows and ONNX prerequisite in PARITY.md |
| 4: Daemon writes and background duties | deferred | — | Mutation/durability/dream/coordination/hook rows in PARITY.md |
| 5: Cutover and retirement | deferred | — | Maintainer owns merge/deploy and behavior retirement |

## Decisions and constraints

Current version evidence is bound to code `62e4f590`, oracle `eb0c13e9` (0.17.0,
schema 55), Windows Python 3.11.9 and Linux Python 3.11.15. Both local release
corpora cover five help, ten unknown-dispatch and eleven installer-schema version
cases, with owned database/daemon cleanup verified. Each OS/layout measurement
has three repeats of ten alternating paired samples per arm and two exact
untimed controls. The [packet](../evals/results/rust-phase2b-version-62e4f590/README.md)
records native/Python mode source ratio 172/125 = 1.376 excluding tests/assets,
the precise denominator and the Windows installed median/p95 regression.
[Rust run 37310594788](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37310594788)
passed both Rust jobs but failed both Parity jobs in a synthetic PYTHONPATH
fixture before the differential judges ran. A test-only seed collaborator fix
passes four targeted checks under ordinary and genuine-venv interpreters;
corrected hosted acceptance remains pending. CLI-VERSION remains deferred, and the
#546, #560 and #589 gates remain separate.

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
- ONNX-PREREQUISITE remains **deferred**. The [CPU receipt](../evals/results/rust-onnx-cpu-prerequisite-20d0e75d.json)
  binds committed instrument `20d0e75d`, the pinned stack, exported fp32 Qwen
  graph/tokenizer and 1,000 seeded texts plus 25 **PROPOSED** topic queries.
  Maximum absolute difference was `5.401670932769775e-7`, minimum cosine
  `0.9999999999933409`; full order matched for 5/25 queries and top eight for
  25/25 against the original 2k stored-vector definition. The stock ST ONNX
  wrapper fails on missing `position_ids`; direct ORT input/pooling is a
  numerical proof, not shipped-backend or daemon parity. The receipt preserves
  the exporter warning and per-query ranking hashes, disagreements and margins.
  Next: the maintainer identifies or accepts the query input and resolves the
  packaging/docs disagreement, then reviews the proposed, unaccepted numerical
  bounds in PORTING.md. The 25-case full-bank corpus is not 25 queries; no ID,
  rank-order, bit-identity or speed acceptance follows from this result.
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

## Current Phase 2 help and unknown dispatch evidence at `779c588c`

The [Windows CLI receipt](../evals/results/rust-port-phase2-cli-windows-779c588c.json)
and [native Linux CLI receipt](../evals/results/rust-port-phase2-cli-linux-native-779c588c.json)
retain executed source `779c588c8c96fe5265b3877bdb15f9d2942b0111`, tree
`ed24dba033ef74d399b6cb5493a76763978a506c`. Each passes Python self-replay and
Rust replay for 15 cases, rejects all 45 exit/stream mutations, and records five
complete passing original test outcomes per implementation. The fifth test
checks that help lists version; it does not invoke version. Capture runtimes are
genuine CPython 3.11.9 on Windows and 3.11.15 on Linux, package 0.16.1/MCP 2.1.1,
with production/tests checked against `f709abb54f7912ae9cd767998d0926ca33df4bcd`.
The accepted Linux run uses native temporary/evidence storage.

The lock implementation uses Rust 1.94 standard file lock/try-lock/unlock APIs,
preserving existing mapped errors and contention behavior. `Cargo.lock` removes
only `fs2` 0.4.3, `winapi` 0.3.9, `winapi-i686-pc-windows-gnu` 0.4.0 and
`winapi-x86_64-pc-windows-gnu` 0.4.0; `libc` remains and no other package is
added or updated. The stale-reservation lock contention/drop-cleanup regression
and complete Rust source validation passed: 277 Linux and 296 Windows tests,
zero skipped, plus locked all-target check and clippy on both OS. These Rust
test counts are source validation, separate from the five routed CLI nodes.

The [Windows help pair](../evals/results/rust-phase2-help-measurement-windows-779c588c.json)
and [Linux help pair](../evals/results/rust-phase2-help-measurement-linux-779c588c.json)
each retain three repeat blocks of ten samples per arm, alternating first arm,
two untimed byte controls and three resource checks. Every timed output was
byte-checked. Pooled nearest-rank p50/p95 use n=30; each floor is max minus min
of the three repeat-block quantiles, not a confidence interval.

| OS | Arm | p50 ms / floor ms | p95 ms / floor ms | Executable bytes |
| --- | --- | --- | --- | ---: |
| Windows | Python | 95.501 / 1.122 | 108.121 / 10.086 | 274,712 |
| Windows | Rust | 15.906 / 0.184 | 27.327 / 12.319 | 13,285,376 |
| Linux | Python | 123.309 / 2.307 | 132.736 / 4.626 | 21,662,864 |
| Linux | Rust | 1.288 / 0.301 | 1.633 / 0.518 | 16,757,408 |

Rust executable SHA256 is
`6dee57d77763d2f4f750cfdb1a480b0a71c3a0838239e54eff3dd3b925c41f98` on Windows
and `deb40db5dea608fa54955c9b533c7aa5adab54d021c32f0cd96b3f590184dd21` on Linux.
Executable size excludes interpreter dependencies. Timing includes process setup,
complete output collection and clean exit with warm filesystem caches; it is not
comparable to Phase 1 first-frame/initialize timing or historical r5. No causal
speedup or fixed performance threshold is asserted. Receipts retain their exact
executed identities; a source identity is not a build attestation.

[Rust workflow 37245992895](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37245992895)
passed all four executing jobs for PR head `779c588c`: Rust Ubuntu 2m23s,
Rust Windows 5m17s, Parity Ubuntu 5m52s and Parity Windows 15m21s. Both retained
CI CLI receipts independently record 15 passing cases, 45 rejected controls and
complete passing five-Python/five-Rust outcomes. Their actual checkout/instrument
commit is `b5f485c9ce1ae943b44f86d2661013a05988c7f6`, the synthetic PR merge
commit with exact tree `ed24dba033ef74d399b6cb5493a76763978a506c` matching the
local executed head. CI receipt SHA256 is
`6df550424f8d0a9b6ae6b4b6f5306bd1046af64b512f97ce7146c6baa73ed2b7` on Windows
and `cac498457bc943559ae36a34c60731800512d1a8ecdee98aae7df7320a492836` on Linux.
All nine reported PR checks passed at `779c588c`. The documentation successor
`6cbb6cac` was published and checked in [run 37248938171](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37248938171):
both Rust jobs and Windows Parity passed; Linux Parity failed on a real-bank
startup stderr mismatch, so its additive CLI lane was skipped. A focused
four-arm replay returned empty stderr for both Python and Rust in both eras
without reproducing or resolving the hosted cause. Current-head acceptance
remains blocked; the earlier green run is not a substitute.

Help and CLI-DISPATCH remain ported only for help aliases/trailing arguments and
the documented unknown-command cases with UTF-8 stdout/stderr, valid Unicode
scalar argv, Windows CRLF and Linux LF. Version identity, locale/default and
other output encodings, non-UTF-8/surrogate argv and every other mode remain
deferred. Historical `6e936887` evidence below stays unchanged. Final independent
review and current documentation-head checks remain required; these results
do not establish that the documentation successor was tested or mark #560 ready.

## Historical Phase 2 help and unknown dispatch evidence at `6e936887`

The [Windows CLI receipt](../evals/results/rust-port-phase2-cli-windows-6e936887.json)
and [native Linux CLI receipt](../evals/results/rust-port-phase2-cli-linux-native-6e936887.json)
retain the executed source `6e936887a05cd848679e6a0cf0b0b3d618f1e29c`, tree
`6649de0fce2440ac3026e633f6269fa03928fd18`. Each passes Python self-replay and
Rust replay for 15 cases, rejects all 45 exit/stream mutations, and records five
complete passing original test outcomes per implementation. The fifth test
checks that help lists version; it does not invoke version. Capture runtimes are
genuine CPython 3.11.9 on Windows and 3.11.15 on Linux, package 0.16.1/MCP 2.1.1,
with production/tests checked against `f709abb54f7912ae9cd767998d0926ca33df4bcd`.

These gates cover help aliases/trailing arguments and the documented unknown
command cases for UTF-8 stdout/stderr and valid Unicode scalar argv, preserving
Windows CRLF and Linux LF. Locale/default and other output encodings,
non-UTF-8/surrogate argv, version identity and other mode contracts stay deferred.
The initial Linux receipt had incomplete pytest collection on a mounted temporary
home; it is not accepted. Native Linux evidence-home placement passed without
changing source, runtime or binary. Raw logs/JUnit and the failed receipt remain
private; only the successful native receipt is linked here.

The [Windows help pair](../evals/results/rust-phase2-help-measurement-windows-6e936887.json)
and [Linux help pair](../evals/results/rust-phase2-help-measurement-linux-6e936887.json)
each retain three repeat blocks of ten samples per arm, alternating first arm,
two untimed byte controls and three resource checks. Every timed output was
byte-checked. Pooled nearest-rank p50/p95 use n=30; each floor is max minus min
of the three repeat-block quantiles, not a confidence interval.

| OS | Arm | p50 ms / floor ms | p95 ms / floor ms | Executable bytes |
| --- | --- | --- | --- | ---: |
| Windows | Python | 95.904 / 0.952 | 109.906 / 5.752 | 274,712 |
| Windows | Rust | 15.939 / 0.219 | 17.034 / 1.186 | 13,289,984 |
| Linux | Python | 123.234 / 0.339 | 130.363 / 9.578 | 21,662,864 |
| Linux | Rust | 1.323 / 0.273 | 1.530 / 0.701 | 16,751,968 |

Rust executable SHA256 is
`cc8ccddab9adfd6e83bd77c209cc1892d8f309c61543787ff9e6423b92e24503` on Windows
and `577c44a2100cd67938f11d1ae446ca04fe990bc7cd1fe56263449731fe99ce11` on Linux.
Executable size excludes interpreter dependencies. Timing includes process setup,
complete output collection and clean exit with warm filesystem caches; it is not
comparable to Phase 1 first-frame/initialize timing or historical r5. No causal
speedup or fixed performance threshold is asserted.

The subsequent merge `cdcca248` integrates only reviewed Phase 1 documentation
and acceptance data from `7f890590`; Rust/Python source, fixtures, instruments
and workflow are unchanged from the executed `6e936887`. These records keep
their original source/tree/runtime/binary identities.

[Rust workflow 37242071017](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37242071017)
passed all four executing jobs for PR head `6e936887`: Rust Ubuntu 2m12s,
Rust Windows 9m38s, Parity Ubuntu 7m17s and Parity Windows 13m57s. Both
`Run unchanged candidates and differential judges` steps executed successfully.
The retained `rust-parity-Windows` and `rust-parity-Linux` CLI receipts each
independently record 15 passing cases, 45 rejected controls and complete passing
five-Python/five-Rust outcomes. Their actual checkout/instrument commit is
`014d37dc2aafa037f28d01be959f206ac391ca37`, the PR merge commit with the exact
same tree `6649de0fce2440ac3026e633f6269fa03928fd18` as the local executed head.
CI receipt SHA256 is
`33efcbb83ad604248fae6a1c638176e47ad2e11da4f2e44d3f08ccbdc2f0644d` on Windows
and `5a06ae45ecf17f0003b2ab3b3429df56ca9b43171eabb3a7c759e880b075a7e1` on Linux.

Help and CLI-DISPATCH are ported only for the UTF-8/scalar/platform-newline
slice described above. Version and every other mode remain deferred. Final
documentation-head review, suites and current CI/checks remain separate;
implementation-head CI does not establish that the docs successor was tested.
