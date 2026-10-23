# Integration batch timeline

Batches opened since 2026-10-19T00:00:00Z, collected 2026-10-22T23:55:02Z; regimes split at 2026-10-21T06:45:04Z.

## before: n=7 merged batches, 31 landed members (1 closed batches, 1 drops, 2 reruns, 0 of them for lanes the ruleset does not require, 9 direct merges)

| Phase | n | median | max | total |
| --- | --- | --- | --- | --- |
| awaiting admission (opened -> label) | 21 | 7h47m | 29h54m | 193h02m |
|   of which revising (opened -> last push) | 21 | 0s | 25h34m | 71h02m |
|   of which after the last push | 21 | 4h38m | 13h06m | 121h59m |
| awaiting build (label -> first build) | 21 | 1m08s | 5h15m | 8h36m |
| rebuild repair (first build -> landing build) | 21 | 0s | 1h47m | 3h34m |
| build (merge commits) | 6 | 4s | 12s | 27s |
| local validation (head -> batch PR) | 6 | 10m09s | 16m34s | 1h07m |
|   of which the local full suite | 6 | 8m01s | 11m36s | 51m11s |
| CI first pass (batch PR -> last attempt-1 job) | 7 | 1h17m | 2h55m | 9h44m |
|   critical path: runner wait | 7 | 15m07s | 2h23m | 3h40m |
|   critical path: execution | 7 | 42m46s | 1h29m | 6h03m |
|   critical path: dispatch and job gaps | 7 | 6s | 10s | 42s |
| failure repair (reruns after the first pass) | 7 | 0s | 25m21s | 30m36s |
| awaiting merge (last job -> merged) | 7 | 56s | 1h09m | 1h15m |
| batch PR wall (opened -> merged) | 7 | 1h21m | 2h56m | 11h30m |
| member lead (label -> merged) | 21 | 2h35m | 6h23m | 48h34m |

## after: n=1 merged batches, 2 landed members (0 closed batches, 0 drops, 1 reruns, 1 of them for lanes the ruleset does not require, 2 direct merges)

| Phase | n | median | max | total |
| --- | --- | --- | --- | --- |
| awaiting admission (opened -> label) | 2 | 3h21m | 5h49m | 6h43m |
|   of which revising (opened -> last push) | 2 | 2h16m | 4h32m | 4h32m |
|   of which after the last push | 2 | 1h05m | 1h17m | 2h11m |
| awaiting build (label -> first build) | 2 | 38s | 39s | 1m15s |
| rebuild repair (first build -> landing build) | 2 | 0s | 0s | 0s |
| build (merge commits) | 1 | 2s | 2s | 2s |
| local validation (head -> batch PR) | 1 | 10m41s | 10m41s | 10m41s |
|   of which the local full suite | 1 | 9m36s | 9m36s | 9m36s |
| CI first pass (batch PR -> last attempt-1 job) | 1 | 58m52s | 58m52s | 58m52s |
|   critical path: runner wait | 1 | 14m16s | 14m16s | 14m16s |
|   critical path: execution | 1 | 44m32s | 44m32s | 44m32s |
|   critical path: dispatch and job gaps | 1 | 4s | 4s | 4s |
| failure repair (reruns after the first pass) | 1 | 15m43s | 15m43s | 15m43s |
| awaiting merge (last job -> merged) | 1 | 1m35s | 1m35s | 1m35s |
| batch PR wall (opened -> merged) | 1 | 1h16m | 1h16m | 1h16m |
| member lead (label -> merged) | 2 | 1h27m | 1h27m | 2h55m |

## Batches

