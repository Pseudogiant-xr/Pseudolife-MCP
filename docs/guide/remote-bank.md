# Sharing one bank across machines

One daemon owns the bank. Every coding-agent session, on this machine or any
other, reaches it through its own stdio shim over streamable HTTP. This page
covers exposing that daemon beyond loopback, giving each remote machine its
own identity, and wiring clients that run no daemon of their own.
Part of the [user guide](../../README.md#documentation).

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

What this is not: there is no offline mode and no replica. When the daemon is
unreachable the remote shim exits and the plugin hooks degrade to
"coordination unavailable"; there is one bank and one writer of record.
Replicating the bank across machines is not part of this feature.

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

In order of preference.

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
  hostname needs no further configuration.

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
that file does not exist yet. The installer then validates the file with the
shim's own owner-only check on every OS, and refuses a file that would fail
at session start.

By hand on Linux or macOS, without the token touching shell history:

```bash
mkdir -p ~/.pseudolife-mcp && chmod 700 ~/.pseudolife-mcp
( umask 077; read -rs -p "token: " t; printf '%s
' "$t" > ~/.pseudolife-mcp/claude-code.token )
```

By hand on Windows, write the token alone to the file with no byte-order
mark (a BOM becomes part of the token and every call is rejected), then
remove inherited access:

```bat
icacls <file> /inheritance:r /grant:r "%USERNAME%:F"
```

### With the installer (client-only path)

The installers take a client-only mode that skips the Docker stack, mints no
token, and wires the selected clients against a remote daemon. A
non-loopback `--daemon-url` implies it; pass it explicitly for a loopback URL
that is really an SSH tunnel.

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

claude mcp add --scope user pseudolife-memory   -e PSEUDOLIFE_WRITER_ID=claude-code   -e PSEUDOLIFE_MCP_NO_SPAWN=1   -e PSEUDOLIFE_MCP_TOKEN_FILE=<path>   -e PSEUDOLIFE_MCP_DAEMON_URL=<url>   -- <shim path>

codex mcp add pseudolife-memory   --env PSEUDOLIFE_WRITER_ID=codex   --env PSEUDOLIFE_MCP_NO_SPAWN=1   --env PSEUDOLIFE_MCP_TOKEN_FILE=<path>   --env PSEUDOLIFE_MCP_DAEMON_URL=<url>   -- <shim path>
```

`<shim path>` is the absolute path of the `pseudolife-mcp` executable pipx
installed, `<url>` an origin such as `http://100.64.0.2:8765` (no `/mcp`, no
path).

The shim waits up to 15 seconds for a remote daemon that does not answer,
which is longer than Codex's default MCP startup timeout. Set the Codex
startup timeout as the [README's Codex setup](../../README.md#quickstart)
describes.

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
`off - this token's principal is not in coordination.allowed_principals`
(the daemon's `principal_not_allowed` reason) when it is missing from the
list.

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
