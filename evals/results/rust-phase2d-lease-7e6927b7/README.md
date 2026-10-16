# Bounded native lease core evidence

Native `lease check`, `lease list` and `lease run` have a functional candidate.
The Windows/Linux core proofs, Windows Ctrl-C proof and remaining admitted
policy cells below passed on executed head `7e6927b7b72db62569281d3477dc8e8924c82292`,
tree `34b24d60d854e28448079bfc0c8e8eea83551d47`. Final hosted acceptance and
independent review remain pending; **CLI-LEASE-core is not yet ported**.
Full CLI-LEASE stays deferred. `hold`, `break`, `delegate` and `designate`
each refuse with exit 1 and `deferred in this candidate`, without Python or
silent fallback. This candidate loses those actions until Phase 4.

The oracle is genuine Python 3.11, pseudolife-mcp 0.17.0, schema 55, pinned
production/tests at `3c01bb31abd60178e15dea99adda369b4bbf92fc`. Windows uses
Python 3.11.9 and Linux 3.11.15. The immutable debug image SHA256 values are
`567c6bb5b61e2997028aaedf014f47e08e781bb0ebe952e55e0b8b131b21479f`
(Windows) and `87366bc2303fea0799cdb0e528b4f4e83008e6789358a17f79fb4718b05db1b9`
(Linux). Source archives, canonical committed source hashes and actual build
recipes are in [the bound evidence](phase2d-lease-core-measurements-evidence.json).
Later documentation/comparison additions do not relabel these execution identities.

| Artifact | Observed scope |
| --- | --- |
| [Core and measurements](phase2d-lease-core-measurements-evidence.json) | Both-OS successful child, exact nonzero child, four deferred actions, release/lock effects and cleanup; Windows actual CTRL_C_EVENT; all four check/list measurements |
| [Core policy details](core-policy-evidence.json) | Actual three-arm clocks/windows, six semantic controls per OS, producing child/source bindings and Windows interrupt helper/effect checks |
| [Affected Rust checks](affected-checks.json) | Final7e explicit Windows unlock, release ordering/child lock, Linux SIGTERM, format and scoped Clippy; actual commands/exits/log hashes and cleanup |
| [Policy outcomes](policy-evidence.json) | Sixteen cells per OS: fourteen retained policy IDs plus two exact timestamp JSON companions; 80 recorded output/state/request controls per OS and 32 offline wrong-exit rejections |
| [Policy inputs](policy-inputs.json) | Exact synthetic argv, initial files and malformed peer reply bytes; no retained credentials or personal paths |
| [Measurement recipe](MEASUREMENTS.md), [invocations](producing-invocations.json) | Exact producing check/list controllers and fresh-credential helper, complete samples, quantiles and descriptive repeat floors |

Only the bare TOKEN_FILE `check` and empty-board `list --json` layouts were
timed: three repeats of ten alternating starts per arm, equal image warmups,
240 timed starts, 24 warmups and 16 rejecting mutations across four cells.
Fixture/reset/auth/state/endpoint checks are outside the timer. These debug
measurements describe complete fresh-process exit after warmups; repeat floors
are ranges, not confidence intervals. Successful `run` is functional only and
has no timing claim. There is no release/installed-performance claim.

## Existing producing APIs and fixture requirements

Run from the genuine pinned oracle/runtime, with candidate eval modules selected
from `$CANDIDATE_ROOT`; call `require_phase1_source`, `require_import_root` and
`cli_process.cli_binding` before capturing. `$IMAGE` is independently hash-bound
to its actual source/image packet. Homes, private token files, PostgreSQL and
daemon are exclusively owned disposable fixtures, with native POSIX homes for
private-file security. Check the shared full-suite resource before fixture work.
Do not substitute an installed production daemon or bank.

For core evidence, the producing APIs are
`evals.rust_baseline.daemon.disposable_database`, `private_directory`,
`launched_daemon`, and `evals.rust_port.cli_process.observe`/`paired_cases`.
The original successful child is the unchanged AST `_command` from
`tests/test_lease_cli.py`; its source/AST hashes are retained in the core detail
artifact. Its actual lease argv is `lease run gpu --expect 20m --purpose
"nightly eval" -- <original _command(home) argv>`, with inherited held `suite`
and project `pseudolife`. Initial files come from the existing
`lease-list-empty-json` case. Reset the same private home before every arm.
Record `time.time()` immediately before/after each actual `observe` call,
including `oracle_repeat`, then invoke:

```python
successful_child_clock(oracle_response, candidate_response,
                       {"oracle": oracle_window, "candidate": candidate_window,
                        "oracle_repeat": repeat_window}, repeat_response)
```

The checker is `evals.rust_port.lease_policy_preparation.successful_child_clock`.
Each finite decimal `/t` must lie in its own invocation window, the two Python
clocks must differ, and every other byte/state must agree. The raw home comparison
is deliberately false; only this admitted semantic comparison passes. Nonzero
coverage uses the existing `lease-run-held-env-appended` corpus case with inherited
`outer`, through `paired_cases`, and compares the full exit-3 response/state
exactly. No timestamp exception applies to nonzero children. Independent board
readbacks and separate local-lock acquisitions follow each run. Deferred actions
are invoked individually as `lease <action>`; only candidate refusal is expected.
Private orchestration hashes are retained separately from these committed APIs.

