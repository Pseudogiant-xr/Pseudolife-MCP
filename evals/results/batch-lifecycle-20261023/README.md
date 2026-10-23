# Integration batch lifecycle, 2026-10-19 to 2026-10-21

Where a reviewed change's time went on its way to master, measured from
GitHub's own record for every integration batch opened since the batch
process began (#851, 2026-10-19). Most of a change's wait comes before it
joins a batch: a member's median time from opened to `ready-to-batch` was
7h47m, against 2h35m from that label to merged. Inside a batch, hosted CI
is the cost, every merged batch's longest chain ran through a Windows parity
shard, and waiting for runners was 37% of that chain's time.

- Collected: `python ops/batch_timeline.py collect --since 2026-10-19`
  at 2026-10-22T23:55:02Z (artifact `artifact.json`, read-only `gh` calls).
- Rendered: `python ops/batch_timeline.py report artifact.json --cutoff
  2026-10-21T06:45:04Z` (`report.md`). The cutoff is #890's merge, after
  which pull request pushes run only the fast lanes and batches the full ones.
- `tests/test_batch_timeline.py::test_committed_artifact_backs_the_findings`
  recomputes every number below from `artifact.json`.

## Sample

9 batch pull requests: 8 merged (#850, #866, #868, #876, #882, #887, #889
before the cutover; #892 after it) and 1 closed and rebuilt (#865, rebuilt as
#866). 33 landed members, 1 dropped member (#884). **After #890 there is one
batch (n=1)**, so the "after" column is a single observation, not a trend;
the "before" numbers are n=7 batches and n=21 members (the 10 members of
#850 predate the label, so they have no admission time). In the same window
11 pull requests merged to master outside a batch, among them #851, #890
and #893, which built the batch process itself.

## Findings

Medians, with the maximum where it matters; n is stated with each.

| Phase | before #890 | after #890 |
| --- | --- | --- |
| Awaiting admission, opened -> label (members) | 7h47m, max 29h54m (n=21) | 3h21m (n=2) |
| of which after the member's last push | 4h38m (n=21) | 1h05m (n=2) |
| Awaiting build, label -> first merge commit | 1m08s, max 5h15m (n=21) | 38s (n=2) |
| Build (merge commits) | 4s (n=6) | 2s (n=1) |
| Local validation, head -> batch PR | 10m09s, of which the suite 8m01s (n=6) | 10m41s, suite 9m36s (n=1) |
| CI first pass, batch PR -> last first-attempt job | 1h17m, max 2h55m (n=7) | 58m52s (n=1) |
| of which runner wait on the critical path | 15m07s, max 2h23m (n=7) | 14m16s (n=1) |
| Failure repair (reruns after the first pass) | 0s, max 25m21s (n=7) | 15m43s (n=1) |
| Awaiting merge, last job -> merged | 56s, max 1h09m (n=7) | 1m35s (n=1) |
| Member lead, label -> merged | 2h35m, max 6h23m (n=21) | 1h27m (n=2) |

1. **The wait before a batch dominates.** Members spent a median 7h47m
   between opening and the `ready-to-batch` label (n=21), and 4h38m of it
   came after their last push. GitHub holds no review event in this repo
   (reviews are recorded in PR bodies), so this phase cannot be split
   further into "waiting for a reviewer" and "waiting for the label";
   it is the measurable upper bound on review latency. Once labelled, a
   member waited a median 1m08s for a build: batches were built as soon as
   something was ready.
2. **Runner wait is a third of hosted CI.** Across the 8 merged batches the
   critical path took 6h47m of execution and 3h54m of runner wait (n=8).
   #868 waited 2h23m of its 2h55m first pass, mostly for one Windows parity
   shard (`w1c-doctor`, 89 minutes queued) during the 2026-10-20 burst of
   batches.
3. **A Windows parity shard is always the long pole.** Every merged batch's
   critical path ran Scope -> a Windows Rust or Candidate build -> a Windows
   parity shard (`cli`, `cli-daemon`, `w1c-connection` or `w1c-doctor`) ->
   the Parity gate (8 of 8).
4. **One rerun came from a lane the ruleset does not require.** In #892
   (the only post-#890 batch) the macOS lite lane was cancelled because no
   hosted runner acquired it ("not acquired by Runner of type hosted even
   after multiple attempts"). `ops/integrate.py merge` waits for every check,
   not only the required ones, so the batch waited 28m57s for the rerun
   click and 13m20s for the rerun: 15m43s added to its critical path. The
   two pre-#890 reruns (#866, #882) failed required Parity lanes.
5. **Integration failures were rare and cheap to repair.** One closed and
   rebuilt batch (#865 -> #866) cost its two members 1h47m; one member
   (#884) was dropped for a stale producer census; 3 reruns in 8 merged
   batches.
6. **The merge click is no longer a wait.** Before #893 the slowest gap
   from green to merged was 1h09m (#889); #892 merged 1m35s after its last
   job.

## What this does not measure

- Review time on its own (finding 1), and anything before a pull request is
  opened.
- Runner minutes or cost: only wall time on the critical path.
- Whether the critical path is exact. The jobs API has no `needs`, so the
  path is walked back through jobs that completed when the next one was
  queued (within 5 s); every batch here produced a complete chain.
- Local validation for #850, built by hand before `ops/integrate.py`
  (no `Merge #N into` commits).
- The `repository` field of the artifact names the repository only as the
  checkout's `gh` default; the data is from Pseudogiant-xr/Pseudolife-MCP.
