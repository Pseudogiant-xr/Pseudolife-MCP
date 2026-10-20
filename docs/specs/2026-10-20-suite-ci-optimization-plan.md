# Faster test suites and CI without weaker coverage

Test feedback is delayed by sequential parity checks, resource-limited pytest
workers, and expensive fixture cleanup. This plan separates those causes and
defines small implementation PRs that can demonstrate a shorter feedback cycle
without dropping tests, sharing mutable banks, or relaxing failure checks.

Status: proposed implementation plan, 2026-10-20. This document changes no
workflow, launcher, fixture, machine configuration, or required check. Merging
it does not authorize a deployment, live-bank operation, or ruleset change.

## Evidence and limits

Source inspection used commit
`2092e7b1b40670c00bde3affaec4dcdbe530dc32`.

- [Completed Python CI run 39353345491](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/39353345491)
  shows test execution dominating installation in the main Linux lane. Its
  `test-diagnostics` artifact includes process accounting, sampled CPU/I/O,
  runtime versions and JUnit durations. That lane uses real embeddings; the
  lite lane and default local suite are not interchangeable comparators.
- [Completed Rust run 39349469222](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/39349469222),
  head `67aff7c3b207208b17cd54d2fe37aaec10178224`, shows the Windows CLI parity
  job executing several substantial harness families serially. Queue delay
  also contributes to time until the aggregate checks complete.
- [The existing worker/fixture change](../../changelog.d/2026-10-19-wsl-suite-xdist-workers.md)
  records earlier checkpoint and file-churn measurements. Those are historical
  evidence, not a fresh profile of a current remote suite.
- Current local/remote launcher defaults already use up to four workers with
  `--dist loadfile`: one worker per two CPUs and per 6 GiB of effective memory,
  capped at four. The remote dispatch default is a 16 GiB scope, which selects
  at most two workers. Explicit pytest arguments currently replace this default.

Low aggregate CPU use alone cannot distinguish idle workers, one busy core,
database locks, storage waits, process startup, sleeps, or host contention.
The investigation found candidates, not a measured speedup from this plan.
Retain a sanitized evidence artifact before hosted artifacts expire; any new
published performance number must follow [eval evidence rules](../../evals/README.md)
and add its backing check to `tests/test_eval_evidence.py`.

## Success and safety criteria

The objective is shorter time from an eligible revision to completed required
checks, with the same semantic coverage. Record queue time, setup, execution,
artifact upload and aggregate-gate delay separately, plus total runner minutes.
Do not optimize job duration while making queue contention or total cost worse
without an explicit trade-off decision in the implementation PR.

Every implementation preserves:

- The test/case multiset, platform coverage, live and golden modes, negative
  controls, candidate/oracle revisions, and expected failures.
- Required status names and fail-closed aggregation: missing, failed, cancelled
  or unexpectedly skipped work cannot produce a successful gate.
- One full suite per machine and the existing homelab capacity policy; more
  workers inside one suite do not create another suite slot.
- Disposable test PostgreSQL, per-run/per-worker names, production-bank refusal,
  CPU-only execution, resource leases, and cleanup of owned resources only.
- Durability and recovery assertions. No global `fsync=off`, weakened isolation,
  widened timeouts, blanket retries, or moving required coverage to nightly jobs
  as a substitute for removing avoidable work.

