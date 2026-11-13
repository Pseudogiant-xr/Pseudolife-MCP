# Merge batches

Master takes one pull request at a time, and each must pass CI on its head
merged with current master, so every merge sends the other open pull
requests back for a fresh CI run of about an hour. A merge batch merges
several reviewed pull requests into one `integrate/<YYYY-MM-DD>-batch-N`
branch, validates that branch once and merges it once. The rule is in
`CLAUDE.md` ("Running tests", the integration-batch bullet); this page is
the procedure behind it.

## What CI runs where

Each change waits for hosted CI once, on its batch:

- **An ordinary pull request** (any head branch but `integrate/*`) runs the
  fast lanes only. In `ci.yml`: `scope`, `guards`, `lock-resolve` and
  `frontend`; the `test` job and the lite OS lanes are skipped. In
  `rust.yml`: `Scope`, the `Rust` job (format, clippy, builds, nextest and
  the selected early parity replay described below),
  the disposable-image check and the `Parity` gate; the candidate, harness
  candidate and fixtures builds and the parity matrix are skipped. Both `scope` jobs decide this from
  `github.head_ref` (an absent one runs everything), whatever the pull
  request changes; only one whose diff is empty or cannot be read runs as
  a batch would. The required `test`
  check reports its skip, which GitHub counts as success, and the required
  `Parity / <os>` gate runs and accepts the skipped candidate and parity
  jobs only on a pull request its `scope` marked not full; the `Rust` job
  must still pass. A newer push to an ordinary pull request cancels its
  in-flight runs of both workflows.

  Ordinary Rust jobs also replay the committed goldens of CLI rows reached
  by the PR's diff before batching. Python ownership follows command
  modules and their static first-party imports; Rust command ownership uses
  the existing parity map, and unknown runtime inputs select
  conservatively. Ubuntu runs each selected row; Windows runs rows whose
  recorded observations or case sets differ, ignoring only recording clocks
  and arm metadata. Lease, lease-operator, transfer, maintainer and invite
  use an owned PostgreSQL fixture for their database-backed cases. Stale
  records fail on the PR before Nextest. The native candidate shares the
  Rust job's debug dependencies and the daemon it already built for PDF
  provisioning; Python dependencies remain in a separate runtime and reuse
  the existing wheel cache. The job summary and its artifact
  (`pr-parity-precheck-<OS>`) name each failed case with its phase; golden
  replay failures fail `Rust` and its `Parity` gate. Batches retain their
  parity matrix and do not repeat this ordinary-PR path.

  Since the Python freeze (14 November 2026, maintainer ruling) nothing in
  pull request, batch, master-push or scheduled CI compares the candidate
  with a live Python oracle. The report-only frozen judge probes, the
  `judges` and `cli-live` suites, every H5 suite, the Windows `Parity live`
  job, the nightly body evidence and the wait-mail capture are gone; every
  parity shard replays committed goldens, which are now Rust regression
  snapshots (`rust/cli_harness/README.md` "Goldens after the freeze"). The rows whose
  only CI replay was a recording the same run made from the oracle (audit,
  backup, migrate_flat; `rust/ci/parity_scope.py` `UNREPLAYED_ROWS`) are
  not replayed at all. The precheck never records, and
  `rust/ci/inventory.py` refuses a step that starts a live comparison in
  any of those jobs, with one named exception: cli-store's `Daemon
  security live checks without a Rust test` (the startup bind and
  token-map refusals, and the 0.0.0.0-bound Host/Origin and bearer cases)
  keeps its live comparison until Rust tests assert those properties
  (`LIVE_SECURITY_EXCEPTIONS`). The weekly source mutants still compare
  with the frozen Python too.
- **The `Python freeze` check** (its own workflow, `python-freeze.yml`)
  refuses an ordinary pull request whose net change touches
  `pseudolife_memory/` outside `pseudolife_memory/web/static/`, the Console
  build output. It decides on the diff's paths and the exact label
  `freeze-exception`, which marks a change the maintainer ruled an
  exception (the expose security fix, the last release's update bridge);
  it reruns when a label is added or removed or the pull request is
  retargeted. It is early feedback, not the gate: `build` applies master's
  copy of the guard to what each member's merge changes on the batch
  branch, and `open` and `merge` judge the whole batch again with the
  members' labels read afresh. The batch pull request is exempt from the
  check because `build` judged its members, not because they passed it: a
  member's own run can be absent, queued or cancelled. A pull request that
  edits `python-freeze.yml` runs its own edited workflow, which review
  catches; admission does not depend on it.
  Because nothing under `pseudolife_memory/` may change, two routine
  changes are exceptions too while they still edit the package, and carry
  the label, applied by the maintainer or by the conductor acting on a
  maintainer ruling (the maintainer is confirming this): a version cut (the
  0.23.1 cut, #1490, bumped `pseudolife_memory/compose/docker-compose.yml`;
  the `release-procedure` skill says so) and a schema bump, until the
  schema has a Rust-owned source (the Rust job's `gen_schema_sql.py
  --check` builds `rust/daemon/src/storage/schema.sql` from
  `pseudolife_memory/storage/schema.py`).
- **A pull request that edits CI or dependency inputs**
  (`.github/workflows/`, `pyproject.toml`, a lock file, the Rust toolchain
  pin) is ordinary too, since 2026-10-29 (maintainer: only batches spend
  the heavy lanes). It used to run every lane on its own head, so that a
  workflow edit could not switch off its own check. That run used the
  edited workflow as well, so it never stopped such an edit; what it gave
  was a run of the edited lanes before master, and the batch gives the
  same: its pull request runs the edited workflow, on a head containing the
  edit, and a batch is the only way to master. Review is what catches an
  edit that makes a check pass vacuously, then as now. On 2026-10-28 such
  pull requests ran 43 `rust.yml` jobs per push (#1006, #1013, #1014, #1016,
  #1022, #1025, #1026), and batch #1031 sat queued for over an hour behind
  them under the account's 20-job hosted limit.
- **A batch pull request** (from `integrate/*`) runs everything its diff
  needs, exactly as every pull request did before 2026-10-21 (the
  Rust-only and documentation-only scope skips still apply).
- **A master push** skips what its batch already passed on the same code.
  `ops/integrate.py merge` merges a batch with a merge commit only while
  master is its base or an ancestor of its head, so the commit pushed to
  master has the batch head's tree. Each scope job runs
  `ops/ci_batch_passed.py`, which lists the jobs that passed on the batch
  pull request's latest run of the same workflow on that head. Only a job
  whose own conclusion there was `success` is skipped; a skipped, failed or
  cancelled one runs. A Rust-only batch skipped the Python suite lanes, so
  its master push still runs them. Anything doubtful skips nothing and logs
  one line saying why: not exactly one merged `integrate/*` pull request
  into master with this commit as its merge commit, a different tree, the
  newest batch run not completed, or an API error. In `ci.yml` that covers
  `lock-resolve`, `test`, the lite OS lanes and `frontend`. In `rust.yml` it
  covers the disposable-image check and each parity pair (OS and suite);
  when the batch passed every pair the push runs no parity shard (the scope
  job's `parity_any` is false and the `Parity` gate accepts the skip).
  The `Rust` job and the candidate, harness candidate and fixtures builds
  always run, which also keeps master's Rust caches current for every pull
  request. Until the Python freeze the `eval` and `judges` suites always ran
  too, because they compared against an event-selected oracle; no suite
  does now. A merge made
  under the `merge-forward` skill's exceptions skips only what passed on
  the forwarded head itself; with no completed run there, its master push
  runs everything, which is the backstop that skill relies on.
- **The nightly and manual runs** run everything, as before. The nightly
  is the full backstop for everything a master push skipped. Since the
  Python freeze the CLI source mutants run weekly, on the Sunday schedule
  (`23 3 * * 0`, which replaces that day's nightly) and on manual CI; the
  `Parity` gate accepts their skip on a weekday nightly only.
- **Never cancelled:** batch runs, master pushes, the nightly, manual and
  stress runs and merge groups each get a concurrency group of their own
  (their run id), so neither a newer push nor a newer run cancels them,
  running or pending. Only an ordinary pull request's runs are superseded.
- **CodeQL** runs as GitHub's default setup, a repository setting with no
  branch filter, so it still analyses every pull request push (four jobs,
  the Rust one about 8 minutes). It is not a required check. Limiting it to
  batches, master and its weekly run needs the maintainer to switch the
  repository to an advanced-setup workflow.

## Joining a batch

A pull request is ready when it has its reviews and its own local targeted
validation (the touched and dependent tests, recorded in its body), and CI
on its head is not failing. Its selected early snapshot replay must also
have finished without a failure, as described below. Other pending or
absent checks are fine, since the batch's CI is its CI. Mark it with
the `ready-to-batch` label:

```bash
gh pr edit <number> --add-label ready-to-batch
```

The label is the claim that the reviews are done; only someone with triage
or write access can apply it. The script does not check reviews, since
this repository records them in the pull request body rather than as
GitHub approvals, but `status` shows the review decision. It does check
CI for a known failure: a labelled pull request whose current head
has a failing check sits the batch out without a comment and keeps its
label, and joins a later batch once a rerun of the failed job passes or a
push replaces that head; the failing reason names the red checks. One
check name is never counted: `Frozen judge probes (report only) / <os>`
from the `Rust shim` workflow (`REPORT_ONLY_CHECK` in `ops/integrate.py`).
No workflow produces it since the Python freeze removed that job, so it
matters only for a head whose run predates that. The script also applies
the Python freeze itself, with master's copy of `rust/ci/python_freeze.py`
(it refuses to build when master has none): a member whose merge changes
`pseudolife_memory/` outside `pseudolife_memory/web/static/` on the batch
branch without the `freeze-exception` label is dropped with the guard's
refusal, whatever its `Python freeze` check reported; judged on the merge,
not on a merge-base diff, which a criss-cross history can leave empty.
`open` and `merge` then refuse a batch holding any commit that is neither
a member's merge nor the one generated commit, a generated commit that
touches the package, or a member whose label has gone since `build`. Subject to the snapshot checkpoint below, pending,
cancelled, green or no checks admit it: a run cancelled by hand or by a
re-trigger gave no verdict. GitHub also reports a job that ran past its
`timeout-minutes` as cancelled, so the script reads the annotations of
the latest attempt of each cancelled job: the runner's timeout message
counts as failing, since the head likely hangs, and annotations it cannot
read leave the pull request out of this batch as `unverified` until a
retry reads them. A gate job (one that runs under `always()` and needs
other jobs, such as `Parity / <os>`) still runs after a cancel and fails
because the jobs it needs did not pass. The Parity gate's first step,
"A needed job was cancelled" (`if: ${{ contains(needs.*.result,
'cancelled') }}`, `run: exit 1`), fails exactly then and skips the verdict
step, which reaches the same result. The gate counts as cancelled only
when all of these hold: in the workflow file at the head commit, the gate
has that exact step (only `name`, `id`, that `if` and that `run`), every
gate step is a `run` step with a unique literal name, the gate sets no
`defaults`, `env`, `container` or `services`, and its other settings and
the workflow's `defaults` and `env` equal the checkout's copy; that step is,
by position and name, the only one that failed, on every failed leg of the
gate in that run attempt, listed in the rollup or not, except a leg that
ran no step at all in a run attempt that itself concluded `cancelled` (a
cancel that lands while the gate is starting fails it before any step, so
it gave no verdict); no other job in that
run attempt failed, timed out or needs action; and the jobs it needs
include a cancelled one, none unfinished and none that ran past its
timeout. A head that changes the gate's settings therefore stays failing
until that change is on master. A head whose gate has
no such step, from before the step was added, stays failing, because there
a broken verdict script and a cancel fail the same step. The script reads
the head's workflow because the API does not name the merge ref GitHub
ran a pull request's workflow from, and refuses the downgrade when the run
lists a job that file does not define. A failure the checkout's own
workflows define no gate for is failing without any API read. Any other
gate failure is failing, and a run, job or step list it cannot read is
`unverified`, naming the read that failed. `merge` still counts a
cancelled or failed check on the batch itself as red.

### Own-head snapshot checkpoint

`status` and `build` read the exact `Replay touched PR goldens` step in
both `Rust / ubuntu-latest` and `Rust / windows-latest` jobs from
`.github/workflows/rust.yml`. The collector uses structured Actions job
and step metadata, never log output, and verifies the PR head SHA, workflow
path, pull-request event and latest run attempt. Similarly named checks
and results from another head or workflow cannot satisfy the checkpoint.
Each replay must be completed with a passing or skipped conclusion;
a skip after a prerequisite failure waits for the job's verdict, since
report-only setup steps can fail under `continue-on-error` when no goldens
are selected on that OS. A passing job satisfies that skip; a failing job
is red. Cancellation before replay leaves it pending. Remaining Rust
steps and other CI may still be running.

The frozen batch base's early-replay selector determines whether any
changed file reaches snapshot rows, using the net merge-tree diff against
that base just as CI does. Runtime edits already present on master do not
turn a remaining docs-only or test-only diff into a replay requirement.
Conflicting heads follow the existing merge/drop path rather than wait for
replay evidence that GitHub cannot produce: budget-only conflicts retain
the existing normalization and use the batch's replay, and other conflicts
are dropped. Documentation accompanied only by the generated
`rust/producer-census.json` follows CI's docs-only skip; census alone or
alongside runtime edits still requires replay. A diff reaching none satisfies
admission immediately, without Actions metadata: documentation and pure
test-only changes do not wait. Tests imported by a runtime harness are
runtime inputs and retain their replay requirement. The selector code
comes from the frozen base and reads checkout files as text; it never
imports a member's Python code. Unknown inputs select conservatively.

Pending, red or unreadable required replay evidence leaves the PR out
without removing its label. A hand-cancelled run needs a push or a rerun of
the Rust workflow. A completed run missing the exact step needs a head
containing it (merge master forward and push), or a rerun if that head
already defines the step. Unreadable metadata names gh's cause and can be
retried once that cause is fixed. `open` checks the selected members again
before publishing; a newly pending or red replay refuses publication,
and a changed head needing replay requires a fresh build. When the batch
carries the first admission-aware opener but its base's builder predates
admission, `open` computes the missing admission data for each held member
and saves it only after publication succeeds. Required replay still must
pass that check; bootstrap does not waive it. A plan missing admission
fields from an already admission-aware base still needs rebuilding. The
snapshot checkpoint does not replace the batch's full parity gate.

Why not wait for all green CI on the member's own head: on 2026-10-20/21 four
things held batches up (#877 broke only against a newer master; an
h5-background harness race, fixed by #883; a stale producer census; #875's
dependency lock that could not resolve), and green CI on the member's own
head caught none of them. Each was caught by the batch, by its local checks
or by a reviewer's targeted run, so waiting for a member's own CI only
doubled the wait.

## Building one

Build a batch when no batch is open and two or more pull requests are
ready, or when a lone ready pull request has waited about one hour (one
hosted-run cycle) or is urgent (a revert, a red-master fix) or a release
cut, because a batch of one pays a full hour of hosted CI for one change
and no pull request from any other branch is merged directly (maintainer
decision 2026-11-09). One batch is open at a time; `build` and `open`
refuse while any pull request from an `integrate/` branch is open, and
`build` enforces only that none is open: the wait is policy.

A release cut (a member whose own diff changes `project.version` in
`pyproject.toml`) is batched alone: parity tests a cut against its own head
only when `release_cut()` in `evals/rust_port/oracle_selection.py` accepts
it, which it never does with code beside it. When a ready cut is listed,
`build` merges only the first cut and leaves the others out, keeping their
labels, for the next batch.

```bash
python ops/integrate.py status
python ops/integrate.py build --dry-run
python ops/integrate.py build            # with the Rust compile check when rust/ changed
PSEUDOLIFE_SUITE_WHERE=local pwsh ops/remote-suite.ps1 > <log> 2>&1
python -m pytest -q -p no:cacheprovider --require-databases <the RUST_HARNESS paths build prints> > <log> 2>&1
python ops/integrate.py open --suite ~/.pseudolife-mcp/suite-results/<run>.json \
  --harness-note "<count> passed at <head short sha>"
python ops/integrate.py merge   # once its CI is green and it has its review
```

- **`status`** lists the labelled pull requests in batch order, each with
  whether its head merges cleanly into master (`git merge-tree`, no working
  tree touched), its CI on that head and its review decision.
- **`build`** needs a checkout with no uncommitted tracked changes whose
  HEAD is `origin/master` (`git switch --detach origin/master` in a fresh
  worktree). It creates the next free local `integrate/<date>-batch-N`
  branch, merges each labelled head with `git merge --no-ff` in the order
  the label was applied (pull request number when that is unknown), and
  leaves the checkout on that branch. It pushes nothing, so no CI has
  started, and leaves the drop comments to `open` (unless no batch forms,
  below); its plan waits in the worktree's git
  directory for `open`. When the batch changes anything under `rust/`, it
  last runs the Rust compile check (below) and exits 1 if that fails.
- **`--dry-run`** does the same merges on a detached HEAD, prints the plan,
  the body and the comments `open` would post, then switches back; it
  creates, pushes and comments on nothing, and only says whether `build`
  would run the Rust compile check.
- **`open`** publishes a built batch once the local full suite passed on
  it. `--suite` takes the result JSON `ops/remote-suite.ps1` writes and
  refuses unless it tested exactly the batch head and exited 0;
  `--suite-note` takes the result in words for a run that wrote no JSON
  (`ops/wsl-suite.ps1` alone); the note must name the head's short sha,
  and the body says it was not checked. A result for a selected run, or
  one from a launcher too old to record its pytest arguments, is refused.
  `--harness-note` takes the Rust harness run's result in words (next
  section); it must name the head's short sha and state pytest's
  `<count> passed` with no failed or error count, and the body records it
  beside the suite as not checked. Before the push `open` refuses a plan whose Rust compile
  check failed or did not run, or that predates the check, and records a
  pass in the body. It also re-reads each
  member's own CI and refuses if one turned red since `build`, naming it:
  rebuild, and it sits out. If any step after the push fails, rerunning `open` with the same
  arguments resumes on the batch's own pull request without repeating a
  comment, even if master has moved since. Before the batch pull request
  exists, it refuses if the branch gained commits or master moved since
  `build`. It
  then pushes the branch, opens the batch pull request titled `integrate:
  <date> batch N (#a, #b)`, whose body lists each member's merged head,
  review link and CI plus the suite and Rust harness results, and comments
  on each dropped member.

Drafts, heads with failing CI and heads that moved after listing are left
out without a comment. A member that conflicts is dropped: the script
aborts that merge, and `open` comments on the pull request naming what it
collided with (master itself, or the earlier members that touch the
conflicted files) and removes its label, so the next batch does not pick
it up again; a dropped member whose head moved since `build` keeps its
label and gets no comment. Its author merges master forward once the collision has
landed, reruns its targeted tests, and re-applies the label. If no member
merges cleanly, no batch is built, the collisions are reported at once and
the local branch is deleted.

The producer census (`rust/producer-census.json`) is derived data: `python
rust/producer_census.py` writes it from the tracked files it scans. A pull
request that committed a regenerated census went stale whenever master
changed any of those files, and had to merge master forward and regenerate
before a batch would take it (#960 and #974 three times each on
2026-10-26). So no pull request regenerates it. After merging the members,
`build` runs the batch head's census script and refreshes CI budget
fingerprints from workflow definitions parsed as data. Changed generated
files share the batch's one generated commit (`Regenerate producer census
and CI metadata for this batch`); no member code is edited. This extends
the derived-data exception to "nothing is fixed inside a batch"
(maintainer, 2026-10-27; CI metadata extended 2026-11-05). The batch pull
request names that commit under `## Generated CI metadata` and lists any
trusted manual shape the new census
withdrew because a member changed its source (`manual_drift`), since no
member's own diff shows it; review those and refresh their fingerprints in
a follow-up pull request (`rust/producer-census.md`).

CI's Rust jobs run `python rust/producer_census.py --ci-check`. It fails a
stale census on an `integrate/*` pull request whatever its diff, and on a
master push or merge group whose diff changes the census inputs (the census
module's `CURRENCY_INPUTS`: the census, its script and the port inventory).
An ordinary pull request's drift is only reported, so master moving never
turns a member's own CI red. A batch whose census `build` regenerated
changes the census, so the master push that merges it checks it again.

If the census script fails on the batch head, `build` drops the members
whose own diff changes the census inputs, naming the failure, and merges
the rest again from master; `open` comments on and unlabels each, as after a
conflict. If no member changes the inputs, or the script still fails
without them, `build` refuses and names where to look: master, this
machine or a member's head. Before
the push, `open` runs CI's check on the batch head and refuses if it would
fail, so a plan built without the regeneration cannot reach CI.

The census script is member code, so `build` (including `--dry-run`)
executes it on this machine, as the full suite will. It runs with only the
environment Python and git need to start, without tokens such as
`GH_TOKEN`. The list of inputs is read from master's copy of the script and
the drift from the committed census files, parsed as data, so no member
code runs inside `ops/integrate.py` itself. With no census on master
no census is regenerated.

CI lane reasons live in independent `rust/ci/ci_budget_accept/*.json`
fragments, written by `python rust/ci/ci_budget.py accept`. Authors leave
`rust/ci/ci_budget_fingerprints.json` unchanged. Before merging a member,
`build` checks that each changed job has a reason binding its prior
baseline and current definition; regenerating fingerprints cannot supply
that reason. The reader and legacy parity resolver come from the frozen
master commit, never a member's executable code. Scope helper source from
members is hashed as data. After merging, `build` derives the combined
baseline without changing authored minutes or limits.

Branches using the former shared budget format can still join: verified
legacy approvals are retained in an inert `imported-<head>.json` archive.
If only the old shared fingerprint/history fields conflict, `build`
merges the authored budget data and regenerates the derived fields. A
conflicting measured cost, limit or other authored value still drops the
member. A legacy branch changing the parity resolver must first merge
master and use acceptance fragments, since its resolver is not executed
inside the batch controller.

Neither the full suite nor the Rust harness run compiles Rust, so two
members that each compile alone can break the batch together: on
2026-10-28 #991 changed `record()`'s signature, #994 called it the old way,
and batch #1017 failed hosted CI with E0061. So when the batch diff against
master changes anything under `rust/` (which holds every workspace Cargo
file) other than the generated census, `build` runs CI's own compile step on
the batch head, `cargo check --workspace --all-targets --locked` from
`rust/` (where rustup reads `rust-toolchain.toml`), after the census. It
took 48 s cold and under a second warm on the maintainer's Windows host.
A failure drops nothing and leaves the branch and plan as they are, and
`open` refuses the plan. cargo's output cannot tell the batch from the machine (a lock file two
members left inconsistent fails like a download; a missing linker or
exhausted memory prints "could not compile"), so the refusal sends you to
master first: run the same command on `origin/master`. If it fails there
too, the cause is master or this machine (cargo's network, disk, linker or
memory): fix that and build again. Otherwise it is a red batch, bisected
like a red suite ("Validating it", step 3). A check that could not start
(no `cargo` on `PATH`) or was interrupted is refused the same way, as not
run. Once the batch pull request is open, `open` resumes it whatever its
plan says. Notes:

- It honours `CARGO_TARGET_DIR`, so point that at the machine's shared
  target directory to keep it warm; a cold check in a fresh worktree
  otherwise fills its own `rust/target`, so mind the 8 GB free-disk floor.
  If another cargo holds that directory, the check waits for its lock
  ("Blocking waiting for file lock").
- It runs member build scripts and proc macros, as the full suite runs
  member code, with the operator's environment minus credential-shaped
  variables, as the census script runs without tokens: names with `TOKEN`,
  `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`, `DSN` or `DATABASE_URL`, or
  ending in `KEY`, `KEY_ID` or `PAT`, and any value with credentials in a
  URL (a database DSN with its password, or a proxy with one).
- It compiles default features on this machine's platform only. Feature- and
  platform-gated code, clippy and fmt stay CI's, as do the crates under
  `evals/rust_port/` (outside the workspace) and a non-`rust/` change that
  breaks an `include_str!` there (`ops/requirements.lock.txt`,
  `tests/cli_audit_float_repr.tsv`).

The script never resolves a conflict (it merges with `rerere` off, so a
remembered resolution is not replayed either) and never force-pushes. If
the push or `gh pr create` fails, nothing is commented on and rerunning
`open` with the same arguments finishes the job.

## Validating it

1. The local WSL full suite runs on the batch head between `build` and
   `open`, before any CI. With #846's four-worker launcher it takes about
   10 minutes, a fraction of one CI run. Use `PSEUDOLIFE_SUITE_WHERE=local`
   so it runs in WSL on the unpushed branch; a run on the Linux box needs
   the commit pushed.
2. The Rust harness Python tests run on the same head, because the full
   suite collects only `tests/` and never runs the Python tests under
   `rust/`: #877 broke `rust/daemon/harness/test_w2e_lifecycle.py`, passed
   its batch's local suite, and was caught only by a reviewer's targeted
   run. `build` prints the command (`RUST_HARNESS` in `ops/integrate.py`):
   every pytest path of `rust.yml`'s `rust` job (a test pins that none is
   missing) plus the eval shard's `pytest rust/ci`; census currency is not
   among them, since `build` regenerates the census and `open` runs CI's
   verdict.
   - **Some database cases run only here.** Hosted runners have no
     disposable-bank login, so the login cases skip there, and the hosted
     Windows runner has no Linux Docker engine, so the mirror seed's Docker
     cases run only on the hosted Linux runner. This run has the
     test login (`~/.pseudolife-mcp/test-pg.env`, or
     `PSEUDOLIFE_TEST_PG_LOGIN_FILE`) for the bench PostgreSQL at
     127.0.0.1:5433, which must be up, and a running Docker engine for the
     mirror seed tests' throwaway PostgreSQL (Linux containers).
   - **`--require-databases`**, part of the printed command, makes a
     database case that finds no login or no Docker engine fail, naming the
     cause and the fix, instead of skipping, so it cannot pass unseen.
     Start the bench PostgreSQL and Docker before running it.

   These need no built Rust binary; the
   parity shards' other harness files drive the release candidates and stay
   CI's. Run it natively from the batch checkout (a targeted run, about
   5 minutes on Windows with its database cases, measured 2026-11-08;
   `rust/ci/test_workflow.py` needs `pwsh`) in any
   Python with the project's dev dependencies, for example `uv run
   --no-project --python 3.11 --with mcp==2.2.0 --with pytest --with
   psycopg --with psutil --with httpx --with pyyaml python -m pytest ...`,
   and pass its result to `open --harness-note`.
3. A red suite, harness run or Rust compile check is bisected locally:
   test master plus half
   the members, then the half that fails, until one member is left. Drop
   that culprit with a comment on its pull request and remove its label,
   switch back to `origin/master`, remove the local batch branch and build
   again. Nothing is fixed inside a batch; the generated census commit is
   not a fix.

   Some failures only hosted CI shows, because the batch is the first full
   run its members get: the Windows and macOS lite lanes, parity shards,
   clippy and fmt. Attribute the failing job to the member whose files it
   covers (`scope` in both workflows, `rust/ci/parity_scope.py` for a
   parity shard). A Windows-only failure with a non-deterministic
   signature gets one rerun on the current merge ref, naming the test and
   its flake issue (open one if none exists) on the batch pull request; a
   second or deterministic Windows-only failure goes to the Windows dev VM
   (CLAUDE.md "Dev machines"); otherwise drop the attributed member with a
   comment, close the batch and build again. If no member can be attributed, bisect by building
   smaller batches.
4. CI on the batch pull request is the combined check of its members and
   their only full CI; no member is ever merged directly. If master moves
   before the merge, merge it forward only under the `merge-forward`
   skill's exceptions, regenerating the producer census in the merge
   commit as the skill says (CI gates every batch on its currency);
   otherwise close the batch and build the next one.
5. The batch pull request gets the usual review pass before the merge
   click and the ruleset's approving review. Its members were reviewed on
   their own, so the batch review checks that its members and merged heads
   match the body, that no conflict was resolved in it, and any
   conflict-marker warning the body carries.

## Merging it

The batch pull request merges with a **merge commit, never squash**: only
then are the members' head commits reachable from master, so GitHub marks
each member merged. A squash would leave every member open with its work
already on master.

Merge it with the script, on the checkout where `build` and `open` ran:

```bash
python ops/integrate.py merge
```

Every merge here uses the admin bypass, so GitHub would merge an ordinary
pull request as readily as a batch; `merge` is the check it does not
make. It reads the plan `open` left in the worktree's git directory and
merges only the pull request that plan names, and only when all of these
hold:

- its head branch is `integrate/*` and is the plan's branch;
- its head is the head `build` made and the suite passed on;
- the ruleset's required checks (`test`, `Parity / ubuntu-latest`,
  `Parity / windows-latest`) have all reported, so a head where only the
  fast `scope` or `guards` finished never passes;
- the latest run of every check on that head has finished as `SUCCESS`,
  `SKIPPED` or `NEUTRAL` (a docs-only batch skips lanes, a required one
  included, and reports the skip). A run still going, or a required lane
  not reported yet, refuses with "wait"; any other conclusion, `STALE` and
  `TIMED_OUT` included, refuses naming the check. A rerun that superseded
  a failure counts;
- master has not moved since `build`, or the head contains current master.

It then runs `gh pr merge <N> --merge --admin --match-head-commit <head>`,
so GitHub merges nothing but that head, and checks each member by its
current head: `MERGED`, or a head on master whatever the state (an open
one is asked again for up to two minutes while GitHub catches up), is
done. A member that pushed after `build` has commits the batch did not
carry: it stays open with its label and joins a later batch. It prints
the merge commit and the next steps: watch CI on master at that commit,
then free the checkout with `git switch --detach origin/master` and `git
branch -D <batch branch>`. Each refusal names what to do next and changes
nothing, and a rerun of `merge` after the merge went through only
confirms the members and finishes. Check that each member's branch is
gone; delete a leftover by hand.

Once the batch has merged, each member's owner whose session has nothing
left stacked on its branch runs `python ops/prune_worktrees.py --mine
--apply` from its own worktree. It removes that worktree and its build
cache only if every safety check passes, and otherwise refuses, changing
nothing ([worktree-retention.md](worktree-retention.md)).

A batch whose head moved after `open` (a merge-forward under the
`merge-forward` skill included) is refused, because its head is not the
one the suite passed on. Merging it under that skill's exceptions is an
admin bypass the maintainer makes, outside the script.

On the maintainer's Windows host a local Claude Code hookify rule blocks a
raw `gh pr merge`, so this script is the only path there. That rule lives
in the maintainer's own configuration, not in this repository: a Codex
session, or any other harness, has no such block and relies on the written
rule (CLAUDE.md, "Shipping checklist" item 4): a batch merges through
`python ops/integrate.py merge`, and only a merge under the
`merge-forward` skill's exceptions is made by hand, by the maintainer.

The maintainer runs it, or the session the maintainer delegated merging
to.

## Measuring batches

`ops/batch_timeline.py` reports where each batch's time went, from GitHub's
own record and without writing to it:

```bash
python ops/batch_timeline.py collect --since 2026-10-19 --out batches.json
python ops/batch_timeline.py report batches.json --cutoff 2026-10-21T06:45:04Z
```

`report` works offline. It splits each member's wait (opened to the label,
then to its first batch build) and each batch's time: build, local
validation, the CI first pass along its critical path (queue wait against
execution) and failure repair (reruns). It also flags reruns, drops and
rebuilt batches. GitHub records no review event here, so review time stays
inside "awaiting admission". The first measurement is in
`evals/results/batch-lifecycle-20261023/`.

## Linux pool recovery

The optional Linux pool keeps the same required Parity checks. If a worker
disappears after the hosted planner selected it, disable the switch and,
once the run has finished or the maintainer has cancelled it, rerun the
`Parity / Linux pool plan` job from that run:

```sh
gh variable set PARITY_BATCH_LINUX_POOL --body hosted && gh run rerun <run-id> --job <planner-job-id>
```

Use the planner's job database ID from `gh run view <run-id> --json jobs`.
GitHub reruns that job and its dependents, so it re-reads the live switch
and sends the parity matrix to hosted. Rerunning only failed jobs would
retain a previous successful planner's self-hosted choice. This does not
change runner labels, Windows routing or the comparison allowlist; it does
not cancel a running batch automatically.

## Windows pool recovery

The optional Windows pool moves only the `windows-latest` `w1c-connection`
shard, and only when the planner saw a free, startable worker
(`docs/specs/2026-10-19-self-hosted-parity-lane.md`, "Optional Windows
batch pool"). If a routed shard is waiting on a worker that will not start,
disable the switch and rerun the planner job from that run, exactly as for
the Linux pool:

```sh
gh variable set PARITY_BATCH_WINDOWS_POOL --body hosted && gh run rerun <run-id> --job <planner-job-id>
```

The Linux pool's switch is separate and unaffected.
