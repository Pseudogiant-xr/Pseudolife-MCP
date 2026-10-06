# Rust port state

The version branch targets the current Python oracle at master
`3c01bb31abd60178e15dea99adda369b4bbf92fc` (0.17.0, schema 55). Scoped CLI-VERSION is ported: the [final both-OS CPU proof](../evals/results/rust-phase2d-version-df2dbf8a/README.md) executed merged master `df2dbf8a`, tree `28823784`, after #608 and #600 merged. Its exact source/image identities remain separate from this documentation carrier, whose independent review and hosted checks remain required. Historical `5220b5ee` and earlier receipts retain their identities and failures.
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
The prepared version-branch manifests enumerate 423 test files, all 191 current-pin function mappings in the nine scoped files and 18 routed nodes (10 public stdio and 8 CLI). The historical `74b5998c` ten-stdio attempt passes five and fails five in both arms with shared HTTP 500 fixture errors; it does not isolate a Rust failure. Only an unchanged Python idle-gap diagnostic established missing offline model files; the other four shared failures were not independently diagnosed. Published `95d5402d` hosted Parity passes all 18 candidate nodes per OS; historical `5220b5ee` CLI captures cover the subsequent console/warm changes; final merged-master `df2dbf8a` proof now supplies scoped CLI-VERSION acceptance below. The evidence carrier retains its own review/check gates. Historical ac0c64a1 outcomes do not prove acceptance. Phase 1 source at a3e95642 is integrated and #560 is merged; its eight-node full-suite receipts retain their own source and ELF bindings.

Historical version candidate and shared CLI fixture evidence at `62e4f590` includes independent code approval and local release evidence on both platforms: 26 passing public cases and 104 rejected controls per OS, plus 240 paired timing samples and eight untimed controls across bare/installed layouts. Windows installed timing regressed beyond the observed floors; the full measurements retain that result. The [version evidence packet](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2b-version-62e4f590/README.md) preserves exact code/oracle/runtime/binary/helper identities and cleanup. Required hosted CI and independent review/checks of the evidence/documentation successor remain pending; CLI-VERSION stays deferred. Earlier 0.16.1 captures remain historical prototypes. Raw comparison receipts stay private and hosted artifacts use allowlisted projections.

The Linux release corpus's fifteen help/unknown-dispatch oracle children invoked
the base interpreter after fixture symlink resolution. Its 26 byte matches and
104 rejected controls remain recorded facts, but those fifteen rows do not prove
parity against the admitted virtualenv. Capture-runtime metadata identifies the
producer, not every child. The eleven version rows, Windows corpus, all four
timing cells and both current startup captures are unaffected. Original receipts
remain intact; the bounded fifteen-row follow-up below records the repaired
Linux invocation evidence.

The fixture backports lexical invocation from `f4bf8a83` and closes its reviewed
original-target gap: both original and selected executable targets remain bound
through preparation and capture, including when an owned copy is selected.
Targeted checks pass on Linux (127) and Windows (120, with seven Unix-only skips).
Independent scoped review approved the repair at clean `804e829e`; the
[bounded Linux follow-up](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2b-version-804e829e/README.md)
passes fifteen actual pairs (five help, ten unknown dispatch) and rejects all
sixty candidate output/file-state controls. Its measured instrument is
`804e829ebb2d0f1e9c80894959420bc814aeaac3`, tree
`297f037211e648d69d1d5a3353750ff2369fedea`, over the current 0.17.0/schema-55
oracle with CPython 3.11.15. All fifteen oracle invocations retain the admitted
lexical virtualenv path; a separate same-fixture child probe confirms that
virtualenv context. The retained ELF is the source-equivalent `62e4f590` build.
Historical receipts remain unchanged. This is not a new complete twenty-six-row
gate; CLI-VERSION, required hosted CI/evidence-successor review, the separate
merge gates and the unapproved derived-name substitution policy remain unchanged.

