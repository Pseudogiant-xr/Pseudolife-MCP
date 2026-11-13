# CLI-CONSOLE-PRINT contract (`console`)

Oracle: `pseudolife_memory/console_cli.py` (with `console_sessions.normalize_origin`
/ `valid_route_id`, `credentials.CredentialProvider`,
`doctor_cli.registration_credentials` and CPython 3.11's `webbrowser`,
`shlex` and `subprocess`) at this branch, CPython 3.11.9.
Candidate: `rust/shim/src/cli/console/` (argparse subset in `args.rs`, local
time in `stamp.rs`, the browser choice in `browser.rs`, the launch in
`launch.rs`, `int()` in `pyint.rs`), reusing `cli/pairing/url.rs`
(`urlsplit`), `cli/doctor` (the registration search), `crate::credentials`,
`cli/pyjson` and `cli/hook_http.rs` (`call_json`). Harness row: `console`
(`rust/cli_harness/rows/console.py`). Oracle tests: `tests/test_console_cli.py`.

## Scope

`console` signs a browser in to the Console with a one-time link, lists the
signed-in browsers and signs one out. The first slice (2026-11-12) ported
every shape that opens no browser; this one ports the sign-in that opens a
browser (with or without `--to` and `--json`) and `--unless-signed-in` (the
session-list read, then that sign-in). Which browser `open_browser` would
start is decided before the code is minted, so a launcher this port does not
reproduce defers before any effect but `--unless-signed-in`'s read; once the
daemon answers the sign-in with a 200 object the candidate answers every
branch itself. Since issue #1497's fixes the oracle refuses each malformed
answer it used to die on (a traceback, exit 1), so no case is a declared
crash any more.

## What runs, in the oracle's order (checked)

1. `cli.main` dispatches `console` with `sys.argv[2:]`.
2. `_parser().parse_args`: help exits there; a missing value or an explicit
   argument to a flag errors there; words collect as extras and error once
   every option is consumed (`unrecognized arguments: a b`).
3. `main`: an empty `--revoke` (`--revoke takes a session id from --list,
   not an empty one`, exit 2, ahead of the next two); `--list` with
   `--revoke` (exit 2); a `--to` that is not a route id
   (`[a-z][a-z0-9-]{0,31}`, its `repr` in the message, exit 2).