The current [repository validation rules](../../CLAUDE.md#running-tests) govern
each implementation PR, including local full suites for shared infrastructure,
native platform checks and independent review. The maintainer owns merge.

## Delivery order

| PR | Scope | Dependency | Deliverable |
| --- | --- | --- | --- |
| A | Comparable profiling and evidence | None | Repeatable baseline and resource attribution |
| B | Split Rust CLI parity families | A's inventory and hosted baseline | Balanced isolated jobs with complete aggregation |
| C | Preserve launcher concurrency with ordinary flags | Current launcher contract tests | Explicit, tested selection semantics |
| D | Reduce database fixture churn and cleanup waits | A identifies the costly operations | Small verified fixture change |
| E | Tune workers and schedule expensive files | A; repeat after D | Measured settings for each environment |
| F | Remove unnecessary dependency barriers and setup cost | B and fresh timing evidence | Smaller CI critical path without weaker gates |

B and C can be developed independently. Do not combine them with D: workflow,
argument parsing and database isolation failures need separate attribution.
E must be remeasured after fixture changes because the resource balance changes.

## A. Establish comparable profiles

Extend existing tools instead of introducing another profiler:

- `ops/ci_tests.sh`: preserve its exit-code handling, process accounting,
  `vmstat`, pressure snapshots and JUnit output.
- `tests/suite_profile_plugin.py` and `evals/suite_profile_summary.py`: use the
  existing opt-in setup/call/teardown, embedding, schema, subprocess and sleep
  counters; measure profiler overhead before enabling it routinely.
- `ops/wsl-suite.sh`, `ops/wsl-suite.ps1`, `ops/remote-suite.ps1`: retain the
  committed-head receipt and add a comparable opt-in profiling path.

Record an allowlist: commit and tested merge revision, command and worker count,
distribution mode, embedding mode, package/runtime versions, CPU quota/affinity,
effective memory, PostgreSQL version, cache state, test inventory and skips.
Never dump environments, connection strings, hostnames or machine paths into
public artifacts. Include child processes and the separate PostgreSQL process
or container when measuring memory; one process's peak RSS is not suite RSS.

Add a monotonic worker timeline: test phase start/end, worker identity and file.
Join it with test-server checkpoint, lock-wait and I/O measurements. Capture
only allowlisted test-server metadata, not SQL text or production activity.
This distinguishes an idle worker from a worker blocked in fixture teardown.

Compare one factor at a time on the same committed revision and environment.
Start with one disposable end-to-end profiling run to prove capture and cleanup;
then use interleaved baseline/candidate runs to reduce cache and host-load bias.
Use at least three complete repetitions per arm for screening, report all runs
and median/range, and add repetitions if ranges overlap materially. A small
sample supports a provisional decision, not a flake-rate guarantee or a p95.
Analyse a longer hosted history separately for queue trends and tail latency.

Acceptance: capture preserves failure status, adds no unexplained skips, records
the exact inventory, survives failure/cancellation sufficiently to diagnose it,
and emits no secrets. Include a deliberately failing disposable test to prove
the wrapper cannot turn failure into success.

## B. Split the Rust CLI parity job

Primary files: `.github/workflows/rust.yml` and
`rust/ci/test_workflow.py`. Include the workflow's other guard tests in the
dependent-test selection.

Inventory every step conditioned on `matrix.suite == 'cli'`, including setup,
control tests, fixture creation, environment exports, summaries and uploads.
Do not split only the conspicuously slow steps and lose their dependencies.

Proposed initial groups, subject to the inventory and timing evidence:

| Group | Work that stays together |
| --- | --- |
| CLI and leases | CLI differential harness, lease and lease-operator live/golden checks |
| Dream | Cursor checks, cancellation assertions and their negative controls |
| HTTP and security | Static HTTP and security admission parity with their controls |
| MCP | Live MCP protocol cases and controls |
| Schema | Storage test-binary preparation and schema-startup parity |
| Startup | Resident-startup cases, writer-guard and clock controls |

Keep existing W1-C, H5 and other shards intact unless the inventory proves an
explicit migration. Group names are design candidates, not permission to omit
an unlisted CLI step. Start with the smallest split that removes the critical
serial chain; rebalance by measured duration rather than test count.

Each job receives the exact candidate artifacts and oracle checkout chosen by
the existing workflow, its own PostgreSQL instance or proven private fixture,
unique ports/data directories, and artifact names including OS and group.
Keep step-local ordering and fail-fast behavior within a harness. Reuse the
candidate build artifact rather than rebuilding the same binary per group.

Coverage guard: compare the multiset of `(OS, family, case/row, mode, control)`
before and after splitting. A total count alone is insufficient: a duplicate
can hide a missing case. Watch the guard fail when a pair is removed, duplicated,
assigned to the wrong OS, or stripped of a negative-control mode.

Aggregation guard: test successful, failed, cancelled, missing and unexpectedly
skipped shards for pull requests, pushes, merge groups and manual/nightly paths.
Preserve the existing documented scope exceptions and required check names;
do not use `continue-on-error` to accommodate new groups.

Acceptance: exact inventory coverage; candidate/oracle binding intact; guards
demonstrably reject broken matrices; native affected-platform validation and
hosted checks on the candidate head pass; repeated end-to-end timings improve
beyond observed baseline variation with acceptable total runner minutes.
Rollback: restore the previous matrix and conditions together, preserving gates.

## C. Make launcher worker selection predictable

Today arguments such as `-q` replace the default full-suite invocation, including
xdist flags. Fix this as a separate launcher contract change, not a worker-count
increase. Targets: the three suite launchers and `tests/test_wsl_suite_launcher.py`.

Define and test a narrow parser contract before editing the shell:

1. No arguments: preserve the current full-suite defaults.
2. An allowlisted full-suite presentation/diagnostic option (`-q`, `-v`, `-rs`,
   `--durations=N`) preserves automatic workers and file grouping.
3. Explicit worker selection (`-n 0`, `-n 1`, `-n N`, supported long/attached
   forms) wins and is never duplicated. An explicit distribution option also
   remains authoritative.
4. Explicit paths/node IDs, selection expressions, collection/help modes and
   unknown arguments retain pass-through behavior initially. Do not guess
   whether an unknown option consumes the next token.
5. `--` terminates launcher parsing. Missing option values and invalid worker
   settings fail clearly before provisioning resources.

Print the effective command shape, workers and source of the choice using a
non-secret allowlist. Preserve result-receipt argument semantics expected by
`ops/integrate.py`; inspect its admission checks before changing receipts.
Do not silently turn a selected run into a full run or claim a selected run as
full-suite validation. Regression cases cover local and remote forwarding,
quoting, explicit serial mode and every affected existing default.

Acceptance: watched failing/passing launcher contract tests, native PowerShell
coverage where required, and end-to-end proof through the actual launcher.
Rollback: revert the parser change; retain diagnostic evidence.

## D. Reduce database work before adding pressure

Targets: `tests/pg_fixtures.py`, `tests/helpers.py`,
`tests/test_pg_conn_reset_files.py`, and affected fixture-isolation tests.
Use A to attribute schema creation, reset, database creation/drop, backend
termination and checkpoint waits separately. Correlate actual server waits;
long teardown duration alone does not prove a checkpoint caused it.

The current reset already truncates populated tables plus `meta` and rewinds
other sequences in place. Preserve its schema-repair behavior: tests deliberately
damage DDL, so unconditional removal of `ensure_schema` is unsafe.

Evaluate in increasing complexity:

1. Remove redundant initialization only where the calling fixture proves the
   schema and lifecycle already satisfy the required contract.
2. Reuse a fixture within its existing safe scope, retaining function isolation
   for mutable state and private banks for daemon/writer-lease tests.
3. If creation dominates, prototype template-based provisioning on a disposable
   server. Account for template connections, schema/version fingerprints,
   extensions, invalidation and tests intended to exercise fresh creation.
4. If drops dominate, evaluate fewer private-bank lifecycles or cleanup
   scheduling. Any deferred cleanup must close clients, retain an exact owned
   inventory, drain before suite success, surface cleanup failure and recover
   after interruption. Do not simply move time outside the reported test step.

Template provisioning and deferred cleanup are conditional experiments, not
committed architecture. Reject them if their complexity outweighs the measured
gain. Do not share a mutable bank across workers to save setup cost.

Acceptance includes empty/populated tables, sequence resets, schema damage,
failed fixture setup, concurrent workers, cancellation, stale leftovers and
writer-lease cleanup. Preserve production-bank refusal. Run required full-suite
validation under real PostgreSQL and affected platform lanes. Record the server's
file/DDL/checkpoint change alongside complete wall time, not only a fast slice.
Rollback: revert the fixture change, stop only owned processes and remove only
owned disposable resources; never repair a failure by touching the live bank.

## E. Tune concurrency and file scheduling

Keep `--dist loadfile` initially. It preserves module-scoped services and ordered
tests within a file; `worksteal` is not a drop-in safety equivalent. See
[xdist distribution semantics](https://pytest-xdist.readthedocs.io/en/latest/distribution.html).

Compare serial, two-worker and four-worker runs only where memory, CPU quota,
disk space and the machine lease permit. Do not force four workers through a
scope sized for two. An operator must approve any needed machine-budget change;
the plan itself leaves configured limits untouched.

For each arm record summed/peak memory including children and PostgreSQL,
memory pressure, swapping, disk waits, worker idle time, completion time and
all failures. Keep embedding mode and thread budgets fixed for the worker
experiment; investigate OpenMP/BLAS budgets separately afterward.
Audit explicit `PSEUDOLIFE_BENCH_DB` overrides before parallel execution:
an intentionally preserved pin can bypass automatic worker separation.

If the tail is one slow file, consider moving independent tests into separate
files or a duration-aware whole-file schedule. Audit module fixtures, shared
corpora, ordering and reset assumptions first, notably recall and fake/real
embedder tests. Preserve the collected test multiset and observed behavior.
Avoid dynamic CPU-triggered worker creation: it confuses waits with available
memory and makes comparisons and isolation harder to reproduce.

Acceptance: adopt the smallest worker count that provides a repeatable gain
without memory pressure, new failures, unexpected skips or cleanup regressions.
Retain the current defaults when evidence is ambiguous. Rollback is restoring
the prior worker policy or file layout, with the suite lock unchanged.

## F. Shorten CI dependencies and repeated setup

After B, inspect whether Linux parity can begin after Linux artifacts/checks
instead of awaiting an entire cross-OS build matrix. This may require separate
job IDs or reusable workflows: matrix `needs` is a job-level barrier.
Keep final gates dependent on every required OS, even if work starts earlier.

Measure runner availability before expanding matrices further. More shards can
add queue delay, repeat installs and consume more runner minutes. Prefer a small
balanced matrix over one job per test. Preserve concurrency cancellation policy
and event/scope classification; never cancel unrelated required validation.

Only optimize dependency/model caching if fresh profiles show it materially
affects the critical path. Cache keys must identify OS, runtime, dependency and
model versions; a cache miss remains a correct supported path. Keep real-model
coverage and platform-specific installation checks. No persistent self-hosted
runner, ruleset or merge-queue change is implied by this plan.

Acceptance: compare complete workflow readiness, not just an earlier Linux job;
test all event/scope branches and missing artifacts; report queue variation and
runner minutes. Rollback restores dependency edges and cache behavior without
altering required statuses.

## Implementation PR checklist

- [ ] Name the one bottleneck, touched files, unchanged coverage and baseline.
- [ ] State exact before/after commands, revisions, environments and cache state.
- [ ] Include case inventories and tests that reject missing coverage/failure.
- [ ] Run targeted/dependent checks and all repository-required full/platform runs.
- [ ] Report failures and skips separately; a rerun does not erase a failed run.
- [ ] Publish sanitized evidence with any numerical performance claim and guard it.
- [ ] Obtain independent review of the final candidate and record dispositions.
- [ ] Verify current-head hosted checks and define a one-change rollback.
- [ ] Have the maintainer decide merge and any separately required configuration action.

Completion means the relevant implementation is validated and the observed
benefit documented. This proposal alone makes no claim that suites are faster.
