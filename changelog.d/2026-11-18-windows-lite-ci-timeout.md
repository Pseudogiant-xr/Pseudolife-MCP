### Fixed (2026-11-18 — Windows integration test timeout)
- Give the Windows lite CI job 40 minutes after both attempts on batch #1742 hit the 30-minute limit with tests still to run (1,732 and 1,664 passed, 23 skipped each). The restored cache lacked the aws-lc-sys build for the new AWS_LC_SYS_NO_JITTER_ENTROPY setting, adding up to about two minutes over a warm build.
