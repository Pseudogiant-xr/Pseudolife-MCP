# CLI-MAINTAINER contract (maintainer enrol-code / confirm / revoke / reset / list)

Oracle: `pseudolife_memory/maintainer_cli.py`, `pseudolife_memory/storage/maintainer.py`
(`MaintainerStore` host operations, schema v54 tables), `_key_change`'s audit
append in `pseudolife_memory/storage/coordination.py`. The line references
below were read at `origin/master` 1ed871a7; that pin is historical, and
the three files are unchanged from it to integration head 8263e7e3, so the
references hold there too. The harness always runs the oracle checkout
under test, not the pin. Candidate: `rust/shim/src/cli/maintainer/`.
Harness row: `maintainer` (`rust/cli_harness/rows/maintainer.py`).

## Canonical shapes (implemented)

- `maintainer enrol-code [--no-wait]`
- `maintainer confirm PREFIX`, `maintainer revoke PREFIX` (any single value but
  `-h`, `--help` and `--`, including a `-`-led id prefix, which `main` guards
  with an inserted `--`)
- `maintainer reset [--yes]`
- `maintainer list`

The bank is reached only through a non-empty `PSEUDOLIFE_MCP_DATABASE_URL`
that the shared native client admits (`rust/shim/src/pg/`), with its 5 s lock
timeout and public search path (`_bank`, maintainer_cli.py:58-80).

## Exact contract items

