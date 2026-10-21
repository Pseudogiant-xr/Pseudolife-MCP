# Merge batches

Master takes one pull request at a time, and each must pass CI on its head
merged with current master, so every merge sends the other open pull
requests back for a fresh CI run of about an hour. A merge batch merges
several reviewed pull requests into one `integrate/<YYYY-MM-DD>-batch-N`
branch, validates that branch once and merges it once. The rule is in
`CLAUDE.md` ("Running tests", the integration-batch bullet); this page is
the procedure behind it.

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
label, and joins a later batch once a push replaces that head. Pending,
green or no checks all admit it.

Why not wait for green on the member's own head: on 2026-10-20/21 four
things held batches up (#877 broke only against a newer master; an
h5-background harness race, fixed by #883; a stale producer census; #875's
dependency lock that could not resolve), and green CI on the member's own
head caught none of them. Each was caught by the batch, by its local checks
or by a reviewer's targeted run, so waiting for a member's own CI only
doubled the wait.

## Building one

Build a batch when three or more pull requests are ready, or about every
4 hours. One batch is open at a time; `build` and `open` refuse while any
pull request from an `integrate/` branch is open.

```bash
python ops/integrate.py status
python ops/integrate.py build --dry-run
python ops/integrate.py build
PSEUDOLIFE_SUITE_WHERE=local pwsh ops/remote-suite.ps1 > <log> 2>&1
python ops/integrate.py open --suite ~/.pseudolife-mcp/suite-results/<run>.json
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
  directory for `open`.
- **`--dry-run`** does the same merges on a detached HEAD, prints the plan,
  the body and the comments `open` would post, then switches back; it
  creates, pushes and comments on nothing.
- **`open`** publishes a built batch once the local full suite passed on
  it. `--suite` takes the result JSON `ops/remote-suite.ps1` writes and
  refuses unless it tested exactly the batch head and exited 0;
  `--suite-note` takes the result in words for a run that wrote no JSON
  (`ops/wsl-suite.ps1` alone); the note must name the head's short sha,
  and the body says it was not checked. A result for a selected run, or
  one from a launcher too old to record its pytest arguments, is refused.
  If any step after the push fails, rerunning `open` with the same
  arguments resumes on the batch's own pull request without repeating a
  comment, even if master has moved since. Before the batch pull request
  exists, it refuses if the branch gained commits or master moved since
  `build`. It
  then pushes the branch, opens the batch pull request titled `integrate:
  <date> batch N (#a, #b)`, whose body lists each member's merged head,
  review link and CI plus the suite result, and comments on each dropped
  member.

Drafts, heads with failing CI and heads that moved after listing are left
out without a comment. A member that conflicts is dropped: the script
aborts that merge, and `open` comments on the pull request naming what it
collided with (master itself, or the earlier members that touch the
conflicted files) and removes its label, so the next batch does not pick
it up again; a dropped member whose head moved since `build` keeps its
label and gets no comment. Its author merges master forward once the collision has
landed, reruns its targeted tests, and re-applies the label. If fewer than two members
merge cleanly, no batch is built, the collisions are reported at once and
the local branch is deleted; merge a lone ready pull request directly.

The producer census gets the same treatment, because the local full suite
does not run CI's census check. CI's Rust jobs run `python
rust/producer_census.py --ci-check`, and it fails only when the census is
stale *and* the diff from master touches the census inputs (the census
module's `CURRENCY_INPUTS`: the census, its script and the port inventory).
Elsewhere, drift is only reported. After merging, `build` runs that check on
the batch head with a pull request event naming the batch's base. If the
check would fail, `build` drops every member whose own diff changes those
inputs, naming the other members that change them too, and merges the rest
again from master. `open` comments on and unlabels each dropped member, as
it does after a conflict. Its author merges master forward once master
holds the rest of the batch, regenerates the census with `python rust/producer_census.py`, reruns
its targeted tests and re-applies the label. A census that is stale while no member
touches its inputs is printed as report-only and left alone, as CI leaves
it. Before the push, `open` runs the same check and refuses if it would
fail, so a plan built without it cannot reach CI.

That check runs the batch head's census script, which is member code, so
`build` (including `--dry-run`) executes it on this machine, as the full
suite will. It runs with only the environment Python and git need to start,
without tokens such as `GH_TOKEN`. The list of inputs is read from master's
copy of the script, so no member code runs inside `ops/integrate.py`
itself. A member that changes the script changes one of the inputs, so it
is caught either way. With no census on master there is no check.

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
2. A red suite is bisected locally: test master plus half the members,
   then the half that fails, until one member is left. Drop that culprit
   with a comment on its pull request and remove its label, switch back to
   `origin/master`, remove the local batch branch and build again. Nothing
   is fixed inside a batch.
3. CI on the batch pull request is the combined check of its members, and
   the `merge-forward` skill's exceptions do not apply to it. If master
   moves before the merge, close the batch and build the next one.
4. The batch pull request gets the usual review pass before the merge
   click and the ruleset's approving review. Its members were reviewed on
   their own, so the batch review checks that its members and merged heads
   match the body, that no conflict was resolved in it, and any
   conflict-marker warning the body carries.

## Merging it

The batch pull request merges with a **merge commit, never squash**: only
then are the members' head commits reachable from master, so GitHub marks
each member merged. A squash would leave every member open with its work
already on master. Check afterwards that each member shows as merged and
its branch is gone; delete a leftover branch by hand.

The maintainer clicks merge, or the session the maintainer delegated
merging to.
