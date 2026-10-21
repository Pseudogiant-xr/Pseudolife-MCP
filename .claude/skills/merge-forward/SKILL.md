---
name: merge-forward
description: Use before merging a PR whose branch was updated from master without waiting for fresh CI on the new head. Holds the only two exceptions to "fresh CI on the current merge ref", the disjoint merge-forward and the tree-identical stack forward, with every condition and the backstop watch they require.
---

# Merging without fresh CI on the updated head

Ordinary PRs never merge directly: they land through an `integrate/*`
batch (`docs/dev/merge-batches.md`), because a push to any other branch
runs only the fast CI lanes. A batch PR normally needs passing CI for its
current head integrated with current master (CLAUDE.md, "Shipping
checklist"). These two exceptions let a batch PR merge on the CI an
earlier head of it passed, after master moved; "the PR" below is that
batch PR and "either side" is the batch against master. Both are to be
removed when the GitHub merge queue is enabled. When in doubt, close the
batch and build the next one.

Merging before CI reports on the new head is a ruleset bypass (admin
merge), so only the maintainer or the maintainer's delegate does it.

## A. Disjoint merge-forward

Added after #592 waited 25 minutes on a CHANGELOG-only conflict. All of
these must hold:

1. **The previous batch head passed** every lane a batch runs: `test`,
   `test-lite-linux`, `test-lite-windows`, `test-lite-macos`, `frontend`,
   CodeQL and both `Parity` gates. The merge-forward commit is the only
   commit pushed since, since nothing is fixed inside a batch.
2. **The two sides share no file except docs-only files** (as CLAUDE.md's
   "Docs-only changes" defines them). Compute it, don't eyeball it:
   `base=$(git merge-base <previous head> origin/master)`. Master's set is
   `git diff --name-only $base..origin/master`; the PR's set is
   `git diff --name-only $base...<previous head>`.
3. **No hand resolution outside docs:** `git show --remerge-diff
   --name-only <merge commit>` lists only docs-only files. `llms-full.txt`
   is regenerated with `python ops/gen_llms_txt.py`, never hand-merged.
4. **Neither side changes shared ground:** `pyproject.toml`, lock files or
   `.github/workflows/`; `tests/conftest.py`, fixtures or imported test
   helpers; schema, DDL or migrations; generated outputs
   (`pseudolife_memory/web/static/`).
5. **Neither side's code reaches the other's** only through a dynamic
   import, a subprocess, or HTTP or MCP calls into a daemon whose
   behaviour the other side changed.
6. **On the merged head, run locally** every test file either side added or
   changed since `$base`, plus the doc guards and every test naming a
   touched doc path. All must pass. Read overlapping doc hunks for
   contradictions; a clean doc auto-merge can still contradict itself.

The PR comment says "disjoint merge-forward: fresh CI skipped" and gives
both file sets.

## B. Tree-identical stack forward

Added after squash-merging #771 left #772 in conflict with master.
Squash-merging the bottom PR of a stack gives master one new commit whose
content the next PR already holds; git does not see that, so the next PR
conflicts wherever both added lines in the same place. The PR may merge on
the CI its earlier head passed when all of these hold (batches merge with
merge commits, so for a batch PR this arises only when master took a
squash of a PR the batch already contains):

1. **Before forwarding,** master's only commits since the PR's previous
   merge base are squashes of its parent PRs, each squashed from a head
   this PR contains, and master's tree equals the last of those heads.
   Check both: `git merge-base --is-ancestor <parent's merged head>
   <tested head>` and `git diff --quiet <parent's merged head>
   origin/master`. If either fails, forward with an ordinary merge, never
   `-s ours` (which would silently drop master's other content), and use
   rule A or wait for fresh CI.
2. **Only then forward** with `git merge -s ours origin/master`, or by
   resolving every conflict to the PR's own side. The forwarded head's tree
   must be byte-identical to the head that passed CI: `git diff --quiet
   <tested head> <forwarded head>`.
3. **That tested head passed every check its gate requires.** A failed,
   cancelled or unfinished job on it means waiting for CI on the forwarded
   head; the identical tree only means that run tests the same content.

The merge comment says "tree-identical stack forward: fresh CI skipped" and
gives the tested head, the forwarded head and the three checks.

## The backstop (both rules)

CI runs again on the push to master. That run is the check, and a cancelled
run is not a pass: a later master push cancels an earlier run
(`cancel-in-progress`). Watch the first master run that completes on a
commit containing the merge: `ci.yml` for rule A, both `ci.yml` and
`rust.yml` for rule B. If it fails, open a revert PR at once (landed as a batch of one), or a fix PR
when the fix is obvious, and make no more such merges until master is green
again.
