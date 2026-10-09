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

## Free items (not contract)

- Wall-clock `created_at` of the redact event and therefore its hash, `redact_hash`
  and `expect_head`: tokenized by the named rule `audit-redact-clock` after
  validating the window and recomputing `audit_hash` with the oracle.
- Snapshot mechanics (one transaction instead of commit-then-reopen; a buffered
  export instead of a server-side cursor); `extra_float_digits` (binary float8).

## Deferred (the leaf's own deferral line, exit 1, before any effect)

`stats` (all shapes); help; the lite tier's embedded bank and the daemon-container
transport (no or empty `PSEUDOLIFE_MCP_DATABASE_URL`); DSNs or ambient PG controls
the shared client refuses; any connection or SQL failure on a read; ISO times
without an explicit offset (local time, including the documented
`--since 2026-09-23`); float spellings other than digits[.digits]; `-`-prefixed
values, `--opt=value`, abbreviations, repeated options; paths `pathlib` would
reprint differently; archives that are not UTF-8 or whose lines Python's `json`
reads differently (NaN/Infinity, lone surrogates, >128 nesting, >4300-digit
integers, invalid JSON); seqs beyond i64; `--out` that cannot be created;
redaction reasons holding a non-Cc category-C character (Cf vs Co/Cn needs a
table the crate does not pin); any redaction SQL error other than lock timeout
(rolled back).

## Declared divergences

- Every deferral above (Python answers; the candidate defers).
- A redaction commit the server does not confirm prints the oracle's generic
  database-failure line (exit 2), not psycopg's exception path.
