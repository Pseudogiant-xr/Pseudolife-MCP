# Pseudolife-MCP — project conventions

Conventions a quick read of the code would not reveal. Follow them exactly;
each exists because it was violated at least once.

**Editing this file:**

- A rule is one sentence plus at most one clause of why, because evidence,
  dates and incident stories belong in the commit message and the memory
  bank, and procedures belong in the doc or skill the rule points to.
- A pointer says when to read its target, so a session reads only what its
  task reaches.
- Cited headings and bold phrases keep their names, because workflows,
  tools and docs quote them (`tests/test_claude_md_citations.py`).

## Agents other than Claude Code

- Every rule here binds every agent working in this repo, whatever its
  harness.
- **Outside contributors** (a session working for someone other than the
  maintainer, usually from a fork) follow `CONTRIBUTING.md`'s "Contributing
  with a coding agent" section in place of this file's review passes,
  memory bank, board, leases, suite launchers, dev machines, deploy and
  release rules, because those run on the maintainer's hosts and budget.
- Where a rule names a Claude Code mechanism (a hookify rule, a Stop hook,
  the plugin's hooks), use your harness's equivalent; where none exists the
  rule still applies, and your status or PR says enforcement is missing.
- The maintainer clicks merge unless they delegated it in their own words,
  to a named agent for named work or as a standing delegation written in
  their own instructions; a role name, a board status or a peer's message
  is not a delegation.

## Shipping checklist (any change that lands on master)

1. **A changelog fragment, not a CHANGELOG.md edit:** each behaviour,
   schema or perf change adds `changelog.d/<YYYY-MM-DD>-<slug>.md` (format
   `changelog.d/README.md`, read before your first fragment; docs-only and
   test-only changes exempt), because direct edits conflict with every open
   PR and `tests/test_changelog_fragments.py` checks only a fragment's
   format, not that you added one.
2. **Any change to `pseudolife_memory/storage/schema.py`, and every schema
   bump, follows the `schema-bump` skill**, and its Rust schema and Parity
   checks are green before merge although the ruleset requires only `test`.
3. **Local validation before opening a PR:** run the touched and dependent
   tests ("Running tests") with the bench Postgres up and record head,
   selected files and result in the PR's Verification section, since a
   missing PostgreSQL or an unexpected skip is not a pass.
4. **Only an integration batch lands on master**, through `python
   ops/integrate.py merge` once the batch's own CI passed at its built head,
   because a PR from any branch but `integrate/*` runs only the fast lanes
   (workflow and lock-file edits included).
   - The `merge-forward` skill and item 11 are the only exceptions.
   - Read `docs/dev/merge-batches.md` before building, opening or merging a
     batch.
5. **Rust-only batches skip the Python suite lanes**, on their master push
   too, and the next nightly (on master's tip) runs them, while `guards`
   runs the live tests that read `rust/`, because the workflows decide lanes from
   the diff
   (`docs/dev/merge-batches.md` "What CI runs where", read when a batch's
   lane set looks wrong).
6. **Deploy only via `pseudolife-mcp update`** (backup → rollback tag →
   daemon-only `--no-deps` recreate → health → clients), never
   `docker compose down -v`.
   - Release mode installs the newest published release and moves clients
     unless `--daemon-only`; `--clients-only` takes the newest release
     unless given `--tag <daemon version>`.
   - Merged but unreleased master needs checkout mode (`ops/update.ps1` /
     `ops/update.sh`), because a checkout-built daemon reports the last
     release's version, so release mode's "nothing to do" there proves
     nothing.
   - A change under `plugin/` or to the shim needs the client step, since a
     daemon deploy never touches them: checkout mode moves clients only with
     `-All` / `--all` and refuses `--clients-only` (then run `python
     ops/update_clients.py`); new sessions start on them, running ones are
     untouched.
7. **After deploy, verify live**, not just `/health`: exercise the changed
   path through the daemon (an MCP call, a psql check of new DDL).
8. **A change is not released until install and update do its setup**
   (next section); the release procedure checks it.
9. **A CI change pays for its slots:** one that adds a hosted job, shard or
   setup cost re-measures `rust/ci/ci_budget.json` from its own hosted runs
   and raises a limit only with its reason (`rust/ci/test_ci_budget.py`),
   because every job shares the account's 20 hosted slots; read
   `docs/dev/ci-budget.md` before adding a job or shard.
10. **A Python behaviour change that a `rust/cli_harness` row covers is
    ported to Rust in the same PR, with those rows run locally**, because
    the rows compare head Python with head Rust (read `rust/PARITY.md` to
    find which rows a change reaches) and otherwise fail the batch.
11. **A change the parity judges cover fails their cases by construction
    until its base includes it**, because PR and batch runs use merge-base
    Python as the oracle: the PR lists the failing cases, and only the
    maintainer approves landing that batch red (read
    `evals/rust_port/README.md` before deciding a judges failure is yours).

## Install and update experience

The installers (`ops/install.sh`, `ops/install.ps1`) and release-mode
`pseudolife-mcp update` do the whole job, so a user never reads a guide,
edits a config file or runs a follow-up command. Checkout mode is a
contributor tool and exempt; existing paths are brought in line as they are
touched, with no retrofit sweep.

- **Host setup a feature needs ships automated** (a config key, login,
  serve, port, file, one-time command) in the same PR or one before the
  release, and the PR body says what install and update do about it; a
  manual step only in docs or release notes means unfinished.
- **Detect, don't ask:** a prompt is allowed only for what cannot be
  detected or before a persistent change the user did not choose by running
  the installer, once per decision, y/N with a stated default, answerable by
  a flag or `--yes` / `-Yes`.
- The installers' existing client setup (plugin, Codex hooks, instruction
  files, autostart) is what running them means and keeps its per-decision
  flags.
- Reruns change nothing on a set-up host and say so in one line, because a
  user reruns to repair, not to redo.
- Unattended runs never hang, and a refusal names its fix and leaves
  nothing half-done.
- Read `docs/dev/install-ux.md` before editing the installers, `update`, or
  the standalone `connect` / `expose` commands.
- **Contributor-only setup stays out:** the test login ships only behind
  `--test-login` / `-TestLogin` and checkout-mode update, and checkout
  builds only by checkout mode.

## Runtime experience

- **Friction Pseudolife causes any user or their agent** (MCP tools and
  errors, plugin hooks, CLI output, the Console, the served instructions)
  **is a product bug**, also in our own sessions; host friction (Claude
  Code, Codex, the OS) is out of scope.
- Fix it in its own PR with a regression test when the fix is small,
  otherwise open an issue with the repro and the regression test it needs,
  because folding it into other work hides it.
- A session that cannot open a PR or issue (a subagent, a reviewer) reports
  the repro to its parent or the maintainer.
- Data loss or a security failure is escalated to the maintainer at once.
- **A guard never blocks a legitimate path:** it decides on the exact
  signal it documents, never a text match something else can produce, and
  its tests include the near-miss legitimate call.
- **An error states the true cause and next step** (the reason, the
  parameter or setting, the one fix), because agents act on it literally.
- **The natural guess works or fails helpfully:** after agents make the
  same wrong call twice, change the tool, description or error until that
  guess succeeds or is refused with the right answer.
- **Reproduce before attributing** a misbehaviour to the host.
- **A derived structure over mutable state** (an index, a cached view, a
  memoized score) is added or changed only after enumerating every mutation
  path and stating the real read/write interleave, following the
  `derived-state` skill, because the daemon alternates store and search.

## Running tests

- **Frozen test files:** a run collects only the live files of
  `tests/suite_classes.txt` (`--include-frozen` adds the rest), because the
  frozen package's code no longer changes (`docs/dev/merge-batches.md` "What
  CI runs where" says when the frozen files still run; read it before
  relying on them).
