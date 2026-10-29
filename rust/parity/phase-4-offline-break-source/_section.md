## Phase 4 offline break source preparation

`shim/src/cli/lease/operator/` prepares native external-DSN break with the
reviewed shared PG client. It locks the lease before waiters, vacates the
holder, removes departed waiters, skips revoked waiters and grants the earliest
eligible ticket with the existing 300-second grant window. Reserved delegate/
designated queues dequeue instead of inheriting an operator role. Mutation and
ordered audit append share one transaction; the audit advisory lock is last,
and success is printed only after an observed commit. Names retain Unicode,
whitespace, length and credential-shaped-text admission before durable writes.
Break never acquires an OS lease lock or signals its holder's process.

The retained container transport uses the same Docker inspect and `docker exec`
argv, inherited streams, terminal choice and container exit. Its Python command
runs inside the daemon container; no host Python storage bridge or agent HTTP
operator route is introduced. The named **`phase4-embedded-pg-deferred`** rule
refuses an existing embedded bank with exit 1 and stderr
`lease: break refused: phase4-embedded-pg-deferred`; it never starts, attaches to
or stops that bank. An explicit DSN takes priority. Native detection uses the
existing bank marker without importing the host's pg0 package.

The named **`native-pg-diagnostics`** rule replaces generic psycopg class names
with safe native PG diagnostics, retaining exit 1, empty stdout and stderr-only
placement. This substitution replaces only the parenthesized exception class
at `pseudolife_memory/lease_cli.py:1639` (`_operator`); it does not add a
`lease: ` prefix. A Windows Python 3.11.9 / psycopg 3.3.4 whole-command
`lease designate resource fixture-agent`, with explicit DSN `not-a-dsn`,
observed `ProgrammingError` before connection setup. Its stdout is empty,
exit is 1, and stderr is the designate deprecation line followed by
`cannot open the bank (ProgrammingError)`. Native DSN admission must use
`cannot open the bank (PostgreSQL DSN is not understood)`; the port-owned
control asserts the same streams, exit, ordering and unchanged files,
without a socket or listener. `designate` retains its deprecation
before the refusal. Unknown DSN options use the fixed parenthesized diagnostic
`unsupported PostgreSQL DSN option`; operator diagnostics never echo an option
spelling supplied by the DSN, which can be a malformed password fragment.
The no-bank line remains exactly the prefix-free producer at
`pseudolife_memory/daemon_exec.py:40-43` and `lease_cli.py:1887-1888`.
The designate rewrite at `lease_cli.py:1854-1859` also applies to the argv
forwarded into the container, so its deprecation is emitted once.
Coordination refusal codes and their surrounding bytes
remain exact. The separate named embedded refusal retains its specified
diagnostic. Failure controls must
compare every durable row, raw stream and local file; no row or time field is
dropped. Transport loss around COMMIT needs an actual outcome/rollback control
before acceptance; preparation is not a guarantee about an unobserved commit.
[Operator closure](../cli-lease-operator-closure-2026/) partially answers
it with server-side aborts at COMMIT; a transport cut is not yet reproduced.

The additive [operator fixture plan](../../../evals/rust_port/lease_operator_cases.json)
pins the first missing-row whole-command cell, queue/reserved/revocation,
rollback, secret-boundary and container/refusal controls. Held-row comparisons
use `fixed-clock-replay` with the same clock seam in both arms and exact audit
hashes. No build, service or native action was executed at the original break
source checkpoint. Subsequent targeted execution is recorded above against
`67478612`; those receipts retain that source identity. New source repairs need
new validation. Broader whole-command, Windows durable-state, cancellation/
rollback coverage, final hosted gates and fresh independent review remain pending.
Every lease mode row remains deferred.
