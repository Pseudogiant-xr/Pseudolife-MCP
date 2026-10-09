# CLI-LEASE remainder

This row covers native `lease hold NAME --while-pid PID` with the documented
expect/ttl/purpose/worktree/no-board options, `lease break NAME`,
`lease delegate PROJECT AGENT [--for DURATION]`, and the deprecated
`designate` alias. Producers are the command help, lease examples in
`docs/guide/configuration.md`, and `evals/qwen_server.ps1`'s lease helpers.
The core check/list/run row keeps its own acceptance.

The operator corpus has 32 Windows and 31 Linux cases. It covers missing,
free and held leases; FIFO handoff; preservation of a separately held OS
lock; delegate replacement and coordinator-role handoff; unique/ambiguous
prefixes and unknown recipients; default/day durations and Unicode project
names; active listeners/rings and warnings; alias/help/refusal/output
behavior; PID lifetime, local contention and mirror renewal. Three owned
fake-Docker executables check inspect/exec argv, inherited binary streams,
stdin and container exit status. One Windows-only CPU launcher case invokes
the shipped PowerShell helpers against the actual arm's CLI and verifies
free/held/refusal/free transitions, bound argv and owned PID/start-time
cleanup. It calls no GPU launcher. Its exact PowerShell startup-profile
cache path is pre-seeded as a directory and preserved in both observations.

Banks are disposable `pl_cf_lease_<n>` databases through the test login.
Seed state is written by `CoordinationStore`. Every CLI mutation compares
the before state and committed coordination rows/sequence positions; mirror
calls capture every intermediate write. The original in-process Python
tests remain oracle tests; no external boundary adapter replaces them.

Named rules extend the shared core rules:

- `lease-operator-clock` verifies each complete raw audit chain and unchanged
  old prefix before associating clocks. A new transaction's event timestamp
  must be one finite value inside that arm's invocation window. Expiry and
  expected-end fields retain their exact duration relative to that timestamp;
  FIFO grants use the original waiter's TTL capped at the grant window.
  Seed timestamps use their own captured seed window. Projected hashes are
  recomputed only after raw-chain verification. Invalid paths remain raw.
- `lease-fixture-pid` associates the actual watched fixture PID in streams,
  requests and state, verifying raw chains before recalculating hashes.
- `lease-launcher-owned` associates only a validated bound executable/leading
  argv and owned process IDs in its route records. Every other route field,
  stream and file stays compared; immutable fixture inputs are verified
  before removal.

Four new real source mutants cover hold status, break holder removal,
delegate duration and designate deprecation. Pure CPU controls reject clock,
duration, audit-prefix/hash and PID tampering.

External DSNs and retained container transport are admitted. Native embedded
PostgreSQL remains the named `phase4-embedded-pg-deferred` refusal (blocker:
native `embedded_pg`); native operator admission tests exercise it. Safe
native PostgreSQL diagnostic classes replace interpreter exception names.
Noncanonical argparse/path shapes remain excluded. There is no per-row
performance or installed-candidate claim.
