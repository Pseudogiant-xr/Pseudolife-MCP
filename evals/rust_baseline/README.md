# Python baseline instrument

The preferred phase-0 evidence is the r5 capture with verified runtime ownership
and child runtime provenance:
[daemon](../results/rust-rewrite-baseline-daemon-20261003-r5.json),
[shim](../results/rust-rewrite-baseline-shim-20261003-r5.json), and
[hosted CI](../results/rust-rewrite-baseline-ci-20261003-r5.json).
The local captures select Python commit
`136a34ae95e981a691fcc31ba9fb4f35d83d4249` and schema 52 through an explicit
source root. Hosted CI records each observed run's own immutable head.
The separate [real daemon smoke](../results/rust-rewrite-baseline-daemon-smoke-20261003-r5.json)
checks schema 52, owned-instance readiness and disposable cleanup; it is not a
performance baseline.

Earlier provisional captures are preserved outside this candidate and superseded
by the corrected readiness, process ownership and runtime provenance instrument.
The r5 local captures used an explicit virtual-environment interpreter; their
child observations record package 0.15.0 and MCP SDK 2.1.0. Windows and WSL
full-suite locks were independently checked while board access was unavailable;
the receipts do not claim a coordinated board check. The original daemon
capture was additionally excluded because peer CPU work overlapped
`2026-10-02T19:25:57Z`–`19:27:03Z` and `2026-10-02T19:27:46Z`–`19:28:09Z`.
Ambient OS activity remains uncontrolled, and write timings have larger
repeated-control spreads than search or mail. Use each artifact's noise floor
when assessing future deltas; these captures make no Rust speedup claim.

Run from the repository root with the explicit interpreter executable in the
prepared Python virtual environment used by the oracle. An unqualified `python`
may select another installation. The commands below use `python` as a placeholder
for that executable.
Output is allowlisted public metadata and raw numeric samples; temporary daemon
logs, DSNs and agent credentials remain inside the disposable private directory.

```console
python -m unittest evals.rust_baseline.test_baseline
python -m evals.rust_baseline.shim --smoke --out evals/results/rust-rewrite-baseline-shim-smoke.json
python -m evals.rust_baseline.ci --count 8 --out evals/results/rust-rewrite-baseline-ci.json
python -m evals.rust_baseline.daemon --smoke --out evals/results/rust-rewrite-baseline-daemon-smoke.json
python -m evals.rust_baseline.shim --repeats 3 --samples 10 --out evals/results/rust-rewrite-baseline-shim.json
python -m evals.rust_baseline.daemon --repeats 3 --samples 40 --bank-size 128 --out evals/results/rust-rewrite-baseline-daemon.json
```

The shim smoke is small enough to prove the CLI/HTTP/artifact/cleanup path while
other work runs. Every non-smoke local measurement and even the daemon smoke
requires a successful `full-suite` resource check. The CLI's structured result
must exit zero, report a free local lock and report no active holder. An available
busy board or busy local lock always refuses the run. If CLI board access is
unavailable, pass either the UTC timestamp of an actual separate board check with
`--board-checked-at`, or a truthful independent local/WSL lock and peer workload
clearance with `--offline-resource-checked-at`. The options are mutually exclusive.
The offline receipt explicitly states that the board was unavailable and never
claims a board check. Resource checks are observations, not a reserved OS lock;
other operators must maintain the serial CPU slot. CPU use before each daemon
repeat is recorded before its model load, corpus seed and warmup. Keep smoke
separate from quiet baseline evidence. Existing artifacts are never overwritten.

The shim instrument times a fresh real CLI process from spawn through stdio
initialization, including discovery, credential resolution and proxy startup.
Its upstream is a loopback fixture; spawn fallback and coordination attachment
are disabled. The OS file cache is warm. This is a named baseline configuration,
not the default deployment startup cost.
The stdio client owns the complete subprocess Job or process group from launch
and reclaims it after both normal root EOF and timeout. Cleanup confirmation
requires the owned subtree to stop and both stream readers to finish.

The daemon instrument starts the real Python daemon on a newly minted
`plbench_` database, using the existing production and server identity guards.
It seeds deterministic generated entries through `memory_store`. Each repeat
uses a new bank and identical requests. Embedding uses real Qwen weights on CPU,
torch fp32, one model thread, default cache size; eight warmed query texts cycle
through the search phase. Store and fact writes then grow the synthetic bank.
Mail times durable HTTP send, immediate receive and their roundtrip; acknowledgment
runs outside timing. Dream and release checks are disabled. RSS is sampled at
idle and every 20 ms during sequential search; it excludes PostgreSQL.
Daemon RSS starts at the verified runtime process and includes its descendants;
the Windows interpreter redirector is excluded. Shim RSS retains its complete
launcher-rooted process tree, including the redirected runtime when present.
The instrument verifies a per-launch secret and owned runtime PID in the real health
callback before yielding, then reclaims the owned process tree and drops only its
minted database. Readiness secrets and runtime PID are omitted from receipts.
Windows may briefly retain a closed log after process cleanup, so scratch removal
has a bounded filesystem-only retry outside all latency and RSS measurement.
This does not test graceful shutdown or durability.

