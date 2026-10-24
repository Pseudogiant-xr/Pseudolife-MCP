# Self-hosted parity lane: the Rust parity shards on the dev machines

Status: comparison week started 2026-10-24. The first PR (#840) added the
workflow job, switched off. On 2026-10-24 the maintainer settled the
decisions below ("Decisions") and had a session set up the sibling Linux
dev box: three runners, rootless Docker, no LAN access, and a watcher that
keeps the repository variable on the online runners. The Windows dev VM
has no runner; its shards stay on hosted runners.

## Problem

The Rust workflow's parity shards wait a long time for hosted runners. Most
of a run's wall time is queueing, not testing. The maintainer's homelab has
dev machines that sit idle most of the day. This design runs the same
shards there as a comparison lane, which the hosted gate never depends on.
After a week of data, the maintainer decides whether any hosted shard
should move.

Measured on run 39194499378 (2026-10-19):

- The Windows Rust build took 18 minutes.
- Of the 24 parity jobs (12 suites on two systems), 12 started within
  2 minutes of their builds. The other 12 queued 11–22 minutes behind the
  account's hosted-job concurrency ceiling. The Windows `cli` shard started
  22 minutes late.
- The run took about 66 minutes of wall time.
- The workflow ran 30 times in the 7 hours around it.

Shard durations in that run, start to finish, show how much work a dev
machine would take on:

| Lane | Shards | Sum of durations |
|---|---|---|
| Hosted Linux | 12 | 116 min |
| Hosted Windows | 11 of 12 (`cli` unfinished) | 153 min, plus `cli` (about 50 min in earlier runs) |

One runner per machine runs its shards one at a time. A full Windows lane
therefore takes about 3.5 hours on one runner, and Linux about 2. That
fits a nightly comparison. It does not fit 30 runs in 7 hours, which is
why this lane stays off pull requests (see "When the lane runs").

## Machines

| Machine (role) | Runs the lane | Why |
|---|---|---|
| The Linux dev box that also serves the live daemon | **Never** | It carries the live daemon. CI load there competes with the bank's own service, which CLAUDE.md "Running tests" already guards with `MemoryMax`. |
| The sibling Linux dev box | Yes, label `pseudolife-parity-linux` | Idle most of the day; already has a suite env. |
| The Windows dev VM | Yes, label `pseudolife-parity-windows` | Idle; the place CLAUDE.md sends Windows CI failures anyway. |

Machine names, addresses and SSH aliases stay in private config and the
memory bank ("dev machines homelab"), never in this tree. Below,
`<runner-user>` is the new account the runner runs as, `<suite-user>` is the
account that runs full suites on that machine, and `<machine>` is the host
part of that machine's suite lease (`full-suite@<machine>`).

## What the first PR adds

One job in `.github/workflows/rust.yml`, `parity-selfhosted`:

- **Same shards, same steps.** YAML anchors share `parity-checks`' suite
  list, its `include` shard table and its whole `steps:` list
  (`&parity_steps`, `*parity_steps`). A step added to the hosted shard
  reaches the self-hosted lane in the same edit. `rust/ci/test_workflow.py`
  pins that the lane uses the aliases and not a copy.
- **Two self-hosted-only steps** in the shared list, each guarded by
  `if: runner.environment == 'self-hosted'`, so hosted jobs skip them:
  - *Yield to this machine's full suite* runs first, before checkout (see
    "Yielding to a full suite").
  - *Give the shard its own Python environment* makes a venv under
    `RUNNER_TEMP`. Without it, every job on a persistent runner would
    install into the same tool-cache Python, and one shard's packages
    would mask another's missing dependency.
- **Distinct artifact names.** Artifact names must be unique within a run.
  Every upload in the shared steps is named
  `${{ env.PARITY_ARTIFACT_PREFIX }}<name>`. The hosted job sets the
  prefix to `''`, so its names do not change; the lane sets `self-hosted-`.
- **No repository-cache writes from self-hosted.** `setup-python`'s pip
  cache and `rust-cache`'s save run only when `runner.environment` is not
  `self-hosted`. Self-hosted entries would compete for the repository's
  cache quota with the hosted Windows Rust cache, whose cold rebuild takes
  26–34 minutes. Both expressions evaluate exactly as before on hosted
  runners.
