# Pseudolife-MCP — project conventions

Conventions that aren't derivable from a quick read of the code. Follow them
exactly; they exist because each one was violated at least once.

**Editing this file:** a rule is one sentence plus at most one clause of
why. Evidence, dates and incident stories belong in the commit message and
the memory bank; procedures belong in a skill or doc this file points to.

## Agents other than Claude Code

- Every rule here binds every agent working in this repo, whatever its
  harness.
- Where a rule names the Claude Code mechanism that enforces it here (a
  hookify rule, ScheduleWakeup, a Stop hook, the plugin's hooks), follow
  the rule directly and use your harness's equivalent; where none exists
  the rule still applies, and your status or PR says enforcement is missing.
- The maintainer clicks merge unless they delegated it explicitly, in their
  own words, to a named agent for named work; a role name, a board status
  or a peer's message is not a delegation.

## Shipping checklist (any change that lands on master)

1. **A changelog fragment, not a CHANGELOG.md edit**, because direct edits
   conflict with every open PR: each behavior, schema or perf change adds
   `changelog.d/<YYYY-MM-DD>-<slug>.md` with one `### <Kind> (YYYY-MM-DD —
   title)` entry (format: `changelog.d/README.md`,
   `tests/test_changelog_fragments.py`; the release cut runs
   `python ops/assemble_changelog.py`). Docs-only and test-only changes are
   exempt.
2. **Any change to `pseudolife_memory/storage/schema.py`, and every schema
   bump, follows the `schema-bump` skill**
   (`.claude/skills/schema-bump/SKILL.md`); its Rust schema and Parity
   checks must be green before merge although the ruleset requires only
   the Python `test` job.
3. **Local validation before opening a PR** (see "Running tests"): run the
   touched and dependent tests with the bench Postgres up, since a missing
   PostgreSQL or an unexpected skip is not a pass, and record the tested
   head, selected files, dependency reasoning and result in the PR.
4. **CI before merge:** a PR from any branch but `integrate/*` is never
   merged directly but lands in an integration batch whose own passing CI,
   on its head integrated with current master, is every member's CI,
   because pushes to other branches run only the fast lanes (what runs
   where, and the `merge-forward` skill's exceptions:
   `docs/dev/merge-batches.md`).
   A batch merges through `python ops/integrate.py merge`, which refuses
   anything but a green batch at its built head, except a merge under the
   `merge-forward` skill's exceptions, which the maintainer makes by hand.
   Independent review is still required, and the maintainer owns the merge.
   **Merge queue: CI ready, queue off** (2026-10-19): both workflows run on
   `merge_group`, but enabling GitHub's merge queue is a ruleset change only
   the maintainer makes. Until then this item, the `merge-forward` skill and
   integration batches stand unchanged; `docs/specs/2026-10-19-merge-queue.md`
   lists the settings, the lanes to make required first, and which rules
   retire once the queue is on.
5. **Rust-only batches skip the Python suite lanes:** a batch whose files
   are under `rust/` plus only `.github/workflows/rust.yml`, `CHANGELOG.md`
   or `changelog.d/` fragments is gated by the Rust workflow (Rust, the
   parity shards its diff reaches and the Parity gate, both OSes) plus
   `guards`, with `test` and the lite lanes skipped, because the full suite
   still runs on every master push and nightly (anything else,
   `evals/rust_port/` included, runs the full suite).
6. **Deploy only via `pseudolife-mcp update`** (backup → rollback tag →
   daemon-only `--no-deps` recreate → health → clients):
   - Release mode installs the newest published release;
     `--daemon-only` / `--clients-only` split it (`--clients-only` takes the
     newest release unless given `--tag <daemon version>`).
   - Merged but unreleased master needs checkout mode (`ops/update.ps1` /
     `ops/update.sh`, i.e. `--checkout <repo>`); a checkout-built daemon
     reports the last release's version, so release mode's "nothing to do"
     there proves nothing.
   - A change under `plugin/` or to the shim needs the client step, since a
     daemon deploy never touches them: checkout mode moves clients only
     with `-All` / `--all` and refuses `--clients-only` (then run `python
     ops/update_clients.py`); release mode moves them unless
     `--daemon-only`. New sessions start on them; running ones are untouched.
   - Never `docker compose down -v`.
7. **After deploy, verify live**, not just `/health`: exercise the changed
   path through the daemon (an MCP call, a psql check of new DDL).
8. **A change is not released until install and update do its setup**
   (next section); the release procedure checks it.

## Install and update experience

The installers (`ops/install.sh`, `ops/install.ps1`) and release-mode
`pseudolife-mcp update` do the whole job: a user never reads a guide, edits
a config file or runs a follow-up command to finish. Checkout mode is a
contributor tool and is exempt; existing paths are brought in line as they
are touched, with no retrofit sweep.

- **Host setup a feature needs ships automated** (a config key, login,
  serve, port, file, one-time command) in the same PR or one before the
  release; a manual step only in docs or release notes means unfinished, and the PR body
  says what install and update do about it.
- **Detect, don't ask** (tier, local or remote daemon, Tailscale, checkout
  or release). Ask only when undetectable, or before a persistent change
  the user did not choose by running the installer (a Tailscale serve, a
  firewall rule, a program the install does not otherwise configure):
  once, y/N with a stated default, answerable by a flag or `--yes` /
  `-Yes`. The installers' existing client setup (plugin, Codex hooks,
  instruction files, autostart) is what running them means and keeps its
  per-decision flags.
