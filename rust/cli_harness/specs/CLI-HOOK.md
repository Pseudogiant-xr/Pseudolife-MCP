# CLI-HOOK spec: `briefing`, `prompt-hook`, `doorbell-prompt-seen`

Python source: `pseudolife_memory/briefing_cli.py` (dispatch `cli.py`),
`shim.py:436-451` (`probe_health`), `daemon_url.py:25-55`,
`runtimes.py:246-263` (`launcher_command`), `codex_doorbell_state.py:82-369`.
Native: `rust/shim/src/cli/briefing_hook.rs`, `hook_json.rs`,
`doorbell_seen.rs`, `doorbell_files.rs`, `doorbell_json.rs`.
Harness cases: `rust/cli_harness/rows/hook.py`. Episode start/end moved to the
separate CLI-EPISODE row.

## Canonical inputs

- `briefing --hook-json` and `briefing --hook-json --coordination`
  (`ops/install-hook.ps1:26,311`, `ops/install-hook.sh:51,83-84`);
  `briefing`, `--max-unsure N`, `--max-lessons N`, `--max-world N` and
  `briefing --coordination` (docs/guide/episodes.md, configuration.md);
  `--help`. Environment: `PSEUDOLIFE_MCP_DAEMON_URL`, `PSEUDOLIFE_MCP_TOKEN`,
  `PSEUDOLIFE_AGENT_COORDINATION`, the shim launcher layout.
- `prompt-hook` with the host's UserPromptSubmit JSON on stdin
  (`ops/install-hook.*`); `PSEUDOLIFE_DIGEST_DIR`, token or token file.
- `doorbell-prompt-seen` with the Codex UserPromptSubmit JSON on stdin
  (`plugin/hooks/coordination-prompt.sh:28-30`, `lifecycle.ps1:346`), against
  a pending record written by `PendingNotice.reserve` (or a legacy record).
Daemon replies come from a fixture serving the real routes' shapes (`/health`,
`/api/briefing`, `/api/hook/session-start`, `/api/hook/coordination-start`,
`/api/hook/memory-changes`); both arms receive identical bytes.

## Exact contract items

| Item | Python source |
|---|---|
| Help at COLUMNS=80 | `briefing_cli.py:107-119` |
| Quiet exit 0 when the daemon is down, `/health` is non-JSON or `null`; degraded JSON 503 still proceeds | `:128-130`, `shim.py:436-451` |
| Route per flags: `/api/briefing?max_unsure&max_lessons&max_world`, `/api/hook/session-start[?launcher=]`, `/api/hook/coordination-start`; bearer only on the payload request; redirects refused | `:41-101,128-140` |
| `?launcher=` only when the shim launcher exists and PATH does not resolve to it; quoted path | `:59-71`, `runtimes.py:246-263` |
| Coordination opt-out: unset or yes-like proceeds, anything else prints nothing | `:122-127` |
| Output: markdown stripped, plain + newline, or the SessionStart hook JSON (ensure_ascii); nothing for empty, null or missing markdown, non-2xx or failed fetch | `:30-38,139-147` |
| Invalid daemon URL: the `[shim] invalid PSEUDOLIFE_MCP_DAEMON_URL` line, exit 1 | `daemon_url.py:30-55` |
| Connection order: each resolved address in turn with its own timeout (a `localhost` URL reaches an IPv4-only daemon inside the 0.25 s probe) | urllib `socket.create_connection` |
| Timeouts bound each receive and send, not the whole reply: a reply trickled inside the timeout completes, one that stalls past it fails; the same over https | `socket.settimeout` |
| Health redirects: a URL at most four times, at most ten distinct targets | `HTTPRedirectHandler.max_repeats/max_redirections` |
| https origins verified against the system trust store or `SSL_CERT_FILE`, no revocation lookup; one handshake deadline; each TLS read yields plaintext within the timeout; ragged EOF reads as EOF | `ssl.create_default_context`, `_ssl.c` deadlines, `suppress_ragged_eofs` |
| Payload requests rejected at a non-2xx head, body unread | `HTTPErrorProcessor` |
| prompt-hook: session id `[A-Za-z0-9._-]{1,128}`; mark `<digest dir>/<sha256>.mark`, first line as `since` when it is `[0-9.]{1,22}`; one GET with session_id and since; cursor line validated before any write | `:153-209` |
| prompt-hook output: the UserPromptSubmit hook JSON only for a non-empty note; mark rewritten as `cursor\n`; marks older than 30 days pruned on a session's first note | `:210-234` |
| prompt-hook silence: exit 0 with no output on invalid input, transport failure or unwritable stdout | `:237-251` |
| doorbell: canonical thread UUID; exact prompt equal to the pending notice text writes `<key>.bell-prompt-seen` = nonce + LF under the `.bell-lock`; otherwise no receipt; legacy record migrates in place | `codex_doorbell_state.py:130-172,340-369` |

## Free items (named rules)

- Request header field names associate case-insensitively (HTTP semantics);
  every field's presence and value is compared exactly, and a repeated name
  always differs (`normalize.wire`).
- `doorbell-legacy-first-seen`: the migrated record's `legacy_first_seen`
  inside the arm's window and `expires_at` exactly 86400 s later.
- `python-shutdown-flush-silent`: CPython's shutdown flush trailer and exit 120
  after prompt-hook stayed silent on an unwritable stdout (declared
  `hook-native-output-failure`).

## Declared divergences (not exercised by canonical cases)

Declared divergences: on Windows a set `SSL_CERT_FILE` replaces the system
store where CPython unions the two; proxy settings (`HTTP(S)_PROXY`,
`NO_PROXY`, the Windows registry) are not consulted (deferred by ruling: no
shipped producer sets a proxy for the daemon URL); non-canonical redirect
shapes (3xx without Location, a redirect-limit reply with a JSON body, the
`URI` header, ftp targets) and 1xx replies other than 100 are not emulated.

The PORTING.md hook substitutions stand: `hook-strict-json-refusal`,
`hook-bounded-json-nesting`, `hook-typed-markdown`, `hook-ascii-numeric`,
`hook-first-line-cursor`, `hook-native-output-failure` (briefing's own output
failure prints `pseudolife-mcp briefing: output failed`, exit 1, where Python
raises), `http-forbidden-input-refused`, and the four doorbell producer domains
in PARITY.md. Not covered by the harness: Windows symlink parents (capability), POSIX distinct real/effective UID and
empty HOME, non-UTF-8 locale output encoding (the Python arm runs with UTF-8
streams), and argparse abbreviations and other non-canonical argv.