- A new test file adds its line there: a test of a freeze-exception change,
  or of anything that survives, is live; read the header of
  `tests/suite_classes.txt`.
- **Exit codes:** never pipe a test run through a pager, since the pipe
  replaces pytest's exit code; redirect, or use `set -o pipefail; ... | tee
  log | tail -60`:

  ```bash
  python -m pytest tests/ > /tmp/pytest-last.log 2>&1; ec=$?; tail -60 /tmp/pytest-last.log; echo "pytest exit: $ec"
  ```

- **Dependent tests:** select the touched test files plus tests reached by
  reverse imports and fixtures and tests naming the changed paths, modules,
  commands and config keys, with the relevant guards and platform tests;
  `--lf` / `--co` never replace that analysis or a passing run.
- Document dynamic-import, subprocess and fixture gaps, and take the
  full-suite path when they cannot be bounded.
- **Never split a broad selection to dodge the suite lock**, because
  targeted runs never lock and that is exactly the incentive.
- **A local full suite before the PR is required only for** schema, DDL and
  migration changes, shared test infrastructure (conftest, fixtures,
  imported helpers, the suite lock), changes whose coverage cannot be
  bounded, and a hand-resolved code conflict in a merge forward, because the
  batch runs the full suite on every head anyway.
- Every other PR writes "local full suite: not required under the
  ordinary-code rule", and daemon lifecycle changes count as ordinary code
  on Linux.
- Finish review fixes and targeted runs before queueing a required full
  run, because a run on a moving head proves nothing.
- Review fixes after a passing required run need no rerun unless they fall
  in a required class (a scope-widening fix is a new change); the PR says
  "local full suite: passed at `<head>`; later commits are review fixes" and
  lists the targeted runs.
- A merge of master forward needs none when no code conflict was resolved
  by hand and the branch's own required validation passed; run the
  overlap's tests, plus the doc guards if docs overlap.
- A refused or interrupted run is not a pass, and a changed imported tree
  needs a fresh run except under the review-fix rule.
- **Platform-specific changes** (`os.name` / `sys.platform` branches,
  `creationflags`, `msvcrt`, `SIGBREAK`, scheduled tasks, launchd, the
  `.ps1` launchers; code resting on signals, kill, file locks, detached
  sessions; Windows daemon lifecycle) run that platform's affected tests
  natively as a targeted run, and those files are in its CI lite lane
  (`test-lite-windows` / `test-lite-macos`; a freeze-exception change's
  tests are live, so they can be); an unbounded Windows change
  takes the Windows dev VM's native full suite, and macOS has only CI.
- **Docs-only changes** (`git diff --name-only origin/master...` lists only
  `*.md`, `llms.txt` / `llms-full.txt` and non-code files under `docs/`)
  skip the local full suite and run the doc guards (`tests/test_release_ux.py`,
  `test_llms_txt.py`, `test_atlas_currency.py`, `test_eval_evidence.py`,
  `test_i18n_readme.py`) plus every test naming a touched path (`git grep -l
  <basename> tests/`); any `.py`, `.ps1`, `.sh`, `.json` outside `docs/`, or
  anything under `tests/`, `ops/`, plugin hooks, workflows or packaging, is
  not docs-only.
- **Test-only changes** (only `tests/test_*.py`, non-code test data and
  docs) skip the local full suite unless they touch a shared fixture,
  module-level state,
  conftest, suite admission or an imported helper, or their cross-file
  effect is uncertain.
- **An integration batch** runs the local WSL full suite, the Rust harness
  run `build` prints and, when it changes `rust/`, `cargo check`, on its
  head before `open` pushes it, because that costs less than one CI run and
  the suite never collects `rust/`.
- Nothing is fixed inside a batch except the one generated census commit
  (a conflicting member or a red batch's culprit is dropped with a comment),
  and it merges with a merge commit, never squash, so GitHub marks its
  members merged.
- **A batch is built when none is open and two or more PRs are ready, or a
  lone PR has waited about one hosted-run cycle or is urgent or a release
  cut**, because a batch of one pays a full hour of hosted CI for one
  change.
- **On the maintainer's Windows host, full suites run in WSL** (`pwsh
  ops/wsl-suite.ps1`, or `pwsh ops/remote-suite.ps1` for the first free of
  WSL and the Linux box), backgrounded and redirected, because native ones
  stall the desktop and the host refuses them (`full-suite.windows`:
  `refuse`).
- `PSEUDOLIFE_SUITE_LOCK=off` is never a way around that refusal, because
  it only skips the queue, not the stall.
- Both launchers test the committed HEAD (commit first; a box run needs it
  pushed), and a PR cites machine and commit from
  `~/.pseudolife-mcp/suite-results`; read `docs/dev/dev-machines.md` before
  a first run on a machine.
- **One full suite per machine at a time**, because two exhaust memory and
  kill test daemons: `tests/conftest.py` queues full runs (all of `tests/`,
  half its files, or an exclude-only `-k` / `-m`) on
  `~/.pseudolife-mcp/locks/full-suite.lock`, targeted runs never lock, and a
  WSL or other-user run does not see your lock.
- The suite runs CPU-only (`CUDA_VISIBLE_DEVICES=-1`, never `""`, which
  leaves the GPU usable on Windows; `PSEUDOLIFE_TEST_CUDA=1` opts in).
- A run the bench Postgres rejects refuses to start; the fix is the
  maintainer's `pseudolife-mcp test-login create`, never copying `ops/.env`
  into a worktree, which holds the bank owner's password.

### Sharing the machine

- Keep `suite=running|queued|idle` and any `gpu=` in your board status,
  because the suite lock is mirrored as the lease `full-suite`
  (`full-suite@<name>` on a dev machine) and notifies peers whose status
  shows `suite=running|queued` or `gpu=` or who parked with `park_clear_by:
  full-suite`.
- A mirrored run needs no hand-written SUITE-START/END; without a bearer
  the mirror says so once and the lock alone rules.
- A run the mirror does not cover (a lock-off or bearer-less run, a GPU
  launch outside `Start-Qwen`, any saturating window) is announced by a
  `memory_message` SUITE-START to every peer whose status shows the
  resource, with pid, worktree, fixed duration or ETA and a stop switch (a
  sentinel file), and ended with a SUITE-END, since peers may not read
  statuses until their next turn.
- **CPU- or memory-saturating work never overlaps a full suite** (stress
  repros, benchmark sweeps, a model server, a large in-memory eval), since
  overlap causes timing flakes and os error 1455.
- Let the lease check run the work (`lease check full-suite -- <command>` on
  this host, `lease check full-suite@<name> -- <command>` on a dev machine,
  nested for `gpu`), because a `;` after a bare check runs the work whatever
  it said.
- Also look for `suite=running` in `memory_agents` list, and stop at once if
  a suite starts.
- **Broadcast a symptom with evidence of shared impact** (a hung
  interpreter, os error 1455, a silent daemon, a failure peers' runs show
  too) by `memory_message` before debugging it, because it is usually
  breaking their runs too; a failure only your change explains is yours.
- Never infer a holder is idle from process stats, a quiet board or an old
  timestamp: ask.
- A park record does not keep a wake listener alive, the host does: without
  a Claude Code Stop hook listening, arm a main-session background
  `pseudolife-mcp wait-mail --timeout 7000` (re-armed after a ring or
  timeout) or the Codex doorbell, or say `next-turn-only` in your park and
  tell the maintainer when an urgent dependency cannot wait.
- Read `examples/hook-instructions.md` before your first board message, for
  the host dialect, the GPU hand-off and what a wake receipt means.

## Dev machines (where work runs)

- The homelab hypervisor carries Linux dev boxes and a Windows dev VM
  (read `docs/dev/dev-machines.md` before first use); names, addresses,
  lease names and database settings stay in private config and the memory
  bank, never in the tree.
- A PR cites a dev-machine run by role ("Windows dev VM", "Linux dev box"),
  never by name or address, with commit, command and result.
- **They are development tools, not merge gates, except that**
  Windows-sensitive Rust changes (`cfg(windows)` / `windows-sys`, process
  spawn/wait/kill, sockets or named pipes, file locks or atomic replace,
  timeouts, Windows parity slices) build and run their affected test
  binaries and harness slices on the VM before CI, an unbounded Windows
  change takes the VM's native full suite, and a second or deterministic
  Windows-only CI failure goes there (next bullets).
- **Never the live bank:** every dev machine tests against its own test
  PostgreSQL, never the live bank's server or the Windows host's bench.
- One full suite per machine under its own lock and `full-suite@<name>`
  lease, two at once on the hypervisor and never three, and your board
  status says which you hold.
- **A Windows-only hosted CI failure gets one rerun** on the current merge
  ref naming the test and its flake issue (open one if none exists), unless
  its signature is deterministic; a second failure, or a deterministic one,
  goes to the VM.
- On the VM, loop the failing test or slice (20 runs to start, CPU load only
  while no other dev machine runs a suite): a reproduction gets a fix PR, a
  confirmed flake a deflake PR, and an unreproduced failure a deflake PR or
  an issue naming the hosted run once it recurs, because 20 clean runs only rule out rates above about 15%.
- **A PR that adds a `windows-latest` test resting on real process
  spawn/wait/kill, sockets or named pipes, file locks or atomic replace, or
  timeouts (a harness `run.py` scenario included; a test that only mocks
  them is exempt), or changes how such a test uses them, merges only after
  20 clean hosted `windows-latest` runs of that test** (the Rust workflow's
  `mode=stress` dispatch), because the VM admits what the hosted runner
  then fails.
- The stress loop allows one replacement per iteration only when jobs API
  history proves setup failed before the test step, with every attempt and
  cause recorded and any test or later failure still failing the loop.
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
  machine, and `cargo clean` your own stale profiles before a big rebuild.
- **Below the floor, stop starting heavy work**, free only your own caches
  and tell the maintainer or your launching session; others' worktrees,
  evidence and caches are not yours, and removing a merged branch's cache
  (by its owner or with the maintainer's go) is recorded (path, size, why).
- **When all of a session's work has merged and nothing is stacked on it,**
  it runs `python ops/prune_worktrees.py --mine --apply` before it ends
  (read `docs/dev/worktree-retention.md` before pruning anything by hand).
- **Watchers include disk:** each heartbeat over a long run reports free
  disk.

## Stopping processes (shared host)

All sessions here share one process table, so stop only processes you can
prove are yours.

- **By the PID recorded at launch, with its tree**, while it still runs: the
  harness's task stop, or `taskkill /PID <pid> /T /F` (Git Bash: `taskkill
  //PID <pid> //T //F`, and the Windows PID is `cat /proc/$!/winpid` because
  `$!` is an MSYS PID; PowerShell: `(Start-Process ... -PassThru).Id`).
