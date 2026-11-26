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

- **An ordinary pull request** (any head branch but `integrate/*`) without a
  docs-only scope decision runs the fast lanes only. In `ci.yml`: `scope`,
  `guards`, `lock-resolve` and
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
  any of those jobs. The last exception, cli-store's live security step,
  left on 16 November 2026: Rust tests the Rust job runs now hold the
  startup bind and token-map refusals and the 0.0.0.0-configured
  Host/Origin and bearer cases. The source mutants were retired on
  17 November 2026; manual stress remains the named live-oracle exception.
- **The `Python freeze` check** (its own workflow, `python-freeze.yml`)
  refuses an ordinary pull request whose net change touches
  `pseudolife_memory/`. It decides on the diff's paths and the exact label
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
  A version cut that still edits the frozen package requires the label, applied by the maintainer or by the conductor acting on a maintainer ruling. Follow the `release-procedure` skill. Schema changes use the Rust daemon's DDL and plan. The Rust job's `gen_schema_sql.py --check` checks those inputs and the derived metadata.
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
- **A docs-only change runs only the documentation checks in CI and Rust shim**, as a pull request and as a push to master: the `scope` decision, the doc guards, and the tests selected for the touched paths (tests under `tests/` whose code mentions a touched file's name, and the readers listed for its file or directory). No suite, lite lane, Rust job, lock-resolve or frontend job runs, and the required checks report as skipped. Both workflows decide with the Rust documentation predicate; when they cannot prove a change is docs-only (a missing, empty or unreadable range, a force push, an ambiguous API result) it runs the lanes it had before this shortcut. The generated producer census is eligible alongside docs on pull requests and merge groups only; documentation can stale it until the next batch regenerates it, and Rust shim reports that drift in the push's Scope job summary. The nightly still runs the lanes a docs push skipped.
- **A master push from an integration batch** skips what that batch already passed on the same code.
  `ops/integrate.py merge` merges a batch with a merge commit only while
  master is its base or an ancestor of its head, so the commit pushed to
  master has the batch head's tree. Each scope job runs
  `ops/ci_batch_passed.py`, which lists the jobs that passed on the batch
  pull request's latest run of the same workflow on that head. Only a job
  whose own conclusion there was `success` is skipped; a skipped, failed or
  cancelled one runs. A Rust-only batch skipped the Python suite lanes, and
  since the Python freeze its master push skips them too (`ci.yml`'s scope
  classifies the push by its merge); the next nightly (on master's tip,
  which contains it) then runs them, because `ops/ci_push_passed.py
  --not-if-job-passed guards` does
  not reuse a push whose `guards` job ran (which it does exactly when scope
  skipped the Python lanes); a push whose lanes its batch had passed stays
  reusable. Meanwhile `guards`, which runs exactly on such a push, runs
  every live test that reads a `rust/` file, with a throwaway PostgreSQL
  (`tests/test_suite_classes.py` keeps that list complete). Every Python
  lane runs only the live test files (`tests/suite_classes.txt`):
  `test-lite-linux`, the whole suite on embedded PostgreSQL, left with the
  lite tier. The frozen files run too in `test` (`--include-frozen`,
  which `ops/ci_tests.sh` adds to its own pytest when scope's
  `include_frozen` is true) when a change touches `pseudolife_memory/`
  (only a freeze-exception change can), when the diff is unreadable, and in
  the
  weekly nightly (Sunday UTC, 02:17 AEDT Monday), since the package still
  ships with floor-only dependencies; that run is Linux-only, as the
  Windows and macOS lanes name live files. While a release still publishes
  the Python package, its local validation runs the whole suite with
  `--include-frozen` too (the `release-procedure` skill). Anything doubtful skips nothing and logs
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
  retirement of CLI source mutants on 17 November 2026, neither schedule
  nor manual CI runs them, and the `Parity` gate no longer depends on them.
  Both existing schedules retain the other checks. Manual `mode=stress`
  selections remain separate and unchanged.
- **Never cancelled:** batch runs, master pushes, the nightly, manual and
  stress runs and merge groups each get a concurrency group of their own
  (their run id), so neither a newer push nor a newer run cancels them,
  running or pending. Only an ordinary pull request's runs are superseded.
- **CodeQL** runs as GitHub's default setup, a repository setting with no
  branch filter, so it still analyses every pull request push (four jobs,
  the Rust one about 8 minutes). It is not a required check. Limiting it to
  batches, master and its weekly run needs the maintainer to switch the
  repository to an advanced-setup workflow.

## Docs-only pull requests

A docs-only pull request may be merged straight onto master without a
batch. "Docs-only" here is what the command below enforces: every path in
the diff passes `is_documentation_path` in `rust/ci/parity_scope.py`. That
is not the `is_docs` test of `CLAUDE.md` ("Docs-only changes"), which
decides the local suite: Markdown under `rust/` or `evals/` can merge
directly although most of it is not docs-only there, and nothing under
`tests/`, `ops/`, `plugin/` or `pseudolife_memory/` can. All of these hold
first:

- It is independent: nothing it says depends on a pull request that is
  still open. One that documents unmerged work joins the batch that carries
  that work, and is dropped with it.
- No batch is built or open. `merge` refuses a batch when master moved after
  its `build` ("Merging it"), and a stacked gate result carries only on an
  identical tree ("Stacked local gates"), so a direct merge at that point
  costs the open batch a rebuild and a fresh gate. Wait for the batch to
  merge, or add the pull request to the next one.
- Its review pass is recorded.

Run `python ops/integrate.py merge-docs <pr>` from the clone holding the
batch worktrees, during your turn to update master. Serialize it with
batch builds, opens, merges and other updates to master.

- The command checks that the pull request is open, non-draft, targets
  master and is mergeable. It checks that no batch is open or built locally.
- It requires both the pull request diff and the proposed master push to
  be docs-only by master's CI classifier (`is_documentation_path` in
  `rust/ci/parity_scope.py`).
