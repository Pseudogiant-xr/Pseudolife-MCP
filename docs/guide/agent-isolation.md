# Running agent sessions under a separate account

Coding agents (Claude Code, Codex) that work on this repository run its test
suite, and until 2026-10-04 that meant holding the bank owner's Postgres
password. This page covers the two halves of keeping agents away from the
bank's keys: a **test login** that cannot open the bank, and a **separate,
standard Windows account** for the agent sessions, so they cannot read the
maintainer's files, Docker, or `ops/.env` at all.

- [Why](#why)
- [The test login: `pseudolife-mcp test-login`](#the-test-login-pseudolife-mcp-test-login)
- [What the agent account gets, and what stays with the maintainer](#what-the-agent-account-gets-and-what-stays-with-the-maintainer)
- [Setting up the agent account (Windows)](#setting-up-the-agent-account-windows)
- [WSL and Docker Desktop](#wsl-and-docker-desktop)
- [The second machine (remote suites)](#the-second-machine-remote-suites)
- [Verifying it, as the agent account](#verifying-it-as-the-agent-account)
- [What this does not cover](#what-this-does-not-cover)

## Why

The bundled Postgres (`pseudolife-mcp-postgres`, `127.0.0.1:5433`) serves
the production bank (`pseudolife_memory`) and the test suite's per-run
databases. Both used one role, `pseudolife`, the server's superuser, and
one password, `POSTGRES_PASSWORD` in `ops/.env`. The suite read that file
(and the full-run refusal told every session to copy it into its
worktree), so any agent that ran tests held the bank owner's password.
`ops/.env` also holds `PSEUDOLIFE_MCP_TOKENS`, the bearer tokens of the
operator principals.

With database-owner access an agent could rewrite any memory, and run the
operator commands that open the bank directly (`invite`, `lease break`,
`board-audit`, and the maintainer-passkey commands that a pending change
adds). Those need nothing but that access.

## The test login: `pseudolife-mcp test-login`

```bash
pseudolife-mcp test-login create          # once, on the daemon host, as the maintainer
```

Run it where the owner credentials already are: on a Docker install it runs
`psql` inside the `pseudolife-mcp-postgres` container over the container's
local socket, as the container's own superuser, so the host never handles the
owner's password (`--admin-url <superuser URL>` instead, for a server
elsewhere: leave the password out of the URL and libpq reads `PGPASSWORD`
or `~/.pgpass`; an error never prints it). It refuses before changing
anything when that connection is not a superuser. Idempotently, it:

| What | Why |
|---|---|
| Creates or resets the role `pseudolife_test`: `LOGIN CREATEDB`, not superuser, no `CREATEROLE`, `REPLICATION` or `BYPASSRLS`, every role membership revoked | It can create, reset and drop the databases it creates, which is everything the suite does on the server, and nothing else |
| `REVOKE CONNECT ON DATABASE pseudolife_memory FROM PUBLIC` (and from the role); also the container's `POSTGRES_DB` and any `--bank DB` | The login cannot connect to the bank. Its owner keeps `CONNECT` as owner, and on the Docker tier the daemon connects as that owner. When `PSEUDOLIFE_MCP_DATABASE_URL` names another user that reaches the bank only through `PUBLIC`, it refuses before any change and names the `GRANT CONNECT` to run first |
| Installs (and updates) `vector` in `template1` | pgvector does not mark its extension trusted, so `CREATE EXTENSION vector` needs a superuser. `CREATE DATABASE` copies `template1`, so the login's databases already have it and the schema's `CREATE EXTENSION IF NOT EXISTS` skips |
| Writes `~/.pseudolife-mcp/test-pg.env`, owner-only: `PSEUDOLIFE_TEST_PG_USER`, `PSEUDOLIFE_TEST_PG_PASSWORD` | The suite reads it before `ops/.env`, so no checkout needs `ops/.env` |

It prints each change. A second run re-applies the file's password (running
suites keep working); `--rotate` draws a new one. When the role already
exists and the file is missing (another account, or a lost file), it
refuses unless `--rotate`, since a new password stops every other copy of
the file: copy the current file instead. The server is sent only
the password's SCRAM-SHA-256 verifier, never the password. It also lists
other databases `PUBLIC` may still connect to: the test login holds no table
privileges in a database it does not own, and `--bank DB` closes one. After
upgrading the Postgres image, run it again to update `vector` in `template1`.

A fresh install, and every installer re-run, runs it once the stack is
healthy; a failure there only warns. `pseudolife-mcp update` does not: it is
the deploy path, and changing roles and grants on the live server is a
separate decision, so on an existing install run the command once by hand.

The suite logs in with the first of:

1. `PSEUDOLIFE_TEST_PG_PASSWORD` (as `PSEUDOLIFE_TEST_PG_USER`, else the
   bank owner `pseudolife`);
2. the test login file (`PSEUDOLIFE_TEST_PG_LOGIN_FILE`, else
   `~/.pseudolife-mcp/test-pg.env`);
3. `POSTGRES_PASSWORD` from the checkout's `ops/.env`, as the bank owner.
   This still works, and the run prints one line saying it is using the bank
   owner's password and naming `test-login create`;
4. the compose default password.

`PSEUDOLIFE_TEST_DATABASE_URL` (CI's form) still overrides all of it.
`ops/wsl-suite.ps1` sends WSL runs the login file and, when there is one,
no longer copies `ops/.env` into WSL.

## What the agent account gets, and what stays with the maintainer

| The agent account gets | Stays with the maintainer's account |
|---|---|
| Its own checkout of the repository (a clone in a folder it owns or is granted), with no `ops/.env` | The deployment checkout with `ops/.env` (the owner's password, the operator tokens) |
| Its own Claude Code / Codex installs, logins and client configs | Docker Desktop (membership of `docker-users`), the compose stack, the daemon, backups, `pseudolife-mcp update` |
| Its own principal and bearer token: `pseudolife-mcp invite` by the maintainer, `pseudolife-mcp pair` by the agent account | `pseudolife-mcp invite`, `lease break`, `lease delegate`, `board-audit`, `test-login create`, and the maintainer passkey and its commands once that change lands |
| A copy of `test-pg.env` | The owner's password, and anything else in the maintainer's profile |
| Network access to `127.0.0.1:8765` (the daemon, gated by its bearer) and `127.0.0.1:5433` (Postgres, where only the test login works for it) | |

## Setting up the agent account (Windows)

Run these as the maintainer, in an elevated PowerShell. `<agent>` is the new
account's name and `<maintainer>` yours; paths are examples.

1. **A standard local account.** Settings, Accounts, Other users, or
   `net user <agent> * /add` (asks for the password). Do not add it to
   `Administrators` or `docker-users`. Check with `net user <agent>`: its
   local group memberships should be `Users` only.

2. **Its profile is private by default.** A standard account cannot read
   another account's `C:\Users\<name>`: the default ACL grants SYSTEM,
   Administrators and the owner. Keep the deployment checkout (and so
   `ops/.env`) inside your own profile, or give `ops/.env` an explicit
   ACL: `icacls ops\.env /inheritance:r /grant:r "<maintainer>:F" "SYSTEM:F" "Administrators:F"`.

3. **A repository it can work in.** Either a clone it makes itself (from
   GitHub, into its own profile), or a shared folder outside both profiles:

   ```powershell
   New-Item -ItemType Directory C:\work\pseudolife-agent
   icacls C:\work\pseudolife-agent /grant "<agent>:(OI)(CI)M"
   ```

   Clone into it as the agent account. Git refuses a repository owned by
   another account ("dubious ownership"); a clone the agent account makes
   is its own. If you read it from your account, `git config --global
   --add safe.directory C:/work/pseudolife-agent` there. Never share the
   deployment checkout: its `ops/` holds `.env`.

4. **Its own principal.** As the maintainer, on the daemon host:

   ```powershell
   pseudolife-mcp invite agent-desktop      # prints a short-lived, single-use code
   ```

   As the agent account, from its checkout, a client-only install against the
   local daemon (the URL is loopback, so client-only must be explicit):

   ```powershell
   ops\install.ps1 -ClientOnly -DaemonUrl http://127.0.0.1:8765 -Client claude -PairingCode <code>
   ```

   or, with the shim already installed, `pseudolife-mcp pair
   http://127.0.0.1:8765 <code>`. The token is minted on the agent account and
   written owner-only in its profile; only its SHA-256 reaches the daemon. See
   [Sharing one bank across machines](remote-bank.md#adding-a-machine-invite-and-pair).

5. **The test login file.** As the maintainer, `pseudolife-mcp test-login
   create` (once), then give the agent account a copy of
   `~/.pseudolife-mcp/test-pg.env` at its own
   `C:\Users\<agent>\.pseudolife-mcp\test-pg.env`, readable only by it. From
   the elevated prompt:

   ```powershell
   New-Item -ItemType Directory -Force C:\Users\<agent>\.pseudolife-mcp | Out-Null
   Copy-Item $HOME\.pseudolife-mcp\test-pg.env C:\Users\<agent>\.pseudolife-mcp\test-pg.env
   icacls C:\Users\<agent>\.pseudolife-mcp\test-pg.env /setowner <agent>
   icacls C:\Users\<agent>\.pseudolife-mcp\test-pg.env /inheritance:r /grant:r "<agent>:F" "SYSTEM:F"
   ```

   It is the test login's password, not the owner's: it opens no bank. After
   `--rotate`, copy it again.

6. **No secrets in machine-wide environment variables.** User variables
   (`PSEUDOLIFE_MCP_TOKEN` set by the installer, for one) are per account;
   anything under System variables is visible to every account. Check with
   `[Environment]::GetEnvironmentVariables('Machine')`.

## WSL and Docker Desktop

- The Docker engine on Windows answers members of `docker-users` (and
  administrators). Anyone who can reach it can read the bank's volume, so the
  agent account stays out of that group.
- Docker Desktop's WSL integration puts a working `docker` command into the
  WSL distributions it is enabled for. WSL distributions are registered per
  Windows account: the agent account installs its own (`wsl --install` as
  that account), and that distribution must not get Docker integration.
  Docker Desktop runs under, and its integration settings belong to, the
  maintainer's account. *Not verified here:* whether Docker Desktop can
  integrate with another account's distribution at all; check with `docker
  ps` inside the agent's distribution (below).
- The agent account's full suites run in its own distribution through
  `pwsh ops/wsl-suite.ps1`, which forwards its `test-pg.env` and copies no
  `ops/.env`. That distribution needs its own `uv`, environment and
  Hugging Face cache (see `ops/wsl-suite.sh`). Reaching `127.0.0.1:5433`
  from WSL needs the networking mode the maintainer's suites already use.
- The suite lock (`~/.pseudolife-mcp/locks/`) is per home directory, so the
  agent account's suites and the maintainer's do not see each other's lock.
  The board lease `full-suite` still mirrors both when each run has a bearer;
  check `pseudolife-mcp lease check full-suite` before a full run, as on any
  shared machine.

## The second machine (remote suites)

`ops/remote-suite.ps1` dispatches a run over SSH with the dispatching
account's key, to the box user in `full-suite.remote`. For agent sessions:

- Give the agent account its own SSH key, for a box user that is **not** in
  the `docker` group (membership there is root-equivalent) and cannot read the
  live stack's checkout or its `ops/.env`.
- That user's suites log in as configured in `env=` or, failing that, as the
  test login in its own `~/.pseudolife-mcp/test-pg.env`. For the box's
  separate test server, as the box's maintainer, give `--admin-url` a
  superuser URL without its password and let libpq read it from
  `PGPASSWORD` or `~/.pgpass`, so it stays off the command line and out of
  the shell history:

  ```bash
  read -rs PGPASSWORD && export PGPASSWORD   # the test server's superuser password
  pseudolife-mcp test-login create --admin-url postgresql://<superuser>@127.0.0.1:<port>/postgres
  unset PGPASSWORD
  ```
- A dispatched run wraps pytest in `sudo systemd-run` for its memory limit,
  and password-free `sudo` for `systemd-run` is root-equivalent (it can run
  any command as root). Such a user is not isolated. Until the launcher can
  use an unprivileged scope (`systemd-run --user`, *not implemented*), keep
  agent-dispatched runs off the box, or run them there without that rule.

## Verifying it, as the agent account

Sign in as the agent account (or `runas /user:<agent> pwsh`) and check:

| Check | Expected |
|---|---|
| `docker ps` | fails: the engine refuses an account outside `docker-users` |
| `Get-Content C:\Users\<maintainer>\<deployment checkout>\ops\.env` | access denied |
| `net user <agent>` | local group memberships: `Users` only |
| `pseudolife-mcp board-audit stats` (or any operator command that opens the bank) | fails: no database URL, and no owner password to make one |
| `python -c "import psycopg; psycopg.connect('postgresql://pseudolife_test:<password from test-pg.env>@127.0.0.1:5433/pseudolife_memory')"` | `permission denied for database "pseudolife_memory"` |
| `python -m pytest tests/test_pg_storage.py -q` in its checkout | passes, with no "as the bank owner" note |
| `pwsh ops/wsl-suite.ps1` (a full suite, in WSL) | passes |
| `pseudolife-mcp maintainer list` (once the maintainer-passkey change lands) | fails, for the same reason |

*Not verified on the maintainer's machine:* the Windows account steps and
the exact refusal texts above were written from the platform's documented
behaviour, not run as a second account. The test login itself was verified
on a scratch server of the same image (`pseudolife-pg:18`, PostgreSQL 18.6,
pgvector 0.8.6): it creates its databases, installs the schema, passes a
781-test slice of the PG-backed suite, and is refused by the bank.

## What this does not cover

- The daemon and the Console listen on loopback, so every local account can
  reach them; the bearer token (`"auth": true`) is what limits an account to
  its own principal.
- A test login can still fill the server's disk or exhaust its connections.
- The test login's databases sit beside the bank on one server; a server
  of their own (`PSEUDOLIFE_TEST_PG_HOST_PORT`, as the box does) separates
  them fully.
