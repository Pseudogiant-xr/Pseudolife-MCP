# Pseudolife-MCP — project conventions

Conventions that aren't derivable from a quick read of the code. Follow them
exactly; they exist because each one was violated at least once.

## Shipping checklist (any change that lands on master)

1. **CHANGELOG.md entry under `[Unreleased]`** — every behavior, schema, or
   perf change gets one, in the existing dated-subsection style. Docs-only and
   test-only changes are exempt.
2. **Schema bumps** touch seven places together: `SCHEMA_META_VERSION` in
   `pseudolife_memory/storage/schema.py`, the doc mentions (README
   capabilities table + the DSN row and version-history table in
   `docs/guide/configuration.md` — both pinned by `tests/test_release_ux.py`,
   the history table including gap detection), the version pin (the single
   `CURRENT_SCHEMA` literal in `tests/test_schema_version.py` — no
   per-version file, no `>=` relaxation pass; the ladder is gap-checked by
   `test_release_ux.py` and `test_atlas_currency.py`. Whatever behavior the
   bump adds gets a test beside its consumer, or a row in
   `tests/test_schema_ddl_shape.py` if it is pure DDL shape — never a new
   `test_schema_vNN.py`),
   a CHANGELOG mention of `vNN` (pinned by `test_release_ux.py`), and
   `docs/atlas/atlas.json` `meta.schema` (pinned by
   `tests/test_atlas_currency.py` — re-verify the affected storage cards,
   don't just renumber), the two `assert meta[0] == NN` literal pins in
   `tests/test_migrate_embeddings.py`, and `python ops/gen_llms_txt.py`
   after any doc edit (`tests/test_llms_txt.py` pins the generated
   `llms-full.txt`). The v30 bump found the last two the hard way.
3. **Local validation before opening a PR; CI before merge.** Run the
   touched and dependent test files locally, with the bench Postgres
   available for PG-backed tests. A missing PostgreSQL service or an
   unexpected platform/database skip is not a pass for the affected
   behavior. Record the tested head, selected files, dependency reasoning
   and result in the PR.

   Ordinary code changes do not require a local full suite. A local full
   suite is required for schema/DDL/migration changes; shared test
   infrastructure, conftest, fixture or suite-lock changes; and changes
   whose dependent coverage cannot be bounded confidently. Daemon process
   creation, ownership, shutdown or recovery changes are ordinary code on
   Linux: CI's full PostgreSQL job starts and stops real daemons there and
   covers recovery against stand-in daemons. When such a change is
   Windows- or macOS-specific, a local full suite would not help (this
   host runs full suites only on Linux). Instead, run that platform's
   affected test files natively as a targeted run (Windows allows one), and
   make sure they are in its CI lite lane's file list (`test-lite-windows`
   / `test-lite-macos` in `.github/workflows/ci.yml`), adding them if
   missing. Platform-specific means code that runs only there (a branch on
   `os.name` / `sys.platform`, `creationflags`, `msvcrt`, `SIGBREAK`,
   scheduled tasks or launchd, the `.ps1` launchers), or shared code whose
   behaviour rests on OS process semantics (signals, terminate and kill,
   file locks, detached sessions).
   Documentation-only, test-only and review-fix changes, and integration
   batches, follow the narrower rules below.

   For a required full run, finish review fixes and targeted validation
   before joining the queue, then run
   `HF_HUB_OFFLINE=1 python -m pytest tests/` with the bench Postgres up
   (127.0.0.1:5433), CUDA hidden, and the existing suite lock enabled —
   on a Windows host, in WSL through `pwsh ops/wsl-suite.ps1` (see
   "Running tests"). Keep
   the PostgreSQL authentication preflight: a fresh worktree must have the
   correct bench configuration before queueing. A refused or interrupted run
   is not a pass; a changed imported tree requires a fresh process on the
   revised head, except as "Review fixes after a passing local full suite"
   (under "Running tests") allows.

   Note that **a full run the bench Postgres rejects refuses to start**
   (conftest asks the server before queueing for the suite lock): without
   the test login file, a fresh worktree has no `ops/.env`, or the
   `.example` copy, so the suite resolves the compose default password and
   every PG-backed test would ERROR on setup (1,424 in one 2026-09-27 run).
   The refusal names the fix: the **test login** (`pseudolife-mcp
   test-login create`, once, on the daemon host, by the maintainer) writes
   `~/.pseudolife-mcp/test-pg.env`, a role that creates its own databases
   and cannot connect to the bank, and every checkout of that account
   reads it. **Do not copy `ops/.env` into worktrees**: it holds the bank
   owner's password and the operator bearer tokens (2026-10-04,
   `docs/guide/agent-isolation.md`). A run that still logs in as the bank
   owner, through `ops/.env` or an exported password, prints one line
   saying so. Targeted runs print one line and start.

   Every PR requires passing CI for its current head integrated with current
   master, including the full PostgreSQL job, lite Linux, Windows, macOS and
   required analysis checks. If master has moved since that integration was
   tested, update the branch and wait for fresh CI; rerunning an old job
   tests its old merge commit. The one exception is a disjoint merge-forward
   (see "A disjoint merge-forward" under "Running tests"). Independent review
   remains required, and the maintainer owns the merge.

   Trial from 2026-09-28; the maintainer reassesses on 2026-10-12 against
   the measures in the maintainer's private suite-gate memo of 2026-09-28.
   Amended 2026-10-05 by the maintainer: review fixes, integration batches
   and the daemon-process trigger. The memo found the local full suite's
   unique catches over PRs #391–#450 were two real problems, both schema or
   shared-test-infrastructure changes. Platform failures came from CI, and
   most defects from reviewers. Three full suites on 2026-10-04/05
   (integration batches 11 and 12, and #576 before its review fixes) all
   agreed with CI.
4. **Deploy only via `pseudolife-mcp update`**
   (`pseudolife_memory/update_cli.py`, the one implementation since #460,
   2026-09-29): backup → rollback tag → daemon-only `--no-deps` recreate →
   health → clients. Release mode (no `--checkout`) pulls the newest
   release (GHCR image, PyPI package) and moves the clients to it;
   `--daemon-only` / `--clients-only` split the halves (`--clients-only`
   targets the newest release unless given `--tag <daemon version>`).
   **Merged but unreleased master needs checkout mode**, which rebuilds the
   daemon from the tree: `ops/update.ps1` / `ops/update.sh` wrap
   `--checkout <repo>` and run the checkout's own code. Release mode
   installs published releases only, and a checkout-built daemon reports
   the last release's version, so release mode's "nothing to do" on such a
   host proves nothing. Never `docker compose down -v` — the bank volumes
   are external precisely so that this is survivable, but don't test it.
   **A change under `plugin/` or to the shim needs the client step**:
   checkout mode moves the clients only with `-All` / `--all` and refuses
   `--clients-only` (use `python ops/update_clients.py` afterwards);
   release mode moves them unless `--daemon-only`. The plugin cache and
   the shim are separate installs that a daemon deploy never touches — the
   2026-09-21 deploy ran an hour on the old hooks. Both install beside what
   running sessions use (the plugin's cache is keyed by marketplace commit
   since 2026-09-30, its manifest carrying no version), so no client
   closes; new sessions start on them.
5. **After deploy, verify live**, not just `/health`: exercise the changed path
   through the daemon (an MCP call, a psql check of new DDL).
6. **Install and update finish the job themselves** (see the next section):
   a change may land on master before its setup is automated, but it is
   not released until the installer or `pseudolife-mcp update` does that
   setup. The release procedure checks it.

## Install and update experience

Maintainer rule, 2026-10-05: the installer (`ops/install.sh`,
`ops/install.ps1`) and release-mode `pseudolife-mcp update` do the whole
job. A user should never need to read a guide, edit a config file or run a
follow-up command to finish an install or an update. The 2026-10-04
passkey and test-login changes reached master with manual operator steps,
and the next release is held until the scripts cover them.

These rules govern end-user installs and release-mode `update`. Checkout
mode (`ops/update.*`, `--checkout`) is a contributor tool and keeps item
4's steps. New steps follow the rules from now on; existing paths are
brought in line as they are touched, with no retrofit sweep.

- **A feature that needs host setup ships with that setup automated.** A
  config key, a login or role, a serve or port, a file to copy, a command to
  run once: the installer and update do it, in the same PR or one that lands
  before the release. A manual step written only in the docs or the release
  notes means the feature is not finished. A PR that adds such a step says
  in its body what install and update do about it.
- **Detect, don't ask.** Work out what the host has (tier, local or remote
  daemon, Tailscale, checkout or release) and choose. Ask only when the
  answer cannot be detected, or before a persistent change the user did not
  choose by running the installer: a Tailscale serve, a firewall rule, a
  program the install does not otherwise configure. The installers' existing
  client setup (plugin, Codex hooks, instruction files, autostart) is what
  running them means, and keeps its per-decision flags. Ask once per change,
  y/N with a stated default; a dedicated flag or `--yes` (`-Yes` in
  PowerShell) answers it for non-interactive runs.
