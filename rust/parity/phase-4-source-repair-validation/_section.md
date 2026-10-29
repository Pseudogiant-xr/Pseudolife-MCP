## Phase 4 source repair validation boundary

`LeaseHome` always sets `PSEUDOLIFE_DAEMON_EXEC=1`, preventing operator tests
without a bank from entering a real daemon container. The additive admission
test first proves its owned synthetic Docker executable works, then requires
both fixture constructors to refuse without invoking it. A separate owned
transport test explicitly removes the guard to inspect designate's rewritten
argv and single deprecation, without using a real container.

The named **`lease-pid-platform-range`** rule retains Python `int()` grammar at
`pseudolife_memory/lease_cli.py:1693-1700`: Unicode decimal digits, underscores
between digits, surrounding whitespace and one sign. Zero and negative values
retain the exact positive-whole-number diagnostic. Platform PID producers
(`$$`, `$PID`, Get-Process and launchers) supply representable PIDs. Other
positive values refuse exit 2 with the usage-shaped `not a process id` error
before any lock, board or child effects: POSIX permits 1 through i32::MAX and
Windows permits 1 through u32::MAX. This avoids the POSIX `os.kill` overflow at
line 1267 and Windows DWORD truncation. Original baseline nodes are
`tests/test_lease_cli.py::test_hold_and_check_usage_errors_exit_2`,
`test_hold_of_a_pid_that_is_gone_releases_at_once` and
`test_hold_keeps_the_lease_while_the_pid_lives_then_releases`.
The port-owned boundary tests require Windows 4294967295 exit 0, POSIX exit 2
with unchanged files, and rejection above the respective range.

DB-free operator units pin all secret/ordinary vectors from unchanged
`tests/test_coordination_secrets.py` and append's canonical material against
the unchanged Python `coordination.audit_hash` producer, including non-ASCII
payload and a null recipient. Audit predecessor and unrelated-payload controls
must change the hash. At source checkpoint `3583bb6c` these controls were unrun;
prior receipts and hosted failures retain their original identities.

Accepted doorbell test repair `7a2d59b2` was merged once into this branch.
Windows successor `58b8f9ff` passed format, check, Clippy with warnings denied
and 196 affected Rust tests under each feature configuration. Its initial
owned-container argv test failed because CMD consumed a trailing digit as a
redirection descriptor; moving the fixture redirection retained the exact
assertion, and all three admission controls then passed. Production bytes are
unchanged from built source `5e6e62f1`.
Whole Python/native Windows no-bank and malformed-DSN controls matched exact
bytes except the named parenthesized native diagnostic; PID grammar refusals
matched, and the native range refusal preceded effects. The unusable-bearer
hold pair matched exact streams and files, retained local ownership and the
followed child, then released normally. Its state, stream and lifecycle
rejecting controls passed. No real container, daemon or PostgreSQL bank was
started for these cells. Remote range/state/TLS/mirror recapture, final hosted
gates and fresh independent review remain pending. All lease acceptance rows
remain deferred.
