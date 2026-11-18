# CLI-TEST-LOGIN: `pseudolife-mcp test-login create`

The native command (`rust/shim/src/cli/test_login/`) answers every invocation
itself. No form is deferred to the Python client.

## Arguments and preflight

- Bare `test-login`, `--help` and `create --help` print usage without a home
  directory or database connection.
- `create` accepts `--rotate`, `--json`, `--role`, `--file`, `--admin-url`,
  `--container` and repeatable `--bank`. Value options accept both a separate
  argument and `--option=value`. A final `--` is accepted.
- Unknown commands, positionals, missing/empty or dash-leading values and
  repeated options are usage errors. Ambiguous abbreviations name every
  matching full option. Diagnostics omit
  argument values so an administrator URL cannot appear in them.
- Home, login-file spelling, writability and staging limits are checked
  before any database connection. Missing, non-Unicode, non-canonical or
  unprintable paths are refused with the setting and its fix.
- An unreadable or ambiguous `PSEUDOLIFE_MCP_DATABASE_URL` is refused;
  protection of the daemon user and database is never guessed.
- Unsupported `--admin-url` grammars and ambient libpq controls are refused.
  Identifier-shaped control names retain their actual spelling and case,
  including lowercase Windows names; malformed names and all values are omitted. `PGPASSWORD`
  remains supported. Ambient certificate/key/CRL files are refused separately.
- An unset/non-Unicode `PATH` is refused on the container path. Empty or
  Docker-free `PATH` retains the existing no-superuser-connection refusal.
- Runtime creation failures report the cause and retry instruction. A failed
  stdout write stops execution and exits 120 with a fixed stderr diagnostic,
  including before any persistent change.

Exit codes: 0 done, 1 database/write/runtime failure, 2 usage, 4 refused
settings, 120 stdout failure. Parsed command reports retain the existing
stdout/JSON format. Parser usage errors are always plain text on stderr,
even with `--json`; parsed preflight and run reports honour `--json`.
Native stderr pipe lines end in LF on every platform, including Windows.

`help` and `create-help` are the only harness cases without an absent
container, a scrubbed PATH or a closed administrator endpoint. They are safe
because help returns before argument parsing, home resolution or connection
selection, with no database, file or subprocess operation.

## Security and database contract

Server selection, role-name restrictions, protected databases and the order
of preflight checks remain in place. Each operation uses an explicit admin
DSN or the selected Docker container. The role cannot be the administrator,
a superuser, a bank owner or the daemon user. Existing connected-role and
daemon CONNECT checks still run before mutation. Role memberships, leftover
test databases, bank CONNECT and template1 extensions retain their behavior.

The login file is staged with exclusive creation and owner-only permissions
before the role change, removed on failure and renamed after the change.
Reapplication reuses a valid password; rotation is explicit. Targets and
ancestors retain regular-file and directory protections. Error redaction
remains in place. Harness environment values have a fixed secret-safe repr
while retaining their actual child-process values.

## Evidence surfaces

`cli_test_login_contract` exercises preflight errors and legitimate near
misses with disposable homes and a closed loopback endpoint. The harness
row uses throwaway PostgreSQL clusters and disposable containers, never an
existing bank or test login. It observes roles, memberships, database ACLs,
template1 extensions, credential authentication and owner-only permissions.
Container cases skip without a Linux Docker engine. Goldens are native
regression snapshots.

Intentionally changed cases in both platform goldens:
`admin-fresh-stdout-closed`, `admin-reapply-stdout-closed` and
`admin-fresh-json-stdout-closed`: each exits 120 with a stdout-failure stderr
diagnostic. Runtime construction failure has no deterministic test.

## Declared divergences

- Administrator-connection error detail is the native driver's text after
  the fixed `the database refused:` prefix, rather than psycopg/libpq text.
- Windows OS error messages use the system-default language through
  FormatMessage; CPython uses the user's default language. C-runtime error
  mapping retains the existing errno mapping.
- The Windows credential file DACL carries the shared native helper's
  auto-inherited bit. Its owner, protection and single OWNER RIGHTS
  full-access ACE retain the existing private-file contract.
- On Windows, the shared native PostgreSQL client's default certificate
  directory comes from `APPDATA`; libpq asks the shell for roaming AppData.
  Redirected `APPDATA` can therefore select a different default directory.
- Container output is UTF-8, as selected by this harness; a locale-codepage
  Python client may transcode Docker streams differently.
- A frozen Python runtime without psycopg aborts the administrator-URL path;
  the native command has no Python-driver dependency.
- For valid JSON server answers that are not objects, the native command
  reads missing keys as absent; the frozen oracle can raise AttributeError.
- On POSIX, a Docker process killed by a signal retains the negative signal
  return code in an otherwise empty error. This is retained behaviour and
  has no harness case; Windows has no POSIX signal exit status.
