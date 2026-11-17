### Fixed (2026-11-17 — Record every batch gate before opening)
- The batch tool runs the Rust harness on the Windows dev VM and shim unit tests on the second Linux dev box, requiring successful results for the built head before opening. After-drop builds waive absent results, retaining the compile check; supplied results must still pass on that head.
- A shared VM database lock prevents the harness and wrapped worker commands from disrupting one another's test PostgreSQL; bounded waits identify the holder.
- Batch status reports own-head Rust jobs and snapshot admission reasons before a build.
- Remote member code receives no ambient credentials or test-selection overrides. The harness refuses reparse points before resetting or cleaning its reusable worktree.
