### Changed (2026-10-21 — a change waits for hosted CI once, on its batch, not twice)

- `python ops/integrate.py build` admits a `ready-to-batch` pull request whose own CI is pending or absent; only a failing head (a known failure) sits the batch out. The batch's CI is its members' CI. Of the four things that held batches up on 2026-10-20/21 (#877 breaking only against a newer master, the h5-background harness race fixed by #883, a stale producer census, #875's unresolvable lock), green CI on the member's own head caught none.
