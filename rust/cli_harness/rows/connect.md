# CLI-CONNECT spec (`pseudolife-mcp connect`, JSON-file clients)

Oracle: `pseudolife_memory/connect_cli.py` with `client_config.py`,
`codex_connection.py` (URL validation, credential check), `runtimes.py`
(client discovery, layout, process table), `credentials.py`,
`daemon_url.py`, `board_status.py`, `shim.py` (`probe_health`) and
`doctor_cli.py` (`_handshake`). `client_updates.py` is not reached by
connect: the shim only runs it after a first frame with
`updates.unattended_clients`, which the handshake never sees.

## Canonical argv (producers)

| Shape | Producer |
|---|---|
| `connect URL --token-file TF --client NAMES --dry-run --json` | `ops/install.sh` 1886, `ops/install.ps1` 1857 (plan) |
| `connect URL --token-file TF --client NAMES --yes` | `ops/install.sh` 1915, `ops/install.ps1` 1892 (apply) |
| `connect URL --dry-run`, `connect URL` | `docs/guide/remote-bank.md` 510-511 |
| `connect URL --client C --token-file ~/... [--read-token]` | `docs/guide/remote-bank.md` 536-541 |
| `connect URL --yes [--json]` | `move_cli.py` 1642, 1635, 948 |
| `connect URL --token-file F` | `move_cli.py` 1708 |
| `connect URL --code CODE` / `--read-code` | `remote-bank.md` 221 (deferred: pairing) |

Native: the URL first, then `--token-file V`, `--client V`, `--read-token`,
`--dry-run`, `--yes`, `--json`, each at most once, a value never empty and
never starting with `-`. Everything else defers before any effect.

## Exact contract items

- Usage refusals, in order (`connect_cli.py` 1024-1046): URL validation by
  `codex_connection._validated_daemon_url` (`codex_connection.py` 104-117)
  with `_shown_url` (292-304) in the message; `--client` list (918-924,
  1030-1032, Python `repr`); `--read-token` without `--token-file`
  (1039-1040); `--read-token` onto an existing path (1041-1046,
  `abspath(expanduser())`). Exit 2.
- Health (1058-1074): `shim.probe_health` (`shim.py` 436-449: any HTTP
  answer's JSON body, 503 included, redirects followed); not a dict or
  status not `"ok"` exits 4 with `(status: str(value))` for a dict, a
  container's `str()` being its `repr`; a remote target with `auth: false`
  exits 4; `daemon` = `{version, auth}`; plain-HTTP warning for a remote
  `http://` target (stderr, `warnings`).
- A stderr that refuses a warning or the failure line: the oracle's
  line-buffered stderr raises at that print, and CPython exits 120 with
  nothing written after it (the plain-HTTP warning comes before the plan,
  the verification warnings after the plan and before the writes; a token
  file `--read-token` already wrote stays). The native leaf stops at the
  same print with exit 120. A refused stdout is not a stop: the oracle's
  stdout is block-buffered on a pipe and raises only at its exit, after
  every step, so the native leaf carries on too. Cases
  `apply-remote-stderr-closed`, `apply-board-off-stderr-closed`.
- Remote = not `_is_loopback_url` (`daemon_url.py` 58-71).
- Discovery (667-677): Claude Code user scope (460-494) with the settings
  copy inserted after a writable registration (497-518), project scopes in
  `projects[...]` and `./.mcp.json` as manual rows; Claude Desktop config
  files in `runtimes.desktop_config_files` order (`runtimes.py` 1253-1271:
  MSIX `Packages/Claude_*` sorted, then `APPDATA`), the legacy
  `pseudolife-memory` entry as manual (521-546); Gemini (549-560); Codex
  absent without a `config.toml`; the process `PSEUDOLIFE_MCP_DAEMON_URL`
  row (628-635). `Path.exists()` / `is_file()` read only pathlib's ignored
  errors (ENOENT, ENOTDIR, EBADF, ELOOP, Windows 21/123/1921) as "no".
- Rows (282-285): key order `client, place, file, key, state, changes,
  detail, notes, backup, created`; `changes` sorted by key with
  `<literal token>` and `_shown_url` masking (314-325); edits per
  `_registration_edits` (347-358, NO_SPAWN for remote unless truthy) and
  `_settings_edits` (361-377); notes (380-395) including the launcher
  comparison by `_path_forms` (`runtimes.py` 1034-1043, 1355-1356);
  register commands (412-428) with the launcher when it is a file.
- JSON reading (`client_config._load_json`, `client_config.py` 75-85):
  utf-8-sig, whitespace-only is `{}`, CPython's C scanner (`NaN`,
  `Infinity`, `-Infinity`, strict control characters, four-digit `\u`
  escapes with surrogate pairing, a repeated key keeping its first place
  with its last value), messages by file name. Writing: `_write_json`
  (88-90: indent 2, `ensure_ascii=False`, trailing newline, `int(lexeme)`
  and `repr(float)` spellings, `NaN`/`Infinity`); `_write_private` (38-62:
  realpath, owner-only temporary, atomic replace); `_backup` (65-72:
  `<name>.bak-pseudolife-<UTC %Y%m%d-%H%M%S-%f>`).