- **An unavoidable prompt is plain.** One short sentence on what will happen
  and why, then the question. A step only a person can do (a passkey tap,
  an approval in another app) says exactly where to click and what they
  will see; the script waits for it and checks that it happened.
- **Rerunning is safe and quiet.** On a host that is already set up, a new
  step changes nothing and says so in one line.
- **Unattended runs never hang.** With no terminal (scheduled updates, CI),
  a step inside install or update that needs a person is skipped with one
  line naming the command that finishes it, and the run carries on. A
  standalone command whose whole job is that step (`connect`, `expose`)
  refuses instead, saying nothing changed and naming `--yes`.
- **A refusal names its fix and leaves nothing half-done.** Check before
  changing anything, or undo on failure, and print the one command or edit
  that fixes it.
- **Contributor-only setup stays out of end-user installs.** The test login
  is created only by the installers' `--test-login` / `-TestLogin` and by
  checkout-mode update (the latter in progress, 2026-10-05), and checkout
  builds only by checkout mode. A default install and release-mode update
  do neither.

## Derived state / caches / indexes

When adding any derived structure over mutable state (an index over band
entries, a cached view of the graph, a memoized score):

- **Enumerate every mutation path first**, including the ones that bypass the
  normal write API: `hydrate_cms` / `load()` / legacy migration append to
  `band.entries` directly and never call `store()`. Grep for the state being
  mutated, not for the API you expect callers to use.
- **State the real workload's read/write interleave and check the maintenance
  policy preserves the win under it.** The daemon's steady state is
  store/search alternation: an invalidate-on-every-write policy rebuilds on
  every read and silently degrades to the cost you were optimizing away.
  Extend-in-place for additions; rebuild only on removals/replacement.
