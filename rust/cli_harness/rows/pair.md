# CLI-PAIRING contract: pair

Oracle: `pseudolife_memory/pair_cli.py` (with `client_config.write_token_file`,
`codex_connection._validated_daemon_url` / `installer_credential_valid`,
`connect_cli._shown_url`, `daemon_url._is_loopback_url`,
`principals.normalize_pairing_code`) at `origin/master` 1ed871a7; the daemon
side is `web/api.py` `_pair` (outcomes the fixture reproduces). Candidate:
`rust/shim/src/cli/pairing/{pair,net,token_file,url,pyjson}.rs`. Harness row:
`pair` (`rust/cli_harness/rows/pair.py`).

Note: on this master, `pair` writes only its token file; it does not touch
any client registration (that is `connect --code`, CLI-CONNECT).

## Canonical shapes (implemented)

- `pair <url> <code> [--token-file P] [--json]`
- `pair <url> --read-code [--token-file P] [--json]` (the installers:
  `pair "$DAEMON_URL" --read-code --token-file "$TOKEN_FILE" --json`)

The URL first, the code (when given) right after it, then options, each at
most once, a value as the next argument.

## Exact contract items

| Item | Python source |
|---|---|
| Exit codes 0 paired and verified, 2 usage, 4 refused (nothing kept), 5 outcome unknown or unverified (file kept) | `pair_cli.py:35-37`, 62-65 |
| URL: http(s) origin, no credentials/path/query/fragment, scheme lowercased, trailing `/` dropped; refusal names only `scheme://host[:port]` | `main` 372-377, `_validated_daemon_url`, `_shown_url` |
| Usage refusals in order: both a code and `--read-code`, no code, `--read-code read no code on stdin` (blank after `str.strip`), not a pairing code; the report carries the validated URL | `main` 378-389 |
| Code: strip, upper, dashes dropped, O->0 I/L->1, 12 of `0123456789ABCDEFGHJKMNPQRSTVWXYZ` | `principals.normalize_pairing_code` 75-84 |
| `--read-code`: one line from piped stdin (universal newlines) | `read_code_line` 202-206 |
| Target: `--token-file` as `abspath(expanduser())`, else `<home>/.pseudolife-mcp/pairing-<8 hex>.token`; an existing file or link refused before any request (`... already exists; pairing creates a token file but never replaces one. Nothing was changed`, exit 4) | `redeem` 216-222, `default_token_file` 156-157, `runtimes.home` |
| `/health`: no proxy, no redirect followed (a 3xx body still read), 64 KiB, JSON object with `status == "ok"`, else refused exit 4; `auth is True`, else refused naming `json.dumps(auth)` | `probe_health` 112-125, `redeem` 224-232 |
| Both answers read over `json.loads`'s whole domain: a lone surrogate kept (`json.dumps` writes `\udXXX`; never a principal name, never printable), an integer past 4300 digits a `ValueError` (not JSON), nesting up to the measured recursion limit (987 containers on both pair paths, CPython 3.11.9 through this CLI); a 2xx answer nested past it is no answer (outcome unknown), a refusal's body past it no payload | `_json` 105-109, `post_pair` 135-141 |
| Token: `secrets.token_urlsafe(32)` written owner-only before any POST: exclusive create (`O_EXCL`, `O_NOFOLLOW`), owner-only DACL (`O:<user>D:P(A;;FA;;;OW)`) or mode 0600 before a byte, the oracle's validation, re-read check, removed on failure; refusals `<Class> while writing <path>; ...`, exit 4 | `redeem` 234-243, `client_config.write_token_file` 184-230, `credentials._secure_windows_file` |
| `POST /api/pair`: body `{"code": "<canonical>", "token_sha256": "<hex>"}` (`json.dumps` defaults), `Content-Type: application/json`, no `Origin`, no `Authorization`, no proxy, no redirect, 10 s | `post_pair` 128-141, `redeem` 244 |
| Outcomes: 200 + object -> paired; after an unknown attempt any 3xx/4xx -> exit 5, file kept; 400 refused; 429 `rate_limited` and its text; 401/404/405 the old-daemon text; other 3xx/4xx refused (`HTTP n`); a 5xx, a non-200 2xx, or a 200 without a JSON object -> unknown, retried after 1, 2, 4 s with the same body; four unknowns -> exit 5, file kept; every refusal removes the file | `redeem` 257-286 |
| Answer: `tier` / `bank` kept when printable str of 1..128 characters; a principal that is a valid, unreserved name moves the default file to `<principal>.token` without replacing one (link, then unlink: both-names warning; existing target: kept-name warning); otherwise the pairing-name warning | `redeem` 288-313, `_text` 165-169, `_move_no_replace` 177-189 |
| Verification: `GET /api/episodes?limit=1` with the bearer, final status 200 (redirects followed), else `unverified`, exit 5, file kept | `redeem` 315-318, `installer_credential_valid` |
| Plain-HTTP warning first for a non-loopback `http://` URL (`localhost` or a loopback address is local) | `main` 391-393, `_is_loopback_url` |
| Report: `json.dumps(report, indent=2)` with keys `url, state, principal, tier, bank, token_file, warnings, error, exit`; text: `pair: warning: ...` lines and `pair: <error>` on stderr, `paired: <principal or ...>[ (tier T)] on <url>` and `token file: <path>` on stdout | `_report` 192-194, `_emit` 346-359 |