- **Not a gate.** The job has `continue-on-error: true`. It is absent from
  the `parity` gate's `needs`, and no other job needs it. The ruleset's
  required check (the Python `test` job) and the Rust `Parity / <os>`
  gates are unchanged.
- **Off by default.** The job runs only when the repository variable
  `PARITY_SELFHOSTED_RUNNERS` holds a JSON list of registered runners,
  such as `["linux", "windows"]`. A job routed to a label that no runner
  serves queues for 24 hours before GitHub fails it, so the variable is
  set only after registration.

### When the lane runs

Only on the nightly (`schedule`) and manual CI (`workflow_dispatch` with
`mode: ci`). Both have their own concurrency groups.

- **Not on pull requests.** Pending jobs would hold a PR's checks open for
  hours, and a failure would show as a failed check. The delegate's watcher
  (`checks_state` in `pseudolife_memory/delegate_watch_cli.py`) reads any
  pending check as pending and any failed one as failing, whatever
  `continue-on-error` says. Every PR would look unfinished or red.
- **Not on master pushes.** Master pushes share one group with
  `cancel-in-progress`. A run with self-hosted jobs still queued would stay
  open for hours, and the next push would cancel it, hosted results and
  all. The master run that the merge-forward rules watch would then end
  "cancelled" far more often.
- **Never fork code.** Schedule runs master. Only someone with write access
  can dispatch.

### Yielding to a full suite

A full suite owns its machine (CLAUDE.md "Running tests": one full suite
per machine, and no saturating work beside one). The gate step:

1. Reads `PSEUDOLIFE_PARITY_LEASE` (must match `full-suite@<machine>`) and
   `PSEUDOLIFE_PARITY_LOCK_DIR` from the runner's environment. A missing
   or malformed value fails the job with a message naming both settings.
2. Requires the machine's own `pseudolife-mcp` on the runner's `PATH`. The
   machine's installed release decides, not the code under test, and it
   answers before any checkout or install.
3. Fails if the work drive has under 8 GB free (CLAUDE.md "Disk space").
4. Runs `pseudolife-mcp lease check --json <lease>`, with
   `PSEUDOLIFE_SUITE_LEASE` and `PSEUDOLIFE_SUITE_LOCK_DIR` set for that
   one process only. The shard's tests never see them.
   - Exit 0 (free): run the shard.
   - Exit 1 (held): check again every 60 seconds for up to 60 minutes
     (`PARITY_YIELD_MINUTES`). Then fail with "Yielded", with a line in
     the job summary saying the shard did not run.
   - Any other exit: fail. Neither held nor free is known.
   - A report with no readable local lock file fails too. A wrong lock
     directory, or a missing read grant, would otherwise read free on every
     probe.

The runner holds no board credential, so the check reads the OS lock
alone. That is the lease's own truth for a process-held suite: the board
mirror only reflects it.

`rust/ci/test_workflow.py` runs the gate script under `pwsh` with a stub
CLI. It covers free, held-then-free, yield, check failure, missing lock
file, the Windows host's bare `full-suite` lease, missing lock directory,
missing CLI and low disk. Each guard was disabled once and its test was
seen to fail.

What the gate does not cover, by design:

- **A suite that starts mid-shard.** The shard finishes beside it. The
  lane only reads the lease, it never holds it, so a suite always gets its
  machine without waiting. The cost is a stretch of overlap, at most one
  shard's length (5–60 minutes). The suite's timing can suffer in that
  window. Decision 5 below offers the alternative.
- **The hypervisor's two-full-suite cap.** A shard is not a full suite,
  and the gate checks only its own machine's lease. Gating on the other
  dev machines' leases needs a board credential on the runner, which this
  design avoids (decision 6).

## Isolation

- **A dedicated, unprivileged OS account per machine** (`<runner-user>`).
  It has no sudo, and no access to the suite user's home beyond read
  access to the lock directory. It holds no bank credential: no
  `PSEUDOLIFE_MCP_DATABASE_URL`, no `ops/.env`, no `test-pg.env`, no
  bearer.
- **Disposable PostgreSQL only.** Every shard that needs PostgreSQL starts
  its own embedded cluster on loopback (`rust/cli_harness/lease_ci.py`,
  `attach_or_start`), on a port of its choosing, with a test login it
  writes under `RUNNER_TEMP`. The sibling box's separate test server
  belongs to the suite user, and the runner never gets its password. The
  live bank runs on neither lane machine.
