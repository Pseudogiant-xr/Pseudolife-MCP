# Install and move

Use the [installer](../../README.md#durable-tier--docker-recommended-for-a-long-lived-bank) for a Docker bank:
`ops/install.sh` on Linux/macOS or `ops/install.ps1` on Windows. For a lite
bank without Docker, install `pip install 'pseudolife-mcp[lite]'` and follow
the [lite quickstart](../../README.md#quickstart). To relocate an
existing Docker bank, use [`pseudolife-mcp move`](remote-bank.md#moving-a-bank)
from its current host. Install the destination first and review `--dry-run`.
Never hand-write or flatten a compose file to install or move a bank.

## Daemon engine

A new Docker install runs the Rust daemon, from the image the checkout's
release publishes: it checks free disk space, pulls that image and
downloads the pinned models (about 2.4 GB, once, into their own
`pseudolife_rust_models` volume, which stays out of every backup) before
any container starts, and records the selection once the daemon reports ready at that
release. With no `--engine`, the install takes the Python daemon instead,
saying why, when it has no bearer token (`--no-token`, `--transport http`
with no token set, or a Python-client install on a host that cannot
install the shim and so mints none), when the release publishes no Rust image or its models cannot be
downloaded, or when the bank already exists
(the next attended `pseudolife-mcp update` moves that bank, after its own
checks). `ops/install.sh --engine python` (Windows: `ops/install.ps1
-Engine python`) chooses Python and records that choice, so later updates
keep it. `--engine rust` names Rust explicitly: without a bearer, image or
models it refuses, names `--engine python`, and starts nothing; for a bank
that already exists it installs on Python and then switches through update
(below).

A release-mode `pseudolife-mcp update` moves a Docker host that never
chose an engine to the Rust daemon when the target release publishes its
image. An ordinary update can move the host from a script, an SSH command
or an agent's shell too. Before it downloads the models or backs
anything up, the update asks the Rust image whether it supports this bank's configuration (`pseudolife-daemon
check-config`: a non-default reranker or embedding model, for instance, is
not). If that check, or anything else before the backup, refuses, the update
removes what it pulled for the move, warns with the refusal and its fix,
and continues on Python; nothing is recorded, so the next update tries
again. `--engine python` keeps Python for good, and `--engine rust` refuses
an unsupported configuration before the backup instead of falling back.
The updater that deploys the daemon is the one already installed, so an
update started from a release older than 0.23.0 installs the new release
on the Python daemon. The next attended `pseudolife-mcp update` can move
the host. When the native updater deploys a Rust daemon over a legacy
bank, it first runs the archived Python 0.23.1 daemon to finish the
import. This applies to banks below schema 57, with counters still in
`weights.pt`, or with an unfinished Chroma import. The archived image is
available only for Linux amd64, including x64 Docker Desktop running
Linux containers. On a host using Linux arm64 images, the native updater
refuses before taking a backup. Restore a verified backup on a Linux
amd64 Docker host, complete the update there, then move the migrated
bank back. The Python updater uses its own import path.

Scheduled (`--unattended`) updates always keep the installed engine. On
a Python host, if the newest release has no Python image or the update
requires the legacy import, the scheduled run holds and posts one notice
per reason per release. Run `pseudolife-mcp update --engine rust --tag <version>`
once at a terminal, using the version in the notice. A scheduled run
leaves an interrupted attended bridge in place. Use the same command at
a terminal to resume it.

A rerun keeps the engine already installed, also after `docker compose
down`. A Rust daemon stays on the release it was installed or last updated
to: move it with `pseudolife-mcp update`, not by pulling the checkout and
rerunning. Naming the other engine on an existing install, or `--engine
rust` for a bank that already exists, finishes the install on the current
engine and then switches through `pseudolife-mcp update --engine <engine>`.
That update backs up the bank, tags a rollback, and accepts the new daemon
only after it passes its health check; if it fails, it prints the commands
that restore the previous one. The first rerun after such a switch
recreates the daemon once to match. A Rust daemon selected by checkout-mode
`update --checkout` is left to that command: the installer refuses and
names it. `--engine` applies only to a Docker install, so a client-only
install refuses it.

## Native client

The installers install the native client by default. It runs its setup and
client-registration steps without host Python. It checks the platform archive
against `SHA256SUMS` and confirms that the client supports the installer's
setup commands before making changes. Use the installer and native archive
from the same release. A rerun of the installer on a machine with the Python
client moves it to the native client, also with `--yes` or redirected input.
The client choice is independent of `--engine`, which selects the Docker
daemon.

When the native client cannot serve the machine, the installer installs the
Python client instead. It says why in one line before it changes anything,
and keeps any verified download for a later run. That happens in these cases:
- no client is published for the platform;
- the checkout's release is not published yet;
- the client does not run there (an older C library, for example);
- the checkout's release predates this installer;
- the Python client registered a tunnel to start by itself.

`PSEUDOLIFE_SHIM_ENGINE=native` makes each of these a refusal instead, and so
does a machine already on the native client, which a rerun never swaps for
the Python one. A release whose `SHA256SUMS` names this platform's archive
in a broken line also refuses, because that release does carry the
platform.

To install the Python client on purpose, set `PSEUDOLIFE_SHIM_ENGINE=python`
for the installer (`$env:PSEUDOLIFE_SHIM_ENGINE = 'python'` in PowerShell).
The installer records that choice in `~/.pseudolife-mcp/client-engine`, and
later installer runs without the variable keep the Python client. A run with
`PSEUDOLIFE_SHIM_ENGINE=native` installs the native client and removes the
record.

The Windows x64 native client bundles its C runtime starting with 0.25.1.
If an older archive fails to start because a system library is missing
(`0xC0000135`), use a release that bundles the runtime or install the
Microsoft Visual C++ Redistributable for x64 to keep using that archive.
Starting with 0.25.1, the native installers and the native updater's
hand-off probe identify this loader error when they detect it.

On an installer-managed Python client from 0.23.0 or later,
`pseudolife-mcp update` moves the machine to the native client, also
`update --clients-only` on this release's Python client. If your client
predates 0.23.0, update it on Python first, then run `pseudolife-mcp update
--clients-only`. The updater checks the release archive and keeps the Python
client, saying why, when it cannot move this installation. It also keeps
the Python client when the update runs without a person, when
`PSEUDOLIFE_SHIM_ENGINE=python` is set, or when an installer run recorded
that choice. Running sessions keep their runtime. New sessions use the
selected client through the existing launcher. Pip, pipx and lite installs
update in place. See [The native client](configuration.md#the-native-client)
for the supported platforms, tunnel limits and the route back to Python.

If the Python client registered a tunnel to start automatically, the
installer keeps the Python client and prints how to remove that registration.
With `PSEUDOLIFE_SHIM_ENGINE=native` it stops in preflight instead. Keep the
Python client to retain autostart. On the native client, tunnels run in the
foreground. See [the tunnel guide](tunnels.md#autostart-and-private-storage)
before moving a host that serves a tunnel.

The installer's guided tunnel setup (`--tunnel` or `-Tunnel`) is available
with the Python client (`PSEUDOLIFE_SHIM_ENGINE=python`). Without that
variable, the installer exits with status 1 before changing the host. For the
native client, rerun without the switch and follow the [explicit profile and
key setup](tunnels.md).

When the native installer sets up Codex with a daemon token (the local
install's own or a supplied file), it waits up to three minutes for an
authenticated daemon read. It retries a connection failure or server error
while the daemon starts. A rejected token (HTTP 401 or 403) stops the check
immediately. If the check fails, the installer completes the rest of the
setup and skips Codex registration.

If the native installer records a setup failure for a requested client (its
connection, its plugin or hooks, or an unreadable Claude Code plugin
registry), or native shim autostart fails for an extractor chosen by flag or
typed at the prompt, it completes the remaining setup and exits with status 5. Its final
summary names each failure and how to retry. Hooks awaiting approval and
platforms without native shim autostart produce warnings. Clients selected by
flag or at the prompt count as requested, including the default accepted
with Enter. A rerun treats a restored list as requested only when the saved
request matches the list. A detected-client failure produces a warning and
allows a successful exit.

On reruns, the native installer can check a client without changing its
registration. It uses read-only helpers to inspect the Pseudolife entry in
Gemini's configuration, verify the existing bearer token with the daemon,
and identify connection plans that need only credential verification.
An identical client-only rerun with `--token-file` leaves Codex's
registration on Codex's own copy of that token while the copy holds the
same token, and says there is nothing to write; a rotated token in the file
moves the registration to the file.
For a current registration on a client-only rerun, credential verification
can wait up to three minutes if the daemon is unreachable. If a grouped
client-only check fails, the installer checks the clients separately and
keeps completed work.

For a new client-only machine, [pair it to the daemon](remote-bank.md#adding-a-machine-invite-and-pair)
with `--pairing-code` / `-PairingCode`. The installer creates a private token
file. If you supply an existing token file with `--token-file` / `-TokenFile`,
use an owner-only regular file. For a new registration on a daemon that
requires a bearer token, use the default stdio transport
(`--transport shim` / `-Transport shim`). The installer can preserve an
existing HTTP registration and exit 0 with a warning to verify the
credential in the client.

Release update can refresh existing Codex hook scripts when their approved
definitions stay the same. If a definition changes, open `/hooks` in the
Codex CLI or **Settings > Hooks** in the Codex app to review and trust the
changed Pseudolife handlers.

## Installer refusals

Both Docker installers check before creating volumes or rewriting
configuration, including on `--yes` / `-Yes` runs. A refusal leaves the
existing bank and configuration intact. Use the recovery named in its
message, then rerun the original installer:

| What preflight found | Recovery |
|---|---|
| A foreign container, even stopped, or a selected volume attached to another stack | Maintain it with that stack's own compose file. For clients only, use `ops/install.sh --client-only --daemon-url <url>` (Windows: `ops/install.ps1 -ClientOnly -DaemonUrl <url>`). |
| An existing volume not recorded by this install | Restore the original `ops/.env`, including `POSTGRES_PASSWORD`, `PSEUDOLIFE_BANK_VOLUME` and `PSEUDOLIFE_STATE_VOLUME`; do not delete the volume. For pre-rename volumes, use the exact names printed by preflight. |
| Postgres rejects `POSTGRES_PASSWORD` | Restore that setting from the original `ops/.env`. If a stopped container records a different initialization password, start the original stack with its own compose file so preflight can verify the current password. |
| Port 8765 or 5433 belongs to another service | Stop or reconfigure that service, then rerun. The installer's own bound ports are valid on reruns. If the refusal says no service answers on the port, a service that stopped a moment ago most likely held it: rerun in about a minute. |
| A different install directory or missing recorded origin | Rerun from the original checkout with its original `ops/.env`; restore that checkout or env file if missing. A stack using multiple env files is maintained with `pseudolife-mcp update`. |
| The configuration no longer retains daemon authentication | Restore the original `ops/.env` instead of generating replacement credentials. |

## Running under Portainer or another stack manager

Use [`ops/docker-compose.stack.yml`](../../ops/docker-compose.stack.yml)
unchanged with Portainer, Dockge, Komodo, or a plain Docker Compose folder.
It is self-contained: two GHCR images, no local builds, includes, or overlays.
This is a Docker Compose deployment, not a Swarm stack.
The source-build installer remains the normal path, including automated
extractor setup. This alternative lets a stack manager own the containers.

### Prepare a fresh Docker host

Run these commands on the Docker host, with Docker Compose v2, Python 3,
`curl`, and the GitHub CLI installed. Download from the selected published
release tag, keeping a host copy for updates and recovery:

```bash
mkdir -p ~/pseudolife-stack
cd ~/pseudolife-stack
VERSION="$(gh release view --repo Pseudogiant-xr/Pseudolife-MCP --json tagName --jq .tagName)"
curl -fLo docker-compose.stack.yml \
  "https://raw.githubusercontent.com/Pseudogiant-xr/Pseudolife-MCP/$VERSION/ops/docker-compose.stack.yml"
```

The stack file ships in version-tagged source. The release asset
`pseudolife-compose-<version>.tar.gz` contains the native updater's compose
bundle; use the source file above for this path.

These next commands are for a **new, empty bank only**. If either container
or volume already exists, stop and identify its bank first. To relocate an
existing bank, use `move`; to retain pre-rename volumes, keep their exact
names in `PSEUDOLIFE_BANK_VOLUME` / `PSEUDOLIFE_STATE_VOLUME` instead of
creating empty replacement volumes.

```bash
[ ! -e .env ] && [ ! -e client.token ] || { echo "Keep existing credentials; stop here" >&2; exit 1; }
for container in pseudolife-mcp-daemon pseudolife-mcp-postgres; do
  if docker container inspect "$container" >/dev/null 2>&1; then
    echo "Existing $container: identify its bank before continuing" >&2; exit 1
  fi
done
for volume in pseudolife-mcp-bank pseudolife-mcp-state; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Existing $volume: identify its bank before continuing" >&2; exit 1
  fi
done
docker volume create pseudolife-mcp-bank
docker volume create pseudolife-mcp-state
umask 077
python3 - "${VERSION#v}" <<'PY'
from pathlib import Path
import secrets
import sys
token = secrets.token_urlsafe(32)
password = secrets.token_hex(32)
with Path('.env').open('x') as env:
    env.write(f'POSTGRES_PASSWORD={password}\nPSEUDOLIFE_MCP_TOKEN={token}\n')
    env.write(f'PSEUDOLIFE_IMAGE_TAG={sys.argv[1]}\n')
with Path('client.token').open('x') as credential:
    credential.write(token + '\n')
PY
chmod 600 .env client.token
```

The first token authenticates the reserved `default` principal. Keep it
private; never paste it into chat or tracked YAML. The Rust daemon gives
the writable bank its identity at startup. Once it is ready, use
[`pseudolife-mcp invite`](remote-bank.md#adding-a-machine-invite-and-pair) on the
daemon host to issue separate credentials to other machines. The stack
passes through every runtime setting from `ops/docker-compose.yml`, including
`PSEUDOLIFE_MCP_TOKENS`, `PSEUDOLIFE_MCP_TIER_MAP`, the dream, fallback and
judge keys, memory limits, and both volume-name overrides. Put overrides in
the same env file, not in a rewritten compose file.

### Deploy and connect clients

In your manager, name the Compose project/stack **`pseudolife-mcp`**, paste
the unchanged stack file, and load `.env` as its substitution environment.
Keep the host copy of that env file synchronized with the manager's values.
If the manager changes the project name despite top-level `name:`, use its
Compose-project setting to retain `pseudolife-mcp`. For a plain folder:

```bash
docker compose --env-file .env -f docker-compose.stack.yml up -d
curl --fail http://127.0.0.1:8765/health
```

Ports remain loopback-only: Postgres on 5433 and the daemon on 8765. For
clients on another machine, follow the [remote-bank guide](remote-bank.md)
to expose the daemon with authentication; keep Postgres private.

The fresh stack needs no extractor process. `/health` reports
`extractor: "none"`: storage, search and explicit facts work, but
automatic dream extraction writes no facts until you configure an
extractor. Existing volumes can retain an extractor configured in the
Console; an empty environment value does not erase that configuration.

Install the clients using a checkout of the same release, **client-only**
so the installer does not take over the stack. On the Docker host:

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
git checkout "$VERSION"
ops/install.sh --client-only --client codex \
  --daemon-url http://127.0.0.1:8765 \
  --token-file "$HOME/pseudolife-stack/client.token"
```

Choose `--client claude`, `--client gemini`, or a comma-separated list as
needed. On a remote client, use its reachable daemon URL and redeem an
invite, or supply its own private token file. To repoint clients that are
already registered (connect does not create missing registrations):

```bash
pseudolife-mcp connect http://127.0.0.1:8765 \
  --token-file "$HOME/pseudolife-stack/client.token"
```

### Enable an extractor when wanted

Choose one path, then redeploy with the same env file:

- **Existing OpenAI-compatible endpoint:** set `PSEUDOLIFE_DREAM_BASE_URL`
  to its container-reachable `/v1` URL, `PSEUDOLIFE_DREAM_MODEL` to the served
  model, and `PSEUDOLIFE_DREAM_API_KEY` when required. A host endpoint uses
  `host.docker.internal`, not `localhost`; it must listen where Docker can
  reach it. See [endpoint examples](dreaming.md#upgrading-the-extractor--bigger-local-models).
- **Claude or Codex CLI shim:** from the client checkout run
  `ops/install-shim-autostart.sh` (Claude) or
  `ops/install-codex-shim-autostart.sh` (Codex), optionally with `--model`.
  These register a Linux user service; Windows has matching `.ps1` scripts.
  Set the stack's
  `PSEUDOLIFE_DREAM_BASE_URL` to `http://host.docker.internal:8082/v1`
  (Claude) or `http://host.docker.internal:8086/v1` (Codex), and
  `PSEUDOLIFE_DREAM_MODEL` to the shim's model. The CLI must be signed in
  on that host. [Shim setup](dreaming.md) includes platform and firewall details.
- **CPU sidecar (explicit local build):** the extractor image is not
  published on GHCR. In a checkout, opt into
  [`ops/docker-compose.stack-extractor.yml`](../../ops/docker-compose.stack-extractor.yml)
  as a second file; set `PSEUDOLIFE_DREAM_BASE_URL` to
  `http://pseudolife-extractor:8081/v1` and `PSEUDOLIFE_DREAM_MODEL=extractor`.
  Run `docker compose --env-file "$HOME/pseudolife-stack/.env" -f
  ops/docker-compose.stack.yml -f ops/docker-compose.stack-extractor.yml up -d
  --build`. This path needs the checkout/build context and a manager that
  supports local builds; keep the default two-image file for GHCR-only hosts.

If the Console owns the extractor settings (`extractor_source: config`),
configure its Extractor panel there instead. Check `pseudolife-mcp doctor`
and the Console's dream status after enabling an extractor.

### Updates, backups and names to preserve

Run release-mode update **on the Docker host**, where Docker can inspect
the containers, with the retained host env file:

```bash
pseudolife-mcp update --env-file "$HOME/pseudolife-stack/.env"
```

It backs up, tags the previous image for rollback, recreates only the daemon,
checks health, then updates clients. The PostgreSQL container belongs to the
stack, so update leaves it as it is and says so; redeploy the stack from the
release's `ops/docker-compose.stack.yml` to pick up a change to it. It recognizes the compose project from
container labels when the original compose files exist on the host (for
example, a plain compose folder or a manager with a shared compose
directory). Keep that env file available and identical to the manager's
environment, especially passwords, tokens and legacy volume names.

If the manager keeps its compose files inside its own container (Portainer
commonly does), export its original stack YAML and environment to the host.
For this unchanged stack, the retained files above are those originals:

```bash
pseudolife-mcp update \
  --compose-file "$HOME/pseudolife-stack/docker-compose.stack.yml" \
  --env-file "$HOME/pseudolife-stack/.env"
```

Repeat `--compose-file` for each original overlay in the original order.
The updater retains the running project label and adds the GHCR image
overlay. An env file alone cannot recover a missing compose file; update
refuses that path before changing the stack. Never substitute a flattened
or installer compose file for the manager's original configuration.

If the manager must perform the redeploy itself, first run the standalone
backup below and retain the old image tag for rollback. Set
`PSEUDOLIFE_IMAGE_TAG` in both manager and host env file to the chosen
published release, redeploy the unchanged stack, and verify `/health` and
a memory call. Run `pseudolife-mcp update --clients-only --tag <that-version>`
on client machines afterwards.

Before the manager's **next redeploy**, set `PSEUDOLIFE_IMAGE_TAG` in both
its environment and the host env file to the version the successful update
reports (without `v`). Update's image-tag override applies to that command;
a manager redeploy with an old pin can start old code against a newer schema.
Disable automatic image-only redeploys; a manager-driven update needs the
explicit backup, previous tag and verification above. Retain the compose file and secrets
alongside backups; never rotate the Postgres password by editing `.env`
alone on an existing database.

For a standalone backup, run on the Docker host; no checkout is needed:

```bash
pseudolife-mcp backup --out "$HOME/pseudolife-backups"
```

Keep both the database dump and state archive, and copy them to another
disk/host. To replace a bank from that pair, run
`pseudolife-mcp restore --backup-file <dump.sql.gz> --state-archive <state.tgz>`.
[Backup and restore](configuration.md#backups) explains
the space checks, restore confirmation and safety backup. Checkout
`ops/backup.*` / `ops/restore.*` remain available for mirrored backups and
scratch restore rehearsals. An update's automatic backup
is additional protection, not a backup schedule.

The manager must preserve project `pseudolife-mcp`, service names
`pseudolife-pg` / `pseudolife-daemon`, container names
`pseudolife-mcp-postgres` / `pseudolife-mcp-daemon` (and
`pseudolife-mcp-extractor` if opted in), and the **existing** external bank
and state volume names. Keep the loopback port bindings, log limits and
daemon capabilities/security options. Never use `down -v`, delete a state
volume, or let the manager replace an external volume with an empty one.
Keep one daemon for one bank.
