### Fixed (2026-11-13 — an interrupted restore names its recovery command)

- Pressing Ctrl-C during `pseudolife-mcp restore` after the database drop
  has begun now stops the daemon and prints the same `Recover with
  pseudolife-mcp restore --backup-file <safety backup> ...` command a
  failure there prints, then exits as interrupted. Before, the interrupt
  left a dropped or half-replayed bank with no instruction naming the
  safety backup. A second Ctrl-C while the daemon is being stopped still
  prints it, saying to stop the daemon first.