- **A clean start for each job.**
  - The checkout action cleans the work tree (`git clean -ffdx`).
  - `RUNNER_TEMP` is emptied between jobs.
  - The shard's venv lives in `RUNNER_TEMP`.
  - At the end of each job the runner stops every process that still
    carries the job's tracking variable. The embedded PostgreSQL
    clusters inherit it.
  - One leftover remains: `lease_ci.py` puts each cluster's data
    directory in the account's home (`~/pl-cli-lease-postgres-*`). A
    job-started hook deletes those before the next job (below).
- **Persistent runners rather than ephemeral ones.** An ephemeral runner
  re-registers after every job, so the machine would need a stored
  credential that can mint registration tokens. A persistent runner keeps
  only its own runner credential (`.credentials`, `.runner` in its
  directory). Anyone who can read those files can impersonate the runner,
  so they stay readable by `<runner-user>` alone. Ephemeral runners are
  decision 7.

## Secrets

None beyond what hosted shards get. The workflow's `permissions:` stays
`contents: read`, and the job references no secret. The registration token
the maintainer creates expires in an hour and is used once, on the machine.
No repository secret is added. The lane's only new setting is a repository
**variable**, which is not secret.

## Public repository risk

GitHub advises against self-hosted runners on public repositories.
Whatever trigger this job uses, a pull request from a fork can edit
`rust.yml`, and a `pull_request` run uses the PR's own workflow file. Such
a PR could route a job of its own to `pseudolife-parity-*` and run code on
a dev machine.

The repository setting that stops this is under **Settings → Actions →
General**: approval for fork pull request workflows set to **"Require
approval for all external contributors"**. A fork's workflow changes then
run only after the maintainer approves them. The REST endpoint
`repos/Pseudogiant-xr/Pseudolife-MCP/actions/permissions/fork-pr-contributor-approval`
should report `all_external_contributors`; the settings page is the
authority if the endpoint differs.

This setting is a **precondition for registering any runner**, not an
option (decision 1). The stronger control, a runner group limited to
selected workflows and refs (only `rust.yml` on master), cannot be set up
for a repository owned by a personal account, which this one is. A guard job inside `rust.yml` would not help either: a
fork that edits the workflow can delete the guard in the same edit. What
limits an approved run's reach is the dedicated account and the absence of
credentials on the machine.

## Setting up the sibling Linux dev box (scripted, 2026-10-24)

`ops/selfhosted/setup-linux-runner.sh`, run as root on the box, does every
Linux step of the manual procedure below and adds what running several
runners and Docker safely needs. Rerunning it is safe:

```bash
setup-linux-runner.sh --runner-user <runner-user> --suite-user <suite-user> \
  --lease full-suite@<machine> --runners 3 --version <x.y.z> --sha256 <hex>
# then, to register, with a token from the command in the next section:
RUNNER_TOKEN=<registration token> setup-linux-runner.sh ...same arguments...
```

What it adds to the manual procedure:

- **Rootless Docker** for `<runner-user>` (decision 3). Its image store is
  its own, so the `w1c-connection` shard's `pseudolife-pg:18` build never
  replaces the root engine's image of that name. In an unprivileged
  container only IDs 0–65535 are mapped, so the account's subordinate IDs
  are 30000–64999, not `useradd`'s default range past 65535 (with which
  `newuidmap` fails).