- The required checks must have reported and each latest check must be
  finished with none failed. `SUCCESS`, `SKIPPED` and `NEUTRAL` qualify.
- It runs the CI documentation guards and selected reader tests locally
  in a disposable worktree at the merge of current master and the pull
  request head, computed locally, because GitHub's test merge can stay on
  an older base; a conflict in that merge refuses.
- After the guards, it rechecks the batch state and checks, and requires the
  base and head to match the tested pair. It pins the checked head in the
  merge request and uses a regular GitHub merge commit. It then verifies
  the landed commit's parents and tree.

What it costs: a few hosted minutes for the doc guards. A push the workflows
cannot classify as docs-only keeps the lanes it had before this shortcut,
which can be the whole push matrix and about one hosted run on the account's
20 slots, so do not merge one of those shortly before a batch opens.
Do not start a direct merge shortly before a batch is built. Leave time
for the local doc guards.

Who merges: the maintainer, or a session the maintainer delegated merges to
in their own words. A policy change (any change to a rule that governs how
agents work on the project, in `CLAUDE.md` or any other file) is docs-only
by this definition when it touches only docs; the maintainer approves the
text it merges with first, and then only the maintainer merges it, or the
conductor under the maintainer's standing delegation (19 November 2026).

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
(it refuses when master has none, except for the batch that introduces it:
when master's history never held the guard, the members are judged with
the one copy they carry): a member whose merge changes
`pseudolife_memory/` on the batch branch without the `freeze-exception`
label is dropped with the guard's
refusal, whatever its `Python freeze` check reported; judged on the merge,
not on a merge-base diff, which a criss-cross history can leave empty.
`open` and `merge` then refuse a batch holding any commit that is neither
a member's merge nor the one generated commit, a generated commit that
touches the package, or a member whose label has gone since `build`.
Each member's own change against master is judged too, whichever merge
carries it: a stacked child merged first brings its parent's change, and
the parent still needs its own label, so an unlabelled parent is dropped
and any member whose head carries it is left out until the parent lands,
leaves the stack, or the maintainer rules it an exception. The parents
looked for are the open pull requests, whatever their base, whose current
head is in a member's history. Two shapes are not found, so the member's
own label covers what its diff shows: a parent that pushed again after the
child stacked on it (its current head is not in the child), and a parent
change from a branch with no open pull request. A child whose frozen files
are exactly a labelled parent's waits for that parent, keeping its label,
and does not keep out a labelled pull request stacked on it; one that
edits them further is refused as its own change. A member whose change
cannot be read (a shallow checkout, or a branch with no history in common
with master) refuses the whole build and changes no label, and so does a
failed or possibly cut-off listing of the open pull requests. A guard deleted from master is found with
`--full-history`, so a deletion inside a merge resolution still refuses, and
a shallow clone refuses rather than guess. A
member whose head the batch already holds (a stacked child merged first, a
head already on master) makes no merge commit; it is recorded as contained,
and its changes count under the merge that brought them. Subject to the snapshot checkpoint below, pending,
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
replay evidence that GitHub cannot produce: compatible budget-only conflicts retain
the existing normalization and use the batch's replay, census-only conflicts
are regenerated on the batch head, and other conflicts
are dropped. A diff whose Rust lanes the Rust workflow's scope skips needs
no replay: admission asks the frozen base's `pull_request_skips_rust` in
`rust/ci/docs_scope.py`, the predicate the scope job decides with, about
the net diff and its file modes. Markdown under `evals/` and documentation
accompanied only by the generated `rust/producer-census.json` are such
diffs; census alone or alongside runtime edits still requires replay, and
so does a skipped diff whose head shows the expanded `Rust / <os>` jobs
(the scope job ran them because its docs test selection failed). A diff reaching none satisfies
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
decision 2026-11-09). One batch is open at a time. Ordinary `build` and
`open` refuse another open integration batch. While its CI runs, the next
batch can use the local procedure in [Stacked local gates](#stacked-local-gates).

A release cut (a member whose own diff changes `project.version` in
`pyproject.toml`) is batched alone, because the release is tagged at the
batch's merge: whatever shares the batch ships in the release, while the
release notes are the fragments the cut folded. When a ready cut is listed,
`build` merges only the first cut and leaves the others out, keeping their
labels, for the next batch. A cut that would leave a fragment unfolded on
the batch head (one that landed on master after the cut was folded, or one
the cut adds) is left out instead, with the fix in its comment, and the
others batch as usual. Nothing in CI compares a cut with the old Python
release since the Python freeze, so a cut no longer has to be version-only
for CI (`release_cut()` in `evals/rust_port/oracle_selection.py` is used
only by a manual stress dispatch); it still needs `freeze-exception`, a
label only the maintainer's ruling for that cut puts on it, while it edits
`pseudolife_memory/compose/docker-compose.yml`, and a cut that changes any
other package path is refused, label or no label.

```bash
python ops/integrate.py status
python ops/integrate.py build --dry-run
python ops/integrate.py build            # with the Rust compile check when rust/ changed
python ops/integrate.py suite > <log> 2>&1   # the full suite on the box, with the native client
python ops/integrate.py harness > <harness.log> 2>&1
python ops/integrate.py shim-tests > <shim.log> 2>&1
python ops/integrate.py open --suite <suite.json> --harness <harness.json> --shim-tests <shim.json>
python ops/integrate.py merge   # once its CI is green and it has its review
```

- **`status`** lists the labelled pull requests in batch order, each with
  whether its head merges cleanly into master (`git merge-tree`, no working
  tree touched), its CI on that head and its review decision. It also names
  both own-head Rust job states, the snapshot checkpoint, and `would leave
  out: <reason>` for a member admission would omit, including a pending
  own-head replay. A finished replay can admit a head whose other jobs run.
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
  A labelled pull request that sits the batch out as not ready (a draft, a
  pending replay, a failing or unreadable CI record) is also judged as if it
  were ready, against master alone, and `build` prints what would still
  refuse it (`#N, once ready: ...`, or "nothing else refuses it"): the
  freeze, a refused parent its head carries, every CI budget job missing an
  accept record, a release cut's unfolded fragments, and a conflict with
  master (a compatible one only in the budget file is normalized, so it is
  not shown). A conflict with another member
  shows only once it merges. The report never refuses the build.
- **`--expect <PR,PR...>`** names the members the batch must hold. A named
  pull request that sits out stops `build` before any merge; after the
  merges, a named one that was dropped or an unnamed one that merged stops
  it before the plan is saved or any comment is posted. Either way it exits
  2, prints each such pull request with the reason, and removes the branch
  it made. Without the option `build` behaves as above.
- **`--dry-run`** does the same merges on a detached HEAD, prints the plan,
  the body and the comments `open` would post, then switches back; it
  creates, pushes and comments on nothing, and only says whether `build`
  would run the Rust compile check.
- **`open`** publishes a built batch once its full suite passed on it
  (a batch built `--after-drop` needs no suite result: "After a drop").
  `--suite` takes the result JSON `ops/remote-suite.ps1` writes (the one
  `suite` names) and refuses unless it tested exactly the batch head,
  exited 0 and ran the real native client built from that head
  (`real_client` true, `real_client_commit` the head; "Validating it");
  `--suite-note` takes the result in words for a run that wrote no JSON
  (`ops/wsl-suite.ps1` alone); the note must name the head's short sha and
  say `real client: yes` for a client the run built, and the body says it
  was not checked. A result for a selected run, or one from a launcher too
  old to record its pytest arguments or the real client, is refused, so a
  batch suite run before this rule needs a rerun.
  `--harness` and `--shim-tests` take the result JSON their commands write.
  Each must bind the exact full head, gate kind, complete command and selection,
  designated machine role, exit 0, a positive passing count without failures,
  and valid start/end timestamps. An interrupted, partial, wrong-head or
  not-run result refuses publication. There is no `--harness-note` bypass.
  Before the push `open` refuses a plan whose Rust compile
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

A conflict only in `rust/producer-census.json` keeps the member: `build`
checks that the census is a regular, unredirected file, takes the batch's
side as a temporary value, completes the member merge, then requires the
batch-head regeneration above to succeed. The plan, build output and batch
body name each member whose census conflict was regenerated. A failed
regeneration drops the census-input members and rebuilds from master,
naming the actual failure; no temporary resolution is published. Census
plus budget or source conflicts still drop the member.

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

The suite's client build and the shim unit tests do not compile every
workspace target, so two members that each compile alone can break the
batch together: on
2026-10-28 #991 changed `record()`'s signature, #994 called it the old way,
and batch #1017 failed hosted CI with E0061. So when the batch diff against
master changes anything under `rust/` (which holds every workspace Cargo
file) other than the generated census, `build` runs CI's own compile step on
the batch head, `cargo check --workspace --all-targets --locked` from
`rust/` (where rustup reads `rust-toolchain.toml`), after the census. It
took 48 s cold and under a second warm on the maintainer's Windows host.

**Where it runs.** The maintainer's host is not for heavy work (his rule,
16 November 2026). When the private `rust-check.remote` beside
`full-suite.remote` names a dev machine (`docs/dev/dev-machines.md`, "Batch
compile check"), `build` runs the check there and not on the host that runs
`build`. The batch reaches the machine as a git bundle over ssh, so nothing
is pushed before the publication check. The far half,
`ops/rust-check-remote.ps1`, does three things before cargo starts: it
fetches master into its clone, refuses a bundle whose head is not the batch
head, and forces one locked gate worktree to that head. The plan records
the machine's role and the batch body names it ("passed at `<head>` on the
Windows dev VM"). Several outcomes are each a check that **did not run**,
and `open` refuses them as above: an unreachable machine, a held gate lock
(another build, waited for up to 20 minutes), a clone that cannot fetch
origin, a missing cargo, or a malformed config. None of them falls back to
the local host. Only `PSEUDOLIFE_RUST_CHECK_WHERE=local` runs the check
here, and the body then says it ran on the maintainer's host.
`PSEUDOLIFE_RUST_CHECK_WHERE=remote` refuses to run without the file.
Without the file the check runs locally, as it always has, which is what an
outside contributor gets. `build --dry-run` names the machine without
contacting it.

A failure drops nothing and leaves the branch and plan as they are, and
`open` refuses the plan. `suite` refuses that head too, before it starts,
since a batch that never opens is not worth a full suite, and `open` judges
the compile check before it reads any suite result, so the refusal names the
compile check, not a missing suite. cargo's output cannot tell the batch from the machine (a lock file two
members left inconsistent fails like a download; a missing linker or
exhausted memory prints "could not compile"), so the refusal sends you to
master first: run the same command on `origin/master`. If it fails there
too, the cause is master or this machine (cargo's network, disk, linker or
memory): fix that and build again. Otherwise it is a red batch, bisected
like a red suite ("Validating it", step 3). A check that could not start
(no `cargo` on `PATH`) or was interrupted is refused the same way, as not
run. Once the batch pull request is open, `open` resumes it whatever its
plan says. Notes:

