# CLI-DOCTOR contract (native `pseudolife-mcp doctor`)

Oracle: `pseudolife_memory/doctor_cli.py` at this branch's base (35b8f5d2),
with the helpers it calls. Line numbers are that file's unless named.

## Canonical argv

Producers: README "pseudolife-mcp doctor", docs/guide/configuration.md
"Coordination diagnostic quickstart" (`doctor --host codex|claude-code`,
`--agent-state <file>`, `--disposable-proof`), docs/guide/providers.md,
ops/install.* ("run doctor from that command's environment").

Answered: `doctor`, with `--host codex|claude-code|claude-desktop|generic`,
`--timeout N` (plain positive decimal) and `--agent-state FILE` (not
starting with `-`), each at most once, any order (:548-554).
`--disposable-proof` without a nonempty `PSEUDOLIFE_TEST_DATABASE_URL`
keeps the existing refusal (:562-566, exit 2).

Deferred before any effect: `--disposable-proof` with a nonempty DSN (it
creates a fixture bank and drives the Python daemon's ASGI app and adapters
in process, coordination_proof.py: there is no native daemon to drive);
`--disposable-proof` with `--agent-state` (an argparse error); every other
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
| `GET /health` (redirects followed by `Location`, else `URI`; where urllib's redirect handler raises HTTPError instead, with no target, a scheme other than http/https/ftp or past its repeat limits, the 3xx body is parsed as `e.read()` is; an error status's JSON body counts); `daemon_status` (`unreachable` for none or `{}`); `BearerRejected` / `DaemonUnavailable` / `BearerMissing` and their recovery text | shim.py:436-451, :600-614 |
| `daemon_version` (`or "unknown"`), `codex_hooks` (`bundle-present`, `not-configured`, `unknown` without digest, `unknown (UnicodeDecodeError)`) | :615-617, client_updates.py:1528-1592 |
| Handshake: own shim with `PSEUDOLIFE_MCP_NO_SPAWN=1`, `PSEUDOLIFE_AGENT_COORDINATION=0`, the overridden environment; SDK initialize, `tools/list` (one page); `instructions_present`, `tool_count`, `tools_missing_annotations`, `coordination_tools_present`; `ok` rule and recovery; version mismatch rule and recovery text; `TimeoutError` (whole handshake incl. shutdown under `--timeout`) and `ExceptionGroup` (anything raised inside the client) with their recovery text | :104-125, :618-646 |
| Wake report: Claude Code Stop hook (registration substring, installed_plugins, settings env block over process env, enabledPlugins false), Codex doorbell (TOML env table, `env_vars` forwarding, writer, bearer, CLI lookup), `_wake_state` texts, caps filter (known keys, JSON integers >= 0, daemon order) | :197-324, codex_doorbell.py:125-172 |
| `--agent-state`: probed only when the host is not claude-desktop and the board is on; `exists()`, `read_legacy` (no symlinked ancestor, `open_private` owner-only regular file, 16384-character text read, object with `bank_url` == daemon URL and non-empty `agent_id`/`credential`), missing bearer; `POST /api/coordination/context` `{agent_id, nonce, read_only: true}` with the bearer, no redirects, no proxies; 200 field check, version-2 binding mismatch, version not None/2, HMAC-SHA256 nonce proof over Python's ASCII compact JSON, non-ASCII proof; 401, 404/405, 400/403 error-code map, >=500, other statuses; transport errors; `ok` false unless authenticated | :446-503, :648-653, :658-659, coordination_identity.py:95-118, private_state.py:36-75 |
| Coordination snapshot fields, order and `next` texts (registration-dependent branches included) | :505-541, :654-657 |
| GitBashMissing and MaintainerPasskeysInvalid finalization | :660-674 |
| `path_resolution`: CPython 3.11 `shutil.which("pseudolife-mcp")` (cwd first and PATHEXT on Windows), launcher from `PSEUDOLIFE_SHIM_RUNTIMES`/`_LAUNCHER` or the default layout, realpath/normcase comparison, warning text | :690-707, runtimes.py:127-154 |
| Request bytes per probe, every header field compared: the GETs carry urllib's fields exactly (`Host` as written, `User-Agent: Python-urllib/3.11`, `Accept-Encoding: identity`, `Connection: close`, an optional `Authorization`, no `Accept`; sent through `cli::hook_http`, which also gives urllib's per-receive timeouts and redirect limits); the context POST carries httpx's (`Host`, `Accept: */*`, `Accept-Encoding: gzip, deflate`, `Connection: keep-alive`, `User-Agent: python-httpx/0.28.1`, `Authorization`, `Content-Type`/`Content-Length`) and its body (compact JSON, nonce tokenized) | board_status.py:51-55, :390-395, shim.py:438, :470-473 |
| Body reads stop where Python's do: board `read(65536)` on success and no read on an error status; maintainer `read(1 << 20)` / `exc.read(65536)`; health reads the whole body. A bounded `read(amt)` of a length-delimited body cut short returns what arrived, none included (`HTTPResponse.read`, http/client.py 3.11); reading to the end, or a chunked body, fails as `IncompleteRead` does. A failed board or maintainer success read is the probes' `except Exception` (unreachable); a failed health read is `None` | board_status.py:55, :393-397, shim.py:438-447 |
| The context POST's answer is decoded as httpx decodes it: `Content-Encoding` codings split on commas, `gzip` and `deflate` (zlib, else raw deflate) applied in reverse, `identity` and unknown codings left alone, a truncated stream yielding what decoded, a corrupt one `unavailable` (DecodingError) | httpx `_decoders.py` |
| The bearer is snapshotted afresh for the maintainer probe and the `--agent-state` check, as each builds a new provider; a failed re-read is `not checked - no usable credential or URL` / `unavailable` | :432-443, :648-653 |
| `json.loads` refusals Python makes and serde would not: integer literals over 4300 digits (CPython 3.11 `int_max_str_digits`), a leading U+FEFF; the context answer's RecursionError past nesting depth 978 (measured on CPython 3.11.9 against the oracle doctor) is `unavailable` | :476-503 |
| No incidental mutation: doctor writes nothing; the only file a run leaves is the handshake shim's own `~/.pseudolife-mcp/handshake-cache/<sha256(url)[:16]>.json` (CLI-SHIM's file, written by the Python shim too) | shim.py:708-725 |