4. `credentials()`: the registration search runs first (`doctor`'s, with the
   environment's token keys winning); the daemon URL (`or` the default) must
   normalize to an origin; then the token file or token. Each failure is
   the `no_credentials` answer (exit 3).
5. The branch: `--list` (GET `/api/console/sessions`), `--revoke` (POST
   `/api/console/sessions/revoke`), else the sign-in.
6. The sign-in: with `--unless-signed-in`, GET `/api/console/sessions` first;
   a 200 object whose `sessions` is a non-empty list answers
   `already_signed_in` (exit 0), anything else (a refusal, no object, an
   empty or non-list `sessions`) goes on; no HTTP answer is `no_daemon`. Then
   POST `/api/console/link` with `origin` and a non-empty `to`.
7. `request()`: no proxy, no redirect followed, 10 s per socket operation,
   `Authorization: Bearer`, `Content-Type: application/json` on GET too (the
   headers dict sets it), `read(262144)` for a 2xx and `exc.read(262144)`
   (a failure reads as no body) for an error status; the body as a JSON
   object or nothing (nesting past the parser's recursion limit included).
   No HTTP answer: `no_daemon` with `str(exc.reason)` (exit 3); a reply
   that is not readable HTTP (`http.client.HTTPException` that is not an
   `OSError`): `bad_reply` (exit 4).
8. Status other than 200, or no object: `_refusal` (exit 4): the named
   codes' messages (only a str `error` is looked up), a hint or the code for
   the four hint-only codes, the old-daemon text for 404/405, else the hint,
   else `the daemon answered HTTP N (code)`, a list or object code by its
   `str()`. `--json` keeps the hint's JSON type in `detail`.
9. `_list`: every row built before anything prints; a time `localtime`
   refuses (`ValueError`, `OverflowError`, `OSError`) or a lone surrogate in
   the text is `bad_answer` naming it (stderr, exit 4). The revoke answer.
   Or `_opened`, each check before the browser and in `--json` too, each a
   `bad_answer` (exit 4) with its own `detail`: `link_is_safe(url, origin,
   to)` (`no sign-in link`), the `code` equal to the link's (`does not match
   its link`), `int(expires_in or 300)` (`no usable expiry time`); then,
   without `--no-open` and when `has_display`, `open_browser(url)`; then the
   JSON object (`state` `opened` or `printed`) or the minutes and the three
   printed lines.

## The browser (`open_browser`, `has_display`, `webbrowser.get()`)

Decided natively before the POST (`browser.rs`), read only from the
environment and the file system (and, with a display on POSIX, the
`xdg-settings get default-web-browser` call `register_standard_browsers`
makes):

- `has_display`: always on Windows and macOS; on Linux a non-empty
  `DISPLAY` or `WAYLAND_DISPLAY`, else the link is printed and no browser
  is consulted.
- `register_standard_browsers`: macOS's AppleScript entries first; Windows'
  `windows-default` first; on POSIX, with a display, `xdg-settings`' answer
  (FileNotFoundError, PermissionError, NotADirectoryError and a non-zero exit
  caught; any other error, or output that is not UTF-8, makes
  `webbrowser.get()` raise, so the link is printed) and the X list in
  CPython's order (`which` as `shutil.which` does it; `gvfs-open` only with
  `GNOME_DESKTOP_SESSION_ID`, `kfmclient` only with `KDE_FULL_SESSION`;
  a name inside xdg-settings' answer goes first); with `TERM`, the console
  browsers, every one refused (`open_browser` refuses Elinks, a
  `UnixBrowser` that falls back to the terminal, by name).
- `BROWSER`, split on `os.pathsep`: every non-empty entry goes to the front,
  the first one first; an entry that is only whitespace raises IndexError in
  `_synthesize` (printed). The first entry decides: with `%s`, `shlex.split`
  (POSIX mode, reproduced; its ValueError prints) and a last word `&` give a
  `BackgroundBrowser` (launched), any other a `GenericBrowser` (refused,
  printed); without `%s`, a `GenericBrowser` (printed) unless the first
  word's basename is a standard browser name, which `_synthesize` may clone
  (deferred).
- No usable `BROWSER`: the first standard controller. `WindowsDefault`
  (`os.startfile`), a `BackgroundBrowser` (`xdg-open`, `gio open --`,
  `gvfs-open`, `x-www-browser`, `skipstone`, `mosaic`), a terminal
  `GenericBrowser` (printed) or none (`webbrowser.Error`, printed).

The launch (`launch.rs`), after `link_is_safe` passed and before anything
prints:

- `BackgroundBrowser.open`: `[program] + [arg.replace("%s", url) ...]`, then
  `p.poll() is None`. POSIX: stdin, stdout and stderr inherited, every other
  descriptor closed (`close_fds=True`; marked close-on-exec here), `setsid`,
  then `_posixsubprocess`'s `execve` over the program as given (it holds a
  slash) or joined to each `os.get_exec_path()` entry, the first error that
  is not ENOENT/ENOTDIR reported; never execvp's `/bin/sh` fallback.
  Windows: std's `CreateProcess` with nothing redirected, so the browser
  gets this command's own standard handles, as CPython's child does
  (Windows copies a parent's redirected standard handles to a child started
  with none named). In both, a pipe this command writes to stays open until
  the browser exits.
- `WindowsDefault.open`: `ShellExecuteW(NULL, NULL, url, NULL, NULL,
  SW_SHOWNORMAL)`, the default verb `os.startfile` uses; True unless it
  returns 32 or less; a URL past 32767 UTF-16 units is `path_converter`'s
  ValueError (False).

Safety against a hostile answer: the URL handed to any launcher has passed
`link_is_safe`, which matches it whole: the answer's own normalized
`http(s)` origin, whose host holds only `[a-z0-9._~-]` or a bracketed
`[0-9a-f:.]` address, then `/ui/#/signin?code=`, a code of both daemons'
pairing-code alphabet (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`) and `-`, and
`&to=<page>` only for the page asked for. No character a POSIX shell or
`cmd.exe` reads as syntax passes but that one `&`. The candidate's URL is
one argv element (`execve` on POSIX; on Windows a `.exe` started by
`CreateProcess`, whose command line std quotes as `list2cmdline` does, never
a batch file or `cmd.exe`) or the one file `ShellExecuteW` opens, which can
only be an `http`/`https` URL; no shell reads it. The oracle starts a batch
file named by `BROWSER` through `cmd.exe` (the candidate defers it), where a
legitimate `&to=<page>` still splits the command line.

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
| `--unless-signed-in`: the session read, `already_signed_in` text and JSON, and going on after every other answer | `main` |
| Link: `link_is_safe` (the whole link: origin host, code alphabet, the asked page), the browser started (its argv) or not, `opened` / `printed` text and JSON | `_opened`, `link_is_safe`, `open_browser`, `has_display`, `webbrowser` |
| `int(expires_in)` exactly (Unicode decimal digits, underscores, whitespace, the 4300-digit limit, floats to their exact integer) and the minutes past 64 bits; a value it refuses is the `no usable expiry time` refusal | `_opened`, CPython `int()` |
| The sign-in refusals, text and JSON, before any browser: no sign-in link, a code other than the link's (a lone surrogate included), no usable expiry | `_opened`, `_bad_answer` |
| The list refusals: a time `localtime` refuses (NaN, the infinities, past `time_t`, past any year it holds, negative on Windows) and a lone surrogate in the printed text, before any line | `_list`, `_stamp`, `_bad_answer` |
| An `error` that is a list or object: an unknown code | `_refusal` |
| The empty `--revoke` usage error | `main` |
| Streams: Python text streams, CRLF on Windows; stderr writes a lone surrogate as `\udXXX` | `print` |

## Deferred before any request (the dispatcher's `mode 'console' is deferred` line, exit 1)

- A launcher this port does not start (`browser.rs`): the `UnixBrowser`
  family but Elinks (Mozilla, Netscape, Galeon, Chrome, Opera), Konqueror,
  Grail and the macOS AppleScript launchers (`MacOSXOSAScript`, the macOS
  default) as the first controller; a `BROWSER` entry without `%s` whose
  first word's basename is a standard browser name (`_synthesize` may clone
  that controller); on Windows a `BackgroundBrowser` whose program is not a
  drive-absolute `.exe` path without a quote (`CreateProcess(NULL, ...)`
  searches the interpreter's own directory and the current directory for a
  bare name, where std does not; a batch file runs through `cmd.exe`); a
  `BROWSER` value that is not valid Unicode. With `--unless-signed-in` the
  session read comes first (a read; case `defer-browser-unless`).
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

## Deferred after a request

Each is an answer this port does not reproduce; the native candidate prints
its deferral line (exit 1) after sending the same request. None is cased.

- A reset, timeout or malformed reply once connected (Python's
  `RemoteDisconnected` and `timed out` are `no_daemon`, `BadStatusLine` and
  `IncompleteRead` the `bad_reply` refusal); a 2xx body that fails to read;
  nesting past 500 (the oracle reads nesting past its parser's recursion
  limit as no JSON; the pairing read paths' measured limits are 985 and 987,
  console's is unmeasured); a stamp `localtime` converts past 2999, or
  before 1019 on Linux, where the oracle prints one; a write to stdout or
  stderr that fails before its first byte. On the sign-in POST these come
  after a code may have been minted: see "Declared divergences".

## Implemented, not compared by the harness

- The default launchers: `WindowsDefault` (`ShellExecuteW`) and the POSIX
  standard list (`xdg-open` and the other `BackgroundBrowser`s), and the
  `xdg-settings` call. Comparing them would open a real browser in one or
  both arms, so every harness case makes them impossible: it sets a
  `BROWSER` whose first entry decides, a `PATH` holding only the home's
  `bin` (checked when the row loads, and by `check_programs` before each
  arm for the names `webbrowser` looks up) and no `WAYLAND_DISPLAY`
  (`DISPLAY` only where a case needs `has_display`). Their decision logic
  is covered by the unit tests in `browser_tests.rs` against a fake
  machine (the try order, the preferred name, the probe's outcomes, `which`
  and `os.get_exec_path` against a temporary directory), none of which
  starts a program.
- Elinks as the first standard controller (refused, printed): a case needs
  no `BROWSER` for it, which the row forbids; `browser_tests.rs` and
  `tests/test_console_cli.py` cover it. A `BROWSER` naming `elinks` is a
  clone `_synthesize` makes, which still defers.
- macOS: the `BROWSER` path is the POSIX code (compiled for macOS by the
  release build only; no CI lane runs it); the macOS default defers.

## Declared divergences

- The deferrals after a request leave the request made: a revoke, or on the
  sign-in POST a code the daemon may already have minted (a transport
  failure once connected, an unreadable 2xx body, nesting past 500, a
  stdout that refuses its first byte); the dispatcher's Python then asks
  for a second code. The candidate is not installed as `pseudolife-mcp`,
  so this is the candidate's answer only.
- On POSIX, `xdg-settings` runs before the sign-in request rather than after
  it, and also when the answer turns out to be no safe link (the oracle then
  never calls `webbrowser.get()`); it only reads.
- On Windows std starts a program with handle inheritance on, so a handle
  this command itself inherited as inheritable, other than its three
  standard ones, reaches the browser; CPython's child gets the standard
  handles only.
- Windows' error text comes from `FormatMessageW` in the system language, as
  CPython's does; the row compares the host's own text.

## The browser stand-in (how the harness compares a launch)

Every case that reaches `open_browser` sets `BROWSER` to `'<python>' -I
'{HOME}/bin/stand-in.py' %s &` (`pythonw.exe` on Windows, so no console
window opens): a `BackgroundBrowser` in both arms. The stand-in records its
argv in `launched/launch-N.json` (the files snapshot compares it, the
daemon's per-arm port tokenized), then waits for a release file the case's
`during` hook writes once the CLI under test has exited, so `p.poll() is
None` holds by construction rather than by timing. The `after` hook waits
for the stand-in the output reports opened (on Windows it holds none of the
arm's pipes), gives any other 0.5 s to show, waits for each started one to
finish and exit (a SYNCHRONIZE handle on Windows, signal 0 and `/proc` on
Linux), and removes its bookkeeping; a stand-in that never finished leaves
`launched/unfinished-N`, which no arm matches.

## Hosted CI and goldens

The row runs live on both OSes in the `w1c-connection` shard. Every case is
built from fixed bytes (the fixture echoes the request's origin and page,
the origin tokenized per arm by the harness, and the stand-in's path is
under the tokenized home), so all are golden cases. The goldens record the
oracle at this branch, whose `pseudolife_memory/console_cli.py` differs
from master by issue #1497's fixes: Windows from this checkout (CPython
3.11.9), Linux from a clone of the branch under WSL Ubuntu (CPython
3.11.15).

The browser cases start a process (the stand-in) and wait on its exit and a
release file, so before the row's new cases join a required lane they need
20 clean hosted `windows-latest` runs: the Rust workflow's `mode=stress`
dispatch with `selection: row:console`.

Local evidence (2026-11-13, after issue #1497's fixes): Windows
workstation, 166/166 live and golden and all 15 of its mutants caught
(`console-display-ignored` is Linux-only); WSL Ubuntu, 166/166 live and
golden and all 15 of its mutants caught (`console-winerror-full-stop` is
Windows-only). The row holds 16 mutants, two CI shards of eight. Before the
port, the row
against master's deferring binary on Windows: 37 of the new and changed
cases differed, and the five that matched are the four declared pre-mint
deferrals and a `--no-open --json` answer the first slice already gave.
Hosted runs are CI's.
