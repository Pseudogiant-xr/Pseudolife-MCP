# Docker build-cache retention — design

**Date:** 2026-07-28
**Status:** approved for implementation
**Origin:** 2026-07-28 disk investigation — `docker_data.vhdx` had ballooned
again, two weeks after the 2026-07-14 manual trim. Root cause confirmed as
unbounded **build cache** regrowth. This is the missing sibling of the
rollback-tag retention shipped 2026-07-14 (commit a268427,
`ops/prune-rollbacks.ps1`).

## 1. Problem

`ops/update.ps1` rebuilds the daemon image on every deploy. Each rebuild adds
BuildKit cache entries, and nothing ever removes them.

Measured on 2026-07-28:

| Observation | Value |
|---|---|
| Build cache at investigation | 51.87 GB across 169 entries |
| Entries **active** (`docker system df`) | 0 — every entry was reclaimable |
| Oldest entries | 5–6 weeks |
| Regrowth window | ~52 GB in the 13 days since the 2026-07-14 trim |
| Cache produced by **one** deploy (measured post-deploy, same day) | 12.45 GB across 17 entries |
| `docker_data.vhdx` file size | 94.74 GB |
| Internal usage after `builder prune -af` + `fstrim` | 49.3 GB (file unchanged at 94.74 GB) |
| File size after elevated `Optimize-VHD -Mode Full` | 47.31 GB |

Two distinct failures compound:

1. **The cache grows without bound.** No retention policy exists at any layer.
2. **The vhdx never shrinks on its own.** Sparse mode was deliberately declined
   as risky in July, so space freed inside the VM stays allocated in the host
   file until an offline compact. That compact needs elevation and Docker
   downtime, so it cannot be automated safely.

Only (1) is automatable. This design fixes (1) and documents (2).

**"Reclaimable" is not "useless."** Measured minutes after a deploy, all 17
fresh entries reported as inactive under `docker system df` and the full
12.45 GB as reclaimable under `docker builder du` — while being precisely the
cache the *next* deploy would reuse. Reclaimability tracks "not pinned by a
running build", not "not worth keeping". Any policy keyed on it (including a
plain `-af`) therefore deletes hot cache and cold-starts the next build. This
is the measured reason age is the primary filter here and size is only a
backstop.

## 2. Why this is safe to automate

Build cache is **pure derived data**. Every entry can be reconstructed by
re-running the build that produced it. The only cost of over-pruning is
rebuild time — never data loss. This is categorically different from images
(which carry the rollback story) and from the `ops_pseudolife_data` /
`ops_pseudolife_pgdata` volumes (which carry the bank).

## 3. Non-goals

Explicitly out of scope, and the scripts must be incapable of them:

- **No image deletion.** `ops/prune-rollbacks.ps1` already owns rollback-tag
  retention; nothing here touches images.
- **No volume operations of any kind.** Never `docker system prune`, never
  `--volumes`, never `docker volume rm`.
- **No automated vhdx compaction.** Requires elevation plus a full Docker
  shutdown. Documented as a manual runbook procedure instead.
- **No container lifecycle changes.**

## 4. Corrections to the originally-suggested approach

Two assumptions in the task framing do not survive contact with this machine:

- **`--keep-storage` no longer exists.** Docker Server here is **29.6.2**;
  the flag was replaced by `--max-used-space` (with `--reserved-space`
  alongside). The suggested command fails outright.
- **Pruning at the `prune-rollbacks` call site would cold-start every deploy.**
  That call sits at `update.ps1` step 2b, *before* `docker compose up --build`.
  A `-af` prune there deletes exactly the cache the imminent rebuild would
  reuse. Build-cache retention must run **after** the build and must be
  age- or size-filtered, never `-af`.

## 5. Design

One primitive, two triggers.

### 5.1 `ops/prune-build-cache.ps1` + `ops/prune-build-cache.sh`

The only component that issues a prune. Parameters (PowerShell names; the
bash port takes the `--kebab-case` equivalents, matching the
`prune-rollbacks` precedent):

| Parameter | Default | Meaning |
|---|---|---|
| `-MaxAgeHours` | `168` | Age pass: evict cache untouched for longer than this |
| `-MaxUsedSpaceGB` | `20` | Ceiling pass: hard cap, backstop for a heavy build week |
| `-DryRun` | off | Report sizes and planned commands; mutate nothing (no prune, no `fstrim`) |
| `-NoTrim` | off | Skip the WSL `fstrim` step |

Sequence:

