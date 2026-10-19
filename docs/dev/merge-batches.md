# Merge batches

Master takes one pull request at a time, and each must pass CI on its head
merged with current master, so every merge sends the other open pull
requests back for a fresh CI run of about an hour. A merge batch merges
several reviewed pull requests into one `integrate/<YYYY-MM-DD>-batch-N`
branch, validates that branch once and merges it once. The rule is in
`CLAUDE.md` ("Running tests", the integration-batch bullet); this page is
the procedure behind it.

## Joining a batch

A pull request is ready when it has its reviews and green CI on a head of
its own. Mark it with the `ready-to-batch` label:

```bash
gh pr edit <number> --add-label ready-to-batch
```

The label is the claim that the pull request is ready; the script does not
re-check reviews or CI, though `status` shows both. Pushing a new head
after labelling needs its own CI before the next batch, since the batch
merges whatever head the pull request has when it is built.

## Building one

Build a batch when three or more pull requests are ready, or about every
4 hours. One batch is open at a time; `build` refuses while another
`integrate/*` pull request is open.

```bash
python ops/integrate.py status
python ops/integrate.py build --dry-run
python ops/integrate.py build
```

- **`status`** lists the labelled pull requests in batch order, each with
  whether its head merges cleanly into master (`git merge-tree`, no working
  tree touched), its CI on that head and its review decision.
- **`build`** needs a checkout with no uncommitted tracked changes whose
  HEAD is `origin/master` (`git switch --detach origin/master` in a fresh
  worktree). It creates the next free `integrate/<date>-batch-N`, merges
  each labelled head with `git merge --no-ff` in the order the label was
  applied (pull request number when that is unknown), pushes the branch and
  opens the batch pull request, titled `integrate: <date> batch N (#a, #b)`.
  Its body lists each member's merged head, review link and CI, plus a
  "Local full suite" line to fill in. The checkout is left on the batch
  branch.
- **`--dry-run`** does the same merges on a detached HEAD, prints the plan,
  the body and the comments it would post, then switches back; it pushes,
  opens and comments on nothing.

Drafts and heads that moved after listing are left out without a comment.
A member that conflicts is dropped: the script aborts that merge, comments
on the pull request naming what it collided with (master itself, or the
earlier members that touch the conflicted files) and removes its label, so
the next batch does not pick it up again. Its author merges master forward
once the collision has landed, waits for CI, and re-applies the label. If
fewer than two members merge cleanly, no batch is opened and the local
branch is deleted; merge a lone ready pull request directly.

The script never resolves a conflict and never force-pushes. Rebuilding a
batch means closing its pull request and building the next number.

## Validating it

1. Run the local WSL full suite on the batch head from the checkout `build`
   left on the batch branch: `pwsh ops/wsl-suite.ps1`, backgrounded and
   redirected (about 10 minutes since #846, cheaper than one CI run). Fill
   in the "Local full suite" line with the machine role, commit and result.
2. A red suite is bisected locally: test master plus half the members,
   then the half that fails, until one member is left. Drop that culprit
   with a comment on its pull request and remove its label, then close the
   batch pull request and build the next one without it. Nothing is fixed
   inside a batch.
3. CI on the batch pull request is the combined check of its members, and
   the `merge-forward` skill's exceptions do not apply to it. If master
   moves before the merge, close the batch and build the next one.

## Merging it

The batch pull request merges with a **merge commit, never squash**: only
then are the members' head commits reachable from master, so GitHub marks
each member merged. A squash would leave every member open with its work
already on master. Check afterwards that each member shows as merged and
its branch is gone; delete a leftover branch by hand.

The maintainer clicks merge, or the session the maintainer delegated
merging to.