Quantiles use nearest rank. A separate statistical median is reported. Identical
repeat control blocks report the range of their medians as a descriptive noise
floor; one repeat has no noise estimate. Differences smaller than the observed
floor are not findings. This is not a statistical confidence interval.

The hosted CI capture is read-only and selects successful completed master runs.
It stores immutable heads, workflow blob identities, links and each job and step
duration. Created-to-last-job includes scheduling; job span excludes initial queue
time. Jobs run in parallel and are never summed into wall time. Varying revisions
and hosted hardware make this observational evidence. Same-head repeated runs
are needed for a CI noise floor; the script makes no speedup claim when absent.
The provenance host describes the local CI collector, not a job runner. Each
job records public runner labels observed through the Actions Jobs API when
available; runner hardware remains unknown. Custom labels and runner names are
omitted. Offline reanalysis preserves the original collector provenance as input
history and does not treat its host label as runner hardware evidence.

The baseline artifact's `provenance.source_head` records the checkout actually
selected, and new receipts include `provenance.source_schema`, read from that
checkout's schema declaration. `source_dirty` states whether any tracked or
untracked work exists there; instrument hashes identify the scripts that ran.
Daemon repeats separately record the schema observed through health. The
top-level `schema: 1` is the artifact format version, not the bank schema.
Historical receipts without `source_schema` retain their observed daemon schema
and immutable commit; do not relabel them after updating the instrument.

To prepare the pinned Python source without changing the instrumentation checkout:

```console
git worktree add --detach ../python-baseline-oracle 136a34ae95e981a691fcc31ba9fb4f35d83d4249
```

The daemon command can keep the instrument separate: add
`--source-root ../python-baseline-oracle`, with private bench configuration
prepared according to `CLAUDE.md`. Reusable callers pass `source_root=oracle_root`
to `launched_daemon()` and `provenance()`. The launcher executes this instrument's child file with the
selected checkout first on Python's module path and as the child working
directory. The corrected real smoke validates that routing. Shim measurements also
accept `--source-root ../python-baseline-oracle`; the fixture remains instrument
code, while the launched CLI comes from the selected source checkout. Keep the
selected oracle fixed for a comparison.

New provenance labels instrument-process metadata as `parent_runtime`. Each
daemon repeat records `actual_child_runtime` from its identity-verified health
callback. Shim samples record an untimed metadata probe using the identical
interpreter, environment and selected source root; this is a separate child
observation. Installed distribution versions, package runtime version, executable
digest and selected-source match are recorded without personal paths. Parent
metadata does not establish child metadata: isolating APPDATA can hide a user-site
installation and expose another installed package version.

Windows virtual-environment interpreters can redirect to a contained CPython
process with a different PID. Readiness therefore requires native membership in
the launcher's owned Job, plus the per-launch secret, healthy status and live
owner/runtime. POSIX uses its owned process group. An unrelated listener cannot
satisfy the ownership proof; runtime PID and readiness secrets remain private.

Source digests hash exact raw checkout bytes, including line endings, rather than
normalized Git blobs. `process_helper_sha256` covers the shared process ownership
helper and its actual imported native binding file; `runtime_provenance_sha256`
identifies the shared metadata observer and current-source bootstrap. On the measured Windows
checkout, `codex_doorbell.py` had CRLF bytes; its committed Git blob has LF bytes.
The [source reconstruction manifest](../results/rust-rewrite-source-reconstruction.json)
provides canonical Git blob IDs/hashes and exact CRLF line ranges for every
source binding in baseline R5, full-bank R5 and selfchecks R4/R5. Read each object
with `git cat-file blob <git_blob_oid>`, verify its canonical hash, replace LF
with CRLF only at the listed one-based inclusive line ranges, and verify the
recorded raw hash. Other bytes remain unchanged; the baseline instrument files
used LF bytes. Do not normalize all files or treat a different digest as equivalent.
Historical captures remain unchanged.

The same recipe applies to capture bytes: `receipts` covers the seven execution
receipts, and `referenced_artifacts` covers the six full-bank/selfcheck corpus
and oracle artifacts. Reconstruct each from its Git blob before checking its
historical `sha256` (also named `recorded_raw_sha256`). The four baseline receipts
use CRLF on every terminated line; all nine port artifacts use LF. These explicit
recipes work on a clean Linux checkout without Windows checkout filters.
Source bindings resolve by path plus raw digest. Different selfcheck versions
retain older source recipes with their declared `source_commit`, while this
baseline and the full-bank capture remain unchanged.
