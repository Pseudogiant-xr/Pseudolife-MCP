# CLI-EPISODE spec: `episode-start`, `episode-end`

Python source: `pseudolife_memory/episode_cli.py`, `session_title.py:87-109`
(`git_project_name`, `title_from_cwd`), `shim.py:436-451` (`probe_health`),
daemon side `web/routes.py:161-165` and `service.py` `episode_start_session` /
`episode_end_session`. Native: `rust/shim/src/cli/episode.rs`,
`episode_input.rs`, transport `hook_http.rs`.
Harness cases: `rust/cli_harness/rows/episode.py`.

## Canonical inputs

The mode-only legacy hook commands `pseudolife-mcp episode-start` and
`episode-end` (written by earlier installers; `ops/install-hook.ps1:79-81,
430-435` and `ops/install-hook.sh:117-123,372-374` remove them as obsolete),
with the host's SessionStart/SessionEnd JSON on stdin: a string `session_id`
and a string, null or absent `cwd` (PARITY "Episode hook producer grammar").
Trailing argv is ignored. Environment: `PSEUDOLIFE_MCP_DAEMON_URL`,
`PSEUDOLIFE_MCP_TOKEN`.

## Exact contract items

| Item | Python source |
|---|---|
| No session id (missing, empty, unparsable or non-object stdin): no request, exit 0, silent | `episode_cli.py:28-40,64-67` |
| Health probe first; down, non-JSON or `null` health: no POST; degraded JSON 503 proceeds | `:69-71`, `shim.py:436-451` |
| Start body `{"session_key": K, "title": T}`, end body `{"session_key": K}`, `json.dumps` spacing and ASCII escaping | `:74-81` |
| Title: git repo root basename walking up from cwd, else cwd basename unless home or a system dir, else `session`; then ` - YYYY-MM-DD HH:MM` local | `session_title.py:87-109` |
| POST through a no-redirect opener with `Content-Type: application/json`, bearer from `PSEUDOLIFE_MCP_TOKEN`; any failure silent, exit 0 | `:43-52,82-83` |
| Transport: urllib's connection order, per-receive timeouts (0.25 s health, 5 s POST), request fields and health redirect limits | `hook_http.rs` (see CLI-HOOK) |
| Daemon effect: start opens (or returns, or reopens) the keyed root episode and records the client session; end closes it, deleting an empty episode, and stamps the client session's end | `service.py:6365-6467` |

## Free items (named rules)

- `episode-title-minute`: the title's minute inside each arm's own window, on
  the clock of the host that ran it.
- Bank rows (`normalize.episode_rows`): uuid4-hex episode ids by first-seen
  symbol, wall-clock seconds inside the run, title minutes inside the run.

## Declared divergences

- `http-forbidden-input-refused` (PORTING): a bearer with C0/DEL bytes refuses
  with exit 1 and the named diagnostic; no shipped producer writes such a token.
- Source-vote session titles are out of scope until their producer lands on
  master (delegate ruling).
- Proxy settings are not consulted (CLI-HOOK transport deferral).
- The bank cases need live oracle daemons and run locally only; CI runs the
  wire cases live and against goldens.