1. **Read cache size.** `docker system df --format '{{.Type}}|{{.Size}}'`,
   selecting the `Build Cache` row and parsing the human-formatted `Size`
   string (e.g. `"51.87GB"`). A Go template rather than JSON, so the bash port
   shares the contract without needing `jq`. Sizes are SI (1000-based) with a
   lowercase `k` (`8.192kB`), so a small parser is needed.

   **Dry-run estimate.** BuildKit has no `--dry-run`, so a dry run sums the
   `Size` of entries from `docker builder du --format
   '{{.CreatedAt}}|{{.Size}}'` whose `CreatedAt` precedes the cutoff. This is
   an **estimate** and must be reported as one: BuildKit's own `until=` filter
   considers last-used rather than created, and shared entries may not free
   fully.
2. **Age pass** — `docker builder prune --force --filter until=<MaxAgeHours>h`.
   Always runs. This is the normal policy: a week of cache is the useful
   window, and every entry found on 2026-07-28 was well past it.
3. **Ceiling pass** — `docker builder prune --force --max-used-space <bytes>`.
   Runs **only** if the post-age size still exceeds the cap. Kept as a separate
   invocation rather than combined flags so each pass reports its own reclaim
   and the ceiling's rarity stays visible in the output.

   **Measured 2026-07-28: this pass can be a complete no-op, and usually will
   be, for exactly the scenario it exists to cover.** Against a 12.45GB /
   17-entry cache produced by one deploy, `docker builder prune --force
   --max-used-space 8000000000` (and with `--reserved-space 0` added)
   reclaimed **0B**. Cause: 14 of the 17 entries were `Shared=true` with the
   live daemon image — sharing means a build-cache prune cannot free those
   layers while that image still exists — and carried essentially all the
   bytes (8.739GB, 2.15GB, 1.159GB, 348.5MB, …). Only the 3 unshared
   (`source.local`) entries, totalling exactly 3.314MB, were prunable; that
   figure matches both `docker system df`'s `RECLAIMABLE` and `docker
   builder du`'s `Private` size for the same cache. So in a "heavy build
   week", the ceiling pass does nothing until the images holding that week's
   cache are removed (e.g. by `ops/prune-rollbacks.*` retiring old rollback
   tags) — at which point the cache unshares and becomes prunable. The age
   pass, not the ceiling, is the mechanism that actually reclaims space in
   the steady state; see the corrected reasoning in
   `docs/runbooks/docker-disk-retention.md`.
4. **fstrim** — Windows-only in effect (gated on `wsl` being on `PATH`,
   which only Windows hosts satisfy — not on an `$IsWindows` check, which
   the test harness cannot stub and which made CI's pwsh-on-Linux runner
   skip the step), and only when a `wsl -d docker-desktop -e sh
   -c true` probe exits 0, confirming the `docker-desktop` distro exists
   (`wsl -l -q` is not used for this: it returns UTF-16LE and is unsafe to
   parse from a shell script, so the code uses the exit-code probe
   instead): `wsl -d docker-desktop -e sh -c "fstrim -v
   /mnt/docker-desktop-disk"`. **Measured 2026-07-28: the bare `-e fstrim`
   form (without `sh -c`) never worked** — `wsl -d <distro> -e <cmd>` fails
   to resolve `fstrim` when passed as the bare `-e` target, even though
   `/sbin` (where `fstrim` lives) is on the child's `PATH`
   (`execvpe(fstrim) failed: No such file or directory`), silently, since
   the step is non-fatal. An absolute path (`/sbin/fstrim`) works, and so
   does `sh -c`, because the shell performs its own `PATH` lookup rather
   than relying on wsl's relay to do it; the corrected form reclaimed
   207.2MiB live. This returns freed blocks to the vhdx free list; it does
   **not** shrink the host file. Its real value is making the later manual
   compact worth running. On Linux there is no vhdx and the step is
   skipped entirely.
5. **Report** before / after / reclaimed.

Verified present on this machine: distro `docker-desktop`, mount
`/mnt/docker-desktop-disk` (40.6 GB used of 1007 GB), `fstrim` from
util-linux 2.41.4.

### 5.2 Deploy hook — `ops/update.ps1` + `ops/update.sh`

New parameters `-KeepCacheHours` (default `168`) and `-NoCachePrune`
(`--keep-cache-hours` / `--no-cache-prune` in bash), mirroring the existing
`-KeepRollbacks` / `-NoBackup` style.

The call becomes **step 5, after the health check**, for two reasons:

- Before the build, it would cold-start the build it precedes.
- On the unhealthy path, it would strip the cache an operator's rollback
  rebuild wants — precisely when they are least able to afford a slow build.
  The unhealthy branch already `exit 1`s, so placing the call after the health
  block skips it for free.

