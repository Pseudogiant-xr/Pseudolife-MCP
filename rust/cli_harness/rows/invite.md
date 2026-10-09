# CLI-PAIRING contract: invite

Oracle: `pseudolife_memory/invite_cli.py`, `principal_store.py`, `principals.py`
(the v53 `principals` table) at `origin/master` 1ed871a7. Candidate:
`rust/shim/src/cli/pairing/{invite,invite_db}.rs`. Harness row: `invite`
(`rust/cli_harness/rows/invite.py`).

## Canonical shapes (implemented)

- `invite <name> [--tier T] [--board|--no-board] [--expires D] [--replace] [--url U] [--port P] [--bank FP] [--json]`
- `invite --list [--bank FP] [--json]`
- `invite --revoke <name> [--yes] [--bank FP] [--json]`
- `invite --version-check` (the Docker path's image probe)

Producers: `docs/guide/remote-bank.md` "Adding a machine", `docs/guide/agent-isolation.md`
step 4, the installers' help text. Each option at most once, its value the
next argument; the name may sit anywhere among the options (argparse's single
optional positional). The bank is reached only through a non-empty
`PSEUDOLIFE_MCP_DATABASE_URL` the shared native client admits.

## Exact contract items

| Item | Python source |
|---|---|
| Exit codes 0 done, 1 database failed, 2 usage / not confirmed, 4 refused before any change | `invite_cli.py:42-43`, 67-70 |
| `--expires`: `<n>[smhd]` (bare = minutes), 1 min..24 h; `--port` 1..65535; `PSEUDOLIFE_MCP_PORT` default read while the parser is built | `_expires` 190-197, `_parser` 480-508, `expose_cli._port` 552-559 |
| Exactly one of name / `--list` / `--revoke` (Python truthiness: an empty value is absent), else `give one of: ...` exit 2; then `target = args.name or args.revoke`, so an empty `--revoke` beside `--list` is validated (and refused) as a name | `main` 520-528 |
| Name grammar `[a-z0-9][a-z0-9._-]{0,63}` and its message with `repr(name)`, exit 2; reserved `default`/`daemon`/`maintainer` (`'name' is reserved...`), names in `PSEUDOLIFE_MCP_TOKENS` (principal lowercased, `rpartition(":")`) or `PSEUDOLIFE_MCP_TIER_MAP`, exit 4 | `main` 523-532, `_name_refusal` 208-218, `principals.py:58,72,103-147`, `toolset_tiers.parse_tier_map` |
| `--revoke` without `--yes` on a non-terminal stdin: `not interactive: re-run with --yes to revoke`, exit 2 | `main` 533-535 |
| Report: text lines on stdout, failures `invite: <line>` on stderr; `--json` one `json.dumps(data, indent=2)` object, members in insertion order `error, notes, exit, [daemon], ...` | `_Report` 162-185 |
| Daemon check (invite only): `/health` on `127.0.0.1:<port>` (no proxy, a redirect is no answer), `status == "ok"`, the `daemon` member `{auth, schema, bank}` (verbatim JSON values), `auth is True`, `schema` an int >= 53 (bool counts as int), `bank` truthy; each refusal's text, exit 4 | `_daemon_check` 221-242, `expose_cli.fetch_health` 116-140 |
| `/health` read over `json.loads`'s whole domain: a lone surrogate kept (printed `\udXXX` on stderr by `backslashreplace`, `\udXXX` in JSON, escaped in `repr`), an integer past 4300 digits a `ValueError` (no healthy daemon), nesting up to the measured recursion limit (985 containers, CPython 3.11.9 through this CLI) | `fetch_health` 129-140, `str.__repr__`, stderr `errors="backslashreplace"` |
| Floats in reports (`expires_at`, list times, daemon values) spelled as CPython's `repr`, ties included (David Gay mode 0: shortest, nearest, half to even) | `json.dumps`, `float.__repr__` |
| Session: `SET search_path TO public`, `SET statement_timeout = '10s'` | `_local` 304-305 |
| Bank confirmation (invite, or any mode with `--bank`): `meta.coordination_bank_id` (a JSON string) -> first 16 hex of its SHA-256; `--bank` checked first, then the daemon's bank; `this database holds bank X, but WHAT is bank Y: it is another database. Nothing was changed`, exit 4 | `_local` 307-313, `_bank_of` 264-268, `_bank_refusal` 255-259, `principal_store.bank_fingerprint` 76-81 |
| `UndefinedTable` (no `meta` / `principals`): `this bank has no principals table yet (schema v53)...`, exit 4; another psycopg error `the database refused the change (<Class>)`, exit 1, the class from psycopg 3.3.4's own SQLSTATE table and SQLSTATE-class fallback (`sqlstate.rs`, generated from the oracle's psycopg); a lost connection `OperationalError` | `_local` 319-325, `psycopg.errors.lookup` / `get_base_exception` |
| create_invite: up to 5 draws of a 12-char Crockford code (`secrets.choice`), only its SHA-256 stored; `SELECT ... FOR UPDATE`, then INSERT (new: tier given or NULL, board given or TRUE, `code_expires_at = now + ttl`, `created_at = now`, database clock) or UPDATE (code, expiry, `paired_code_hash = NULL`, tier/board only when given, a revoked row's token/paired/revoked cleared); a paired row without `--replace` refused (`... is already paired; pass --replace ...`, exit 4, rolled back); a unique violation draws again | `principal_store.py:468-528` |
| Invite report: state words (`invited` / `re-invited (...)` / `re-invited after its revocation` / `given a replacement code (...)`), `tier X or the daemon default`, board on/off, the code as `XXXX-XXXX-XXXX`, `expires in round(ttl/60) min` (half to even), expiry `%Y-%m-%d %H:%M UTC` of `gmtime` (floored), the pair command, the installer line, the placeholder note when the URL is `<daemon-url>`, the closing note; JSON adds `principal, state, code, expires_at, tier, board, url, pair_command` | `_invite` 328-356, `_when` 284-285 |
| `--url ""` is no URL (`args.url or ...`) | `_invite` 335 |
| List: the SQL (`ORDER BY principal`, database clock `now`), states revoked / pending (expiry > now) / expired / paired / unpaired, the column layout (`<24`, `<9`, `<8`, `<6`, `<21`, `<21`), `pending until <when>`, tier `or 'default'`, `no stored principals`; JSON `principals` rows (`code_expires_at` only while a code is pending) | `_list` 367-378, `list_principals` / `describe_rows` 531-562 |
| Revoke: one transaction, `revoked_at = COALESCE(revoked_at, now)`, pending code cleared; none: `no stored principal is named X`, exit 4; done: `X: revoked; the daemon stops accepting its token within 10 seconds` | `_revoke` 359-364, `revoke` 565-577 |

## Free items

- The code's draw order and the random source (both draw uniformly from the
  32-character alphabet through the OS CSPRNG).