- Run locally, it honours `CARGO_TARGET_DIR`. Leave it unset in the
  checkout that builds batches, so the check keeps that checkout's own `rust/target`
  warm from one batch to the next, and do not point it at a directory
  other checkouts build into: cargo can reuse another checkout's units from
  a shared one and report a check that never compiled this tree (CLAUDE.md,
  "Disk space"). A cold check fills `rust/target`, so mind the 8 GB
  free-disk floor.
  If another cargo holds that directory, the check waits for its lock
  ("Blocking waiting for file lock").
- On a dev machine (`rust-check.remote`) the gate worktree keeps its own
  target directory beside the clone, one per source copy, for the same
  reason: a target shared between copies hands back another copy's units.
- Run remotely, it runs in that machine's own environment; nothing from
  the operator's environment is sent, and ssh forwards no agent, port or
  display. The far half withholds the same credential-shaped variables
  from cargo, and its git runs no hooks. Still, a member's build script or
  proc macro runs as that machine's ssh user, with the network open.
  - It can read every file that user can, and use that user's stored
    credentials.
  - It can leave files that outlive the batch, such as the persistent
    target directory.
  So keep that user's credentials to what the check needs. Hosted CI
  still gates every merge.
- **A plan crosses tool versions.** `Plan.load` drops a field it does not
  know and gives a missing one its default. So a plan built by one version
  of the tool can be opened and merged by another. A plan written with a
  remote configured carries `rust_check_where`, and a loader from before
  this change (`Plan(**raw)` with no filter) refuses it with a TypeError.
  That matters only for a batch that reverts this change.