Wrapped non-fatal (`try`/`catch` + warning in PowerShell, `if !` + warning in
bash), exactly like the `prune-rollbacks` call at step 2b. A retention hiccup
must never fail a deploy that otherwise succeeded.

**Step ordering is load-bearing for the ceiling pass's own reasoning, and
currently accidental.** `prune-rollbacks` runs at step 2b, *before* the
build, retiring old rollback image tags; the build-cache age pass runs at
step 5, *after* health. So by the time `until=168h` fires, the images that
were pinning the >168h-old cache layers are already gone and that cache has
unshared — which is why the sharing constraint documented in §5.1 step 3
(a build-cache prune cannot free layers a live image still holds) does not
neuter the age pass the way it neuters the ceiling pass. Neither script
enforces this ordering explicitly; it holds only because `-KeepRollbacks`
runs early and the cache prune runs late. A future change to
`-KeepRollbacks` (e.g. keeping enough rollback tags to pin week-old cache)
or to either script's step position could silently degrade the age pass
back down to the ceiling pass's no-op behavior, with nothing to catch it.

This hook is what ships retention to every user of the public repo, with no
scheduled task to register.

### 5.3 `ops/install-cache-retention.ps1`

Registers a weekly Windows Scheduled Task, modelled on
`ops/install-autostart.ps1` (`Register-ScheduledTask`, base64
`-EncodedCommand`, `-StartWhenAvailable` so a run missed while the machine was
off fires at next boot).

- Task name: `Pseudolife-MCP Docker cache retention`
- Default schedule: Sunday 03:00
- Parameters: `-MaxAgeHours`, `-MaxUsedSpaceGB`, `-DayOfWeek`, `-At`,
  `-Unregister`

This covers the gap the deploy hook cannot: stretches with no deploys.

### 5.4 `ops/compact-docker-vhdx.ps1`