- The connect timeout (psycopg 5 s, this client 10 s) and the session's
  connection settings other than `search_path` and `statement_timeout`.
- The HTTP request headers other than the target (User-Agent, Accept,
  Connection): the daemon reads none of them on `/health`.

## Deferred before any effect

An invite makes one request (`GET /health`). Everything that can still
defer runs before it: the session is opened and set up, and `prepare` reads
the `principals` / `meta` column types and the database clock, so nothing
after the request can meet a value this client cannot decode or a date it
cannot print. List and revoke make no request.

- No `PSEUDOLIFE_MCP_DATABASE_URL` (empty counts as none): the lite tier's
  embedded bank, the Docker re-exec (`docker exec ... invite`), and the
  psql-in-container fallback (`_through_postgres`). Proposed row
  CLI-PAIRING-CONTAINER.
- A DSN the shared client does not admit, or a connection it cannot open or
  set up. The oracle asks `/health` first and then prints `could not connect
  to the bank's database (<Class>)`, exit 1 (or the health refusal, when
  that comes first); the candidate defers before any request (harness case
  `defer-refused-login`: no request).
- A `principals` / `meta` column of another type than the v53 DDL's, or a
  database clock outside 1970..9999 less two days (an invite only).
- Argparse's errors and `--help` (help and usage text depend on `COLUMNS`),
  repeated options, `--opt=value`, abbreviations, values that start with `-`,
  `PSEUDOLIFE_MCP_PORT` / `--port` / `--expires` spellings other than ASCII
  digits (and the unit), a port outside 1..65535.
- `--revoke` without `--yes` on a terminal (the question).
- Token or tier maps that would log a warning (malformed, duplicate, reserved
  principal) or hold non-ASCII text.
- An invite where `_listed_note` could read a `config.yaml` (the data dir's,
  or with no `PSEUDOLIFE_MCP_DATA_DIR` either the lite default's or
  `./data`'s): its reading is the whole AppConfig loader, any failure silent.
- An invite without `--url` when any `tailscale` CLI candidate exists (on
  `PATH`, Windows' cwd, `%ProgramW6432%`/`%ProgramFiles%\Tailscale`, the macOS
  app): `expose status` would run it. With none, the URL is `<daemon-url>`,
  as the oracle answers.
- A list time outside 1970-01-01..9999-12-31 (glibc prints a year before
  1000 unpadded, Windows' `gmtime` refuses negative times: the oracle's own
  answer depends on the host), or a non-finite one.

## Declared divergences

- A `/health` nested past CPython's recursion limit (more than 985
  containers): the oracle dies with an uncaught `RecursionError` traceback
  (exit 1, naming its own source files); the candidate prints its deferral
  line, exit 1, after the same request. Rule `invite-recursion-traceback`
  maps only that traceback with exit 1. The limit is measured on CPython
  3.11.9; another oracle interpreter can move it.
- The native session runs `RESET lock_timeout` after the shared client's
  5 s setting, so a lock wait ends at the 10 s statement timeout as in Python
  (`QueryCanceled`, exit 1).
- A failure of the session settings (`SET`) themselves: Python raises it
  uncaught; the candidate defers (it happens before any request).

## Harness instruments

- Clock witness (`pl_cf_clock`): in each arm's write bank a BEFORE trigger
  notes the database clock as a write reaches a row; `invite-written`
  accepts a written time only within 50 ms before that reading
  (`code_expires_at - ttl` likewise), and the printed expiry only spelled
  exactly as the oracle spells the stored value. Mutant
  `invite-ttl-shortened` (0.6 s short) is caught by it.
