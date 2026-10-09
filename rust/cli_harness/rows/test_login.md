# CLI-TEST-LOGIN: `pseudolife-mcp test-login create`

Oracle: `pseudolife_memory/test_login_cli.py` (line numbers below are at
c10d2ce9). Rust: `rust/shim/src/cli/test_login/`. Harness row: `test_login`.

## Canonical shapes (answered)

`test-login create` followed by any of `--rotate`, `--json` (flags, once
each), `--role R`, `--file P`, `--admin-url URL`, `--container NAME` (once
each, value as the next argument) and `--bank DB` (repeatable, argparse
`append`). Producers: the docs (`create`, `create --rotate`,
`create --admin-url postgresql://<superuser>@127.0.0.1:<port>/postgres`,
`--bank DB`), `update_cli.create_test_login` (`create --json`), the parser's
own options (`_parser`, 456-482).

## Exact contract items

| Item | Python |
|---|---|
| Exit codes 0 done, 1 database/write failed, 2 usage, 4 refused before any change | 76-79 |
| All report text on stdout, LF translated on Windows; nothing on stderr | `_Report.say` / `finish` 441-453 |
| `--json`: one `json.dumps` line, keys `exit, error, changes` then `role, file, banks, template1, others, password_reused` once known | 446-453, 697-698 |
| Every error passes `redacted` (URL userinfo, `password=`, percent-encoded token) | 149-183, 447 |
| Role name `[a-z_][a-z0-9_]{0,62}`, refusal with Python `repr`, exit 2 | 103, 492-494 |
| `--bank postgres/template0/template1` refused, exit 2, first offender named | 97, 495-497 |
| File: `--file`, else `$PSEUDOLIFE_TEST_PG_LOGIN_FILE`, else `~/.pseudolife-mcp/test-pg.env`; shown `~/…` under the home (case-insensitive on Windows), else as written | 84-85, 426-430, 498 |
| Banks: `pseudolife_memory` + `--bank` deduplicated in order; the daemon DSN's dbname (admin path), the container's `POSTGRES_DB` (container path); never the admin/template databases | 92, 499-505, 565-567 |
| Daemon DSN `user`/`dbname` (psycopg `conninfo_to_dict`, else stdlib) | 525-550 |
| Executor: `--admin-url` (psycopg), else `docker` on PATH (`shutil.which`), else refusal exit 4 naming the container | 501-511 |
| Container path: `docker exec -i NAME sh -c 'psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${1:-$POSTGRES_DB}"' sh DB`, script on stdin (text mode: CRLF on Windows), 60 s timeout, answer = last non-blank stdout line stripped, error = stderr or stdout or `exit N`, stripped; spawn failure / timeout as the exception class name | 119-146 |
| Admin path: one connection per query, `client_min_messages = warning`, autocommit statements, answer = first column of the last row-returning statement | 186-211 |
| whoami refusals: not superuser (repr of the login), role = the login | 556-564 |
| State query and the refusals from it, in order: superuser role, role owns a bank, role is the daemon's user, a connected role that keeps CONNECT only through PUBLIC, the daemon user locked out | 288-328, 569-622 |
| Daemon line: not set / names no user / not a role / keeps CONNECT on … | 598-622 |
| Existing role without a reusable file refused unless `--rotate` | 623-631 |
| Leftover run databases (`LEFTOVER_DATABASE`) handed to the role | 101, 633-635, 361-366 |
| Password: re-applied from the file, else `secrets.token_urlsafe(32)`; the server only gets the SCRAM-SHA-256 verifier (4096 iterations, 16-byte salt) | 216-229, 636 |
| File written owner-only before the role change (staged `.<name>.<pid>.new`, `O_EXCL`, 0600 or the owner-only ACL), unlinked if the change fails, renamed over the target after it | 413-423, 639-653; `private_state.open_private`; `credentials._secure_windows_file` |
| Role statements, one transaction: create/alter with fixed attributes, revoke every membership, hand over leftovers, revoke CONNECT on each present bank from PUBLIC and ALL from the role, revoke CONNECT on template1 from PUBLIC | 331-375 |
| Report lines and their order (role, memberships, leftovers, banks, missing banks, daemon line, connected roles, template1, extension, wrote, others, done) | 654-705 |
| `vector` created/updated in template1, `before|after` versions reported | 378-393, 681-688 |
| Verification after the change and its problems line, exit 1 | 690-700, 708-730 |
| Server state afterwards (roles, memberships, database owners/ACLs, template1 extensions), the file's password authenticating, its verifier verifying it, the role refused by the bank | harness `_observe` |

## Free items

- The text after `the database refused: ` on the `--admin-url` path: the
  client library's own message (psycopg/libpq for the oracle; the crate's
  client, which deliberately prints no driver text, for Rust). Exit code,
  prefix and every earlier line stay exact. Rule `test-login-driver-error`.
- A newly drawn password (random by design), compared only after validation
  against that arm's server. Rule `test-login-password`.
- The staged file's pid (never observable after a successful rename).
- The DACL's auto-inherited control bit (SDDL `AI`), which grants nothing.
- The SQL text itself (ported verbatim, compared through its effects).

## Deferred (before any connection or file change)

Any other argv (help at either level, `--opt=value`, abbreviations, repeats
other than `--bank`, values starting with `-`, empty values other than
`--role`, `--`, extra positionals); a `--file` or
`PSEUDOLIFE_TEST_PG_LOGIN_FILE` value that does not print as written
(`str(Path(p)) != p` or `..`); an undeterminable or non-canonical home; a
login-file target that exists but is not a regular file, or an existing
ancestor that is not a directory; a daemon DSN the shared PostgreSQL client
cannot read or that psycopg and the standard library would read differently;
an admin URL outside the shared client's grammar (no TCP host, other
options, fragments); libpq environment controls the shared client refuses
(`PGHOST`, `PGUSER`, `PGSSL*`, `PGPASSFILE`, `PGSERVICE`, ...); PATH unset or
not UTF-8 on the container path.

## Declared divergences

1. `--admin-url` failures: driver text after the fixed prefix (free, above).
2. A failed final rename (after the role change) and OS-level write failures
   print Rust's `io::Error` text inside the oracle's sentence; the
   `PrivateStateError` sentences are reproduced. Shapes that predictably fail
   defer instead.
3. Windows: the new file's DACL carries the auto-inherited bit (the crate's
   shared `make_private`); owner, protection and the single OWNER RIGHTS
   full-access ACE match.
4. Container streams are UTF-8 (Python UTF-8 mode, as the Phase 2 contract
   selects); a locale-codepage oracle would transcode docker's streams.
5. A psycopg-less shim runtime crashes on `--admin-url` (ImportError
   traceback); the native leaf answers as the full install does.
6. Unreachable: a server answer that is valid JSON but not an object makes
   the oracle raise `AttributeError`; Rust reads the missing keys as absent.