## Free items

- The interpreter identity values (declared substitution below).

## Harness rules and comparison scope

- Requests are compared in full except traffic positively tagged as the
  handshake shim's: `/mcp` (any method), `/api/episode/*`, and every
  `GET /health` after doctor's single one (doctor_cli.py probes health once,
  :601, before it starts the shim). That traffic is CLI-SHIM's contract and is
  summarized as one entry naming the JSON-RPC methods it proxied; any other
  request a doctor makes is compared request by request.
- `doctor-runtime-identity`: see below. Each observation carries the arm
  that produced it (`after` hook), and the rule tokenizes only when that
  arm's own identity is the one reported.
- `doctor-context-nonce`: the context request's `uuid4().hex` nonce, only
  that span of that body.
- `shim-handshake-cache-semantic`: temporary, below.

## Declared substitutions and divergences

- `doctor-runtime-identity` (harness rule; substitution accepted, maintainer
  decision 2026-10-09): `interpreter`, `source` and `pseudolife-mcp`
  describe the running runtime. The oracle reports its Python interpreter,
  package directory and installed distribution version; the native doctor
  reports its own executable, the directory it resolves into and its Cargo
  version. Python's `mcp` key (its MCP SDK version) has no native
  counterpart and the native report omits it entirely, by the same decision:
  after cutover a placeholder such as `not installed` would read as a broken
  install. The rule checks that each arm reports its OWN identity (the
  native arm its executable, resolved directory, Cargo version and no `mcp`
  key; the oracle arm an existing Python interpreter, its package directory
  and a real version string in `mcp`), deletes the oracle's `mcp` line (its
  line break and trailing comma, exactly once), then tokenizes the three
  shared fields and that arm's own version where the report or the shim's
  mismatch warning repeats it. Every other byte, key order included, must
  equal Python's. A native report carrying Python's identity stays a
  difference (mutant `doctor-identity-claims-python`).
  `tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes`
  asserts `interpreter == sys.executable` and can never hold for a native
  runtime.
- `shim-handshake-cache-semantic` (harness rule, inherited from CLI-SHIM,
  temporary): the Python shim writes the handshake cache with `json.dumps`
  (spaced separators, ASCII escapes, `url` first); the native shim's
  `cache.rs` writes compact UTF-8 with `url` last. Compared as parsed JSON,
  that path only. The connect leaf's cache.rs fix writes Python's bytes; the
  rule goes once it merges.