- Plan text and exits (963-975, 1083-1115): exit 3 messages with
  `_places`; dry-run note; nothing-to-write notes.
- Confirmation (1118-1123): non-interactive without `--yes` exits 2.
- `--read-token` (1125-1133, `client_config.write_token_file` 184-230):
  stdin is read only here, after confirmation; HelperError messages, class
  names for OSError/CredentialError.
- Verify (691-736): distinct credentials in row order; the
  no-credential refusal; `check_token_file` recovery text (the BOM text
  included); literal well-formedness; `installer_credential_valid`
  (`codex_connection.py` 137-148: `/api/episodes?limit=1`, bearer, no
  redirects, 200 only, the header put as `http.client` puts it: latin-1
  bytes, and no request at all for a character past U+00FF or a CR/LF not
  followed by folding whitespace); MCP handshake (146-184: child env,
  initialize + tools/list, `the handshake exited N` / `the daemon listed no
  tools`); `board_status` line (`board_status.py` 40-71, `bytes.strip()`
  including `\x0b`); board warning with the hint. Refusal exits 4 after
  removing a `--read-token` file.
- Apply (755-786, 861-879) and rollback (822-858): per-file record
  (`_bytes`: only FileNotFoundError reads as "no file"), backup, created
  flag, read-back compared with Python dict equality (unordered keys,
  bool/int/float numerically, json's shared NaN equal to itself; a
  read-back that is not JSON takes the HelperError branch, which refreshes
  `written`); outcomes `unchanged`/`left`/`restored`/`removed`/`failed`
  with their key orders and the rollback lines (1166-1172); exit 1.
- Report (1174-1201): backups, verified lines, `_restart` (1000-1014) over
  `runtimes.list_processes` / `processes_inside` (`runtimes.py` 920-1064),
  the Desktop and board notes, post-apply re-discovery and health (exit 5).
- `--json` (931-961): one `json.dumps(indent=2, default=str)` document,
  ASCII-escaped, key order `url, remote, dry_run, daemon, rows,
  verification, warnings, notes, restart, rollback, error, exit`.
- Connect's own requests on the wire, every header field and the body
  compared (the harness's wire projection): the health probe
  (`shim.probe_health`, `urlopen`, 2 s), the credential check
  (`installer_credential_valid`, no-redirect opener, 3 s, `read(1)`) and
  the board probe (`board_status.board_probe`, no-redirect opener, 2 s,
  `read(65536)`) are GETs as urllib sends them, through `cli::hook_http`:
  `Host` as written, `User-Agent: Python-urllib/3.11`,
  `Accept-Encoding: identity`, `Connection: close`, the optional
  `Authorization` as Latin-1 bytes, no `Accept`; per-receive timeouts; the
  health probe follows redirects under `HTTPRedirectHandler`'s limits and
  parses a 3xx where urllib raised `HTTPError` with it, the other two never
  follow.

## Free items

- Timeouts are budgets, not observed values.
- The handshake child's own transport (its MCP request order and count,
  its own `/health` and episode posts, its request fields, how it encodes a
  non-ASCII bearer) belongs to the shim's rows: the harness compares
  connect's own requests raw and in order and, of the child, only the
  `Authorization` values its MCP posts carry.
- The temporary file name beside a written file (removed before exit).
- Proxy discovery: urllib reads the proxy environment variables and the
  Windows registry proxy; connect's native requests connect directly.
  Neither is set in any supported install path.

## Shared-file fix: the handshake cache bytes