- **No local network.** An nftables table, loaded by its own systemd unit,
  rejects `<runner-user>`'s IPv4 traffic to private, link-local, CGNAT
  (Tailscale), multicast and broadcast addresses, and all its IPv6 traffic
  but loopback (the box has no global IPv6 route, and a LAN that gains a
  global prefix stays covered). Jobs, and their rootless
  containers (whose traffic leaves through that account's `slirp4netns`),
  reach the internet but not the live bank, the hypervisor or other
  guests. It does not use the box's `nftables.service`, whose stock
  configuration flushes every table, the root Docker engine's included.
  - The rules match the account's subordinate IDs as well as its own: a
    job can map itself onto one in a user namespace that keeps the box's
    network namespace.
  - The box's own services are refused too: every port another account
    listens on when the script runs (an SSH server, a test PostgreSQL, a
    mail relay), on every local address. A port in use is never one a
    job's own server binds. Rerun the script after adding a service.
  - The runner units are bound to the rule's unit (`BindsTo=`), so they
    do not start without it and stop when it stops. A rerun reloads the
    table in one transaction, never leaving the account unfiltered.
  - DNS works because the box resolves through a public resolver. A box
    whose resolver is on the LAN or Tailscale would need that address
    allowed.
- **Root never runs or writes the account's files after creating them.**
  A job may have replaced any file in the account's home, so the service
  units are written by the script, not by the runner's `svc.sh` (which
  root would run from that directory), the job-started hook is root's
  (`/usr/local/lib/pseudolife-parity-runner/`), and `.env` and `.path` are
  written as the account. What this does not give: every job runs as the
  one account, so a job can change what later jobs run (the account's
  tools, tool cache, `.env` files and rootless Docker). The lane runs only
  master's code and dispatches by someone with write access, and the
  account can reach nothing on the network that matters, so that is
  accepted; ephemeral runners would remove it (decision 7).
- **Reruns leave running jobs alone.** A runner restarts only when its
  `.env`, `.path` or unit changed, or it is down.
- **Several runners per machine, each with its own `HOME`**
  (`runners/<n>/home`, set in the runner's `.env`). The job-started hook
  deletes `$HOME/pl-cli-lease-postgres-*`; with one shared `HOME`, one
  runner's hook would delete another's running job's clusters. The hook
  stops a postgres only when its working directory is that cluster's data
  directory, so a PID reused by another runner's cluster is left alone.
  The cost is per-runner caches: pip's, and the Rust toolchain the shards
  that build install under `HOME` (about 1.5 GB each).
- **Python 3.11 in a shared tool cache** (`AGENT_TOOLSDIRECTORY`).
  `actions/setup-python` ships builds for Ubuntu only, so the script copies
  a standalone CPython 3.11 build from `uv` into the cache layout
  `setup-python` looks up first.

Verified on the box on 2026-10-24 as `<runner-user>`: the hypervisor's
address and a Tailscale address were refused, from the account and from a
rootless container; the internet answered; the suite user's home was
unreadable and its lock directory readable; `lease check` read the lock as
free; the root engine's images were not visible. The three runners came
online. Untested until the first nightly: the shards themselves on this
box (glibc, the tool-cache Python, `docker build` under rootless Docker).

## The watcher (decision 11)

`ops/selfhosted/runner_watch.py` lists a machine in
`PARITY_SELFHOSTED_RUNNERS` while one of its runners is online, and for 10
minutes after its last one was seen online, so a runner's update restart
does not drop it. With none online it deletes the variable, which switches
the lane off. It writes only when the value is wrong, and an API error
changes nothing.

`ops/selfhosted/setup-runner-watch.sh` installs it as a systemd timer
(every two minutes) under a system account of its own. It runs on a dev
box that is not a runner, because a machine that goes down cannot take
itself out. Its token is a fine-grained token for this repository alone,
with "Administration: read" (listing runners needs it) and "Variables:
read and write", stored readable by the watcher's account only:
`setup-runner-watch.sh --token < <token file>`. Until it is stored the
timer's runs are skipped.

What it cannot do: jobs already queued for a machine that then goes down
stay queued. GitHub has no API to cancel one job of a run, so the
nightly's next run still cancels them ("Switching the lane on").

## Commands for the maintainer

These are the manual steps behind the script above, and still the procedure
for the Windows dev VM. Use the runner release currently listed at
`https://github.com/actions/runner/releases`, and check its SHA-256 against
that page.

### On the maintainer's own machine, for each registration

```bash
gh api -X POST repos/Pseudogiant-xr/Pseudolife-MCP/actions/runners/registration-token --jq .token
```

The token expires after an hour and lets a runner join this repository
only. Create it just before registering.

### The sibling Linux dev box

First, as `<suite-user>`: name the machine's lease (this box had no lease
file as of 2026-10-10) and make sure the lock file exists. The gate fails
closed without it.

```bash
mkdir -p ~/.pseudolife-mcp/locks
echo 'full-suite@<machine>' > ~/.pseudolife-mcp/locks/full-suite.lease
touch ~/.pseudolife-mcp/locks/full-suite.lock
```

Then as root:

