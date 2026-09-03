# Docker disk retention — operator runbook

Two separate problems on the deploy host; only one of them is automated.

## 1. Build cache grows without bound (AUTOMATED)

Every daemon rebuild adds BuildKit cache entries, and nothing ever removed
them. Measured 2026-07-28: 51.87GB across 169 entries had accumulated,
every entry inactive, some 5-6 weeks old — a single deploy alone adds
~12.45GB across 17 entries. After the 2026-07-14 manual trim, ~52GB regrew
over the following 13 days: manual cleanup doesn't stick, which is why this
now runs on its own.

Build cache is pure derived data — the only cost of pruning it is rebuild
time on whatever `docker build` runs next — so retention is safe to
automate aggressively.

**What runs, and when:**

| Trigger | Script | Default policy |
|---|---|---|
| After every healthy deploy | `ops/update.ps1` / `.sh`, step 5 (last) | age 168h, 20GB ceiling, fstrim |
| Weekly, if registered | Scheduled Task → `ops/prune-build-cache.ps1` | age 168h, 20GB ceiling, fstrim |

The deploy hook only passes `-MaxAgeHours`; `-MaxUsedSpaceGB` therefore
takes its own default (20GB), so the two triggers enforce the identical
ceiling, and both run `fstrim` (neither passes `-NoTrim`).

### The deploy hook

`ops/update.ps1` / `ops/update.sh` call the prune script as their last
step, deliberately after the `/health` check passes:

- Before the build, pruning would delete the cache the build itself
  reuses — cold-starting every deploy.
- On the unhealthy path it would strip the cache an operator's rollback
  rebuild needs — but that branch `exit`s before reaching this step, so
  retention never runs against a failed deploy.
- The call is wrapped so a retention failure only warns
  (`Write-Warning` / a stderr `WARNING:` line) and never fails a deploy
  that already succeeded.

Flags on the deploy script:

```powershell
.\ops\update.ps1                      # default: prune, keep 168h
.\ops\update.ps1 -KeepCacheHours 24   # tighter retention window for this run
.\ops\update.ps1 -NoCachePrune        # skip retention entirely
```
```bash
./ops/update.sh                          # default: prune, keep 168h
./ops/update.sh --keep-cache-hours 24    # tighter window for this run
./ops/update.sh --no-cache-prune         # skip retention entirely
```

### The weekly Scheduled Task

Covers the gap the deploy hook can't reach: stretches with no deploys at
all, which is exactly how 51.87GB piled up by 2026-07-28.

```powershell
.\ops\install-cache-retention.ps1                          # Sunday 03:00, defaults
.\ops\install-cache-retention.ps1 -DayOfWeek Wednesday -At 21:00
.\ops\install-cache-retention.ps1 -Unregister              # remove it
```

`-MaxAgeHours` / `-MaxUsedSpaceGB` on the installer pass straight through
to the registered `prune-build-cache.ps1` call (same defaults: 168h /
20GB). The task is named `Pseudolife-MCP Docker cache retention` and runs
`pwsh.exe` with a base64-encoded command; `StartWhenAvailable` is set on
purpose — a desktop is often off at 03:00 on a Sunday, and a run that
silently never happens is the exact failure this task exists to close.

**Run this installer from the permanent checkout, not a git worktree.**
It bakes `$PSScriptRoot` — its own directory *at registration time* — into
the command Task Scheduler stores. It was registered once from a worktree
during development, purely to verify it works, then **deliberately
unregistered before merge**: a worktree is deleted once its branch merges,
and a task still pointing at that path would then fail silently every
Sunday with nothing to alert an operator. Register it from wherever this
repository is permanently checked out, and **re-run the installer any time
that checkout's location changes** — `Register-ScheduledTask -Force`
overwrites the existing registration in place, so re-running is always
safe.

This and the MSIX `pwsh.exe` resolution below are the two ways this
feature quietly stops working: nothing fails loudly if the registered
task's target no longer exists, it just never runs again.

**Check whether it's actually running:**

