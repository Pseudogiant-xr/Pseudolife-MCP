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

1. **Read cache size.** `docker system df --format json` emits one JSON object
   per line; select `Type == "Build Cache"` and parse the human-formatted
   `Size` string (e.g. `"51.87GB"`). A small size parser is needed;
   `docker builder du` is the fallback source if the JSON shape shifts.
2. **Age pass** — `docker builder prune --force --filter until=<MaxAgeHours>h`.
   Always runs. This is the normal policy: a week of cache is the useful
   window, and every entry found on 2026-07-28 was well past it.
3. **Ceiling pass** — `docker builder prune --force --max-used-space <bytes>`.
   Runs **only** if the post-age size still exceeds the cap. Kept as a separate
   invocation rather than combined flags so each pass reports its own reclaim
   and the ceiling's rarity stays visible in the output.
4. **fstrim** — Windows only, and only when `wsl -l -q` lists a
   `docker-desktop` distro: `wsl -d docker-desktop -e fstrim -v
   /mnt/docker-desktop-disk`. Non-fatal on any failure. This returns freed
   blocks to the vhdx free list; it does **not** shrink the host file. Its
   real value is making the later manual compact worth running. On Linux there
   is no vhdx and the step is skipped entirely.
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

**Stated limitation.** Build cache is **0 B right now** — the 2026-07-28
manual prune already emptied it. A live run today therefore reclaims nothing
and cannot exercise the reclaim path end-to-end. Plan: seed the cache with one
daemon rebuild, then run `-DryRun` (proving the no-op) followed by a real run,
and record both `docker system df` readings. A single rebuild is cheap and is
the only way to observe the numbers actually move.

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