- Timeouts are observable: a small `--timeout` (or the 2-second probe cap)
  races startup and response speed differently in each runtime, and the
  native doctor bounds connect and each read the way sockets do, not
  urllib's exact sequence of socket operations. Cases use budgets far from
  either arm's latency; `--timeout` over 1,000,000 seconds defers (Python
  accepts any positive float).
- Proxies: urllib honours `http_proxy`/`https_proxy` and, without them, the
  Windows registry (WinINet) and macOS `_scproxy` settings. The native probes
  connect directly; either variable defers, the platform settings are not
  consulted.
- https daemons: TLS trust is the native shim's reqwest/rustls
  configuration, not CPython's default `ssl` context.
- Closed stdout: the oracle's print raises and CPython exits 120 after a
  shutdown flush trailer; the native doctor exits 1.
- Context answers after the handshake cannot defer: a 200 or 400/403
  `/api/coordination/context` body that only Python's decoder admits
  (UTF-16/32, encoded surrogates, NaN or Infinity) is answered as a JSON
  failure (`unsupported_capability`); a malformed body whose nesting passes
  978 before its first error is answered by the error, not RecursionError.
  The daemon's `_send_json` never produces one.
- The nesting threshold 978 is the oracle's recursion limit (1000) less the
  frames above the decode in doctor's call path, measured on CPython 3.11.9
  on Windows; another interpreter build can move it by a few levels. On
  Linux (CPython 3.11.15 under WSL, 2026-10-09) the oracle parses depth 978,
  so the threshold differs there and is not measured: the three threshold
  cases and their mutant run on Windows only. No daemon answer nests this
  deep.
- The context POST advertises `gzip, deflate` because the oracle
  environments (Windows CPython 3.11, the WSL venv) have httpx 0.28.1 and
  none of brotli, brotlicffi or zstandard; with one of those installed httpx
  also advertises and decodes `br` or `zstd`, which the native doctor
  neither sends nor decodes.
- A maintainer error status whose body fails to read (a timeout, a reset, a
  truncated chunked body) defers after the read-only GETs: Python's
  `exc.read(65536)` runs inside `except HTTPError` (doctor_cli.py:396), so
  the failure escapes and doctor ends in a traceback with exit 1. This is
  declared residue, as on the other leaves, not an answer.
- `python-httpx/0.28.1` is the oracle environment's httpx; another httpx
  version sends another agent string.
- Missing Git for Windows (`GitBashMissing`) is not reachable through the
  harness on a host with Git in its default directory: doctor_cli.py
  hard-codes `C:\Program Files\Git\bin\bash.exe` and the x86 variant with
  no environment override. A Rust unit test pins the finalization instead.

## Deferral domain (before the first request unless noted)

nonempty-DSN `--disposable-proof`; noncanonical argv; `--timeout` past
1,000,000 seconds; an `--agent-state` path with a `..` component, a
symlinked or junction ancestor of an existing file, or a stat error pathlib
would raise (a missing file is `missing_registration` whatever its ancestors:
`exists()` comes first, doctor_cli.py:458); a nonempty `http_proxy` or
`https_proxy` (any case); `PYTHONINTMAXSTRDIGITS` set; an invalid daemon
URL; a credential provider error (missing, unsafe or malformed token file,
invalid static token); a bearer outside printable ASCII (urllib would send
Latin-1, but the handshake's Python shim and the `--agent-state` check use
httpx, which cannot); saved tunnels present (`~/.pseudolife-mcp/tunnel`);
JSON files only Python may read (NaN, Infinity, `\u` escapes, nesting past
the parser's limit) and every TOML parse failure other than a leading BOM;
non-UTF-8 environment values read; settings values whose `str()` needs
Python's repr (floats, non-empty containers); `~` in
`CODEX_HOME`/`PSEUDOLIFE_CODEX_BIN`; UNC, device and drive-relative paths;
PATH absent on POSIX (`os.confstr`); stat errors pathlib would raise.
After a read-only GET (board, maintainer or health) but always before the
handshake starts the shim: a non-object health body, a
non-string `status`, a truthy non-string `version`, a `hooks_digest` with a
Codex plugin configured (digest comparison not ported), a health redirect
to `ftp:` or to a target that is not text (urllib would follow it), a
maintainer error body that fails to read (Python crashes there), a
duplicate or non-ASCII `X-PL-Board` header, maintainer `rp_id`/`origin`
that are not strings or null, response bodies only Python's decoder
admits.