| Item | Python source |
|---|---|
| Exit 0 done, 1 refused, 2 nothing could be done | maintainer_cli.py:30, `main` 162-206 |
| No maintainer tables: `to_regclass('public.maintainer_passkeys')`, `error: this bank has no maintainer tables yet (schema older than v54); start a v54 daemon once to create them`, exit 2, checked before every action (also `reset` without `--yes`) | `_bank` 70-74, 201-203 |
| Refusals on stderr: `refused: ` + the `_REFUSALS` text, or the bare code (`coordination_unavailable`), exit 1; the transaction rolls back | `_REFUSALS` 32-38, 192-194 |
| `enrol-code`: 10 symbols of `ABCDEFGHIJKLMNOPQRSTUVWXYZ234567`; one transaction: `LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE`, refused `enrolment_closed` while any key is pending or active, `DELETE` unused codes, `INSERT (sha256(code), now+600)`; the two code lines; `--no-wait` exits 0 | `_enrol_code` 93-100, `bootstrap_code` storage/maintainer.py:724-736, `code_hash` 154-161 |
| Waiting form: `Waiting for the Console to redeem it...`; every 2 s (`--poll` default) redeemed (code row joined to its key: prefix and label, the four instruction lines, exit 0), else burned (unused row, `failed_attempts >= 5`: the burned line, exit 1); at 600 s on the monotonic clock `The code expired unredeemed.`, exit 1; all on stdout | `_enrol_code` 101-120, `bootstrap_redeemed` 738-744, `bootstrap_burned` 746-750 |
| Prefix: at least 6 code points of `[A-Za-z0-9_-]` (else `invalid_request`, inside the transaction, before any read); `credential_id LIKE prefix-with-_-escaped%` (`AND state='pending'` for confirm), `FOR UPDATE`; not exactly one row: `credential_not_found` | `_by_prefix` 752-762 |
| `confirm`: `state='active', active_from=now`; `maintainer_key` event `confirm`; `Active: <12>  label: <label>` | `confirm` 764-773, 164-167 |
| `revoke`: `state='revoked', revoked_at=now, revoked_by='host' WHERE ... AND state<>'revoked'`; the event only when a row changed; `Revoked: <12>  label: <label>` either way | `revoke` 775-785, 168-171 |
| `reset` without `--yes`: `reset revokes every passkey and rotates the secret; pass --yes` on stderr, exit 1, after the table check | 172-176 |
| `reset --yes`: revoke every unrevoked key (count), delete every bootstrap row and the secret, insert a fresh `token_hex(32)` JSON string (`ON CONFLICT DO NOTHING`), read it back (not 64 hex: `coordination_unavailable`), event `reset` with `revoked`; `Revoked N passkey(s); secret rotated; ...` | `reset` 787-797, `_secret` 205-213, 177-180 |
| Key-change event: `event='maintainer_key'`, `actor='operator'`, empty principal/agent/project/task/hlc, NULL recipient and message id, payload `_canonical({change, credential_id, label, by: 'host', path: 'host', revoked})`, its own clock read after the change's; chain lock `pg_advisory_xact_lock(hashtextextended('coordination-audit-chain', 0))`, head `ORDER BY seq DESC LIMIT 1` (genesis 64 zeros), `audit_hash` over the compact UTF-8 material with `float(created_at)` in CPython repr | `_key_change` 220-231, `_event` coordination.py:1223, `_chain_head` 1235, `_append` 1248, `audit_hash` 957-971 |
| `list`: `ORDER BY created_at, credential_id`; `No passkeys.` when empty; per key `<12>  <state:8> <repr(label)>  enrolled_by=<12>  active_from=<when>  last_used=<when>` and `  FLAGGED (sign count went backwards)` when `flagged_at` is truthy (0.0 is not, NaN is); `when` is `-` for NULL else `time.strftime("%Y-%m-%d %H:%M", time.localtime(value))` (floored seconds) | 181-190, `_when` 89-90, `key_prefix` storage/maintainer.py:164 |
| Text streams: UTF-8, LF translated to CRLF on Windows (stdout and stderr) | `print` |
| A Windows console: the listing goes through std's stdout, which writes UTF-16 with `WriteConsoleW` as CPython's console writer (`_WindowsConsoleIO`) does, so a label such as `☎` renders whatever the console's code page; any other stdout (a pipe, a file, every POSIX stream) takes the UTF-8 bytes through a counted duplicate of the descriptor. The harness pipes stdout, so the console branch is pinned by the unit test `only_a_windows_console_takes_the_console_path` (review of #675) | `sys.stdout` on a console |
| A committed change whose report stdout refuses: exit 120 (CPython's failed shutdown flush); the waiting form's code and `Waiting...` lines go out in one `flush=True` print, whose refusal first prints `error: <OSError on Windows, BrokenPipeError on POSIX>; check PSEUDOLIFE_MCP_DATABASE_URL` | `main` 162-191, 204-206, `_enrol_code` 96-102 |
| A failed COMMIT (the change may be durable) and any bank error while the committed code waits: `error: <class>; check PSEUDOLIFE_MCP_DATABASE_URL`, exit 2, never a deferral. `<class>` is psycopg 3.3.4's for the error's SQLSTATE (`_sqlcodes`, then the `_base_exc_map` prefix fallback, then `DatabaseError`); for a closed connection, the SQLSTATE of the server error that ended it (a FATAL such as 57P01 `AdminShutdown` that arrived between polls), else `OperationalError` | `main` 204-206, `_Storage._txn` 50-54, psycopg `errors._class_for_state` |

## Free items (not contract)

- The code, its hash, the rotated secret, and every timestamp the CLI writes
  from its clock (`active_from`, `revoked_at`, a code's `expires_at`, the
  event's `created_at`) and therefore the event's hash and a later row's
  `prev_hash`: tokenized by the named rule `maintainer-host-write` after
  validating each against this arm's run window, sha256 of the printed code,
  the seed's secret, and the oracle's `audit_hash` recomputed over the row.
  With a refused stdout the code is never shown, so the one unused code row
  in the window has only its 64-hex shape checked.
- CPython's ignored-exception trailer after a refused stdout (exit 120 kept):
  named substitution `maintainer-stdout-closed-trailer`.
- Statement texts (`SELECT credential_id,label` instead of `SELECT *` under
  the same `FOR UPDATE`), the code drawn as random bytes masked to five bits
  (uniform, as `secrets.choice`), the order of the code's generation and the
  connection. The prefix check is made inside the transaction, as Python
  makes it, before the transaction's first statement.

## Harness instruments (test-only, on each arm's own disposable copy)

- `commit-refused-*`: a deferred constraint trigger raising SQLSTATE P0001,
  XX999 (unknown: prefix fallback) or 57P01 at COMMIT, after every statement
  succeeded.
- `enrol-code-wait-terminated`: `pg_terminate_backend` of this login's other
  sessions on the arm's copy while the CLI waits between polls (guarded by
  `assert_disposable_database` on the same connection).
- `enrol-code-wait-lock-timeout`: `LOCK TABLE maintainer_bootstrap IN ACCESS
  EXCLUSIVE MODE` held while the CLI polls (55P03 after the 5 s lock timeout).
- `list-*` on Windows: `maintainer-list-guard` computes the native local-time
  rule with CPython's `time.localtime` over the seeded values, and when it
  trips (near New Year or a DST change) expects the deferral instead.

## Deferred (the dispatcher's `mode 'maintainer' is deferred` line, exit 1, before any effect)

- `setup` (guided and interactive: it probes the daemon's health over HTTP,
  detects Docker and Tailscale, may run `tailscale serve`, writes the daemon's
  config and restarts it, then drives enrol-code and confirm through the
  daemon's environment; its `--check` form also needs the daemon's config and
  the container transport, so no non-interactive shape is tractable here).
- Help, `--poll`, `--opt=value`, abbreviations, repeated or extra arguments,
  `confirm -- PREFIX`, non-Unicode argv.
- The lite tier's embedded bank and the daemon-container re-run (no or empty
  `PSEUDOLIFE_MCP_DATABASE_URL`), including Python's `no bank found` error;
  DSNs or ambient PG controls the shared client refuses; a server that does
  not answer (Python: `error: OperationalError; ...`, exit 2).