A subsequent disposable-child probe reproduced a fixture receipt defect: a
callback could change the captured environment after launch, and the returned
receipt aliased that mutable dictionary. Observation now retains an independent
launch snapshot and refuses whole-environment changes after capture and poststate
collection. Eleven watched regressions fail before the repair on each platform;
the repaired focused and compatibility checks pass 138 tests on Linux and 131
tests with seven Unix-only skips on Windows. The three-value preparation API and
original/selected executable checks remain intact. This fixture repair does not
rerun or replace the historical parity and timing receipts, and does not close
the outstanding hosted CI or CLI-VERSION acceptance gates.

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
| 1: Stdio shim | merged; final checks passed | [#560](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/560), merged | Merge `465d75d9` retains validated `a3e95642`, all 14 passing checks and its two-machine eight-route suite/source/ELF bindings. Implementation acceptance at `8b7a6c95` and measurements/judges at `690bb8ac` retain their original identities. The historical documentation head `7f890590` had two hosted real-bank `/stderr` mismatches; those failures and their unresolved cause remain retained. Phase 1 acceptance does not verify the version branch's 18 admissions. |
| 2: Client CLI leaves | Help/scoped unknown dispatch and version ported; remaining leaves deferred | — | All 28 modes classified; 19 requested leaves plus carried help/version/channel accounted for. Current native Windows/Linux receipts at 779c588c each pass 15 cases, 45 controls and five Python/five Rust nodes; both help 3x10 pairs and floors are below. Standard file locks replace fs2 with precise unused dependency pruning; source validation passes 277 Linux/296 Windows Rust tests. All four Rust/Parity jobs passed in run 37245992895 with actual CLI outcomes at same-tree merge checkout b5f485c9. Historical 6e936887 evidence retained. Scope is UTF-8 streams, valid Unicode scalar argv and platform newlines; other encodings and remaining modes stay deferred. Scoped CLI-VERSION acceptance at `df2dbf8a` is recorded below. Final documentation-head review/checks remain required. |
| 3: Daemon read path | deferred | — | HTTP/read/ranking rows and ONNX prerequisite in PARITY.md |
| 4: Daemon writes and background duties | deferred | — | Mutation/durability/dream/coordination/hook rows in PARITY.md |
| 5: Cutover and retirement | deferred | — | Maintainer owns merge/deploy and behavior retirement |

## Historical Phase 2c publication snapshot

Historical snapshot: 2026-10-06 08:57 UTC. These rows retain the observed state at that time, with subsequent #560 merge disposition already recorded. Its cleanup repair later passed hosted checks and both candidate-routed suites. Current version state is recorded below; a run validates only its recorded head.

| Branch | PR | Published head | Base | Rust workflow run / status | Next action |
|---|---|---|---|---|---|
| `codex/rust-phase1` | [#560](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/560), merged | `465d75d9` merge; validated `a3e95642` | `master` @ `3c01bb31` | #37443932505: Rust and Parity green on both OSes at `a3e95642` | Merged Phase 1 retains its own eight-route full-suite/source/ELF bindings; it does not verify the version branch's 18 current admissions. |
| `codex/rust-phase2b-version` | [#600](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/600), draft | `db0d59cb` published; `74b5998c` measured | `master` after #560 | No hosted run on `74b5998c`; historical #37315503446 failed at `ac0c64a1` | Freeze the documentation/evidence successor, repeat required final-head cells on both OSes, then require hosted validation and independent review. The current ten-stdio attempt fails five shared fixture nodes in both arms. |
| `codex/rust-phase2b-wait-mail` | [#601](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/601), draft | `9861ced9` | `codex/rust-phase2b-version` | #37428816392: both Rust jobs failed; Parity skipped | Carry the help-fixture LF correction and shared Linux cleanup repair, then rebase after version is ported and recapture. |
| `codex/rust-phase2b-lease` | [#602](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/602), draft | `a6cdf024` | `codex/rust-phase2b-version` | #37428908416: Rust green, Parity red on both OSes | Apply the named policies; carry registration and Windows path-admission repairs after version is ported, then capture final-head mode evidence. |
| `codex/rust-phase2b-episode` | [#603](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/603), draft | `267ee9a2` | `codex/rust-phase2b-version` | #37429259333: Rust green, Parity red on both OSes | Apply clock/header rules; carry shared repairs after version is ported, then capture final-head mode evidence. |
| `codex/rust-phase2b-briefing-hook` | [#604](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/604), draft | `7171d6cc` | `codex/rust-phase2b-version` | #37429570773: Rust green, Parity red on both OSes | Carry shared repairs after version is ported; investigate the additional unretained normal-transcript stderr difference with final-head raw evidence. |
| `codex/rust-phase2b-doorbell-seen` | [#605](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/605), draft | `2c026331` | `codex/rust-phase2b-version` | #37429996211: Rust green, Parity red on both OSes | Apply traceback, clock and temporary-name rules; carry shared repairs after version is ported, then rebind mode evidence. |
| `codex/rust-phase2b-fixture` | [#606](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/606), draft | `1ff47a1a` | `codex/rust-phase2` @ `c9705e88` | #37430021881: Rust and Parity green on both OSes | Preserve #589 until its dependent fixture and evidence split is complete. |
| `codex/rust-phase2b-fixes` | Folded into #560; redundant worktree archived | `7f890590` (local ref) | `master` | Historical #37242617729 failed before folding | Validate the surviving fixes through #560; no redundant PR. |
| `codex/rust-phase2` | [#589](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/589), draft | `c9705e88` | `codex/rust-phase1` | Historical #37253327797 succeeded | Preserve until the dependent fixture and evidence split is complete. |
| `codex/rust-phase2c-users` | [#607](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/607), draft | `43e05ac1` | `codex/rust-phase1` | #37438008142 in progress | Complete hosted checks for the independently reviewed users-only removal. |

## Decisions and constraints

Historical version evidence is bound to code `62e4f590`, oracle `eb0c13e9` (0.17.0,
schema 55), Windows Python 3.11.9 and Linux Python 3.11.15. Both local release
corpora cover five help, ten unknown-dispatch and eleven installer-schema version
cases, with owned database/daemon cleanup verified. Each OS/layout measurement
has three repeats of ten alternating paired samples per arm and two exact
untimed controls. The [packet](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2b-version-62e4f590/README.md)
records native/Python mode source ratio 172/125 = 1.376 excluding tests/assets,
the precise denominator and the Windows installed median/p95 regression.
[Rust run 37310594788](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37310594788)
passed both Rust jobs but failed both Parity jobs in a synthetic PYTHONPATH
fixture before the differential judges ran. A test-only seed collaborator fix
passes four targeted checks under ordinary and genuine-venv interpreters;
corrected hosted acceptance remains pending. CLI-VERSION remains deferred, and the
#546, #560 and #589 gates remain separate.

[Rust run 37315503446](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37315503446)
at `ac0c64a1` passed both Rust jobs and 151 CLI fixture checks per platform.
Both Parity jobs then failed the Phase 1 judge with six differences each:
two stale 0.16.1 startup stderr expectations against both 0.17.0 arms, and
four generated registration-name differences between Python replay arms.
All ten selected public stdio nodes passed per platform; the later version
judge was skipped. The new [startup proof](../evals/results/rust-phase2b-startup-eb0c13e9/windows.json)
and [Linux startup proof](../evals/results/rust-phase2b-startup-eb0c13e9/linux.json) bind a
literal 0.17.0 contract to complete storage-free captures, with the frozen
f709/0.16.1 contract retained separately. Derived-name identity substitution
remains unapproved and its comparison policy is unchanged. Hosted acceptance
and fresh independent review of the repair remain pending; CLI-VERSION stays
deferred. The earlier run and local measurement records above remain historical.

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
- ONNX-PREREQUISITE remains **deferred**. The [CPU receipt](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/13dbe0b032527bf110acbaf4f1ea1ae00ca20cef/evals/results/rust-onnx-cpu-prerequisite-20d0e75d.json)
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

The [Windows CLI receipt](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-windows-779c588c.json)
and [native Linux CLI receipt](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-linux-native-779c588c.json)
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

The [Windows help pair](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2-help-measurement-windows-779c588c.json)
and [Linux help pair](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2-help-measurement-linux-779c588c.json)
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

The [Windows CLI receipt](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-windows-6e936887.json)
and [native Linux CLI receipt](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-linux-native-6e936887.json)
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

The [Windows help pair](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2-help-measurement-windows-6e936887.json)
and [Linux help pair](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2-help-measurement-linux-6e936887.json)
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


## Historical prepared version integration at the current Python pin

The version identity checkpoint `9e7ba302` is integrated with the Phase 1
source at `a3e95642`. The current Python oracle is `3c01bb31`, package 0.17.0,
schema 55. Mapping retains 18 routes: ten stdio and eight CLI, including the
three version admissions. The 191 scoped functions remain classified as ten
candidate, one oracle and 180 internal. In the historical prepared state, the
two refusal routes had not executed. At `74b5998c` both ran once/non-skipped
per arm on native Linux: tool refusal failed in both arms; unknown-parameter
refusal passed in both. At published `95d5402d`, both passed once/non-skipped in each OS candidate lane; no Python ten-stdio original lane was reported. Later combined-head acceptance remains pending.

The prepared native identity checks its own executable beneath a six-digit
runtime entry's `Scripts` or `bin`, derives the runtime root and matches the
runtime path as written or canonicalized. Strict manifest limitations include
NaN, Infinity, lone surrogates and integers beyond u64. The fixture admission
preserves canonical containment, existence and symlink checks, including
Windows short/long spellings of the same owned file.

Historical/provisional Windows release proof captured at `74b5998c` passes
the original compiled contracts with both feature settings, 26 exact CLI cases,
104 rejected controls and all eight routed CLI nodes. The admitted Python
runtime is genuine 3.11.9 at the current pin. The Linux release corpus passes
26 cases and 104 controls; its eight CLI nodes pass once/non-skipped in both
arms. At that prepared checkpoint, final paired measurements had not launched and
were required after the documentation freeze. The historical ten-stdio attempt passes five and fails
five in both arms with shared HTTP 500 fixture errors. An unchanged Python
idle-gap diagnostic found missing offline model files. These provisional
captures do not support a ported row. The implementation, instrument and documentation freeze with this
packet; subsequent captured-result additions cite that frozen validation head.
At that checkpoint, actual final-head invocations and paired measurements on
both OSes, hosted validation and independent review remained required. The
bee4 capture below completes the CLI invocations and measurements; hosted
validation and independent review remain pending, so version stays deferred.

The provisional Windows paired installed median at `74b5998c` is 288.004 ms against Python
178.390 ms, a 109.614 ms regression beyond the 14.118 ms observed median floor;
p95 is 324.564 versus 200.897 ms, beyond the 12.753 ms p95 floor. With the same
current image, installed home and manifest, fresh help and version both cost
284–288 ms, while immediate reuse costs 22–24 ms. Most of the fresh-copy
penalty occurs in launch and process ownership setup before output collection.
This records a fresh-image startup/cache condition affecting both commands;
it does not attribute a security provider or dismiss the regression as noise.
The historical 278/156 ms finding and measurements remain intact.

Historical captured native/Python mode source ratio is 176/125 = 1.408: 163 physical Rust
lines before `#[cfg(test)]` plus 13 net dispatcher lines versus `c9705e88`,
against 223 Python CLI module lines minus the 98-line AST `_USAGE` asset.
Counts include comments/blanks and exclude tests, assets and shared runtime
support, preserving the historical denominator definition. The provisional
[Windows/Linux packet](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2c-version-74b5998c/README.md)
retains the actual capture identities and final-head limitations.


## Historical frozen-head version capture at bee4fe02

The [historical CLI capture packet](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2c-version-bee4fe02/README.md)
records actual invocations at `bee4fe0269fe31069f24cf8ddcf122a908e0ead8`
on Windows and Linux, with genuine Python 3.11/package 0.17.0 at `3c01bb31`.
Both pass the installed smoke, 26 exact CLI cases, 104 rejected controls,
native invalid argv and all eight CLI nodes once/non-skipped per arm.
Both original 3x10 bare and installed measurements are complete. Windows
installed Rust/Python medians are 286.480/176.449 ms: the 110.030 ms regression
exceeds the 11.695 ms larger observed floor. Linux bare and installed Rust
medians are 2.581/3.110 ms against Python 126.947/599.200 ms; descriptive
floors and p95 values are retained in the packet. The source ratio remains
176/125 = 1.408. Historical captures, raw failures and fresh-copy diagnostics
remain intact. Final-head stdio/current both-feature hosted CI and independent
review remain pending, so version remains deferred. This result/status
successor carries the executed `bee4fe02` receipts without relabelling their
head or transferring them to different `808c`/`8a4c` image bytes.

## Historical version admission correction before final proof

The 2026-10-07 decision corrected the brief-origin own-executable substitution: a runtime must also contain its named console as a file, `Scripts/pseudolife-mcp.exe` on Windows or `bin/pseudolife-mcp` on POSIX, matching Python's `runtimes.py::list_runtimes`. The candidate retains its own executable, layout and canonical/as-written runtime matching. Valid fixtures seed the named console without changing positive assertions; new missing-console and directory controls constrain admission. These source and fixture changes are not covered by the earlier receipts.

Published `95d5402d` has all 14 checks green. [Rust/Parity run 37489572403](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37489572403) passed all four jobs and [general run 37489572384](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37489572384) passed all five jobs. The actual merge-test commit was `2caec278`, whose tree equals `95d5402d`'s `8156ec27`; neither identity is relabelled. On each OS, artifacts record 18 unique candidate nodes (10 stdio, eight CLI) and eight Python CLI nodes, each once/passed/non-skipped; no Python ten-stdio original lane was reported. Rust default/no-default contracts pass Linux 303/295 and Windows 314/306, with zero skips. These results validate the published own-executable criterion, not the subsequent named-console correction or warm measurement instrument.

The Windows installed Rust/Python medians of approximately 269/159 ms at `95d5402d` use the old protocol and retain the 109.798 ms regression, per-arm repeat floors and recorded desktop-load context. The bee4 110.030 ms and earlier 278/156 ms findings remain historical evidence. Fresh-copy/reuse diagnostics show a launch/cache condition affecting help and version; they do not establish its cause or prove a security-provider explanation. Required successor measurements must warm both arms under the corrected protocol, retain the exact byte/file/environment controls and record three repeats of ten paired samples with per-arm floors. Old results are not discarded or relabelled as warm-protocol captures.

The scoped output contract is UTF-8 stdout/stderr with platform newlines. This is a deliberate output change from Python locale/default encoding: the harness sets `PYTHONIOENCODING=utf-8` on the Python arm. Locale/default encoding parity remains deferred. The `5220b5ee` proof includes actual non-ASCII homes on both OSes, exact missing-console fallback, native argv, all eight original CLI nodes per arm and warm pairs. Version remains deferred and #600 is not ready until required successor hosted checks and independent review pass. These CLI cells establish no stdio or full-suite acceptance.

Captured `5220b5ee` source is 184/125 = 1.472: 171 physical Rust version lines before `#[cfg(test)]` plus 13 net dispatcher lines versus `c9705e88`, divided by 223 Python CLI module lines minus the 98-line AST `_USAGE` asset. Comments/blanks count; tests, assets and shared support do not. Historical 176/125 retains its own binding.

Historical publication state before #600 merged: #600 remained draft, with published `95d5402d` checks and local `5220b5ee` captures retaining their own identities. #589 and #606 closed after verified survival. Historical receipt links point to merged #610 CLI and #609 ONNX evidence. Master `4d6b6dc99cdfd1d3df189be4ed39c8b950575110` is integrated: all 26 historical CLI receipt blobs and the ONNX receipt remain byte-identical ancestor content and are absent from this PR diff. The only merge conflict was `rust/PHASE3-READ-PATH.md`; master retained the complete old Git byte prefix plus its 13,823-byte, 45-line historical ranking appendix, and its whole file was retained. At master-forward `13ec5ec7`, all 341 capture-bound source/instrument/production paths remained exactly unchanged from `5220b5ee`, with checkout-byte and canonical Git hashes kept separate. The subsequent integration of master `7b0abf921fe4bd4ef1c84864eef118309b400705` inherits the reviewed #607 users runtime/dependency changes without a conflict: 5 of the 341 capture-bound paths differed at `181a50d1` from executed `5220b5ee` (`rust/Cargo.lock`, `rust/Cargo.toml`, `rust/shim/Cargo.toml`, `rust/shim/src/board/state.rs`, `rust/shim/src/credentials.rs`), while the other 336 and all 146 original production/test guard paths remain exact. The `5220b5ee` measurements remain historical source-bound executions and do not validate the inherited users implementation or this combined tree; current hosted CI and fresh review are required. Successor hosted checks and fresh independent review remain pending.

## Frozen combined version evidence at 5220b5ee

This section retains the historical state before the final merged-master proof. Current scoped CLI-VERSION acceptance is recorded in the final `df2dbf8a` section below.

The implementation and measurement instrument were captured at
`5220b5ee4cf4b7ae235ee070109b8de17fa3a1d0`, tree
`11e7267bd7a884ace22f57ebf5afd6c77ee25f91`, against Python 0.17.0/schema 55
at `3c01bb31abd60178e15dea99adda369b4bbf92fc`. Windows uses genuine Python
3.11.9 and Linux 3.11.15. The fresh Windows release image is SHA256
`2ec6ce230d7aa06f016fbc63342ec2f5f70ebd9b7d2c28a5d43493048cb02916`
(13,267,968 bytes); Linux is
`0b10d4618844c7cb8b7f0194fb8cf89ce5cd0292090cca732c80f2ea72a9b4a5`
(16,826,112 bytes). Hashes and retained build output establish provenance,
not reproducible-build attestation. The final evidence packet is separate
from this implementation/documentation carrier. It is the
[5220b5ee packet](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/ceb1e3bb0fbe84afeec6dc22f7b10cdd4c4379da/evals/results/rust-phase2d-version-5220b5ee/README.md),
whose evidence carrier retains the captured execution head.

Each OS passes 28 exact cases and 112 candidate mutation controls: five help,
ten unknown-dispatch and thirteen version cases. New cases retain a Python-
shaped marker with native image present but canonical console missing, and a
valid installed fixture whose actual home contains non-ASCII characters.
The missing console produces Python's exact fallback. Output, ordinary exit,
stderr, environment and pre/post files remain exact. Installed smoke verifies
the relocated genuine Python runtime. Each OS also passes its one native
invalid-argv control and all eight immutable original CLI nodes once, passed
and non-skipped per arm. These storage-free CLI cells start no database or
daemon and do not establish a guarded real-bank transcript.

Each of three version measurement blocks begins with one untimed invocation
per arm. Ten alternating pairs then reuse those exact executable file
identities and identical restored input state. Each OS/layout records two
untimed byte controls, six warm starts and sixty timed starts (thirty per
arm), retaining output and state. Preparation, restoration and file checks
are untimed; owned-process setup, output collection and clean exit remain
timed. Version enables warmup by default in `cli_measurement.py`; help can
explicitly opt in with `--warm-images`. Other CLI benchmark reset semantics
are unchanged. The historical `cold_start_to_exit_ms` key denotes a fresh
process after the recorded image warmup.

| Platform | Layout | Arm | Median ms | p95 ms | Median floor ms | p95 floor ms |
|---|---|---|---:|---:|---:|---:|
| Windows | bare | Python | 124.089 | 134.394 | 2.348 | 2.507 |
| Windows | bare | Rust | 17.540 | 19.887 | 0.524 | 0.502 |
| Windows | installed | Python | 148.556 | 159.223 | 4.560 | 14.248 |
| Windows | installed | Rust | 20.591 | 23.021 | 1.138 | 0.585 |
| Linux | bare | Python | 119.715 | 122.582 | 1.635 | 1.914 |
| Linux | bare | Rust | 2.529 | 3.027 | 0.147 | 1.844 |
| Linux | installed | Python | 578.670 | 595.723 | 3.844 | 5.837 |
| Linux | installed | Rust | 2.741 | 3.222 | 0.078 | 0.860 |

Python's version path calls runtime discovery and marker/console checks, but their cost was not isolated and the cause of the Linux installed Python median of 578.670 ms versus 119.715 ms bare remains unknown.

Under this recorded warm-image condition, Rust's median and p95 are lower in
all four cells; each difference exceeds the larger observed floor of both
arms. Floors describe repeat-block ranges, not confidence intervals. Desktop
load was recorded: Windows CPU samples were 62% and 46% before its bare and
installed cells; Linux before/during/after samples remain in the packet.
No controlled-idle condition is established. Executable sizes exclude
interpreter dependencies; these timings are not shim first-frame or
initialize-return measurements.

The cold-copy Windows regressions remain historical measurements of that
condition. Fresh-copy/reuse observations do not establish a security-provider
cause. Read-only Windows context showed Defender enabled and no Zone.Identifier
on the actual source and copied image. No cause investigation or recapture
is required unless that condition recurs.

Linux's unchanged version contracts pass 8/8 serially with default features
and 8/8 without default features; twelve new harness controls pass. Earlier
parallel `ETXTBSY`, stale-image and metadata failures remain retained. The
parallel cause is unresolved; serial success does not imply a parallel pass.
Captured checkout-byte hashes remain distinct from canonical Git hashes,
including CRLF and the copied eval test's extra CR. Source equivalence does
not replace their exact byte identities. Owned fixtures and workloads were
cleaned up. A docs/results-only carrier preserves the executed `5220b5ee`
identities instead of relabelling those captures to its own head. Successor
hosted checks and fresh independent review remain pending; CLI-VERSION stays
deferred, with no new full-suite or ported claim.

Integration of master `57c007ff292cffdbb6755911bd54de837a00f9a5` retains #611 readiness evidence, #607 users and #608 executable resolution. Of the 341 paths bound to executed `5220b5ee`, 11 now differ (`evals/rust_port/phase1.py`, `evals/rust_port/phase1_receipts.py`, `evals/rust_port/stdio_judge.py`, `rust/Cargo.lock`, `rust/Cargo.toml`, `rust/shim/Cargo.toml`, `rust/shim/src/board/state.rs`, `rust/shim/src/credentials.rs`, `rust/shim/src/lifecycle.rs`, `rust/shim/tests/auth_windows_private_state/mod.rs`, `rust/shim/tests/nonboard_final_assertions.rs`); 330 remain exact, including all 146 original production/test guard paths. Incoming changes from reviewed `181a50d1` affect 9 bound paths. The old timing images and captures remain historical executions of `5220b5ee`, without acceptance claims for this combined tree.

The `57c007ff` master-forward resolved CHANGELOG by retaining both dated entries, reconciled Cargo.lock with the combined manifests, and retained the master `find_executable` export alongside the prior standard-library file-lock calls in lifecycle.rs. That import conflict is a code conflict, not a documentation-only merge. No new local full suite ran; fresh combined-head hosted full CI is required. Published `181a50d1` has 13 of 14 checks green, with the retained Windows installer PowerShell 30-second timeout classified by the maintainer delegate as a flake; no rerun was requested and no cause is established. Independent review of `181a50d1` remains historical for the successor.

## Final scoped CLI-VERSION acceptance at df2dbf8a

Scoped CLI-VERSION is ported at merged master
`df2dbf8adbe0d1fc84b87fb0d05da6457ba7d9ff`, tree
`288237846a3923ad49dfb6cf11d099935461a4d8`, after #608 and #600 merged.
The [final packet](../evals/results/rust-phase2d-version-df2dbf8a/README.md) binds the actual executed head/tree, fresh default
release images, genuine Windows Python 3.11.9/Linux 3.11.15 and unchanged
Python oracle `3c01bb31` (0.17.0/schema 55). This evidence-only carrier does
not relabel those executions to its own commit. The merged runtime work and
this carrier's independent review/hosted checks are separate evidence streams.

Each OS passes 28 exact cases and 112 rejecting sensitivity controls, named-console
file admission/missing-console fallback, actual non-ASCII homes, one native argv
case and all eight unchanged original CLI nodes once/passed/non-skipped per arm.
Serial version contracts pass 8/8 with each feature configuration on both OSes;
Linux also passes 12 focused harness tests. Original production and tests remain
unchanged. Each of four final layouts retains two byte controls, six warm starts
and 60 alternating timed starts (3x10 per arm), exact output/environment/restored
pre/post state and unchanged warmed executable file identities.

| Platform/layout | Python/Rust median ms | Python/Rust p95 ms | Python/Rust median floor ms | Python/Rust p95 floor ms |
|---|---:|---:|---:|---:|
| Windows bare | 121.790 / 18.374 | 156.851 / 48.134 | 20.483 / 12.649 | 9.356 / 7.180 |
| Windows installed | 139.033 / 18.983 | 160.904 / 21.471 | 1.824 / 0.818 | 13.104 / 6.578 |
| Linux bare | 115.188 / 2.317 | 118.990 / 2.671 | 1.077 / 0.018 | 2.151 / 0.379 |
| Linux installed | 560.897 / 2.820 | 572.864 / 3.269 | 6.413 / 0.357 | 1.497 / 0.599 |

Floors are descriptive repeat-block ranges, not confidence intervals. Recorded
desktop CPU load was Windows 11%/17% before bare/installed and Linux 47%/29%/20%
before/during-installed/after; no controlled idle is claimed. Cold-copy Windows
regressions and failures remain historical; their cause is unknown, with no
Defender attribution. The cause of the Linux installed Python timing gap also
remains unknown. Historical Linux parallel `ETXTBSY` stays unresolved; the new
serial passes do not imply parallel success.

No-default contract tests replaced canonical cache outputs. The immutable measured
default images were explicitly copied back after children/compilers stopped,
restoring exact original canonical hashes while preserving all measured bytes
and receipts. The retained first Windows audit failed its final canonical-cache
hash check. Its default rebuild exited zero but differed in 24 PE timestamp/PDB
GUID bytes; both rebuild hashes and explicit copy-restoration provenance are in
the packet. This does not establish a byte-reproducible rebuild.

The ratio remains 184/125 = 1.472: 171 physical Rust version lines before
`#[cfg(test)]` plus 13 net dispatcher lines versus `c9705e88`, divided by 223
Python CLI module lines minus the 98-line AST `_USAGE` asset. Comments/blanks
count; tests/assets/shared support do not. Historical `5220b5ee` retains the same ratio
under its own identity; the ratio is reporting with no hard target.

Acceptance covers installer-shaped manifests and UTF-8 streams/platform newlines.
Manifest NaN/Infinity, lone surrogates and beyond-u64 integers remain deferred,
as do locale/default or other output encodings and non-UTF-8/surrogate argv.
The candidate is not installed; other CLI modes retain their existing gates.
Owned fixtures and process subtrees were cleaned up. These CPU cells claim no
new optional stdio proof or full Python suite, and no GPU/model work.

## Phase 2b native lease candidate

Native `lease check`, `lease list` and `lease run` have a local implementation
candidate. Operator actions remain deferred. Targeted Windows proofs against
the historical Python 0.16.1 pin cover raw-surrogate identity, held/free names,
exact 8191/8192/8193-byte output boundaries and nested-run precedence. The latest
focused repair has 22 matching byte pairs, three matching closed-check stream
pairs and 72 rejected candidate-output controls; two traceback cases use the
explicitly deferred presentation boundary in PORTING. Earlier receipts retain
their original candidate identities, including the corrected distinction
between authenticated peer observations and local no-board observations.

The seven native lease files contain 2,884 physical production lines, including
693 JSON compatibility-helper lines, against 1,889 in the pinned Python lease
module (1.52673). Test modules, assets and dispatch are excluded; the earlier
2,996-line count included 112 test lines. The Python denominator includes
deferred operator modes. Current-master and shared-helper source integration uses Python
0.17.0/schema55, and committed helper admission passes. The latest review repairs
cover run-option ambiguity and ISO holder timestamps with 25 exact byte pairs
and 100 rejected output controls. A subsequent CPython grammar comparison fixes
signed-year refusal and numeric week-date separator selection: 2059 sampled
holder strings, six lease units and the affected process test pass. Its 30
focused public pairs match exactly, with 120 output controls rejected; the
previous binary differs in 14 of those pairs. Independent review then found
seven UTC-marker/NUL counterexamples. The focused repair passes 2113 sampled
strings, six lease units and the affected process test; all 22 focused public
pairs match, including those seven failures, with 88 output controls rejected.
These are focused Windows results, not complete grammar or cross-platform
acceptance. Original integration preparation retained 230 passed, one failed and
two deselected checks before the separate committed helper-admission repair.
Ten surrogate-name list observations match
the failure exit, terminal exception and unchanged state, but their full Python
tracebacks remain unmatched; this third case is outside the two approved
presentation exclusions. Fresh repair review, both-platform real daemon/storage
and signal acceptance, CI and paired measurements remain pending. Historical
receipts retain their original source and runtime identities. The CLI-LEASE
parity row remains deferred.

The first disposable Python 0.17.0/schema55 Windows check/list/run/release proof
matches four process pairs using the fixture's environment bearer. Its earlier
file-source attempt remains a failed board proof: Windows `chmod` did not make
the token file owner-only. Fixture preparation now calls the pinned production
secure token writer and binds the executed credentials module. File-source
writer/read admission and targeted fixture checks pass; the same four-cell
real-daemon proof on a committed helper head and fresh review remain pending.

At `07761381`, the Windows token-file proof passes four real-daemon pairs and
16 output controls without an environment bearer; owned cleanup is verified.
Independent review approves the token-writer change at that head. The first
Linux build exits 101 because the Unix lock path names a nonexistent Rustix
`ACCES` constant. Successor `8a1ead9b` uses `ACCESS`; its Linux build and six
lease units pass, but the Unix signal test fails compilation under its existing
`forbid(unsafe_code)` lint. This candidate uses safe Rustix signal and liveness
APIs while preserving that lint and the test assertions. Linux fmt and all-target
compilation pass, followed by six lease units, 13 lease-board tests and 15
lease-CLI tests. The real-daemon proof on a committed successor remains pending.
Both original compiler failures are retained.

At `961d60e5`, the Linux TOKEN_FILE proof records four mismatching pairs:
the fixture resolves the virtualenv executable symlink and invokes base Python,
which lacks httpx and falls back locally while native board access succeeds.
Raw counterexamples and complete owned cleanup are retained. This fixture repair
preserves the invocation path and checks resolved bytes and ownership separately.
A disposable process roundtrip observes the intended 0.17 runtime and exact raw
streams and file effects. Targeted checks pass on Linux (147) and Windows (143,
with four POSIX-only skips). Committed real-daemon recapture and the fifteen
affected Linux help/unknown-command rows remain pending. Comparison policy and
traceback scope are unchanged; CLI-LEASE remains deferred.

At `f4bf8a83`, the committed Linux token-file path matches four real-daemon
pairs and rejects 16 output controls, with the installed 0.17 virtualenv,
httpx, board access and owned cleanup observed. This point-in-time proof does
not close independent review R1: preparation could select an owned copy while
retargeting the original executable symlink to equal bytes. This candidate
retains the original resolved target independently through preparation, context
entry and capture. All four new refusal cases fail on the preceding helper and
pass after repair. A changed helper return shape initially broke four existing
measurement tests; restoring its three-value API closes those failures. The
combined targeted checks pass on Linux (129) and Windows (121, with eight
Unix-only skips). R1 remains pending fresh successor review; no additional
daemon proof or affected help-row recapture has run on this repair. CLI-LEASE
and the third traceback scope decision remain deferred.

The lease candidate now integrates the reviewed version dependency `804e829e`,
including its startup contract, historical evidence packet and runtime erratum.
The three-value preparation API, secure token writer, complete helper inventory,
process context and original/selected target checks are retained. Both sets of
invocation regressions remain: four lease context/owned-copy refusals and the
version snapshot, preparation, prelaunch and postcapture refusals. The aligned
targeted selection passes 133 checks on Linux and 121 on Windows, with twelve
Unix-only skips; it includes the existing measurement API tests. The version
invocation repair has scoped independent approval at `804e829e`; fresh review of
this aligned lease tree remains pending. These checks do not establish whole-mode
acceptance, recapture the affected Linux help rows, extend the two approved
traceback exclusions or provide new daemon/timing evidence.

At `b1359c8f`, a bounded Linux proof against the disposable Python 0.17.0/schema55
daemon passes one basic check pair with four controls and three FIFO process
pairs. Both arms observe waiter one and then waiter two queued behind a holder,
and execute holder, waiter one, waiter two in that order. Process streams, exits
and home-file bytes match; raw queue identities and enqueue times are retained
without cross-arm equality claims. Post-release checks show no holder or queued
waiter, and owned processes, database and homes are cleaned up. This is a
point-in-time Linux FIFO proof, not Windows, timeout, capacity, renewal, signal
or timing acceptance. Independent review of the complete candidate at that head
then found three blocking defects: mutable environment receipts, host timestamp
formatting and non-ASCII registration headers. Their successor repair is below;
the historical proof does not establish acceptance of that successor.

The successor repairs the original counterexamples. The fixture retains an independent
launch environment and refuses changes after capture and context exit/poststate
collection; eleven watched regressions fail before repair on each platform.
Focused fixture and compatibility checks pass 144 tests on Linux and 132 on
Windows, with twelve Unix-only skips. Native timestamp formatting now uses each
host's CRT conversion and formatting, including its actual range and year
padding. Registration retains JSON string codepoints and refuses non-ASCII
instance headers before sending, including unpaired surrogates. Thirty-five
owned-peer public pairs match exactly per platform, including the original five
review counterexamples, two surrogate-header cases and 28 timestamp boundaries.
Native lease checks pass 36 tests on Linux and 37 on Windows; fmt and locked
builds pass on both. These are bounded repair checks with actual matching request
traffic, not a new real-daemon proof or complete mode acceptance. Fresh combined
review, committed successor proofs, current CI and measurements remain pending;
the third traceback exclusion remains unapproved and CLI-LEASE stays deferred.

Independent successor review closes the environment-receipt finding but retains
two code blockers. On Linux, a successful host time conversion can yield a year
that Python rejects when converting its tuple for formatting; check must return
the exact exit-70 error without a partial report. Python also transmits ASCII
DEL in a malformed registration reply's instance fields, while the native HTTP
header type refuses it before transport, changing requests, exit and lock state.
Raw counterexamples from both platforms remain retained. A proposed DEL-case
deferral is unapproved; no unchecked header construction or comparison exception
is adopted. This candidate remains local pending repair and fresh review.

The version dependency is subsequently integrated through `db0d59cb`, retaining
its reviewed fifteen-row Linux receipt and independent environment-snapshot
repair. The fixture merge preserves lease process contexts and all original and
selected executable checks, and checks the admitted environment immediately
after context exit as well as after capture and poststate collection. Combined
compatibility checks pass 186 tests on Linux and 174 on Windows, with twelve
Unix-only skips; one actual disposable-child cell also passes on each platform.
These fixture checks do not close the remaining native clock/header findings or
the mode's acceptance gates.

The remaining C-int-year repair now propagates the host formatting error through
check and queued-run callers: check returns its exact exit-70 error before any
report, JSON bypasses formatting, and queued run takes the Python-compatible
local fallback. A new public corpus matches all seventeen Windows pairs and
fifteen of seventeen Linux pairs. The two Linux list rows match exit 1, empty
stdout, the terminal OverflowError, file state and captured request projections;
their full Python tracebacks remain unmatched and their proposed presentation
exclusion is unapproved. The DEL-header and earlier surrogate-name traceback
boundaries also remain unresolved. Seventeen focused native tests and locked
all-target shim clippy pass on each host; earlier compiler/lint failures remain
recorded. These uncommitted-source repair checks do not substitute for committed
successor acceptance. Fresh review and the broader mode gates remain pending.

At `c4fc33593d55983b127ccf537307ff9c838e75ee`, tree
`88446f4bb2e3638ef2c4b693cbf7fc5795caeecd`, the bounded committed TOKEN_FILE
path passes four real-daemon Python/native pairs and rejects all 16 output/file
controls on each OS. Genuine Python 0.17.0/schema 55 at the oracle above is used;
the check/list observations show available board, no holder and zero queued
waiters, and the run preserves its exact child streams and exit 3. Post-release
check is free again, with owned PostgreSQL, daemon, children, database and homes
cleaned up. Local untracked evidence is `lease-c4fc-committed-proof-result.json`
and `lease-c4fc-{windows,linux}-proof.json`. This bounded path does not establish
the remaining host/storage/concurrency/recovery/signal gates.

The existing CLI instrument at
`127e04ed1c7e5902c77e8b0f83873825a2822b81`, tree
`381a8c5a230ab5a75cea715dcf7b040d1942584d`, measures only the retained positive
empty/free `lease check sample --json` TOKEN_FILE case. Each OS completes three
repeats of ten cold-start-to-exit samples per arm, alternating arms and retaining
the existing repeat floors; 1x1 plumbing smokes remain separate. Independent
expected controls retain 286 Windows bytes and 269 Linux bytes, exit 0, empty
stderr and exact unchanged token/instance home bytes. Four field mutations are
rejected, independent endpoint state is unchanged and owned cleanup is verified.
Timed rows omit raw stdout/stderr/exit and post-file payloads: exact recorded
arm-level controls are distinct from the instrument's programmatic per-invocation
output/environment/home comparisons before a timing row is appended.

| OS | Local untracked final receipt | Receipt SHA256 |
| --- | --- | --- |
| Windows | `lease-measurement-windows-3x10.json` | `5652af321e77cbca9b31a9b3e837eb124f763c14fc9943e97d04e161a5596317` |
| Linux | `lease-measurement-linux-3x10.json` | `4aecc3583c684e69dadb742452cf8219ab6fa9b6673cb0c55c410bc823a6dac1` |

Native executable SHA256 is
`072490df14742c80f9aa597c4511649e40dd686d236cd189747841eba97e0020` on Windows
and `368e7b120ac92a5a52e8f4fa24a411ca14af72c245428895c08d701bb94e08bd` on Linux.
Retained raw Rust/Cargo hashes match the prior per-OS source maps, and native
executable hashes are unchanged; the Linux successor preserves the original
cache and ELF.
These are debug measurements with incomplete producing-invocation/profile
attestation, not release-profile evidence or a general speed claim. Local
`lease-measurement-results.md` and `lease-measurement-audit.json` record the
descriptive distributions, helper/runtime bindings and retained-row limitations.

Full CLI-LEASE acceptance remains deferred. Pending groups 1 (ten surrogate-name
list traceback cells), 3 (D3 ASCII DEL registration-reply headers) and 4 (two
Linux C-int-year list traceback cells) remain unapproved, preserving all twelve
traceback cells. No unchecked header construction, normalization or new traceback
exclusion is adopted. Next action is the DEL compatibility/scope decision and
the remaining traceback and broader acceptance gates, including hosted CI;
this check-only timing does not close `lease list` or `lease run` acceptance.

The independently reviewed list instrument at
`073c975e18c1ce4922ca84d0e6293ab5c0a9bb5f`, tree
`f1a7d2d93a69dac0a78a18a8542480266408f71e`, extends measurement to exactly
`lease list --json` on an available empty owned board, using the retained
TOKEN_FILE and instance bytes. Windows and Linux each complete a separate 1x1
plumbing smoke and three repeats of ten alternating cold-start-to-exit samples
per arm. Final arm-level controls retain 318 Windows bytes and 323 Linux bytes,
exit 0, empty stderr, the actual owned URL/lock-directory path and unchanged
home bytes. Four field mutations are rejected per receipt; independent endpoint
bytes before/after agree and owned PostgreSQL, database, daemon, children and
private-home cleanup is verified. Retained native binaries and per-OS raw
Rust/Cargo source maps above remain unchanged; current instrument/helper/runtime
bindings were checked against both native checkouts and the pinned oracle.

| OS | Local untracked final receipt | Receipt SHA256 |
| --- | --- | --- |
| Windows | `lease-list-measurement-windows-3x10.json` | `52fefa6a010ba004abedacabcf93ee98fe3f1f6181b72fe9573602d448fe4619` |
| Linux | `lease-list-measurement-linux-3x10.json` | `e37502bcb9eb12fbdb82bede34cb78834ca66dd4f4acb6a6f2bf2b59b88b3ed6` |

Local untracked `lease-list-measurement-results.md` and
`lease-list-measurement-selfcheck.json` retain receipt hashes and recomputed
metrics. Timed rows omit raw stdout/stderr/exit and post-home payloads: exact
recorded arm-level controls remain distinct from the instrument's programmatic
per-invocation response/environment/home comparisons. These are debug-only
measurements with incomplete producing-invocation/profile attestation; no
release-profile evidence or general speed claim follows.

Successful-run timing remains deferred under pending group 13. The unchanged
existing exit-0 child writes `time.time()` into `ran.json`; the local untracked
`lease-list-run-windows-first.json` preserves both exit-0 captures, exact empty
streams, released leases and the sole raw post-home mismatch at that timestamp.
The narrow comparison-policy question remains unanswered; no timestamp exception
or normalization is adopted. Earlier exit-3 proof bytes remain unchanged.
Full CLI-LEASE acceptance, groups 1/3/4 (all twelve retained traceback cells and
DEL headers), broader acceptance gates and hosted CI remain outstanding; this
bounded empty-board list timing does not close them.

### Lease phase 2c preparation at a6cdf024

Draft #602 remains deferred. PORTING.md names the three approved policies and
PARITY.md lists all 23 retained instances: 14 historical traceback-contract
matches, eight forbidden-header cases requiring fatal refusal implementation,
one successful-child clock case incomplete without own invocation windows and
a second oracle run. `rust/lease-policy-cases.json` binds the retained receipt
basenames/hashes and exact terminal/diagnostic contracts. These decisions
supersede the preceding historical notes that called groups 1/3/4/13 unapproved.
New static preparation tests establish only the offline checker. No captures,
measurements, rebases, builds or current-head CI proof were performed. Next:
wait for the version branch to become ported, implement fatal refusal, wire
named comparison scopes, and rebind final-head Windows/Linux receipts before
any row promotion. Existing tests and eval instruments remain unchanged.

### Lease phase 2d integration scope

The lease candidate is integrated onto corrected version base `5220b5ee`.
Its Parity job now selects the lease corpus as well as help and version. The
Chrono timestamp substitution in PARITY.md preserves ordinary local producer
output and JSON values, displays `?` for unsupported dates, and excludes
Windows CRT-only timezone overrides. Original clock failures and the phase 2c
policy ledger remain historical evidence; no CLI-LEASE row is promoted.

Still deferred are fatal forbidden-header refusal and its executable corpus,
successful-child invocation-window comparison, shared lease fixture/help drift
guards, explicit Windows unlock and Ctrl-C coverage, and the disposition of
`hold`, `break` and `delegate`. Those three actions currently return a deferred
error rather than dispatching to Python. Full mode, final-head hosted,
measurement and independent-review gates require separate closure.

### Lease shared fixture and help guard follow-up

The shared lease Home fixture and help drift guards were added at `cb0d89fd`.
All eight committed help/usage assets are compared with the genuine pinned
Python argparse output at `3c01bb31`, using `COLUMNS=80`. Existing Rust test
assertions are retained; the fixture move changes only their setup names.
Windows checks at that helper head passed nine Python guards, 15 lease CLI
tests and 17 lease board tests. Linux checks at the same head passed nine
Python guards, 15 lease CLI tests and 16 lease board tests.

This closes the shared Rust fixture and help drift guard item in the preceding
integration scope. The full source review at `d8a936eb` remains bound to that
candidate. The new carrier merges version `181a50d1` and inherits its users
dependency removal: `rust/Cargo.lock`, `rust/Cargo.toml`, `rust/shim/Cargo.toml`,
`rust/shim/src/board/state.rs` and `rust/shim/src/credentials.rs` change, and
the Unix account resolver, its tests and the comparison/evidence files are
added. The merged Cargo workspace retains the lease Chrono version requirement
and its Tokio signal and Windows I/O features.
The version section's 341-path counts describe the version carrier alone.

Lease-owned CLI source, assets, shared Home/help guards and CLI/measurement
instruments remain unchanged from `cb0d89fd`; original Python, tests and
conftest remain unchanged. Earlier clock, version/dispatch, disposable-cell,
instrument, R1 and help executions retain their `6016a3ce`, `5ba6f84a`,
`06a2bbae`, `7a4a6976` and `cb0d89fd` source bindings. They do not validate
the inherited users changes or this new combined tree. No combined-head
execution, capture or timing is claimed; current hosted CI and fresh review
remain pending. CLI-LEASE stays deferred: fatal forbidden-header refusal and
executable corpus, successful-child own invocation windows and second oracle
capture, explicit Windows unlock and Ctrl-C, `hold`/`break`/`delegate` policies,
the final both-OS/hosted matrix and measurements still require closure.


### Lease master-forward and forbidden-header candidate

Master `df2dbf8adbe0d1fc84b87fb0d05da6457ba7d9ff` is integrated by ordinary
merge, retaining both historical evidence sections, lease Chrono/platform
features and the LF help-asset rule. The Windows lease child lookup now uses
master's `lifecycle::find_executable` after the `which` dependency removal;
lookup failure still falls back to the original command. The lockfile delta
only removes that dependency and its unused package entry. Original Python
source and original `tests/`/conftest bytes are unchanged.

The fatal forbidden-header candidate and its additive public-process corpus
close only the implementation gap for the approved header policy. Prior
`49342a21` hosted results remain historical on the old `181a50d1` base. New
local evidence retains its own exact source and executable identities; fresh
review and successor hosted checks are separate. CLI-LEASE stays deferred.
Successful-child own invocation windows and second oracle capture, explicit
Windows unlock/Ctrl-C, measurements and final acceptance remain pending.

The header corpus also covers combined malformed daemon URLs with selected
environment/private-file bearers and incomplete registration replies. Forbidden
admitted headers preempt ordinary fallback; otherwise URL-first credential
diagnostics, private-file security and missing/empty address behavior remain.
Raw ordinary controls retain non-ASCII and surrogate reply behavior.

`hold`, `break`, `delegate` and `designate` remain Phase 4. This native
candidate loses those four actions until Phase 4; each must explicitly refuse
with exit 1 and a diagnostic containing `deferred in this candidate`, without
Python or silent fallback. A future CLI-LEASE-core check/list/run row may be
accepted only through its own gates, and full CLI-LEASE requires every action
to be native. This candidate makes neither promotion.
