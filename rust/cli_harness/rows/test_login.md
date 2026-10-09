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
| A stdout that refuses a report line (`print(..., flush=True)` raises): the run stops at that line, so nothing after it runs. Refused before the login file is staged, the native leaf defers (nothing was written; the oracle's answer is its own traceback). Refused after, it exits 120 with nothing on stderr and no later step. Cases `admin-fresh-stdout-closed`, `admin-reapply-stdout-closed`, `admin-fresh-json-stdout-closed` | 441-453, 514-523, 637-640 |
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
  client library's own message. The oracle prints psycopg's
  `<ExceptionClass>: <libpq text>`; Rust prints `DatabaseError: <the
  server's primary message>` for a server error and `OperationalError:
  <the shared client's category, e.g. PostgreSQL connection failed>`
  otherwise (the shared client deliberately carries no driver text). Exit
  code, prefix and every earlier line stay exact. Rule
  `test-login-driver-error`: replaces only that substring of the raw stdout,
  after checking the surrounding bytes (text: the message starts a line and
  stdout ends with the platform newline; JSON: stdout is exactly Python's
  `json.dumps` of the report plus the platform newline).
- A newly drawn password (random by design), masked only after validation
  against that arm's server and only when it is not the file's pre-run
  password; whether the pre-run password was kept compares exactly. Rule
  `test-login-password`: the arm itself rewrites that line to
  `<validated>` once its server has validated it, before the home is
  snapshotted, so no comparison or golden carries a drawn password. The
  re-apply fixture `GENERATED_PASSWORD` is a fixed token-shaped constant, the
  same in every harness process.
- A verifier echoed by the server's error CONTEXT when the role change fails
  (its salt and keys come from a password drawn for that run and never
  kept); the iteration count stays exact. Rule `test-login-verifier-echo`.
- The staged file's pid (never observable after a successful rename).
- The DACL's auto-inherited control bit (SDDL `AI`), which grants nothing.
- The SQL text itself (ported verbatim, compared through its effects).
- A refused stdout: the oracle's traceback. Before any change the leaf
  defers, and rule `test-login-stdout-deferral` replaces the oracle arm's
  exit and stderr with the deferral line only after validating its
  traceback (exit 120, empty stdout, a `test_login_cli.py` frame, the
  closed-stdout error and CPython's shutdown-flush trailer); files and the
  server compare as observed. After the change (`--json`), rule
  `test-login-stdout-traceback` drops that validated traceback; the leaf
  exits 120 with empty stderr, and everything the run changed compares.

## Deferred (before any connection or file change)

Any other argv (help at either level, `--opt=value`, abbreviations, repeats
other than `--bank`, values starting with `-`, empty values other than
`--role`, `--`, extra positionals); a `--file` or
`PSEUDOLIFE_TEST_PG_LOGIN_FILE` value that does not print as written
(`str(Path(p)) != p` or `..`); an undeterminable or non-canonical home; a
login file the oracle would fail to write after connecting: a target that
exists but is not a regular file (or is read-only, on Windows), an existing
ancestor that is not a directory, a nearest existing directory this process
cannot add entries to (POSIX `access(W_OK|X_OK)`; on Windows a probe file
the system deletes on close, or a probe directory removed at once), a staged
name `.<name>.<pid>.new` past 255 units, or on Windows a path past MAX_PATH
(260 for the staged file, 248 for its directory); a daemon DSN the shared
PostgreSQL client cannot read or that psycopg and the standard library would
read differently; an admin URL outside the shared client's grammar (no TCP
host, other options, fragments); libpq environment controls the shared
client refuses (`PGHOST`, `PGUSER`, `PGSSL*`, `PGPASSFILE`, `PGSERVICE`,
...); PATH unset or not UTF-8 on the container path.

## OS errors that remain possible

A failure the checks above cannot foresee (a race, a full disk) prints
`str(OSError)` as CPython 3.11 builds it: `[Errno N] <strerror>: '<file>'`
on POSIX and for `os.open` and writes on Windows (the C runtime's
`_dosmaperr` mapping and `strerror` texts), `[WinError N] <message>:
'<file>'` (trailing dots and spaces dropped) for `os.mkdir`, `os.unlink`
and `os.replace` on Windows, with ` -> '<dst>'` for a replace, file names in
`repr` form. Unit-tested against the oracle interpreter's own texts.

## Declared divergences

1. `--admin-url` failures: driver text after the fixed prefix (free, above).
2. Windows `[WinError N]` texts: Rust asks FormatMessage for the system
   default language, CPython for the user default; they differ only where an
   account's display language differs from the system's. A Win32 code
   outside the C runtime's `_dosmaperr` table maps to `EINVAL`, as the C
   runtime does.
3. Windows: the new file's DACL carries the auto-inherited bit (the crate's
   shared `make_private`); owner, protection and the single OWNER RIGHTS
   full-access ACE match.
4. Container streams are UTF-8 (Python UTF-8 mode, as the Phase 2 contract
   selects); a locale-codepage oracle would transcode docker's streams.
5. A psycopg-less shim runtime crashes on `--admin-url` (ImportError
   traceback); the native leaf answers as the full install does.
6. Unreachable: a server answer that is valid JSON but not an object makes
   the oracle raise `AttributeError`; Rust reads the missing keys as absent.
7. Shared PostgreSQL client (`pg/tls.rs`): on Windows its default root
   certificate directory comes from the `APPDATA` environment variable,
   where libpq asks the shell for the roaming AppData folder; they differ
   only when `APPDATA` is redirected. This leaf adds no TLS behaviour.
8. Docker ended by a signal: `Popen.returncode` is minus the signal, so an
   error with no output reads `exit -N`; the port reproduces that on POSIX
   (Windows has no signals). Not exercised by a case.

## Hosted CI and goldens

Both Parity lanes (`.github/workflows/rust.yml`) run the row live after the
lease step installs pg0, whose `~/.pg0/installation/<ver>/bin` supplies
`initdb` and pgvector for the throwaway clusters. The ubuntu lane first
builds `pseudolife-pg:18` from `ops/Dockerfile.pg`, so its container cases
run too.

- Container cases skip unless `docker version` reports a Linux server: a
  WSL distro without Docker integration, and hosted Windows, whose engine
  runs Windows containers only.
- `goldens/test_login.windows.json` is replayed on hosted Windows. The Linux
  golden is recorded on the hosted ubuntu lane: recording needs pg0's
  `initdb` (which needs libicu) and a Linux Docker engine, and the WSL
  distro that records the other Linux goldens has neither. While
  `goldens/test_login.linux.json` is absent, that lane leaves the row out of
  its golden pass, records it, uploads it as the `test_login-linux-golden`
  artifact and writes the executed and skipped counts to the step summary;
  once the artifact is committed, the lane replays it like every other row.
- Rule `test-login-run-tokens`: each arm records the throwaway cluster's
  port and the disposable containers' name token it ran with, and only
  those exact spellings (`127.0.0.1:<port>`, `pl-cf-w1c-testlogin-<token>-`,
  `...-absent-<token>`) are replaced in the streams, so a golden recorded by
  one harness process replays in another.