The working scratchpad script moved into the repo with one required change:
its hardcoded absolute path (`C:\Users\<user>\AppData\Local\Docker\wsl\disk\`)
becomes `Join-Path $env:LOCALAPPDATA 'Docker\wsl\disk\docker_data.vhdx'`, with
a `-Path` override parameter. **As written it would trip
`tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`**
— the repo forbids OS usernames in tracked files.

Kept as-is: the elevation check, the file-lock check before `Optimize-VHD`
(a held handle makes the compact fail with an opaque lock error), and the
before/after/reclaimed reporting.

Deliberately **not** wired to any trigger. It stops Docker Desktop and
shuts down every WSL distro; that is an operator decision, not a scheduled one.

### 5.5 `docs/runbooks/docker-disk-retention.md`

Covers: what the automation does and when it fires; what it deliberately does
not do; how to run the manual compact and why it stays manual; the 2026-07-28
numbers as the worked example; and the pointer that `docker system prune
--volumes` must never be used on this host.

## 6. Testing

`tests/test_ops_prune_build_cache.py`, following the harness established by
`tests/test_ops_prune_rollbacks.py`: drive the **real** scripts with `docker`
stubbed as a shell function (a PowerShell `function global:docker` shadows
`docker.exe` on command lookup; bash uses `export -f docker`), parametrised
over `ps1` / `sh`. No Docker daemon required.

The load-bearing assertion is **negative**:

- **The script never issues `docker system prune`, `docker rmi`, or any
  `docker volume` command.** The stub fails the test on any such call.
  Mutation-check this guard by removing it and confirming the test goes red —
  per project CLAUDE.md, a hook that never fires red is decoration.

Also pinned:

- Age pass always fires, with `--filter until=168h` by default and the
  parameter threaded through.
- Ceiling pass fires only when the post-age size exceeds `-MaxUsedSpaceGB`,
  and does not fire when under it.
- `-DryRun` issues **no** prune call at all, and still prints a size report
  (so the test cannot pass vacuously against a missing script).
- `fstrim` is skipped when no `docker-desktop` distro is listed, and a
  failing `fstrim` does not fail the script.
- `update.ps1` / `update.sh` wiring: the parameters exist with the documented
  defaults, the call is present, it is wrapped non-fatal, and it appears
  **after** the health-check block rather than beside `prune-rollbacks`.

## 7. Verification (live)

`docker system df` before and after, per the task's acceptance criterion.

A deploy on 2026-07-28 repopulated the cache to 12.45 GB / 17 entries, so no
seeding rebuild is needed. But it creates the opposite problem: **every entry
is minutes old, so a default run correctly reclaims nothing.** Right behaviour,
still no observable reclaim. Four steps, each proving a different claim:

| Step | Command | Proves |
|---|---|---|
| 1 | `-DryRun` (defaults) | Size reading is correct (12.45 GB) and the age pass plans **no** eviction — nothing is older than 168h. |
| 2 | `-DryRun -MaxAgeHours 0` | The age filter is genuinely plumbed through: the planned command carries `until=0h` and the reclaim estimate approaches the full 12.45 GB. Still mutates nothing. |
| 3 | **Real run**, `-MaxUsedSpaceGB 8` | ~~The ceiling pass reclaims for real — roughly 4.5 GB — while leaving ~8 GB of hot cache intact.~~ **This prediction was wrong; see the RESULT below.** Still the only step that mutates. |
| 4 | `docker system df` after | Records the actual delta. |

**RESULT (run 2026-07-28) — steps 1, 2 and 4 held; step 3's prediction did
not, and two real defects surfaced that no test had caught.**

- Steps 1 and 2 passed exactly as designed: the default dry run read 12.45 GB
  and estimated 0 B (nothing older than 168h); `-MaxAgeHours 0` carried
  `until=0h` and estimated the full 12.45 GB. Neither mutated anything.
- **Step 3 reclaimed 0 B, not ~4.5 GB.** The ceiling pass fired as designed
  (`12.45GB still over the 8.00GB ceiling; enforcing`) and freed nothing.
  14 of the 17 entries were `Shared=true` with the live daemon image, and a
  build-cache prune cannot free layers a live image still holds. Only the 3
  unshared `source.local` entries — 3.314 MB — were prunable. §5.1 step 3
  carries the full measurement and its consequence for the ceiling's value.
- **The `fstrim` step had never worked.** `wsl -d <distro> -e <cmd>` fails to
  resolve `fstrim` when passed as the bare `-e` target, even though `/sbin`
  (where `fstrim` lives) is on the child's `PATH` — the bare form always
  failed `execvpe(fstrim)`. Being non-fatal, it only warned. An absolute
  path works, and so does `sh -c`, because the shell performs its own
  `PATH` lookup rather than relying on wsl's relay to do it; the `sh -c`
  form reclaimed 207.2 MiB live on first correct run.

The lesson is the reason this section exists: **every layer of stubbed testing
passed against both defects.** A stub cannot know that a real BuildKit refuses
to evict shared layers, or that a real WSL exec has no `PATH`. Command-shape
tests pin the command; only execution pins the behaviour.

Deliberately **not** run live: a default-age real run (would be a no-op today)
and any `-af`-equivalent (would cold-start the next deploy for no benefit).
The exact CLI contract of every pass is pinned by the stubbed-`docker` tests
in §6 instead, which is where command-level correctness belongs; the live run
exists to confirm the numbers move on real infrastructure.

The first *default-policy* reclaim will land on its own once these entries
age past 168h — via whichever trigger fires first.

## 8. Shipping checklist (project CLAUDE.md)

- CHANGELOG entry under `[Unreleased]` — this is a behavior change.
- No schema change, so no version bump and no schema-pin test.
- Full suite before commit: `HF_HUB_OFFLINE=1 python -m pytest tests/` with the
  bench Postgres up on 127.0.0.1:5433.
- Not a retrieval- or extraction-affecting change, so neither
  `evals/regression_gate.ps1` nor the ladder applies.
- These are host-side ops scripts. **No daemon deploy is required**; the deploy
  hook fires on the next `ops/update.ps1` run of its own accord.
- No PII: the compact script must be de-identified before it is tracked
  (§5.4), and the runbook uses `<user>`-style placeholders for any path that
  would otherwise embed a home directory.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Over-pruning slows a subsequent build | Accepted and bounded — cost is time only. The 168h window preserves the week of cache that is actually reused. |
| The deploy hook fails and blocks a deploy | Wrapped non-fatal, mirroring `prune-rollbacks`; pinned by test. |
| A future edit reaches for `docker system prune` for convenience | The negative test forbids it at the script level, mutation-checked. |
| `docker system df --format json` shape changes across Docker versions | Parser is isolated in one function; `docker builder du` is the documented fallback source. |
| Scheduled task silently stops firing | Out of scope to monitor. The deploy hook is the independent second trigger, which is the reason for having both. |
| **Ceiling pass reclaims nothing during a heavy build week** — measured 2026-07-28: `--max-used-space` against a 12.45GB/17-entry cache reclaimed 0B because 14 entries were `Shared=true` with the live image (only 3.314MB was unshared/prunable). | Accepted, and not a regression from the design's intent: the age pass was always meant to be primary. Documented in §5.1 and the runbook rather than mitigated in code — fixing it would mean pruning images, which is explicitly out of scope (§3) and owned by `ops/prune-rollbacks.*`. |