The shim the handshake starts writes `~/.pseudolife-mcp/handshake-cache/
<digest>.json`. `shim.py` 708-717 writes `json.dumps({"url": url,
**cached})` (default separators, ASCII escapes, `url` first);
`rust/shim/src/cache.rs` wrote compact JSON with `url` last. `store` now
writes the oracle's bytes (top-level order and formatting; numbers keep
their lexemes; the tool objects stay the daemon's wire objects, which the
Python daemon emits from the same MCP models `model_dump` reads). Pinned by
`the_handshake_cache_is_written_as_the_oracle_writes_it` (watched red first,
bytes captured from the oracle's own writer) and compared byte for byte by
every apply case of this row.

## Deferred before any effect

Every deferral below happens before any request, except the last item.

- Pairing (`--code`, `--read-code`).
- A readable Codex `config.toml` with `codex` selected (its app-server owns
  Codex's config).
- A question or token prompt on a terminal (Python's `isatty`, so a Windows
  character device such as `NUL` too).
- A Windows User-scope `PSEUDOLIFE_MCP_DAEMON_URL` (`reg query`); an
  unattended-update schedule (`schtasks /Query` on Windows, the systemd user
  unit on Linux) when every client is selected.
- A target URL with a space, a control character, DEL or any non-ASCII
  character (urllib's lstrip/tab-removal and NFKC netloc check are not
  reproduced), an IPvFuture or zoned IPv6 host, or userinfo.
- Environment base directories not already in `str(pathlib.Path(...))`
  form; a stat error pathlib would raise; a read error other than
  FileNotFoundError on a file the plan would write.
- JSON the Python reader holds but this one cannot: a lone surrogate,
  nesting past 100, an integer past 4300 digits (CPython raises ValueError).
- A real run whose credentials hold DEL (urllib sends it; the `http`
  crate's header type refuses it).
- After the unauthenticated `GET /health` only: a health body with one of
  the JSON inputs above.

## Declared divergences

- The handshake child is the candidate's own shim (`current_exe()`), as
  the oracle's is its own interpreter's shim (`sys.executable -m
  pseudolife_memory.cli`); every failure inside it reads `the handshake
  exited 1`, which is what the oracle prints for any exception in its
  child; a child that cannot be started reads `OSError` (the oracle names
  the subclass).
- OS error class names follow CPython's errno/Windows error maps for the
  codes a config write meets; others read `OSError`.
- Windows `normcase` uses Unicode simple lowercase (U+0130 to `i`), not
  the NLS table of the running Windows version.
- Windows `realpath` resolves the longest existing prefix with the OS and
  appends the rest (`ntpath` normalizes first, as this does); a dangling
  symlink inside the path is not followed by hand as `ntpath._readlink_deep`
  would. POSIX walks components as `posixpath._joinrealpath` does.
- Races only (a file changed by something else during the run): a read
  error on a target after the preflight is reported as that file's write
  failure and rolled back, and a rollback that cannot read a file leaves it
  with its backup, where the oracle would raise a traceback; a post-apply
  re-read that meets an input outside the native domain reports as a failed
  post-apply check; a second `/health` body outside the reproduced JSON
  domain reads as not ok.
- A `--read-token` token holding DEL is refused (the `http` crate cannot
  send it);
  the oracle would send it. Its stdin is read only after confirmation, so it
  cannot be checked before the plan.

## Harness notes

- Each arm gets `TEMP`/`TMP`/`TMPDIR` = `<home>/tmp` (under the default
  temp root, same ACLs). The oracle runs its handshake child and the shim
  under it from `tempfile.gettempdir()` with that directory first on
  `sys.path`; on the shared Windows host the default TEMP held ~115,000
  entries churned by other sessions, and 6 of 46 oracle apply cases missed
  the child's 20 s budget (standalone: 1 of 6 runs failed from TEMP, 0 of 6
  from a small directory). This is an oracle fragility on busy hosts, not
  candidate behaviour.
- Requests are attributed to the process holding the client end of each
  connection (`psutil`), so connect's own requests are compared exactly:
  method, target, every header field and the body, as received (review of
  #670: the earlier capture kept only a credential label, so a different
  rejected credential or a changed field was invisible). Same-outcome
  controls: mutants `connect-latin-1-credential-as-utf8` (a Latin-1
  credential sent as UTF-8, still refused alike) and
  `connect-user-agent-changed`.
- Fixture credentials are generated per harness process and appear in no
  source or golden. After every check that reads the raw observation, the
  row's `after` hook replaces exactly those values, wherever this arm's
  observation holds them (streams, request fields, every file under the
  home, rewritten in place before the snapshot), by `<credential:good>` /
  `<credential:other>`; anything around them, a suffix or another encoding
  included, stays as sent.
- `shim-handshake-cache-name` (`rows/_handshake_cache.py`): the handshake
  shim's cache file is named `sha256(url)[:16]` of the fixture URL, whose
  port differs per run; only that name, validated against this case's own
  fixture URL, becomes `<fixture-url-hash>.json`, so goldens replay.
- `connect-target-url`: where no fixture daemon answers the target
  (`health-unreachable`), the harness's daemon token never runs; the case's
  own target URL (a free loopback port the harness picked, recorded as
  `fixture_url`) becomes `{DAEMON}` in the streams and files, that exact
  URL only.
- A remote target is `http://localhost.:<port>`: not loopback to
  `_is_loopback_url`, resolved to `::1`/`127.0.0.1` where the fixture
  listens.
- Permissions: master's core compares POSIX modes; on Windows each file's
  owner-only verdict (the oracle's `credentials._windows_owner_only`) is
  recorded in the compared `db` field as `windows_acl`.
- `connect-backup-stamp` keeps two names that normalize alike apart
  (` <normalized-collision>`), and replaces a stamp in the streams only
  when this arm's snapshot holds a backup with that stamp.
- Not covered by a case: the read-back failure (it needs a file replaced
  between write and read-back), pinned instead by the Rust unit tests on
  `read_back`; and the post-apply pending path (it needs a file to change
  between the writes and the re-read), which has no test.