- **An unavoidable prompt is plain:** one sentence on what and why, then
  the question; a step only a person can do says where to click and what
  they will see, then waits and checks it happened.
- **Rerunning is safe and quiet:** on a set-up host a step changes nothing
  and says so in one line.
- **Unattended runs never hang:** inside install or update, a step needing
  a person is skipped with one line naming the command that finishes it; a
  standalone command for that step (`connect`, `expose`) refuses, saying
  nothing changed and naming `--yes`.
- **A refusal names its fix and leaves nothing half-done** (check first or
  undo on failure, then print the one fixing command or edit).
- **Contributor-only setup stays out:** the test login is created only by
  `--test-login` / `-TestLogin` and checkout-mode update, and checkout
  builds only by checkout mode.

## Runtime experience (every surface a user or their agent meets)

No user, or agent working for one, should hit friction Pseudolife caused:
MCP tools and errors, plugin hooks, CLI output, the Console, the served
instructions.

- **Pseudolife-caused friction is a product bug**, also in our own
  sessions: fix it with a regression test in its own PR opened before the
  session ends, never folded into other work; a session that cannot open
  one (subagent, reviewer) reports the repro to its parent or the
  maintainer. Log the `memory_outcome` as usual; the lesson records the
  friction, only the code change fixes it. Host friction (Claude Code,
  Codex, the OS) is out of scope.
- **A guard never blocks a legitimate path:** it decides on the exact
  signal it documents, never a text match something else can produce, and
  its tests include the near-miss legitimate call.
- **An error states the true cause and next step** (the reason, the
  parameter or setting, the one fix), because agents act on it literally.
- **The natural guess works or fails helpfully:** after agents make the
  same wrong call twice, change the tool, description or error until that
  guess succeeds or is refused with the right answer.
- **Reproduce before attributing** a misbehaviour to the host (compaction,
  a flake, the network).

## Derived state / caches / indexes

For any derived structure over mutable state (an index over band entries, a
cached graph view, a memoized score):

- **Enumerate every mutation path first**, grepping for the state, not the
  API (`hydrate_cms` / `load()` / legacy migration append to `band.entries`
  without `store()`).
- **State the real read/write interleave and check the maintenance policy
  keeps the win under it:** the daemon alternates store and search, so
  extend in place for additions and rebuild only on removals or
  replacement; invalidate-on-write rebuilds on every read.
- **List what the replaced code provided implicitly** (tie-break iteration
  order; `bands=` filtering on the holding band, not the stale
  `entry.bank`; live reads of supersession flags) and preserve each, or
  change it consciously and say so in the commit.

## Running tests

**Exit codes:** never pipe a test run through a pager (`| tail`, `| head`,
`Select-Object`), since the pipe replaces pytest's exit code; redirect, or
`set -o pipefail; ... | tee log | tail -60`:

```bash
python -m pytest tests/ > /tmp/pytest-last.log 2>&1; ec=$?; tail -60 /tmp/pytest-last.log; echo "pytest exit: $ec"
```

**Dependent tests:** select the touched test files plus tests reached by
reverse imports and fixtures and tests naming the changed paths, modules,
commands and config keys, with the relevant guards and platform tests;
`--lf` / `--co` never replace that analysis or a passing run. Document dynamic-import,
subprocess and fixture gaps, and take the local-full path if they cannot be
bounded. Never split a broad selection to dodge the suite lock.

