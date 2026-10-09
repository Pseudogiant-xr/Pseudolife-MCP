# CLI-LEASE-core

Canonical producers are `pseudolife_memory/lease_cli.py`'s help and
`ops/remote-suite.ps1`, `ops/wsl-suite.ps1`, the suite gate in
`.github/workflows/rust.yml`, the `evals/qwen_server.ps1` launch gate, and the lease examples in
`docs/guide/agent-isolation.md` and `docs/guide/configuration.md`.
The row admits `lease check NAME [--json]`, `lease list [NAME] [--json]`,
and `lease run NAME [--expect DURATION] [--ttl DURATION] [--purpose TEXT]
[--no-board] [--timeout DURATION] -- COMMAND...`, plus their help.
`hold` and the offline operator actions are the separate CLI-LEASE remainder.

The 66 cases exercise absent/free/held OS locks, eight-slot suite lock
selection and another machine's suite name, the board's independent session
claim, same-instance stale mirror and another instance's mirror, FIFO
acquisition behind an earlier waiter, transient retry, registration refusal,
malformed registration, local contention, nested refusal, timeout, hashed
lock names, missing executables, binary child streams and child exit status.
Closed stdout is tested for check/list/help and for the inherited child.

Each arm receives a disposable home and a loopback fixture daemon. PostgreSQL
databases have unique `pl_cf_lease_<n>` names and use only the test login.
`CoordinationStore` writes all agent, lease, waiter and audit state. Its clock
and registration entropy are injected at the Python writer boundary, with
canonical UUID4 ids and 43-character URL-safe credentials. The fixture does
not replace CLI code or issue SQL inserts. Every successful register, acquire
and release is followed by a dump of coordination table rows and their
sequence positions; those intermediate states and the final state compare
exactly. Unrelated schema catalogs are excluded from the lease observations.
FIFO fixtures also capture both fixture releases separately. Databases and
owned lock-holder processes are cleaned up after each arm; process cleanup
uses the original process object, recorded PID and OS start time.

The named comparison rules are narrow:

- `lease-http-json` compares each captured request's decoded JSON and checks
  the declared byte length against the parsed body. This is a body consistency
  check, rather than an independent HTTP framing measurement. JSON whitespace and the resulting byte length
  are not application fields. All JSON values and other admitted wire
  headers remain exact. Python's `python-httpx/<numeric-version>` User-Agent
  is omitted; the native lease client does not send a library User-Agent.
- `lease-seeded-clock` validates displayed holder age against the arm's own
  invocation window and the fixed producer timestamp, and displays of the
  two fixed timestamps against their own captured historical UTC offsets. Surrounding
  text, timestamps in JSON and database state remain exact.
- `lease-native-stdout` declares the native diagnostic `lease: stdout write
  failed` in place of CPython's exact closed-stdout shutdown trailer. Both
  must have empty stdout and exit 120. Other stderr text and every file
  remain exact; an unrecognized diagnostic does not normalize.

The fixture children explicitly select UTF-8, so their interpreter encoding
is the same in both arms. Interpreter-specific argument parsing/path corner
cases, noncanonical producer shapes, and the phase 4 actions are excluded
from this core row. There is no per-case performance acceptance.

Seven real source mutants cover lock-name hashing, child exit status, local
lock truth, same-instance stale mirrors, timeout status, mirror release and
the named list filter.
Goldens are recorded separately on Windows and Linux and use the same rules
as live comparison.

`lease-native-signal-lifecycle` governs SIGINT and child stopping through
the native lifecycle implementation: exit `128 + signal`, forward the signal
to the owned child, bounded escalation, and local-lock release before board
cleanup. The unchanged native `lease_sigint` probes pass on Linux (three
tests); signal-delivery scheduling and interpreter traceback bytes are not
part of this differential row. This is a declared substitution, not a new
signal-equivalence claim.
