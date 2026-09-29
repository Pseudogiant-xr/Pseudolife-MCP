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
  (the client-only installer warns and stops: `ops/install.sh` 2335-2338,
  `ops/install.ps1` 2265);
- a hand edit of `[mcp_servers.pseudolife-memory.env]` in Codex's
  `config.toml`, after deleting `~/.codex/pseudolife/connection.json`
  so that `ops/setup-codex-hooks.py` would accept a new URL (it refuses a
  URL that differs from the configured server's, `setup-codex-hooks.py`
  around 666);
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
    [--token-file PATH | --read-token PATH]
    [--client claude-code,codex,desktop,gemini | --client all]
    [--dry-run] [--yes] [--json]
```

- `<daemon-url>` is an origin (`http(s)://host[:port]`), validated by the
  shim's own rule (`daemon_url._validated_daemon_url`). Loopback is allowed,
  which is how a machine moves back to a local daemon.
- **Token.** With `--token-file`, that file is used for the selected clients.
  `--read-token PATH` reads a token without echo and creates `PATH`
  owner-only, refusing an existing file, exactly as the installer does today
  (`client_credentials.write_token_file`). With neither, each registration
  keeps the token file it already names. That is the common case: the bank
  was restored with its tokens, and only the address changed.
- `--client` narrows the set; the default is every client found.
- Interactive runs show the plan and ask once; `--yes` skips the question,
  and a non-TTY run without `--yes` prints the plan and exits 2.

### Phase 1: verify before writing

Nothing is written until the target has proven it will accept these clients.
Reusing existing checks:

