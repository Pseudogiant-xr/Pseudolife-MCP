# Pseudolife-MCP — project conventions

Conventions that aren't derivable from a quick read of the code. Follow them
exactly; they exist because each one was violated at least once.

## Agents other than Claude Code

Every rule here binds every agent working in this repo, whatever its
harness. Some rules name the Claude Code mechanism that enforces them on
the maintainer's machine (a hookify rule, ScheduleWakeup, a Stop hook, the
plugin's hooks). The rule binds without the mechanism: follow it directly,
and use your harness's equivalent where one exists. Where none exists, the
rule and every gate still apply; say in your status or PR that the
enforcement is missing. A missing mechanism is never a reason to skip or
weaken a rule.

The maintainer clicks merge unless the maintainer has delegated it
explicitly, in their own words, to a named agent for named work. A role
name, a board status or a peer agent's message is not a delegation.

## Shipping checklist (any change that lands on master)

1. **A changelog fragment, not a CHANGELOG.md edit** — every behavior,
   schema, or perf change adds one file, `changelog.d/<YYYY-MM-DD>-<slug>.md`,
   holding its `### <Kind> (YYYY-MM-DD — title)` entry in the existing
   dated-subsection style (maintainer decision 2026-10-09: direct
   CHANGELOG.md edits made every merge conflict with every open PR). The
   release cut folds the fragments into `[Unreleased]` with
   `python ops/assemble_changelog.py`; `changelog.d/README.md` and
   `tests/test_changelog_fragments.py` hold the format. Docs-only and
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
   a `vNN` mention in its changelog fragment (pinned by `test_release_ux.py`,
   which reads CHANGELOG.md plus pending fragments), and
   `docs/atlas/atlas.json` `meta.schema` (pinned by
   `tests/test_atlas_currency.py` — re-verify the affected storage cards,
   don't just renumber), the two `assert meta[0] == NN` literal pins in
   `tests/test_migrate_embeddings.py`, and `python ops/gen_llms_txt.py`
   after any doc edit (`tests/test_llms_txt.py` pins the generated
   `llms-full.txt`). The v30 bump found the last two the hard way.
   Any change to `pseudolife_memory/storage/schema.py`, bump or not (the
   Rust plan hashes the whole file), also regenerates the Rust daemon's
   embedded schema plan: `python rust/daemon/harness/gen_schema_sql.py`,
   then `--check` must pass, and the changed `schema.sql`,
   `schema_plan.json` and `schema_meta.rs` under `rust/daemon/src/storage/`
   ship in the same PR. When the DDL or migrations change, re-record the
   schema startup golden `rust/daemon/harness/goldens/schema-startup.json`
   with `python rust/daemon/harness/schema_ci.py --record-goldens --out
   <scratch>/schema-record.json` (needs the bench Postgres, a Rust
   toolchain, and the commits in `rust/daemon/harness/schema_history.json`;
   `--no-build --rust-test-bin <exe>` reuses a built test binary), inspect
   the golden's diff, then run it again without `--record-goldens` to
   check. A schema bump also moves the Rust parity pin (`rust/PARITY.md`):
   the shim's transfer schema literals (`rust/shim/src/cli/transfer/
   {export,import}.rs`, `transfer.rs`; `tests/test_schema_version.py`
   pins them), and every harness golden that digests the catalog or
   dumps a bank — dream cursors (`rust/daemon/harness/dream_cursors.py
   live --record`), the daemon `run.py` golden rows, graph store and
   principals, and the CLI harness rows whose goldens carry a bank dump
   or an export (`rust/cli_harness/goldens/<row>.{linux,windows}.json`:
   transfer, invite, maintainer at least; `python rust/cli_harness --row
   <row> --record --candidate <shim>`) — re-recorded on both OSes. The
   v56 bump (#807) missed all of these and four Parity jobs failed; the
   CLI harness goldens failed a second round after the first re-record.
   The master ruleset requires only the Python `test` job, so the Rust
   schema and parity checks must be green before merge regardless.
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
   Windows- or macOS-specific, a Linux full suite would not test it.
   Instead, run that platform's affected test files natively as a targeted
   run (for Windows, on the Windows dev VM or as a targeted run on this
   host), and make sure they are in its CI lite lane's file list
   (`test-lite-windows` / `test-lite-macos` in `.github/workflows/ci.yml`),
   adding them if missing. When a Windows-specific change's dependent
   coverage cannot be bounded, the Windows dev VM's native full suite is
   its local full suite (see "Dev machines"). macOS has no dev machine;
   CI's macOS lane stays its only check. Platform-specific means code that
   runs only there (a branch on `os.name` / `sys.platform`,
   `creationflags`, `msvcrt`, `SIGBREAK`, scheduled tasks or launchd, the
   `.ps1` launchers), or shared code whose behaviour rests on OS process
   semantics (signals, terminate and kill, file locks, detached sessions).
   Documentation-only, test-only and review-fix changes, and integration
   batches, follow the narrower rules below.

   For a required full run, finish review fixes and targeted validation
   before joining the queue, then run
   `HF_HUB_OFFLINE=1 python -m pytest tests/` with the bench Postgres up
   (127.0.0.1:5433), CUDA hidden, and the existing suite lock enabled —
   on a Windows host, in WSL through `pwsh ops/wsl-suite.ps1` (see
   "Running tests"). A native Windows full suite on the Windows dev VM
   runs against the VM's embedded PostgreSQL through the wrapper in its
   private config instead (see "Dev machines"); PG-backed tests skipped
   there are not a pass either. Keep
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
   tests its old merge commit. The exceptions are a disjoint merge-forward
   and a tree-identical stack forward (see both under "Running tests").
   Independent review remains required, and the maintainer owns the merge.

   **Rust-only PRs skip the Python suite lanes** (maintainer decision
   2026-10-09). A PR that touches `rust/`, and whose every other changed file
   is `.github/workflows/rust.yml`, `CHANGELOG.md` or a `changelog.d/`
   fragment, changes nothing the
   Python suite covers (`rust/`'s own Python is checked by the Rust
   workflow): CI's `scope` job skips `test` and the three lite lanes (they
   still run if `scope` itself fails), and the `guards` job runs
   every test file that reads `CHANGELOG.md` plus the tracked-tree identifier
   and control-byte scan. Such a PR's gate is the Rust workflow (Rust and
   Parity on both OSes) plus `guards`. The full suite still runs on every
   master push and nightly. Anything outside that allowlist, including
   `evals/rust_port/`, runs the full suite as before.

   **Merge queue: CI ready, queue off** (2026-10-19). Both workflows run on
   `merge_group` and classify a queued group like a PR over its own range,
   so the queue can be enabled without a workflow change. Enabling it is a
   ruleset change only the maintainer makes;
   `docs/specs/2026-10-19-merge-queue.md` lists the settings, how a
   reviewed PR is queued, and which rules here retire once it is on. Until
   the maintainer enables it, every merge rule in this file stands
   unchanged, including the up-to-date CI requirement, the disjoint
   merge-forward, the tree-identical stack forward and integration batches.

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

## Runtime experience (every surface a user or their agent meets)

Maintainer rule, 2026-10-15: a user, or the agent working for them, should
never hit friction that Pseudolife caused. That covers MCP tools and their
errors, the plugin's hooks (session start, per-turn notes, Stop and wake,
PreToolUse guards), CLI output, the Console and the served instructions.
On 2026-10-15 the subagent board guard refused a main session's status
update as a subagent's call. The model had passed an `agent_id` argument
that `memory_agents` does not take, and the guard's pattern for the
subagent marker (a top-level `agent_id` key in the hook input) also matched
keys inside `tool_input`. The session believed the refusal and stopped
updating its status.

- **Friction a Pseudolife surface causes is a product bug**, also when it
  shows up in our own sessions: a confusing error, a false refusal, a hook
  that misfires, or a tool whose shape invites the wrong call is what users
  will hit. Fix it in the product with a regression test, as its own PR
  opened before the session ends, never folded into unrelated work. A
  session that cannot open one (a subagent, a read-only reviewer) reports
  the repro to its parent or the maintainer. Log the `memory_outcome` as
  usual: the lesson records the friction, only the code change fixes it.
  Friction from the host (Claude Code, Codex, the OS) is outside this rule.
- **A guard never blocks a legitimate path.** Anything that denies,
  refuses or rewrites a call decides on the exact signal it documents,
  never on a text match that something else can produce. Its tests include
  the near-miss: the legitimate call that looks most like the forbidden
  one.
- **An error states the true cause and the next step**, the runtime
  counterpart of "A refusal names its fix" above. Agents act on error text
  literally, so a wrong cause is worse than none. Name the real reason, the
  parameter or setting involved, and the one fix.
- **The natural guess works or fails helpfully.** Once agents have made
  the same wrong call twice (an invented parameter, a field echoed back
  from a result), change the tool, its description or its error until that
  guess either succeeds or is refused with the right answer.
- **Reproduce before attributing.** Don't put a Pseudolife misbehaviour down
  to the host (compaction, a flake, the network) until you've reproduced it
  or ruled it out.

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
  `~/.pseudolife-mcp/locks/full-suite.remote`, never in the tree. The
  Windows dev VM (see "Dev machines" below) is a further full-suite
  machine with its own lock and lease; `remote-suite.ps1` does not
  dispatch to it.
  - **Each machine mirrors under its own lease**: `full-suite` on the
    Windows host, `full-suite@box` on the box (its `full-suite.lease`
    file; `PSEUDOLIFE_SUITE_LEASE` overrides). Gate CPU- or
    memory-saturating work on the lease of the machine it runs on: `lease
    check full-suite` on the Windows host, `lease check full-suite@box` on
    the box. A suite on the Windows host does not load a dev machine, nor
    the reverse. The dev machines share the hypervisor's RAM, which
    carries two full suites at once in any mix (two Linux dev boxes, or a
    Linux dev box and the Windows VM), never three (see "Dev machines"
    below).
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
- **A tree-identical stack forward needs no fresh pre-merge CI**
  (maintainer decision 2026-10-16, after squash-merging #771 left #772 in
  conflict with master). Squash-merging the bottom PR of a stack gives
  master one new commit whose content the next PR already holds. Git does
  not see that, so the next PR conflicts with master wherever both added
  lines in the same place. It never applies to `integrate/*` branches.
  The PR may merge on the CI its earlier head already passed when all of
  these hold:
  - Master's only commits since the PR's previous merge base are squashes
    of its parent PRs, each squashed from a head this PR contains, and
    master's tree equals the last of those heads. Check both **before
    forwarding**: `git merge-base --is-ancestor <parent's merged head>
    <tested head>` and `git diff --quiet <parent's merged head>
    origin/master`. If either fails, forward with an ordinary merge,
    never `-s ours` (which would silently drop master's other content),
    and use the disjoint merge-forward rule or wait for fresh CI.
  - Only then forward with `git merge -s ours origin/master`, or by
    resolving every conflict to the PR's own side. The forwarded head's
    tree must be byte-identical to the head that passed CI: `git diff
    --quiet <tested head> <forwarded head>`.
  - That tested head passed every check its gate requires. A failed,
    cancelled or unfinished job on it means waiting for CI on the
    forwarded head; the identical tree only means that run tests the same
    content.

  Merging before CI reports on the forwarded head is a ruleset bypass
  (admin merge), so only the maintainer or the maintainer's delegate does
  it. The merge comment says "tree-identical stack forward: fresh CI
  skipped" and gives the tested head, the forwarded head and the three
  checks. The backstop is the master push runs of both `ci.yml` and
  `rust.yml`: watch the first of each that completes on a commit
  containing the merge, and handle a failure as for a disjoint
  merge-forward.
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

## Dev machines (where work runs)

Besides the maintainer's Windows host and hosted CI, the maintainer's
homelab hypervisor carries dev machines for sessions to use: Linux dev
boxes (the box above and its sibling) and a Windows dev VM. They are
development tools, not merge gates: hosted CI stays the gate, and a PR
cites the machine and commit of any run it relies on. Their names,
addresses, SSH aliases, lease names and test-database settings live only
in private config (the maintainer's `~/.ssh/config`,
`~/.pseudolife-mcp/locks/full-suite.remote`, each machine's
`~/.config/pseudolife-suite/env` and `full-suite.lease`) and the memory
bank (search "dev machines homelab"), never in the tree.

- **Where work goes.**
  - Linux dev boxes: Linux full suites, Rust builds and tests on Linux,
    and long or CPU-heavy Linux work that would otherwise load the
    Windows host. `ops/remote-suite.ps1` dispatches to the one box its
    `full-suite.remote` names. `PSEUDOLIFE_SUITE_REMOTE` can point one
    run at the sibling instead, once the sibling has its own suite env
    and lease file; a dispatched run refuses a machine without its own
    lease name.
  - Windows dev VM: Windows-only flake repro loops (run the one failing
    test many times there, instead of CI round-trips), Windows Rust
    builds under MSVC, and native-Windows behaviour (process ownership
    and shutdown, named pipes, junctions, UNC and device paths). It is
    also where native Windows full suites run, which the maintainer's
    Windows host refuses. Its disk is slow: on 2026-10-10 a cold `cargo
    test --no-run` of one shim integration-test binary took 15 minutes
    there, so keep its build tree warm (its one shared `CARGO_TARGET_DIR`,
    as under "Disk space") rather than cleaning it.
  - Local WSL: the Windows host's own full suites (`ops/wsl-suite.ps1`),
    as above.
  - Hosted CI: the merge gate. Its Windows job runs a curated lite list
    of test files, not the full suite (`.github/workflows/ci.yml`,
    `test-lite-windows`).
- **One full suite per machine**, under that machine's own lock. Each dev
  machine mirrors its suite on the board under its own `full-suite@<name>`
  lease, set in its private `full-suite.lease`; without that file a run
  falls back to `full-suite`, the Windows host's lease, and the board
  mislabels it. **The hypervisor carries two full suites at once, never
  three** (maintainer decision 2026-10-18): two Linux dev boxes, or a
  Linux dev box and the Windows VM, each still one suite per machine. The
  earlier rule that the Windows VM and a Linux box could not overlap is
  retired. Before starting a full suite on a dev machine, check that at
  most one other dev machine is running one (`lease check` on each
  machine's lease, or a running pytest process there), and say on the
  board which one you hold. Nothing enforces this limit:
  `ops/remote-suite.ps1` checks only the machine it dispatches to.
- **Windows CI failures go to the Windows VM before a rerun** (maintainer
  decision 2026-10-18, after two Windows-only parity failures cost an
  hour-long hosted rerun each on 2026-10-17/18). When a check fails only
  on hosted Windows, reproduce it on the VM before rerunning it: loop the
  one failing test or harness slice there (20 runs is a good start; add
  CPU load for a timing failure only while no other dev machine runs a
  full suite, since the load lands on the shared hypervisor). A failure
  that reproduces gets a fix PR; a flake confirmed there gets a deflake
  PR. Twenty clean runs only rule out a failure rate above roughly 15%, so
  a failure the VM cannot reproduce still gets a deflake PR or issue
  naming the hosted run once it recurs. A rerun alone is not the fix.
  Rerun without a fresh VM attempt only when a deflake PR or issue
  already names the same failure, or the VM is busy (say so); the rerun
  must test the current merge ref (a stale run tests its old merge
  commit), and the PR or merge comment names the deflake.
- **Windows-sensitive Rust changes run on the Windows VM before CI.** A
  Rust PR is Windows-sensitive when it touches code that runs only on
  Windows (`cfg(windows)`, `windows-sys`), process spawning, waiting or
  killing, sockets or named pipes, file locks or atomic replacement,
  timeouts and deadlines, or a harness slice the Windows parity shard runs.
  Before it is pushed for CI, it builds on the VM and runs the test
  binaries and harness slices that exercise the changed code, in whichever
  crate (shim, daemon, shared). The hosted Windows runners are the slowest
  lane, so the VM catches there what would otherwise cost an hour-long CI
  round trip. Other Rust changes, docs-only and Linux-only changes skip
  this.
- **Cite dev-machine runs by role** ("Windows dev VM", "Linux dev box"),
  never by name or address, with the commit, the command and the result.
- **Never the live bank.** Every dev machine tests against its own test
  PostgreSQL: the boxes use their separate loopback test server, and the
  Windows VM uses the `[lite]` extra's embedded PostgreSQL on loopback
  inside the VM, started for each test run by a wrapper kept in the VM's
  private config. A Windows SSH session's child processes end with the
  session, so a server started by hand dies when you disconnect. None of
  them may point a test at the live bank's server or the Windows host's
  bench server.
- **No automatic restarts.** The Windows VM's updates are notify-only and
  paused, with automatic restart disabled; patch it only when the
  maintainer asks. Restart only the machine you are using, and only a dev
  machine, never the hypervisor or any other guest.
- The Windows VM's default SSH shell is PowerShell 7. Use the redirect
  form for test runs there too (`... > log 2>&1; "exit $LASTEXITCODE"`),
  never a pipe.

## Disk space (builds and long runs)

A full disk kills a run mid-step and can leave half-written files behind.
It happened twice in two days:
- 2026-10-11: a Linux dev box filled to 100% and a session's Rust build
  died with os error 28.
- 2026-10-12: the sibling box hit 0 bytes free. One worktree's Rust
  `target/` grew from 17 GB to 28 GB while rebuilding after a merge from
  master, and the session crashed mid-task.

- **Check free space before** every Rust build, full suite, eval run or bulk
  download: `df -h /` on Linux, `Get-PSDrive` on Windows (the drive that
  holds your worktree and build tree). Don't start below 8 GB free; one Rust
  workspace build can add 10 GB or more.
- **A Rust `target/` directory is a cache.** It measured 8–28 GB per
  worktree on 2026-10-12. Prefer one shared `CARGO_TARGET_DIR` per machine to
  one per worktree. Before a big rebuild, such as after merging master into a
  long-lived branch, `cargo clean` your own stale profiles (for example
  `--profile dev` when you are working in release).
- **Below the floor, stop starting heavy work.** Free only your own caches,
  and tell the maintainer, or the session that launched you, instead of
  deleting anything else. Another session's worktree, evidence or cache is
  not yours to delete. A merged branch's build cache may be removed by the
  session that owns it, or with the maintainer's go, and the deletion is
  recorded (path, size, why).
- **Watchers include disk.** A heartbeat or watcher that tracks a long run
  reports free disk on each check, so a filling disk shows up before the run
  dies.

## Stopping processes (shared host)

Every session on this host shares one process table, so a kill that selects
more than your own run takes down other sessions' work. Stop only processes
you can prove are yours.

- **By the PID you recorded at launch, with its tree**, while it is still
  running: the harness's task stop, or `taskkill /PID <pid> /T /F`. In Git
  Bash, MSYS rewrites `/PID` into a path, so write `taskkill //PID <pid>
  //T //F` (or prefix `MSYS_NO_PATHCONV=1`). In Git Bash `$!` is not a
  Windows PID; read it with `cat /proc/$!/winpid`. In PowerShell use
  `(Start-Process ... -PassThru).Id`. A PID can be reused once its process
  exits, and an identical command line is not proof either: another session
  may run the same command in a shared worktree. So record the process's
  start time with its PID (PowerShell: `(Get-Process -Id <pid>).StartTime`),
  verify both just before stopping, and do not stop it if either no longer
  matches or cannot be read.
- **By a marker unique to your run** when the process has left your tree (a
  daemon that detached or outlived its launcher). Put a run id in the argv
  or use your own harness home. The worktree path is not unique: reviewers
  and subagents share it. Exclude the searching process and its parent,
  list the matches, check them, then stop them by PID.
- A server started by a tool you do not control is stopped through that
  tool, or by asking its owner on the board.
- **Never stop processes selected only by image name, shared attributes or a
  command-line pattern that does not prove ownership.** That means
  `taskkill /IM` or `/FI "IMAGENAME eq ..."`, `Stop-Process -Name`,
  `Get-Process <name> | Stop-Process`, `wmic process where name=... delete`,
  `Get-CimInstance Win32_Process -Filter "Name=..." | Invoke-CimMethod
  -MethodName Terminate`, `pkill` or `pkill -f`, `killall`. It also means a
  command-line pattern another session's processes could match
  (`pseudolife_memory.cli serve`, `pseudolife-daemon`, `run.py`).

Both shapes happened on 2026-10-09. `taskkill /IM codex.exe` killed every
Codex process on the host, the Codex app and other sessions' turns
included. A command-line pattern meant for one session's harness could also
match other sessions' test daemons. On the maintainer's machine an untracked
hookify rule (`.claude/hookify.block-kill-by-name.local.md`) blocks the
by-name forms in Claude's Bash tool. The rule applies everywhere
regardless, including the PowerShell tool and Codex sessions, which the
hook does not see.

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
- **Every review pass also runs a Codex second review, alongside it, not
  instead of it**: a headless Codex CLI review on GPT-6.1 Sol at high
  effort, started in the background over the same diff —
  `codex exec review --base origin/master -m gpt-6.1-sol -c model_reasoning_effort='"high"' -o "$OUT.md" > "$OUT.log" 2>&1`,
  with `$OUT` a per-pass path outside the working tree (the session
  scratchpad, named by the reviewed short sha so a rerun does not
  overwrite it). Use `--uncommitted` for a pre-commit pass and
  `--commit <sha>` for a single commit. Pin the model and effort on the
  command line, and check the log's header names both. To stop one early,
  kill its own process tree by the PID taken at launch, never by image
  name (see "Stopping processes (shared host)"). Verify
  its findings against the code like a subagent's, and record both reviews
  in the PR (or the commit body when there is no PR yet), each finding
  labelled by source with its verdict. A missing, signed-out or failed
  Codex run is recorded as a single-reviewer pass, never skipped silently.
- **The two review-pass rules above bind only the session that owns the
  PR** — the main thread opening it or forwarding it for merge — which runs
  both the Claude review and the Codex review. A reviewer (a `codex exec
  review` run, a reviewer subagent, any agent asked only to review) reviews
  the diff it was given: it does not start other reviews, subagents or Codex
  runs, and does not call its own pass single-reviewer. (2026-10-18: a Codex
  second reviewer read the bullet above through AGENTS.md and tried to
  launch a nested `codex exec review`; only its sandbox stopped the
  recursion.)
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
