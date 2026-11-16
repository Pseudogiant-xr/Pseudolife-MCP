# Dev machines and suite launchers

Where development work runs on the maintainer's setup, and how the
full-suite launchers behave. The rules themselves (one full suite per
machine, never the live bank, leases, restarts) are in `CLAUDE.md`; this
page holds the detail behind them. Machine names, addresses, SSH aliases,
lease names and test-database settings live only in private config (the
maintainer's `~/.ssh/config`, `~/.pseudolife-mcp/locks/full-suite.remote`,
each machine's `~/.config/pseudolife-suite/env` and `full-suite.lease`) and
in the memory bank (search "dev machines homelab"), never in this tree.

## The machines

- **The maintainer's Windows host.** Interactive work and targeted test
  runs. Its full suites run in local WSL (`ops/wsl-suite.ps1`), because a
  native Windows full suite's hook-script tests start Git Bash processes
  in bursts that stall mouse input.
- **Linux dev boxes** (the homelab "box" and its sibling, guests of one
  hypervisor). Linux full suites, Rust builds and tests on Linux, and long
  or CPU-heavy Linux work that would otherwise load the Windows host.
  `ops/remote-suite.ps1` dispatches to the one box its `full-suite.remote`
  names. `PSEUDOLIFE_SUITE_REMOTE` can point one run at the sibling once
  the sibling has its own suite env and lease file; a dispatched run
  refuses a machine without its own lease name.
- **The Windows dev VM** (a guest of the same hypervisor). Windows-only
  flake repro loops, Windows Rust builds under MSVC, and native-Windows
  behaviour: process ownership and shutdown, named pipes, junctions, UNC
  and device paths. It is also where native Windows full suites run, which
  the Windows host refuses. Its disk, like the Linux boxes', is on
  the hypervisor's NVMe pool, so its Rust target is an ordinary cache. Its default
  SSH shell is PowerShell 7. Its updates are notify-only and paused, with
  automatic restart disabled.
- **Hosted CI.** The merge gate. Its required Windows job runs the product
  paths that differ on Windows, not the full suite (`.github/workflows/ci.yml`,
  `test-lite-windows`); the Windows installer, hook and board plumbing runs
  in the non-required `test-windows-nightly` job.

The dev machines share the hypervisor's RAM, which carries two full suites
at once in any mix (two Linux boxes, or a Linux box and the Windows VM),
never three. Nothing enforces that limit: `ops/remote-suite.ps1` checks
only the machine it dispatches to.

## Test databases

- **Linux boxes** use a separate loopback test server (127.0.0.1:5434),
  never the live bank's Postgres (5433). The suite user's shells source
  `~/.config/pseudolife-suite/env`, which sets
  `PSEUDOLIFE_TEST_PG_HOST_PORT=127.0.0.1:5434` and its
  `PSEUDOLIFE_TEST_PG_PASSWORD`, so every default path (per-run databases,
  the admin URL, reachability probes) uses it. Do not use a fixed
  `PSEUDOLIFE_TEST_DATABASE_URL` instead: that shares one database between
  concurrent runs and covers only the fixtures, leaving the default paths
  on 5433.
- A **dispatched run** (`PSEUDOLIFE_SUITE_DISPATCHED`) refuses to start
  unless that address names a port other than 5433 and the machine
  has its own lease name, and its pytest then refuses any server it would
  use that holds a production bank, so the live bank's container on its
  Docker-network address (port 5432) is refused too (`dispatched_live_bank_refusal` in
  `tests/pg_defaults.py`). Dispatched runs add a systemd `MemoryMax=16G`
  scope, since the box also serves the live daemon; that needs
  password-free sudo that may preserve the environment (`NOPASSWD:SETENV:`
  for `systemd-run`, or `NOPASSWD: ALL`). Sessions on a box run
  `python -m pytest tests/` natively, under the same one-suite lock.
- **The Windows VM** uses the `[lite]` extra's embedded PostgreSQL on
  loopback inside the VM, started for each test run by a wrapper kept in
  the VM's private config. A Windows SSH session's child processes end
  with the session, so a server started by hand dies when you disconnect.

## Suite launchers

- **`ops/remote-suite.ps1`** tests the committed HEAD on the box over
  SSH. The box's clone fetches a pushed commit from origin; an unpushed
  one travels inside the dispatch as a git bundle of what `origin/master`
  lacks, so nothing is pushed for a run. With a box configured, the box is the
  only candidate unless the maintainer has said this PC is available (the
  switch below): then the box comes first and local WSL takes the overflow,
  or the run when the box cannot be probed. Without that statement it
  waits for the box, saying so once a minute, and refuses a box it cannot
  probe. `PSEUDOLIFE_SUITE_WHERE=local|remote` forces a
  machine, and `local` says it overrides the rule. With no box configured,
  local WSL runs as before. The first lines name the machine and why. Its
  log and a JSON result (machine, commit, exit code, pytest's summary line,
  whether the real native client ran) land in
  `~/.pseudolife-mcp/suite-results`. It does not dispatch to the Windows VM.
- **`ops/wsl-suite.ps1`** tests the worktree's committed HEAD from a copy
  on WSL's own filesystem and refuses uncommitted changes to tracked files.
  With a box configured and no availability statement it refuses a full
  run (not a targeted one), naming the remote command.
  It keeps a uv environment per checkout under `~/.venvs/pseudolife` in WSL
  and copies under `~/.cache/pseudolife-suite/work`; each run prunes those
  unused for 3 days or past the 8 most recent, under their locks
  (`PSEUDOLIFE_SUITE_PRUNE_DAYS`, `PSEUDOLIFE_SUITE_KEEP`,
  `PSEUDOLIFE_SUITE_PRUNE=off`). It forwards the test login file
  (`~/.pseudolife-mcp/test-pg.env`; only without one does it copy the
  worktree's `ops/.env`), needs the models in WSL's own Hugging Face cache
  (see `ops/wsl-suite.sh`), and forwards the bearer, so the run still shows
  as the `full-suite` lease. From Windows, `lease check full-suite` sees a
  WSL run through the board only; its local-lock line stays free.
- **Workers.** With no arguments, `ops/wsl-suite.sh` (which both launchers
  run) adds `-n N --dist loadfile --durations=30`. N is at most 4, one per
  two CPUs and one per 6 GB of memory, the memory being the smaller of RAM
  and the run's cgroup limit: 4 in the Windows host's WSL (8 CPUs, 40 GB),
  at most 2 under a box run's 16 GB systemd scope. Below 2 it runs one
  process. `PSEUDOLIFE_SUITE_WORKERS` overrides it (`1` for one process).
  Measured 2026-10-19 in WSL at 4 workers: about 11 min against 28-31 min
  single-process, peak 20.2 GB used. Arguments still replace the default
  unchanged.
- **Leases.** Each machine mirrors its suite on the board under its own
  lease: `full-suite` on the Windows host, `full-suite@<name>` on a dev
  machine, set in its private `full-suite.lease` (`PSEUDOLIFE_SUITE_LEASE`
  overrides). Without that file a run falls back to `full-suite`, the
  Windows host's lease, and the board mislabels it.
- **The real native client.** `PSEUDOLIFE_SUITE_NATIVE_CLIENT=1` makes
  `ops/wsl-suite.sh` build `pseudolife-stdio` on the machine the suite runs
  on, as the hosted `test` lane does, and run the tests that need it
  (`docs/dev/merge-batches.md` "Validating it"). That machine needs
  cargo through rustup, flock, and 8 GB free where the build cache lives
  (`~/.cache/pseudolife-suite/cargo-target`, or
  `PSEUDOLIFE_SUITE_CARGO_TARGET_DIR` set there; a general
  `CARGO_TARGET_DIR` is not used). `ops/remote-suite.ps1` turns it on by
  itself for an `integrate/*` branch. Every run prints `wsl-suite: real
  client: yes|no` before pytest starts.

## Sharing

The rules are in `CLAUDE.md` ("Sharing the machine"); this section holds the
knobs and procedures they point at.

- **This PC's availability for full suites.** It is the maintainer's
  statement only: the file `~/.pseudolife-mcp/locks/pc-available` on the
  Windows host, which the conductor writes and removes on the maintainer's
  word. The launchers only read it (`ops/suite-availability.ps1`) and never
  guess from processes, the GPU, input or the hour. An empty file means
  available until it is removed. One line `until=<ISO 8601 time with an
  offset>` means available until then. Anything else, an expired time or an
  unreadable file reads as not available, and the run says why. To set it
  until 07:00 AEDT, and to clear it:

  ```powershell
  Set-Content -Path "$HOME\.pseudolife-mcp\locks\pc-available" -Value 'until=2026-11-17T07:00+11:00'
  Remove-Item -Path "$HOME\.pseudolife-mcp\locks\pc-available"
  ```

- **A shared Rust target directory can hand you another checkout's
  binary.** Cargo keys a workspace package by its path inside the
  workspace and judges it fresh by file times, so a build in one worktree
  can reuse units another worktree left in the same `CARGO_TARGET_DIR`
  and print `Finished` with that worktree's code (reproduced 16 November
  2026). Share a target only for third-party crates you never test against
  a specific checkout, or `cargo clean -p` your workspace's packages when
  you switch, as the suite launchers' native-client build does.
- **Suite lock switches.** `PSEUDOLIFE_SUITE_LOCK=fail` exits instead of
  queueing, `=off` skips the lock (the CI default), and targeted runs never
  lock. `PSEUDOLIFE_SUITE_SLOTS` stays at 1 on the Windows host.
  `PSEUDOLIFE_SUITE_WINDOWS=allow` is only for a deliberate native Windows
  run with no WSL suite going; `PSEUDOLIFE_SUITE_LOCK=off` is never a way
  around the host's refusal of native runs.
- **Offline models.** The launchers set `HF_HUB_OFFLINE=1`
  (`ops/wsl-suite.sh`); a direct native full run sets it by hand.
- **Lease check exit codes.** `pseudolife-mcp lease check <lease> --
  <command>` exits 75 while the lease is held, 70 on a failed check, and
  otherwise with the command's own exit code, so a `;` after a bare check
  would run the work whatever the check said.
- **Reproducing a Windows CI failure on the VM.** Loop the failing test or
  slice, 20 runs to start, with CPU load only while no other dev machine
  runs a suite; 20 clean runs rule out failure rates above about 15%, so an
  unreproduced failure that recurs on hosted CI gets a deflake PR or an
  issue naming the hosted run.
