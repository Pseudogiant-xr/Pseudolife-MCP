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
  `rust.yml`: `Scope`, the `Rust` job (format, clippy, builds, nextest),
  the disposable-image check and the `Parity` gate; the candidate, harness
  candidate and fixtures builds and the parity matrix are skipped. Both `scope` jobs decide this from
  `github.head_ref` (an absent one runs everything). An ordinary pull
  request that changes CI or dependency inputs (`.github/workflows/`,
  `pyproject.toml`, a lock file, the Rust toolchain pin) or whose diff
  cannot be read runs as a batch would, so a workflow edit is tested by the
  lanes it changes before it can switch them off. The required `test`
  check reports its skip, which GitHub counts as success, and the required
  `Parity / <os>` gate runs and accepts the skipped candidate and parity
  jobs only on a pull request its `scope` marked not full; the `Rust` job
  must still pass.
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
  covers the disposable-image check and each parity pair (OS and suite).
  The `Rust` job, the candidate, harness candidate and fixtures builds and
  the `eval` and `judges` suites always run. They compare
  against the event-selected Python oracle (or, for `Rust`, its package
  metadata), which is the merge base on a pull request and the pushed head
  on a push, so the batch's pass is not the push's result. A merge made
  under the `merge-forward` skill's exceptions skips only what passed on
  the forwarded head itself; with no completed run there, its master push
  runs everything, which is the backstop that skill relies on.
- **The nightly and manual runs** run everything, as before. The nightly
  is the full backstop for everything a master push skipped, and since
  2026-10-24 the only automatic run of the non-gating Windows `cli-live`
  job (`parity-live`).

## Joining a batch

A pull request is ready when it has its reviews and its own local targeted
validation (the touched and dependent tests, recorded in its body), and CI
on its head is not failing. It does not wait for green CI of its own: the
batch's CI is its CI, so pending or absent checks are fine. Mark it with
the `ready-to-batch` label:

```bash
gh pr edit <number> --add-label ready-to-batch
```

The label is the claim that the reviews are done; only someone with triage
or write access can apply it. The script does not check reviews, since
this repository records them in the pull request body rather than as
GitHub approvals, but `status` shows the review decision. It does check
CI, only for a known failure: a labelled pull request whose current head
has a failing check sits the batch out without a comment and keeps its
label, and joins a later batch once a rerun of the failed job passes or a
push replaces that head. Pending,
green or no checks all admit it.

Why not wait for green on the member's own head: on 2026-10-20/21 four
things held batches up (#877 broke only against a newer master; an
h5-background harness race, fixed by #883; a stale producer census; #875's
dependency lock that could not resolve), and green CI on the member's own
head caught none of them. Each was caught by the batch, by its local checks
or by a reviewer's targeted run, so waiting for a member's own CI only
doubled the wait.

## Building one

Build a batch whenever no batch is open and at least one pull request is
ready; a batch of one is how a lone pull request (an urgent fix, a revert,
a red-master fix, a release cut) gets its full CI, since no pull request
from any other branch is merged directly. One batch is open at a time;
`build` and `open` refuse while any pull request from an `integrate/`
branch is open.

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
python -m pytest -q -p no:cacheprovider <the RUST_HARNESS paths build prints> > <log> 2>&1
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
`build` runs the batch head's census script and, when the census changed,
commits it as the batch's one generated commit (`Regenerate the producer
census for this batch`), touching only that file; no member is edited.
This is the one exception to "nothing is fixed inside a batch"
(maintainer, 2026-10-27). The batch pull request names that commit under
`## Producer census` and lists any trusted manual shape the new census
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
nothing is regenerated.

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
   the pytest steps of `rust.yml`'s `rust` job plus the eval shard's
   `pytest rust/ci`; census currency is not among them, since `build`
   regenerates the census and `open` runs CI's verdict. These need no built
   Rust binary; the
   parity shards' other harness files drive the release candidates and stay
   CI's. Run it natively from the batch checkout (a targeted run, about
   140 s on Windows; `rust/ci/test_workflow.py` needs `pwsh`) in any
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
   parity shard). Rerun once only for the flake classes CLAUDE.md names
   (issues #830 and #855), naming the issue on the batch pull request;
   otherwise drop the attributed member with a comment, close the batch
   and build again. If no member can be attributed, bisect by building
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
