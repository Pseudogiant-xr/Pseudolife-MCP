## CLI-LEASE operator closure (2026-10-10)

The [operator row](../../cli_harness/rows/lease_operator.py) closes the remainder
with 37 Windows and 36 local WSL/Linux live and golden matches. It verifies
hold lifetime, local contention and mirror renewal; break's missing/free/held
states and FIFO handoff; delegate replacement, coordinator handoff, recipient
resolution, durations and listener reachability; and the designate warning/forward.
Three owned container executables compare inspect/exec argv, binary streams,
stdin and exit status. The Windows-only CPU fixture invokes the shipped
PowerShell lease helpers and verifies owned PID/start-time cleanup.

Every write compares before/committed coordination rows and sequence positions.
Raw audit chains and unchanged prefixes are verified before the declared clock
and PID rules are applied. All 120 comparison controls pass; eight real source
mutants are caught.

Failure outcomes (added 2026-10-17, answering the review of `4b614194`). A
trigger installed in each arm's own bank after seeding makes the CLI's own
transaction fail three ways: an audit INSERT refused inside the block, a
COMMIT the server refuses, and a session the server ends at COMMIT: its
backend terminates itself in a deferred trigger, before the commit record,
so the client's answer is FATAL 57P01 (`break` on a held and a FIFO lease;
`delegate` on a coordinator handoff, for both a refused and an ended COMMIT).
The cases are named `*-commit-lost` after the transfer row's identical
fixture. For the first two, where the server's answer is an ERROR and it
rolled back, both arms exit 1 with `<action> failed (...); nothing was
changed`. For the session ended at COMMIT (answered 2026-10-17, after the
Python change in #798) the client cannot know the outcome, so both arms exit
1 with ``<action> lost the answer to its COMMIT (...); it may have been
committed: run `pseudolife-mcp lease list` before retrying``, the line
CoordinationConnection._txn's `CommitOutcomeUnknown` selects for a FATAL,
PANIC or missing answer. In every case only the parenthesized class
differs, under `native-pg-diagnostics`, and the rule maps only the line kind
the case expects. The oracle arm must leave every
coordination row exactly as seeded and must have reached the fault (its own
failure class; a FIFO block's fence `nextval` advanced), and the candidate's
rows and sequence positions must equal the oracle's. Sequences can advance on rollback (a FIFO
grant's fence `nextval`), and both arms do so identically. Two mutants that
report success after a swallowed audit failure or an ignored COMMIT result
are caught, and two more that report a lost COMMIT answer as certain, or a
refused COMMIT as unknown. This partially answers the COMMIT-loss outcome/rollback
requirement of the [break preparation](../phase-4-offline-break-source/):
in each case the server rolled back. The refused cases get a definite
answer (an ERROR); the ended session gets none the client can trust, which
is why both arms report its outcome as unknown.

Declared scope. Transport loss around COMMIT is not reproduced: a cut after
the server commits but before its answer arrives (a loopback TCP proxy could
produce it; none is built). Both implementations print the
COMMIT-outcome-unknown line there too, since neither gets a server answer;
the FATAL case above exercises the same branch. Cancelling the host CLI while the retained
container transport runs is not exercised: the transaction then runs in the
container's own Python oracle, and the native side only relays argv, streams
and exit status, which the three fake-Docker cases compare. Platform goldens name their Python oracle revision. The
existing Parity matrix runs these controls and operator live/golden comparisons
after its disposable PostgreSQL preparation; closing-head CI and independent
review are required before merge.

The [specification](../../cli_harness/specs/CLI-LEASE.md) declares the retained
`phase4-embedded-pg-deferred` refusal, safe native PostgreSQL diagnostics and
native signal lifecycle. External DSNs and retained container transport are
admitted. The original Python in-process tests remain oracle baselines. Full
lease mode closes over the canonical producer shapes covered by both CLI rows;
daemon coordination actions retain separate acceptance. No GPU, per-row
performance or installed-candidate claim is made.