**When a local full suite is required** (every case still needs CI on the
current merge ref, except as the `merge-forward` skill allows):

- Required for schema/DDL/migration changes, shared test infrastructure
  (conftest, fixtures, imported helpers, the suite lock) and changes whose
  coverage cannot be bounded. Ordinary code needs none; its PR states the
  selection and "local full suite: not required under the ordinary-code
  rule".
- **Platform-specific changes** (OS-only code: `os.name` / `sys.platform`
  branches, `creationflags`, `msvcrt`, `SIGBREAK`, scheduled tasks,
  launchd, the `.ps1` launchers; or code resting on signals, kill, file
  locks, detached sessions) run that platform's affected tests natively as
  a targeted run, and those files must be in its CI lite lane
  (`test-lite-windows` / `test-lite-macos`). Unbounded Windows changes take
  the Windows dev VM's native full suite; macOS has only CI. Daemon
  lifecycle changes count as ordinary code on Linux.
- **Docs-only changes skip it.** Docs-only: `git diff --name-only
  origin/master...` lists only `*.md`, `llms.txt` / `llms-full.txt` and
  non-code files under `docs/`; any `.py`, `.ps1`, `.sh`, `.json` outside
  `docs/`, or anything under `tests/`, `ops/`, plugin hooks, workflows or
  packaging is not. Run the doc guards (`tests/test_release_ux.py`,
  `test_llms_txt.py`, `test_atlas_currency.py`, `test_eval_evidence.py`,
  `test_i18n_readme.py`) and every test naming a touched path (`git grep -l
  <basename> tests/`).
- **Test-only skips it** (only `tests/test_*.py`, non-code test data and
  docs) unless it changes a shared fixture, module-level state, conftest,
  suite admission or an imported helper, or its cross-file effect is
  uncertain.
- **Review fixes after a passing local full suite** need none unless the
  fixes fall in a required class (a scope-widening fix is a new change);
  the PR says "local full suite: passed at `<head>`; later commits are
  review fixes" and lists the targeted runs.
- **A merge of master forward** needs none when no code conflict was
  resolved by hand and the branch's own required validation passed (a
  pass carried under the review-fix rule counts); run the overlap's tests
  (plus doc guards if docs overlap). A hand-resolved code conflict always
  requires one.
