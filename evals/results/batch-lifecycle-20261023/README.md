# Integration batch lifecycle, 2026-10-19 to 2026-10-21

This measures where a reviewed change's time went on its way to master, from
GitHub's own record, for every integration batch opened since the batch
process began (#851, 2026-10-19). Most of a change's wait came before it
joined a batch: from opened to the `ready-to-batch` label a member's median
time was 7h47m, against 2h35m from that label to merged. Inside a batch,
hosted CI is the cost, and every merged batch's longest chain of jobs ran
through a Windows parity shard.

- Collected: `python ops/batch_timeline.py collect --since 2026-10-19`
  at 2026-10-22T23:55:02Z (artifact `artifact.json`, read-only `gh` calls).
- Rendered: `python ops/batch_timeline.py report artifact.json --cutoff
  2026-10-21T06:45:04Z` (`report.md`). The cutoff is #890's merge, after
  which pull request pushes run only the fast lanes and batches the full ones.
- `tests/test_batch_timeline.py::test_committed_artifact_backs_the_findings`
  recomputes every number in this note and in `rust/CUTOVER.md` ("What sets
  the pace") from `artifact.json`.

## Sample

9 batch pull requests: 8 merged (#850, #866, #868, #876, #882, #887, #889
before the cutoff; #892 after it) and 1 closed and rebuilt (#865, rebuilt as
#866). 33 landed members, 1 dropped member (#884, which then merged on its
own) and 1 member carried twice (#860). **After #890 there is one batch
(n=1)**, so the "after" column is a single observation, not a trend. The
"before" numbers are n=7 batches and n=21 members; the 10 members of #850
predate the label, so they have no admission time. In the same window 12
pull requests merged to master outside a batch, among them #851, #890 and
#893, which built the batch process itself, and #884.

## Findings

Medians, with the maximum where it matters; each median is its own phase's,
so they do not add up.

| Phase | before #890 | after #890 |
| --- | --- | --- |
| Awaiting admission, opened -> label (members) | 7h47m, max 29h54m (n=21) | 3h21m (n=2) |
| After the member's last commit, -> label | 4h38m (n=21) | 1h05m (n=2) |
| Awaiting build, label -> first merge commit | 1m08s, max 5h15m (n=21) | 38s (n=2) |
| Build (merge commits) | 4s (n=6) | 2s (n=1) |
| Local validation, head -> batch PR | 10m09s (n=6); the suite alone 8m01s | 10m41s (n=1); the suite 9m36s |
| CI first pass, batch PR -> last first-attempt job | 1h17m, max 2h55m (n=7) | 58m52s (n=1) |
| Queue wait on the critical path | 15m07s, max 2h23m (n=7) | 14m16s (n=1) |
| Failure repair (reruns after the first pass) | 0s, max 25m21s (n=7) | 15m43s (n=1) |
| Awaiting merge, last job -> merged | 56s, max 1h09m (n=7) | 1m35s (n=1) |
| Member lead, label -> merged | 2h35m, max 6h40m (n=21) | 1h27m (n=2) |

1. **The wait before a batch dominates.** Members spent a median 7h47m
   between opening and the `ready-to-batch` label (n=21); the median time
   from a member's last commit to its label was 4h38m. GitHub holds no
   review event in this repo (reviews are recorded in PR bodies), and the
   timeline dates a commit when it was made, not pushed, so this phase cannot
   be split further into "waiting for a reviewer" and "waiting for the
   label"; it is the measurable upper bound on review latency. Once labelled,
   a member waited a median 1m08s for a build: batches were built as soon as
   something was ready.
2. **Queue wait is real but bursty.** Across the 8 merged batches the
   critical path took 6h47m of execution and 3h54m of queue wait (created to
   started), 37% pooled; the median batch's share was 25%, and without #868
   it was 19%. #868 waited 2h23m of its 2h55m first pass: its first job
   queued 35 minutes and one Windows parity shard (`w1c-doctor`) 89 minutes,
   during the 2026-10-20 burst of batches. Queue wait includes the account's
   concurrent-job cap, not only the supply of hosted runners.
3. **A Windows parity shard is always the long pole.** Every merged batch's
   critical path ran Scope -> a Windows Rust or Candidate build -> a Windows
   parity shard (`cli`, `cli-daemon`, `w1c-connection` or `w1c-doctor`) ->
   the Parity gate (8 of 8).
4. **One rerun came from a lane the ruleset does not require.** In #892
   (the only post-#890 batch) the macOS lite lane was cancelled because no
   hosted runner acquired it ("not acquired by Runner of type hosted even
   after multiple attempts"). `ops/integrate.py merge` waits for every check,
   not only the required ones. The rerun was clicked 28m57s after that CI
   run's failed attempt and ran 13m20s; because the Rust workflow kept the
   batch busy until 09:09:55, the rerun delayed the batch by 15m43s. The two
   pre-#890 reruns (#866, #882) were for parity shards whose failure failed
   the required Parity gates.
5. **Integration failures were rare, but one was not cheap.** One closed and
   rebuilt batch (#865 -> #866) cost its two members 1h47m; one member
   (#884) was dropped for a stale producer census and merged on its own; 3
   reruns in 8 merged batches. #860 was carried by #876 with an older head,
   took two more commits after its label, and landed with #882 5h30m after
   its first build: the longest lead (6h40m) of the sample.

## What this does not measure

- Review time on its own (finding 1), and anything before a pull request is
  opened.
- Runner minutes or cost: only wall time on the critical path.
- Whether the critical path is exact. The jobs API has no `needs`, so the
  path is walked back through jobs that completed when the next one was
  queued (within 5 s); every batch here produced a complete chain with one
  candidate at every step.
- Local validation for #850, built by hand before `ops/integrate.py`
  (no `Merge #N into` commits).
- The `repository` field of the artifact names the repository only as the
  checkout's `gh` default; the data is from Pseudogiant-xr/Pseudolife-MCP.
