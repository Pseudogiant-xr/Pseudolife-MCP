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
| After every healthy deploy | `ops/update.ps1` / `.sh`, step 5 (last) | age 168h |
| Weekly, if registered | Scheduled Task → `ops/prune-build-cache.ps1` | age 168h, 20GB ceiling |

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

This is currently the single most likely way this feature quietly stops
working: nothing fails loudly if the registered task's target no longer
exists, it just never runs again.

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
1. Age pass — always: `docker builder prune --force --filter until=<N>h`.
2. Ceiling pass — only if the cache is still over the size cap *after* the
   age pass: `docker builder prune --force --max-used-space <cap>`. This
   is a backstop for an unusually heavy build week and should rarely fire.
3. `fstrim` of the WSL disk (`wsl -d docker-desktop -e fstrim -v
   /mnt/docker-desktop-disk`) — Windows-only, skipped quietly (not a
   failure) if not on Windows, if `wsl` isn't on `PATH`, or if the
   `docker-desktop` distro isn't present. `-NoTrim` / `--no-trim` skips
   this step outright. A failed fstrim only warns; it never fails the run.

**Why age and not "reclaimable".** Minutes after a deploy, `docker builder
du` reports the whole fresh cache as reclaimable — reclaimable means "not
pinned by a running build", not "not worth keeping". A policy keyed on
that signal would delete hot cache and cold-start the very next build. The
168h window keeps the week of cache that actually gets reused; the size
ceiling only backstops an unusually heavy build week.

Note the two commands disagree, so read the right one. For the same fresh
12.45GB cache measured 2026-07-28, `docker builder du` reported
`Reclaimable: 12.45GB` while `docker system df`'s Build Cache row reported
`RECLAIMABLE 3.314MB` (its `ACTIVE` count was 0). `docker system df` is
the size-of-cache reading; `docker builder du` is the what-could-be-freed
reading.

**What these scripts will never do:** touch images (that's
`ops/prune-rollbacks.ps1` / `.sh`), touch containers, or run
`docker system prune` or any volume command — enforced by
`tests/test_ops_prune_build_cache.py`, not by convention alone. Never run
`docker system prune --volumes` on this host: the bank lives in the
external `ops_pseudolife_data` and `ops_pseudolife_pgdata` volumes.

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
