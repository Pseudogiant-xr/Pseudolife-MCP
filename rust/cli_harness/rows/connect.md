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
| `connect URL --code CODE` / `--read-code` | `remote-bank.md` 239, 393-394 |

Native: options may come before or after the URL. `--token-file`, `--client`
and `--code` accept a separate value or `--option=value`; `--` ends options.
`--help` and `-h` print help, and repeated boolean flags are harmless.
Repeated value options, missing values, abbreviations, unknown options,
extra positional arguments and non-Unicode arguments receive a native usage
refusal that names the fix without echoing a possible credential. Pairing (`--code`, `--read-code`) has been native since #1563, and
Codex's `config.toml` is parsed as text only to find the shim's
registration (and to classify a file without one); the env plan is read
from, and the write made through, Codex's own app-server. #1568 made the terminal native too: a run with changes to apply at a
terminal without `--yes` asks `Apply these changes? [y/N]` on stderr,
`--read-token` and `--read-code` at a terminal read without echo, and a run
for every client plans the unattended-update schedules it finds
(`schedule.rs`: one exactly as `update --schedule` writes it, of the kind
`--schedule` writes on this machine, is re-created against the new URL
keeping its kind and daily time, or is `current` when it already names that
URL; the daemon's own schedule when the daemon moves to another machine,
and any other, is a manual row).

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
- A refused stdout still changes the status: CPython's interpreter-shutdown
  flush fails, prints an ignored-exception trailer and exits 120, whatever
  `main` returned. The native leaf remembers the refusal and exits 120 at
  the end (rule `python-stdout-closed-trailer` removes only that trailer, at
  exit 120). Cases `apply-move-yes-stdout-closed` and
  `apply-move-json-stdout-closed` compare status, files and requests beside
  their healthy controls `apply-move-yes` and `apply-move-json`. Limit: this
  holds while the refused output fits CPython's 8192-byte buffer (one print
  over 8192 bytes raises at that print instead); connect's plan and report
  are far smaller, and a longer one is not modelled.
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
  connect's own requests in order, each with method, target, every header
  field by name (repeats flagged) and the body, and, of the child, only the
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

## Native input handling

The inventory below was made against `f91ec4071`. `Defer` mentions in result
signatures and infallible `J::py_str` calls are propagation sites, not additional
input conditions. The command now always returns a native status. The legacy
marker remains only to keep `schedule.rs` and its URL helper interface source
compatible; the URL helper no longer constructs that marker. Pairing's
`redeem_for_connect` error arm remains unchanged and belongs to the pairing
workstream.

Unless noted, tests are in `rust/shim/tests/cli_connect_contract.rs`; `native_`
regressions were run against the baseline as well as the changed binary. Error
messages never include the token or an unrecognized argument's value. A refusal
before application writes no configuration or token. Post-apply failures retain
the existing rollback and verification handling.

