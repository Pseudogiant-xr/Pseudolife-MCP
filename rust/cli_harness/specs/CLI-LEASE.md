# CLI-LEASE remainder

This row covers native `lease hold NAME --while-pid PID` with the documented
expect/ttl/purpose/worktree/no-board options, `lease break NAME`,
`lease delegate PROJECT AGENT [--for DURATION]`, and the deprecated
`designate` alias. Producers are the command help, lease examples in
`docs/guide/configuration.md`, and `evals/qwen_server.ps1`'s lease helpers.
The core check/list/run row keeps its own acceptance.

The operator corpus has 37 Windows and 36 Linux cases. It covers missing,
free and held leases; FIFO handoff; preservation of a separately held OS
lock; delegate replacement and coordinator-role handoff; unique/ambiguous
prefixes and unknown recipients; default/day durations and Unicode project
names; active listeners/rings and warnings; alias/help/refusal/output
behavior; PID lifetime, local contention and mirror renewal. Five failure
cases install a trigger in each arm's own bank after seeding: an audit
INSERT the server refuses inside the block (a FIFO break), a COMMIT the
server refuses (a FIFO break, and a coordinator handoff by delegate), and a
session the server ends at COMMIT: the backend terminates itself in a
deferred trigger before the commit record, so the client's answer is FATAL
57P01 (a held break, and a coordinator handoff by delegate; named
`*-commit-lost` after transfer's identical fixture). The first two kinds
print the certain `nothing was changed` line; an ended session prints the
COMMIT-outcome-unknown line, because a FATAL, PANIC or missing answer leaves
the client unable to tell whether the commit happened. Each oracle arm must
exit 1 with its expected failure line and class and
every coordination row exactly as seeded; sequences may only advance,
because a FIFO grant's fence `nextval` is not rolled back, and a FIFO
case's fence must have advanced, proving the block reached the grant. The candidate's rows and exact
sequence positions are compared with the oracle's. Three owned
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
- `lease-native-pg-diagnostics` (the `native-pg-diagnostics` rule) maps
  only the oracle arm's single whole stderr failure line of the kind the
  case expects, with exit 1 and the class the case expects. A refused
  COMMIT or audit INSERT expects `<break|delegate> failed (RaiseException);
  nothing was changed`, mapped to the candidate's `(PostgreSQL transaction
  failed)`. An ended session expects ``<break|delegate> lost the answer to
  its COMMIT (AdminShutdown); it may have been committed: run
  `pseudolife-mcp lease list` before retrying``, mapped to the candidate's
  `(PostgreSQL session ended)`. Any other line, line kind, class, exit or
  arm stays raw.
- `lease-launcher-owned` associates only a validated bound executable/leading
  argv and owned process IDs in its route records. Every other route field,
  stream and file stays compared; immutable fixture inputs are verified
  before removal. The bound span must occur exactly five times in one JSON
  spelling, either pwsh 7's literal non-ASCII or the escaped form, so a
  non-ASCII installation path associates.

Eight real source mutants cover hold status, break holder removal,
delegate duration, designate deprecation, a swallowed audit-insert failure
(break then reports success on a rolled-back bank), an ignored COMMIT
result (delegate then reports a grant that never committed), a lost COMMIT
answer reported as certainly rolled back, and a refused COMMIT reported as
an unknown outcome. Pure CPU controls reject clock,
duration, audit-prefix/hash and PID tampering.

External DSNs and retained container transport are admitted. Native embedded
PostgreSQL remains the named `phase4-embedded-pg-deferred` refusal (blocker:
native `embedded_pg`); native operator admission tests exercise it. Safe
native PostgreSQL diagnostic classes replace interpreter exception names.
Noncanonical argparse/path shapes remain excluded.

Declared scope of the failure outcomes. Both implementations print
`nothing was changed` for every database failure, including one raised by
COMMIT itself; in the session-ended fixture the server rolls back, so the
line is true there, but after a transport cut neither client can know it.
That wording is the oracle's (`lease_cli.py` `_operator`), so a change
follows Python first. Transport loss around COMMIT is not reproduced. Cancelling the host CLI while a retained
container transport runs is not exercised: the transaction then runs in the
container's own Python oracle, and the native side only relays argv,
streams and exit status, which the fake-Docker cases compare. There is no per-row
performance or installed-candidate claim.