- **An integration batch** (`integrate/<date>-batch-N` from `python
  ops/integrate.py build`, over `ready-to-batch` PRs with their review,
  their own local targeted validation and own-head CI that has not
  failed (label once review and targeted runs are done, with own-head CI
  still pending, since the batch's CI is its members' CI), built whenever
  none is open and one is ready, a lone PR or release cut
  as a batch of one) passes the local WSL full
  suite and the `rust/` harness run `build` prints on its head before
  `ops/integrate.py open` pushes it, because since #846 that costs less
  than one CI run and the suite never collects `rust/`; nothing
  is fixed inside a batch (a conflicting member, or the culprit a red
  batch's local bisection finds, is dropped with a comment), and the
  maintainer or the delegated session merges it with a merge commit, never
  squash, so GitHub marks its members merged (the command: item 4).
  Procedure:
  `docs/dev/merge-batches.md`.
- These suite-gate rules (the 2026-09-28 trial with its 2026-10-05
  amendments) are permanent by maintainer decision of 2026-10-19.

**Running one:**

- Finish review fixes and targeted runs, then start `HF_HUB_OFFLINE=1
  python -m pytest tests/` in the background with the bench Postgres
  (127.0.0.1:5433) up, CUDA hidden and the suite lock on. Open the PR while
  it queues ("local full suite: queued" with the head), then record the
  result; merge only after it and CI pass. A refused or interrupted run is
  not a pass, and a changed imported tree needs a fresh run (except under
  the review-fix rule).
- A run the bench Postgres rejects refuses to start; the fix is the test
  login (`pseudolife-mcp test-login create`, once, on the daemon host, by
  the maintainer), whose `~/.pseudolife-mcp/test-pg.env` role cannot open
  the bank; keep that authentication preflight, so a fresh worktree has
  the right bench configuration before it queues. **Never copy `ops/.env`
  into a worktree:** it holds the bank owner's password and operator
  tokens.
- **One full suite per machine at a time, CPU-only,** every harness,
  because two exhaust memory and kill test daemons. `tests/conftest.py`
  queues full runs (all of `tests/`, half its files, or an exclude-only
  `-k` / `-m`) in arrival order on `~/.pseudolife-mcp/locks/full-suite.lock`;
  `PSEUDOLIFE_SUITE_LOCK=fail` exits instead, `=off` skips it (the CI
  default), targeted runs never lock, and a WSL or other-user run does not
  see your lock. Slots (`PSEUDOLIFE_SUITE_SLOTS`) stay at 1 here.
- **On the maintainer's Windows host, full suites run in WSL**, because
  native ones stall the desktop: `pwsh ops/wsl-suite.ps1`, or `pwsh
  ops/remote-suite.ps1` for the first free of WSL and the Linux box,
  backgrounded and redirected; with no arguments they run up to 4 xdist
  workers sized to the machine. Both test the committed HEAD (commit first;
  a box run needs it pushed); cite machine and commit from
  `~/.pseudolife-mcp/suite-results`. The host refuses native Windows full
  runs (`full-suite.windows`: `refuse`); `PSEUDOLIFE_SUITE_WINDOWS=allow`
  is only for a deliberate run with no WSL suite going, and
  `PSEUDOLIFE_SUITE_LOCK=off` is never a way around it. Launcher detail:
  `docs/dev/dev-machines.md`.
- The suite sets `CUDA_VISIBLE_DEVICES=-1` (`PSEUDOLIFE_TEST_CUDA=1` opts
  in); never `""`, which leaves the GPU usable on Windows.

**Sharing the machine:**

- The lock is mirrored on the board as the machine's lease (`full-suite`,
  or `full-suite@<name>` on a dev machine) and notifies peers whose status
  shows `suite=running|queued` or `gpu=` (or parked with
  `park_clear_by: full-suite`), so keep `suite=running|queued|idle` in your
  status and read `pseudolife-mcp lease check full-suite` rather than
  asking or sending hand-written SUITE-START/END messages; without a bearer the mirror says so once and the lock alone
  rules.
- **What the mirror does not cover** (a lock-off or bearer-less run, a GPU
  launch outside `Start-Qwen`, any saturating window) is announced by
  `memory_message` to every peer whose status shows the resource, with pid,
  worktree and ETA, since peers may not read statuses until their next
  turn. Never infer a holder is idle from process stats, a quiet board or
  an old timestamp: ask. Host dialect: `examples/hook-instructions.md`.
- **CPU- or memory-saturating work never overlaps a full suite** (stress
  repros, benchmark sweeps, a model server, a large in-memory eval), since
  it causes timing flakes and os error 1455. Gate it on the lease of the
  machine it runs on (`lease check full-suite` on the Windows host, `lease
  check full-suite@<name>` on a dev machine) and on `lease check gpu` (0
  free, 1 held, anything else a failed check), also look for `suite=running` in
  `memory_agents` list, announce the window with a fixed duration and a
  stop switch (a sentinel file), and stop at once if a suite starts.
- **Broadcast a host-shaped symptom to every active peer before debugging
  it** (a hung interpreter, os error 1455, a database refusing its
  password, a silent daemon, or anything else failing in a way unrelated to
  your change): it is usually breaking their runs too.
