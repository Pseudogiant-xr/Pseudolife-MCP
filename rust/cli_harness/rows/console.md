# CLI-CONSOLE-PRINT contract (`console`, the shapes that open no browser)

Oracle: `pseudolife_memory/console_cli.py` (with `console_sessions.normalize_origin`
/ `valid_route_id`, `credentials.CredentialProvider` and
`doctor_cli.registration_credentials`) at this branch's base, CPython 3.11.9.
Candidate: `rust/shim/src/cli/console/` (argparse subset in `args.rs`, local
time in `stamp.rs`), reusing `cli/pairing/url.rs` (`urlsplit`),
`cli/doctor` (the registration search), `crate::credentials`, `cli/pyjson`
and `cli/hook_http.rs` (`call_json`). Harness row: `console`
(`rust/cli_harness/rows/console.py`). Oracle tests: `tests/test_console_cli.py`.

## Scope

`console` signs a browser in to the Console with a one-time link, lists the
signed-in browsers and signs one out. This row ports every shape that opens
no browser. The sign-in that opens one (`open_browser`, `webbrowser.get()`)
spawns a process and needs its own Windows work, so it defers by name, as
does `--unless-signed-in` (what install runs: a list read, then that same
sign-in).

## What runs, in the oracle's order (checked)

1. `cli.main` dispatches `console` with `sys.argv[2:]`.
2. `_parser().parse_args`: help exits there; a missing value or an explicit
   argument to a flag errors there; words collect as extras and error once
   every option is consumed (`unrecognized arguments: a b`).
3. `main`: `--list` with a non-empty `--revoke` (exit 2); a `--to` that is
   not a route id (`[a-z][a-z0-9-]{0,31}`, its `repr` in the message, exit 2).