```bash
useradd --create-home --shell /bin/bash <runner-user>
# PowerShell 7 (the shards' steps use shell: pwsh) and the en_US.UTF-8
# locale the Linux shards set; install pwsh from Microsoft's package feed.
locale-gen en_US.UTF-8
# Read-only access to the suite lock directory (the default ACL covers
# lock files created later):
setfacl -m u:<runner-user>:x /home/<suite-user> /home/<suite-user>/.pseudolife-mcp
setfacl -m u:<runner-user>:rx /home/<suite-user>/.pseudolife-mcp/locks
setfacl -d -m u:<runner-user>:r /home/<suite-user>/.pseudolife-mcp/locks
setfacl -m u:<runner-user>:r /home/<suite-user>/.pseudolife-mcp/locks/*
```

As `<runner-user>`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# The installer only edits shell startup files. Put uv's bin directory on
# PATH now, so config.sh records it for the service.
source "$HOME/.local/bin/env"
uv tool install pseudolife-mcp
mkdir ~/actions-runner && cd ~/actions-runner
curl -LO https://github.com/actions/runner/releases/download/v<version>/actions-runner-linux-x64-<version>.tar.gz
tar xzf actions-runner-linux-x64-<version>.tar.gz
./config.sh --unattended --url https://github.com/Pseudogiant-xr/Pseudolife-MCP \
  --token <registration token> --name parity-linux --labels pseudolife-parity-linux --work _work
