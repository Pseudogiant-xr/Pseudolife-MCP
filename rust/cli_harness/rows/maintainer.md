# CLI-MAINTAINER contract (maintainer enrol-code / confirm / revoke / reset / list)

Oracle: `pseudolife_memory/maintainer_cli.py`, `pseudolife_memory/storage/maintainer.py`
(`MaintainerStore` host operations, schema v54 tables), `_key_change`'s audit
append in `pseudolife_memory/storage/coordination.py`, at `origin/master`
1ed871a7. Candidate: `rust/shim/src/cli/maintainer/`. Harness row:
`maintainer` (`rust/cli_harness/rows/maintainer.py`).

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
| A committed change whose report stdout refuses: exit 120 (CPython's failed shutdown flush) | `main` 162-191 |

## Free items (not contract)

- The code, its hash, the rotated secret, and every timestamp the CLI writes
  from its clock (`active_from`, `revoked_at`, a code's `expires_at`, the
  event's `created_at`) and therefore the event's hash and a later row's
  `prev_hash`: tokenized by the named rule `maintainer-host-write` after
  validating each against this arm's run window, sha256 of the printed code,
  the seed's secret, and the oracle's `audit_hash` recomputed over the row.
- CPython's ignored-exception trailer after a refused stdout (exit 120 kept):
  named substitution `maintainer-stdout-closed-trailer`.
- Statement texts (`SELECT credential_id,label` instead of `SELECT *` under
  the same `FOR UPDATE`), the prefix check made before `BEGIN` instead of just
  after it, the code drawn as random bytes masked to five bits (uniform, as
  `secrets.choice`), the order of the code's generation and the connection.

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
- Any SQL failure before the change commits (Python: `error: <class>; ...`,
  exit 2); the transaction rolls back, so nothing changed.
- `list` when stdout refuses the listing, or when any shown time is outside
  1970-9999, `TZ` is set, or (Windows) the time, or a time two days either
  side of it, is outside the current local year or has another UTC offset:
  CPython's C runtime applies the zone's current rules to every year, Chrono
  asks Windows for each year's.

## Declared divergences (after a commit, so not deferrable)

- The waiting form when the bank stops answering mid-wait prints
  `error: OperationalError; check PSEUDOLIFE_MCP_DATABASE_URL`, exit 2: the
  class psycopg raises for a lost connection, not observed in the harness.
- The waiting form when stdout refuses a line: exit 120 with nothing on
  stderr. CPython raises inside the wait, prints `error: <BrokenPipeError on
  POSIX, OSError on Windows>; check ...`, and its shutdown flush then exits
  120 with the trailer.
- The 600 s expiry of the waiting form is implemented but not run by the
  harness (it would hold each arm for ten minutes).
