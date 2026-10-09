# CLI-AUDIT contract (board-audit verify / export / redact)

Oracle: `pseudolife_memory/board_audit_cli.py`, `pseudolife_memory/storage/coordination.py`
at `rust-cf/w1c-base` (c10d2ce9). Candidate: `rust/shim/src/cli/board_audit/`.
Harness row: `audit` (`rust/cli_harness/rows/audit.py`).

## Canonical shapes (implemented)

- `board-audit verify --input PATH [--expect-head SEQ:HASH]` (either option order)
- `board-audit verify [--expect-head SEQ:HASH]` against the bank
- `board-audit export [--project P] [--task T] [--agent A] [--since S] [--until U] [--out PATH]`
  (any order)
- `board-audit redact --message-id ID --reason TEXT` (either order)

The bank is reached only through a non-empty `PSEUDOLIFE_MCP_DATABASE_URL`
that the shared native client's DSN grammar admits (`rust/shim/src/pg/`).

## Exact contract items

| Item | Python source |
|---|---|
| Exit 0 ok, 1 broken chain / unconfirmed head / refused redaction, 2 could not check | `board_audit_cli.py:23-27`, `main` 555-626 |
| `--expect-head` is `SEQ:HASH`, seq >= 1, 64 lowercase hex | `_head` 92-97 |
| `--since`/`--until`: epoch seconds or ISO 8601 (only digit/decimal epochs and ISO with explicit `+HH:MM`/`-HH:MM` offsets are admitted) | `_time` 78-89 |
| Bank: DSN, read-only repeatable-read snapshot, `to_regclass('public.coordination_events')` check and its "no audit log yet" text (exit 2); redact also needs the v46 `body` column (its text, exit 2) | `_bank` 105-146 |
| Export: an existing `--out` refused before any bank (`{path} exists; export never replaces a file`, exit 2); file created before `--agent` resolves, so a refused agent leaves it empty; JSON lines `json.dumps(row, ensure_ascii=True, separators=(",", ":"))`, payload `json.loads`ed, columns in `AUDIT_COLUMNS` order then `body`, `body_salt`; stdout lines CRLF on Windows, `--out` LF; closed stdout prints `the output was closed before the export finished`, exit 2 | `_export` 173-217, `main` 612-620 |
| `--agent`: full id or unique 8-31 hex prefix among agents, log actors and recipients (`ORDER BY agent_id LIMIT 9`), refusals `no agent id starts with A` / `--agent A matches several ids: P1, P2; give a longer prefix` | `_export` 184-192, `resolve_agent_id` coordination.py:721, `distinguishing_prefixes` 667 |
| Filters: `project=`, `task=`, `(agent_id= OR recipient_agent_id=)`, `created_at>=since`, `created_at<until`, `ORDER BY seq` | `audit_events` coordination.py:1164-1199 |
| Archive reader: universal newlines, blank lines (`str.strip`) skipped, duplicate decoded keys (`line N has a duplicate key...`), shape check (`line N is not an exported audit event`), `cannot read PATH`, every diagnostic naming `str(Path(PATH))` | `_read_export` 233-270 |
| Row hash: sha256(prev_hash ‖ compact `json.dumps([format, seq, event, actor, principal, agent_id, recipient, project, task, message_id, float(created_at), hlc, payload_text], ensure_ascii=False)`), payload re-canonicalized (`sort_keys`) when parsed | `audit_hash` coordination.py:957-971 |
| Chain walk: `broken_link` (genesis / prev hash), `sequence_gap`, `hash_mismatch`, `body_mismatch` (salt without body, body not opening `text_commitment`, body still beside its operator redact), `body_missing` / `body_not_exported` for the lowest unexplained v46 send, `unanchored_start` (cut record: daemon actor, window >= 1 day, `audit_cutoff`, first row not older), `head_pruned` (with `start_cut`), `head_missing`, `head_mismatch`; report key order and `print(json.dumps(report))` spelling | `verify_audit_chain` 1035-1150, `_cut` 988-1010, `audit_cutoff` 978-985, `body_commitment` 516-525, `_body_matches` 1024-1032 |
| Redact: id shape (`invalid_message_id`), reason (`invalid_reason`: blank, > 240 chars, Cc/Cf/Cs/Zl/Zp), `secret_like_body`, prefix resolution (`message_not_found` / `ambiguous_message_id`), one transaction: live row `FOR UPDATE`, chain lock, send event, `gone`/`kept`/`removed` paths, `already_redacted`, `body_in_hashed_payload`, live copy blanked with `expires_at=LEAST(expires_at, now)`, chained operator `redact` event, burst siblings; refusal JSON echoes the id as given plus the `_REDACT_REFUSALS` line (`: detail` when present); busy (`LockNotAvailable`, 5 s) exit 2 | `CoordinationStore.redact` 3987-4112, `_burst_siblings` 4114, `_redact` 318-365, `_REDACT_REFUSALS` 58-71 |
| Post-commit `_vacuum` (`SET client_min_messages TO warning`, both VACUUMs, setting restored; WARNING notices are skips), `vacuumed` and the four stderr notes, the `--expect-head` record line | `_vacuum` 283-315, `_redact` 331-365 |