4. `credentials()`: the registration search runs first (`doctor`'s, with the
   environment's token keys winning); the daemon URL (`or` the default) must
   normalize to an origin; then the token file or token. Each failure is
   the `no_credentials` answer (exit 3).
5. The branch: `--list` (GET `/api/console/sessions`), a non-empty
   `--revoke` (POST `/api/console/sessions/revoke`), else the sign-in (POST
   `/api/console/link` with `origin` and a non-empty `to`).
6. `request()`: no proxy, no redirect followed, 10 s per socket operation,
   `Authorization: Bearer`, `Content-Type: application/json` on GET too (the
   headers dict sets it), `read(262144)` for a 2xx and `exc.read(262144)`
   (a failure reads as no body) for an error status; the body as a JSON
   object or nothing. No HTTP answer: `no_daemon` with `str(exc.reason)`
   (exit 3).
7. Status other than 200, or no object: `_refusal` (exit 4): the named
   codes' messages, a hint or the code for the four hint-only codes, the
   old-daemon text for 404/405, else the hint, else `the daemon answered
   HTTP N (code)`. `--json` keeps the hint's JSON type in `detail`.
8. `_list`, the revoke answer, or `_opened` with `--no-open` (`state:
   printed`; text with `max(1, int(expires_in or 300) // 60)` minutes).

## Exact contract items

| Item | Python source |
|---|---|
| Help at `COLUMNS=80`, stdout, exit 0; usage + `pseudolife-mcp console: error: ...` on stderr, exit 2 | `_parser`, argparse 3.11 |
| `--list and --revoke are separate commands; give one`; `--to takes a Console page id such as board, not 'X'`, stderr, exit 2 | `main` |
| `no_credentials` text and JSON for: a URL that is no origin, a missing token file, an empty token-file path, an invalid token, no token | `credentials` |
| A token from `~/.claude.json`'s `pseudolife-memory` env block when the environment has none | `doctor_cli.registration_credentials` |
| Connection refused: `[WinError 10061] ...` (Windows, trailing full stop dropped) / `[Errno 111] Connection refused` (Linux) | `request`, CPython `OSError.__str__` |
| https against a CA trusted through `SSL_CERT_FILE` (system trust, certificate and hostname verification) | `request`, CPython's default context |
| Request method, target, every header field's value, and the body bytes (`json.dumps`, ASCII) | `request`, urllib |
| Each refusal shape, text and JSON | `_refusal`, `_emit` |
| List: columns padded by code point, `str()` of non-string cells (`None`, floats, lists by `repr`), `-` for a missing or non-numeric stamp, a falsy label as `-`, non-object rows skipped, the paired-token note on `operator: false`; stamps as local `%Y-%m-%d %H:%M` (pinned `TZ=XYZ-10`) | `_list`, `_stamp` |
| Revoke: `Signed out ID.` (stdout, 0) / `No signed-in browser ID ...` (stderr, 4), JSON `revoked` / `not_found` | `main` |
| Link: `link_is_safe` (a str code, a URL at the answer's normalized origin with no control or space character), the printed lines and the JSON object | `_opened`, `link_is_safe` |
| Streams: Python text streams, CRLF on Windows; stderr writes a lone surrogate as `\udXXX` | `print` |

## Deferred before any request (the dispatcher's `mode 'console' is deferred` line, exit 1)

- The sign-in without `--no-open`, `--unless-signed-in` on the sign-in path,
  and an empty `--revoke` there (`elif args.revoke:` is false for `""`, so the
  oracle makes a sign-in link: see the report's open question).
- argparse corners: abbreviations, `--`, a value starting with `-`, any
  other token starting with `-`, `--help=x`; help or usage at a width other
  than `COLUMNS=80` or with `FORCE_COLOR` / `PYTHON_COLORS` / `NO_COLOR` set
  or a terminal; non-UTF-8 argv (POSIX locale decoding as `move` reads it).
- Credentials: any `CredentialError` other than the three compared texts; a
  non-UTF-8 environment value; a registration doctor's port does not read;
  a non-ASCII daemon URL (urlsplit's NFKC checks); a token outside printable
  ASCII (urllib would send Latin-1 or raise).
- Connection: a name that does not resolve or resolves to nothing; any
  connect failure but a refused one (a timeout, an unreachable network:
  their text is not compared); a TLS handshake failure (case
  `defer-https-untrusted`).
- `PYTHONINTMAXSTRDIGITS` set: `json.loads`' integer digit limit moves
  (case `defer-int-digits`).

## Deferred after the request

Each is a failure where the oracle prints a traceback (exit 1) or something
this port does not reproduce; the native candidate prints its deferral line
(exit 1) after sending the same request.

- Declared, with a case each (rules `console-*-traceback`): an `error` list
  or dict (`code in _REFUSALS` is unhashable); an `expires_in` that `int()`
  refuses (text that is no integer, a list, NaN); a NaN stamp, a
  stamp past `time_t`, a negative stamp on Windows (the CRT's `EINVAL`); a
  lone surrogate on stdout (strict UTF-8). The `--list` crashes come after
  the header line, which the rule accepts as the oracle's only stdout.
- Not cased: a reset, timeout or malformed reply once connected (Python's
  `RemoteDisconnected`, `timed out`, `BadStatusLine` traceback); a 2xx body
  that fails to read; nesting past 500 (the pairing read paths' measured
  limits are 985 and 987; console's is unmeasured); a link answer whose
  `origin` or `url` holds a non-ASCII character or a lone surrogate; an
  `expires_in` float at or past 1e30 or an integer past 128 bits; a write to
  stdout or stderr that fails before its first byte; a
  stamp past 2999, or before 1019 on Linux, where the oracle may still print
  one; an `expires_in` string other than plain ASCII digits with an optional
  sign (`int()` also takes spaces, underscores and other digits).

## Declared divergences

- The deferrals after a request leave the request made: a revoke or a
  sign-in code the daemon already acted on. The candidate is not installed
  as `pseudolife-mcp`, so this is the candidate's answer only.
- Windows' error text comes from `FormatMessageW` in the system language, as
  CPython's does; the row compares the host's own text.

## Hosted CI and goldens

The row runs live on both OSes in the `w1c-connection` shard. Every case is
built from fixed bytes (the fixture echoes the request's origin, which the
harness tokenizes per arm), so all are golden cases. Both goldens bind the
oracle tree of `ca803afd` (no `pseudolife_memory/` file differs on this
branch): Windows from this checkout (CPython 3.11.9), Linux from an export
of the branch under WSL Ubuntu (CPython 3.11.15, `declared`).

Local evidence (2026-11-12): Windows workstation, 105/105 live and golden
and all 8 mutants caught; WSL Ubuntu, 105/105 live and golden and all 7 of
its mutants caught (`console-winerror-full-stop` is Windows-only). Before
the port, the row's first 101 cases against the deferring binary: 89
differed, and the 12 that matched were the declared deferrals. Hosted runs
are CI's.