| Batch | regime | members | outcome | build | local validation (suite) | CI first pass (runner wait / execution / dispatch) | failure repair | awaiting merge | wall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #850 | before | #833, #835, #845, #849, #840, #834, #837, #838, #846, #848 | merged | - | - (-) | 1h20m (10s / 1h20m / 5s) | 0s | 49s | 1h21m |
| #865 | before | #842, #853 | closed (rebuilt as #866) | 1s | 9m07s (7m34s) | 5m54s (6s / 5m43s / 5s) | 0s | - | 4m46s |
| #866 | before | #853, #842 | merged | 2s | 16m34s (7m23s) | 1h29m (7s / 1h29m / 6s) | 25m21s | 32s | 1h55m |
| #868 | before | #859, #862, #863, #858 | merged | 5s | 12m57s (11m36s) | 2h55m (2h23m / 31m57s / 4s) | 0s | 31s | 2h56m |
| #876 | before | #860, #861, #869, #870, #871, #839, #872 | merged | 12s | 10m00s (8m47s) | 50m53s (16m54s / 33m55s / 4s) | 0s | 1m50s | 52m43s |
| #882 | before | #860, #874, #847, #878, #879 | merged | 6s | 10m18s (8m04s) | 51m58s (5m44s / 46m04s / 10s) | 5m15s | 56s | 58m09s |
| #887 | before | #883, #880 | merged | 1s | 8m46s (7m22s) | 58m00s (15m07s / 42m46s / 7s) | 0s | 1m19s | 59m19s |
| #889 | before | #885, #886 | merged | 1s | 8m58s (7m58s) | 1h17m (38m35s / 38m20s / 6s) | 0s | 1h09m | 2h26m |
| #892 | after | #888, #891 | merged | 2s | 10m41s (9m36s) | 58m52s (14m16s / 44m32s / 4s) | 15m43s | 1m35s | 1h16m |

## Critical paths, reruns, drops

- #850 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / cli -> Rust shim: Parity / ubuntu-latest
- #865 critical path: Rust shim: Scope -> Rust shim: Candidate / ubuntu-latest -> Rust shim: Parity / ubuntu-latest
- #866 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / cli -> Rust shim: Parity / ubuntu-latest
  - rerun Rust shim attempt 2: clicked 38s after the failed attempt, ran 24m43s (runner wait 9m14s); cause: Parity checks / windows-latest / h5-daemon-mutants failure (not a required lane): Process completed with exit code 1.; Parity / ubuntu-latest failure (required): Process completed with exit code 1.; Parity / windows-latest failure (required): Process completed with exit code 1.
- #868 critical path: Rust shim: Scope -> Rust shim: Candidate / windows-latest -> Rust shim: Parity checks / windows-latest / w1c-doctor -> Rust shim: Parity / windows-latest
- #876 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / cli -> Rust shim: Parity / ubuntu-latest
- #882 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / cli-daemon -> Rust shim: Parity / windows-latest
  - rerun Rust shim attempt 2: clicked 1m19s after the failed attempt, ran 3m56s (runner wait 6s); cause: Parity checks / ubuntu-latest / h5-background failure (not a required lane): Process completed with exit code 1.; Parity / ubuntu-latest failure (required): Process completed with exit code 1.; Parity / windows-latest failure (required): Process completed with exit code 1.
- #887 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / w1c-connection -> Rust shim: Parity / ubuntu-latest
  - dropped #884: changes the producer census inputs (rust/producer-census.json) while the committed census is stale on the batch head, so CI's census check (`python rust/producer_census.py --ci-check`) would fail; once master holds the rest of the batch, merge master forward, regenerate the census with `python rust/producer_census.py`, let CI pass, then re-apply the label
- #889 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / w1c-connection -> Rust shim: Parity / ubuntu-latest
- #892 critical path: Rust shim: Scope -> Rust shim: Rust / windows-latest -> Rust shim: Parity checks / windows-latest / w1c-connection -> Rust shim: Parity / ubuntu-latest
  - rerun CI attempt 2: clicked 28m57s after the failed attempt, ran 13m20s (runner wait 1m10s); cause: test-lite-macos cancelled (not a required lane) , no runner acquired: The job was not acquired by Runner of type hosted even after multiple attempts
  - CI on the member's own head was not green at build: #891

## Members