| `--out` write failure: the created file is removed, then `cannot write PATH: <strerror>`, exit 2 (POSIX: the C library text std prints; Windows: only `ERROR_DISK_FULL` -> `No space left on device` is pinned) | `_export` 196-216, `_write_error` 165-170 |
| Archive rows: a repeated key is reported when its object closes (`object_pairs_hook`), so a scan error or an over-long integer earlier in that object wins | `_read_export` 250-257, `_unique_keys` 224-230 |
| A redaction whose result line stdout refuses: exit 120 after every stderr note (CPython's failed shutdown flush) | `_redact` 337-365 |

## Free items (not contract)

- Wall-clock `created_at` of the redact event and therefore its hash, `redact_hash`
  and `expect_head`, and a live row's `expires_at` that `LEAST(expires_at, now)`
  moved to that clock: tokenized by the named rule `audit-redact-clock` after
  validating the window, recomputing `audit_hash` with the oracle, and checking
  that every printed hash reference is that arm's own validated hash.
- CPython's ignored-exception trailer after a refused stdout (exit 120 kept):
  named substitution `audit-stdout-closed-trailer`.
- Snapshot mechanics (one transaction instead of commit-then-reopen; a buffered
  export instead of a server-side cursor); `extra_float_digits` (binary float8).

## Deferred (the leaf's own deferral line, exit 1, before any effect)

- `stats` (all shapes) and help.
- The lite tier's embedded bank and the daemon-container transport (no or empty
  `PSEUDOLIFE_MCP_DATABASE_URL`); DSNs or ambient PG controls the shared client
  refuses; any connection or SQL failure on a read.
- ISO times without an explicit offset (local time, including the documented
  `--since 2026-09-23`); float spellings other than digits[.digits].
- `-`-prefixed values, `--opt=value`, abbreviations, repeated options; paths
  `pathlib` would reprint differently (`.` components, repeated or trailing
  separators, `/` on Windows, any Windows colon but an `X:\` drive).
- `--input` read errors other than not-found; archives that are not UTF-8 or
  whose lines Python's `json` reads differently (NaN/Infinity, lone
  surrogates, >128 nesting, >4300-digit integers, invalid JSON); seqs beyond i64.
- Stored payloads Python's `json` reads differently (same list) on export,
  bank verify and redact.
- `--out` that cannot be created; on Windows, a `--out` write failure with a
  Win32 error other than `ERROR_DISK_FULL` (the file is removed first).
- `export` to stdout when descriptor 1 is closed (POSIX; CPython has no
  `sys.stdout`); a verify report or a redaction refusal stdout refuses.
- Redaction reasons holding a non-Cc category-C character (Cf vs Co/Cn needs
  a table the crate does not pin); any redaction SQL error other than lock
  timeout (rolled back).

## Declared divergences

- Every deferral above (Python answers; the candidate defers).
- More than `pg::NOTICE_LIMIT` (4096) warnings in one vacuum keep only the
  newest 4096 in the clean-up note.
- A process limit that raises SIGXFSZ during a `--out` write: CPython ignores
  the signal and reports `cannot write`; the native process is not shielded.

## Coverage notes

- Vacuum skips: the test login owns every table, so Postgres never skips for
  it; the harness drives the same path with an expression index whose
  ANALYZE-time `RAISE WARNING` arrives exactly like a skip, with the database
  hiding warnings by default (`redact-vacuum-warning`). The restored
  `client_min_messages` itself is session state and not observable.
- `--out` write failures and closed descriptor 1 are not reproducible in the
  harness; unit tests pin the strerror mapping, and the POSIX check needs a
  Linux run.

## Proposed PARITY split

- **CLI-AUDIT-VERIFY-EXPORT-REDACT: ported** for the canonical shapes above over
  an explicit DSN (harness row `audit`).
- **CLI-AUDIT-STATS: deferred.** Blockers: the report's `version` is the
  installed Python distribution's version; `generated_at` and the default
  window come from the wall clock; the suite durations file carries local-time
  ISO stamps; Python `round` and nearest-rank percentiles need a qualified match.
- **CLI-AUDIT-TRANSPORT: deferred.** The embedded and container transports and
  local-time ISO arguments (including the documented date-only `--since`).