1. `probe_health(url)` (`shim.py`) reports `status: ok`. For a non-loopback
   URL, `auth: false` is refused (the installer's client-only preflight rule)
   and plain `http://` gets one warning line, as in the installer.
2. For each distinct token file: `check_token_file` (owner-only, no BOM), then
   an authenticated request that follows no redirects
   (`installer_credential_valid` in `ops/setup-codex-hooks.py`) must succeed.
3. The MCP handshake doctor runs (`doctor_cli._handshake`), with the new URL
   and token file in its environment, must list tools.
4. `board_status(url, token)` is reported, not required: a principal the
   board does not admit is a warning naming
   `coordination.allowed_principals`, because memory works without the board.

A failure here exits 4 and writes nothing.

### Phase 2: plan

Discovery extends `runtimes.find_registrations`, which already finds the
Claude Code, Codex, Claude Desktop (every `desktop_config_files` path) and
Gemini registrations. The plan also covers the copies of the URL and token
that the registrations do not hold:

| Place | Key(s) | Written by |
|---|---|---|
| `~/.claude.json` `mcpServers.pseudolife-memory.env` | `PSEUDOLIFE_MCP_DAEMON_URL`, `PSEUDOLIFE_MCP_TOKEN_FILE`, `PSEUDOLIFE_MCP_NO_SPAWN` | JSON edit (`_replace_config`) |
| `~/.claude/settings.json` `env` | `PSEUDOLIFE_MCP_DAEMON_URL`, `PSEUDOLIFE_MCP_TOKEN_FILE` | JSON edit; read by the plugin hooks |
| Codex `config.toml` `[mcp_servers.pseudolife-memory.env]` | as for Claude Code | Codex's app-server config writer (`config/batchWrite`) |
| `~/.codex/pseudolife/connection.json` + `~/.codex/pseudolife/token` | URL and token-file path; Codex's own token copy | the Codex credential writer |
| Claude Desktop `claude_desktop_config.json` `pseudolife-desktop` | managed env keys | `register_claude_desktop.merge_config` |
| `~/.gemini/settings.json` `mcpServers.pseudolife-memory.env` | as for Claude Code | JSON edit |
| unattended-update task or unit, if scheduled | `--daemon-url`, `PSEUDOLIFE_MCP_DAEMON_URL` | `unattended_update.schedule` re-run |

Each row is reported as `current` (already names the target), `change`
(old → new), `manual` (found but not safely writable, with the reason) or
`absent`. Tokens are never printed; a token file is shown by path.

Rules the plan applies:

- **`NO_SPAWN`.** A non-loopback target always gets
  `PSEUDOLIFE_MCP_NO_SPAWN=1`. A loopback target keeps the registration's
  existing value: whether a local shim may start a daemon is the install's
  choice (the `spawning` state in `migrate_registrations`), not `connect`'s.
- **Only managed keys change.** Everything else in an entry (the command,
  `PSEUDOLIFE_WRITER_ID`, `PSEUDOLIFE_AGENT_STATE_DIR`, Codex timeouts,
  user-added env) is left as is. A literal `PSEUDOLIFE_MCP_TOKEN` in a
  registration is replaced by the token file when one is given, and
  otherwise kept with a warning.
- **Claude Code project and local scopes.** `find_registrations` reads the
  top-level `mcpServers` only. A `pseudolife-memory` entry under
  `projects[*].mcpServers`, or in a `.mcp.json` of the current directory,
  is reported as `manual` with its location; part 1 does not rewrite
  project-scoped files.
- **Registrations that are not the shim** (a `type: http` entry, a foreign
  `pseudolife-memory` in Desktop) are `manual`.
- **The command is not changed.** A registration still on an old command is
  a job for `ops/shim_runtime.py migrate`; `connect` names it.

`--dry-run` stops here and exits 0.

### Phase 3: apply

- Every file is backed up (`<name>.bak-YYYYmmdd-HHMMSS`, `runtimes._backup`)
  before its first write, then written and read back.
- **All-or-nothing.** If any write or read-back fails, every file already
  written in this run is restored from its backup, and the run exits 1 naming
  the file that failed. The unattended-update re-schedule runs last, because
  it is the only step that is not a file restore.
- **Codex** is written through the same writer the installer uses, in a new
  replace mode. Today that writer rejects a URL that differs from the one
  already configured; with `replace=True` it writes the Codex server env,
  then `connection.json` and the token copy, in that order, with its existing
  backups and version checks.
- **Board identity.** Agent state is keyed by the daemon URL
  (`coordination_identity.bound_state_path`), so each client gets a fresh
  board address on the new URL. The old state directory is left in place;
  the report says the session's peers will see it under a new address.

### Phase 4: report

- One line per row of the plan, with backups.
- The sessions that need a restart: the processes running the current shim
  runtime (the check `ops/update_clients.py` already makes before replacing
  it), plus "restart Claude Desktop" when its entry changed.
- A final `doctor` summary for the new URL.

Exit codes: 0 done or already current; 1 a write failed and was rolled back;
2 usage, or no `--yes` on a non-TTY run; 3 no registrations found; 4
verification refused, nothing written.

### Where the code lives

`connect` must run from a release install, which has no `ops/` directory.
Today the Codex credential writer (`ops/setup-codex-hooks.py`
`configure_credential_file` and the app-server config writer) and the token
file helpers (`ops/client_credentials.py`) exist only in a checkout. Part 1
moves the pieces `connect` needs into the package, and the `ops/` scripts
import them from there:

- `pseudolife_memory/connect_cli.py`: argument parsing, the phases, the
  report. Registered as one `elif mode == "connect"` branch in
  `pseudolife_memory/cli.py` plus a `_USAGE` line.
- `pseudolife_memory/client_config.py`: the token-file helpers
  (`write_token_file`, `check_token_file`) and the JSON env edits, moved out
  of `ops/client_credentials.py`.
- `pseudolife_memory/codex_connection.py`: `connection.json`, the token copy
  and the Codex config write, moved out of `ops/setup-codex-hooks.py`, with
  the new replace mode.
- The Claude Desktop merge (`register_claude_desktop.merge_config` and the
  config path resolution already duplicated in `runtimes.desktop_config_files`)
  gets one home in the package.

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
  plugin, hooks, and **`pseudolife-mcp connect` instead of the per-client
  registrars**, so an existing registration that names an old daemon is
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

On a machine with existing registrations, the client-only path runs
`connect` with `--yes` after showing its plan in the installer's own summary,
so the installer asks nothing twice.

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

- Each client row: `absent`, `current`, `change`, `manual`; managed keys
  replaced and nothing else touched; a literal token replaced when a file is
  given.
- `NO_SPAWN`: set for a remote target, preserved for a loopback one.
- Verify-before-write: an unreachable daemon, `auth: false` on a remote URL,
  a rejected token, and a failed handshake each exit 4 with every file byte-
  identical.
- All-or-nothing: a write failure injected on the third file restores the
  first two; read-back mismatch counts as a failure.
- Idempotence: a second run reports every row `current` and writes nothing.
- `--dry-run` writes nothing; a non-TTY run without `--yes` exits 2.
- Codex: the replace mode rewrites the server env, `connection.json` and the
  token copy; the plugin hooks' Codex path (`plugin/hooks/session-start.sh`,
  `lifecycle.ps1`) then reads the new URL.
- Claude Code project-scoped entries are reported, never written.
- The installer: the question's three answers, non-interactive behaviour
  unchanged (existing tests in `tests/test_client_install_ux.py` stay green),
  and the client-only path calls `connect` rather than warning.
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