- Record the start time at launch (`(Get-Process -Id <pid>).StartTime`) and
  re-check PID and start time just before stopping, because PIDs are reused
  and command lines repeat; if either mismatches or can't be read, don't.
- **By a marker unique to your run** once it left your tree (a run id in
  argv or your own harness home; the shared worktree path is not unique):
  exclude the searcher and its parent, check the matches, stop them by PID.
- A server started by a tool you don't control is stopped through that
  tool, or by asking its owner on the board.
- **Never by image name, shared attributes or a non-proving command-line
  pattern:** `taskkill /IM` or `/FI "IMAGENAME eq ..."`, `Stop-Process
  -Name`, `Get-Process <name> | Stop-Process`, `wmic ... where name=...`,
  `pkill` / `pkill -f`, `killall`, or patterns like `pseudolife_memory.cli
  serve`, `pseudolife-daemon`, `run.py`.

## Review discipline

- **Recall precedes review:** `memory_search` and `memory_lesson_search`
  the target area first and report memory-vs-file mismatches as findings;
  the session that owns the work corrects memory (`memory_fact_set` +
  `memory_outcome`), a reviewer only reports.
- **Every PR gets a review pass before the merge click** (`/code-review`
  medium, or a reviewer subagent over the branch diff); green CI does not
  replace it.
