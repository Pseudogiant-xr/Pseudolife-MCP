# CLI-PAIRING contract: invite

Oracle: `pseudolife_memory/invite_cli.py`, `principal_store.py`, `principals.py`
(the v53 `principals` table) at `origin/master` 1ed871a7. Candidate:
`rust/shim/src/cli/pairing/{invite,invite_db}.rs`. Harness row: `invite`
(`rust/cli_harness/rows/invite.py`).

## Canonical shapes (implemented)

- `invite <name> [--tier T] [--board|--no-board] [--expires D] [--someone-else|--mine] [--replace] [--url U] [--port P] [--bank FP] [--json]` (v59: `--someone-else` / `--mine` set `owned`, refused before any write on a bank before v59 (`prev59` seed); a re-invite without either keeps it; on a paired row either alone changes ownership with no code; the report's `owned` member and the `for a machine that is not yours, add --someone-else (an older pseudolife-mcp refuses it: update it first)` note when neither is given, the row is the owner's and the bank has the column; one text however invite was run, since a host CLI one release behind forwards a plain invite into the daemon container exactly as a direct run looks (cases `invite-v021-forward*`: v0.21.0's argv, no `PSEUDOLIFE_DAEMON_EXEC`; `invite-forwarded`: with `PSEUDOLIFE_DAEMON_EXEC` set, as a current host CLI forwards))
- `invite --list [--bank FP] [--json]` (v59: the owner column and its two footer lines, `-` unknown, JSON `owner`; deferred where a `config.yaml` could hold `coordination.not_owner_principals`, `PSEUDOLIFE_MCP_CONFIG` included)
- `invite --revoke <name> [--yes] [--bank FP] [--json]`
- `invite --version-check` (the Docker path's image probe: `pseudolife-mcp invite 1` then `pseudolife-mcp invite 2`, line 1 first for host CLIs before v59 that read only the start; the probe takes the highest line that is exactly `pseudolife-mcp invite <N>` (universal newlines: `\r\n`, then a lone `\r`, read as `\n`, then split on `\n` only; `N` ASCII digits that fit a u64), and `--someone-else` / `--mine` need 2; a forwarded invite carries only options an image from #1154 parses; with the daemon container stopped, `--list` shows an environment row's owner as unknown, `-` and JSON `null`, since the stopped daemon's `coordination.not_owner_principals` is not this host's)

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
| List: the SQL (`principals LEFT JOIN principal_activity`, then activity names with no stored row; database clock `now`; a bank before v58 falls back to the stored rows with no times), states revoked / pending (expiry > now) / expired / paired / unpaired / environment, each environment name (`PSEUDOLIFE_MCP_TOKENS` principals, `default` when `PSEUDOLIFE_MCP_TOKEN` is set) not listed added, all sorted by code point, the column layout (`<24`, `<11`, last seen `<21` (`never` when none), `<8`, `<6`, `<21`, `<21`), `pending until <when>`, tier `or 'default'`, board `-` for an environment row, the environment note line, `no stored principals`; then a blank line and either `no refused tokens recorded` or the refused-tokens heading, `  {'prefix':<8} {'refused':>8}  {'first':<21} last` and one line per `rejected_bearers` row (newest first); JSON `principals` rows (`code_expires_at` only while a code is pending, `last_seen_at`) and `rejected` (`hash_prefix, rejections, first_seen_at, last_seen_at`) | `_list`, `_environment_names`, `list_principals` / `list_rejected` / `describe_rows` |
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

- The lite tier's embedded bank remains outside this transport slice: without a database URL or daemon container, native invite refuses instead of opening the embedded bank.
- A DSN the shared client does not admit, or a connection it cannot open or
  set up. The oracle asks `/health` first and then prints `could not connect
  to the bank's database (<Class>)`, exit 1 (or the health refusal, when
  that comes first); the candidate defers before any request (harness case
  `defer-refused-login`: no request).
- A `principals` / `meta` column of another type than the v53 DDL's, or a
  database clock outside 1970..9999 less two days (an invite only).
- Argparse's errors (help is a canonical 80-column snapshot),
  repeated options, `--opt=value`, abbreviations, values that start with `-`,
  `PSEUDOLIFE_MCP_PORT` / `--port` / `--expires` spellings other than ASCII
  digits (and the unit), a port outside 1..65535.
- Token or tier maps that would log a warning (malformed, duplicate, reserved
  principal) or hold non-ASCII text.
- A list time outside 1970-01-01..9999-12-31 (glibc prints a year before
  1000 unpadded, Windows' `gmtime` refuses negative times: the oracle's own
  answer depends on the host), or a non-finite one.

## Declared divergences

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

## Hosted CI and goldens

Both Parity lanes run the row live on the disposable PostgreSQL the lease
step provisions, and against `goldens/invite.<os>.json`. No tailscale or
docker can be reached on a runner either: `PATH` stays inside the home and
the Windows Program Files default (`ProgramW6432\Tailscale`) is moved into
it, and core's program preflight proves both before every arm. Rule
`invite-port`: the `--port` a case passes is a free port drawn when the row
loads; each arm records it, and only `port <p>`, `127.0.0.1:<p>` and
`localhost:<p>` with that exact value become `<port>` in the streams.

The 39 cases in `LIVE_ONLY` (`invite.py`) show the seeded principals in
their output or bank dump. The oracle's writers stamp those rows with the
wall clock and draw their codes and tokens at random in every harness
process, so the cases run live only (`Case.golden` False).
The goldens hold the other 86. A `/health` with more than 100 brackets
open at once is "not the daemon" in both arms (`expose_cli._json`'s bound,
2026-11-13); before it, one nested past CPython's recursion limit killed
the oracle with a traceback.

## Operator transport extension (2026-11-02 candidate)

Engine-labelled daemon reruns select `pseudolife-mcp` for `rust` and the
Python module for Python or absent labels, retaining the nesting guard.
Invite adds stopped-daemon psql list/revoke for v53 and v58; pair adds
trusted/untrusted HTTPS and terminal code input; maintainer adds help and
polling; test-login adds help while retaining its host-local login file.
Linux native builds and the live differential rows passed, including the engine-selected transport and help cases. Native Windows contract tests passed; pairing interruption and backspace also passed both oracle and native console arms.
The activated former deferral cases now compare the actual oracle outcome;
their old case IDs remain stable. Linux goldens were re-recorded from the verified oracle tree at
`7dcd738fa736c15c111fd67c5ffb83ea3f158b91` and replayed against the native candidate.
The corresponding Windows goldens also replayed successfully. Hosted stress
remains a separate qualification step. This extension makes
no claim about guided setup or lite storage.