- **List what the replaced code provided implicitly** — iteration order
  (tie-break determinism), containment semantics (`bands=` filters on the band
  that holds the entry, not `entry.bank`, which goes stale across preset
  changes), live-object reads (supersession flags are read at query time).
  Preserve each one or change it consciously and say so in the commit.

## Running tests (exit-code discipline)

Never pipe a test run through a pager (`pytest ... | tail` / `head` /
`Select-Object`) — the pipe replaces the suite's exit code with the pager's,
which shipped two PRs on failing suites. On the maintainer's machine an
untracked hookify rule (`.claude/hookify.block-masked-test-exit.local.md`)
blocks the pattern outright; the convention applies everywhere regardless.
Use the redirect form from the start:

```bash
python -m pytest tests/ > /tmp/pytest-last.log 2>&1; ec=$?; tail -60 /tmp/pytest-last.log; echo "pytest exit: $ec"
```

or `set -o pipefail; ... | tee /tmp/pytest-last.log | tail -60`.

**Dependent local tests.** Select the union of touched test files, tests
reached through reverse imports and fixture dependencies, and tests found by
references to changed paths, module names, commands and configuration keys.
Include relevant cross-cutting guards and affected platform tests. `--lf`
may add previously failing tests; `--co` may verify the inventory; neither
replaces dependency analysis or a passing run. Document dynamic-import,
subprocess and fixture gaps; if they cannot be bounded, take the local-full
path. A broad selection remains subject to the suite lock; do not split it
to bypass admission.

