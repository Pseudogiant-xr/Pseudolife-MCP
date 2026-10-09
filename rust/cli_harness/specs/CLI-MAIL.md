# CLI-MAIL spec: `pseudolife-mcp wait-mail`

Python source: `pseudolife_memory/wait_mail_cli.py` (dispatch `cli.py:205-207`),
`coordination_identity.py:18-80`, `wake_liveness.py:53-82`. Native:
`rust/shim/src/cli/wait_mail.rs`, `wait_mail_args.rs`.
Harness cases: `rust/cli_harness/rows/mail.py`.

## Canonical inputs

Argv the shipped producers and docs pass (`docs/guide/configuration.md`
"Waking an idle session", CLAUDE.md `--timeout 7000`): `wait-mail`,
`--session-id ID` or `--digest PATH` (mutually exclusive), `--timeout S`,
`--interval S`, `-h`/`--help`. Environment: `CLAUDE_CODE_SESSION_ID`,
`PSEUDOLIFE_DIGEST_DIR`, `CLAUDE_PID` with its `claude-<pid>.host` record.
Files: the adapter's digest, `.ring` and `.seen` writers
(`coordination_adapter.py` `_write_digest`, `_write_ring`, `_mark_seen`,
all through `_write_private`). Every other argv shape is deferred.

## Exact contract items

| Item | Python source |
|---|---|
| Exit 0 ring / 3 timeout / 2 setup, argument and stdout failure | `wait_mail_cli.py:49-51,232-306` |
| Help text at COLUMNS=80; argparse usage/error lines and exit 2 | `:83-99,233-243` |
| Bounds: timeout (0, 86400], interval [0.01, 60], digest name `[0-9a-f]{64}.txt` | `:236-241` |
| Digest path: `--digest`, else session id (`--session-id`, else env) keyed by SHA-256 under `PSEUDOLIFE_DIGEST_DIR` or `~/.pseudolife-mcp/digests`; pathlib spelling of the path in diagnostics | `coordination_identity.py:18-35`, `wait_mail_cli.py:245-255` |
| `/clear` host record: digits-only `CLAUDE_PID`, env session match, regular file, line 1 key, line 2 SHA-256 of the session | `coordination_identity.py:40-80` |
| Setup diagnostics: no session, no digest file (missing or not regular), stat refusal | `wait_mail_cli.py:251-271` |
| Fire rule: digest watermark and ring watermark past `.seen`, non-blank body, `rung` ring | `:197-229` |
| Ring grammar: regular file, line 1 minus CR/space is 1-12 ASCII digits, line 2 minus one CR is `rung [-A-Za-z0-9_ ]*` | `:111-130` |
| `.seen` read: strip, empty is 0; write: never lower, `<n>\n`, atomic replace | `:146-173` |
| Digest gone mid-wait: exit 2 diagnostic | `:203-205,276-279` |
| Delivery: stderr announcement, stdout body verbatim, then `.seen`, then ledger line `epoch\twait\tkey8\twatermark\tchars\tring` | `:287-306,176-182` |
| Body flushed before the marker moves | `:291-301` |
| Own `.wait-armed` listener record while armed (`<key>.<token>.wait-armed`, token then an expiry at most a minute ahead), removed on exit | `wake_liveness.py:53-82` |
| Diagnostic path spelling: pathlib's (native separators, `.` dropped, two leading POSIX slashes kept) | `wait_mail_cli.py:92,246,262-265` |

## Free items (named rules in `normalize.py`)

- `mail-clock`: the announcement's `HH:MM:SS` (on the running host's clock)
  and elapsed seconds, and the ledger epoch, each inside its own arm's
  invocation window.
- `mail-stdout-error-text`: the parenthesized OS error text inside the
  closed-stdout diagnostic; the rest of the line and the exit stay exact.
- `python-shutdown-flush`: CPython's shutdown flush trailer and exit 120 after
  its own exit 2 for an unwritable stdout (declared `wait-mail-direct-stdout`).

## Declared divergences (not exercised by canonical cases)

The `wait-mail` producer substitutions table in `rust/PARITY.md` stands:
arbitrary-width digest/seen counters and corrupt-record refusal, ASCII numeric
option grammar, COLUMNS other than 80, failed stderr writes (exit 120),
native temporary-name collisions and native error-text mapping. Not covered
by the harness: Ctrl-C (exit 130) and Windows sharing violations mid-replace.
Stat refusal is proven on Linux by the `stat-refused` case (chmod 000) and on
Windows by the injected `metadata_error_refuses_arming_and_retries_during_wait`
unit.
