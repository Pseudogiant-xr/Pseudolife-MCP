# Merge queue for master

**Status:** CI is ready for the merge queue (2026-10-19). The queue itself is
off until the maintainer turns it on in the master ruleset. Until then, every
merge rule in CLAUDE.md still applies unchanged.

PRs land on master one at a time. Each PR needs CI on its head integrated with
current master, so every merge makes the other open PRs' CI stale and they run
again. About 13 merges a day against a Rust workflow run of about an hour gave
a median open-to-merge time of 5.8 hours (measured 2026-10-19 over 60 merged
PRs). GitHub's merge queue tests queued PRs together on top of the master they
will land on, then merges the group, so a merge no longer invalidates the
queued PRs' runs. This document says what CI now does for queued groups, what
the maintainer sets to turn the queue on, and which merge rules then retire.

## What CI does for a merge group

Both workflows (`.github/workflows/ci.yml` and `rust.yml`) trigger on
`merge_group` as well as `pull_request`. A queued group runs the same jobs
under the same check names, so the ruleset's required checks report for it:

| Required check | Workflow | Job |
| --- | --- | --- |
| `test` | CI | `test` (full Python suite against PostgreSQL) |
| `Parity / ubuntu-latest` | Rust shim | `parity` gate, Linux |
| `Parity / windows-latest` | Rust shim | `parity` gate, Windows |

(Read from ruleset 19138403 on 2026-10-19; `tests/test_ci_merge_queue.py`
pins the names.)

- **Scope.** Both `scope` jobs classify a group the way they classify a pull
  request, using the same rules over everything the group adds to master:
  they fetch master's current tip and diff it against the group head. A group whose
  every file is under `rust/` (or is `rust.yml`, `CHANGELOG.md` or a
  `changelog.d/` fragment) skips the Python suite lanes and runs `guards`. A
  documentation-only group skips the Rust and parity jobs. If one PR in the
  group touches anything else, the whole group runs everything. A group
  whose `merge_group.base_ref` is not `refs/heads/master`, or whose master
  tip cannot be fetched, also runs everything.
- **Parity gate.** The `Parity` gate accepts the same skips for a group as
  for a pull request. It accepts the documentation-only skip, and it accepts
  a skipped source-mutant matrix, which still runs only nightly and on manual
  CI.
- **Python oracle.** `evals/rust_port/oracle_selection.py` treats a group as
  a pull request against its base. The oracle is the merge base of the
  group's base and head, which is the base itself. The candidate is the
  group head, which is also the checkout. The release-cut exception (oracle
  = head) applies when the group's whole range is a release cut.
- **Producer census and daemon image.** Both take the group's
  `merge_group.base_sha` as the comparison base, as they take a PR's base.
- **Concurrency.** Each queue entry runs on its own branch ref
  (`gh-readonly-queue/master/...`). Its runs therefore never share a
  concurrency group with a pull request or a master push. No PR push can
  cancel a queue run, and a queue run cancels nothing.

The scope jobs diff from master's tip rather than `merge_group.base_sha`
because GitHub does not formally define `base_sha`. It is either master's tip
or the head of the queue entry ahead of this one. If it were the entry ahead,
a range starting there would see only this entry's files. Under `HEADGREEN`
the earlier entry need not pass its own run, so a Python change ahead could
then merge behind a Rust-only head whose `test` was skipped. Master's tip
counts every unmerged entry whatever `base_sha` means. If master has moved
on, files from those extra commits only add to the range, which can only run
more jobs.

The oracle selector, the producer-census currency check and the daemon-image
input check still take `merge_group.base_sha` as their base, as they take a
PR's base. Under `ALLGREEN` (recommended below) each entry must pass its own
run, so a per-entry range is sound there. Before switching to `HEADGREEN`,
move the census and image ranges to master's tip as well.

## What the maintainer sets (ruleset 19138403)

In **Settings → Rules → Rulesets → "master: PRs with review + CI"**, add
**Require merge queue**. In the REST API, that is the `merge_queue` rule:

| Setting | API parameter | Value | Why |
| --- | --- | --- | --- |
| Merge method | `merge_method` | `SQUASH` | Matches today's squash merges. |
| Maximum group size | `max_entries_to_merge` | **4** (range 4–6) | Batches a working day's ~13 merges into a few master pushes. |
| Build concurrency | `max_entries_to_build` | **4**, at least the maximum group size | A group can only include entries that have been built. |
| Minimum group size | `min_entries_to_merge` | 2 | Keeps a lone PR from pushing master on its own while others queue behind it. |
| Wait for the minimum | `min_entries_to_merge_wait_minutes` | 20 | After 20 minutes a single PR merges anyway. |
| Status check timeout | `check_response_timeout_minutes` | 180 | Longer than a slow Rust run (about 1 h, up to 2 h behind the 20-job cap). A hung run blocks the queue for this long. |
| Grouping strategy | `grouping_strategy` | `ALLGREEN` | Every entry must pass its own run, as every PR must pass today. See "Flakes" below for the trade-off with `HEADGREEN`. |

**Required checks: add the lanes the queue would otherwise ignore.** The
queue waits only for required status checks. Today the ruleset requires
`test`, `Parity / ubuntu-latest` and `Parity / windows-latest`, and CLAUDE.md
covers the other lanes by having the merger wait for every check. Under the
queue nobody waits by hand, so a group whose `test-lite-windows` went red
would still merge. Before enabling the queue, add these jobs to the
**Require status checks to pass** rule:

