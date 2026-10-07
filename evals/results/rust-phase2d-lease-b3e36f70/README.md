# Reduced native lease core: source-bound local evidence

This bundle records actual Windows/Linux executions on
`b3e36f707d4e8b038c258b5378c064d62b4d977a`, tree
`6730038c3e4b40399c6493060293670ffca9be61`. The later publishing commit adds
only evidence and documentation; runtime, assets, Cargo inputs and tests remain
byte-identical to b3. Final hosted acceptance and fresh independent review are
pending. **CLI-LEASE-core(check/list/run) and full CLI-LEASE remain deferred.**
`hold`, `break`, `delegate` and `designate` each refuse with exit 1 and
`deferred in this candidate`; the candidate loses those actions until Phase 4.

| Artifact | Actual observed scope |
| --- | --- |
| [Source/image bindings](source-image-binding.json) | Canonical committed Rust blobs, executed head/tree, immutable images and debug build recipe |
| [Affected checks](affected-checks.json) | Both-OS fmt, both feature configurations of Clippy, targeted native tests; exact commands, exits and log hashes |
| [Functional and policy evidence](functional-policy-evidence.json) | Both-OS successful child, raw exit 3, four refusals, release/reacquisition, 192 headers and 16 admitted policy cells per OS, four paired duration/auth cells, Windows Ctrl-C and cleanup |
| [Measurement evidence](measurement-evidence.json), [recipe](MEASUREMENTS.md), [invocations](producing-invocations.json) | All four actual check/list cells, full samples, repeat quantiles/floors and exact unchanged producing controllers |
| [Policy inputs](policy-inputs.json) | Exact synthetic argv/files/raw malformed replies and extreme timestamp lines |
| [Producer inventory](PRODUCER-INVENTORY.md) | Supported producers, removed interpreter grammar and named substitutions |

The genuine oracle is Python 3.11/pseudolife-mcp 0.17.0/schema 55 at
`3c01bb31abd60178e15dea99adda369b4bbf92fc`: Windows Python 3.11.9 and Linux
3.11.15. Header instrument receipts retain their independently pinned oracle
identity; their production source remains the required phase pin. Source/image
hashes must describe the actual image, not an arbitrary rebuild.

All four measurements use the established bare debug layout, three repeats of
ten alternating starts per arm with equal image warmups: 240 timed starts,
24 warmups and 16 rejecting mutations. Repeat floors are descriptive ranges,
not confidence intervals. Successful run is functional only; no release or
installed performance claim is made. The producing controller/helper bytes are
unchanged and included here. Actual portable invocation records distinguish
machine-path aliases from the original private command hashes.

## Existing producing APIs and fixture requirements

Run from the genuine pinned oracle/runtime, with candidate eval modules selected
from `$CANDIDATE_ROOT`; for strict producing-controller reproduction, check
out executed b3 and supply its exact HEAD/TREE rather than the later
documentation-only publishing head. Call `require_phase1_source`, `require_import_root` and
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
in the [historical helper directory](../rust-phase2d-lease-7e6927b7/). Load `phase2d-lease-windows-ctrlc-capture-v3.py` and call
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
`shutil`, `base64`, `subprocess`, `contextlib`, `locked` (if a selected case
uses held files), and the committed
`checked_files`, `fixture_env`, `file_path`, `snapshot` into their namespace.
Invoke `direct(case, command, home, False)` for the exact ordinary control,
then `direct(case, command, home, True)`; the pipe reader is closed **before**
launch. These function ASTs are exact producing source, not a later adapter.
All fixture processes/threads/homes must be reaped and checked after captures.

For the ten I3 cases, apply the approved `http-reply-not-understood` target:
exit 1, empty stdout, `lease: HTTP_REPLY_NOT_UNDERSTOOD` plus the platform newline,
unchanged pre-state and no request after the response. Preserve the raw Python
response before comparison; do not remove its surrogate bytes or traceback.
For timeout overflow, require native exit 1, empty stdout,
`lease: timeout is out of range` plus the platform newline and unchanged state.
For large closed-stdout list C3, require exit 1 and the unprefixed
`stdout write failed` plus the platform newline, with no captured stdout and the
same post-state; its ordinary open-output control remains raw-exact.
These targets implement only the named substitutions in PARITY. Mutate exit,
stdout, stderr, state and requests separately; each unrelated mutation fails.

For the two extreme text IDs only, invoke
`extreme_list_response(id, oracle, candidate, expected_stdout)` from
`evals.rust_port.lease_policy_preparation` with the independently specified
complete lines in `policy-inputs.json`. Construct the lock directory with
`str(home / '.pseudolife-mcp/locks')`, use the actual peer URL and join with the
platform newline including the final newline. Windows is raw-exact. Linux
retains Python OverflowError exit 1/native exit 0 with `?`, empty stderr and
unchanged state under the exact two-ID `python-traceback-not-contract` rule.
Both JSON companions retain numeric timestamps and full responses exactly;
all other list exit differences fail. There is no generic exit waiver.

For the four duration/non-ASCII cells, select the existing
`evals.rust_port.lease_corpus.cases` IDs `lease-run-wide-duration-timeout`,
`lease-run-wide-duration-expect`, `lease-run-wide-duration-ttl` and
`lease-check-nonascii-bearer`. Run reset paired arms with `cli_process.observe`
and apply `lease_corpus.expected_response(case, oracle)` to the full response.
The three finite 32-digit duration inputs remain exact; only the named reason
bytes change in the non-ASCII bearer case. Exit, other bytes and state remain
exact, and separate output/state/request mutations reject.

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

## Retained failures and limits

The accepted malformed JSON, suite ISO, interpreter digit-cap retirement and
native diagnostics are narrow substitutions; no generic traceback or exit
normalization is admitted. Encoding is UTF-8/platform newlines. Local timestamp
flooring, unsupported-date `?`, unchanged JSON numbers and POSIX-TZ/Windows OS
zone scope remain in PARITY. Locale/default encoding and Windows CRT-only TZ
overrides, installation and full mode remain deferred.

The Windows policy receipt is an explicit composite: thirteen accepted cells
from a failed batch plus corrected C3 and two JSON companions from a passing
continuation. The failed whole batch remains failed. Its private adapter had
incorrectly expected a diagnostic prefix; runtime was unchanged. An accidental
old-adapter replay also failed and contributes no duration evidence. A Ctrl-C
launcher hit a historical filename collision before candidate launch; the
unique-label continuation passed. An initial Linux CRLF shell wrapper failed
before launch; the retained LF wrapper subsequently launched actual receipts.
These are separate observations, never relabelled passing whole executions.

Earlier unused-import/test-lint and new additive Windows fixture failures are
retained. The final fixture explicitly uses blocking accepted sockets and exact
quoted deferred-action text; no runtime or old assertion was weakened. Old
7e/04 measurements, auth review RED cells, frozen68 whole-driver/continuation
failures, cached-CRLF help failures and mounted POSIX private-home setup failures
remain historical in the [earlier bundle](../rust-phase2d-lease-7e6927b7/README.md)
and private raw records. All final owned PG/daemon/child/helper/parent processes
were absent at cleanup; raw auth/home state remains private. The core artifact includes each actual
invocation window and only the extracted decimal `/t` values; full raw response
hashes remain bound without publishing credentials or personal paths.