```powershell
Get-ScheduledTaskInfo -TaskName 'Pseudolife-MCP Docker cache retention'
```

- `LastTaskResult` of `0` means the last run exited cleanly.
- `LastTaskResult` of **`267011`** (`0x41303`, `SCHED_S_TASK_HAS_NOT_RUN`) is
  **not a failure** — it means the task has never run yet, which is the normal
  state between registration and the first Sunday. It ships alongside a
  `LastRunTime` of `30/11/1999` (Task Scheduler's "never") and a
  `NumberOfMissedRuns` of `0`. Don't chase this as a phantom failure; if the
  task was registered this week, this is exactly what you should see.
- Any *other* non-zero `LastTaskResult`, paired with a real `LastRunTime`,
  means the script threw or exited non-zero on that invocation.
- `LastTaskResult` of **`2147942402`** (`0x80070002`, file not found) with a
  real `LastRunTime` means Task Scheduler could not find `pwsh.exe` at all:
  the action was registered as a bare `pwsh.exe`, which resolves against the
  MACHINE path, and a Store/MSIX PowerShell 7 install does not appear there.
  The task registers, reports Ready, and fires on schedule while every run
  dies before the retention script starts — found live 2026-08-20 with the
  cache at 23.6 GB against its 20 GB ceiling. Fixed 2026-08-20 (the installer
  now registers an absolute path, preferring the stable per-user app-execution
  alias `%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe` over the versioned
  package directory, which changes on every Store update). **A registration
  made before that date keeps the defect — re-run
  `ops\install-cache-retention.ps1` once to pick up the fix.**
- `LastRunTime` should be within the last week once the task has run at all;
  right after a Sunday the machine was off for, expect it shortly after the
  next boot instead, courtesy of `StartWhenAvailable`.

**`NextRunTime` cannot detect the failure this section exists for.** It is
computed from the trigger alone, so a task whose action points at a deleted
worktree still reports a healthy, upcoming `NextRunTime` every week while
never doing anything. To actually check the registration is live, verify the
baked-in script path still exists:

```powershell
$t = Get-ScheduledTask -TaskName 'Pseudolife-MCP Docker cache retention'
$cmd = [Text.Encoding]::Unicode.GetString(
    [Convert]::FromBase64String(($t.Actions[0].Arguments -split '-EncodedCommand ')[-1]))
$path = ([regex]::Match($cmd, "'([^']+\.ps1)'")).Groups[1].Value
"target: $path"
"exists: $(Test-Path $path)"
```

`exists: False` — or a `target:` under a worktree path — means re-run
`ops\install-cache-retention.ps1` from the permanent checkout. That is the
check to run; `NextRunTime` only tells you a trigger exists.

Windows-only — there is no cron/systemd-timer installer. On Linux/macOS
the deploy hook (`ops/update.sh`) is the only automated trigger; run
`ops/prune-build-cache.sh` by hand, or wire it into your own cron entry,
to cover stretches with no deploys.

### Running retention by hand

```powershell
.\ops\prune-build-cache.ps1 -DryRun     # report only; mutates nothing
.\ops\prune-build-cache.ps1 -NoTrim     # prune, but skip the fstrim step
.\ops\prune-build-cache.ps1             # age pass, ceiling pass, fstrim
```
```bash
./ops/prune-build-cache.sh --dry-run
./ops/prune-build-cache.sh --no-trim
./ops/prune-build-cache.sh
```

Both scripts also accept `-MaxAgeHours` / `--max-age-hours` (default 168,
validated 0–876000) and `-MaxUsedSpaceGB` / `--max-used-space-gb` (default
20, validated 0–100000).

`-DryRun` / `--dry-run` prints what would run, plus an *estimated* reclaim
for the age pass, and executes nothing — no prune, no fstrim. The estimate
sums each cache entry's `CreatedAt`; BuildKit's own `until=` filter
actually keys on last-used time, and shared layers may not free in full,
so read the dry-run number as a floor, not a promise.

**What runs, in order, on a real (non-dry-run) invocation:**
1. Age pass — always: `docker builder prune --all --force --filter
   until=<N>h`.
2. Ceiling pass — only if the cache is still over the size cap *after* the
   age pass: `docker builder prune --all --force --max-used-space <cap>`,
   repeated (re-measure between passes, max 5). Three exits, only the
   first quiet: under the ceiling (done); a pass makes no progress —
   the remaining cache is pinned (live images or a running build) —
   warns; all 5 passes spent while still over the ceiling warns too.
   Warnings never fail the run: it still exits 0.
3. `fstrim` of the WSL disk (`wsl -d docker-desktop -e sh -c "fstrim -v
   /mnt/docker-desktop-disk"`) — Windows-only in effect, skipped quietly
   (not a failure) if `wsl` isn't on `PATH` (the case on Linux/macOS), or if the
   `docker-desktop` distro isn't present. `-NoTrim` / `--no-trim` skips
   this step outright. A failed fstrim only warns; it never fails the run.

**Why age and not "reclaimable".** Minutes after a deploy, `docker builder
du` reports the whole fresh cache as reclaimable — reclaimable means "not
pinned by a running build", not "not worth keeping". A policy keyed on
that signal would delete hot cache and cold-start the very next build. The
168h window keeps the week of cache that actually gets reused.

**`--all` is load-bearing on every prune — without it, prune removes
nothing at all under the containerd image store.** Measured 2026-08-06
(Docker Desktop, engine 29.6.2, buildx 0.35, `driver-type:
io.containerd.snapshotter.v1`): every non-`--all` form of `docker builder
prune` — age-filtered, `--max-used-space`-capped, or bare — exited 0 and
reported `Total: 0B`, whatever the cache held. Both retention passes ran
as silent no-ops from the script's introduction until then, letting the
cache grow to 38.95GB against the 20GB ceiling while every run reported
success. With `--all`, the otherwise-identical commands reclaimed ~20GB
(38.95GB -> 18.22GB), fstrim returned 20.4GiB to the vhdx free list, and
the live images were untouched. The 2026-07-28 measurement — `docker
builder prune --force --max-used-space 8000000000` reclaiming **0 B**
against a 12.45GB cache, `--reserved-space 0` likewise — attributed the
0 B to layer sharing (14 of 17 entries `Shared=true`); the dominant cause
was the missing `--all`. This is consistent with the original manual
cleanup: the 51.87GB backlog was reclaimed with `docker builder prune
-af`, `-a` included.

**One ceiling pass can stop well above the target.** Cache-record parent
chains unwind one pass at a time, and containerd's GC frees space
asynchronously — measured live: 38.95GB -> 35.61GB -> 20.37GB -> 18.22GB
across successive identical passes, some of which themselves reported
`Total: 0B` while the measured size kept falling. The script therefore
re-measures and repeats (bounded at 5 passes) instead of trusting one
pass or its self-reported total.

Sharing still matters at the margin — a record shared with a live image
can be deleted (with `--all`), but its layers only leave the disk when
the image holding them goes. That is why the two du/df commands disagree,
and which one to trust:

- **`docker system df`'s `RECLAIMABLE`** (3.314MB in this measurement) is
  what a prune would free *right now* — it already accounts for sharing.
  This is the honest number.
- **`docker builder du`'s `Reclaimable`** (12.45GB in this measurement)
  counts every entry not pinned by a running build, including shared ones
  that will not free while the image holding them still exists.

**The age pass remains the primary mechanism, the ceiling the backstop**:
aged cache is the cheapest to lose, and its layers free in full once the
images that held them are cleaned up (old rollback tags pruned by
`ops/prune-rollbacks.*`). The ceiling loop covers the heavy-build-week
case where nothing is old enough yet; when the remaining cache is
genuinely pinned by live images it stalls, warns, and leaves the rest to
the age pass on a later run.

**Step ordering still helps the age pass free actual disk, and it is
accidental.** `ops/prune-rollbacks.*` runs at `update.ps1`/`.sh` step 2b,
*before* the build, retiring old rollback image tags; the build-cache age
pass runs at step 5, *after* health. By the time `until=168h` fires, the
images that were pinning the >168h-old cache layers are already gone, so
deleting those records frees their bytes too. Neither script enforces
this ordering explicitly — a future change to `-KeepRollbacks` or to
either script's step position would quietly turn aged-record deletion
into bookkeeping-only until the pinning images expire, with nothing to
catch it.

**What these scripts will never do:** touch images (that's
`ops/prune-rollbacks.ps1` / `.sh`), touch containers, or run
`docker system prune` or any volume command. All four are **enforced**, not
just intended: `tests/test_ops_prune_build_cache.py` fails the run outright if
`system prune`, `rmi` / `image rm`, any `volume` verb, or any container verb
(`container`, `docker rm` / `stop` / `kill`) reaches its docker stub. The
container verbs were added to that list on 2026-07-29 — until then this
guarantee was documented but unguarded, so treat a pre-that-date branch as
convention only.

Never run `docker system prune --volumes` on this host: the bank lives in
external volumes that a volume prune would take with it. On a default install
those are `pseudolife-mcp-bank` (the Postgres data — the bank itself) and
`pseudolife-mcp-state` (daemon state: ChromaDB, cortex/graph snapshots), per
`ops/docker-compose.yml`. Installs predating the rename override both names
via `PSEUDOLIFE_BANK_VOLUME` / `PSEUDOLIFE_STATE_VOLUME` in the gitignored
`ops/.env`, so check there before assuming the shipped names are what your
host actually uses:

```powershell
docker volume ls --filter name=pseudolife
```

## 2. The .vhdx never shrinks (MANUAL — needs elevation + full downtime)

Pruning frees space *inside* the VM's virtual disk. `fstrim` — run
automatically as the last retention step above — returns those freed
blocks to the disk's own internal free list. **Neither shrinks the host
`.vhdx` file.** Sparse mode was deliberately declined as too risky, so the
file only gets smaller under an offline `Optimize-VHD -Mode Full`, which
this repo does not run automatically: it requires an Administrator prompt
and stops Docker Desktop plus every WSL distro for its duration, so it
stays an operator's decision rather than a scheduled one.

```powershell
# From an elevated ("Run as Administrator") PowerShell prompt:
.\ops\compact-docker-vhdx.ps1
.\ops\compact-docker-vhdx.ps1 -Path D:\path\to\docker_data.vhdx   # non-default location
```

Default `-Path` is `$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx`.

**Before it touches anything**, the script checks two things and refuses
early rather than leaving Docker stopped over a problem it can't fix:
- **`Optimize-VHD` must be importable.** That cmdlet ships with the
  Hyper-V PowerShell module, which Docker Desktop's WSL2 backend does not
  require and which **Windows Home lacks entirely** — there is no toggle
  to enable it there. On Pro/Enterprise/Education, enable it via Windows
  Features → Hyper-V → Hyper-V Management Tools → Hyper-V Module for
  Windows PowerShell.
- **`-Path` must point at an actual file**, not a directory — a typo'd
  path is caught up front instead of surfacing later as an opaque lock
  error after Docker has already been torn down.

**What it does, in order:** requires elevation (throws immediately if
not); stops Docker Desktop and its helper processes
(`com.docker.backend`, `com.docker.build`, `vpnkit`); runs
`wsl --shutdown`; opens the file to confirm nothing still holds a lock on
it (a genuine lock and a permissions/AV problem are reported as distinct
errors, not both folded into "still locked, retry"); runs
`Optimize-VHD -Mode Full`; reports before/after size and the amount
reclaimed. Docker Desktop is **not** restarted for you — start it manually
once the script finishes.

**Measured 2026-07-28.** Prune + fstrim took internal usage from 87GB to
49.3GB while the file itself stayed at 94.74GB — confirming fstrim alone
does not shrink it. The offline compact then took the file from 94.74GB to
47.31GB. Both halves are needed: the prune reclaims space inside the VM,
the compact is what returns it to the host filesystem.
