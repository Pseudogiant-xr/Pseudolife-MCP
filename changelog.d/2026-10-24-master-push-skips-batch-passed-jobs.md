### Changed (2026-10-24 — master pushes stop re-running jobs the merged batch already passed)

- A push to master that merges an integration batch now skips each CI job
  that passed on the batch pull request's latest run of the same workflow,
  because the merge commit's tree is the batch head's
  (`ops/integrate.py merge` refuses otherwise). `ops/ci_batch_passed.py`
  checks the associated pull request, its merge commit, its `integrate/`
  branch, the tree, and that the run completed; any doubt or API error
  skips nothing and logs why. Only a job whose own conclusion was `success`
  is skipped: a Rust-only batch's skipped Python lanes still run on its
  master push.
- In `ci.yml` this covers `lock-resolve`, `test`, the lite OS lanes and
  `frontend`. In `rust.yml` it covers the disposable-image check and each
  passed parity pair (OS and suite), removed from the matrix by
  `rust/ci/parity_scope.py --batch-passed`. The `Rust` job, the candidate
  build, `parity-live` and the `eval` and `judges` suites always run on a
  push, since they read the event-selected oracle, which differs between a
  pull request and a push. Pull requests, merge groups, the nightly and
  manual runs are unchanged; the nightly stays the full backstop.
- Both scope jobs gain `actions: read` and `pull-requests: read` for the
  lookup; every other job keeps `contents: read`.