cat >> .env <<'ENV'
PSEUDOLIFE_PARITY_LEASE=full-suite@<machine>
PSEUDOLIFE_PARITY_LOCK_DIR=/home/<suite-user>/.pseudolife-mcp/locks
ACTIONS_RUNNER_HOOK_JOB_STARTED=/home/<runner-user>/actions-runner/job-started.sh
ENV
```

`~/actions-runner/job-started.sh`, mode 0755. It removes the previous
job's PostgreSQL data directories. Every process it stops is owned by this
account, named by that directory's own `postmaster.pid`, and running in that
data directory, so a reused PID is never stopped. (The script above puts
the hook in a root-owned directory instead, where jobs cannot change it.)

```bash
#!/usr/bin/env bash
set -u
me=$(id -un)
for dir in "$HOME"/pl-cli-lease-postgres-*/; do
  [ -d "$dir" ] || continue
  while IFS= read -r pidfile; do
    pid=$(head -n 1 "$pidfile")
    data=$(dirname "$(readlink -f "$pidfile")")
    if [[ "$pid" =~ ^[0-9]+$ ]] && [ "$(stat -c %U "/proc/$pid" 2>/dev/null)" = "$me" ] \
       && [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$data" ]; then
      kill -INT "$pid"; sleep 5
    fi
  done < <(find "$dir" -maxdepth 4 -name postmaster.pid)
  rm -rf -- "$dir"
done
exit 0
```

Then, as root, from `~<runner-user>/actions-runner`:

```bash
./svc.sh install <runner-user>
./svc.sh start
```

The Linux `w1c-connection` shard builds a container image and starts
disposable containers. It needs a Docker engine the runner account can
use (decision 3).

### The Windows dev VM

In an elevated PowerShell 7:

```powershell
$password = Read-Host -AsSecureString 'Password for the runner account'
New-LocalUser -Name <runner-user> -Password $password -PasswordNeverExpires
icacls "C:\Users\<suite-user>\.pseudolife-mcp\locks" /grant "<runner-user>:(OI)(CI)R"
icacls "C:\Users\<suite-user>" /grant "<runner-user>:(X)"
icacls "C:\Users\<suite-user>\.pseudolife-mcp" /grant "<runner-user>:(X)"
```

As `<suite-user>`, once: check that
`~\.pseudolife-mcp\locks\full-suite.lease` reads `full-suite@<machine>`
and that `full-suite.lock` exists beside it. Create either if missing, as
on the Linux box.

Signed in as `<runner-user>`, install `uv`, then run
`uv tool install pseudolife-mcp`.

Install the runner, in an elevated PowerShell 7. Without `--unattended`,
`config.cmd` asks for the account's password itself, so the password
stays out of the command line and the shell history:

```powershell
New-Item -ItemType Directory C:\actions-runner | Out-Null
Set-Location C:\actions-runner
Invoke-WebRequest https://github.com/actions/runner/releases/download/v<version>/actions-runner-win-x64-<version>.zip -OutFile runner.zip
Expand-Archive runner.zip -DestinationPath .
.\config.cmd --url https://github.com/Pseudogiant-xr/Pseudolife-MCP `
  --token <registration token> --name parity-windows --labels pseudolife-parity-windows `
  --work C:\actions-runner\_work --runasservice --windowslogonaccount ".\<runner-user>"
@'
PSEUDOLIFE_PARITY_LEASE=full-suite@<machine>
PSEUDOLIFE_PARITY_LOCK_DIR=C:\Users\<suite-user>\.pseudolife-mcp\locks
ACTIONS_RUNNER_HOOK_JOB_STARTED=C:\actions-runner\job-started.ps1
'@ | Add-Content C:\actions-runner\.env
icacls C:\actions-runner /grant "<runner-user>:(OI)(CI)M"
```

`config.cmd` records the elevated shell's `PATH` in
`C:\actions-runner\.path`, which does not include the runner account's
tools. Append `;C:\Users\<runner-user>\.local\bin` to the single line in
that file, then restart the runner service so it reads `.env` and `.path`.

`C:\actions-runner\job-started.ps1` does the same job as the Linux hook:

```powershell
$me = "$env:USERDOMAIN\$env:USERNAME"
foreach ($dir in Get-ChildItem -Directory -Path $HOME -Filter 'pl-cli-lease-postgres-*') {
  foreach ($pidfile in Get-ChildItem -Recurse -Depth 3 -Path $dir.FullName -Filter postmaster.pid) {
    $id = [int](Get-Content -LiteralPath $pidfile.FullName -TotalCount 1)
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id"
    if ($proc -and $proc.Name -eq 'postgres.exe') {
      $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner
      if ("$($owner.Domain)\$($owner.User)" -eq $me) { Stop-Process -Id $id; Start-Sleep 5 }
    }
  }
  Remove-Item -Recurse -Force -LiteralPath $dir.FullName -ErrorAction Continue
}
exit 0
```

### Switching the lane on, and the first run

```bash
gh variable set PARITY_SELFHOSTED_RUNNERS --repo Pseudogiant-xr/Pseudolife-MCP --body '["windows"]'
gh workflow run rust.yml --repo Pseudogiant-xr/Pseudolife-MCP --ref master -f mode=ci \
  -f frozen_head="$(gh api repos/Pseudogiant-xr/Pseudolife-MCP/commits/master --jq .sha)"
```

Enable one machine first and add the second after its first clean run:
`'["linux", "windows"]'`. To switch the lane off, delete the variable
(`gh variable delete PARITY_SELFHOSTED_RUNNERS`). The job then skips, and
no queued job is left waiting for a runner.

**Take a machine out of the variable as soon as it goes offline.** A job
routed to an offline runner queues for up to 24 hours. The nightly's
concurrency group cancels the previous nightly when the next one starts.
So while a listed machine is down, every nightly ends "cancelled" rather
than "failure", and a real hosted nightly failure in `parity-checks` or
`source-mutants` sends no failure notice.

Check these on the first dispatched run. Each is an assumption this
design could not test without a machine:

- The Linux binaries, built on hosted `ubuntu-latest`, start on the
  sibling box. An older glibc there fails them at exec.
- `actions/setup-python` installs 3.11 into the runner's tool cache.
  - On a Linux distribution other than Ubuntu, pre-seed the tool cache.
  - On Windows, run the first install with enough rights to write it.
- The gate step prints "free" and the lease name.
- `lease check` sees a held lock: start a full suite on that machine, then
  dispatch, and the gate should wait.
- Artifacts appear with the `self-hosted-` prefix beside the hosted ones.

## Rollout

1. **This PR**: the job, switched off. No runner, no variable, no change
   to any hosted result.
2. **Registration** (maintainer): the commands above, then a dispatched
   run on one machine, then the other.
3. **One week of comparison**, nightly plus any manual dispatches. For
   each shard, compare the two lanes on the same commit:
   - pass/fail agreement;
   - wall time, and time queued before it started;
   - how many times it yielded;
   - failures on one lane only, each reproduced or explained (a
     self-hosted-only failure goes to the Windows VM procedure in
     CLAUDE.md "Dev machines" like any other);
   - the machine's peak memory and free disk while the lane ran.

   The job names differ (`Parity self-hosted / <runner> / <suite>`), so
   `rust/ci/timings.py`'s run export separates the lanes.

   Wall time is biased against the lane. Self-hosted jobs never save the
   repository Rust cache, and checkout's `git clean` wipes `rust/target`
   between jobs, so every shard whose harness calls `cargo` builds cold.
   Hosted shards restore a warm cache. Compare the steps that do not
   compile, or give the runner a persistent `CARGO_TARGET_DIR` (decision 10)
   before reading too much into the totals.
4. **The maintainer decides** whether the lane:
   - replaces some hosted shards;
   - stays a nightly comparison; or
   - is removed.

   Hosted runners stay the required checks until the maintainer decides
   otherwise.

A note for that decision. Replacing the hosted Windows shards on pull
requests would bring back the two problems this design keeps the lane
away from:

- A self-hosted job on every PR has to be fast enough not to hold PRs
  open. That means several runners per machine; one VM runs 12 Windows
  shards in about 3.5 hours.
- It has to be safe against fork PRs, which depends on the approval
  setting.

A smaller win may be more realistic: move only nightly-only work (the
source-mutant matrix, 74 jobs per nightly) to the dev machines. That
frees hosted capacity without touching the PR path. The week's numbers
should decide.

## Decisions

Settled by the maintainer on 2026-10-24:

- **1, fork approval:** set to "Require approval for all external
  contributors"; the endpoint reads `all_external_contributors`. A
  `gh api -X PUT ... -f approval_policy=...` call returned success without
  changing it; a JSON body (`--input`) did. Read the setting back after
  changing it.
- **2, runner account:** a dedicated unprivileged account.
- **3, Docker:** rootless Docker for that account.
- **4, runners per machine:** three on the sibling Linux box, whose disk
  was grown by 30 GB for them. One check rarely keeps more than one or two
  of its 8 cores busy; memory and disk are the limits. The Windows dev VM
  gets none for now: its disk is the hypervisor's old spinning one, and it
  stays free to reproduce Windows CI failures.
- **7, persistent runners** with the job-started hook.
- **11, offline machines:** the watcher above.

Still open: 5, 6, 8, 9 and 10. The original options follow.

1. **Fork PR workflow approval**: "Require approval for all external
   contributors", set and verified before any runner is registered. This
   design treats it as a precondition: it is the only control that keeps a
   fork's edited workflow off the dev machines.
2. **Runner account**: a dedicated unprivileged account per machine
   (recommended), or the suite user's account, which is simpler but gives
   jobs that account's files.
3. **Docker on the sibling box** for the Linux `w1c-connection` shard:
   - rootless Docker for the runner account (recommended);
   - membership of the `docker` group, which is root-equivalent on that
     box. The shard also runs `docker build -t pseudolife-pg:18`, the tag
     `ops/docker-compose.yml` uses. On a shared root engine, a CI build
     would replace that image for any stack on the box that uses it; or
   - none, in which case that one shard fails at its image build and is
     left out of the comparison.
4. **Runners per machine**: one each for the trial (recommended).
5. **Yield policy**: the implemented policy waits up to 60 minutes, then
   fails as "Yielded", and lets a suite that starts mid-shard overlap it.
   The alternative is for the shard to hold a lease of its own that suite
   launchers check before starting, which makes suites wait.
6. **The hypervisor's two-suite cap**: treat shards as outside it
   (recommended), or gate each shard on the other dev machines' leases,
   which needs a board credential on every runner.
7. **Persistent runners with the job-started hook** (recommended), or
   ephemeral runners, which need a token-minting credential stored on each
   machine.
8. **The nightly's timing**: `23 3 * * *` UTC falls in the maintainer's
   afternoon. The machines must be free then, or the lane mostly yields.
9. **After the week**: replace, keep as comparison, or remove (see
   "Rollout").
10. **A persistent Rust build cache on the runners** (a fixed
    `CARGO_TARGET_DIR` per machine, as CLAUDE.md "Disk space" suggests), to
    remove the cold-build bias from the comparison. Not in the first PR:
    harness steps read `rust/target` paths directly, so it needs its own
    check.
11. **Offline-machine handling**: accept that an offline listed runner
    turns nightlies "cancelled" until it is taken out of the variable (as
    designed), or ask for a watcher that does that automatically.
