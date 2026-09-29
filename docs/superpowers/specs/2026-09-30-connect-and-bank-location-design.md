# Connecting a machine to a bank — `pseudolife-mcp connect` and the installer's first question

**Date:** 2026-09-30 · **Status:** approved (decisions below, 2026-09-30) · **Part 1 of 3**
(part 2: `expose` and `invite`/pairing; part 3: `move`)

## Problem

When a bank's daemon changes address, every client on every machine has to be
pointed at the new one, and today nothing does that. Each registrar fills in
what is missing and leaves an existing daemon URL alone, so a user whose
daemon moved re-runs the installer, reads "existing registration unchanged"
or "Edit it in place", and edits four or five files by hand.

The maintainer's own move of a live bank from a Windows host to a Linux
container on 2026-09-29 is the worked example. After the target daemon was
installed, restored and exposed, re-pointing the source machine's clients
took:

- a hand edit of the `pseudolife-memory` env block in `~/.claude.json`
  (the client-only installer only warns and carries on: `ops/install.sh`
  2335-2338, `ops/install.ps1` 2265);
- a hand edit of `[mcp_servers.pseudolife-memory.env]` in Codex's
  `config.toml`, after deleting `~/.codex/pseudolife/connection.json`
  and re-running the Codex credential setup, which refuses an installer URL
  that differs from the configured server's (`setup-codex-hooks.py`
  666-670), while the plugin hooks refuse a `connection.json` that disagrees
  with the URL they are given;
- the Claude Desktop entry (the one registrar that replaces values,
  `ops/register_claude_desktop.py` `merge_config`);
- the `env` block of `~/.claude/settings.json`, which the plugin hooks read.

Each file has its own format and its own writer, and the only way to know
that all of them had moved was a live check from every client.
The installer also has no way to ask where the bank should live: whether a
run is a local install or a client of a remote bank is decided by whether
`--daemon-url` names a non-loopback host, before the banner, and a user who
does not know that flag gets a local bank.

## Goals

1. One command re-points every client registration on a machine at a daemon
   URL and token file, replacing old values, and proves the result works.
2. Its effect is safe to repeat, visible before it happens (`--dry-run`),
   reversible (a backup of every file it writes) and all-or-nothing across
   files.
3. The installer asks where the bank lives as its first question, and its
   client-only path uses the same command, so installing and re-pointing are
   one mechanism.
4. It works on every install type: a checkout, a release install (PyPI or
   the GitHub release) with no `ops/` directory, Windows, macOS and Linux.

Out of scope for part 1: creating principals or tokens on the daemon host,
admitting them to the board, exposing the daemon (part 2), and moving the
bank's data (part 3). `connect` takes a daemon that already accepts the
caller's token.

## `pseudolife-mcp connect`

```
pseudolife-mcp connect <daemon-url>
    [--token-file PATH [--read-token]]
    [--client claude-code,codex,claude-desktop,gemini | --client all]
    [--dry-run] [--yes] [--json]
```

- `<daemon-url>` is an origin (`http(s)://host[:port]`), checked by the same
  rule the shim applies (`daemon_url`), but reported as a usage error with
  its own message (exit 2), not the shim's `SystemExit(1)`. Loopback is
  allowed, which is how a machine moves back to a local daemon.
- **Token.** With `--token-file`, that file is used for every selected
  client. `--read-token` (no argument, used with `--token-file`, as in the
  installer) reads a token without echo and creates that file owner-only,
  refusing an existing file (`client_credentials.write_token_file`). With
  neither, each registration keeps the credential it already names. That is
  the common case: the bank was restored with its tokens, and only the
  address changed. Because one `--token-file` applies to every selected
  client, the guide recommends one run per client
  (`--client codex --token-file <codex token>`) to keep one principal per
  client.
- Client names are those of `runtimes.Registration.client`; `--client`
  narrows the set, and the default is every client found.
- **`connect` never creates a registration.** A client with none is reported
  `absent`, with the installer command that registers it. Creating
  registrations stays with the installer's registrars.
- Interactive runs show the plan and ask once; `--yes` skips the question,
  and a non-TTY run without `--yes` prints the plan and exits 2.

### Phase 1: discover and plan (no credentials sent)

