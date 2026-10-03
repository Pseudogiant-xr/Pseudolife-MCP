# Sharing one bank across machines

One daemon owns the bank. Every coding-agent session, on this machine or any
other, reaches it through its own stdio shim over streamable HTTP. This page
covers exposing that daemon beyond loopback, giving each remote machine its
own identity, and wiring clients that run no daemon of their own.
Part of the [user guide](../../README.md#documentation).

For ChatGPT developer apps and a dot, use the optional guided
[Secure MCP Tunnel setup](tunnels.md). It connects to this same daemon and
keeps existing local client registrations.

## How it fits together

```
machine A (daemon host)                 machine B (client only)
  Postgres (loopback only)                Claude Code / Codex session
  daemon  127.0.0.1:8765  <-- tunnel --   stdio shim (pseudolife-mcp)
  local sessions -> shims                 token file ~/.pseudolife-mcp/
```

The agent board (`memory_agents`, `memory_message`, leases) lives in the
daemon too, so once a remote principal is admitted, sessions on different
machines see each other and exchange mail with no further setup.

What this is not: there is no offline mode and no replica. While the daemon is
unreachable no memory call runs, and the plugin hooks degrade to
"coordination unavailable"; there is one bank and one writer of record. A link
that answers the shim's health check but stalls its board registration leaves
memory working and the registration retrying in the background (see
[configuration](configuration.md)).
Replicating the bank across machines is not part of this feature.

### When the daemon is unreachable

A shim whose daemon is on another machine (or, with `PSEUDOLIFE_MCP_NO_SPAWN=1`,
an external local one) waits up to 5 s for it at startup and then starts the
session anyway, so the client's MCP handshake does not time out. Claude Code
and Claude Desktop never retry a stdio server whose startup failed: before
this, a session started during a few minutes' tailnet outage ran without
memory tools for its whole life unless `/mcp` reconnected it.

- **Handshake.** The shim answers `initialize` and `tools/list` from the last
  handshake it saw from that daemon URL, kept in `handshake-cache/` under
  `PSEUDOLIFE_AGENT_STATE_DIR` (which the installers set per client), else
  under `~/.pseudolife-mcp/` (one file per URL, the 16 most recently written
  kept). With no cache it serves no tools until the daemon
  answers. The served instructions open with a line saying the daemon did not
  answer at startup.
- **Calls.** Each tool call tries the daemon. One that cannot connect returns
  an MCP error saying the daemon is unreachable and being retried; nothing ran.
  A connection attempt gives up after 5 s.
- **Recovery.** The shim probes `/health` in the background at growing
  intervals (1, 2, 5, 10 and 30 s apart, then every 60 s; the intervals keep
  growing until a request actually gets through, so a `/health` that answers
  while `/mcp` still fails cannot make the client re-list every second). When the daemon answers, or a call gets
  through, it sends `notifications/tools/list_changed`, and the client's next
  `tools/list` returns the live tools. The instructions stay as served: MCP
  has no notification for changed instructions. A daemon that stops answering
  mid-session gets the same treatment.

A loopback daemon the shim starts itself is unchanged: if it never comes up,
the shim still exits with the recovery commands.

## What the daemon allows

- The daemon binds `127.0.0.1` by default and **refuses a non-loopback bind
  without a token** (`PSEUDOLIFE_MCP_TOKEN` or a `PSEUDOLIFE_MCP_TOKENS` map;
  `PSEUDOLIFE_MCP_TRUST_BIND` is the explicit escape hatch for a container
  whose boundary is enforced outside it). See `pseudolife_memory/daemon.py`.
- With a token configured, `/mcp` accepts any `Host` header, so a tailnet
  name, a LAN address or a reverse-proxy hostname works. Without one, `/mcp`
  serves loopback `Host` values only and answers `421 Invalid Host header`
  (`transport_security_for` in `pseudolife_memory/mcp_server.py`).
- In the Docker install the container binds `0.0.0.0`, but
  `ops/docker-compose.yml` publishes `127.0.0.1:8765` only. The real exposure
  switch is therefore outside the daemon: a tunnel, a proxy or a port publish.
- The daemon always speaks plain HTTP. TLS, where you need it, comes from the
  tunnel or the proxy. Postgres stays loopback-only on the daemon host.

**A tunnel to loopback bypasses the bind guard.** Traffic forwarded to
`127.0.0.1:8765` looks local to the daemon, so the refusal above never fires.
Before exposing anything, confirm `curl -s http://127.0.0.1:8765/health`
reports `"auth": true` on the daemon host. A default installer run mints the
token; a `--no-token` install must not be exposed.

## Exposing the daemon

In order of preference. An installer run on the daemon host that answers
**3** to its first question ("On this machine, and other machines will
connect to it") is a local install that ends by printing a short version of
these steps and the two sections after them.

### Expose the daemon with one command

On the daemon host, with Tailscale installed and logged in:

```bash
pseudolife-mcp expose tailscale
```

It runs the Tailscale Serve TCP forward described in the next section, with
the checks around it:

- It refuses unless `http://127.0.0.1:8765/health` answers `status: ok`
  with `"auth": true`, so an unauthenticated bank is never exposed. A
  redirect or an answer that is not the daemon's `/health` JSON is refused
  as something other than the daemon.
- It refuses when Tailscale is not installed or not running (`tailscale
  up`), and when the tailnet port already serves anything other than a TCP
  forward to `127.0.0.1:8765`, or has Funnel (public internet) switched on:
  it never replaces another serve. If that forward is already there it
  reports "already exposed" and changes nothing.
- It shows the command and the client URL (`http://<tailnet-ip>:8765`) and
  asks before running it; `--yes` skips the question, and a run with no
  terminal and no `--yes` exits 2 without changing anything.
- A user Tailscale does not let change the serve config is refused with
  the fix for the OS: on Linux, `sudo tailscale set --operator=$USER` (or
  run as root); on Windows, run it as the user logged in to Tailscale or
  from an elevated shell; on macOS, as the user logged in to the Tailscale
  app.
- Afterwards it checks that the serve status shows the forward (if not, it
  removes it again and exits 5; if the status cannot be read back, it exits
  5 and says to check `tailscale serve status`), then fetches `<url>/health` through the
  tailnet and compares its `bank` fingerprint with the local one. That probe
  is advisory: a host cannot always reach its own tailnet address, so a
  failed probe says to check from another machine with
  `curl <url>/health` and leaves the forward in place.

`pseudolife-mcp expose status` prints the client URL, or why there is none;
`pseudolife-mcp expose off` removes the forward, and only a forward to
`127.0.0.1:8765`. A port open to Funnel is never reported as exposed and
never removed. All three take `--port` for a daemon on another port and
`--json` for a machine-readable report. Exit codes: 0 done or already in
that state; 2 usage, declined, or no terminal without `--yes`; 4 refused
before any change; 5 the serve did not take and was undone, or its result
could not be verified.

`/health` reports the bank fingerprint as `bank`: the first 16 hex
characters of the SHA-256 of the bank's coordination identity. It is not a
secret; it tells two daemons' banks apart, which `version` and `schema`
cannot. It is `null` until the daemon's storage has started and the bank
identity exists (a freshly started daemon that has not served a call yet,
a bank whose agent board has never been used, or a file-mode daemon), and
in any `/health` whose database check failed; callers treat `null` as
unknown. Once storage has started, `/health` reads the identity after a
successful database check, at most once a minute, until it exists, then
keeps it.

### 1. Tailscale Serve, TCP mode

No certificate needed; the link is WireGuard-encrypted end to end. On the
daemon host:

```bash
tailscale serve --bg --tcp=8765 tcp://127.0.0.1:8765
```

Clients use `http://<daemon-tailnet-ip>:8765` (for example
`http://100.64.0.2:8765`). Remove the forward with:

```bash
tailscale serve --tcp=8765 off
```

### 2. Tailscale Serve, HTTPS

When the tailnet has HTTPS certificates enabled:

```bash
tailscale serve --bg 8765
```

Clients then use `https://<machine>.<tailnet>.ts.net`, with no port and no
path.

### 3. LAN or reverse proxy

Plain HTTP is acceptable only inside a private network you trust.

- **Docker install:** publish the port on one specific interface address,
  never `0.0.0.0` blindly. Put the publish in the gitignored
  `ops/docker-compose.override.yml`, which `ops/update.ps1` and
  `ops/update.sh` add to every compose call; it sits beside the loopback
  publish, so local clients keep working:

  ```yaml
  services:
    pseudolife-daemon:
      ports:
        - "192.168.1.20:8765:8765"
  ```

- **Host-process daemon:** set `PSEUDOLIFE_MCP_HOST` to the interface
  address; the token requirement then applies directly.
- **Reverse proxy:** terminate TLS at the proxy and forward to
  `127.0.0.1:8765`. The token relaxes the `Host` check, so the proxy's
  hostname needs no further configuration. Pass the daemon's error
  responses through unchanged, `Content-Length` included. The stdio shim
  trusts a `503 principals_unavailable` (no tool ran) only in the daemon's
  exact small JSON form; a rewritten one reads as an operation that may
  have completed.

## Adding a machine: `invite` and `pair`

Two commands give another machine its own identity on the bank, with no
daemon restart and no token travelling between machines. On the daemon
host, after [exposing it](#expose-the-daemon-with-one-command):

```bash
pseudolife-mcp invite laptop            # prints a code such as 7K3Q-M9XA-2PDF
```

On the new machine, either run the installer, answer **2** ("On another
machine that already runs it"), give the daemon's URL, and paste the code
where it asks for the token (or pass `--pairing-code <code>` /
`-PairingCode <code>`), or, where the shim is already installed:

```bash
pseudolife-mcp pair http://100.64.0.2:8765 7K3Q-M9XA-2PDF
pseudolife-mcp connect http://100.64.0.2:8765 --code 7K3Q-M9XA-2PDF   # pair, then re-point every client
```

`pair` mints the bearer token on the new machine, writes it to an
owner-only `~/.pseudolife-mcp/<principal>.token` (never replacing an
existing file), and sends the daemon only the token's SHA-256. The token
never appears in a terminal, a chat, shell history or a network response.
`--read-code` reads the code from stdin instead of the command line. When
an answer is lost on the way back, `pair` retries; if the outcome stays
unknown, it keeps the token file and says so (exit 5), because the code may
be spent and that file is then the only copy of the token.

What an invited machine can do: **read and write the whole bank** and use
the agent board (unless invited with `--no-board`), with its own writer
identity and board addresses. It cannot change the daemon's configuration:
`POST /api/config` and `POST /api/daemon-notice` answer
`403 {"error": "operator_principal_required"}` for a stored principal.
Principals in `PSEUDOLIFE_MCP_TOKENS` keep every right they had.

The rules:

- The code is 12 characters (60 bits), single use, and expires after 15
  minutes (`--expires 2h`, at most 24 hours). Case and dashes do not
  matter, and `O`, `I` and `L` read as `0`, `1` and `1`.
- A name is lowercase `[a-z0-9][a-z0-9._-]{0,63}`, never `default`,
  `daemon` or `maintainer`, and never one already in `PSEUDOLIFE_MCP_TOKENS`
  or `PSEUDOLIFE_MCP_TIER_MAP` (the environment always wins). `--tier
  minimal|core|full` sets its default toolset tier; `--board` /
  `--no-board` its board access. Re-inviting a name keeps both unless
  given. A tier sets which tools a client sees at first, not what it may
  call: `memory_toolset` expands any session to the full set.
- `invite --list` shows each name, whether it is paired or pending, its
  tier, board access and times: never a token or a code.
  `invite --revoke laptop` takes the token away within 10 seconds, or
  within 60 seconds at worst while the daemon cannot read its database.
  Neither needs a running daemon, so access can be withdrawn while it is
  down; `--bank <fingerprint>` makes them check the database is the bank
  you mean. With a Docker install's daemon container stopped, they run
  through `psql` in the Postgres container.
  `invite laptop --replace` gives a paired machine a new code; its old
  token keeps working until the new code is redeemed. A revoked name can be
  invited again.
- The daemon must have a token of its own (`"auth": true`): invited
  principals do not turn authentication on. An invite also checks that
  the database it writes is the bank the local daemon serves (`/health`'s
  `bank`), and refuses while that is still `null`.
- On a Docker install `invite` runs inside the daemon container, so the
  host never handles the database password; elsewhere it uses
  `PSEUDOLIFE_MCP_DATABASE_URL` or the lite tier's embedded Postgres.

Invited principals live in the bank's `principals` table (schema v53), with
only the SHA-256 of each token and code. A physical backup carries them; a
logical export (`pseudolife-mcp export`) leaves them out, as it does the
board's instance credentials. The daemon reads the table every 10 seconds
on its own connection. If that read has failed for over 60 seconds, a
bearer that matches nothing in the environment gets `503 {"error":
"principals_unavailable"}` rather than 401, so a valid invited token is
never reported as wrong. A tool call whose bearer stops resolving between
the gate and the tool is never run as the default principal. It gets the
JSON-RPC error `principals_unavailable` (-32003) when the table cannot be
checked, and `unauthorized` (-32004) when the token was revoked or
replaced (`invite --replace`) meanwhile. The stdio shim passes both on as refusals in which no tool
ran.

Redemption (`POST /api/pair`) refuses any request with an `Origin` header,
takes only JSON bodies up to 1 KiB, answers every failure with the same
`400 {"error": "pairing_refused"}`, and after 20 failed attempts in a
minute answers `429` until the minute has passed. Every attempt counts
against that budget the moment it arrives (a success is given back), at
most two redemptions run at once, and a third answers `429` at once. A
code for a name the environment or a reservation uses is refused without
being spent.

The sections below are the hand-made alternative: tokens in the daemon's
environment, admitted in its configuration.

## One identity per remote client

Give each remote client its own principal, so its writes, its board address
and its toolset tier are its own. On the daemon host, in `ops/.env`:

```bash
# token:principal entries, one per client
PSEUDOLIFE_MCP_TOKENS=<token-1>:<machine>-claude-code,<token-2>:<machine>-codex
# principal:tier entries
PSEUDOLIFE_MCP_TIER_MAP=<machine>-claude-code:full,<machine>-codex:full
```

Tiers are `minimal`, `core` or `full`
([toolset tiers](configuration.md#toolset-tiers)). Generate each token with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Keep the existing `PSEUDOLIFE_MCP_TOKEN` line: it is the local sessions'
`default` principal. Then redeploy, which recreates the daemon with the new
environment:

```powershell
ops\update.ps1        # or: ops/update.sh
```

Never put a token in a URL or in shell history. Move it to the client out of
band.
The installer takes one token file per run; see
[With the installer](#with-the-installer-client-only-path) for wiring two
clients on one machine.

## Admitting the new principals to the board

The board admits only principals listed under
`coordination.allowed_principals` in the daemon's `config.yaml`
(`CoordinationConfig` in `pseudolife_memory/utils/config.py`). In the Docker
install that file is `/data/config.yaml` inside the daemon container. Keep
`default` if the local sessions should stay on the board:

```yaml
coordination:
  allowed_principals: [default, <machine>-claude-code, <machine>-codex]
```

The file is read at boot, so restart the daemon container
(`docker restart pseudolife-mcp-daemon`). The key and its defaults are in
[Configuration — turning the board on](configuration.md#turning-the-board-on).

## Setting up a client machine

The client runs no daemon, no Postgres and no Docker: only the shim, the
token file and the agent's registration.

### The token file

One file per client, `~/.pseudolife-mcp/<client>.token`, owner-only (mode
`600` on Linux and macOS, an owner-only ACL on Windows). The shim refuses to
start when the daemon requires auth and the file is missing or readable by
anyone else.

The simplest way to write it is the installer's `--read-token`
(`-ReadToken`): it reads the token without echo and writes the
`--token-file` path owner-only on Linux, macOS and Windows, and only when
that file does not exist yet: it refuses an existing file, so delete a bad
one first. The installer then validates the file with the
shim's own owner-only check on every OS, and refuses a file that would fail
at session start.

By hand on Linux or macOS, without the token touching shell history:

```bash
mkdir -p ~/.pseudolife-mcp && chmod 700 ~/.pseudolife-mcp
( umask 077; read -rs -p "token: " t; printf '%s\n' "$t" > ~/.pseudolife-mcp/claude-code.token )
```

By hand on Windows, write the token alone to the file with no byte-order
mark (the installer refuses a file that starts with one), then remove
inherited access:

```bat
icacls <file> /inheritance:r /grant:r "%USERNAME%:F"
```

Run this from a normal shell, not an elevated one: from an elevated shell a
new file is owned by Administrators rather than by you, and fails the
owner-only check. Or let `-ReadToken` write it.

### With the installer (client-only path)

Run the installer on the client machine (`ops/install.sh`, or
`ops\install.ps1` on Windows). Its first question is where the bank lives:

```
Where does the memory bank live?
  1) On this machine (default)
  2) On another machine that already runs it
  3) On this machine, and other machines will connect to it
```

Answer **2**. It asks for the daemon's URL, then, once you have chosen the
agents, for the token: paste it, and it is written to a new owner-only file
(`~/.pseudolife-mcp/<client>.token` by default; it never replaces an
existing file), or name a token file that already holds it. The
client-only install follows: it skips the Docker stack, mints no token,
checks the daemon's `/health`, and sets up the shim, the plugin and hooks
and each client's registration against the remote daemon.

The same install without questions takes flags: a non-loopback
`--daemon-url` implies client-only; pass `--client-only` explicitly for a
loopback URL that is really an SSH tunnel. A run that names the daemon
(the flag, or `PSEUDOLIFE_MCP_DAEMON_URL` in the environment), or has no
terminal, is not asked; one given `--token-file` or `--read-token` without
a URL is asked only for the URL.

**Re-running it re-points what is already registered.** When this machine
already has registrations (from an earlier local install, or a client-only
install against a daemon that has since moved), the installer runs
[`pseudolife-mcp connect`](#moving-a-client-to-a-new-daemon) for the
clients it installs, with that run's token file, before the Codex
credential setup and the registrars: it shows connect's plan, applies it,
and stops if connect fails.
The registrars then create only what is still missing. Entries connect does
not rewrite (a project-scoped Claude Code entry, for example) are named, for
you to change by hand. A shim too old to have `connect` (one a running
session kept in place) only gets a warning: that run leaves existing
registrations as they are, as before.

The installer takes one token file per run, so run it once per client, each
with that client's own token file:

```bash
ops/install.sh --client-only --daemon-url <url> --client claude --token-file ~/.pseudolife-mcp/claude-code.token --read-token
ops/install.sh --client-only --daemon-url <url> --client codex --token-file ~/.pseudolife-mcp/codex.token --read-token
```

```powershell
ops\install.ps1 -ClientOnly -DaemonUrl <url> -Client claude -TokenFile <path> -ReadToken
```

If per-client attribution on the board does not matter, one shared principal
and one token file for every client on the machine is fine too.

Codex does not use the operator's token file directly: its credential helper
keeps its own copy under `~/.codex/pseudolife/`, which Codex's hooks read. A
token rotation therefore has to be written to both places.

### By hand

```bash
pipx install pseudolife-mcp

claude mcp add --scope user pseudolife-memory \
  -e PSEUDOLIFE_WRITER_ID=claude-code \
  -e PSEUDOLIFE_MCP_NO_SPAWN=1 \
  -e PSEUDOLIFE_MCP_TOKEN_FILE=<path> \
  -e PSEUDOLIFE_MCP_DAEMON_URL=<url> \
  -- <shim path>

codex mcp add pseudolife-memory \
  --env PSEUDOLIFE_WRITER_ID=codex \
  --env PSEUDOLIFE_MCP_NO_SPAWN=1 \
  --env PSEUDOLIFE_MCP_TOKEN_FILE=<path> \
  --env PSEUDOLIFE_MCP_DAEMON_URL=<url> \
  -- <shim path>
```

`<shim path>` is the absolute path of the `pseudolife-mcp` executable pipx
installed, `<url>` an origin such as `http://100.64.0.2:8765` (no `/mcp`, no
path).

The shim waits up to 5 seconds for a remote daemon that does not answer,
inside Codex's default 10-second MCP startup timeout, and then starts the
session without it and gets memory back by itself once the daemon answers
(see [When the daemon is unreachable](#when-the-daemon-is-unreachable)).

The Claude Code plugin adds the session hooks:

```bash
claude plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
claude plugin install pseudolife-memory@pseudolife-mcp --scope user
```

Its hooks read the Claude Code process environment, not the MCP
registration, so also set `PSEUDOLIFE_MCP_TOKEN_FILE` and
`PSEUDOLIFE_MCP_DAEMON_URL` in the `env` block of `~/.claude/settings.json`.

On a headless box, the provider CLIs (`claude`, `codex`) must be on the
`PATH` of a non-interactive shell, not only a login shell.

### `PSEUDOLIFE_MCP_NO_SPAWN=1` is required

Set it on every client registration. The shim never spawns a local daemon
for a non-loopback URL, but a client-only install over an SSH tunnel uses a
loopback URL: if the tunnel drops, a shim without this setting would spawn a
local daemon on the tunnel's port and serve an empty bank in place of the
real one.

### Verify

```bash
pseudolife-mcp doctor
```

Run it from the same environment as the registered command. The `board` line
reads `on - token present, principal allowed` when admission worked, and
`off - this token's principal is not in coordination.allowed_principals;
list it there, or invite this machine with ...` (the daemon's
`principal_not_allowed` reason) when it is missing from the list.

## Moving a client to a new daemon

When the daemon moves (a new host, a new tailnet address, a restore onto
another machine), point this machine's clients at it with one command:

```bash
pseudolife-mcp connect http://100.64.0.2:8765 --dry-run   # the plan; writes nothing, sends no token
pseudolife-mcp connect http://100.64.0.2:8765             # asks once, then applies
```

It finds every registration of the shim (Claude Code, Codex, Claude
Desktop, Gemini CLI) and the copies the plugin hooks read (the `env` block
of `~/.claude/settings.json`, and Codex's `~/.codex/pseudolife/connection.json`),
and reports each as `current`, `change`, `manual` (found but not written; the
line says what to do) or `absent` (with the command that registers it;
`connect` never creates a registration). Only the daemon URL, the token-file
path and, for a daemon on another machine, `PSEUDOLIFE_MCP_NO_SPAWN=1`
change; the command, writer id and anything else in an entry stay as they
are.

Before writing anything it proves the target accepts every credential it
would point there: the shim's owner-only check of each token file, an
authenticated request, and an MCP handshake. A refusal exits 4 with nothing
written. The writes are all or nothing: each file is backed up beside itself
first, and if one write fails the files already written are restored.

- **Token files.** Without `--token-file`, each registration keeps the token
  file it names, which is right when the bank was restored with its tokens.
  `--token-file` applies one file to every selected client, so to keep one
  principal per client run it once per client:

  ```bash
  pseudolife-mcp connect <url> --client claude-code --token-file ~/.pseudolife-mcp/claude-code.token
  pseudolife-mcp connect <url> --client codex --token-file ~/.pseudolife-mcp/codex.token
  ```

  Add `--read-token` to create that file from a token typed without echo
  (it refuses a file that exists).
- **Not written:** project-scoped Claude Code entries (`projects[...]` in
  `~/.claude.json`, a `.mcp.json`), a project's `.claude/settings*.json`,
  non-stdio registrations, Claude Desktop's old `pseudolife-memory` entry
  name (the installer's Desktop step migrates it), Codex settings that come
  from another Codex config layer, a `PSEUDOLIFE_MCP_DAEMON_URL` in your
  shell or Windows user environment, and a scheduled unattended update
  (the report names the `--schedule` command to re-run). Edit those by hand.
- **Afterwards:** restart the sessions the report lists (and fully quit and
  relaunch Claude Desktop when its entry changed). Each session gets a new
  board address on the new URL.
- **Rolling back:** run `connect` with the old URL, or restore the backups
  the report names.
- **With the installer:** a client-only installer run (answer 2, or
  `--daemon-url`) runs `connect` for the clients it installs, so re-running
  the installer against the new URL re-points them too.

`--yes` applies without asking (a run that is not interactive needs it),
and `--json` prints one machine-readable report. Exit codes: 0 done or
already current, 1 a write failed and was rolled back, 2 usage or not
confirmed, 3 no registration it can write (none found, or only `manual`
ones), 4 verification refused, 5 applied but the post-apply check failed.

## Moving a bank

`pseudolife-mcp move` moves a Docker-tier bank to another host where the
installer has already run from a checkout (its daemon and Postgres
containers exist), with no lost writes. Run it on the bank's current host:

```bash
pseudolife-mcp move --to root@box --dry-run   # the plan; changes nothing
pseudolife-mcp move --to root@box             # asks once, then moves
```

`--to` is anything `ssh` accepts (`root@box`, or a `~/.ssh/config` alias).
Key-based ssh is required: every remote command runs as `ssh -o
BatchMode=yes` with keepalives (`ServerAliveInterval=15`,
`ServerAliveCountMax=4`), and nothing ever types a password. A dropped
connection (ssh's exit 255) is always a failure, never read as an answer.
The target must be a Linux or macOS host with a POSIX login shell and
`curl`, `python3` and `docker` on its PATH. Its checkout is
`~/src/Pseudolife-MCP` unless `--target-checkout` names another. The URL
clients will use is `--target-url`, else the target's `pseudolife-mcp
expose status`; when the target is not exposed yet, the move runs `expose
tailscale --yes` there first.

**Run it under `tmux`, `screen` or `nohup`.** A move takes as long as a
dump, a copy and a restore of the whole bank. On Linux and macOS a closed
terminal (SIGHUP), a SIGTERM or Ctrl-C rolls it back, and the rollback
itself ignores Ctrl-C. On Windows, Ctrl-C and Ctrl-Break roll it back (each
rollback step runs in its own process group, so a second Ctrl-C cannot kill
one), but a closed console window or a dropped ssh session to the source
ends the process with no rollback at all. Every step's progress, with the
manual rollback for exactly what has been done so far, is written as it
happens to `~/.pseudolife-mcp/moves/<move id>/record.json` on the source:
after a move that ended that way, read it and run its `manual_rollback`
lines in order.

**Don't run commands that write the database directly during a move**
(`lease break`, `lease delegate`, `board-audit redact`, `coordination-recovery`, a `psql`
session). The move cuts such connections off before the final dump and
fences the database right after it, but the dump has to connect first, so
a write landing in those seconds would miss the dump.

The target's PostgreSQL must be the source's major version or newer: a
dump restores only forward, and preflight refuses an older target.

The order:

1. **Preflight, nothing changed.** The source is healthy and reports its
   bank fingerprint; ssh works; the target checkout exists; the target
   daemon is healthy, at the source's schema or newer, serving a different
   bank, and its bank is empty (no entries, facts or invited principals).
   `move` never merges into or overwrites a used bank. A target that is not
   ready is refused with the install line to run there.
2. **Plan and confirm.** Every step, with sizes and the target URL.
   `--dry-run` stops here; a run that is not interactive needs `--yes`.
3. **Expose the target** if needed, and check this host reaches
   `<target-url>/health`.
4. **Pause, then stop the source.** The target's unattended update and its
   daemon's restart policy are paused (either could start the target's
   daemon mid-move), and so is this host's unattended update; each only when
   it is enabled now. Then `docker stop -t 120` the source daemon (never
   `rm` or `down`); Postgres stays up.
5. **Final backup, then fence.** Other connections to the source database
   are cut off, `pg_dump` runs from the Postgres container, then at once
   `ALTER DATABASE <db> WITH ALLOW_CONNECTIONS false`, which stops every
   other writer (an older daemon image, `lease break`, `board-audit`), and a
   check that nothing is still connected. Then the daemon's `/data` and a
   manifest with per-table row counts. The files stay under
   `~/.pseudolife-mcp/moves/<move id>/` on the source.
6. **Copy** the three files to the target over ssh and check their SHA-256
   there.
7. **Restore on the target without starting it**: a resume marker is
   written beside the copies (`<checkout>/data/move-<move id>/marker.json`,
   outside the daemon's `/data`), then `ops/restore.sh --apply --no-start
   --backup-file <the dump> --state-archive <the archive>` (it takes the
   target's own safety dump first). The restored bank's identity (`meta`
   `coordination_bank_id`) and row counts must match the source's. Then
   `/data/move.json` is written: a daemon refuses to start while it is
   there, so the target cannot come up outside the move.
8. **Carry the identities.** The source container's
   `PSEUDOLIFE_MCP_TOKEN`, `PSEUDOLIFE_MCP_TOKENS`,
   `PSEUDOLIFE_MCP_TIER_MAP` and `PSEUDOLIFE_MCP_TOOLSET` are written into
   the target's `ops/.env` (backed up beside itself first, its owner and
   mode kept), over ssh stdin, never on a command line, so no client
   anywhere needs a new token. Invited machines need nothing: they are in
   the bank. The target's own installer-minted token is replaced; the
   report names the files on the target that held it (found with `grep`
   reading the token from stdin, never rewritten). `--no-keep-tokens` skips
   this step, and the report lists every environment principal to invite
   again.
9. **Start the target once.** `/data/move.json` is lifted and
   `ops/update.sh` recreates the daemon. Its `/health` `bank` fingerprint
   must equal the source's within 180 s (a cold daemon reports null until
   it has read its bank identity, so the move nudges it with one
   authenticated read a minute), and `docker inspect` must show it runs
   with each carried value (compared by SHA-256). Then the source
   container's restart policy is set to `no` (so the refusing source never
   crash-loops) and `/data/moved.json` is written into it (a daemon new
   enough to read it refuses to start and names the new location). That is
   the commit point: from here on nothing rolls back. What step 4 paused on
   the target is restored and the resume marker is removed last; if one of
   those fails (a dropped ssh connection, say), the move exits 5 and lists
   each follow-up with its exact command.
10. **Re-point this machine's clients** with `pseudolife-mcp connect
    <target-url> --yes`.
11. **Report**: the manual rollback, the `connect <target-url>` line for
    every other machine's principal, board leases still held (each with its
    `lease break` line), undelivered mail per old board address, the source
    leftovers to retire (the backup task or cron, extractor shims, saved
    tunnel profiles, the daemon's autostart, the tailnet serve, the
    unattended update, left paused), and what the state archive replaced on
    the target (`config.yaml`, and `last-backup.json` until the target's
    first backup).

**Rollback.** A failure in steps 4 to 9 undoes, in this order: the target
daemon is stopped if it was started (and its `/data/move.json` put back),
the target's `ops/.env`, unattended update and restart policy are restored,
the database fence is lifted, the source's `moved.json` is removed, its
restart policy and paused schedule are restored, and the source daemon is
started again. The target is never left running beside a running source:
if it cannot be stopped, the source stays down, and so it does while the
fence or `moved.json` cannot be undone; the move then exits 6 and the
report and the record say what to finish by hand. A target that comes
back up after the stop (a remote `ops/update.sh` that kept going when the
local ssh died) is stopped again, and the source starts only once the
target reports stopped. When the gate cannot be written, the target's
unattended update and restart policy stay paused (either could start the
clone), and the rollback says so. The target keeps its resume marker, so
`pseudolife-mcp move --to <target> --resume` may overwrite that
half-restored bank on the next attempt; `--resume` refuses any bank whose
marker names another move.

**A target left gated.** While `/data/move.json` is there the target's
daemon refuses to start, and the bank under it is a clone of the source's.
To abandon the move there, restore the target's own pre-move safety dump
(the one `ops/restore.sh` took first, in its `data/backups`) with
`ops/restore.sh --apply --backup-file <that dump> --state-archive <its
state archive>`, which replaces `/data`, gate included. Remove the gate by
hand (`docker run --rm --volumes-from pseudolife-mcp-daemon <image> rm -f
/data/move.json`) only to finish a move deliberately, with the source
stopped and fenced.

**Afterwards** the source is stopped and fenced, never deleted. To roll the
move back by hand: `docker stop pseudolife-mcp-daemon` on the target, then
gate it so its next scheduled update cannot bring it back (`docker run --rm
--entrypoint touch --volumes-from pseudolife-mcp-daemon <image>
/data/move.json` there); on the source, `docker exec pseudolife-mcp-postgres psql -U pseudolife -d postgres
-c "ALTER DATABASE <db> WITH ALLOW_CONNECTIONS true"`, `docker run --rm
--entrypoint rm --volumes-from pseudolife-mcp-daemon <image> -f
/data/moved.json` (`docker cp` cannot delete), `docker update
--restart=unless-stopped pseudolife-mcp-daemon`, `docker start
pseudolife-mcp-daemon`, and `connect` back. The report prints these with
the real names filled in.

Sessions get new board addresses on the new URL; see
[the move exemption](coordination-recovery.md#a-move-is-not-a-restore) for
what that means for pending mail.

Exit codes: 0 moved; 1 failed and rolled back (the source runs again);
2 usage, declined, or no TTY without `--yes`; 4 preflight refused, nothing
changed; 5 the bank moved but re-pointing this machine's clients failed
(rerun the `connect` line it prints), or a follow-up on the target after
the commit point failed (run the commands it lists); 6 failed and the rollback could not
finish (finish it with the commands the report and the move record list).
`--json` prints one report object.

## Troubleshooting

**"unhandled errors in a TaskGroup" on every `tools/list`, while `curl` with
the same token succeeds.** This is not an auth failure. The shim's MCP client
library must be on the same major-minor line as the daemon's: a client on
`mcp` 2.2 against a daemon on 2.1 fails exactly this way. The project pins
the range (`mcp>=2.1,<2.2`); a shim pipx-installed from an older release may
need:

```bash
pipx runpip pseudolife-mcp install "mcp<2.2"
```

or a reinstall of `pseudolife-mcp`.

**The shim exits at startup naming the credential file.** The file is
missing, not owner-only, or holds more than the token. Fix the file; the
error line names the path it read.

## What latency to expect

One informal measurement on one setup, not a benchmark: 2026-09-28, a Linux
container client reaching a Windows daemon host over a direct tailnet path.
`/health` took 7-10 ms, an authenticated REST call 60-70 ms, and the
per-turn prompt hook 43-45 ms on the client (about 135 ms on the Windows
host itself, because the hook's cost is process start, not the network). A
relayed (DERP) tailnet path adds tens of milliseconds per call.

## Hosting the daemon on another machine

The daemon host does not have to be your workstation. Plan for:

- **Disk:** tens of GB. The images are about 5 GB (daemon), 12 GB (extractor
  sidecar) and 0.6 GB (Postgres), plus the bank volumes.
- **Memory:** the reference deployment caps the daemon container at 6 GiB
  (`PSEUDOLIFE_DAEMON_MEM_LIMIT`); it runs at about 3 GB in steady state.
- **Extractor:** dreams need an extractor the daemon host can reach: the
  in-stack sidecar, or an extractor endpoint reachable from that host
  ([Dreaming](dreaming.md)).
- **Backups:** `ops/backup.ps1` / `ops/backup.sh` run where the daemon runs
  ([backups](configuration.md#backups)).
- **Images:** pull the GHCR images or build from the checkout; the
  extractor sidecar always builds locally
  ([README — install](../../README.md#install--containerized-any-os)).
