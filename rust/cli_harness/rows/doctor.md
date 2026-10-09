# CLI-DOCTOR contract (native `pseudolife-mcp doctor`)

Oracle: `pseudolife_memory/doctor_cli.py` at this branch's base (35b8f5d2),
with the helpers it calls. Line numbers are that file's unless named.

## Canonical argv

Producers: README "pseudolife-mcp doctor", docs/guide/configuration.md
"Coordination diagnostic quickstart" (`doctor --host codex|claude-code`,
`--agent-state <file>`, `--disposable-proof`), docs/guide/providers.md,
ops/install.* ("run doctor from that command's environment").

Answered: `doctor`, with `--host codex|claude-code|claude-desktop|generic`
and `--timeout N` (plain positive decimal), each at most once, any order
(:548-552). `--disposable-proof` without a nonempty
`PSEUDOLIFE_TEST_DATABASE_URL` keeps the existing refusal (:562-566, exit 2).

Deferred before any effect: `--agent-state` (saved-instance proof,
:446-503, :648-653); `--disposable-proof` with a nonempty DSN (it runs the
Python daemon's ASGI app in process, coordination_proof.py); every other
argv shape (abbreviations, `=` forms, repeats, help, bad values).

## Exact contract items

| Item | Oracle |
|---|---|
| Report key order and insertion semantics (`ok` first, later `ok`/`recovery` reassignments keep position, late `error` appended) | :579-682 |
| `json.dumps(report, indent=2)` + newline (ensure_ascii, `": "`, CRLF on Windows text stdout); exit 0 iff `ok` | :681-682 |
| Credential borrowing: shell token or token-file presence = `environment`; else Claude Code `.claude.json` (`CLAUDE_CONFIG_DIR` or home, utf-8-sig) then Codex `config.toml` (`CODEX_HOME` or `~/.codex`) `mcp_servers.pseudolife-memory.env`; URL only when the shell has none; label `"<client> registration (<pathlib str>)"` | :144-185, :586-590 |
| Board line: no token = `missing_bearer`; `GET /api/hook/coordination-start` with `Bearer`, no redirects; HTTP error states 401/404-405/other; `X-PL-Board` on / served body / `reason=` table / unrecognized / unsupported | board_status.py:46-73, :340-354 |
| Maintainer passkeys only when the board is on; `GET /api/maintainer`; 200 on-line with active-key count, 404/405, 409 unset/invalid(+recovery)/older, other `HTTP n code`; not-checked when the board is off | :380-443, :594-596 |
| Git Bash (Windows only): settings env block over process env, `CLAUDE_CODE_GIT_BASH_PATH` name check, default install dirs, `git` on PATH two levels up; `bash` on PATH and WSL-launcher test; both recovery texts | :26-101, :598-599 |
| `GET /health` (redirects followed; an error status's JSON body counts); `daemon_status` (`unreachable` for none or `{}`); `BearerRejected` / `DaemonUnavailable` / `BearerMissing` and their recovery text | shim.py:436-451, :600-614 |
| `daemon_version` (`or "unknown"`), `codex_hooks` (`bundle-present`, `not-configured`, `unknown` without digest, `unknown (UnicodeDecodeError)`) | :615-617, client_updates.py:1528-1592 |
| Handshake: own shim with `PSEUDOLIFE_MCP_NO_SPAWN=1`, `PSEUDOLIFE_AGENT_COORDINATION=0`, the overridden environment; SDK initialize, `tools/list` (one page); `instructions_present`, `tool_count`, `tools_missing_annotations`, `coordination_tools_present`; `ok` rule and recovery; version mismatch rule and recovery text; `TimeoutError` (whole handshake incl. shutdown under `--timeout`) and `ExceptionGroup` (anything raised inside the client) with their recovery text | :104-125, :618-646 |
| Wake report: Claude Code Stop hook (registration substring, installed_plugins, settings env block over process env, enabledPlugins false), Codex doorbell (TOML env table, `env_vars` forwarding, writer, bearer, CLI lookup), `_wake_state` texts, caps filter (known keys, JSON integers >= 0, daemon order) | :197-324, codex_doorbell.py:125-172 |
| Coordination snapshot fields, order and `next` texts | :505-541, :654-657 |
| GitBashMissing and MaintainerPasskeysInvalid finalization | :660-674 |
| `path_resolution`: CPython 3.11 `shutil.which("pseudolife-mcp")` (cwd first and PATHEXT on Windows), launcher from `PSEUDOLIFE_SHIM_RUNTIMES`/`_LAUNCHER` or the default layout, realpath/normcase comparison, warning text | :690-707, runtimes.py:127-154 |
| No incidental mutation: doctor writes nothing; the only file a run leaves is the handshake shim's own `~/.pseudolife-mcp/handshake-cache/<sha256(url)[:16]>.json` (CLI-SHIM's file, written by the Python shim too) | shim.py:708-725 |

## Free items

- Request headers other than the path and `Authorization` (user agent,
  `Accept-Encoding`, `Connection`) and the shim's own HTTP traffic (CLI-SHIM).
- `--timeout` budgets are bounds, not observable values; socket timeouts are
  per operation in both arms.
- The interpreter identity values (declared substitution below).

## Declared substitutions and divergences

- `doctor-runtime-identity` (harness rule): `interpreter`, `source`,
  `pseudolife-mcp`, `mcp` describe the running runtime. The oracle reports
  its Python interpreter, package directory, installed distribution and MCP
  SDK versions; the native doctor reports its own executable, the directory
  it resolves into, its Cargo version and `mcp: not installed`. The rule
  validates each arm's values against what that arm must report, then
  tokenizes them and that arm's own version where the report or the shim's
  mismatch warning repeats it. `tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes`
  asserts `interpreter == sys.executable` and can never hold for a native
  runtime.
- `shim-handshake-cache-semantic` (harness rule, inherited from CLI-SHIM):
  the Python shim writes the handshake cache with `json.dumps` (spaced
  separators, ASCII escapes, `url` first); the native shim's `cache.rs`
  writes compact UTF-8 with `url` last. Compared as parsed JSON, that path
  only.
- Proxies: urllib honours `*_proxy` variables and, on Windows without them,
  the WinINet registry proxy. The native probes connect directly; any
  `*_proxy` variable defers, the registry is not consulted.
- Closed stdout: the oracle's print raises and CPython exits 120 after a
  shutdown flush trailer; the native doctor exits 1.

## Deferral domain (before the first request unless noted)

`--agent-state`; nonempty-DSN `--disposable-proof`; noncanonical argv; any
`*_proxy` variable; an invalid daemon URL; a credential provider error
(missing, unsafe or malformed token file, invalid static token); a bearer
outside Latin-1 or with control characters; saved tunnels present
(`~/.pseudolife-mcp/tunnel`); JSON files only Python may read (NaN,
Infinity, `\u` escapes, nesting past the parser's limit) and every TOML
parse failure; non-UTF-8 environment values read; settings values whose
`str()` needs Python's repr (floats, non-empty containers); `~` in
`CODEX_HOME`/`PSEUDOLIFE_CODEX_BIN`; UNC, device and drive-relative paths;
PATH absent on POSIX (`os.confstr`); stat errors pathlib would raise.
After a read-only GET (board, maintainer or health) but always before the
handshake starts the shim: a non-object health body, a
non-string `status`, a truthy non-string `version`, a `hooks_digest` with a
Codex plugin configured (digest comparison not ported), an unfollowed
redirect, a duplicate or non-ASCII `X-PL-Board` header, maintainer
`rp_id`/`origin` that are not strings or null, response bodies only
Python's decoder admits.