| Check | Workflow | Skipped when |
| --- | --- | --- |
| `guards` | CI | the change is not Rust-only |
| `test-lite-linux` | CI | the change is Rust-only |
| `test-lite-windows` | CI | the change is Rust-only |
| `test-lite-macos` | CI | the change is Rust-only |
| `frontend` | CI | never |
| `Rust daemon / disposable image` | Rust shim | the change is documentation-only |

A job skipped by its `if:` reports success, so requiring these never blocks
a change that legitimately skips them. Add a CodeQL check only after
confirming that it reports for merge groups. A required check that never
reports holds the queue until the status check timeout. Leave "Require
branches to be up to date" off, because the queue does that integration.
Keep the pull request rule (one approving review) as well. A PR must have its
approval and green PR checks before it can be queued.
`tests/test_ci_merge_queue.py` checks that every name in both tables is a real
check.

**Cost to watch.** Each queue entry is a full CI run, and `rust.yml` is
about 32 jobs against the account's 20-concurrent-job cap. Four entries
building at once share that cap, so a group's wall time grows with its size.
These values are a starting point. After the first week, compare median
open-to-merge time and the number of cancelled master `rust.yml` runs with
the 2026-10-19 baseline (5.8 h median; 2 of 10 master runs cancelled on
2026-10-18), then adjust group size and build concurrency.

## How the delegate queues a reviewed PR

Instead of clicking merge, the delegate adds the PR to the queue once its
gate is met: review recorded, approval given, and PR CI green on its head.

```bash
gh pr merge <number>
```

On a branch whose ruleset requires a merge queue, this adds the PR to the
queue (with no merge-method flag; the queue's method applies). If the PR's
required checks are still running, it turns on auto-merge, and the PR joins
the queue when they pass. To take a PR out, use **Remove from queue** on the
PR page, or the GraphQL `dequeuePullRequest` mutation. The PR page and the
repository's queue page for master (`/queue/master`) show its position and
group.

The delegation rule is unchanged. Queuing is the merge click, so only the
maintainer, or an agent the maintainer has named for that work, queues a PR.

## What happens when a group fails

GitHub documents no bisection. Instead, each queued PR is its own entry with
its own run on the stacked branch: master, then every entry ahead, then this
PR. The per-entry runs therefore do the bisection. When an entry's required
checks fail, the queue removes that PR, then rebuilds every entry behind it
on a branch without the failed PR. The removed PR's author fixes it and
queues it again. Entries ahead of the failure are unaffected and can still
merge. Under `ALLGREEN` every entry in a group must pass. Under `HEADGREEN`,
a group merges once its head entry passes, even if an earlier entry's own run
failed or is still running.

**Release cuts.** The oracle selector takes the release-cut exception only
when the group's whole range is the version cut. A release PR queued behind
other PRs may compare against the pre-release Python oracle, which would fail
parity. Until the first queued release confirms the behaviour, queue a
release cut only when the queue is empty, and queue nothing behind it until
it merges.

## Flakes matter more under batching

A failed entry removes its PR and forces a rebuild of every entry behind it,
each about an hour of `rust.yml`. A Windows Parity flake therefore costs the
whole tail of the queue, not just one PR's rerun, and it ejects a PR that did
nothing wrong. It also waits for that PR to be queued again. Under the
recommended `ALLGREEN`, a flake in any entry does this. `HEADGREEN` would
ignore flakes in earlier entries of a group, though a flake on the head entry
still ejects that PR and rebuilds everything behind it. It would also merge
an earlier entry whose own run genuinely failed, provided the head passes.
Start with `ALLGREEN`. Consider `HEADGREEN` only once Windows Parity flakes
are rare, and only after the census and image ranges also move to master's
tip (see "What CI does for a merge group"). The rule "Windows CI failures go
to the Windows VM before a rerun" (CLAUDE.md, "Dev machines") matters more
once the queue is on. Clear the open Windows Parity deflake issues before you
turn the queue on.

## CLAUDE.md rules that retire once the queue is on

These rules exist to keep PR CI current against a moving master. The queue
makes them unnecessary, so retire them in the same change that records the
queue as on, not before:

- **"Every PR requires passing CI for its current head integrated with
  current master … update the branch and wait for fresh CI"** (shipping
  checklist item 3). The queue's group run is the integrated check, provided
  every lane is a required check (see "Required checks" above). Without
  that, this rule must stay for the lanes the ruleset does not require. A PR
  needs green CI on its own head before it is queued. It no longer needs to
  be updated and rerun each time master moves.
- **Disjoint merge-forward** ("Running tests"). It exists to skip a fresh CI
  run after a master forward-merge. With the queue, nothing waits on that
  run, and the admin-merge bypass it requires is no longer needed.
- **Tree-identical stack forward** ("Running tests"). A stacked child still
  conflicts with master after its parent squash-merges, so it is still
  forwarded with that rule's checks. The forwarded PR is then queued, with no
  admin merge and no "fresh CI skipped" comment.
- **Integration batches** (`integrate/<date>-batch-N` branches, "Running
  tests"). A queue group is the combined CI run of several reviewed PRs, so
  hand-built batch branches are no longer needed.

These rules stay: local validation before opening a PR, the local full-suite
triggers, review, the Codex second review, and the Windows VM repro rule.