- **Every review pass of a diff touching anything but docs and ordinary
  tests** (source in any language, scripts, hooks, workflows, dependency
  and lock files, `tests/conftest.py` and shared test helpers) **also runs a
  Codex second review** in the background over the same diff, `codex exec
  review --base origin/master -m gpt-6.1-sol -c
  model_reasoning_effort='"high"' -o "$OUT.md" > "$OUT.log" 2>&1` (`$OUT`
  per pass outside the tree, named by the reviewed short sha;
  `--uncommitted` pre-commit, `--commit <sha>` for one commit), because one
  model family misses what another catches; docs-only diffs and test-only
  diffs (ordinary `tests/test_*.py` and test data) skip it.
- Pin model and effort on the command line and check the log header names
  both, and stop a run early only by its own process tree ("Stopping
  processes").
- Verify each Codex finding against the code and record both reviews in
  the PR (or commit body), each finding labelled by source with your
  verdict, disagreements surfaced rather than silently resolved; a missing,
  signed-out or failed Codex run is recorded as a single-reviewer pass.
- **The review-pass and Codex rules bind only the session that owns the
  PR** (the opener,
  the forwarder, or the maintainer's session taking up an outside PR): a
  reviewer of any kind reviews the diff it was given, starts no other
  review, subagent or Codex run, and does not call its own pass
  single-reviewer.
- When Codex authored and owns the PR, one fresh `gpt-6.1-sol` review at
  high effort or above meets both passes, since a second same-model pass
  adds little independence for its cost; a Claude session that owns or
  forwards it still runs both.
- **Under a conductor, the opener reviews once** (its review pass and Codex
  second review, or the one review for a Codex-owned PR) before handing
  over, and the conductor reviews each fix round without a second pair,
  because two pairs on one fix round only duplicate each other; a
  scope-widening or self-initiated change still gets the opener's fresh
  pair.
- **Perf, cache and index changes get an independent review before
  commit** (`/code-review` medium or a reviewer subagent).
- **Codex approves hooks by their `plugin/hooks/hooks.json` definition**
  (command, commandWindows, timeout, async, statusMessage, matcher), so
  changing those or adding a handler makes every Codex user re-approve: put
  new behaviour in an existing handler's script, and if unavoidable update
  the pin in `tests/test_codex_hook_launcher.py` and say so in the changelog
  fragment.
- **New behaviour has a test watched RED before the fix;** for invalidation
  contracts, disable each hook and confirm the test goes red (one that never
  does is decoration: say so).
- **A tuning constant set for a measured reason carries a comment naming
  the measurement** (what, when, scale; see `recency_boost_enabled` in
  `utils/config.py`), added as files are touched, no retrofit sweeps.
- **Eval- or retrieval-affecting changes run `evals/regression_gate.ps1`
  before commit, and extraction or dream-path changes re-run the ladder
  instead**; the bench server, comparator and control-arm rules are "Eval
  discipline" at the top of `evals/README.md`, read before any eval run.
- **A published benchmark number** commits its artifact with the claim and
  adds its row to `tests/test_eval_evidence.py` in the same change.

## Extractor model and mode lists

- The extractor modes and dreamer models are listed in several places
  (installers, shim autostart help, `docs/guide/dreaming.md`, the Console's
  `DREAMER_MODELS`, the Extractor panel, both shims' `/models`) and
  `tests/test_extractor_model_lists.py` fails while any list or named
  default differs.
- A real new Claude or OpenAI release joins every list in one change, as an
  option, and becomes the default or "recommended" only after a ladder run
  measured it, with the claim it replaces retired at its old site, not
  deleted.

## Pull requests

- **Open the body with 1–3 plain-language sentences** (the problem as a
  user or maintainer would notice it, then the change), then the detail
  and the Verification block `.github/PULL_REQUEST_TEMPLATE.md` asks for,
  because a body that opens with a plan inventory is unreadable six months
  later.
- **Titles say why it matters, not the mechanism**, within `type(scope):`.

## Release / publish procedure

Invoke the `release-procedure` skill for any release, version cut or
publish (GitHub release, PyPI, MCP registry, GHCR, plugin marketplace); do
not release from memory of it.

## Repo hygiene — no PII, ever (public repo)

- **Never commit PII or machine identifiers** (emails, OS usernames such as
  `C:\Users\<real name>`, hostnames, LAN IPs or subnets, tokens, keys); use
  the placeholders the guard allows (`<user>`, `example.com`, synthetic
  `10.0.0.x`), because merged-PR commits stay reachable via `refs/pull/*`
  forever.
- **Extend the guard, don't just scrub:** a new identifier class joins
  `tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`
  with a watched RED before the scrub, because the guard has known gaps (no
  generic email scan) and supplements the rule.
- **Commit as the GitHub noreply address**
  (`Pseudogiant-xr@users.noreply.github.com` for the maintainer's sessions;
  an outside contributor uses their own), and keep tee'd script output
  (`deploy-*.log`) out of the tree, since it embeds home paths.
- Check an unexpected remote commit with `git show --format=%ae` before
  building on it, because GitHub web-UI edits stamp the real email unless
  "Keep my email addresses private" is on.
- **A real secret in a pushed commit is rotated first**; a rewrite is
  tidiness, not remediation.

## Memory (Pseudolife MCP tools)

- Log `memory_outcome` at task end, since outcomes alone feed the lessons
  shown at session start; deploys and eval results get a `memory_store`
  with source `pseudolife-mcp`, status chatter `source="status"`.
- **In-flight work is written, not remembered:** launching long-running
  work writes a `source="status"` entry (what, where, expected finish) and
  another when it ends or is abandoned.
- Questions about current status or other sessions' work are memory
  questions: `memory_search` with `sources=["status"]` before or beside git.

## Glossary

Use these terms, and only these, with the maintainer, in PR bodies and in
commit messages; the full list is `docs/dev/glossary.md`, read before a PR
body or commit message that names a memory concept. The terms this file
uses:

- **bank / daemon / shim**: the durable memory volumes; the HTTP service
  owning them (one process, one bank); the stdio MCP bridge to the daemon.
- **conductor**: the maintainer's board role that acts for them on the
  board, held as the lease `conductor:<project>`.
- **episode**: the session-scoped handle every write is stamped with.
- **dream**: consolidation: pull the unconsolidated stream, extract claims,
  `fact_set`, advance the monotonic cursor.
- **cortex / slot / supersede**: the canonical-fact store; one current value
  per `(entity, attribute)`; replacement that keeps the old value as audit
  history.
- **lessons / outcomes**: `memory_outcome` signals distilled into the
  guidance shown at session start.