## Free items

- Request headers other than the content type and the absence of `Origin`
  / `Authorization` (User-Agent, Accept, Connection, Host spelling).
- The token's and the pairing name's random values (normalized by the
  validated `pair-token` rule: the stand-in daemon checks, while each POST
  is in flight, that one of the arm's token files hashes to the posted
  digest; the rule tokenizes the digest only then).
- urllib's per-socket-operation timeouts versus the native per-request ones.

## Deferred before any effect

- An `https` URL (the TLS trust store differs between the runtimes).
- Environment proxies (any non-empty `*_proxy` but `no_proxy`): the
  oracle's token check honours them through `urlopen`.
- `--read-code` with a terminal on stdin (`getpass`), non-ASCII code text
  (`str.upper` can map it into the alphabet).
- Argparse errors, `--help`, a code after an option, repeated options,
  `--opt=value`, abbreviations, a `--token-file` value starting with `-`.
- A home (`USERPROFILE`, else `HOME`) that pathlib would respell, or none;
  a target whose existence check pathlib would raise on; a non-Unicode path.

The token and the pairing name are drawn before the first request, so no
random-source failure can defer after it.

## Declared divergences

Two oracle crashes after a request (each an uncaught exception: exit 1, a
traceback naming the oracle's own source files). The candidate leaves the
same state (requests, files) and exits 1 with its deferral line; rules
`pair-recursion-traceback` / `pair-cleanup-traceback` map only that
traceback with exit 1:

- a `/health` nested past the recursion limit (`_json` sits outside the
  probe's `try`; limit measured on CPython 3.11.9);
- a refusal whose `target.unlink(missing_ok=True)` raises anything but a
  missing file (harness case `refusal-cleanup-fails`, Windows: the file held
  open without delete sharing). The token file stays, as in Python.

Others:

- Windows moves the default file by hard link then unlink (the POSIX
  oracle's own way) where the oracle renames without replace; on a volume
  without hard links the native move refuses (kept-name warning, exit 0)
  where Python renames.
- The token check's redirect following keeps reqwest's rule (the bearer is
  dropped on a cross-origin redirect); urllib 3.11 keeps it.
- Windows registry proxy settings: the native token check never uses a
  proxy; urllib's `getproxies()` reads the registry when no proxy variable
  is set.
- An `OSError` while writing the token file is named by `io::ErrorKind`
  (`PermissionError`, `FileNotFoundError`, `FileExistsError`,
  `NotADirectoryError`, `IsADirectoryError`, else `OSError`); CPython maps
  some other errnos to further subclasses.