- **A park record does not keep a wake listener alive; the host does.**
  The plugin's Stop hook listens while a Claude Code session is open.
  Without it, arm a main-session background `pseudolife-mcp wait-mail`
  (Bash tool's maximum timeout, `--timeout 7000`, re-armed after a ring or
  timeout); in Codex use the doorbell where the host supports it. With no durable path, say
  `next-turn-only` in the park or status and tell the maintainer when an
  urgent dependency cannot wait (the delegate especially: the Roles band
  and a grant show a role holder with no live listener). A `rung` receipt means an armed path, not
  that the recipient acted; on `no_path` use the recipient host's
  messaging (Claude Desktop's `send_message`) or expect its next turn.

## Dev machines (where work runs)

The maintainer's homelab hypervisor carries Linux dev boxes and a Windows
dev VM; `docs/dev/dev-machines.md` says what runs where. Names, addresses,
lease names and database settings stay in private config and the memory
bank ("dev machines homelab"), never in the tree.

- They are development tools, not merge gates: a PR cites a dev-machine
  run by role ("Windows dev VM", "Linux dev box"), never by name or
  address, with commit, command and result.
- **One full suite per machine**, under its own lock and `full-suite@<name>`
  lease; the hypervisor carries two at once, never three, so check that at
  most one other dev machine runs one and say on the board which you hold.
- **Never the live bank:** every dev machine tests against its own test
  PostgreSQL, never the live bank's server or the Windows host's bench.
- **Windows-only CI failures go to the Windows VM before a rerun,** except
  two classes the VM reproduces at a fraction of the hosted rate: a
  `WinError`/`os error 10053` or `10054` loss followed in the same case by
  a `headers-only replay` timeout (issue #830), and a timeout on a step's
  first TLS handshake or first spawn of a binary when later cases in the
  same step passed (issue #855); those rerun once on the current merge ref
  naming the open issue in the PR or merge comment, and a second failure
  takes the VM path. Otherwise loop the failing test or slice there (20
  runs to start; CPU load only while no other dev machine runs a suite). A
  reproduction gets a fix PR, a confirmed flake a deflake PR, and an
  unreproduced failure a deflake PR or issue naming the hosted run once it
  recurs (20 clean runs only rule out rates above about 15%). Rerun
  without a VM attempt only when a deflake PR or issue already names it or
  the VM is busy (say so), on the current merge ref, and name the deflake
  in the PR or merge comment.
- **A PR adding a test that runs on `windows-latest` and rests on process
  spawn/wait/kill, sockets or named pipes, file locks or atomic replace, or
  timeouts (a harness `run.py` scenario included) merges only after 20
  clean hosted `windows-latest` runs of that test** (the Rust workflow's
  `mode=stress` dispatch), because the VM admits what the runner then
  fails (#835: 0 of 525 on the VM, 3 of 5 hosted).
- **Windows-sensitive Rust changes** (`cfg(windows)` / `windows-sys`,
  process spawn/wait/kill, sockets or named pipes, file locks or atomic
  replace, timeouts, Windows parity slices) build on the VM and run the
  affected test binaries and harness slices before CI.
- **No automatic restarts:** patch the VM only when the maintainer asks,
  and restart only the dev machine you use, never the hypervisor or
  another guest.
- Test runs over SSH use the redirect form too (`... > log 2>&1; "exit
  $LASTEXITCODE"` in the VM's PowerShell 7), never a pipe.

## Disk space (builds and long runs)

- **Check free space before** a Rust build, full suite, eval run or bulk
  download (`df -h /`, `Get-PSDrive`) and don't start below 8 GB, because a
  full disk kills runs mid-step.
- **A Rust `target/` is a cache:** prefer one shared `CARGO_TARGET_DIR` per
  machine, and `cargo clean` your own stale profiles before a big rebuild
  (on the Windows dev VM, whose disk is slow, keep the shared
  `CARGO_TARGET_DIR` warm instead of cleaning it).
- **Below the floor, stop starting heavy work**, free only your own caches
  and tell the maintainer or your launching session; others' worktrees,
  evidence and caches are not yours, and removing a merged branch's cache
  (by its owner or with the maintainer's go) is recorded (path, size, why).
- **Watchers include disk:** each heartbeat over a long run reports free
  disk.

## Stopping processes (shared host)

All sessions here share one process table, so stop only processes you can
prove are yours.

- **By the PID recorded at launch, with its tree**, while it still runs:
  the harness's task stop, or `taskkill /PID <pid> /T /F` (Git Bash:
  `taskkill //PID <pid> //T //F` or `MSYS_NO_PATHCONV=1`, and the Windows
  PID is `cat /proc/$!/winpid`; PowerShell: `(Start-Process ...
  -PassThru).Id`). Record its start time (`(Get-Process -Id
  <pid>).StartTime`) and re-check PID and start time just before
  stopping, because PIDs are reused and command lines repeat; if either
  mismatches or can't be read, don't.
- **By a marker unique to your run** once it left your tree (a run id in
  argv or your own harness home; the shared worktree path is not unique):
  exclude the searcher and its parent, check the matches, stop them by PID.
- A server started by a tool you don't control is stopped through that
  tool, or by asking its owner on the board.
- **Never by image name, shared attributes or a non-proving command-line
  pattern:** `taskkill /IM` or `/FI "IMAGENAME eq ..."`, `Stop-Process
  -Name`, `Get-Process <name> | Stop-Process`, `wmic ... where name=...`,
  `Get-CimInstance ... -Filter "Name=..." | Invoke-CimMethod`, `pkill` /
  `pkill -f`, `killall`, or patterns like `pseudolife_memory.cli serve`,
  `pseudolife-daemon`, `run.py`.

## Review discipline

- **Recall precedes review:** first `memory_search` and
  `memory_lesson_search` the target area, fix stale memory on the spot
  (`memory_fact_set` + `memory_outcome`), and report memory-vs-file
  mismatches as findings.
- **Every PR gets a review pass before the merge click** (`/code-review`
  medium, or a reviewer subagent over the branch diff); green CI does not
  replace it.
- **Every review pass also runs a Codex second review alongside it**, in the
  background over the same diff: `codex exec review --base origin/master -m
  gpt-6.1-sol -c model_reasoning_effort='"high"' -o "$OUT.md" > "$OUT.log"
  2>&1`, `$OUT` a per-pass path outside the tree named by the reviewed short
  sha (`--uncommitted` pre-commit, `--commit <sha>` for one commit). Pin
  model and effort on the command line and check the log header names
  both; stop one early only by its own
  process tree. Verify its findings against the code and record both reviews in the PR (or
  commit body), each finding labelled by source with its verdict; a
  missing, signed-out or failed Codex run is recorded as a single-reviewer
  pass.
- **Those two rules bind only the session that owns the PR** (the main
  thread opening or forwarding it). A reviewer of
  any kind reviews the diff it was given, starts no other review, subagent
  or Codex run, and does not call its pass single-reviewer.
- **Perf, cache and index changes get an independent review before
  commit** (`/code-review` medium or a reviewer subagent).
- **Codex approves hooks by their `plugin/hooks/hooks.json` definition**
  (command, commandWindows, timeout, async, statusMessage, matcher), so
  changing those or adding a handler makes every Codex user re-approve: put
  new behaviour in an existing handler's script, and if unavoidable update
  the pin in `tests/test_codex_hook_launcher.py` and say so in the
  changelog fragment.
- **TDD with a watched RED;** for invalidation contracts, disable each hook
  and confirm the test goes red (one that never does is decoration: say so).
- **A tuning constant set for a measured reason carries a comment naming
  the measurement** (what, when, scale; see `recency_boost_enabled` in
  `utils/config.py`), added as files are touched, no retrofit sweeps.
- **Evals:** eval- or retrieval-affecting changes run
  `evals/regression_gate.ps1` before commit (extraction and dream-path
  changes re-run the ladder); never hand-roll the GPU bench server
  (`evals/qwen_server.ps1`, `Start-Qwen`); resolve a "current/production"
  comparator from the deployed config in `ops/`; give every model-vs-model
  comparison an identical-input control arm. Details: "Eval discipline" at
  the top of `evals/README.md`.
- **A published benchmark number** commits its artifact with the claim and
  adds its row to `tests/test_eval_evidence.py` in the same change; the
  other publishing rules are in the same `evals/README.md` section.

## Extractor model and mode lists

The extractor modes and dreamer models are listed in several places (both
installers, the shim autostart help, `docs/guide/dreaming.md`, the
Console's `DREAMER_MODELS`, the Extractor panel's suggestions, both shims'
`/models`); `tests/test_extractor_model_lists.py` fails while any list or
named default differs.

- The lists are a menu, not a gate: a CLI shim mode passes an unlisted
  model id through, with one line saying so.
- A real new Claude or OpenAI release is added to every list in one change.
- A new model joins as an option; it becomes the default or "recommended"
  only after a ladder run measured it, and the claim it replaces is retired
  at its old site, not deleted.

## Pull requests

- **Open the body with 1–3 plain-language sentences:** the problem as a
  user or maintainer would notice it, then what the change does; then the
  dense body (dated evidence, decisions, what was measured and not shipped,
  verification). Bad lead (#137): "Phase 0+1 of the detector-precision
  plan…"; good (#136): "`dream_run` had no concurrency guard … two triggers
  racing pulled and extracted the same cursor window twice."
- **Titles say why it matters, not the mechanism**, within `type(scope):`:
  "fix(dream): stop concurrent dreams double-extracting the same window",
  not "fix(dream): add non-blocking guard to dream_run".

## Release / publish procedure

Invoke the `release-procedure` skill
(`.claude/skills/release-procedure/SKILL.md`) for ANY release, version cut
or publish (GitHub release, PyPI, MCP registry, GHCR, plugin marketplace);
do not release from memory of it.

## Repo hygiene — no PII, ever (public repo)

Anything pushed is public forever: merged-PR commits stay reachable via
`refs/pull/*`, which owners cannot purge.

- **Never commit PII or machine identifiers** (emails, OS usernames such as
  `C:\Users\<real name>`, hostnames, LAN IPs or subnets, tokens, keys); use
  placeholders (`<user>`, `example.com`, synthetic `10.0.0.x`).
- **Extend the guard, don't just scrub:** a new identifier class joins
  `tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`
  with a watched RED before the scrub.
- **Commit as the GitHub noreply address**
  (`Pseudogiant-xr@users.noreply.github.com`), and keep tee'd script output
  (`deploy-*.log`) out of the tree, since it embeds home paths.
- **Commit metadata leaks past the guard:** GitHub web-UI edits stamp the
  real email unless "Keep my email addresses private" is on, so check an
  unexpected remote commit with `git show --format=%ae` before building on
  it.
- **A real secret in a pushed commit is rotated first**; a rewrite is
  tidiness, not remediation.

## Memory (Pseudolife MCP tools)

- Log `memory_outcome` at task end, since outcomes alone feed the lessons
  shown at session start; deploys and eval results get a `memory_store`
  with source `pseudolife-mcp`, status chatter `source="status"`.
- **In-flight work is written, not remembered:** launching long-running
  work (an eval, training, a deploy, a sweep) writes a `source="status"`
  entry (what, where, expected finish), and another when it ends or is
  abandoned.
- Questions about current status or other sessions' work are memory
  questions: `memory_search` with `sources=["status"]` before or beside git,
  which only knows what landed. This binds every agent with the MCP tools.

## Glossary

Use these terms, and only these, with the maintainer, in PR bodies and in
commit messages.

- **continuum / bands**: the associative store's layout; default one flat
  band (`preset: flat`), the 8-band `working`→`forever` continuum opt-in.
  An **entry** is one memory in a band.
- **cortex**: the slot-keyed canonical-fact store, one *current* value per
  `(entity, attribute)` **slot**, superseded not decayed; a **set-valued
  slot** holds many concurrent members (add/remove).
- **contender**: a competing value parked against a slot instead of
  overwriting it (low-trust dream claims, number-led set adds).
- **supersede**: replace a slot's value, keeping the old one as audit
  history; nothing is silently overwritten.
- **freshness_class / stale**: `evergreen` / `slow` / `volatile`; `stale:
  true` (past twice the TTL) means re-verify at the source, never "the
  value is wrong".
- **HLC**: the hybrid logical clock that orders supersession; wall-clock
  times are display-only.
- **dream**: consolidation: pull the unconsolidated stream → extract
  `(entity, attribute, value)` claims → `fact_set` → advance the monotonic
  **cursor**.
- **session digest**: one prose entry per closed session episode
  (`source="digest"`) from the idle dream cycle; default-off
  (`digest_enabled`).
- **extractor / sidecar**: the dream-extraction model: the bundled CPU
  sidecar, the Sonnet CLI shim, or an OpenAI-compatible endpoint.
- **deep dream**: the manual full-corpus graph pass: safe self-clean plus
  merge/link candidates routed to review.
- **merge proposal / fold direction / merge veto**: graph entity dedup; a
  proposal folds one entity into another, fold direction (re-derived at
  review) says which survives, a veto is a name-shape rule blocking a bad
  fold at filing.
- **review queue**: pending graph proposals (the Console's Review view).
- **quarantine**: always qualified: *serving-side* is the `stale_policy`
  withholding a stale value; *consolidation* is the two-man rule parking
  low-trust dream claims as contenders.
- **world facts**: cited external facts (URL + quote), a separate cortex.
- **lessons / outcomes**: `memory_outcome` signals distilled into the
  do/avoid guidance shown at session start.
- **episode**: the session-scoped handle every write is stamped with.
- **bank / daemon / shim**: the durable memory volumes; the HTTP service
  owning them (one process, one bank); the stdio MCP bridge to the daemon.
- **principal / tier**: the bearer-token caller identity, which keys the
  toolset tier.
- **Console**: the web UI (Cortex Console). The **System Atlas**
  (`docs/atlas/`) is the curated codebase architecture map, not the
  Console's Graph view of the bank.