- It runs member build scripts and proc macros, as the full suite runs
  member code, with the operator's environment minus credential-shaped
  variables, as the census script runs without tokens: names with `TOKEN`,
  `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`, `DSN` or `DATABASE_URL`, or
  ending in `KEY`, `KEY_ID` or `PAT`, and any value with credentials in a
  URL (a database DSN with its password, or a proxy with one).
- It compiles default features on the platform of the machine that runs it
  only. Feature- and
  platform-gated code, clippy and fmt stay CI's, as do the crates under
  `evals/rust_port/` (outside the workspace) and a non-`rust/` change that
  breaks an `include_str!` there (`ops/requirements.lock.txt`,
  `tests/cli_audit_float_repr.tsv`).

The script resolves only the derived metadata exceptions above (it merges
with `rerere` off, so a remembered resolution is not replayed either) and
never force-pushes. If
the push or `gh pr create` fails, nothing is commented on and rerunning
`open` with the same arguments finishes the job.

## Validating it

1. The full suite runs on the batch head between `build` and `open`,
   before any CI. With #846's four-worker launcher it takes about 10
   minutes, a fraction of one CI run.
   - **It runs on the Linux box by default.** A full suite in WSL overloads
     the maintainer's PC while he uses it, so `python ops/integrate.py
     suite` runs it there (maintainer ruling, 16 November 2026). It runs
     `ops/remote-suite.ps1` with `PSEUDOLIFE_SUITE_WHERE=remote` and the
     native client on, then names the result JSON for `open --suite`. The
     batch head is not on origin yet, so `ops/remote-suite.ps1` carries it
     inside the dispatch as a git bundle of what `origin/master` lacks. The
     box's clone fetches origin, then the bundle, into the ref its test copy
     is checked out from. Nothing is pushed before `open`.
   - **This PC only when he says so.** Local WSL is a candidate only while
     `~/.pseudolife-mcp/locks/pc-available` says the PC is available
     (`docs/dev/dev-machines.md` "Sharing"). Then
     `PSEUDOLIFE_SUITE_WHERE=local pwsh ops/remote-suite.ps1` runs the
     unpushed batch in WSL; without it that command overrides the rule and
     says so.
   - **It runs the real native client.** The hosted `test` lane builds
     `pseudolife-stdio` and exports `PSEUDOLIFE_NATIVE_TEST_BINARY` with
     `PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED=1`
     (`docs/dev/native-runtimes.md`), so the tests that need a real client
     run it there. A local suite without it uses their stand-in or skips
     them: a batch passed its local suite that way and then failed 18 of
     them hosted (#1634). So on an `integrate/*` or `stacked/*` branch
     `ops/remote-suite.ps1` sets `PSEUDOLIFE_SUITE_NATIVE_CLIENT=1` unless
     the variable is set, says so before anything starts, and the run builds
     the client from the batch head inside the place the suite runs (the
     WSL copy, or the box). It runs `cargo build --locked -p
     pseudolife-stdio -j 4` in `rust/`, the dev profile, as the lane does.
     The run smoke-runs the client's `version` and exports both variables to
     pytest. The result JSON records `real_client`, `real_client_commit` and
     `real_client_path`.
   - **The build cache** is one per machine, written only by these runs
     (`~/.cache/pseudolife-suite/cargo-target`, or
     `PSEUDOLIFE_SUITE_CARGO_TARGET_DIR`; never a general
     `CARGO_TARGET_DIR`). Cargo keys a workspace package by its path inside
     the workspace and judges it fresh by file times, so a cache another
     checkout built into can hand this one that checkout's binary. So, under
     the cache's own lock, a build that follows another checkout's first
     runs `cargo clean -p` on the workspace's own packages; third-party
     crates stay cached. The build holds the machine's slot-0 full-suite
     lock (`full-suite.lock`) while it compiles, so no full suite on slot 0
     runs beside it; with `PSEUDOLIFE_SUITE_SLOTS` above 1 a suite on
     another slot can (the machines run one slot).
   - **`open` refuses a result without it:** `real_client` must be true and
     `real_client_commit` the batch head, and a `--suite-note` must say
     `real client: yes`. There is no escape for a batch that "touches
     nothing the real-client tests cover", because no tool can judge that
     (conductor ruling, 16 November 2026).
   - **A gate that cannot build stops, and the batch does not open.** Under
     8 GB free where the cache lives, the run refuses before building and
     names the space. Without cargo it refuses and names the need. A failed
     build or a failed `version` fails the run with cargo's error. None of
     them falls back to the stand-in. Free the space, or fix the toolchain
     or the culprit member, and rerun.
   - **What it matches, and what it cannot.** It matches the lane's
     `--locked` dev-profile build and the two variables. It does not match
     the lane's workers: the local suite keeps its workers sized to the
     machine, where the lane runs `-n 2 --dist loadfile`. It is Linux only
     (WSL or the Linux box), so it covers the hosted `test` lane's
     conditions and never the macOS or Windows lite lanes'. Their
     real-client tests are still first run by the batch's hosted CI.
     A dev-profile Linux client is about 300 MB, over the client's 256 MiB
     archive limit; the installer tests pack a debug-stripped copy
     (#1556, #1560).
2. `python ops/integrate.py harness` runs the Rust harness Python tests on
   the Windows dev VM at that same head. The full suite collects only
   `tests/`, never these tests under `rust/`. Before any compile or suite,
   `build` reads rust.yml and the literal `RUST_HARNESS` from each tentative
   batch head without importing its Python. A member that makes the list
   omit a Rust-job pytest path is left out with a comment. An unreadable
   base produces a warning naming the offending workflow lines; members
   may repair it, and the completed head must read cleanly. While the base
   is unreadable, coverage failures cannot be attributed to individual
   members: an unrepaired final head refuses the whole batch instead of
   dropping a member. The warning identifies the base SHA, even though its
   quoted reader diagnostic uses the generic "batch-head" wording.
   The selection also includes the
   parity-checks job's candidate-free pytest cases and delegated unittest
   discovery, classified in `rust/ci/harness-test-selections.json`. Mixed
   files contribute their unit selectors; candidate-required cases stay in
   their lanes. Unknown commands, options or classifications fail closed.
   Of the workflow files, only `.github/workflows/rust.yml` is read.
   `ci.yml`'s `rust/ci` tests are covered by `RUST_HARNESS`;
   `python-freeze.yml` and `release.yml` run no Rust tests. Those three
   workflow files are not parsed by this reader.
   Every job and `uses:` step in rust.yml is audited. Fixed test selections
   come from `rust`, `parity-checks` and its mirrored `parity-selfhosted` job.
   The fixture job may invoke the schema wrapper only in build-only mode.
   The scope/pool jobs have explicitly classified routing helpers, and the
   Rust job's other Python helpers generate/check metadata or replay selected
   PR goldens. The daemon-image job's explicitly classified image tests and
   probes stay in that hosted image lane. Stress jobs have explicitly
   classified manual-dispatch drivers, not another fixed batch selection.
   Other jobs cannot introduce a Rust test command without a classification:
   any line carrying a Rust Python path, pytest or unittest must use a
   recognised launcher in its classified job. A refusal names the job, step
   and run-line number. Unknown actions/reusable jobs and executable action
   inputs refuse too; recognised actions only check out, provision tools,
   cache or transfer artifacts. Tests pin these boundaries, including a
   new test-bearing line in another job.
   The reader classifies literal command lines. Python test/helper scripts
   must run from a repository-root step (`.` or `${{ github.workspace }}`),
   overriding a job's `working-directory: rust` where needed. It refuses
   script/test calls from other directories, non-literal selections and tests after an in-step
   `cd`/`Set-Location`; move them to a separate root-directory step.
   Test-bearing compound lines refuse, including glued `;`, `&&`, `||`,
   `|` and bash `&`. The leading PowerShell call operator and the known
   report redirection `2>&1` remain supported. Comments are removed before
   joining continued lines. Unknown shell script invocations refuse; the
   current rust.yml invokes no `.sh`/`.ps1` wrappers. Python wrapper entry
   points have explicit classifications. Inline bodies in other languages
   are not interpreted: a shell-lexer failure on a line with no Rust script,
   pytest or unittest token is a classified skip. The test that reads the
   real rust.yml pins the existing commands and its `shell: python` step.
   Candidate-free fixtures can declare optional package names in that file;
   their exact version pins come from the batch head's parity job and are
   installed into the fresh environment. Missing or conflicting pins refuse
   before gating, and the result binds those pins beside its test selection.
   `harness` rechecks the final head and `open` requires that exact command
   and runner selection in its result.
   The command sends the head as a git bundle, runs the controller's
   `ops/rust-harness-remote.ps1`, and records its result and live log beside
   the suite results. No push or private gate kit is needed.
   The candidate-free parity phase adds about 5.4 minutes to every gate,
   measured including its fresh environment, service startup and cleanup.
   - The VM is configured through `rust-check.remote`, with role `Windows
     dev VM`. Its private `rust-harness-services.ps1` provides
     `Start-HarnessServices` and `Stop-HarnessServices`: start and prove the
     VM's test PostgreSQL and Linux Docker engine, then stop them even after
     a partially failed start. Failed cleanup cannot produce a passing result.
   - The run takes the machine's shared database lock, its full-suite lock,
     then its clone lock, keeping all through service shutdown. Worker
     database commands take that same database lock (dev-machines.md,
     "Sharing"). The database turn refuses fast, naming the holder and elapsed
     hold time; `--database-turn-wait-seconds` requests a bounded wait. Suite
     and clone locks retain their configurable `--lock-wait-seconds` default
     of 1200. Bare Rust pytest and standalone database harness launchers take
     the turn on marker-enabled machines too.
     Only the Windows dev VM currently carries the marker, because its gate
     starts and stops PostgreSQL. Do not mark the Linux dev boxes yet: their
     PostgreSQL stays up for concurrent targeted runs, and there is no
     containment off Windows. Their no-marker path remains unchanged.
   - `--require-databases` remains mandatory: missing login or Docker fails
     database cases instead of silently skipping them. Hosted runs without
     that option retain their existing platform/login skips.
   - Offline, busy past the wait, missing tools/services, or less than 8 GB
     free beside the worktree produces **not run**, never a local fallback.

   `python ops/integrate.py shim-tests` separately runs
   `cargo nextest run --locked -p pseudolife-stdio --lib --bins -j 4 --no-fail-fast`
   on the **second Linux dev box**, configured by `shim-tests.remote`. It
   sends the unpushed head as a bundle to its own clone/worktree and target,
   under that box's full-suite lock. A bounded lock wait, offline machine,
   missing cargo/nextest or less than 8 GB free on the work/cache filesystem
   gives **not run**, with no local fallback. Both commands print the result
   JSON paths to give `open`; failed tests keep their actual exit codes.
   They are separate commands so the conductor can take each machine by turn
   without exceeding the hypervisor's two-heavy-run limit.

3. A red suite, harness, shim test run or Rust compile check is bisected locally:
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
   smaller batches. When the dropped member shares nothing with the rest,
   the batch built again can take the fast path ("After a drop", below).
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

## Stacked local gates

While the open integration batch runs hosted CI, start in a second worktree
at `origin/master`. Keep the open batch's original build worktree and plan.

```bash
python ops/integrate.py build --stacked
python ops/integrate.py suite
python ops/integrate.py harness
python ops/integrate.py shim-tests
```

`build --stacked` verifies the sole open batch's plan, local branch, and
published head. It starts from that head and uses the normal admission and
build checks, excluding members already in the parent batch. During this
build the freeze, census, and budget tools come from the open parent's head,
which includes code that has yet to merge. The resulting
`stacked/<date>-batch-N` branch stays local and `open` refuses it. The gates
run on that commit with the usual recipes, including the native client in
the full suite. Keep the source worktree, branch, plan, and result files.

Once the parent merges, fetch master and switch a clean worktree to
`origin/master`. Use the branch name printed by the stacked build.

```bash
python ops/integrate.py build --from-stacked stacked/<date>-batch-N
python ops/integrate.py open --suite <suite.json> --harness <harness.json> --shim-tests <shim.json>
```

The first command rebuilds the integration batch on current master. It
judges admission again with the freeze, census, and budget tools from merged
master, then compares the complete Git trees, including generated files.
A different tree needs an ordinary build and fresh gates. A closed unmerged parent, a moved
head, a missing plan, or a dirty checkout also prevents reuse. A successful
compile result carries only when its recipe matches and the trees are
identical. At `open`, the tool checks the source and tree again. Supply a
successful JSON result from either the verified stacked commit or the rebuilt
commit for each gate. Every supplied result must pass its ordinary checks.
The batch body names the stacked source commit and the matching tree. The
normal hosted CI and merge checks apply to the rebuilt batch.

Keep the stacked branch, plan, and source worktree until the rebuilt batch
merges so that `open` can be retried. Then switch the source worktree away
from the stacked branch, delete the branch, and remove its plan from the
source worktree's Git directory. Locate it there with
`git rev-parse --git-path integrate-plans/stacked/<date>-batch-N.json`.
If the source worktree has already been pruned, build normally and run fresh gates.

`status` lists stacked plans and flags invalid ones. `--stacked`,
`--from-stacked`, and `--after-drop` are mutually exclusive.

## After a drop

A batch that opened, went red in hosted CI and was closed already paid for
a full local gate and most of a hosted run. When its culprit is dropped
and that pull request touched nothing the rest share, the batch built again
does not pay for them twice (the maintainer's ruling, 17 November 2026):

```bash
python ops/integrate.py build --after-drop <closed batch PR>
python ops/integrate.py open
python ops/integrate.py merge   # once `test` passed on the new head
```

**What it skips.** The whole off-PC gate except the compile check: the
local full suite, the Rust harness and the shim unit tests. `open` requires
none of those three results when absent, and the body says they were not run.
Any supplied result is checked exactly as for an ordinary batch: it must
pass on this head, and its passing evidence is recorded in the body. A
failed, not-run or mismatched supplied result refuses the open. This is
kept in the tool's one `_after_drop_rulings` function. The Rust
compile check still runs, and `open` still refuses a head that did not
pass it.

**What it carries.** `merge` still waits for `test` to pass on the new head
itself (or, on a Rust-only head, to be skipped by a successful `scope`,
below). A job still running on the new head is not waited for when it
concluded `SUCCESS` on the closed batch's head. A job that was skipped,
neutral, failed, cancelled or missing there must pass on the new head. For
a job that failed there, a skip on the new head counts as a pass only when
its workflow's scope job (CI's `scope`, the Rust workflow's `Scope`)
succeeded on the new head and nothing in that workflow's run there failed,
was cancelled or timed out, since scope skips a lane once the dropped
change that triggered it is gone (the conductor's ruling, 17 November
2026; the tool's `AFTER_DROP_SKIP_PASSES` demands a success instead). A
neutral result is never a pass for such a job. A
required lane the new head never reported counts as carried only once
every workflow run on the new head has finished. A job that did not pass on
the closed head and has not appeared on the new one is waited for; once
every run on the new head has finished without it, the fast path refuses.
Any check that has finished red or cancelled on the new head refuses the
merge, carried or not, and so does a failure whose rerun is still running,
so a carried job that fails before the click is a failure. Before it
merges, `merge` reads every check again and posts a comment on the batch
pull request (and prints it): each carried job with its run on the closed
head, each job that passed on the new head, and the state of every check
at that moment. It reads every check once more after the comment and
merges with the head pinned.

**When it applies.** `build --after-drop` checks each of these itself and
refuses, naming the one that failed and building nothing, unless all hold:

- the named pull request is a batch from an `integrate/*` branch into
  `master`, closed and not merged, and this checkout holds the plan of the
  run that built and opened it, at the head GitHub shows;
- `master` is exactly the commit the closed batch was built on;
- the new batch's members are the closed batch's members minus the dropped
  ones, at the same heads: nobody joined, nobody pushed, at least one was
  dropped, and the build left nobody else out;
- no file a dropped pull request changes is changed by a remaining member;
- a dropped pull request changes no code every test or job shares: anything
  under `tests/` but `tests/test_*.py`, any `conftest.py`, Python under
  `rust/` other than `test_*.py`, anything in a folder under a Rust crate's
  `tests/` (`tests/common/mod.rs`, which many test binaries compile in), `.github/`, `ops/integrate.py`,
  dependency manifests and lock files (`pyproject.toml`, `uv.lock`,
  requirements files, any `Cargo.toml` or `Cargo.lock`, the Rust toolchain
  file, `package.json` and the npm lock files), JSON under `rust/ci/`, the
  settings a runner, linter or build tool reads by itself (anything under a
  `.config/` or `.cargo/` folder, `pytest.ini`, `clippy.toml`,
  `rustfmt.toml`, `tsconfig*.json`, `vite.config.*` and the like), any path
  a workflow's steps, inputs or filters name (a CI script, a Dockerfile; a
  comment does not count), and a test module another Python file imports
  (a shared fixture);
- the new head differs from the closed one only in the dropped members'
  files and the batch's generated metadata.

The generated metadata the batch's own census commit rewrites
(`rust/producer-census.json`, `rust/ci/ci_budget_fingerprints.json` and
the imported accept records) is left out of these comparisons; a member's
own edit to `rust/ci/ci_budget.json` or another accept record is not. The
plan keeps what it relied on (the closed batch's number and head, the
dropped members and their files, the remaining members and their heads),
and `build`, `open` and the batch body print it.

**It merges early.** The rebuilt batch merges before every hosted job on
its new head has finished, which no other batch does: once `test` passed
there, the jobs that passed on the closed batch are not waited for again.

**When it saves time.** A summary job, such as `Parity / <os>` over the
parity shards, is a job like any other: if it did not pass on the closed
head (one shard went red), it must pass on the new one, so it waits for the
carried shards it needs, and the fast path saves no time while they run;
`merge` says so, naming them. So the fast path pays off when the closed
batch went red outside the Rust workflow, `test` alone for example.

**A Rust-only rebuilt batch** (the maintainer's ruling, 17 November 2026):
`scope` skips `test` on a head with no Python change, and `merge` accepts
that `test` as skipped only when `scope` itself succeeded on the new head.
So on a Rust-only rebuilt batch only the compile check and the quick lanes
run fresh on the new head. A `test` skipped while `scope` is still running
waits for `scope`; one skipped while `scope` failed, was skipped or never
reported, and a neutral `test`, refuse the fast path: rerun the job, or
close the batch and build it again without `--after-drop`.

A batch that went red in the local gate and never opened has no hosted
results to carry, so it is built again the ordinary way.

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

A batch built `--after-drop` merges on the carried checks described under
"After a drop" instead of waiting for every check; everything else here
holds for it too.

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
`python ops/integrate.py merge`, a direct docs merge uses
`python ops/integrate.py merge-docs <pr>` under
[Docs-only pull requests](#docs-only-pull-requests), and only a merge under
the `merge-forward` skill's exceptions is made by hand, by the maintainer.

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
