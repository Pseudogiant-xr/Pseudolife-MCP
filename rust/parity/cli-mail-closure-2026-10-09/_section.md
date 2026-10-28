## CLI-MAIL closure (2026-10-09)

The contract-first [CLI differential harness](cli_harness/README.md) closes
CLI-MAIL. It runs `python -m pseudolife_memory.cli wait-mail` and the native
binary on each canonical case (the argv shapes in the
[spec](cli_harness/specs/CLI-MAIL.md)) in the same disposable home, with state
seeded through the coordination adapter's own writers, and compares exit,
both streams, every file under the home (with POSIX mode bits) and the
waiter's `.wait-armed` listener record, observed once it has armed. All
cases match on Windows (40) and Linux (42, adding the chmod-000 stat refusal
and a double-leading-slash path). Three named rules apply,
each validated before it rewrites its span: `mail-clock` (announcement clock,
elapsed seconds and ledger epoch inside each arm's own window),
`mail-stdout-error-text` (OS error wording inside the closed-stdout line) and
`python-shutdown-flush` (CPython's shutdown trailer and exit 120 after its own
exit 2, the declared `wait-mail-direct-stdout` substitution). Eight source
mutants (ring width, decision, `.seen` advance, timeout exit, ledger column,
help text, listener renewal, leading `.` in paths) are each caught on both
OSes; CI runs the live and golden comparisons, the mutants run locally. The Windows stat refusal is the
injected `metadata_error_refuses_arming_and_retries_during_wait` unit, watched
red with its seam disabled. The harness and its reviews found path-spelling
defects in diagnostics, fixed here: mixed separators in the Windows default
digest path, a forward-slash `PSEUDOLIFE_DIGEST_DIR` not rejoined as pathlib
does, a leading `./` kept, and POSIX `//` collapsed. Not
covered: Ctrl-C (exit 130), Windows sharing violations mid-replace, and the
non-canonical inputs the substitutions below govern. Earlier receipts and the
13-digit negative control keep their own scope; the 13-digit ring is also a
harness case (exit 3 in both arms).
