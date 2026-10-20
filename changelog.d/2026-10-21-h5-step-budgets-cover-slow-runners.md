### Fixed (2026-10-21 — slow hosted runners no longer time out the H5 parity steps)
- The Linux H5 parity steps' time limits are now five times each suite's
  hosted ubuntu p99 (about 300 successful steps per suite, 2026-10-11 to 10-20) instead
  of about twice a 2026-10-11 local measurement. Master f82141c4's runner ran
  about five times slower than usual, so `h5-graph-mutants` hit its
  14-minute limit with no mutant surviving; it now has 50 minutes, and its
  job 65. `h5-backup`'s slowest normal step had reached its 2-minute limit.
  Windows limits are unchanged. The distribution is recorded in
  `rust/ci/h5_linux_measurements.json`, and `rust/ci/test_workflow.py` pins
  the limits to it.