- Any SQL failure before the change's COMMIT (Python: `error: <class>; ...`,
  exit 2); the transaction rolls back, so nothing changed.
- `list` when stdout refuses it before any byte left (CPython: exit 120 with
  the trailer); whenever the encoded listing is 8192 bytes or longer,
  whatever stdout is, a healthy one included (unconditional: CPython would
  write such a listing part way through its prints, where a refused stdout
  takes a different path, and the native leaf does not reproduce that
  split); or when any shown time is negative, past 9999-12-31 UTC,
  or has a local year outside 1970-9999, `TZ` is set, or (Windows) the time,
  or a time two days either side of it, is outside the current local year
  or has another UTC offset: CPython's C runtime applies the zone's current
  rules to every year, Chrono asks Windows for each year's. A listing that
  stdout refuses after some bytes left exits 120, as CPython's shutdown
  flush does.

## Declared divergences and uncovered paths

- A lost connection with no server error (I/O failure, no FATAL received)
  prints `OperationalError`, psycopg's class there; not run by the harness.
- A refused stdout is classed `OSError` except a POSIX broken pipe
  (`BrokenPipeError`); other POSIX classes CPython would name for other
  write errors (for example `PermissionError`) are not reproduced.
- CPython buffering assumptions: the report and listing paths assume
  CPython's default block buffering of a non-terminal stdout; with
  `PYTHONUNBUFFERED` set the oracle writes per print and fails differently.
- The 600 s expiry of the waiting form is implemented but not run by the
  harness (it would hold each arm for ten minutes).
- Runs: the new cases are no longer pending on Linux; PR #675's body
  reports Windows 51/51 and Linux 51/51 at 267fc354. Later revisions record
  their runs in their commits.

## Goldens

Seeding is deterministic (fixed credential ids and authenticator keys, the
store's `secrets` drawn from a generator seeded per kind, the seed clock
fixed at 2026-07-01T00:00:00Z), so a kind's bank is byte-identical on every
host and `goldens/maintainer.<platform>.json` replays against it. Two rules
make the listing host-independent:

- `maintainer-local-times`: a listed key's `active_from` / `last_used`
  becomes `<local>` only where it is exactly CPython's `time.localtime`
  rendering of that key's seeded value on the host that ran the arm
  (recorded with the observation).
- `maintainer-list-guard` decides at comparison, on the host running the
  candidate, from the seeded times the observation carries. Once 2026 has
  passed, Windows hosts defer every listing with a time, so the Windows
  listing cases check the deferral until the seed clock is re-pinned and
  the goldens re-recorded.
