# The hosted CI budget

Every hosted job this repository starts takes one of the account's 20
concurrent job slots, shared with every other workflow and repository on
the account. When a full integration-batch run starts more jobs than there
are free slots, the rest queue. A run is then bound by its total runner
minutes, not by its longest job: on 2026-10-24 the longest Windows parity
shard started 10 minutes late because 27 shards competed for 19 slots
(batch run 40047950017).

`rust/ci/ci_budget.json` records, for `rust.yml` and `ci.yml`, the measured
minutes of every job a pull request can start, and four limits: the job
count, their summed runner minutes, the longest single job, and the
critical path (the slowest chain of measured jobs through `needs`, as if
every job had a free slot).
`rust/ci/test_ci_budget.py` runs in the eval parity shard and in every
batch's Rust harness run. It fails when:

- a pull request can start a job the budget has not measured, or the
  budget lists one the workflow no longer has;
- the job count, runner minutes, longest job or critical path exceeds its
  limit (master's workflow before #922, measured on batch run 40047950017,
  fails two: its 25.9-minute Windows cli-daemon shard, and the 44-minute
  chain from scope through the Rust job to the parity shards);
- a job `needs` another whose outputs and artifacts it never reads, unless
  `needs_without_data` gives the reason (the Parity gate reads results);
- two job runs save under one rust-cache key on one runner OS (the key as
  Swatinem/rust-cache builds it: prefix, shared-key or key and job id, OS;
  one save per matrix combination whose conditions can hold);
- a `rust.yml` install of the checkout or torch byte-compiles at install
  time (`pip install --no-compile` installs the same packages);
- a workflow a pull request starts has no budget entry.

Which jobs a pull request can start is worked out, not declared:
`ci_budget.py` evaluates each job's condition in GitHub's expression
language for a pull request, a merge group and a push, with every value it
cannot know (another job's outputs, labels, refs, variables) left unknown.
A job counts unless its condition is false for all three, so only a
condition that really excludes those events (`github.event_name ==
'schedule'`, `inputs.mode == 'ci'`, the success of a job that never runs)
keeps a job out of the budget.

## Changing the shape of CI

1. Make the change and push it. Since 2026-10-29 an ordinary pull request
   runs only the fast lanes, even when it edits a workflow, so its own
   runs measure only those. A changed heavy job carries an `accept` entry
   (its cost holds) or an `estimate` (below) in the same pull request, and
   is measured from its batch's or master's runs afterwards.
2. `python rust/ci/ci_budget.py measure --workflow rust.yml <run> <run> [...]`
   rewrites that workflow's measured minutes: each job's median over two or
   more completed runs, successful jobs of the latest attempt only, and only
   from runs whose head commit had the job's current definition (it reads
   the workflow at each run's head). It records each job's fingerprint and
   never changes a limit. A job edited since its minutes were taken fails
   the check until it is re-measured, or until `ci_budget.py accept
   --workflow <file> --job <id> --reason "..."` records why its cost holds
   (a reworded step, say); the reason stays in the budget for review.

   A change that adds a parity shard without editing a workflow (a new
   suite in `rust/ci/parity_scope.py`) starts no parity job on its own pull
   request, and a manual dispatch on a branch cannot bind the Python
   oracle. Enter each new job with `ci_budget.py estimate --workflow rust.yml
   --job "Parity checks / <os> / <suite>" --minutes <n> --reason "<the same
   steps' times on a named run>"`. The suite also changed the existing
   `parity-checks` job, so `accept` it with a reason a reviewer can check
   ("adds suite X; no other shard's steps changed"): an estimate never
   vouches for the other shards. `ci_budget.py check` lists every estimate,
   and measuring the batch run replaces it.
3. If a limit no longer holds, raise it in the same change and say why in
   its `why` field and in the pull request: what the extra slots or minutes
   buy, and what was measured. A reviewer then decides with the cost in
   view.

`python rust/ci/ci_budget.py check` prints every disagreement without
running the tests.

## What keeps a run cheap

These came out of the 2026-10-24 pass (#922, #924), which took the batch
Parity gate from a median 58.8 minutes to 36.7:

- **Need only what you read.** A job that waits for another it takes nothing
  from starts late for no reason. The parity shards waited 18 minutes for
  the whole Rust job when one 2-minute fixture build was all they used.
- **Add jobs where slots sit idle, not where they are full.** The first
  minutes of a run use a handful of slots, so splitting a build there is
  nearly free. Splitting a 10-minute shard in the parity phase costs a slot
  and its setup (about 3 minutes on Windows) for every run.
- **Split a shard only when it is the long pole,** and measure the parts
  first. Each new job repeats checkout, Python and dependency installs.
- **Install only what the job uses.** The Rust toolchain only where cargo or
  rustc runs; `--no-compile` for large Python installs.
- **One saver per cache key.** A job that only needs another job's
  dependencies restores that job's key read-only (`shared-key` naming the
  same key, `save-if: false`). The repository cache stood at its 10 GB
  limit on 2026-10-24, and a second saver evicts someone else's cache.
  Check that the reading job builds the same dependency features, or it
  compiles cold anyway.
- **Measure medians over several runs.** Hosted Windows varies by tens of
  percent, and the account's other work changes how long jobs queue. Use
  each attempt's own start, and drop jobs kept from an earlier attempt.