**One full suite at a time per machine** — every session, worktree and
harness (Claude Code, Codex, anything else) — **and CPU-only.** Measured
2026-09-23 on the maintainer's Windows host: a full CPU suite commits ~20 GB
(pytest ~14.9 GB, its spawned test daemons ~4.5 GB) against a ~120 GB commit
limit of which the desktop, WSL and ~75 MCP shims already hold ~100 GB. Two
concurrent suites killed test daemons with os error 1455 ("paging file is too
small") where the same head passed alone, and a GPU run beside two suites
took 143 CUDA OOMs.

- `tests/conftest.py` enforces it. A full run (all of `tests/`, half or more
  of its files, or a `-k`/`-m` that only excludes, like `not slow`) takes
  the lock `~/.pseudolife-mcp/locks/full-suite.lock` and waits its turn,
  naming the holder about once a minute, so start full runs in the
  background. Waiters are served in arrival order: each holds a ticket in
  `full-suite.queue/` beside the lock, and only the earliest live waiter may
  try it (a worktree whose base predates the queue takes no ticket and
  still races for the lock — rebase it). `PSEUDOLIFE_SUITE_LOCK=fail` exits
  instead; `=off` skips the lock (the default on GitHub Actions: one job per
  VM). Targeted runs are never locked. The lock lives in your home
  directory: a WSL or other-user run does not see it.
- **On the maintainer's Windows host, full suites run in WSL** (maintainer
  decision 2026-10-02): `pwsh ops/wsl-suite.ps1` from the worktree, in the
  background, output redirected as below; any arguments go to pytest, none
  means the full suite. Measured that day: a native Windows full suite's
  hook-script tests start Git Bash processes in bursts of 10-32 a second,
  and mouse input stalled 82-347 ms in exactly those seconds while CPU,
  memory, disk and the compositor stayed normal. A WSL run's lock lives in
  the WSL home, and neither `flock` nor `lockf` there sees a Windows
  holder, so that host refuses native Windows full runs:
  `~/.pseudolife-mcp/locks/full-suite.windows` says `refuse`
  (`PSEUDOLIFE_SUITE_WINDOWS=refuse|allow` overrides it; `allow` only for a
  deliberate run with no WSL suite going). Targeted runs on Windows are
  unaffected, and CI's Windows job covers Windows-only behaviour. The
  launcher tests the worktree's committed HEAD from a copy on WSL's own
  filesystem (it refuses uncommitted changes to tracked files: commit
  first), keeps a uv environment per checkout under `~/.venvs/pseudolife`
  in WSL (copies under `~/.cache/pseudolife-suite/work`; each run prunes
  those unused for 3 days or past the 8 most recent, under their locks:
  `PSEUDOLIFE_SUITE_PRUNE_DAYS`, `PSEUDOLIFE_SUITE_KEEP`,
  `PSEUDOLIFE_SUITE_PRUNE=off`), forwards the test login file (`~/.pseudolife-mcp/test-pg.env`;
  only without one does it copy the worktree's `ops/.env`), needs the
  models in WSL's own Hugging Face cache (see `ops/wsl-suite.sh`), and
  forwards the bearer, so the run still shows as the `full-suite` lease; from Windows,
  `lease check full-suite` sees it through the board only (its local-lock
  line stays free). `PSEUDOLIFE_SUITE_LOCK=off` skips the refusal along
  with the lock, so it is never a way to start a Windows full run here.
  Rebase a branch onto master before its first WSL run: code from before
  the WSL database-name tag (2026-10-02) names its databases after a WSL
  pid that Windows runs cannot see, and any Windows pytest session drops
  them mid-run.
- **Two machines, one full suite each** (maintainer decision 2026-10-02):
  the Windows host (WSL) and the maintainer's homelab box each allow one
  full suite, so two can run at once — never two on one machine. From the
  Windows host, start a full run with `pwsh ops/remote-suite.ps1` (in the
  background, output redirected): it tests the committed HEAD on the first
  free machine, local WSL or the box over SSH, and waits for whichever
  frees first; `PSEUDOLIFE_SUITE_WHERE=local|remote` forces one, and a
  remote run needs the commit pushed. Its log and a JSON result (machine,
  commit, exit code, pytest's summary line) land in
  `~/.pseudolife-mcp/suite-results`; cite the machine and commit in the PR.
  The box's address and settings live only in the private
  `~/.pseudolife-mcp/locks/full-suite.remote`, never in the tree.
  - **Each machine mirrors under its own lease**: `full-suite` on the
    Windows host, `full-suite@box` on the box (its `full-suite.lease`
    file; `PSEUDOLIFE_SUITE_LEASE` overrides). Gate CPU- or
    memory-saturating work on the lease of the machine it runs on: `lease
    check full-suite` on the Windows host, `lease check full-suite@box` on
    the box. A suite on the other machine does not load yours.
  - **On the box, suites never touch the live bank's Postgres** (5433):
    the suite user's shells source `~/.config/pseudolife-suite/env`, which
    sets `PSEUDOLIFE_TEST_PG_HOST_PORT=127.0.0.1:5434` (the separate test
    server) and its `PSEUDOLIFE_TEST_PG_PASSWORD`, so every default path —
    per-run databases, the admin URL, reachability probes — uses that
    server. A dispatched run (`PSEUDOLIFE_SUITE_DISPATCHED`) refuses to
    start unless that address names a port other than 5433 and the machine
    has its own lease name (`full-suite@box`), and its pytest then refuses
    any server it would use that holds a production bank, so the live
    bank's container on its Docker-network address (port 5432) is refused
    too (`dispatched_live_bank_refusal` in `tests/pg_defaults.py`). Not a
    fixed `PSEUDOLIFE_TEST_DATABASE_URL`: that shares one database between
    concurrent runs and covers only the fixtures, leaving the default
    paths on 5433.
    Sessions there run `python -m pytest tests/` natively (Linux needs no
    WSL), under the same one-suite lock. Dispatched runs add a systemd
    `MemoryMax=16G` scope, since the box also serves the live daemon, which
    needs password-free sudo that may preserve the environment
    (`NOPASSWD:SETENV:` for `systemd-run`, or `NOPASSWD: ALL`).
- The lock has a slot count, default 1: `PSEUDOLIFE_SUITE_SLOTS`, else
  `~/.pseudolife-mcp/locks/full-suite.slots`. Leave it at 1 on the
  maintainer's host (maintainer decision 2026-09-25 ~19:15). A two-slot
  trial that afternoon ran each suite in ~50 min instead of ~17, with
  load-timeout failures, so two at once finished fewer suites than one after
  another. With more slots, waiters still take free ones in arrival order
  and the waiting notice names every holder.
- The suite sets `CUDA_VISIBLE_DEVICES=-1` itself (`PSEUDOLIFE_TEST_CUDA=1`
  opts back in). Never `""`: on Windows an empty value leaves the GPU usable.
- The lock is mirrored on the board as the lease `full-suite` (since
  2026-09-28): a queued run is a board waiter, a running one the holder,
  with pid, worktree and expected end, and acquiring or releasing sends one
  notice to every peer whose status says `suite=running|queued` or `gpu=`
  (and any peer parked with `park_clear_by: full-suite`). That replaces
  hand-written `SUITE-START` / `SUITE-END` messages: keep
  `suite=running|queued|idle` in your status so the notices reach you, and
  read `pseudolife-mcp lease check full-suite` (or `lease list`) rather
  than asking. The mirror needs the session's bearer in the environment
  pytest starts in; without one it says so once and the lock alone rules.
- **An announcement is a message, not a status line** (2026-09-28), for
  what the lease mirror does not cover: a suite run with
  `PSEUDOLIFE_SUITE_LOCK=off` or without a bearer, a GPU launch outside
  `Start-Qwen`, or any other saturating window. Send it with
  `memory_message` to every peer whose status shows the resource
  (`suite=running`, `suite=queued`, `gpu=`), with your pid, worktree and
  ETA; keep `suite=running|queued|idle` in your own status as well. The
  status is what a peer sees when it looks; the message is what it acts
  on, and a peer may not look until its next turn. Never infer that a
  holder is idle from process stats, a quiet board or an old timestamp:
  ask them. This host's dialect for the served check-in's rules is
  `examples/hook-instructions.md`.
- **A park record does not keep a wake listener alive; the host does.**
  Claude Code's Stop-hook watcher is armed at every turn end and listens
  while the session stays open (14 days; it stopped after 59 minutes before
  2026-10-05), so a plugin session needs no `wait-mail` once its clients
  carry the 2026-10-05 plugin (`update --all` or `ops/update_clients.py`;
  until a session restarts on it, the 59-minute hook still runs). Without the
  plugin's Stop hook, arm a main-session background `pseudolife-mcp
  wait-mail`, but Claude Code stops a background task at the Bash tool's
  `timeout` (30 minutes by default, 2 hours at most): pass the maximum and
  `--timeout 7000`, and re-arm after a ring or timeout. In Codex, use the
  doorbell where the host supports it. Without a durable host path, say
  `next-turn-only` in the park/status and tell the maintainer when an
  urgent dependency cannot wait. The delegate especially: the Roles band
  and a grant say when a role holder has no live listener. Inspect send receipts:
  `rung` means a currently armed path, not that the recipient acted; for
  `no_path`, use recipient host messaging when available (Claude Desktop's
  session `send_message` starts a user turn), otherwise expect its next turn.
- **A host-shaped symptom is broadcast before it is debugged**: a hung
  interpreter, os error 1455, a database refusing its password, a daemon
  that stopped answering, or anything else that fails in a way unrelated to
  your change. Message every active peer first, then debug: on this host it
  is usually breaking their run too (2026-09-28: several sessions timed out
  on one hung `python3` alias and each investigated it alone).
- **CPU- or memory-saturating work never overlaps a full suite**
  (maintainer rule 2026-09-25). That means load or stress repros (CPU
  burners, memory hogs), benchmark sweeps, parallel stress loops, and
  anything else that pegs the CPU or commits several GB (a model server, a
  large in-memory eval) on the maintainer's host. First check no full suite
  is running: `pseudolife-mcp lease check full-suite` exits 1 while one
  holds the lock (any slot), naming its pid, worktree and expected end, and
  `lease check gpu` does the same for the bench server; gate scripts on
  that exit code (0 free; anything else means the check itself failed). A
  run with `PSEUDOLIFE_SUITE_LOCK=off` takes neither the lock nor the
  lease, so no check sees it: also look for `suite=running` in
  `memory_agents` list, which is why that status still matters.
  Announce the window on the board, bound it with a fixed
  duration and a stop switch (a sentinel file), and stop at once if a suite
  starts. On 2026-09-25 a 16-worker × 25-min burner ran at 100% CPU beside
  two full-suite gate runs. Timing flakes, or os error 1455 when commit
  runs out, invalidate gates and send sessions chasing false regressions.
- **Docs-only changes skip the local full suite** (maintainer decision
  2026-09-25). Docs-only means the diff against `origin/master`
  (`git diff --name-only origin/master...`) touches only documentation:
  `*.md` files anywhere (READMEs and their translations, `docs/**`,
  `CHANGELOG.md`, `CONTRIBUTING.md`, `examples/*.md`, `plugin/README.md`,
  skill `.md` files), `llms.txt` / `llms-full.txt`, and non-code files under
  `docs/` such as `docs/atlas/atlas.json`. Any `.py`, `.ps1` or `.sh`, any
  `.json` outside `docs/`, or any change under `tests/`, `ops/`, plugin
  hooks, workflows or packaging means it is NOT docs-only. Run locally
  instead the doc guards (`tests/test_release_ux.py`,
  `tests/test_llms_txt.py`, `tests/test_atlas_currency.py`,
  `tests/test_eval_evidence.py`, `tests/test_i18n_readme.py`) plus every
  test file that names a touched path (`git grep -l <file basename> tests/`).
  CI's full PostgreSQL job (`test`) must still be green before merge.
- **Merging origin/master forward needs no new local full suite** when no
  code conflict is resolved by hand and any local full suite required for
  the branch's own change has already passed. Run the test files covering
  the overlap, plus doc guards when docs overlap, and require fresh CI on
  the updated merge ref (unless the disjoint merge-forward bullet below
  applies). A manually resolved code conflict requires a local
  full suite. Ordinary code branches with no local-full requirement do not
  acquire one merely by merging master forward. A pass carried forward
  under the review-fix rule below counts as the branch's passed run.
- **A disjoint merge-forward needs no fresh pre-merge CI** (maintainer
  decision 2026-10-05, after #592 waited 25 minutes on a CHANGELOG-only
  conflict). It never applies to `integrate/*` branches: their CI is the
  only combined check of their PRs. All of these must hold:
  - The PR's previous head passed `test`, `test-lite-linux`,
    `test-lite-windows`, `test-lite-macos`, `frontend` and CodeQL. The
    merge-forward commit is the only commit pushed since; a review fix
    pushed with it needs its own CI.
  - The two sides share no file except docs-only files, as the "Docs-only
    changes" bullet defines them. Compute it, don't eyeball it:
    `base=$(git merge-base <previous head> origin/master)`. Master's set is
    `git diff --name-only $base..origin/master`, and the PR's set is
    `git diff --name-only $base...<previous head>`.
  - No hand resolution outside docs: `git show --remerge-diff --name-only
    <merge commit>` lists only docs-only files. `llms-full.txt` is
    regenerated with `python ops/gen_llms_txt.py`, never hand-merged.
  - Neither side changes shared ground:
    - `pyproject.toml`, lock files or `.github/workflows/`;
    - `tests/conftest.py`, fixtures or imported test helpers;
    - schema, DDL or migrations;
    - generated outputs (`pseudolife_memory/web/static/`).
  - Neither side's code reaches the other's only through a dynamic import,
    a subprocess, or HTTP or MCP calls into a daemon whose behaviour the
    other side changed.
  - On the merged head, run locally every test file either side added or
    changed since `$base`, plus the doc guards and every test naming a
    touched doc path. All must pass. Read overlapping doc hunks for
    contradictions; a clean doc auto-merge can still contradict itself.

  Merging before `test` reports on the new head is a ruleset bypass (admin
  merge), so only the maintainer or the maintainer's delegate does it. The
  PR comment says "disjoint merge-forward: fresh CI skipped" and gives both
  file sets.

  CI runs again on the push to master (`ci.yml` triggers on it). That run
  is the check, and a cancelled run is not a pass: a later master push
  cancels an earlier run (`cancel-in-progress`). Watch the first master
  run that completes on a commit containing the merge. If it fails, open a
  revert PR at once, or a fix PR when the fix is obvious, and make no more
  disjoint merges until master is green again. When in doubt, wait for CI.
- **Review fixes after a passing local full suite need no new one** unless
  the fixes themselves fall under a class that requires one (shipping
  checklist item 3, including its platform-specific process rule). A review
  fix answers findings on the same PR; a fix that widens the PR's scope is
  a new change. Run the touched and dependent tests for the fixes, and
  require CI on the current merge ref. The PR says "local full suite:
  passed at `<head>`; later commits are review fixes" with the targeted
  runs listed (maintainer decision 2026-10-05, after #576).
- **An integration batch needs no batch-level local full suite** when every
  merge into it is clean (no code conflict resolved by hand) and each PR's
  own required local validation has passed. An integration batch is an
  `integrate/<date>-batch-N` branch that lands several reviewed PRs under
  one CI run. CI on the batch's merge ref is the combined check; run the
  test files covering any overlap, plus the doc guards when docs overlap.
  The PR says "local full suite: not required, clean integration batch". A
  hand-resolved code conflict requires a local full suite on the batch
  head.
- **Test-only changes skip the local full suite** when the diff touches only
  `tests/test_*.py`, non-code test data and optional docs-only files, and
  does not change shared fixture behavior, module-level state affecting
  other files, conftest, suite admission or imported test helpers. Run
  touched and dependent test files locally, plus the docs-only checks when
  applicable, and require current-merge-ref CI. Shared fixtures/helpers and
  uncertain cross-file effects take the local-full path regardless of
  filename.
- **Open the PR while a required local full suite is queued.** Review and CI
  may run in parallel. State “local full suite: queued” with the queued
  head, then record its actual result. Except for a disjoint merge-forward,
  merge only after all required local
  validation and current-merge-ref CI pass. Ordinary code PRs state the
  local selection and “local full suite: not required under the
  ordinary-code rule”. An old-head full pass is not evidence for a changed
  head, except under the review-fix rule above.

## Review discipline

- **Recall precedes review.** Before reviewing code, docs, or a PR, search
  the bank first (`memory_search` + `memory_lesson_search` on the target
  area), then compare what memory says against the files and correct drift
  in both directions — fix stale memory on the spot (`memory_fact_set` +
  `memory_outcome`), and treat memory-vs-file mismatches as review
  findings, not noise. The served session-start block carries the same
  rule for any install; the per-turn memory-change note repeats it on
  turns where memory changed (the plugin's hook, and
  `pseudolife-mcp prompt-hook` for `ops/install-hook.*` installs).
- Every PR gets a review pass before the merge click — `/code-review` medium,
  or a reviewer subagent over the branch diff. The 2026-08-19 transcript audit
  found 1 of 59 merges across seven weeks carried any in-transcript review;
  the pass is not optional because CI was green.
- Perf/cache/index changes get an independent review pass before commit
  (`/code-review` medium, or a reviewer subagent) — the 2026-07-12 slot-index
  audit found three of these classes post-deploy; the pass is cheaper.
- **`plugin/hooks/hooks.json` is what Codex approves.** Codex approves a
  hook by its definition (command, commandWindows, timeout, async and
  statusMessage measured 2026-09-30; the pin also covers matcher, not
  measured), not by the script it runs, so changing one of those fields,
  or adding a handler, makes every Codex
  user approve the hooks again. Put new behaviour in an existing handler's
  script; when a hooks.json change is unavoidable, update the pin in
  `tests/test_codex_hook_launcher.py` and say in the CHANGELOG that Codex
  users approve once more.
- TDD with a watched RED — write the failing test and watch it fail before
  writing the fix; never trust a test you have not seen red. For invalidation
  contracts, spot-check that each hook is load-bearing by disabling it and
  confirming the test goes red (a hook that never fires red is decoration, and
  worth saying so).
- **A tuning constant set or changed for a measured reason carries a comment
  naming the measurement** — what was measured, when, at what scale (the
  `recency_boost_enabled` comment in `utils/config.py` is the shape). Adopt
  as files are touched; no retrofit sweeps — a comment-only pass across the
  tree is merge-conflict noise with no behavior change.
- Eval- or retrieval-affecting changes run `evals/regression_gate.ps1`
  before commit (pinned replicated slice vs committed baseline; exit 1 =
  regression). Extraction/dream-path changes re-run the ladder instead —
  the gate deliberately does not cover them.
- **A comparator defined as "current/production X" is resolved from the
  deployed config** — grep `ops/` (launchers, compose, scheduled tasks) and
  say where you read it — never from a memory record (dated at write time)
  or from whichever variant has the most baseline artifacts. The 2026-07-26
  extractor smoke nearly ran against the v1 prompt because the v1 baseline
  existed and a 07-11 memory said "v1 deployed", while
  `ops/install-shim-autostart.ps1` had defaulted to v2 since 07-21.
- **Never hand-roll the GPU bench server launch — dot-source
  `evals/qwen_server.ps1`** and call `Start-Qwen` (or `Start-Qwen -Fast`).
  It owns the eval env protocol and the config choice, which is not a
  preference: `run-server-turboq.bat`'s fused TBQ4_0 flash-attention KV is
  not bit-reproducible (identical inputs flip ~7% of verdicts, ±0.05 accuracy
  per arm; MTP off and prompt cache off both change nothing —
  `evals/results/judge-determinism-check.json`), while the stock build with
  `--cache-type-k/v q8_0` reproduces exactly. Default is reproducible; `-Fast`
  is only for output that is never judged — it buys 2.4x on long-generation
  work (13.8s vs 33.5s/call) and nothing at all on answer/judge calls
  (0.5s/call either way). Both configs bind :1234, so "something answered the
  probe" is not proof the right one is running; the helper checks and
  replaces. A judged run whose replicates disagree has drifted onto the fast
  server — `replicate.py` warns on exactly that.
- **Every model-vs-model comparison carries a control arm whose input is
  identical across the runs** — the LME `rag` arm is built from raw turns and
  never touches the extractor, so any disagreement there is pure measurement
  noise and bounds what the other arms can claim. Report it next to the
  effect: a delta smaller than the control's spread is not a finding.
  `evals/judge_determinism_check.py` measures the floor directly;
  `evals/analyze_extractor_comparison.py` reports it beside each paired test.

## Extractor model and mode lists

The installer's extractor modes, and the dreamer models its CLI shim modes
offer, are written in several places: `ops/install.sh` (`EXTRACTOR_MODES`,
`CLAUDE_MODELS`, `OPENAI_MODELS` and its menus), `ops/install.ps1` (the same
three lists, its menus and the `-Extractor` ValidateSet), the `Claude
models:` / `OpenAI models:` help text of the shims' autostart scripts
(`ops/install-shim-autostart.*`, `ops/install-codex-shim-autostart.*`), the
"Extractor modes and dreamer models" section of `docs/guide/dreaming.md`,
the Console's `DREAMER_MODELS` (`frontend/src/lib/dreamer.ts`), the
Extractor panel's `extractor_model_override` suggestions
(`pseudolife_memory/web/config_io.py`) and both shims' `/models` lists
(`evals/claude_shim.py`, `evals/codex_shim.py`). The lists are the menu, not
a gate: a CLI shim mode passes a model id they do not hold to the shim
unchanged, with one line saying so, so a new release is usable the day it
ships. When a new Claude or OpenAI model is a real release, add it to every
list in one change; `tests/test_extractor_model_lists.py` fails while any
list differs, and while the places that name a default (the autostart
scripts, the installers' menu entry 1 and non-interactive choice, the guide)
disagree. A new model joins as an option: it becomes the default, and takes
the "recommended" wording, only after a ladder run has measured it (see the
"Eval- or retrieval-affecting changes" bullet under Review discipline), and
the claim it replaces is retired at its old site, not deleted.

## Pull requests

- **Open the body with 1–3 plain-language sentences: the problem as a user
  or maintainer would notice it, then what the change does about it.** Only
  then the dense technical body — evidence with dates, design decisions,
  what was measured and deliberately not shipped, verification. Keep writing
  that body; it is what makes old PRs reconstructable. The lead exists
  because bodies that open with a plan inventory are unreadable to a human
  six months later.
  - Bad lead (PR #137): "Phase 0+1 of the detector-precision plan, grounded
    in the 2026-08-11 full merge-queue triage…" — names the plan step, not
    the problem.
  - Good lead (PR #136): "`dream_run` had no concurrency guard … two
    triggers racing pulled and extracted the same cursor window twice." —
    problem first, in one breath.
- **Titles say why the change matters, not the mechanism**, within the
  existing `type(scope):` convention where one applies. "fix(dream): stop
  concurrent dreams double-extracting the same window" beats "fix(dream):
  add non-blocking guard to dream_run".

## Publishing a benchmark number

Whether a number is *right* needs a GPU and stays a human gate. Whether it
is *backed* is pure parsing, and `tests/test_eval_evidence.py` enforces it —
add a row there in the same change that adds the claim to the docs.

- **Commit the artifact with the claim.** A number whose evidence lives only
  in a terminal or an untracked working-copy file was never really measured:
  nothing contradicts it, so no guard test and no currency pass will ever
  surface it. Both audits (2026-07-17, 2026-07-21) found this same failure —
  the band-ablation significance table shipped with all five replicates
  untracked and no comparison artifact at all.
- **Every bench writes a file.** `replicate.py compare` and
  `lesson_synthesis_bench.py` both took `--out` retroactively because they
  printed and forgot. New harnesses persist by default.
- **A p-value needs its own artifact** — an aggregate of means cannot
  justify a significance claim.
- **Retire numbers at the old site, not just the new one.** The retired
  0.705 came back in a CHANGELOG entry written *after* its retirement,
  because the retirement note lived in a different file. Mark the superseded
  number where a reader will meet it.
- **Never overwrite a canonical result file on a rerun** — tag the run and
  promote deliberately. A v2 prompt run silently rewrote `sonnet-5.json`
  while also writing its own tagged file (2026-07-21).
- **A bench-instrument migration blocks the release gate until the
  docs-currency pass lands.** The judge and the answerer are terms in every
  published accuracy, so swapping either invalidates the docs the way a
  schema bump invalidates the version tables. The 2026-08-17 Qwen3.6→3.8
  migration shipped with the docs promotion deliberately deferred, and
  v0.14.0 then went out with a README headline (cascade 0.936) that the new
  instrument scores 0.846 — below its own control (#188).
- **Before promoting a claim to the README, run the headline slice under two
  independent judge families.** Determinism is not validity: the 0.936 run
  replicated at std 0.0000 three times and still did not survive a judge
  swap, because reproducibility measures the instrument's steadiness, not
  its agreement with any other instrument. A conclusion that moves when the
  judge moves is a finding about the bench, not about the memory.

## Release / publish procedure (four public surfaces)

Moved to the `release-procedure` project skill
(`.claude/skills/release-procedure/SKILL.md`) — invoke it for ANY release,
version cut, or publish work (GitHub release, PyPI, MCP registry, plugin
marketplace). It carries the docs currency pass and the five-file version
cut; do not release from memory of it.

## Repo hygiene — no PII, ever (public repo)

Anything pushed is public forever: GitHub keeps merged-PR commits reachable
via `refs/pull/*`, which owners cannot purge (Support ticket only) — one
leaked email already cost a full history rewrite plus a fresh-repo publish.

- **Never commit PII or machine identifiers**: emails, OS usernames
  (`C:\Users\<real name>`), hostnames, LAN IPs/subnets, tokens/keys. Docs and
  tests use placeholders (`<user>`, `example.com`) or the synthetic `10.0.0.x`
  examples already in the tree.
- **Extend the guard, don't just scrub**: a removal without a test regresses
  (2026-07-12 lesson). Any newly-spotted identifier class gets added to
  `tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`
  with a watched RED before the scrub.
- **Commit identity stays the GitHub noreply address**
  (`Pseudogiant-xr@users.noreply.github.com`); tee'd script output
  (`deploy-*.log` etc.) stays out of the tree — it embeds absolute home paths.
- **Commit METADATA is a leak channel the guard test can't see**: GitHub
  web-UI edits stamp the account's real email unless Settings → Emails →
  "Keep my email addresses private" is ON (verified on, 2026-07-16 — it was
  off, and one web edit leaked; inspect any unexpected remote commit with
  `git show --format=%ae` before building on it).
- **If a real secret ever lands in a pushed commit: rotate it first.** A
  rewrite is tidiness, not remediation.

## Memory (Pseudolife MCP tools)

Log `memory_outcome` at task end — success/failure/correction signals are the
only feeder for the lessons surfaced at session start. Deploys and eval results
get a `memory_store` with source `pseudolife-mcp` (status chatter →
`source="status"`).

**In-flight work is written, not remembered.** Any session launching
long-running work (an eval run, a training job, a deploy, a sweep) writes a
`source="status"` entry at launch — what was launched, where it runs, when it
should finish — and another when it completes or is abandoned. "What is in
progress" exists only in the bank; the repo cannot answer it. The read side is
symmetric: any question about current status, in-progress work, or what other
sessions are doing is a memory question — `memory_search` (include
`sources=["status"]`) before or alongside git, which only answers what has
landed. This convention is harness-agnostic on purpose: it must hold for any
agent with the MCP tools, not only ones with reminder hooks.

## Glossary

Use these terms — and only these — when describing work back to the
maintainer, in PR bodies, and in commit messages. The point is less that you
understand them (you will infer them) and more that every session describes
the same thing with the same word.

- **continuum / bands** — the associative store's banded layout. Since
  2026-08-15 the default is ONE flat band (`preset: flat`, the measured
  tie from the flat-band verdict); the 8-band `working`→`forever`
  continuum survives as an opt-in preset and its machinery stays in the
  tree. An **entry** is one memory in a band.
- **cortex** — the slot-keyed canonical-fact store: one *current* value per
  `(entity, attribute)` **slot**; supersession, not decay. A **set-valued
  slot** holds many concurrent members (add/remove, not replace).
- **contender** — a competing value parked against a slot instead of
  overwriting it (low-trust dream claims and number-led set adds land here).
- **supersede** — replace a slot's value while keeping the old version as
  audit history. Nothing is ever silently overwritten.
- **freshness_class / stale** — how fast a slot rots: `evergreen` / `slow` /
  `volatile`. `stale: true` (past twice the TTL) means re-verify at the
  source before acting, never "the value is wrong".
- **HLC** — the hybrid logical clock; the monotonic ordering authority for
  supersession. Wall-clock times are display-only.
- **dream** — the consolidation pass: pull the unconsolidated stream →
  extract `(entity, attribute, value)` claims → `fact_set` → advance the
  **cursor** (monotonic; each memory is processed once).
- **session digest** — the mid-density layer: one narrative prose entry
  per closed session episode (`source="digest"`), generated in the idle
  dream cycle, competing in normal dense retrieval. Default-off
  (`digest_enabled`) pending the sidecar quality probe + BEAM verdict.
- **extractor / sidecar** — the model that does dream extraction: the
  bundled CPU sidecar, the Sonnet CLI shim, or any OpenAI-compatible
  endpoint.
- **deep dream** — the separate, manually-triggered full-corpus graph pass:
  safe self-clean plus merge/link candidates routed to review.
- **merge proposal / fold direction / merge veto** — graph entity dedup: a
  proposal folds one entity into another; fold direction says which side
  survives (re-derived from current evidence at review time); a veto is a
  name-shape rule that blocks a bad fold at filing.
- **review queue** — pending graph proposals awaiting accept/reject (the
  Console's Review view).
- **quarantine** — overloaded; qualify it: *serving-side* quarantine is the
  `stale_policy` that withholds a stale value; *consolidation* quarantine is
  the two-man rule parking low-trust dream claims as contenders.
- **world facts** — cited external facts (source URL + quote); a separate
  cortex from project facts.
- **lessons / outcomes** — `memory_outcome` success/failure/correction
  signals, distilled into the do/avoid guidance surfaced at session start.
- **episode** — the session-scoped attribution handle every write is
  stamped with.
- **bank / daemon / shim** — the durable memory volumes; the HTTP service
  that owns them (one process, one bank); the stdio MCP process bridging a
  client to the daemon.
- **principal / tier** — bearer-token caller identity; the principal keys
  the toolset tier.
- **Console** — the web UI (Cortex Console). The **System Atlas**
  (`docs/atlas/`) is the hand-curated *codebase* architecture map — distinct
  from the Console's Graph view, which visualizes the memory bank's graph.