| File / condition | Native result or refusal | Regression |
|---|---|---|
| `args.rs`: `--help`, `-h` | Print usage, exit 0 | `native_help`, `native_short_help` |
| `args.rs`: options before URL, `=`, repeated flags, `--`, dash-leading value | Parse natively | `native_option_before_url`, `native_equals_option`, `native_repeated_flag`, `native_separator`, `native_dash_value` |
| `args.rs`: missing URL / value, empty value, extra positional argument | `a daemon URL is required`, `<option> needs a value`, or `give exactly one daemon URL`, with syntax to use | `native_missing_url`, `native_missing_value`, `native_option_as_value`, `native_empty_value`, `native_empty_url`, `native_extra_url` |
| `args.rs`: unknown or abbreviated option, repeated value option, flag with a value | `unknown option; use the full option names`, `<option> was given more than once; pass it once`, or `<option> takes no value` | `native_unknown_option`, `native_duplicate_value`, `native_flag_with_value` |
| `args.rs`: non-Unicode argv | `an argument is not Unicode; pass Unicode arguments` | `native_non_unicode_arguments_are_named_without_echoing_them` |
| `url.rs`: Unicode host | IDNA hostname, after checking the original origin boundaries | `url::native_url_regressions::unicode_host_is_idna_without_changing_origin_boundaries` (unit) |
| `url.rs`: whitespace/control/DEL, invalid bracketed host, IPvFuture or zoned IPv6 | Native daemon-origin usage refusal; give a plain http(s) origin without path, query, fragment or credentials | `native_url_control`, `native_invalid_ipv6`, `native_invalid_ipvfuture`, `native_invalid_zone`; ordinary IPv6 remains covered by the URL unit tests |
| `url.rs`, `discover.rs`, `codex.rs`: unsupported saved/environment URL to show or compare | Redacted `<unreadable URL>`; preserve existing manual/change-row rules | `native_unsupported_saved_url_is_redacted_in_the_plan`, `native_unsupported_process_url_is_a_manual_row`, `an_unsupported_user_scope_url_is_redacted_and_planned` (unit) |
| `paths.rs`: non-Unicode environment setting | `<setting> is not Unicode; set <setting> to a Unicode value` | `native_non_unicode_environment_is_named_without_echoing_it` with a Unicode-directory control |
| `paths.rs`: no USERPROFILE or HOME | `USERPROFILE and HOME are unset or empty; set HOME to your absolute home directory` | `native_missing_home_has_a_remedy` |
| `paths.rs`: trailing/repeated separators, `.` components, Windows forward slashes | Normalize equivalent absolute components without resolving links | `native_trailing_path_separator_selects_the_same_registration`, `paths::tests::equivalent_absolute_spellings_keep_the_same_path` |
| `paths.rs`: relative HOME/USERPROFILE, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME, XDG_DATA_HOME, APPDATA, LOCALAPPDATA, explicit shim layout paths | `<setting> must be an absolute path; set <setting> to the full directory path` | `native_relative_*`, each with an absolute-path control |
| `paths.rs`: parent components / NUL, Windows UNC/device paths | Name the setting and require an absolute path without those components, or a local drive-absolute path | `native_parent_components_name_the_directory_setting`, `paths::tests::equivalent_absolute_spellings_keep_the_same_path`; the existing absolute-path boundary is retained |
| `paths.rs`: non-Unicode MSIX package name | Ignore unrelated packages; name `LOCALAPPDATA/Packages` and repair the Claude Desktop installation for a matching Claude package | `native_non_unicode_package_names_only_block_claude_packages` (Windows) |
| `discover.rs`, `paths.rs`: non-Unicode current directory / displayed file path | Name the current directory or client path and require a Unicode location | `native_non_unicode_working_directory_has_a_remedy` |
| `mod.rs`: `--token-file` cannot become an absolute Unicode path | Name `--token-file`; pass an absolute Unicode file path | `native_non_unicode_token_path_has_a_remedy` with a readable relative-path control |
| `files.rs`: stat failure other than the existing missing-path cases | Name the file, OS error class and access repair | `native_inaccessible_config_names_the_path_and_permissions` with a permission-restored control |
| `files.rs`, `pyjson.rs`: lone surrogate, depth over 100, integer over 4300 digits | Name the JSON file and exact limit; repair that JSON file | `native_json_surrogate`, `native_json_depth`, `native_json_integer_limit`, with valid surrogate-pair / at-limit controls |
| `mod.rs`: literal/file credential contains DEL | Name client and `PSEUDOLIFE_MCP_TOKEN` or `PSEUDOLIFE_MCP_TOKEN_FILE`; replace the invalid credential | `native_del_token_is_refused_without_disclosure_or_writes`, `native_del_in_token_file_names_the_setting` |
| `files.rs`: DEL read from stdin | Name `--read-token`; supply a valid token before any directory/file creation | `read_token_del_is_refused_before_creating_a_directory` (unit), with `~` control |
| `net.rs`: unsupported /health JSON | Name `/health` and the exact JSON limit; repair the daemon or proxy response | `native_health_json_limit_has_a_remedy`; valid health responses are exercised throughout the contracts |
| `net.rs`: unsupported redirect | `/health response redirects to an unsupported URL; use its direct http(s) daemon origin` | `native_unsupported_health_redirect_has_a_remedy`; existing `health-redirect` golden pins ordinary HTTP redirects |
| `mod.rs`: preflight backup read fails after discovery | Name the target and OS error; restore read access before running connect | Existing rollback/read-only-file contracts; this read-after-discovery race is not separately fault-injected |
| `discover.rs`: missing string command after `is_shim` admitted it | Defensive named registration refusal | Unreachable for an unchanged parsed registration: `is_shim` already requires a string; non-shim rows remain manual |
| `files.rs`: missing/non-Unicode basename; `pyjson.rs`: float parser fails on a scanner-admitted number | Defensive named file/number refusal | Fixed configuration basenames and the scanner make these unreachable through ordinary input; parser-domain unit tests retained |
| `mod.rs`: I/O runtime creation fails | Name the OS failure; resolve that system error and re-run | `native_runtime_failure_names_the_system_cause` (Unix unit subprocess with exhausted descriptors), with an unrestricted control |
| `mod.rs`, `discover.rs`: signal setup or current-directory lookup fails | Name the system failure and the retry/remedy | Signal setup is not fault-injected; current-directory Unicode failure is covered above |

No source-mutant entry points at a removed connect branch. The existing connect
mutants continue to protect writes, backups, credentials, ordering and transport.
No new harness file or hosted job is added.

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

- `apply-restart-pids` starts a copied host executable. Its OS-version-specific
  bytes are fixture input, not connect output: after the owned process exits,
  `connect-runtime-image` projects the captured observation to a fixed marker
  only if its bytes still equal the seeded bytes exactly. No fixture file is
  rewritten. The image must remain a single-link regular file inside its
  pre-command bound home; its mode and ACL stay exact. Failed validation always
  causes a DIFF, even if the command wrote the final marker itself; recording
  refuses a failed projection. Only this Windows
  golden case was re-recorded; its other recorded cases remain intact.
  The snapshot key is derived from the same resolved image used for containment,
  against the fixed home captured before the command, so Windows short-path
  aliases select the recorded file. Refusals retain each filesystem predicate,
  capture/hash checks and sanitized path-relation flags; diff text includes
  these checks and the seeded/captured hashes without host paths.
- Each arm gets `TEMP`/`TMP`/`TMPDIR` = `<home>/tmp` (under the default
  temp root, same ACLs). The oracle runs its handshake child and the shim
  under it from `tempfile.gettempdir()` with that directory first on
  `sys.path`; on the shared Windows host the default TEMP held ~115,000
  entries churned by other sessions, and 6 of 46 oracle apply cases missed
  the child's 20 s budget (standalone: 1 of 6 runs failed from TEMP, 0 of 6
  from a small directory). This is an oracle fragility on busy hosts, not
  candidate behaviour.
- Requests are attributed to the process holding the client end of each
  connection (`psutil`): the arm's process, or, when the oracle's
  `python.exe` is a Windows venv launcher that runs the interpreter as its
  child (measured once per executable), that child. The self-hosted lane's
  per-shard venv dropped every own request of the oracle on Windows until
  this (2026-10-31: 60 of 69 cases "requests differ", hosted passing). So
  connect's own requests are compared exactly:
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
- Live-only off Windows: `apply-restart-pids` runs live on both OSes but
  is left out of the Linux golden (`Case.golden`), retaining the established
  corpus split. Its copied `sleep` image is validated and projected by the
  same capture as the Windows fixture.
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