1. An unauthenticated health probe with the remote timeout the shim uses
   (`shim._REMOTE_PROBE_TIMEOUT_S`, 2 s; `probe_health`'s own default is
   0.25 s) must report `status: ok`. For a non-loopback URL, `auth: false` is
   refused (the installer's client-only preflight rule), and plain `http://`
   gets one warning line, as in the installer.
2. Discovery extends `runtimes.find_registrations`, which already finds the
   Claude Code, Codex, Claude Desktop (every `desktop_config_files` path) and
   Gemini stdio registrations. It skips `type: http` entries today, so a
   second pass surfaces those as `manual`. The plan also covers the copies of
   the URL and token that the registrations do not hold:

| Place | Key(s) | How it is written |
|---|---|---|
| `~/.claude.json` `mcpServers.pseudolife-memory.env` | `PSEUDOLIFE_MCP_DAEMON_URL`, `PSEUDOLIFE_MCP_TOKEN_FILE`, `PSEUDOLIFE_MCP_NO_SPAWN` | JSON edit of those keys only (`_replace_config`) |
| `~/.claude/settings.json` `env` | `PSEUDOLIFE_MCP_DAEMON_URL`, and `PSEUDOLIFE_MCP_TOKEN_FILE` when the block holds no credential yet | JSON edit, only when a Claude Code registration exists; read by the plugin hooks |
| Codex `config.toml` `[mcp_servers.pseudolife-memory.env]` | as for Claude Code | Codex's app-server config writer (`config/batchWrite`) |
| `~/.codex/pseudolife/connection.json`, and the token copy `~/.codex/pseudolife/token` where the writer keeps one | URL and token-file path | the Codex credential writer |
| Claude Desktop `claude_desktop_config.json` `pseudolife-desktop` | as for Claude Code | JSON edit of those keys only |
| `~/.gemini/settings.json` `mcpServers.pseudolife-memory.env` | as for Claude Code | JSON edit of those keys only |
| unattended-update task or unit, if scheduled | `--daemon-url`, `PSEUDOLIFE_MCP_DAEMON_URL`, and on Linux the unit's `PSEUDOLIFE_MCP_TOKEN_FILE` | reported only, with the command that re-schedules it |
| ambient `PSEUDOLIFE_MCP_DAEMON_URL` (the process environment; on Windows also the User-scope variable) | the URL | reported only, when it differs from the target |

Each row is reported as `current` (already names the target), `change`
(old → new), `manual` (found but not safely writable, with the reason) or
`absent`. Tokens are never printed; a token file is shown by path.

Rules the plan applies:

- **`NO_SPAWN`.** A non-loopback target always gets
  `PSEUDOLIFE_MCP_NO_SPAWN=1`. A loopback target keeps the registration's
  existing value: whether a local shim may start a daemon is the install's
  choice (the `spawning` state in `migrate_registrations`), not `connect`'s.
- **Only those keys change.** Everything else in an entry (the command,
  `args`, `PSEUDOLIFE_WRITER_ID`, `PSEUDOLIFE_AGENT_STATE_DIR`, Codex
  timeouts, user-added env) is left as is. That is why the Desktop entry is
  not written through `register_claude_desktop.merge_config`, which rewrites
  `PSEUDOLIFE_WRITER_ID` and `NO_SPAWN`, sets the command and drops `args`.
  A literal `PSEUDOLIFE_MCP_TOKEN` in a registration is replaced by the
  token file when one is given, and otherwise kept with a warning.
- **`manual` rows.** A `type: http` entry; a foreign `pseudolife-memory`
  entry in Desktop, or a legacy one this project's registrar wrote (the
  installer's registrar migrates it); a Claude Code entry under
  `projects[*].mcpServers` or in a `.mcp.json` of the current directory
  (project-scoped files, including a project's `.claude/settings*.json`,
  are not read or rewritten in part 1); a registration with a
  fixed `PSEUDOLIFE_AGENT_STATE` file, which is not keyed by URL and which
  the adapter refuses for a different bank; for Codex, a missing `codex`
  executable (the config writer needs its app-server) or credentials that
  come from another Codex configuration layer.
- **The command is not changed.** A registration still on an old command is
  a job for `ops/shim_runtime.py migrate`; `connect` names it.

A failed health probe, or `auth: false` on a remote URL, exits 4 before
anything is shown or written, with or without `--dry-run`. `--dry-run`
stops after the plan. It sends no credential and says so, and exits 3 if
nothing was found and 0 otherwise.

### Phase 2: confirm, then verify

No authenticated request is sent before the user confirms the plan (or
passes `--yes`), so a mistyped host never receives a bearer. After
confirmation:

1. With `--read-token`, the token file is created now. It is removed again if
   verification fails.
2. Every distinct credential that will be pointed at the target is checked:
   each token file (`check_token_file`: owner-only, no BOM) and each literal
   `PSEUDOLIFE_MCP_TOKEN` a registration carries. Each gets an authenticated
   request that follows no redirects (the logic of
   `installer_credential_valid`, which takes a token, so files are read
   first).
3. An MCP handshake with the new URL and token file: the check
   `doctor_cli._handshake` makes, run with an explicit subprocess environment
   (it reads `os.environ` and takes no arguments), with `PSEUDOLIFE_MCP_TOKEN`
   removed so that it cannot override the file, and with the shim's board
   check-in off, so that verification leaves no agent address on the target's
   board. `run_doctor` is not called here: its `registration_credentials`
   would read the old registrations.
4. `board_status(url, token)` is reported, not required: a principal the
   board does not admit is a warning naming
   `coordination.allowed_principals`, because memory works without the board.

A failure here exits 4 and writes nothing.

### Phase 3: apply

- Every file is backed up before its first write, using the backup naming of
  the writer that owns that file, then written and read back.
- **All-or-nothing.** If any write or read-back fails, every file already
  written in this run is put back, and the run exits 1 naming the file that
  failed. A file is restored from its backup only if it still holds exactly
  what `connect` wrote. `~/.claude.json` is live state that running sessions
  rewrite, so a file that changed underneath is left as it is now, with its
  backup kept and reported (the stance `runtimes.migrate_registration`
  already takes). A file `connect` created is removed.
- **Codex** is written through the same writer the installer uses, in a new
  replace mode. Today that writer refuses an installer URL that differs from
  the configured server's when the Codex env carries no token file or literal
  token (`setup-codex-hooks.py` 666-670). As that writer already does, a
  literal `PSEUDOLIFE_MCP_TOKEN` in the Codex env moves into Codex's token
  copy. In replace mode it writes the Codex
  server env, then `connection.json`, then the token copy where it keeps one
  (backed up first), so that the plugin hooks, which compare the two and
  refuse a mismatch (`plugin/hooks/session-start.sh` 111-125,
  `lifecycle.ps1`), see a consistent pair.
- **Board identity.** Agent state under `PSEUDOLIFE_AGENT_STATE_DIR` is keyed
  by the daemon URL (`coordination_identity.bound_state_path`), so each such
  client gets a fresh board address on the new URL. The old state directory
  is left in place, and the report says the session's peers will see it
  under a new address.

### Phase 4: report

- One line per row of the plan, with backups.
- The sessions that need a restart: the processes running the current shim
  runtime (the check `ops/update_clients.py` already makes before replacing
  it), plus "restart Claude Desktop" when its entry changed.
- A final check: discovery runs again and every written place must name the
  target, and the target must still answer `/health`. `doctor` itself is not
  run here, because its handshake would check in on the target's board; the
  report suggests running it from a client's environment.

Exit codes: 0 done or already current; 1 a write failed and was rolled back
as above; 2 usage, an invalid URL, a declined confirmation, or no `--yes`
on a non-TTY run; 3 no
registrations found; 4 verification refused, nothing written; 5 applied, but
the final check failed (the report says what to do and lists the backups).

### Where the code lives

`connect` must run from a release install, which has no `ops/` directory.
Today the Codex credential writer (`ops/setup-codex-hooks.py`
`configure_credential_file` and the app-server config writer) and the token
file helpers (`ops/client_credentials.py`) exist only in a checkout. Part 1
moves the pieces `connect` needs into the package, and the `ops/` scripts
import them from the checkout, the way `ops/client_credentials.py` already
imports `pseudolife_memory.credentials` (it puts the repository root on
`sys.path` first), so the installer can keep running them with an
interpreter that has no package installed:

- `pseudolife_memory/connect_cli.py`: argument parsing, the phases, the
  report. Registered as one `elif mode == "connect"` branch in
  `pseudolife_memory/cli.py` plus a `_USAGE` line.
- `pseudolife_memory/client_config.py`: the token-file helpers
  (`write_token_file`, `check_token_file`) and the JSON env edits, moved out
  of `ops/client_credentials.py`.
- `pseudolife_memory/codex_connection.py`: `connection.json`, the token copy
  and the Codex config write, moved out of `ops/setup-codex-hooks.py`, with
  the new replace mode.

`ops/register_claude_desktop.py` stays as it is: `connect` edits the Desktop
entry's keys directly and finds its file with `runtimes.desktop_config_files`.

All of it stays standard library only: these are client modes, so they join
`_CLIENT_ENTRY_MODULES` in `tests/test_shim_runtimes.py`, and the import-
closure test proves the shim runtime can import them.

## The installer's first question

After the banner and before the coding-agent selection, an interactive run
asks:

```
Where does the memory bank live?
  1) On this machine (default)
  2) On another machine that already runs it
  3) On this machine, and other machines will connect to it
```

- **1** is today's local install, unchanged.
- **2** asks for the daemon URL, then the token: an existing token file path,
  or paste a token (the `--read-token` path, which creates a new owner-only
  file). It then runs the client-only install: preflight, shim runtime,
  plugin and hooks. The per-client registrars still create any registration
  that does not exist yet, and **`pseudolife-mcp connect` re-points the ones
  that do**, so an existing registration that names an old daemon is
  re-pointed rather than warned about.
- **3** is a local install that ends by printing the exposure steps from
  `docs/guide/remote-bank.md` (Tailscale Serve first) and the per-machine
  principal steps. Part 2 replaces the printed steps with `expose` and
  `invite`.

Non-interactive behaviour does not change: `--daemon-url` naming a
non-loopback host still means client-only, and a run with no TTY and no
`--daemon-url` is still a local install. No new flag is needed; the question
is the interactive form of `--daemon-url`.

The mode is currently resolved before the banner (`ops/install.sh` 319-384,
`ops/install.ps1` 296-345). The question moves that resolution after the
banner; the validation and the `CLIENT_ONLY` rejection of extractor, model,
port and `--no-token` flags stay exactly as they are and run on the answer.

On a machine with existing registrations, the client-only path shows
`connect`'s plan in the installer's own summary and then runs it with
`--yes`, so the installer asks nothing twice. It runs `connect` before any
registrar or the Codex credential setup: once the existing registrations
name the new daemon, the Codex setup no longer refuses its URL
(`setup-codex-hooks.py` 666-670), and the registrars only create what is
still missing. If `connect` exits non-zero the installer stops there. With
no existing registration it does not run `connect` at all, so `connect`'s
exit 3 never fails an install.

## Updating users

- A user whose daemon moved runs `pseudolife-mcp connect <new-url>` and
  restarts their sessions. `docs/guide/remote-bank.md` gains a "Moving a
  client to a new daemon" section with that one command, and its "By hand"
  section stays as the reference.
- `pseudolife-mcp update` is unchanged.
- Rolling back is `connect` with the old URL, or restoring the backups it
  names.

## Testing

TDD with watched REDs, stdlib filesystem fakes for every config path (the
pattern `tests/` already uses for `register_claude_desktop.py`), and a small
in-process HTTP stub for the daemon.

- Each client row: `absent`, `current`, `change`, `manual`; only the URL,
  token-file and `NO_SPAWN` keys replaced and nothing else touched (Desktop
  included); a literal token replaced when a file is given; nothing ever
  created for an `absent` client.
- `NO_SPAWN`: set for a remote target, preserved for a loopback one.
- Verify-before-write: an unreachable daemon, `auth: false` on a remote URL,
  a rejected token (file or literal), and a failed handshake each exit 4 with
  every file byte-identical; no authenticated request is sent before
  confirmation or with `--dry-run`; a `--read-token` file is removed when
  verification fails.
- All-or-nothing: a write failure injected on the third file restores the
  first two; a read-back mismatch counts as a failure; a file changed by
  someone else after `connect` wrote it is left alone with its backup
  reported; a created file is removed.
- Idempotence: a second run reports every row `current` and writes nothing.
- `--dry-run` writes nothing; a non-TTY run without `--yes` exits 2.
- Codex: the replace mode rewrites the server env, `connection.json` and the
  token copy; the plugin hooks' Codex path (`plugin/hooks/session-start.sh`,
  `lifecycle.ps1`) then reads the new URL.
- `manual` rows are reported, never written: project-scoped Claude Code
  entries, `type: http` entries, a legacy or foreign Desktop entry, a fixed
  `PSEUDOLIFE_AGENT_STATE`, and Codex without its executable or with
  credentials from another layer. A differing ambient
  `PSEUDOLIFE_MCP_DAEMON_URL` and an unattended-update schedule are reported.
- The installer: the question's three answers, non-interactive behaviour
  unchanged (existing tests in `tests/test_client_install_ux.py` stay green),
  and the client-only path calls `connect` for existing registrations rather
  than warning, and only then.
- Import closure: the new modules import under the shim runtime's
  requirements.
- Windows: token-file ownership, no BOM, and the MSIX Desktop config path.

## Decisions (maintainer-approved, 2026-09-30)

1. The Codex credential writer and the token-file helpers move into the
   package, rather than making `connect` checkout-only: `connect` is most
   needed on client-only machines, which are typically release installs.
2. All-or-nothing across files, rather than best-effort with a report.
   Half-moved clients are the failure the 2026-09-29 move had to rule out
   client by client.
3. Project-scoped Claude Code entries reported, not rewritten, in part 1.
4. Option 3 of the installer question ships in part 1 as printed steps.