Windows interrupt capture uses the three **unchanged actual producing helpers**
in this directory. Load `phase2d-lease-windows-ctrlc-capture-v3.py` and call
`capture(label, command, commands, oracle_root, home, owned_evidence, url,
pre_files)` once per arm against the owned available daemon. `pre_files` are the
existing empty-board list case's initial files, with a reset identical home.
The helper creates only a new hidden owned console, starts the unchanged
signal-resistant child, waits for child readiness and an actual held board
lease, then sends CTRL_C_EVENT(0,0). Exact exit 130/interrupted bytes, identical
post-home state, resistant-child absence **before** helper cleanup, zero helper
kills, released board state and reacquired byte lock are required. No clock
or signal normalization applies.

For policy cells use the exact cases in `policy-inputs.json`, one owned loopback
HTTP peer (for I3/date cells only) returning each raw reply with status 200 after
admitting the same synthetic environment bearer in both arms. Unset TOKEN_FILE.
Turn each input into the existing capture case shape: set `mode` to `lease-run`
for timeout overflow and `lease-list` otherwise, set `environment_deltas` to
unset `PSEUDOLIFE_MCP_TOKEN_FILE` and set `PSEUDOLIFE_MCP_TOKEN` to the peer's
synthetic bearer (or unset it for timeout/C3). Replace the timeout child argv's
`$PINNED_PYTHON` with the actual absolute pinned interpreter. Use
`cli_process.observe(case, command, commands, root=oracle_root, home=home,
url=url)` for each reset arm. The existing captures retain raw response, complete
environment, pre/post files and request bytes **before** applying a policy.
For C3, use the exact extracted `reset`/`direct` functions in
`retained-closed-stdout-capture.py`: inject `commands`, `url`, `root`, `os`,
`shutil`, `base64`, `subprocess`, `contextlib`, and the committed
`checked_files`, `fixture_env`, `file_path`, `snapshot` into their namespace.
Invoke `direct(case, command, home, False)` for the exact ordinary control,
then `direct(case, command, home, True)`; the pipe reader is closed **before**
launch. These function ASTs are exact producing source, not a later adapter.
All fixture processes/threads/homes must be reaped and checked after captures.

Apply `traceback_response(oracle, candidate, terminal)` to the ten I3 cases,
timeout overflow and C3, preserving the exact platform terminal (C3:
Windows OSError22, Linux BrokenPipeError32), exits, streams and state outside
the validated Python traceback header/frame span. For the two extreme text
IDs only, invoke `extreme_list_response(id, oracle, candidate, expected_stdout)`
with the complete independently specified fixture lines in `policy-inputs.json`.
Construct the lock-directory string with `str(home / '.pseudolife-mcp/locks')`,
use the actual peer URL and join every line with the platform newline, including
the final newline. Windows responses are raw-exact; Linux
retains Python OverflowError exit 1/native exit 0 with `?`, empty stderr and
unchanged state. This is the explicit two-cell `python-traceback-not-contract`
rule. Both JSON companions preserve the numeric timestamp and full response
exactly. Every other list exit difference fails. The private producing adapter
and actual instrument hashes remain distinguished from this later offline guard;
no standalone public policy-capture CLI or hosted execution of that adapter is claimed.

The focused rejecting control is reproducible without fixtures or candidate:

```text
$PINNED_PYTHON -B -m pytest --noconftest evals/rust_port/test_lease_extreme_list_exit_policy.py -q
```

The existing hosted lane separately runs the full general and forbidden-header
corpora and both feature configurations. Header reproduction is
`$PINNED_PYTHON -B -m evals.rust_port.lease_headers --root $CANDIDATE_ROOT
--candidate $IMAGE --out $OWNED_NEW_RECEIPT`; its existing `--case` selector
can select retained D3 cells. Final hosted re-execution is pending. The eight
genuinely routed original help/version nodes are separate from original direct
lease tests, which remain Python baselines rather than Rust-routed proof.
Normal hosted Linux full/lite lanes through `ops/ci_tests.sh` supply those
unchanged Python baselines on PRs; their outgoing-head status remains pending.

## Retained limits and failures

UTF-8/platform newlines, local-date flooring, unsupported-date `?`, unchanged
JSON numbers and the POSIX-TZ/Windows-OS-timezone scope remain explicit in
PARITY/PORTING. Locale/default-encoding and Windows CRT-only TZ overrides remain
deferred. Full mode, installation and four Phase 4 actions remain deferred.

Earlier forbidden-header exit/effect RED cells, malformed-URL and partial reply
review counterexamples, old-base Windows help/parser failures and stale CRLF
assets remain historical. Canonical LF export fixed the cached-asset problem
without output/oracle/assertion normalization. Mounted POSIX token-home setup
failed earlier. Frozen68's Windows clock cell matched, but its whole driver
failed an assertion; its continuation also failed, and neither whole run passed.
The first final Linux policy launcher failed at Git before any fixture/candidate;
the established interop profile then completed the actual receipts. Those
failures do not become final passing executions. Historical unsupported timing
claims stay removed; only the committed samples above support current metrics.