| Member | batches | outcome | awaiting admission (revising / after last push) | awaiting build | rebuild repair | lead |
| --- | --- | --- | --- | --- | --- | --- |
| #833 | #850 | merged | - (- / -) | - | - | - |
| #834 | #850 | merged | - (- / -) | - | - | - |
| #835 | #850 | merged | - (- / -) | - | - | - |
| #837 | #850 | merged | - (- / -) | - | - | - |
| #838 | #850 | merged | - (- / -) | - | - | - |
| #839 | #876 | merged | 26h17m (21h57m / 4h19m) | 2m01s | 0s | 1h04m |
| #840 | #850 | merged | - (- / -) | - | - | - |
| #842 | #865, #866 | merged | 15h51m (10h04m / 5h47m) | 32s | 1h47m | 3h59m |
| #845 | #850 | merged | - (- / -) | - | - | - |
| #846 | #850 | merged | - (- / -) | - | - | - |
| #847 | #882 | merged | 29h54m (25h34m / 4h19m) | 1m11s | 0s | 1h09m |
| #848 | #850 | merged | - (- / -) | - | - | - |
| #849 | #850 | merged | - (- / -) | - | - | - |
| #853 | #865, #866 | merged | 5h44m (0s / 5h44m) | 27s | 1h47m | 3h59m |
| #858 | #868 | merged | 9h19m (0s / 9h19m) | 13s | 0s | 3h09m |
| #859 | #868 | merged | 8h19m (15m06s / 8h03m) | 26s | 0s | 3h09m |
| #860 | #876, #882 | merged | 13h06m (0s / 13h06m) | 2m24s | 0s | 1h05m |
| #861 | #876 | merged | 13h02m (7h46m / 5h16m) | 2m20s | 0s | 1h05m |
| #862 | #868 | merged | 8h03m (9m47s / 7h54m) | 22s | 0s | 3h09m |
| #863 | #868 | merged | 7h47m (0s / 7h47m) | 17s | 0s | 3h09m |
| #869 | #876 | merged | 4h38m (0s / 4h38m) | 2m15s | 0s | 1h05m |
| #870 | #876 | merged | 4h28m (0s / 4h28m) | 2m11s | 0s | 1h05m |
| #871 | #876 | merged | 4h23m (0s / 4h23m) | 2m06s | 0s | 1h05m |
| #872 | #876 | merged | 3h51m (0s / 3h51m) | 1m00s | 0s | 1h03m |
| #874 | #882 | merged | 1h53m (0s / 1h53m) | 5h15m | 0s | 6h23m |
| #878 | #882 | merged | 4h22m (0s / 4h22m) | 1m08s | 0s | 1h09m |
| #879 | #882 | merged | 4h10m (31m17s / 3h39m) | 1m05s | 0s | 1h09m |
| #880 | #887 | merged | 6h01m (4h25m / 1h36m) | 1h29m | 0s | 2h37m |
| #883 | #887 | merged | 1h23m (0s / 1h23m) | 1h30m | 0s | 2h38m |
| #884 | #887 | dropped | 56m39s (0s / 56m39s) | 45s | - | - |
| #885 | #889 | merged | 10h26m (17m20s / 10h09m) | 34s | 0s | 2h35m |
| #886 | #889 | merged | 9h57m (0s / 9h57m) | 31s | 0s | 2h35m |
| #888 | #892 | merged | 5h49m (4h32m / 1h17m) | 39s | 0s | 1h27m |
| #891 | #892 | merged | 53m52s (0s / 53m52s) | 36s | 0s | 1h27m |

## Merged to master outside a batch

- #831 2026-10-19T02:38:41Z (rust-cf/w2e-supersede): feat(rust): serve memory_supersede and memory_reinstate over MCP
- #832 2026-10-19T04:20:55Z (rust-cf/w2e-forget): feat(rust): serve memory_forget over MCP
- #843 2026-10-19T05:37:36Z (claude/ecstatic-feynman-4bfebe): docs(claude-md): cut the agent rulebook to 40% of its size, keeping every rule
- #841 2026-10-19T10:21:05Z (claude/festive-fermi-92708a): ci: let queued merge groups run CI, so master can use GitHub's merge queue
- #852 2026-10-19T15:37:02Z (revert/835-body-limit-test): revert(parity): back out #835 so master's Windows cli parity shard goes green again
- #854 2026-10-19T16:09:27Z (docs/ci-flake-admission-rule): docs(claude-md): admit timing-sensitive tests on hosted Windows evidence, not VM runs
- #857 2026-10-19T17:41:27Z (docs/ci-rules-review-fixes): docs(claude-md): make the two Windows CI rules applicable literally
- #851 2026-10-19T21:24:48Z (claude/dazzling-bardeen-05269a): feat(ops): batch ready PRs so master stops forcing serial CI reruns
- #877 2026-10-21T05:28:35Z (fix/w2e-init-warmup-settle): test(rust-harness): stop w2e step 0 reporting a half-landed warmup probe
- #890 2026-10-21T06:45:04Z (ci/batch-without-double-ci): ci: wait for hosted CI once per change: batches run the full lanes, PR pushes run the fast ones
- #893 2026-10-21T09:29:26Z (ci/integrate-merge-guard): ci(integrate): merge a batch only when its full CI is green, and never an ordinary PR
